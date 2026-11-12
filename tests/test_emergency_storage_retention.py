from __future__ import annotations

import os
import ast
import json
import shutil
import sqlite3
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import closing, contextmanager
from types import SimpleNamespace

import pytest
from rem_card.app import emergency_standby as standby_module
from rem_card.app.emergency_validation import compute_file_hash

from rem_card.app.emergency_metadata import (
    EmergencySessionMetadata,
    EmergencyStandbyMetadata,
    atomic_write_json,
    metadata_to_dict,
)
from rem_card.app.emergency_paths import (
    ACTIVE_SESSION_METADATA_FILE_NAME,
    archived_dir,
    standby_generation_dir,
    standby_generation_medical_db_path,
    standby_generation_metadata_path,
    standby_generation_settings_db_path,
    standby_metadata_path,
)
from rem_card.app.emergency_standby import (
    DEFAULT_STANDBY_MAX_AGE_DAYS,
    EmergencyStandbyManager,
    EmergencyStandbyRefreshResult,
    standby_status_user_message,
)
from rem_card.app.emergency_store import EmergencyLocalStore


def _manager(root: Path) -> EmergencyStandbyManager:
    manager = EmergencyStandbyManager.__new__(EmergencyStandbyManager)
    manager.root = str(root)
    manager.settings_required = True
    manager.store = EmergencyLocalStore(root=str(root), settings_required=True)
    manager.store.ensure_root_dirs()
    return manager


def _generation(root: Path, generation_id: str) -> EmergencyStandbyMetadata:
    generation_path = Path(standby_generation_dir(str(root), generation_id))
    generation_path.mkdir(parents=True)
    medical_path = Path(
        standby_generation_medical_db_path(str(root), generation_id)
    )
    settings_path = Path(
        standby_generation_settings_db_path(str(root), generation_id)
    )
    medical_path.write_bytes(b"medical")
    settings_path.write_bytes(b"settings")
    now = datetime.now().replace(microsecond=0).isoformat()
    metadata = EmergencyStandbyMetadata(
        standby_id="standby-test",
        created_at=now,
        updated_at=now,
        source_remote_db_path="network-medical.db",
        source_remote_fingerprint={},
        source_settings_db_path="network-settings.db",
        source_settings_fingerprint={},
        remote_last_change_id=1,
        schema_version=1,
        app_version="test",
        medical_db_path=str(medical_path),
        medical_db_hash="hash-medical",
        medical_db_size=medical_path.stat().st_size,
        medical_db_mtime=medical_path.stat().st_mtime,
        settings_db_path=str(settings_path),
        settings_db_hash="hash-settings",
        settings_db_size=settings_path.stat().st_size,
        settings_db_mtime=settings_path.stat().st_mtime,
        quick_check_status="ok",
        settings_quick_check_status="ok",
        validation_status="valid",
        validation_error=None,
        generation_id=generation_id,
        generation_dir=str(generation_path),
    )
    atomic_write_json(
        standby_generation_metadata_path(str(root), generation_id),
        metadata_to_dict(metadata),
    )
    return metadata


def _session(session_id: str, status: str, ended_at: str) -> EmergencySessionMetadata:
    return EmergencySessionMetadata(
        emergency_session_id=session_id,
        status=status,
        created_at=ended_at,
        started_at=ended_at,
        ended_at=ended_at,
        merged_at=ended_at if status == "merged" else None,
        source_machine="test",
        source_windows_user="test",
        source_client_id="test",
        source_role="nurse",
        app_version="test",
        schema_version=1,
        base_remote_db_path="network.db",
        base_remote_fingerprint={},
        base_last_change_id=1,
        base_snapshot_hash="hash",
        base_snapshot_created_at=ended_at,
        standby_last_change_id=1,
        last_observed_remote_change_id=1,
        local_db_path="local.db",
        base_snapshot_path="base.db",
        settings_snapshot_path="settings.db",
        merge_attempt_count=1,
        last_merge_error=None,
        validation_status="valid",
        validation_error=None,
        discarded_at=ended_at if status == "discarded" else None,
    )


