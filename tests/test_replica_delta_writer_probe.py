"""Real historical reconciliation with slow metadata and publication fences."""
from __future__ import annotations

from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

import pytest

from rem_card.app import local_replica_worker as worker, replica_delta, replica_refresh
from rem_card.app.replica_provenance import remember_source
from rem_card.app.role_admission import file_identity


def _databases(tmp_path, *, changed=True, row_count=12_000):
    source = tmp_path / "central" / "archiv" / "central.db"
    source.parent.mkdir(parents=True)
    local = tmp_path / "local.db"
    with closing(sqlite3.connect(source)) as connection:
        connection.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO meta VALUES('db_cycle_started_at', 'synthetic');
            INSERT INTO meta VALUES('unified_schema_fastpath_rev', '28');
            CREATE TABLE change_log(id INTEGER PRIMARY KEY, entity_name TEXT,
                entity_id INTEGER, admission_id INTEGER, action TEXT,
                changed_at TEXT, changed_by TEXT, version INTEGER);
            CREATE TABLE history(id INTEGER PRIMARY KEY, payload TEXT);
        """)
        connection.executemany("INSERT INTO history VALUES (?, ?)",
                               ((index, "history" * 20) for index in range(1, row_count + 1)))
        connection.commit()
        with closing(sqlite3.connect(local)) as destination:
            connection.backup(destination)
        if changed:
            # Historical mutations without a change_log event must remain
            # visible; a tail-only shortcut would lose both of these changes.
            connection.execute("UPDATE history SET payload='updated' WHERE id=10")
            connection.execute("DELETE FROM history WHERE id=20")
            connection.execute("INSERT INTO history VALUES (?, 'new')", (row_count + 1,))
            connection.commit()
    remember_source(str(local), file_identity(str(source)))
    request = dict(central_db_path=str(source), local_db_path=str(local),
                   temp_db_path=str(tmp_path / "candidate.db"),
                   writer_lock_path=str(source.parent / "db.lock"),
                   rotation_lock_path=str(source.parent / "rotation.lock"),
                   snapshot_lease_path=str(tmp_path / "reader.lock"))
    return source, local, request


def _assert_released(request):
    assert not Path(request["snapshot_lease_path"]).exists()
    assert not Path(worker.replica_snapshot_gate_path(request["central_db_path"])).exists()
    assert not Path(request["temp_db_path"]).exists()
    # No leaked SQLite read transaction may prevent the clinical writer.
    with closing(sqlite3.connect(request["central_db_path"], timeout=0)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE history SET payload=payload WHERE id=1")
        connection.commit()


def test_large_history_preserves_old_updates_and_deletes_with_slow_metadata(tmp_path, monkeypatch):
    source, local, request = _databases(tmp_path)
    original_exists = os.path.exists
    probes = []

    def slow_metadata(path):
        if str(path) in {request["writer_lock_path"], request["rotation_lock_path"]}:
            probes.append(path)
            time.sleep(.001)  # Model metadata latency, rather than SQLite xRead.
        return original_exists(path)

    before = hashlib.sha256(source.read_bytes()).digest()
    local_before = local.read_bytes()
    monkeypatch.setattr(os.path, "exists", slow_metadata)
    result = worker._sync_snapshot_with_network_lease(request)
    assert result["status"] == "snapshot_ready" and result["sync_mode"] == "delta"
    metrics = result["delta_metrics"]
    assert metrics["reconciliation_rows_scanned"] >= 24_000
    assert metrics["coordination_check_count"] >= 12_000
    # A 1 ms metadata call for every row previously caused seconds of delay.
    # Bound the actual costly operations, without a fragile wall-clock limit.
    assert len(probes) < 64
    assert metrics["coordination_probe_count"] < 25
    assert metrics["coordination_probe_ms"] > 0
    with closing(sqlite3.connect(result["temp_db_path"])) as candidate:
        assert candidate.execute("SELECT payload FROM history WHERE id=10").fetchone() == ("updated",)
        assert candidate.execute("SELECT payload FROM history WHERE id=20").fetchone() is None
        assert candidate.execute("SELECT payload FROM history WHERE id=12001").fetchone() == ("new",)
        assert candidate.execute("PRAGMA quick_check").fetchone() == ("ok",)
    assert local.read_bytes() == local_before
    assert hashlib.sha256(source.read_bytes()).digest() == before


def _clock(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(worker, "time", SimpleNamespace(
        monotonic=lambda: clock.now, perf_counter=lambda: clock.now, sleep=time.sleep))
    return clock


@pytest.mark.parametrize("lock_name,error_class", [
    ("writer_lock_path", worker.LocalReplicaWriterBusy),
    ("rotation_lock_path", worker.LocalReplicaRotationBusy),
])
def test_writer_and_rotation_interrupt_historical_scan_with_bounded_poll(
    tmp_path, monkeypatch, lock_name, error_class
):
    _, local, request = _databases(tmp_path)
    clock = _clock(monkeypatch)
    original_key = replica_delta._row_key
    local_before = local.read_bytes()
    requested_at = []
    key_calls = 0

    def compare_row(row, info):
        nonlocal key_calls
        key_calls += 1
        clock.now += .001
        if key_calls == 200:
            Path(request[lock_name]).write_text("pending", encoding="utf-8")
            requested_at.append(clock.now)
        return original_key(row, info)

    monkeypatch.setattr(replica_delta, "_row_key", compare_row)
    with pytest.raises(error_class):
        worker._sync_snapshot_with_network_lease(request)
    assert requested_at and clock.now - requested_at[0] <= .11
    assert key_calls < 500  # Stop while reconciling, not after scanning 12,000 rows.
    assert local.read_bytes() == local_before
    _assert_released(request)


@pytest.mark.parametrize("changed", [True, False], ids=["delta", "unchanged"])
@pytest.mark.parametrize("lock_name,error_class", [
    ("writer_lock_path", worker.LocalReplicaWriterBusy),
    ("rotation_lock_path", worker.LocalReplicaRotationBusy),
])
def test_final_fence_rejects_lock_appearing_inside_poll_interval(
    tmp_path, monkeypatch, changed, lock_name, error_class
):
    _, local, request = _databases(tmp_path, changed=changed)
    _clock(monkeypatch)  # Time never reaches the next periodic metadata poll.
    from rem_card.app import replica_source_stamp
    capture = replica_source_stamp.capture_source_stamp
    calls = 0
    local_before = local.read_bytes()

    def final_stamp(connection, path):
        nonlocal calls
        result = capture(connection, path)
        calls += 1
        if calls == 2:
            Path(request[lock_name]).write_text("pending", encoding="utf-8")
        return result

    monkeypatch.setattr(replica_source_stamp, "capture_source_stamp", final_stamp)
    with pytest.raises(error_class):
        worker._sync_snapshot_with_network_lease(request)
    assert calls == 2
    assert local.read_bytes() == local_before
    _assert_released(request)


def test_local_cancellation_remains_row_level_when_boundary_probe_is_separate(tmp_path):
    _, local, request = _databases(tmp_path)
    checks = 0
    local_before = local.read_bytes()

    def cancelled():
        nonlocal checks
        checks += 1
        if checks >= 500:
            raise worker.LocalReplicaWorkerCancelled("cancelled during historical scan")

    with pytest.raises(worker.LocalReplicaWorkerCancelled):
        replica_refresh.try_incremental_snapshot(
            request, check_cancel=cancelled, check_boundary=lambda: None, progress=lambda _: None)
    assert checks == 500
    assert local.read_bytes() == local_before
    _assert_released(request)


def test_slow_metadata_probes_do_not_restart_on_every_row(monkeypatch):
    clock = _clock(monkeypatch)
    calls = []

    def slow_metadata(path):
        calls.append(path)
        clock.now += .2  # A single metadata call exceeds the polling interval.
        return False

    monkeypatch.setattr(os.path, "exists", slow_metadata)
    probe = worker._DeltaCoordinationProbe("writer.lock", "rotation.lock")
    for _ in range(10_000):
        probe()
    assert calls == ["writer.lock", "rotation.lock"]
    clock.now += .11
    probe()
    assert len(calls) == 4


def test_local_cancel_during_fsync_rejects_candidate_even_with_separate_fence(tmp_path, monkeypatch):
    _, local, request = _databases(tmp_path, row_count=100)
    cancelled = False
    original_fsync = os.fsync
    local_before = local.read_bytes()

    def fsync(handle):
        nonlocal cancelled
        original_fsync(handle)
        cancelled = True

    def check_cancel():
        if cancelled:
            raise worker.LocalReplicaWorkerCancelled("cancelled before publication")

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(worker.LocalReplicaWorkerCancelled):
        replica_refresh.try_incremental_snapshot(
            request, check_cancel=check_cancel, check_boundary=lambda: None, progress=lambda _: None)
    assert cancelled and local.read_bytes() == local_before
    _assert_released(request)
