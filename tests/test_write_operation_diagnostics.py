"""Write evidence is correlated, private and unable to change persistence."""
from contextlib import closing
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from rem_card.app import sqlite_shared as shared
from rem_card.app import write_operation_diagnostics as diagnostics
from rem_card.services import data_service


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "private_database_name.db"
    connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    connection.execute("CREATE TABLE marks(value TEXT)")
    controller = shared.SQLiteWriteController(str(path), str(tmp_path / "db.lock"), "test", retry_delay_ms=10)
    yield connection, controller
    connection.close()


@pytest.fixture
def events(monkeypatch):
    rows = []
    def record(name, value=None, **fields):
        rows.append({"metric": name, "value": value, **fields})
    monkeypatch.setattr(diagnostics, "record_metric", record)
    monkeypatch.setattr(data_service, "record_metric", record)
    return rows


def _transaction_event(events):
    return next(row for row in events if row["metric"] == "sqlite_write_transaction_diagnostic")


def test_successful_queue_transaction_and_accepted_outcome_are_correlated_and_private(database, events, monkeypatch):
    connection, controller = database
    monkeypatch.setattr(diagnostics, "SLOW_WRITE_MS", 0)
    service = SimpleNamespace(_runtime_role="nurse", _runtime_session_id="b" * 32)
    metadata = {"request_id": "a" * 32, "role": "nurse", "admission_id": 123456,
                "order_id": 987654, "table_code": "private_table", "operation_id": "c" * 32}
    description = "nurse_order_mark:123456:987654:Private Patient"
    accepted = data_service.DataService._register_accepted_write(service, description, metadata)
    metadata["accepted_write_id"] = accepted
    done = threading.Event()
    queue = shared.LocalWriteQueue()
    statements = []
    connection.set_trace_callback(statements.append)

    def operation():
        with controller.transaction(connection, source=description, write_options=metadata) as cursor:
            cursor.execute("INSERT INTO marks VALUES (?)", ("private_value",))

    def committed(_result):
        data_service.DataService._set_write_outcome(service, accepted, "committed")
        done.set()

    try:
        with diagnostics.write_diagnostic_scope(metadata):
            queue.submit(operation, description, on_success=committed, retryable=False)
        assert done.wait(2)
    finally:
        assert queue.shutdown(2)
    transaction = _transaction_event(events)
    outcome = next(row for row in events if row["metric"] == "accepted_write_outcome" and row["result"] == "committed")
    queued = next(row for row in events if row["metric"] == "write_queue_diagnostic")
    assert transaction["accepted_write_id"] == outcome["accepted_write_id"] == queued["accepted_write_id"] == accepted
    assert transaction["task_id"] == outcome["task_id"] == queued["task_id"]
    assert transaction["request_id"] == outcome["request_id"] == "a" * 32
    assert transaction["database_outcome"] == "committed"
    assert {"connection_guard", "file_lock", "begin", "body", "commit", "lock_release"} <= transaction["phases"].keys()
    assert all(row["total_ms"] >= 0 for row in transaction["phases"].values())
    assert connection.execute("SELECT value FROM marks").fetchall() == [("private_value",)]
    assert not __import__("os").path.exists(controller.lock_path)
    encoded = json.dumps(events)
    for secret in ("123456", "987654", "Private Patient", "private_value", "private_table", controller.db_path):
        assert secret not in encoded
    assert not any(statement.startswith("SELECT") for statement in statements[:-1])


def test_real_commit_budget_exhaustion_records_confirmed_rollback_and_sqlite_code(database, events):
    writer, controller = database
    with closing(sqlite3.connect(controller.db_path, isolation_level=None)) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM marks").fetchall()
        with pytest.raises(shared.QueuedWriteDeadlineExceeded):
            with controller.transaction(writer, source="nurse_order_mark:private", write_options={"write_budget_ms": 100}) as cursor:
                cursor.execute("INSERT INTO marks VALUES ('unsaved')")
        reader.execute("ROLLBACK")
    row = _transaction_event(events)
    assert row["database_outcome"] == "rolled_back"
    assert row["failure_stage"] == "commit"
    assert row["sqlite_errorcode"] & 255 == sqlite3.SQLITE_BUSY
    assert row["sqlite_errorname"] == "SQLITE_BUSY"
    assert row["remaining_budget_ms"] == 0
    assert row["phases"]["rollback"]["count"] == 1
    assert not writer.in_transaction
    assert writer.execute("SELECT * FROM marks").fetchall() == []


class _AmbiguousCommit:
    def __init__(self, connection, *, rollback_fails=False):
        self.connection = connection
        self.rollback_fails = rollback_fails

    @property
    def in_transaction(self):
        return self.connection.in_transaction

    def cursor(self):
        return self.connection.cursor()

    def execute(self, statement, *args):
        if statement == "COMMIT":
            if not self.rollback_fails:
                self.connection.execute(statement)
            exc = OSError("private patient text and path")
            exc.winerror = 64
            raise exc
        if statement == "ROLLBACK" and self.rollback_fails:
            raise sqlite3.OperationalError("private rollback failure")
        return self.connection.execute(statement, *args)