def test_generation_cleanup_keeps_current_and_one_previous(tmp_path):
    manager = _manager(tmp_path)
    old = _generation(tmp_path, "gen_old")
    previous = _generation(tmp_path, "gen_previous")
    current = _generation(tmp_path, "gen_current")
    manager.store.write_standby_metadata(current)

    result = manager.cleanup_standby_generations(
        preferred_previous_generation_id=previous.generation_id,
    )

    assert result["removed_generations"] == 1
    assert not Path(old.generation_dir).exists()
    assert Path(previous.generation_dir).exists()
    assert Path(current.generation_dir).exists()


def test_crash_orphan_cannot_grow_generation_count_without_bound(tmp_path):
    manager = _manager(tmp_path)
    current = _generation(tmp_path, "gen_current")
    manager.store.write_standby_metadata(current)
    _generation(tmp_path, "gen_crash_orphan")
    _generation(tmp_path, "gen_older_orphan")

    manager.cleanup_standby_generations()

    remaining = [
        path
        for path in Path(current.generation_dir).parent.iterdir()
        if path.is_dir()
    ]
    assert len(remaining) == 2
    assert Path(current.generation_dir) in remaining


def test_completed_archives_are_removed_after_seven_days_only(tmp_path):
    store = EmergencyLocalStore(root=str(tmp_path))
    store.ensure_root_dirs()
    now = datetime(2026, 7, 27, 12, 0)
    cases = {
        "old_merged": _session(
            "old_merged",
            "merged",
            (now - timedelta(days=8)).isoformat(),
        ),
        "fresh_discarded": _session(
            "fresh_discarded",
            "discarded",
            (now - timedelta(days=6)).isoformat(),
        ),
        "protected_failed": _session(
            "protected_failed",
            "merge_failed",
            (now - timedelta(days=30)).isoformat(),
        ),
    }
    for session_id, metadata in cases.items():
        session_path = Path(archived_dir(str(tmp_path))) / session_id
        session_path.mkdir(parents=True)
        (session_path / "payload.db").write_bytes(b"x" * 32)
        atomic_write_json(
            str(session_path / ACTIVE_SESSION_METADATA_FILE_NAME),
            metadata_to_dict(metadata),
        )

    result = store.cleanup_completed_archived_sessions(
        retention_days=7,
        now=now,
    )

    archive_root = Path(archived_dir(str(tmp_path)))
    assert result["removed_sessions"] == 1
    assert not (archive_root / "old_merged").exists()
    assert (archive_root / "fresh_discarded").exists()
    assert (archive_root / "protected_failed").exists()


def test_temp_cleanup_waits_twenty_four_hours(tmp_path):
    manager = _manager(tmp_path)
    standby_root = Path(standby_metadata_path(str(tmp_path))).parent
    old_staging = standby_root / ".staging.old"
    fresh_staging = standby_root / ".staging.fresh"
    old_staging.mkdir()
    fresh_staging.mkdir()
    now_ts = time.time()
    os.utime(old_staging, (now_ts - 90000, now_ts - 90000))

    removed = manager.cleanup_failed_temp_files(now_ts=now_ts)

    assert removed == 1
    assert not old_staging.exists()
    assert fresh_staging.exists()


def test_expired_standby_validation_keeps_metadata_and_generation(tmp_path):
    manager = _manager(tmp_path)
    metadata = _generation(tmp_path, "gen_expired")
    expired = replace(
        metadata,
        updated_at=(datetime.now() - timedelta(days=DEFAULT_STANDBY_MAX_AGE_DAYS + 1)).isoformat(),
    )
    manager.store.write_standby_metadata(expired)

    result = manager.validate_standby()

    assert result.status == "expired"
    assert not result.ok
    assert result.metadata == expired
    assert Path(standby_metadata_path(str(tmp_path))).is_file()
    assert Path(expired.generation_dir).is_dir()
    assert Path(expired.medical_db_path).is_file()
    assert Path(expired.settings_db_path).is_file()


def test_invalid_or_future_standby_date_is_rejected_without_deletion(tmp_path):
    manager = _manager(tmp_path)
    metadata = _generation(tmp_path, "gen_bad_date")
    for updated_at, expected_reason in (
        ("not-a-date", "invalid"),
        ((datetime.now() + timedelta(hours=1)).isoformat(), "future"),
    ):
        candidate = replace(metadata, updated_at=updated_at)
        manager.store.write_standby_metadata(candidate)

        result = manager.validate_standby()

        assert result.status == "invalid"
        assert expected_reason in result.reason
        assert Path(standby_metadata_path(str(tmp_path))).is_file()
        assert Path(candidate.generation_dir).is_dir()


