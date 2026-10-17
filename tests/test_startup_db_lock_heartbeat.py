import json
import tempfile
import unittest
import os
import socket
import time
from unittest.mock import patch
from pathlib import Path

from rem_card.app.sqlite_shared import FileWriteLock
from rem_card.app.startup_db_guard import _LockHeartbeat, _remove_stale_startup_lock_if_needed


class StartupDbLockHeartbeatTests(unittest.TestCase):
    def _clean(self, path):
        with patch("rem_card.app.startup_db_guard.write_audit_event"):
            _remove_stale_startup_lock_if_needed(
                str(path), stale_timeout_sec=120, source="db_profile",
                role="nurse", baza_dir=str(path.parent),
            )

    def test_old_live_and_foreign_owner_are_preserved_even_without_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "db.lock")
            for host, timestamp in ((socket.gethostname(), time.time() - 7200), ("foreign-host", None)):
                payload = {"host": host, "pid": os.getpid(), "source": "clinical", "lock_token": "active"}
                if timestamp is not None:
                    payload["timestamp"] = timestamp
                path.write_text(json.dumps(payload), encoding="utf-8")
                before = path.read_bytes()
                self._clean(path)
                self.assertEqual(path.read_bytes(), before)

    def test_confirmed_dead_local_owner_is_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "db.lock")
            path.write_text(json.dumps({"host": socket.gethostname(), "pid": 12345,
                                        "timestamp": time.time(), "lock_token": "dead"}), encoding="utf-8")
            with patch("rem_card.app.sqlite_shared.is_local_pid_alive", return_value=False):
                self._clean(path)
            self.assertFalse(path.exists())

    def test_unknown_local_owner_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "db.lock")
            path.write_text(json.dumps({"host": socket.gethostname(), "pid": 12345,
                                        "timestamp": time.time() - 7200, "lock_token": "unknown"}), encoding="utf-8")
            with patch("rem_card.app.sqlite_shared.is_local_pid_alive", return_value=None):
                self._clean(path)
            self.assertTrue(path.exists())

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


class ReplicaLeaseAbandonmentTests(unittest.TestCase):
    def _lease(self, path):
        return FileWriteLock(str(path), stale_timeout_sec=1,
                             allow_expired_lease_cleanup=True,
                             allow_legacy_replica_cleanup=True)

    def test_expired_foreign_live_unknown_and_unidentified_owners_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "snapshot.lock")
            cases = (("foreign-host", 12345, False),
                     (socket.gethostname(), os.getpid(), True),
                     (socket.gethostname(), 12345, None),
                     ("", 12345, False),
                     (socket.gethostname(), None, False))
            for host, pid, alive in cases:
                for has_expiry in (True, False):
                    payload = {"host": host, "pid": pid, "source": "local_replica_snapshot",
                               "timestamp": time.time() - 7200, "lock_token": "owner"}
                    if has_expiry:
                        payload["lease_expires_at"] = time.time() - 3600
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    before = path.read_bytes()
                    with patch("rem_card.app.sqlite_shared.is_local_pid_alive", return_value=alive):
                        self.assertFalse(self._lease(path).cleanup_abandoned(source="db_rotation_replica_wait"))
                        self.assertFalse(self._lease(path).acquire("other-owner", "local_replica_snapshot"))
                    self.assertEqual(path.read_bytes(), before)

    def test_dead_local_owner_is_recovered_without_waiting_for_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "snapshot.lock")
            path.write_text(json.dumps({"host": socket.gethostname(), "pid": 12345,
                                        "source": "local_replica_snapshot", "lock_token": "dead",
                                        "timestamp": time.time(), "lease_expires_at": time.time() + 3600}),
                            encoding="utf-8")
            with patch("rem_card.app.sqlite_shared.is_local_pid_alive", return_value=False):
                self.assertTrue(self._lease(path).cleanup_abandoned(source="db_rotation_replica_wait"))
            self.assertFalse(path.exists())

    def test_replaced_owner_is_preserved_during_dead_pid_check(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "snapshot.lock")
            dead = {"host": socket.gethostname(), "pid": 12345,
                    "source": "local_replica_snapshot", "lock_token": "dead", "timestamp": time.time()}
            replacement = {**dead, "pid": os.getpid(), "lock_token": "replacement"}
            path.write_text(json.dumps(dead), encoding="utf-8")

            def replace_during_probe(_pid):
                path.write_text(json.dumps(replacement), encoding="utf-8")
                return False

            with patch("rem_card.app.sqlite_shared.is_local_pid_alive", side_effect=replace_during_probe):
                self.assertFalse(self._lease(path).cleanup_abandoned(source="db_rotation_replica_wait"))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), replacement)


if __name__ == "__main__":
    unittest.main()
