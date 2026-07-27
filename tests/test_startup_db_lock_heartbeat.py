import json
import tempfile
import unittest
from pathlib import Path

from rem_card.app.sqlite_shared import FileWriteLock
from rem_card.app.startup_db_guard import _LockHeartbeat


class StartupDbLockHeartbeatTests(unittest.TestCase):
    def test_heartbeat_preserves_token_and_lock_can_be_released(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir, "db.lock")
            startup_lock = FileWriteLock(str(lock_path), stale_timeout_sec=120)
            self.assertTrue(startup_lock.acquire("startup-owner", "db_profile"))

            before = json.loads(lock_path.read_text(encoding="utf-8"))
            heartbeat = _LockHeartbeat(startup_lock, role="doctor", source="db_profile")
            heartbeat.start()
            try:
                after = json.loads(lock_path.read_text(encoding="utf-8"))
                self.assertEqual(after["lock_token"], before["lock_token"])
                self.assertEqual(after["user_id"], "startup-owner")
                self.assertEqual(after["thread_id"], before["thread_id"])
                self.assertEqual(after["role"], "doctor")
            finally:
                heartbeat.stop()

            self.assertTrue(startup_lock.release())
            self.assertFalse(lock_path.exists())

            connection_lock = FileWriteLock(str(lock_path), stale_timeout_sec=120)
            self.assertTrue(connection_lock.acquire("connection-owner", "connection_profile"))
            self.assertTrue(connection_lock.release())


if __name__ == "__main__":
    unittest.main()