def test_standby_status_message_reports_age_limit_state_and_known_gap(tmp_path):
    metadata = _generation(tmp_path, "gen_message")
    updated_at = datetime(2026, 9, 10, 8, 30)
    metadata = replace(metadata, updated_at=updated_at.isoformat(), remote_last_change_id=17)
    status = EmergencyStandbyRefreshResult(
        ok=False,
        status="expired",
        reason="standby is older than 3 days",
        metadata=metadata,
    )

    message = standby_status_user_message(
        status,
        last_observed_remote_change_id=20,
        now=datetime(2026, 9, 16, 8, 30),
    )

    assert "10.09.2026 08:30" in message
    assert "Возраст аварийной копии: 6 сут. 0 ч." in message
    assert "Допустимый возраст аварийной копии: 3 сут." in message
    assert "срок актуальности аварийной копии истёк" in message
    assert "в копии отсутствует часть последних изменений" in message


def test_standby_status_message_distinguishes_missing_and_invalid_date(tmp_path):
    missing = standby_status_user_message(
        EmergencyStandbyRefreshResult(False, "missing", "metadata is missing"),
    )
    metadata = replace(_generation(tmp_path, "gen_invalid_message"), updated_at="not-a-date")
    invalid = standby_status_user_message(
        EmergencyStandbyRefreshResult(False, "invalid", "bad date", metadata=metadata),
    )

    assert "аварийная копия отсутствует" in missing
    assert "Дата последнего обновления аварийной копии некорректна" in invalid


@pytest.fixture
def settings_reuse_pair(tmp_path):
    if os.name != "nt":
        pytest.skip("Header reuse uses Windows handle-owned byte locks; POSIX must use source backup")
    from scripts.regression_checks.emergency_standby import _prepare_emergency_standby_manager_fixture

    manager, medical, settings = _prepare_emergency_standby_manager_fixture(str(tmp_path))
    with _closed_sqlite(settings) as conn:
        conn.executescript("CREATE TABLE reuse_probe(value TEXT); INSERT INTO reuse_probe VALUES('initial');")
    first = manager.create_or_refresh_standby(forced=True)
    assert first.ok, first.reason
    assert first.metadata.source_settings_fingerprint[standby_module._SETTINGS_REUSE_PROOF_KEY]
    calls = []
    original_backup = manager._backup_sqlite_to_temp

    def observed_backup(source_path, target_path, *, source):
        calls.append(source)
        return original_backup(source_path, target_path, source=source)

    manager._backup_sqlite_to_temp = observed_backup
    return manager, medical, settings, first.metadata, calls


@contextmanager
def _closed_sqlite(path, **kwargs):
    with closing(sqlite3.connect(path, **kwargs)) as conn:
        with conn:
            yield conn


def _advance_reuse_medical(medical):
    from scripts.regression_checks.emergency_standby import _append_emergency_medical_change

    return _append_emergency_medical_change(medical, entity_id=991)


def test_unchanged_settings_reused_for_new_medical_generation(settings_reuse_pair):
    manager, medical, settings, previous, calls = settings_reuse_pair
    cursor = _advance_reuse_medical(medical)

    result = manager.create_or_refresh_standby()

    assert result.ok, result.reason
    assert calls == ["emergency_medical_standby"]
    assert result.metadata.remote_last_change_id == cursor
    assert result.metadata.generation_id != previous.generation_id
    assert result.metadata.settings_db_path != previous.settings_db_path
    assert os.stat(result.metadata.settings_db_path).st_ino != os.stat(previous.settings_db_path).st_ino
    assert result.metadata.settings_db_hash == previous.settings_db_hash
    assert compute_file_hash(previous.settings_db_path) == previous.settings_db_hash
    assert manager.validate_standby().ok
    status = manager.probe_network_sources()
    assert not manager.should_refresh_standby(cursor, settings_fingerprint=status.settings_validation.fingerprint,
                                             source_schema_version=status.medical_validation.schema_version)
    # The proof connection has closed; it cannot prevent a later settings write.
    with _closed_sqlite(settings, timeout=0.1) as conn:
        conn.execute("UPDATE reuse_probe SET value='updated'")


