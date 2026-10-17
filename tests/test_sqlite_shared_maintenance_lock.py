from __future__ import annotations

import sys
import threading
import unittest
import sqlite3
import time
from contextlib import closing

import pytest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.app.sqlite_shared import (  # noqa: E402
    _SQLITE_NATIVE_MAINTENANCE_LOCK,
    run_integrity_check,
    run_quick_check,
    SQLiteWriteController,
    QueuedWriteDeadlineExceeded,
)


class _BlockingPragmaConnection:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def execute(self, sql: str):
        self.sql = sql
        self.started.set()
        if not self.release.wait(timeout=2.0):
            raise TimeoutError("test timed out waiting to release pragma")
        return self

    def fetchone(self):
        return ("ok",)


class MaintenanceCheckLockTest(unittest.TestCase):
    def _assert_check_does_not_hold_native_lock(self, check_func, expected_sql: str):
        conn = _BlockingPragmaConnection()
        errors: list[BaseException] = []

        def run_check() -> None:
            try:
                self.assertEqual(check_func(conn), (True, "ok"))
            except BaseException as exc:  # pragma: no cover - reported by main thread
                errors.append(exc)

        worker = threading.Thread(target=run_check)
        worker.start()
        self.assertTrue(conn.started.wait(timeout=1.0), "maintenance pragma did not start")

        acquired = _SQLITE_NATIVE_MAINTENANCE_LOCK.acquire(timeout=0.2)
        if acquired:
            _SQLITE_NATIVE_MAINTENANCE_LOCK.release()

        conn.release.set()
        worker.join(timeout=1.0)

        self.assertFalse(worker.is_alive(), "maintenance thread did not finish")
        if errors:
            raise errors[0]
        self.assertEqual(conn.sql, expected_sql)
        self.assertTrue(acquired, "maintenance check held the native sqlite setup lock")

    def test_quick_check_does_not_hold_native_setup_lock_while_running(self):
        self._assert_check_does_not_hold_native_lock(run_quick_check, "PRAGMA quick_check")

    def test_integrity_check_does_not_hold_native_setup_lock_while_running(self):
        self._assert_check_does_not_hold_native_lock(run_integrity_check, "PRAGMA integrity_check")


if __name__ == "__main__":
    unittest.main()


def test_background_write_budget_bounds_real_sqlite_busy_and_restores_timeout(tmp_path):
    path = tmp_path / "budget.db"
    with closing(sqlite3.connect(path, isolation_level=None)) as writer, closing(sqlite3.connect(path, isolation_level=None)) as blocked:
        writer.execute("CREATE TABLE entries(value TEXT)")
        writer.execute("BEGIN IMMEDIATE")
        blocked.execute("PRAGMA busy_timeout=10000")
        controller = SQLiteWriteController(str(path), str(tmp_path / "db.lock"), "test", retry_delay_ms=10)
        started = time.perf_counter()
        with pytest.raises(QueuedWriteDeadlineExceeded):
            with controller.transaction(blocked, source="auto_release", write_options={"write_budget_ms": 120}):
                pytest.fail("native SQLite lock was bypassed")
        assert time.perf_counter() - started < 1.0
        assert blocked.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
        assert not (tmp_path / "db.lock").exists()
        writer.execute("ROLLBACK")
        with controller.transaction(blocked, write_options={"write_budget_ms": 1000}) as cursor:
            cursor.execute("INSERT INTO entries VALUES ('saved')")
        assert writer.execute("SELECT value FROM entries").fetchall() == [("saved",)]


def test_expired_background_body_rolls_back_before_commit(tmp_path):
    path = tmp_path / "body.db"
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        conn.execute("CREATE TABLE entries(value TEXT)")
        controller = SQLiteWriteController(str(path), str(tmp_path / "db.lock"), "test")
        with pytest.raises(QueuedWriteDeadlineExceeded):
            with controller.transaction(conn, write_options={"write_budget_ms": 30}) as cursor:
                cursor.execute("INSERT INTO entries VALUES ('discard')")
                time.sleep(0.04)
        assert conn.execute("SELECT * FROM entries").fetchall() == []
        assert not (tmp_path / "db.lock").exists()
