from collections import OrderedDict, deque
import json
import sqlite3
import threading

import pytest

from rem_card.app import sqlite_connection_diagnostics as registry
from rem_card.app import db_wait_diagnostics as waits


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    monkeypatch.setattr(registry, "_connections", OrderedDict())
    monkeypatch.setattr(registry, "_aliases", OrderedDict())
    monkeypatch.setattr(registry, "_resources", OrderedDict())
    monkeypatch.setattr(registry, "_recent_cleanup", deque(maxlen=registry.MAX_RECENT_CLEANUP))
    monkeypatch.setattr(registry, "_overflow", 0)


def snapshot(database="synthetic", **kwargs):
    return registry.candidates(registry.canonical_resource(database), **kwargs)


def test_alias_mapping_is_explicit_cached_and_has_no_filesystem_probe(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("diagnostics must not probe filesystem or network")
    monkeypatch.setattr(registry.os, "stat", forbidden)
    monkeypatch.setattr(registry.os.path, "realpath", forbidden)
    monkeypatch.setattr(registry.socket, "gethostname", forbidden)
    mapped = r"Z:\PRIVATE\central.db"
    unc = r"\\server\share\private\central.db"
    assert registry.canonical_resource(mapped) != registry.canonical_resource(unc)
    registry.register_database_alias("Z:\\", "\\\\server\\share\\")
    assert registry.canonical_resource(mapped) == registry.canonical_resource(unc)
    assert registry.canonical_resource(r"\\?\UNC\server\share\private\central.db") == registry.canonical_resource(unc)
    assert registry.canonical_resource(r"Z:\private2\central.db") != registry.canonical_resource(unc)


def test_generation_token_is_idempotent_and_changes_on_new_factory_lifetime():
    connection = object()
    first = registry.register_connection(connection, "synthetic", source="save:SECRET_PATIENT")
    assert registry.register_connection(connection, "synthetic") == first
    second = registry.register_connection(connection, "synthetic", new=True)
    assert second != first and registry.connection_token(connection) == second
    registry.connection_closed(connection)
    assert registry.connection_token(connection) is None


def test_registry_does_not_touch_connection_or_cursor_objects():
    class Opaque:
        def __getattribute__(self, name):
            raise AssertionError(f"unexpected inspection: {name}")
    connection, cursor = Opaque(), Opaque()
    registry.register_connection(connection, "PRIVATE_DB", source="save:SECRET_PATIENT")
    registry.transaction_started(connection, "read", source="read:SECRET_PARAM")
    registry.cursor_opened(connection, cursor, source="SELECT PRIVATE_SQL")
    token = registry.statement_started(connection, source="read:PRIVATE_SQL")
    row, = snapshot("PRIVATE_DB")["local_connection_candidates"]
    assert row["transaction"] == "read" and row["active_statements"] == row["open_cursors"] == 1
    assert row["holder_confidence"] == "candidate"
    assert not any(secret in json.dumps(row) for secret in ("SECRET_", "PRIVATE_", "SELECT"))
    registry.statement_finished(connection, token)
    registry.cursor_closed(connection, cursor)
    registry.transaction_finished(connection, "rolled_back")
    assert snapshot("PRIVATE_DB")["local_connection_candidates"] == []


def test_same_thread_other_connection_is_candidate_for_actual_failed_begin(tmp_path):
    path = str(tmp_path / "synthetic.db")
    holder = sqlite3.connect(path, isolation_level=None)
    writer = sqlite3.connect(path, isolation_level=None, timeout=0)
    try:
        holder.execute("CREATE TABLE sample(value INTEGER)")
        for connection in (holder, writer):
            registry.register_connection(connection, path, source="test_write")
        holder.execute("BEGIN IMMEDIATE")
        registry.transaction_started(holder, "write", source="holder")
        with pytest.raises(sqlite3.OperationalError):
            writer.execute("BEGIN IMMEDIATE")
        operation = waits.Operation("writer", database=path, connection=writer, stage="sqlite_begin_wait")
        observed = waits._operation_snapshot(operation, waits.time.monotonic())
        candidate, = observed["local_connection_candidates"]
        assert candidate["connection_token"] == registry.connection_token(holder)
        assert candidate["thread_id"] == threading.get_ident()
        assert candidate["transaction"] == "write"
        assert observed["native_owner_identified"] is False
        assert observed["holder_confidence"] == "unknown"
    finally:
        for connection in (holder, writer):
            registry.close_connection(connection)


def test_unfinished_autocommit_cursor_is_visible_without_explicit_transaction():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    registry.register_connection(connection, "synthetic", readonly=True)
    cursor = connection.execute("SELECT 1 UNION ALL SELECT 2")
    try:
        registry.cursor_opened(connection, cursor, source="fetch_one")
        assert cursor.fetchone() == (1,)
        assert connection.in_transaction is False
        row, = snapshot()["local_connection_candidates"]
        assert row["transaction"] is None and row["open_cursors"] == 1
        cursor.close()
        registry.cursor_closed(connection, cursor)
        assert snapshot()["local_connection_candidates"] == []
    finally:
        registry.close_connection(connection)


def test_cleanup_failure_survives_quarantine_and_successful_close():
    class Connection:
        fail = True
        def close(self):
            if self.fail:
                raise RuntimeError("PRIVATE close failure")
    connection = Connection()
    registry.register_connection(connection, "synthetic")
    registry.transaction_started(connection, "write", source="save")
    registry.cleanup_failed(connection, phase="rollback", error_class="OperationalError")
    registry.quarantine_connection(connection)
    with pytest.raises(RuntimeError, match="PRIVATE close failure"):
        registry.close_connection(connection)
    row, = snapshot()["local_connection_candidates"]
    assert row["quarantined"] and row["transaction"] == "write"
    assert row["cleanup_phase"] == "close"
    connection.fail = False
    registry.close_connection(connection)
    after = snapshot()
    assert after["local_connection_candidates"] == []
    recent, = after["recent_connection_cleanup"]
    assert recent["connection_state"] == "closed" and recent["cleanup_error"] == "RuntimeError"
    assert [(error["phase"], error["error_class"]) for error in recent["cleanup_errors"]] == [
        ("rollback", "OperationalError"), ("close", "RuntimeError"),
    ]
    assert recent["holder_confidence"] == "not_active"
    assert "PRIVATE" not in json.dumps(after)


@pytest.mark.parametrize("stage", ["sqlite_begin_wait", "commit", "central_read_execute", "pragma_worker_busy_timeout", "replica_delta_collect"])
def test_any_overdue_operation_snapshot_includes_idle_transaction(stage):
    holder = object()
    registry.register_connection(holder, "synthetic", source="forgotten_reader")
    registry.transaction_started(holder, "read", source="forgotten_reader")
    operation = waits.Operation("current", resource="synthetic", stage=stage)
    row, = waits._operation_snapshot(operation, waits.time.monotonic())["local_connection_candidates"]
    assert row["active_statements"] == 0 and row["open_cursors"] == 0 and row["transaction"] == "read"


def test_registry_caps_do_not_silently_claim_complete_coverage(monkeypatch):
    monkeypatch.setattr(registry, "MAX_CONNECTIONS", 4)
    connections = [object() for _ in range(8)]
    for connection in connections:
        registry.register_connection(connection, "synthetic")
        registry.transaction_started(connection, "read")
    data = snapshot(limit=2)
    assert data["connection_registry_size"] == data["connection_registry_overflow"] == 4
    assert data["connection_candidates_total"] == 4 and data["connection_candidates_truncated"]
    assert len(data["local_connection_candidates"]) == 2
    assert not data["native_owner_identified"]


def test_cursor_and_statement_overflow_remain_visible_until_connection_close(monkeypatch):
    monkeypatch.setattr(registry, "MAX_CURSORS", 1)
    monkeypatch.setattr(registry, "MAX_STATEMENTS", 1)
    connection, first, second = object(), object(), object()
    registry.register_connection(connection, "synthetic")
    registry.cursor_opened(connection, first)
    registry.cursor_opened(connection, second)
    token = registry.statement_started(connection)
    assert registry.statement_started(connection) is None
    registry.statement_finished(connection, token)
    registry.cursor_closed(connection, first)
    row, = snapshot()["local_connection_candidates"]
    assert row["cursor_overflow"] == row["statement_overflow"] == 1
    assert row["active_statements"] == row["open_cursors"] == 0
    registry.connection_closed(connection)
    assert snapshot()["local_connection_candidates"] == []


def test_slow_snapshot_collects_registry_once_per_database(monkeypatch):
    holder = object()
    registry.register_connection(holder, "synthetic")
    registry.transaction_started(holder, "read")
    operations = [waits.Operation("read", resource="synthetic", stage="replica_delta_collect") for _ in range(5)]
    monkeypatch.setattr(waits, "_active", {op.id: op for op in operations})
    monkeypatch.setattr(waits, "THRESHOLD_SEC", 0)
    monkeypatch.setattr(waits, "_last_snapshot", float("-inf"))
    events, calls = [], []
    original = registry.candidates
    monkeypatch.setattr(registry, "candidates", lambda *args, **kwargs: calls.append(1) or original(*args, **kwargs))
    monkeypatch.setattr(waits, "_emit", lambda event, payload: events.append((event, payload)))
    waits._snapshot()
    assert calls == [1]
    data, = events[0][1]["connection_registry_snapshots"]
    assert len(data["local_connection_candidates"]) == 1
    assert data["native_owner_identified"] is False


def test_broken_memory_hooks_cannot_change_sql_result_or_original_failure(monkeypatch):
    connection = sqlite3.connect(":memory:", isolation_level=None)
    def fail():
        raise RuntimeError("diagnostic context unavailable")
    monkeypatch.setattr(registry, "_context", fail)
    try:
        assert registry.register_connection(connection, "synthetic") is None
        with registry.statement_scope(connection, source="read"):
            assert connection.execute("SELECT 7").fetchone() == (7,)
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            with registry.statement_scope(connection, source="read"):
                connection.execute("SELECT * FROM missing")
    finally:
        registry.close_connection(connection)


def test_native_scalar_is_observed_during_actual_fetch_and_keeps_exact_result():
    from rem_card.app.replica_delta import _scalar
    connection = sqlite3.connect(":memory:", isolation_level=None)
    registry.register_connection(connection, "synthetic", source="replica_readonly")
    observed = []
    connection.create_function("observed", 0, lambda: observed.append(snapshot()) or 7)
    try:
        assert _scalar(connection, "SELECT observed()") == 7
        row, = observed[0]["local_connection_candidates"]
        assert row["active_statements"] == 1 and row["statement_sources"] == ["replica_delta_scalar"]
        assert snapshot()["local_connection_candidates"] == []
    finally:
        registry.close_connection(connection)


def test_network_worker_sql_closes_statement_and_cursor_without_logging_sql():
    from rem_card.app.network_write_worker import _execute_sql
    connection = sqlite3.connect(":memory:", isolation_level=None)
    registry.register_connection(connection, "synthetic", source="network_writer")
    try:
        result = _execute_sql(connection, {"cmd": "execute", "sql": "SELECT ?", "params": ["PRIVATE_PARAM"]})
        assert result["rows"] == [("PRIVATE_PARAM",)]
        assert snapshot()["local_connection_candidates"] == []
        assert "PRIVATE" not in json.dumps(snapshot())
    finally:
        registry.close_connection(connection)


def test_replica_readonly_factory_registers_and_closes_central_source(tmp_path):
    from rem_card.app.local_replica_worker import _open_central_readonly
    path = str(tmp_path / "synthetic.db")
    with registry.closing_connection(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE sample(value INTEGER)")
    with registry.closing_connection(_open_central_readonly(path)) as reader:
        assert registry.connection_token(reader)
        registry.transaction_started(reader, "read", source="replica_delta_collect")
        row, = snapshot(path)["local_connection_candidates"]
        assert row["readonly_at_creation"] is True and row["scope"] == "worker"
    assert snapshot(path)["connection_registry_size"] == 0