@pytest.mark.parametrize("change", [
    "settings_change_same_mtime", "settings_rotation", "medical_rotation", "source_path",
    "corrupt_generation", "manifest_mismatch", "legacy_proof", "identity_unavailable",
    "wal", "writer_fence", "inconsistent_header", "counter_regressed",
])
def test_settings_reuse_falls_back_to_backup_on_change_or_uncertainty(settings_reuse_pair, monkeypatch, change):
    manager, medical, settings, previous, calls = settings_reuse_pair
    _advance_reuse_medical(medical)
    wal_connection = None
    if change == "settings_change_same_mtime":
        before = os.stat(settings)
        with _closed_sqlite(settings) as conn:
            conn.execute("UPDATE reuse_probe SET value='updated'")
        os.utime(settings, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif change in {"settings_rotation", "medical_rotation", "source_path"}:
        source = settings if change != "medical_rotation" else medical
        replacement = source + ".replacement"
        shutil.copy2(source, replacement)
        if change == "source_path":
            manager.source_settings_db_path = replacement
        else:
            os.replace(replacement, source)
    elif change == "corrupt_generation":
        Path(previous.settings_db_path).write_bytes(b"invalid old snapshot")
    elif change == "manifest_mismatch":
        path = standby_generation_metadata_path(manager.root, previous.generation_id)
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        payload["settings_db_hash"] = "0" * 64
        atomic_write_json(path, payload)
    elif change == "legacy_proof":
        fingerprint = dict(previous.source_settings_fingerprint)
        fingerprint.pop(standby_module._SETTINGS_REUSE_PROOF_KEY)
        old = replace(previous, source_settings_fingerprint=fingerprint)
        manager.store.write_standby_metadata(old)
        atomic_write_json(standby_generation_metadata_path(manager.root, previous.generation_id), metadata_to_dict(old))
    elif change == "identity_unavailable":
        monkeypatch.setattr(standby_module, "_source_file_identity", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("unknown identity")))
    elif change == "wal":
        wal_connection = sqlite3.connect(settings)
        assert wal_connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        wal_connection.execute("UPDATE reuse_probe SET value='updated'")
        wal_connection.commit()
    elif change == "writer_fence":
        Path(settings).with_name("settings.db.lock").write_text("active", encoding="ascii")
    else:
        with open(settings, "r+b") as handle:
            handle.seek(24)
            counter = int.from_bytes(handle.read(4), "big")
            if change == "counter_regressed":
                handle.seek(24)
                handle.write((counter - 1).to_bytes(4, "big"))
            handle.seek(92)
            handle.write((counter - 1).to_bytes(4, "big"))
    try:
        result = manager.create_or_refresh_standby()
        assert result.ok, result.reason
        assert calls == ["emergency_medical_standby", "emergency_settings_standby"]
        assert manager.validate_standby().ok
        if change in {"settings_change_same_mtime", "wal"}:
            with _closed_sqlite(result.metadata.settings_db_path) as conn:
                assert conn.execute("SELECT value FROM reuse_probe").fetchone()[0] == "updated"
    finally:
        if wal_connection is not None:
            wal_connection.close()


def test_settings_change_during_local_reuse_triggers_fresh_backup(settings_reuse_pair, monkeypatch):
    manager, medical, settings, previous, calls = settings_reuse_pair
    _advance_reuse_medical(medical)
    copy = standby_module.shutil.copy2

    def racing_copy(source, target, *args, **kwargs):
        result = copy(source, target, *args, **kwargs)
        if str(source) == previous.settings_db_path:
            with _closed_sqlite(settings) as conn:
                conn.execute("UPDATE reuse_probe SET value='updated'")
        return result

    monkeypatch.setattr(standby_module.shutil, "copy2", racing_copy)
    result = manager.create_or_refresh_standby()
    assert result.ok, result.reason
    assert calls == ["emergency_medical_standby", "emergency_settings_standby"]
    with _closed_sqlite(result.metadata.settings_db_path) as conn:
        assert conn.execute("SELECT value FROM reuse_probe").fetchone()[0] == "updated"


