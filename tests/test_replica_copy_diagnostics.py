from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from rem_card.app import local_metrics, local_replica_worker as worker
from rem_card.app import replica_copy_diagnostics as diagnostics
from rem_card.app import startup_diagnostics


@pytest.fixture
def summaries(monkeypatch):
    rows = []
    def record(name, value=None, **fields):
        if name == "replica_copy_summary":
            rows.append(fields)
    monkeypatch.setattr(local_metrics, "record_metric", record)
    monkeypatch.setattr(startup_diagnostics, "_sample", lambda: {"process_cpu_ms": 10.0})
    return rows


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "Baza" / "archiv" / "central.db"
    path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)")
        connection.execute("INSERT INTO meta VALUES (?,?)", (worker.DB_CYCLE_META_KEY, "synthetic"))
        connection.execute("CREATE TABLE change_log(id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO change_log VALUES (1)")
        connection.execute("CREATE TABLE source_rows(id INTEGER PRIMARY KEY,payload BLOB)")
        connection.execute("INSERT INTO source_rows VALUES (1,?)", (b"synthetic" * 300000,))
    return path


def message(source, tmp_path):
    return dict(central_db_path=str(source), temp_db_path=str(tmp_path / "candidate.db"),
        local_state={}, writer_lock_path=str(source.parent / "db.lock"),
        rotation_lock_path=str(source.parent / "rotation.lock"),
        snapshot_lease_path=str(source.parent.parent / "locks" / "reader.lock"))


def test_real_full_snapshot_is_content_free_and_cleans_owned_locks(source, tmp_path, summaries):
    request = message(source, tmp_path)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    with startup_diagnostics.startup_attempt("a" * 32, "doctor"):
        result = worker._sync_snapshot_with_network_lease(request)
    assert result["status"] == "snapshot_ready"
    with closing(sqlite3.connect(request["temp_db_path"])) as copied:
        assert copied.execute("SELECT payload FROM source_rows").fetchone() == (b"synthetic" * 300000,)
        assert copied.execute("PRAGMA quick_check").fetchone() == ("ok",)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert not Path(request["snapshot_lease_path"]).exists()
    assert not Path(worker.replica_snapshot_gate_path(str(source))).exists()
    assert len(summaries) == 1  # Nested _sync_snapshot shares the outer observation.
    row = summaries[0]
    assert row["schema_version"] == 1 and row["scope"] == "network_lease"
    assert row["outcome"] == "ok" and row["sync_mode"] == "full"
    assert row["role"] == "doctor" and row["session_id"] == "a" * 32
    assert len(row["operation_id"]) == 32  # Same outer DB_WAIT operation.
    assert row["source_size_before_bytes"] == row["source_size_after_bytes"] == source.stat().st_size
    assert row["callback_count"] >= 2 and row["sqlite_status_counts"]["done"] == 1
    assert row["remaining_last"] == 0 and row["page_count_max"] > 256
    assert row["full_backup_ms"] >= row["callback_ms"] >= row["callback_writer_probe_ms"]
    assert {"source_open", "source_state", "local_open", "checkpoint", "quick_check", "fsync",
            "gate_release", "lease_release"}.issubset(row["phases"])
    assert row["phases"]["source_open"]["count"] == 4
    assert not row["process_io_available"]
    encoded = json.dumps(row)
    assert str(source) not in encoded and "source_rows" not in encoded and "synthetic" not in encoded


class ScriptedSource:
    def __init__(self, connection, events, clock, failure=None):
        self.connection, self.events, self.clock, self.failure = connection, events, clock, failure

    def execute(self, *args, **kwargs):
        return self.connection.execute(*args, **kwargs)

    def close(self):
        self.connection.close()

    def backup(self, destination, *, pages, progress, sleep):
        assert pages == 256 and sleep == .01
        self.connection.backup(destination)  # A real local copy, without extra callback events.
        for at, status, remaining, total in self.events:
            self.clock[0] = at
            progress(status, remaining, total)
        if self.failure is not None:
            raise self.failure


def scripted_backup(monkeypatch, events, failure=None):
    clock = [99.0]
    original_open = worker._open_central_readonly
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(worker, "_open_central_readonly", lambda path:
        ScriptedSource(original_open(path), events, clock, failure))
    return clock


