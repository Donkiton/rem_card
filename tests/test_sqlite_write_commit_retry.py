from __future__ import annotations

import sqlite3
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from rem_card.app.sqlite_shared import SQLiteWriteController, configure_connection


class SQLiteWriteCommitRetryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "central.db"
        self.lock_path = self.root / "db.lock"
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            configure_connection(conn, profile="network")
            conn.execute(
                "CREATE TABLE clinical_events (id INTEGER PRIMARY KEY, value TEXT)"
            )
        finally:
            conn.close()

    def tearDown(self):
        self._tmp.cleanup()

    def test_interactive_commit_waits_for_reader_without_replaying_write(self):
        reader = sqlite3.connect(
            self.db_path,
            isolation_level=None,
            check_same_thread=False,
        )
        writer = sqlite3.connect(self.db_path, isolation_level=None)
        configure_connection(reader, readonly=True, profile="network")
        configure_connection(writer, profile="network")
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM clinical_events").fetchall()

        reader_released = threading.Event()

        def release_reader():
            time.sleep(0.35)
            reader.execute("ROLLBACK")
            reader_released.set()

        release_thread = threading.Thread(target=release_reader, daemon=True)
        release_thread.start()
        controller = SQLiteWriteController(
            db_path=str(self.db_path),
            lock_path=str(self.lock_path),
            owner_id="test-nurse",
            retry_delay_ms=50,
        )
        try:
            with patch("rem_card.app.sqlite_shared.record_metric") as metric:
                with controller.transaction(
                    writer,
                    source="nurse_order_mark:test",
                    write_options={
                        "interactive": True,
                        "role": "nurse",
                        "source": "nurse_order_mark:test",
                        "timeout_ms": 5000,
                    },
                ) as cursor:
                    cursor.execute(
                        "INSERT INTO clinical_events(value) VALUES ('saved')"
                    )

            release_thread.join(timeout=2.0)
            self.assertTrue(reader_released.is_set())
            rows = writer.execute(
                "SELECT value FROM clinical_events ORDER BY id"
            ).fetchall()
            self.assertEqual([tuple(row) for row in rows], [("saved",)])
            metric_names = [call.args[0] for call in metric.call_args_list]
            self.assertIn("sqlite_write_commit_retry", metric_names)
            self.assertIn("sqlite_write_commit_wait_ms", metric_names)
        finally:
            if reader.in_transaction:
                reader.execute("ROLLBACK")
            release_thread.join(timeout=2.0)
            reader.close()
            writer.close()

    def test_interactive_commit_timeout_rolls_back_without_replaying_write(self):
        reader = sqlite3.connect(self.db_path, isolation_level=None)
        writer = sqlite3.connect(self.db_path, isolation_level=None)
        configure_connection(reader, readonly=True, profile="network")
        configure_connection(writer, profile="network")
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM clinical_events").fetchall()
        controller = SQLiteWriteController(
            db_path=str(self.db_path),
            lock_path=str(self.lock_path),
            owner_id="test-nurse",
            retry_delay_ms=50,
        )
        body_calls = 0
        started = time.perf_counter()
        try:
            with (
                patch(
                    "rem_card.app.sqlite_shared."
                    "_bounded_opblock_interactive_timeout_ms",
                    return_value=300,
                ),
                patch("rem_card.app.sqlite_shared.record_metric") as metric,
            ):
                with self.assertRaisesRegex(
                    sqlite3.OperationalError,
                    "database is locked",
                ):
                    with controller.transaction(
                        writer,
                        source="nurse_order_mark:test",
                        write_options={
                            "interactive": True,
                            "role": "nurse",
                            "source": "nurse_order_mark:test",
                            "timeout_ms": 300,
                        },
                    ) as cursor:
                        body_calls += 1
                        cursor.execute(
                            "INSERT INTO clinical_events(value) VALUES ('no-save')"
                        )

            elapsed = time.perf_counter() - started
            self.assertLess(elapsed, 0.75)
            self.assertEqual(body_calls, 1)
            self.assertFalse(writer.in_transaction)
            metric_names = [call.args[0] for call in metric.call_args_list]
            self.assertIn("sqlite_write_commit_retry", metric_names)
            self.assertIn("sqlite_write_commit_timeout", metric_names)

            reader.execute("ROLLBACK")
            rows = writer.execute(
                "SELECT value FROM clinical_events ORDER BY id"
            ).fetchall()
            self.assertEqual(rows, [])
        finally:
            if reader.in_transaction:
                reader.execute("ROLLBACK")
            reader.close()
            writer.close()


if __name__ == "__main__":
    unittest.main()