def test_corrupt_new_reuse_copy_is_rejected_by_full_hash(settings_reuse_pair, monkeypatch):
    manager, medical, _settings, previous, calls = settings_reuse_pair
    _advance_reuse_medical(medical)
    copy = standby_module.shutil.copy2

    def corrupt_copy(source, target, *args, **kwargs):
        result = copy(source, target, *args, **kwargs)
        if str(source) == previous.settings_db_path:
            with open(target, "r+b") as handle:
                handle.seek(-1, os.SEEK_END)
                handle.write(b"!")
        return result

    monkeypatch.setattr(standby_module.shutil, "copy2", corrupt_copy)
    result = manager.create_or_refresh_standby()
    assert result.ok, result.reason
    assert calls == ["emergency_medical_standby", "emergency_settings_standby"]
    assert result.metadata.settings_db_hash == previous.settings_db_hash


def test_settings_proof_is_disabled_on_non_windows_without_touching_source(monkeypatch):
    manager = object.__new__(EmergencyStandbyManager)
    monkeypatch.setattr(standby_module, "os", SimpleNamespace(name="posix"))
    assert manager._settings_source_proof() is None


def test_source_change_during_backup_does_not_seed_false_reuse_proof(settings_reuse_pair):
    manager, medical, settings, _previous, calls = settings_reuse_pair
    _advance_reuse_medical(medical)
    with _closed_sqlite(settings) as conn:
        conn.execute("UPDATE reuse_probe SET value='updated'")
    backup = manager._backup_sqlite_to_temp

    def racing_backup(source, target, *, source_kind=None, **kwargs):
        kind = kwargs.get("source", source_kind)
        result = backup(source, target, source=kind)
        if kind == "emergency_settings_standby":
            with _closed_sqlite(settings) as conn:
                conn.execute("UPDATE reuse_probe SET value='changed'")
        return result

    # Preserve the public helper's keyword name without colliding with its path.
    manager._backup_sqlite_to_temp = lambda source_path, target, *, source: racing_backup(source_path, target, source_kind=source)
    result = manager.create_or_refresh_standby()
    assert result.ok, result.reason
    assert standby_module._SETTINGS_REUSE_PROOF_KEY not in result.metadata.source_settings_fingerprint
    assert "emergency_settings_standby" in calls


@pytest.mark.parametrize("cancel", [False, True])
def test_reused_pair_failure_or_cancel_keeps_previous_atomic_generation(settings_reuse_pair, monkeypatch, cancel):
    manager, medical, settings, previous, calls = settings_reuse_pair
    _advance_reuse_medical(medical)
    if cancel:
        copy = standby_module.shutil.copy2

        def cancelled_copy(source, target, *args, **kwargs):
            if str(source) == previous.settings_db_path:
                raise KeyboardInterrupt("synthetic cancellation")
            return copy(source, target, *args, **kwargs)

        monkeypatch.setattr(standby_module.shutil, "copy2", cancelled_copy)
        with pytest.raises(KeyboardInterrupt):
            manager.create_or_refresh_standby()
    else:
        monkeypatch.setattr(manager.store, "write_standby_metadata", lambda _metadata: (_ for _ in ()).throw(OSError("publication failure")))
        assert not manager.create_or_refresh_standby().ok
    assert manager.store.read_standby_metadata() == previous
    assert compute_file_hash(previous.settings_db_path) == previous.settings_db_hash
    assert list(Path(previous.generation_dir).parent.iterdir()) == [Path(previous.generation_dir)]
    assert not list(Path(standby_metadata_path(manager.root)).parent.glob(".staging.*"))
    assert calls == ["emergency_medical_standby"]
    with _closed_sqlite(settings, timeout=0.1) as conn:
        conn.execute("UPDATE reuse_probe SET value='updated'")


def test_settings_source_header_probe_holds_only_short_real_shared_lock(settings_reuse_pair, monkeypatch):
    manager, _medical, settings, _previous, _calls = settings_reuse_pair
    read_header = standby_module._settings_header_proof
    checked = []

    def check_lock(path):
        with _closed_sqlite(settings, timeout=0.01) as writer:
            writer.execute("UPDATE reuse_probe SET value='updated'")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                writer.commit()
            writer.rollback()
        checked.append(True)
        return read_header(path)

    monkeypatch.setattr(standby_module, "_settings_header_proof", check_lock)
    assert manager._settings_source_proof() is not None
    assert checked == [True]
    with _closed_sqlite(settings, timeout=0.1) as writer:
        writer.execute("UPDATE reuse_probe SET value='updated'")