def test_busy_locked_counts_and_writer_probe_cadence_are_unchanged(source, tmp_path, summaries, monkeypatch):
    events = [(100, 0, 90, 100), (100.02, 0, 80, 100), (100.04, 5, 80, 100),
              (100.05, 6, 80, 100), (100.08, 0, 90, 110), (100.16, 101, 0, 110)]
    clock = scripted_backup(monkeypatch, events)
    original_exists = worker.os.path.exists
    probes = []
    def exists(path):
        if path == str(source.parent / "db.lock"):
            probes.append(clock[0])
        return original_exists(path)
    monkeypatch.setattr(worker.os.path, "exists", exists)
    worker._sync_snapshot(message(source, tmp_path))
    row = summaries[0]
    assert row["sqlite_status_counts"] == dict(ok=3, done=1, busy=1, locked=1, other=0)
    assert row["callback_count"] == 6 and row["remaining_increase_count"] == 1
    assert row["page_count_first"] == 100 and row["page_count_last"] == 110
    assert probes == [99.0, 100, 100.04, 100.05, 100.16]
    assert row["phases"]["writer_probe"]["count"] == len(probes)


@pytest.mark.parametrize("probe_failure", [False, True])
def test_partial_backup_errors_keep_identity_and_cleanup(source, tmp_path, summaries, monkeypatch, probe_failure):
    error = OSError("synthetic failure")
    scripted_backup(monkeypatch, [(100, 0, 10, 20)], None if probe_failure else error)
    request = message(source, tmp_path)
    original_exists = worker.os.path.exists
    calls = []
    def exists(path):
        if path == request["writer_lock_path"]:
            calls.append(path)
            if probe_failure and len(calls) == 2:
                raise error
        return original_exists(path)
    monkeypatch.setattr(worker.os.path, "exists", exists)
    with pytest.raises(OSError) as raised:
        worker._sync_snapshot(request)
    assert raised.value is error
    assert not Path(request["temp_db_path"]).exists()
    assert len(summaries) == 1
    assert summaries[0]["outcome"] == "error" and summaries[0]["error_class"] == "OSError"
    assert summaries[0]["callback_count"] == 1
    assert summaries[0]["failure_phase"] in {"full_backup", "writer_probe"}


