import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication
from rem_card.app import operblock_local_sync as sync


class LocalSyncTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_shutdown_callback_occurs_once_even_when_local_queue_read_fails(self):
        with tempfile.TemporaryDirectory() as root:
            scheduler = sync.OperBlockLocalSyncScheduler("", local_root=root)
            calls = []
            with patch.object(sync, "completed_pending_count", side_effect=RuntimeError("unavailable local queue")):
                scheduler.begin_close(lambda: calls.append("closed"))
            self.app.processEvents()
            self.assertEqual(calls, ["closed"])
            self.assertTrue(scheduler._stopped)

    def test_queue_only_mode_sleeps_when_empty_and_retries_until_drained(self):
        with tempfile.TemporaryDirectory() as root:
            scheduler = sync.OperBlockLocalSyncScheduler("", local_root=root, maintenance_enabled=False)
            try:
                with patch.object(sync, "completed_pending_count", return_value=0), patch.object(sync.multiprocessing, "get_context") as spawn:
                    scheduler.request_sync()
                    self.assertFalse(scheduler._retry.isActive())
                    spawn.assert_not_called()
                with patch.object(sync, "completed_pending_count", return_value=2):
                    scheduler._finish({"ok": False})
                    self.assertTrue(scheduler._retry.isActive())
                scheduler._retry.stop()
                with patch.object(sync, "completed_pending_count", return_value=0):
                    scheduler._finish({"ok": True})
                    self.assertFalse(scheduler._retry.isActive())
                    scheduler.set_maintenance_enabled(True)
                    self.assertTrue(scheduler._retry.isActive())
                    scheduler.set_maintenance_enabled(False)
                    self.assertFalse(scheduler._retry.isActive())
            finally:
                scheduler.stop()

    def test_queue_only_worker_does_not_backup_or_access_network_when_empty(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as root:
            pipe = Mock()
            with patch('rem_card.app.operblock_local_storage.backup_local_operations') as backup, patch('rem_card.app.operblock_local_destination.open_central_for_operblock') as central:
                sync._export_worker('unused-network-root', root, pipe, False)
            backup.assert_not_called()
            central.assert_not_called()
            pipe.send.assert_called_once_with({'ok': True, 'pending': 0})

    def test_worker_timeout_terminates_process_and_retains_retry(self):
        class Process:
            alive = True
            def is_alive(self): return self.alive
            def terminate(self): self.alive = False
            def join(self, timeout): pass
            def close(self): pass
        class Receiver:
            def poll(self): return False
            def close(self): pass
        with tempfile.TemporaryDirectory() as root:
            scheduler = sync.OperBlockLocalSyncScheduler("", local_root=root)
            results = []
            scheduler.status_changed.connect(results.append)
            process = Process()
            scheduler._process, scheduler._receiver = process, Receiver()
            scheduler._started = time.monotonic() - 60
            scheduler._poll_result()
            self.assertFalse(process.alive)
            self.assertFalse(results[-1]["ok"])
            self.assertTrue(scheduler._retry.isActive())
            scheduler.stop()

    def test_active_database_backups_run_without_central_destination(self):
        import sqlite3
        from pathlib import Path
        from contextlib import closing
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "active" / "operblock_local.db"
            path.parent.mkdir()
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("CREATE TABLE local_work(value TEXT)")
                conn.commit()
            scheduler = sync.OperBlockLocalSyncScheduler("", local_root=root)
            statuses = []
            scheduler.status_changed.connect(statuses.append)
            try:
                scheduler.request_sync()
                self.assertIsNotNone(scheduler._process)
                self.assertEqual(statuses[-1]["state"], "maintenance")
                deadline = time.monotonic() + 20
                while scheduler._process is not None and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(0.02)
                self.assertIsNone(scheduler._process)
                self.assertTrue(list((Path(root) / "backups").glob("operations_*.db")))
            finally:
                scheduler.stop()
