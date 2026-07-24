from __future__ import annotations

import json
import sqlite3
import time
import unittest
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
            time.sleep(1.15)
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
