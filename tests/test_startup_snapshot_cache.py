"""Focused safety tests for persistent startup snapshot reuse."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from contextlib import closing

import pytest

from rem_card.app import startup_snapshot as snapshots
from rem_card.app import startup_snapshot_cache as cache
from rem_card.app.role_admission import ADMISSION_TTL_SEC


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    from rem_card.app import runtime_paths

    monkeypatch.setattr(runtime_paths, "get_local_cache_dir", lambda: str(tmp_path / "cache"))
    secret = b"synthetic user-bound protector"

    def seal(value: bytes) -> bytes:
        return hmac.new(secret, value, hashlib.sha256).digest() + value

    def unseal(value: bytes) -> bytes | None:
        if len(value) < 32:
            return None
        signature, plain = value[:32], value[32:]
        return plain if hmac.compare_digest(signature, hmac.new(secret, plain, hashlib.sha256).digest()) else None

    monkeypatch.setattr(cache, "_seal", seal)
    monkeypatch.setattr(cache, "_unseal", unseal)
    yield
    for path in list(snapshots._owned_workspaces):
        snapshots.discard({"workspace": path})


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "central" / "archiv" / "rao_journal.db"
    path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript("""
            CREATE TABLE example(id INTEGER PRIMARY KEY, value TEXT);
            INSERT INTO example VALUES (1, 'sample');
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO meta VALUES ('db_cycle_started_at', 'cycle-a');
            INSERT INTO meta VALUES ('unified_schema_fastpath_rev', 'revision-a');
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY);
            INSERT INTO schema_migrations VALUES (26);
            CREATE TABLE change_log(
                id INTEGER PRIMARY KEY,
                entity_name TEXT NOT NULL,
                entity_id INTEGER,
                admission_id INTEGER,
                action TEXT NOT NULL,
                changed_at TEXT,
                changed_by TEXT,
                version INTEGER
            );
        """)
    return path


def checked(source) -> dict:
    workspace = snapshots.create_workspace()
    ok, result, corrupt = snapshots.check_snapshot(str(source), workspace)
    assert ok, result
    assert not corrupt
    return json.loads(result)


def test_next_startup_reuses_persisted_validated_master(source):
    first = checked(source)
    snapshots.discard(first["snapshot"])

    second = checked(source)

    assert first["sync_mode"] == "full"
    assert second["sync_mode"] == "cache"
    assert second["full_checked_at_epoch"] == first["full_checked_at_epoch"]
    assert first["snapshot"]["source_stamp"]
    assert second["snapshot"]["source_stamp"] == first["snapshot"]["source_stamp"]
    with closing(sqlite3.connect(second["snapshot"]["path"])) as conn:
        assert conn.execute("SELECT value FROM example").fetchone() == ("sample",)


def test_original_ttl_is_not_renewed_by_cache_reuse(source, monkeypatch):
    first = checked(source)
    original = first["full_checked_at_epoch"]
    snapshots.discard(first["snapshot"])
    monkeypatch.setattr(cache.time, "time", lambda: original + ADMISSION_TTL_SEC - 1)
    second = checked(source)
    assert second["sync_mode"] == "cache"
    assert second["full_checked_at_epoch"] == original
    snapshots.discard(second["snapshot"])

    monkeypatch.setattr(cache.time, "time", lambda: original + ADMISSION_TTL_SEC)
    assert checked(source)["sync_mode"] == "full"


@pytest.mark.parametrize("change", ["identity", "schema", "migration", "cycle", "quarantine"])
def test_live_source_safety_change_denies_cache_reuse(source, change):
    first = checked(source)
    snapshots.discard(first["snapshot"])
    if change == "identity":
        replacement = source.with_name("replacement.db")
        replacement.write_bytes(source.read_bytes())
        os.replace(replacement, source)
    elif change == "quarantine":
        (source.parent.parent / "quarantine").mkdir()
    else:
        with closing(sqlite3.connect(source)) as conn, conn:
            if change == "schema":
                conn.execute("CREATE TABLE added(id INTEGER PRIMARY KEY)")
            elif change == "migration":
                conn.execute("INSERT INTO schema_migrations VALUES (27)")
            else:
                conn.execute("UPDATE meta SET value='cycle-b' WHERE key='db_cycle_started_at'")

    assert checked(source)["sync_mode"] == "full"


@pytest.mark.parametrize("target", ["database", "manifest"])
def test_cache_tampering_fails_closed_to_full_snapshot(source, target):
    first = checked(source)
    snapshots.discard(first["snapshot"])
    db_path, manifest_path = cache.cache_paths(str(source))
    tampered = db_path if target == "database" else manifest_path
    with open(tampered, "ab") as handle:
        handle.write(b"tampered")

    assert checked(source)["sync_mode"] == "full"


def test_unavailable_manifest_protection_disables_reuse_without_blocking_admission(source, monkeypatch):
    first = checked(source)
    snapshots.discard(first["snapshot"])
    monkeypatch.setattr(cache, "_unseal", lambda value: None)
    monkeypatch.setattr(cache, "_seal", lambda value: None)

    result = checked(source)

    assert result["sync_mode"] == "full"
    assert result["snapshot"] is not None


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI is required")
def test_windows_dpapi_round_trip_and_tamper_rejection():
    plain = b'{"format":1,"proof":"synthetic"}'
    sealed = cache._crypt(plain, protect=True)

    assert sealed and sealed != plain
    assert cache._crypt(sealed, protect=False) == plain
    tampered = bytearray(sealed)
    tampered[len(tampered) // 2] ^= 0x01
    assert cache._crypt(bytes(tampered), protect=False) is None