def test_step_intervals_exclude_previous_callback_and_separate_probes(summaries, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(diagnostics.time, "perf_counter", lambda: clock[0])
    observation = diagnostics.CopyDiagnostics("direct_snapshot", {})
    began = observation.begin_phase("full_backup")
    clock[0] = .010
    callback = observation.callback_begin(0, 10, 20)
    def probe():
        clock[0] += .003
        return False
    assert observation.call("writer_probe", probe) is False
    clock[0] = .015
    observation.callback_end(callback)
    clock[0] = .035
    callback = observation.callback_begin(101, 0, 20)
    clock[0] = .037
    observation.callback_end(callback)
    clock[0] = .040
    observation.end_phase("full_backup", began)
    observation.finish(None)
    row = summaries[0]
    assert (row["step_first_ms"], row["step_last_ms"], row["step_max_ms"], row["step_total_ms"]) == (10, 20, 20, 30)
    assert row["callback_ms"] == 7 and row["callback_writer_probe_ms"] == 3
    assert row["full_backup_ms"] == 40


@pytest.mark.parametrize("failure", ["process_api", "metric_writer", "timer"])
def test_diagnostic_api_failures_do_not_change_real_snapshot(source, tmp_path, monkeypatch, failure):
    def broken(*args, **kwargs):
        raise RuntimeError("diagnostics unavailable")
    if failure == "process_api":
        monkeypatch.setattr(startup_diagnostics, "_sample", broken)
    elif failure == "metric_writer":
        monkeypatch.setattr(local_metrics, "record_metric", broken)
    else:
        monkeypatch.setattr(diagnostics.time, "perf_counter", broken)
    request = message(source, tmp_path)
    assert worker._sync_snapshot(request)["status"] == "snapshot_ready"
    with closing(sqlite3.connect(request["temp_db_path"])) as copied:
        assert copied.execute("SELECT count(*) FROM source_rows").fetchone() == (1,)


def test_process_io_deltas_are_local_and_optional(summaries, monkeypatch):
    samples = iter([dict(process_cpu_ms=10, read_ops=2, read_bytes=100),
                    dict(process_cpu_ms=14, read_ops=5, read_bytes=200)])
    monkeypatch.setattr(startup_diagnostics, "_sample", lambda: next(samples))
    observation = diagnostics.CopyDiagnostics("direct_snapshot", {})
    observation.begin_phase("full_backup")
    observation.finish(None)
    row = summaries[0]
    assert row["process_io_available"] is True
    assert row["process_cpu_ms_delta"] == 4 and row["read_bytes_delta"] == 100
    assert row["read_ops_delta"] == 3 and "write_bytes_delta" not in row


def test_only_startup_emits_early_refusal_and_fast_reuse(source, tmp_path, summaries):
    request = message(source, tmp_path)
    writer = Path(request["writer_lock_path"])
    writer.write_text("synthetic", encoding="utf-8")
    with pytest.raises(worker.LocalReplicaWriterBusy):
        worker._sync_snapshot_with_network_lease(request)
    assert summaries == []  # Background two-second polls retain existing metrics.
    with startup_diagnostics.startup_attempt("b" * 32, "nurse"):
        with pytest.raises(worker.LocalReplicaWriterBusy):
            worker._sync_snapshot_with_network_lease(request)
    writer.unlink()
    assert len(summaries) == 1 and summaries[0]["full_backup_ms"] == 0
    assert summaries[0]["error_class"] == "LocalReplicaWriterBusy"
    with closing(sqlite3.connect(source)) as connection:
        request["local_state"] = worker._read_database_state(connection)
    with startup_diagnostics.startup_attempt("c" * 32, "doctor"):
        assert worker._sync_snapshot(request)["status"] == "unchanged"
    assert len(summaries) == 2 and summaries[-1]["result_status"] == "unchanged"


@pytest.mark.parametrize("status,mode", [("unchanged", "unchanged"), ("snapshot_ready", "delta")])
def test_background_reuse_and_delta_do_not_emit_raw_summaries(source, tmp_path, summaries, monkeypatch, status, mode):
    from rem_card.app import replica_refresh
    monkeypatch.setattr(replica_refresh, "try_incremental_snapshot", lambda *args, **kwargs:
                        dict(status=status, sync_mode=mode))
    request = {**message(source, tmp_path), "local_db_path": str(source)}
    assert worker._sync_snapshot_with_network_lease(request)["sync_mode"] == mode
    assert summaries == []
    with startup_diagnostics.startup_attempt("d" * 32, "doctor"):
        worker._sync_snapshot_with_network_lease(request)
    assert len(summaries) == 1 and summaries[0]["full_backup_ms"] == 0
    assert summaries[0]["sync_mode"] == mode
    assert not Path(request["snapshot_lease_path"]).exists()


def test_observation_adds_no_sql_source_stat_or_exists_calls(source, tmp_path, summaries, monkeypatch):
    original_open = worker._open_central_readonly
    original_exists, original_stat = worker.os.path.exists, worker.os.stat
    counts = dict(sql=[], exists=0, source_stat=0)
    def open_source(path):
        connection = original_open(path)
        connection.set_trace_callback(counts["sql"].append)
        return connection
    def exists(path):
        counts["exists"] += 1
        return original_exists(path)
    def stat(path, *args, **kwargs):
        if str(path) == str(source):
            counts["source_stat"] += 1
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(worker, "_open_central_readonly", open_source)
    monkeypatch.setattr(worker.os.path, "exists", exists)
    monkeypatch.setattr(worker.os, "stat", stat)
    monkeypatch.setattr(worker.time, "monotonic", lambda: 100.0)
    request = message(source, tmp_path)
    token = diagnostics._CURRENT.set(diagnostics._NONE)
    try:
        worker._sync_snapshot_with_network_lease(request)
    finally:
        diagnostics._CURRENT.reset(token)
    baseline = dict(sql=list(counts["sql"]), exists=counts["exists"], source_stat=counts["source_stat"])
    counts.update(sql=[], exists=0, source_stat=0)
    worker._sync_snapshot_with_network_lease(request)
    assert counts == baseline
    assert len(summaries) == 1