@pytest.mark.parametrize("rollback_fails", [False, True])
def test_missing_commit_ack_or_failed_rollback_remains_unknown(database, events, rollback_fails):
    connection, controller = database
    proxy = _AmbiguousCommit(connection, rollback_fails=rollback_fails)
    with pytest.raises((OSError, sqlite3.OperationalError)):
        with controller.transaction(proxy, source="mark:Private Patient") as cursor:
            cursor.execute("INSERT INTO marks VALUES ('private_value')")
    row = _transaction_event(events)
    assert row["database_outcome"] == "unknown"
    assert row["failure_stage"] == "commit" and row["winerror"] == 64
    assert row["cleanup_error"] if rollback_fails else not row["cleanup_error"]
    assert "private" not in json.dumps(row)
    if rollback_fails:
        assert connection.in_transaction
        connection.rollback()
    else:
        assert connection.execute("SELECT COUNT(*) FROM marks").fetchone() == (1,)


@pytest.mark.parametrize("broken_event", [
    "write_queue_wait_ms", "sqlite_write_lock_wait_started", "sqlite_write_lock_acquired",
    "sqlite_write_lock_released", "sqlite_write_transaction_diagnostic",
])
def test_broken_telemetry_cannot_skip_write_callback_or_lock_release(database, monkeypatch, broken_event):
    connection, controller = database
    done = threading.Event()
    errors = []
    queue = shared.LocalWriteQueue()
    monkeypatch.setattr(diagnostics, "SLOW_WRITE_MS", 0)

    def broken(name, *_args, **_kwargs):
        if name == broken_event:
            raise RuntimeError("telemetry sink unavailable")

    monkeypatch.setattr(shared, "record_metric", broken)
    monkeypatch.setattr(diagnostics, "record_metric", broken)
    def operation():
        with controller.transaction(connection, source="mark") as cursor:
            cursor.execute("INSERT INTO marks VALUES ('saved')")

    try:
        queue.submit(operation, "mark", on_success=lambda _: done.set(), on_error=errors.append, retryable=False)
        assert done.wait(2)
    finally:
        assert queue.shutdown(2)
    assert errors == [] and queue.is_idle()
    assert connection.execute("SELECT * FROM marks").fetchall() == [("saved",)]
    assert not __import__("os").path.exists(controller.lock_path)


def test_fast_success_does_not_add_detailed_transaction_summary(database, events):
    connection, controller = database
    with controller.transaction(connection, source="mark") as cursor:
        cursor.execute("INSERT INTO marks VALUES ('saved')")
    assert not any(row["metric"] == "sqlite_write_transaction_diagnostic" for row in events)


def test_nested_success_does_not_claim_outer_transaction_committed(database, events, monkeypatch):
    connection, controller = database
    monkeypatch.setattr(diagnostics, "SLOW_WRITE_MS", 0)
    connection.execute("BEGIN")
    with controller.transaction(connection, source="mark") as cursor:
        cursor.execute("INSERT INTO marks VALUES ('nested')")
    row = _transaction_event(events)
    assert row["nested"] and row["database_outcome"] == "unknown"
    assert connection.in_transaction
    connection.rollback()
    assert connection.execute("SELECT * FROM marks").fetchall() == []


def test_broken_accepted_metric_and_logger_preserve_registry_and_real_write(database, monkeypatch):
    connection, controller = database
    service = SimpleNamespace(_runtime_role="nurse", _runtime_session_id="b" * 32)
    def broken(*_args, **_kwargs):
        raise RuntimeError("sink failed")
    monkeypatch.setattr(data_service, "record_metric", broken)
    monkeypatch.setattr(data_service.logger, "debug", broken)
    operation_id = data_service.DataService._register_accepted_write(service, "nurse_order_mark")
    with controller.transaction(connection, source="mark") as cursor:
        cursor.execute("INSERT INTO marks VALUES ('saved')")
    data_service.DataService._set_write_outcome(service, operation_id, "committed")
    assert service._write_outcome_registry[operation_id]["state"] == "committed"
    assert connection.execute("SELECT * FROM marks").fetchall() == [("saved",)]


def test_phase_or_error_observation_failure_cannot_replace_original_failure(database, monkeypatch):
    connection, controller = database
    def broken(*_args, **_kwargs):
        raise RuntimeError("diagnostic collector failed")
    monkeypatch.setattr(diagnostics.WriteOperationDiagnostics, "_record_phase", broken)
    monkeypatch.setattr(diagnostics, "write_error_fields", broken)
    original = ValueError("business failure")
    with pytest.raises(ValueError) as raised:
        with controller.transaction(connection, source="mark") as cursor:
            cursor.execute("INSERT INTO marks VALUES ('unsaved')")
            raise original
    assert raised.value is original
    assert not connection.in_transaction
    assert connection.execute("SELECT * FROM marks").fetchall() == []
