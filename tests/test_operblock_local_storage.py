from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta

from tests import test_operblock_offline_migration as _fixtures
from rem_card.app.operblock_local_storage import (
    backup_local_operations, latest_valid_local_backup, prune_verified_local_cases,
)
from rem_card.app.operblock_local_destination import CentralArchiveConnection
from rem_card.app.operblock_local_sync import _export_worker, completed_pending_count


class LocalStorageTest(_fixtures.OperBlockOfflineMigrationTest):
    def test_retention_removes_only_old_verified_case_and_preserves_database(self):
        sent_id = self._case()
        self.assertTrue(self._run().ok)
        active_id = self._case(status="active")
        old = (datetime.now().astimezone() - timedelta(days=91)).isoformat()
        self.local.execute("UPDATE operation_cases SET migrated_at=? WHERE id=?", (old, sent_id))
        self.local.commit()
        self.assertEqual(prune_verified_local_cases(self.central, str(self.root)), 1)
        self.assertTrue(self.local_path.is_file())
        self.assertEqual(self.local.execute("SELECT id FROM operation_cases").fetchone()[0], active_id)

    def test_retention_keeps_local_change_and_recent_confirmation(self):
        case_id = self._case()
        self.assertTrue(self._run().ok)
        self.assertEqual(prune_verified_local_cases(self.central, str(self.root)), 0)
        old = (datetime.now().astimezone() - timedelta(days=91)).isoformat()
        self.local.execute("UPDATE operation_cases SET migrated_at=?, planned_operation_name='Правка' WHERE id=?", (old, case_id))
        self.local.commit()
        self.assertEqual(prune_verified_local_cases(self.central, str(self.root)), 0)

    def test_backup_is_valid_and_changes_following_local_save(self):
        self._case()
        first = backup_local_operations(str(self.root))
        self.assertEqual(latest_valid_local_backup(str(self.root)), first)
        self._case()
        second = backup_local_operations(str(self.root))
        with closing(sqlite3.connect(second)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM operation_cases").fetchone()[0], 2)

    def test_central_gateway_never_creates_missing_database(self):
        target = self.root / "absent_central"
        with self.assertRaises(sqlite3.OperationalError):
            CentralArchiveConnection(str(target))
        self.assertFalse(target.exists())

    def test_real_gateway_worker_exports_and_retries_without_duplicate(self):
        self._case()
        network_root = self.root / "network"
        central_path = network_root / "archiv" / "rao_journal.db"
        central_path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(central_path)) as conn:
            self.central._remcard_conn.backup(conn)

        class Pipe:
            result = None
            def send(self, value):
                self.result = value
            def close(self):
                pass

        pipe = Pipe()
        _export_worker(str(network_root), str(self.root), pipe)
        self.assertTrue(pipe.result["ok"], pipe.result)
        self.assertEqual(completed_pending_count(str(self.root)), 0)
        _export_worker(str(network_root), str(self.root), pipe)
        self.assertTrue(pipe.result["ok"], pipe.result)
        with closing(sqlite3.connect(central_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM operation_cases").fetchone()[0], 1)

    def test_missing_network_keeps_pending_and_creates_local_backup(self):
        self._case()
        class Pipe:
            result = None
            def send(self, value):
                self.result = value
            def close(self):
                pass
        pipe = Pipe()
        _export_worker(str(self.root / "unavailable"), str(self.root), pipe)
        self.assertFalse(pipe.result["ok"])
        self.assertEqual(completed_pending_count(str(self.root)), 1)
        self.assertIsNotNone(latest_valid_local_backup(str(self.root)))
