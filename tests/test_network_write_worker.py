from __future__ import annotations

import json
import sqlite3
import time
import unittest
import pytest
from pathlib import Path
from tempfile import TemporaryDirectory

from rem_card.app.network_write_worker import (
    NetworkWriteWorkerClient,
    NetworkWriteWorkerTimeout,
)


def _initialize_worker_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE worker_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                value TEXT NOT NULL
            );

            CREATE TABLE change_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_name TEXT NOT NULL,
                entity_id INTEGER,
                admission_id INTEGER,
                action TEXT NOT NULL,
                changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE runtime_write_receipts (
                operation_id TEXT PRIMARY KEY,
                request_id TEXT,
                source TEXT NOT NULL,
                node_id TEXT NOT NULL,
                role TEXT,
                admission_id INTEGER,
                operation_case_id INTEGER,
                result_json TEXT NOT NULL DEFAULT 'null',
                affected_rows_json TEXT NOT NULL DEFAULT '[]',
                committed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TRIGGER worker_items_change_log
            AFTER INSERT ON worker_items
            BEGIN
                INSERT INTO change_log (
                    entity_name,
                    entity_id,
                    admission_id,
                    action
                )
                VALUES ('worker_items', NEW.id, 17, 'insert');
            END;
            """
        )
        conn.commit()
    finally:
        conn.close()


class NetworkWriteWorkerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.db_path = root / "journal.db"
        self.lock_path = root / "journal.db.write.lock"
        _initialize_worker_database(self.db_path)
        self.client = NetworkWriteWorkerClient(
            db_path=str(self.db_path),
            lock_path=str(self.lock_path),
            node_id="test-client:operblock",
            timeout_sec=3.0,
        )

    def tearDown(self):
        self.client.close()
        self._tmp.cleanup()

    def _fetch_one(self, sql: str, params=()):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()

    def test_commit_receipt_and_affected_rows_are_atomic(self):
        def operation(cursor):
            cursor.execute("INSERT INTO worker_items(value) VALUES (?)", ("first",))
            inserted_id = int(cursor.lastrowid)
            row = cursor.connection.execute(
                "SELECT id, value FROM worker_items WHERE id = ?",
                (inserted_id,),
            ).fetchone()
            return {"id": int(row["id"]), "value": str(row["value"])}

        result = self.client.execute(
            operation,
            operation_id="operation-1",
            source="operblock_test_insert",
            metadata={
                "request_id": "request-1",
                "role": "operblock",
                "admission_id": 17,
                "operation_case_id": 23,
            },
        )

        self.assertEqual(result, {"id": 1, "value": "first"})
        self.assertEqual(self.client.last_affected_change_id, 1)
        receipt = self._fetch_one(
            """
            SELECT request_id, source, node_id, role, admission_id,
                   operation_case_id, result_json, affected_rows_json
            FROM runtime_write_receipts
            WHERE operation_id = ?
            """,
            ("operation-1",),
        )
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt[:6], ("request-1", "operblock_test_insert", "test-client:operblock", "operblock", 17, 23))
        self.assertEqual(json.loads(receipt[6]), {"id": 1, "value": "first"})
        self.assertEqual(
            json.loads(receipt[7]),
            [
                {
                    "change_id": 1,
                    "entity_name": "worker_items",
                    "entity_id": 1,
                    "admission_id": 17,
                    "action": "insert",
                }
            ],
        )

    def test_retry_with_same_operation_id_does_not_duplicate_write(self):
        first = self.client.execute(
            lambda cursor: cursor.execute(
                "INSERT INTO worker_items(value) VALUES (?)",
                ("once",),
            ).lastrowid,
            operation_id="operation-deduplicated",
            source="operblock_test_insert",
        )

        def must_not_run(_cursor):
            raise AssertionError("Повторная операция не должна выполняться после найденной квитанции")

        repeated = self.client.execute(
            must_not_run,
            operation_id="operation-deduplicated",
            source="operblock_test_insert",
        )

        self.assertEqual(repeated, first)
        self.assertEqual(self._fetch_one("SELECT COUNT(*) FROM worker_items")[0], 1)
        self.assertEqual(self._fetch_one("SELECT COUNT(*) FROM runtime_write_receipts")[0], 1)

    def test_hard_timeout_terminates_process_and_rolls_back(self):
        def slow_operation(cursor):
            cursor.execute("INSERT INTO worker_items(value) VALUES (?)", ("must-rollback",))
            # Stall SQL in the worker, not Python in the caller. A caller sleep
            # lets finish/commit race process termination, and a confirmed
            # receipt correctly returns success instead of a rollback timeout.
            cursor.execute(
                "WITH RECURSIVE delay(n) AS ("
                "VALUES(0) UNION ALL SELECT n + 1 FROM delay WHERE n < 100000000"
                ") SELECT SUM(n) FROM delay"
            )
            return cursor.lastrowid

        started = time.monotonic()
        with self.assertRaises(NetworkWriteWorkerTimeout):
            self.client.execute(
                slow_operation,
                operation_id="operation-timeout",
                source="operblock_test_timeout",
                timeout_sec=1.5,
            )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2.5)
        self.assertEqual(self._fetch_one("SELECT COUNT(*) FROM worker_items")[0], 0)
        self.assertEqual(self._fetch_one("SELECT COUNT(*) FROM runtime_write_receipts")[0], 0)

        recovered = self.client.execute(
            lambda cursor: cursor.execute(
                "INSERT INTO worker_items(value) VALUES (?)",
                ("after-timeout",),
            ).lastrowid,
            operation_id="operation-after-timeout",
            source="operblock_test_recovery",
        )
        self.assertEqual(recovered, 1)
        deadline = time.monotonic() + 1.0
        while self.lock_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(self.lock_path.exists())


if __name__ == "__main__":
    unittest.main()


@pytest.fixture
def write_diagnostic_events(monkeypatch):
    from rem_card.app import write_operation_diagnostics as diagnostics
    rows = []
    monkeypatch.setattr(diagnostics, "SLOW_WRITE_MS", 0)
    monkeypatch.setattr(diagnostics, "record_metric", lambda name, value=None, **fields:
                        rows.append({"metric": name, "value": value, **fields}))
    return rows


def test_real_isolated_nurse_mark_correlates_client_ack_without_private_values(tmp_path, write_diagnostic_events):
    from rem_card.app.write_operation_diagnostics import write_diagnostic_scope
    source = tmp_path / "private_path.db"
    _initialize_worker_database(source)
    client = NetworkWriteWorkerClient(db_path=str(source), lock_path=str(tmp_path / "db.lock"),
                                      node_id="test", timeout_sec=3)
    context = {"accepted_write_id": "a" * 32, "request_id": "b" * 32, "task_id": "c" * 32, "role": "nurse"}
    try:
        with write_diagnostic_scope(context):
            result = client.execute(lambda cursor: cursor.execute(
                "INSERT INTO worker_items(value) VALUES (?)", ("private_value",)).lastrowid,
                operation_id="d" * 32, source="nurse_order_mark:Private Patient:123456", metadata=context)
        assert result == 1
    finally:
        client.close()
    row = next(item for item in write_diagnostic_events if item["metric"] == "network_write_client_diagnostic")
    assert row["database_outcome"] == "committed"
    assert row["accepted_write_id"] == "a" * 32 and row["request_id"] == "b" * 32
    assert {"mutex_wait", "worker_start", "begin", "body", "commit"} <= row["phases"].keys()
    encoded = json.dumps(row)
    assert all(private not in encoded for private in (str(source), "Private Patient", "123456", "private_value", "INSERT"))
    with sqlite3.connect(source) as connection:
        assert connection.execute("SELECT COUNT(*) FROM worker_items").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM runtime_write_receipts").fetchone() == (1,)


def test_child_summary_observes_existing_receipt_and_commit_sql_without_adding_queries(tmp_path, write_diagnostic_events, monkeypatch):
    from rem_card.app import network_write_worker as worker
    source = tmp_path / "central.db"
    _initialize_worker_database(source)
    statements = []
    original_open = worker._open_network_connection
    def open_observed(path):
        connection = original_open(path)
        connection.set_trace_callback(statements.append)
        return connection
    monkeypatch.setattr(worker, "_open_network_connection", open_observed)
    class Pipe:
        def __init__(self):
            self.responses = []
            self.commands = iter([
                {"cmd": "execute", "sql": "INSERT INTO worker_items(value) VALUES (?)", "params": ["private_value"]},
                {"cmd": "finish", "result": 1},
            ])
        def send(self, response):
            self.responses.append(response)
        def recv(self):
            return next(self.commands)
    pipe = Pipe()
    worker._worker_write(pipe, {"db_path": str(source), "lock_path": str(tmp_path / "db.lock"),
        "operation_id": "e" * 32, "source": "nurse_order_mark:private", "metadata": {"accepted_write_id": "a" * 32}})
    assert pipe.responses[-1]["status"] == "committed"
    row = next(item for item in write_diagnostic_events if item["metric"] == "network_write_worker_diagnostic")
    assert row["database_outcome"] == "committed" and row["accepted_write_id"] == "a" * 32
    assert {"connection_open", "receipt_lookup", "file_lock", "begin", "body", "receipt_store", "commit", "lock_release"} <= row["phases"].keys()
    receipt_selects = [sql for sql in statements if "SELECT operation_id, result_json" in sql]
    assert len(receipt_selects) == 2  # The preexisting before-lock and in-transaction lookups.
    assert sum(sql == "BEGIN IMMEDIATE" for sql in statements) == 1
    assert sum(sql == "COMMIT" for sql in statements) == 1


def test_remote_sqlite_error_code_survives_ipc_and_confirmed_abort(tmp_path, write_diagnostic_events):
    from rem_card.app.network_write_worker import NetworkWriteWorkerError
    source = tmp_path / "central.db"
    _initialize_worker_database(source)
    client = NetworkWriteWorkerClient(db_path=str(source), lock_path=str(tmp_path / "db.lock"), node_id="test", timeout_sec=3)
    try:
        with pytest.raises(NetworkWriteWorkerError) as raised:
            client.execute(lambda cursor: cursor.execute("INSERT INTO worker_items(value) VALUES (NULL)"),
                           operation_id="f" * 32, source="nurse_order_mark:private")
        assert raised.value.sqlite_errorcode & 255 == sqlite3.SQLITE_CONSTRAINT
    finally:
        client.close()
    row = next(item for item in write_diagnostic_events if item["metric"] == "network_write_client_diagnostic")
    assert row["database_outcome"] == "rolled_back"
    assert row["sqlite_errorcode"] & 255 == sqlite3.SQLITE_CONSTRAINT
    assert row["remote_error_class"] == "IntegrityError"


@pytest.mark.parametrize("receipt", [None, {"missing": True}, {"result": "confirmed"}], ids=["unavailable", "missing", "found"])
def test_timeout_confirmation_keeps_unknown_until_commit_evidence(tmp_path, write_diagnostic_events, monkeypatch, receipt):
    from types import SimpleNamespace
    client = NetworkWriteWorkerClient(db_path=str(tmp_path / "unused"), lock_path=str(tmp_path / "unused.lock"), node_id="test")
    client._pipe = SimpleNamespace(send=lambda _: None, close=lambda: None)
    monkeypatch.setattr(client, "_ensure_started", lambda: None)
    confirmations = []
    def timed_out(**kwargs):
        raise NetworkWriteWorkerTimeout(operation_id=kwargs["operation_id"], source=kwargs["source"],
            timeout_sec=.5, phase=kwargs["phase"], outcome_unknown=True)
    def confirm(**kwargs):
        confirmations.append(kwargs)
        return receipt
    monkeypatch.setattr(client, "_recv", timed_out)
    monkeypatch.setattr(client, "_confirm_after_timeout", confirm)
    try:
        if receipt and not receipt.get("missing"):
            assert client.execute(lambda _: None, operation_id="a" * 32, source="nurse_order_mark", timeout_sec=.5) == "confirmed"
        else:
            with pytest.raises(NetworkWriteWorkerTimeout) as raised:
                client.execute(lambda _: None, operation_id="a" * 32, source="nurse_order_mark", timeout_sec=.5)
            assert raised.value.outcome_unknown is (receipt is None)
    finally:
        client.close()
    assert len(confirmations) == 1
    row = next(item for item in write_diagnostic_events if item["metric"] == "network_write_client_diagnostic")
    assert row["database_outcome"] == ("committed" if receipt and not receipt.get("missing") else "unknown")
    assert row["confirmation_result"] == ("unavailable" if receipt is None else "missing" if receipt.get("missing") else "found")
    if receipt and not receipt.get("missing"):
        assert not row["outcome_unknown"]
        assert not row.get("error_class") and not row["failure_stage"]
        assert row["recovered_error"]["error_class"] == "NetworkWriteWorkerTimeout"
        assert row["recovered_error"]["failure_stage"] == "begin"
        assert row["recovered_error"]["outcome_unknown"]
    else:
        assert row["outcome_unknown"] is (receipt is None)
        assert row["error_class"] == "NetworkWriteWorkerTimeout"
        assert not row["recovered_error"]
        assert not row["cleanup_error"]
        if receipt is not None:  # Final missing receipt has the original known-failed flag.
            assert row["attempt_error"]["outcome_unknown"]
            assert row["attempt_error"]["failure_stage"] == "begin"
            assert row["failure_stage"] == "begin"


def test_broken_parent_and_child_summary_sink_does_not_change_commit(tmp_path, monkeypatch):
    from rem_card.app import network_write_worker as worker, write_operation_diagnostics as diagnostics
    source = tmp_path / "central.db"
    _initialize_worker_database(source)
    monkeypatch.setattr(diagnostics, "SLOW_WRITE_MS", 0)
    def broken(*args, **kwargs):
        raise RuntimeError("telemetry unavailable")
    monkeypatch.setattr(diagnostics, "record_metric", broken)
    class Pipe:
        def __init__(self):
            self.responses = []
            self.commands = iter([{"cmd": "execute", "sql": "INSERT INTO worker_items(value) VALUES ('saved')"}, {"cmd": "finish", "result": 1}])
        def send(self, response):
            self.responses.append(response)
        def recv(self):
            return next(self.commands)
    pipe = Pipe()
    worker._worker_write(pipe, {"db_path": str(source), "lock_path": str(tmp_path / "db.lock"),
                              "operation_id": "a" * 32, "source": "nurse_order_mark"})
    assert pipe.responses[-1]["status"] == "committed"
    client = NetworkWriteWorkerClient(db_path=str(source), lock_path=str(tmp_path / "db.lock"), node_id="test", timeout_sec=3)
    try:
        assert client.execute(lambda cursor: cursor.execute("INSERT INTO worker_items(value) VALUES ('saved again')").lastrowid,
                              operation_id="b" * 32, source="nurse_order_mark") == 2
    finally:
        client.close()
    with sqlite3.connect(source) as connection:
        assert connection.execute("SELECT COUNT(*) FROM worker_items").fetchone() == (2,)
    assert not (tmp_path / "db.lock").exists()


@pytest.fixture
def begin_retry_worker(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from rem_card.app import network_write_worker as worker
    source = tmp_path / "central.db"
    _initialize_worker_database(source)
    original_open = worker._open_network_connection
    original_acquire = worker.FileWriteLock.acquire
    clock = SimpleNamespace(now=0.0)
    counts = {"begin": 0, "file_lock": 0}

    def sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))

    def acquire(self, *args, **kwargs):
        counts["file_lock"] += 1
        return False if counts["file_lock"] == 1 else original_acquire(self, *args, **kwargs)

    monkeypatch.setattr(worker.FileWriteLock, "acquire", acquire)

    def run(*, terminal=False):
        class FirstBeginFails:
            def __init__(self, connection):
                self.connection = connection
            def __getattr__(self, name):
                return getattr(self.connection, name)
            def execute(self, sql, *args):
                if sql == "BEGIN IMMEDIATE":
                    counts["begin"] += 1
                    if counts["begin"] == 1:
                        raise sqlite3.OperationalError("disk I/O error" if terminal else "database is locked")
                return self.connection.execute(sql, *args)

        monkeypatch.setattr(worker, "_open_network_connection", lambda path: FirstBeginFails(original_open(path)))
        class Pipe:
            def __init__(self):
                self.responses = []
                self.commands = iter([{"cmd": "execute", "sql": "INSERT INTO worker_items(value) VALUES ('saved')"},
                                      {"cmd": "finish", "result": 1}])
            def send(self, response):
                self.responses.append(response)
            def recv(self):
                return next(self.commands)
        pipe = Pipe()
        worker._worker_write(pipe, {"db_path": str(source), "lock_path": str(tmp_path / "db.lock"),
            "operation_id": "a" * 32, "source": "nurse_order_mark"})
        with sqlite3.connect(source) as connection:
            saved = connection.execute("SELECT COUNT(*) FROM worker_items").fetchone()[0]
        return pipe.responses[-1], counts, saved
    return run


def test_recovered_begin_and_file_lock_retries_are_not_terminal_errors(begin_retry_worker, write_diagnostic_events):
    response, counts, saved = begin_retry_worker()
    assert response["status"] == "committed" and saved == 1
    assert counts == {"begin": 2, "file_lock": 2}
    row = next(item for item in write_diagnostic_events if item["metric"] == "network_write_worker_diagnostic")
    assert row["database_outcome"] == "committed" and row["retries"] == 2
    assert not row.get("error_class") and not row["failure_stage"]
    assert not row["outcome_unknown"] and not row["recovered_error"]


def test_fast_recovered_begin_does_not_emit_failure_summary(begin_retry_worker, write_diagnostic_events, monkeypatch):
    from rem_card.app import write_operation_diagnostics as diagnostics
    monkeypatch.setattr(diagnostics, "SLOW_WRITE_MS", 500)
    response, _, saved = begin_retry_worker()
    assert response["status"] == "committed" and saved == 1
    assert write_diagnostic_events == []


def test_terminal_begin_error_remains_failure_after_file_lock_retry(begin_retry_worker, write_diagnostic_events):
    response, counts, saved = begin_retry_worker(terminal=True)
    assert not response["ok"] and saved == 0
    assert counts == {"begin": 1, "file_lock": 2}
    row = next(item for item in write_diagnostic_events if item["metric"] == "network_write_worker_diagnostic")
    assert row["error_class"] == "OperationalError" and row["failure_stage"] == "begin"
    assert row["database_outcome"] == "not_started" and row["retries"] == 1
    assert not row["recovered_error"]
