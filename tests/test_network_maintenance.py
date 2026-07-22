from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.app.network_maintenance import (  # noqa: E402
    NETWORK_MAINTENANCE_LOCK_FILE,
    find_active_network_sessions,
)


class NetworkMaintenanceTest(unittest.TestCase):
    @staticmethod
    def _write_lock(directory: str, name: str, payload: dict) -> str:
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return path

    def test_fresh_mtime_is_used_as_role_lock_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_lock(
                tmp,
                "doctor.lock",
                {
                    "timestamp": time.time() - 3600,
                    "role": "doctor",
                    "pid": 111,
                    "host": "remote-pc",
                },
            )
            os.utime(path, None)

            active = find_active_network_sessions(tmp, stale_sec=75)

            self.assertEqual([item.role for item in active], ["doctor"])

    def test_current_process_stale_and_maintenance_locks_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_lock(
                tmp,
                "nurse.lock",
                {
                    "timestamp": time.time(),
                    "role": "nurse",
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                },
            )
            stale_path = self._write_lock(
                tmp,
                "doctor.lock",
                {
                    "timestamp": time.time() - 3600,
                    "role": "doctor",
                    "pid": 222,
                    "host": "remote-pc",
                },
            )
            old = time.time() - 3600
            os.utime(stale_path, (old, old))
            self._write_lock(
                tmp,
                NETWORK_MAINTENANCE_LOCK_FILE,
                {"timestamp": time.time(), "pid": 333, "host": "remote-pc"},
            )

            self.assertEqual(find_active_network_sessions(tmp, stale_sec=75), [])


if __name__ == "__main__":
    unittest.main()
