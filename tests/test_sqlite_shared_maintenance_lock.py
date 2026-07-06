from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.app.sqlite_shared import (  # noqa: E402
    _SQLITE_NATIVE_MAINTENANCE_LOCK,
    run_integrity_check,
    run_quick_check,
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