def test_direct_copy_guard_accepts_only_current_local_generation_protocol():
    from scripts.regression_checks.emergency_standby import _standby_direct_copy_policy

    source_path = Path(standby_module.__file__)
    text = source_path.read_text(encoding="utf-8")
    assert _standby_direct_copy_policy(text, allow_settings_reuse=True) == (True, "ok")
    # The scheduler gets no exception, even for the admitted manager protocol.
    assert not _standby_direct_copy_policy(text)[0]
    scheduler = source_path.with_name("emergency_standby_scheduler.py").read_text(encoding="utf-8")
    assert _standby_direct_copy_policy(scheduler) == (True, "ok")


@pytest.mark.parametrize("old,new", [
    pytest.param("shutil.copy2(previous.settings_db_path, staging_path)",
                 "shutil.copy2(self.source_settings_db_path, staging_path)", id="live-source"),
    pytest.param("shutil.copy2(previous.settings_db_path, staging_path)",
                 "shutil.copy2(previous.settings_db_path, self.source_settings_db_path)", id="live-target"),
    pytest.param("if self._trusted_settings_generation(previous, before, source_status):", "if True:", id="untrusted-generation"),
    pytest.param("old = validate_settings_db_snapshot(previous.settings_db_path)", "old = source_status.settings_validation", id="unvalidated-old"),
    pytest.param("old.file_hash == previous.settings_db_hash", "True", id="missing-old-sha"),
    pytest.param("old.file_size == previous.settings_db_size", "True", id="missing-old-size"),
    pytest.param("copied = self._validate_identical_settings_copy(staging_path, old)", "copied = old", id="unvalidated-target"),
    pytest.param("after = self._settings_source_proof()", "after = before", id="missing-source-recheck"),
    pytest.param("copied is not None and before == after", "copied is not None", id="source-race"),
    pytest.param("expected_settings = standby_generation_settings_db_path(self.root, previous.generation_id)",
                 "expected_settings = self.source_settings_db_path", id="redirect-generation"),
    pytest.param("if metadata_to_dict(manifest) != metadata_to_dict(previous):", "if False:", id="untrusted-manifest"),
    pytest.param("digest = compute_file_hash(path)", "digest = validated.file_hash", id="missing-target-sha"),
    pytest.param('before["size_bytes"] != validated.file_size', "False", id="missing-target-size"),
    pytest.param("before != _source_file_identity(path) or digest != validated.file_hash",
                 "False or digest != validated.file_hash", id="target-race"),
    pytest.param("before = self._settings_source_proof()\n        try:",
                 "before = self._settings_source_proof()\n        staging_path = self.source_settings_db_path\n        try:", id="reassigned-target"),
])
def test_direct_copy_guard_rejects_weakened_generation_protocol(old, new):
    from scripts.regression_checks.emergency_standby import _standby_direct_copy_policy

    text = Path(standby_module.__file__).read_text(encoding="utf-8")
    assert old in text, "mutation must exercise the current safety contract"
    assert not _standby_direct_copy_policy(text.replace(old, new, 1), allow_settings_reuse=True)[0]


@pytest.mark.parametrize("extra", [
    "\ndef unsafe(source, target):\n    shutil.copy2(source, target)\n",
    "\nfrom shutil import copy2 as clone\nclone(live, staging)\n",
    "\nimport shutil as fs\nfs.copyfile(live, staging)\n",
    "\nclone = shutil.copy2\nclone(live, staging)\n",
    "\ngetattr(shutil, 'copy2')(live, staging)\n",
])
def test_direct_copy_guard_rejects_additional_or_aliased_copy(extra):
    from scripts.regression_checks.emergency_standby import _standby_direct_copy_policy

    text = Path(standby_module.__file__).read_text(encoding="utf-8")
    assert not _standby_direct_copy_policy(text + extra, allow_settings_reuse=True)[0]
    assert not _standby_direct_copy_policy(extra)[0]


def test_direct_copy_guard_rejects_copy_before_old_validation():
    from scripts.regression_checks.emergency_standby import _standby_direct_copy_policy

    tree = ast.parse(Path(standby_module.__file__).read_text(encoding="utf-8"))
    stage = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_stage_settings_snapshot")
    trusted = stage.body[1].body[0]
    validation = trusted.body[1]
    copy_statement = validation.body.pop(0)
    trusted.body.insert(0, copy_statement)
    assert not _standby_direct_copy_policy(ast.unparse(tree), allow_settings_reuse=True)[0]
