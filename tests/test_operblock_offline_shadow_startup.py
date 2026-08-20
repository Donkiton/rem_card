from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.app.operblock_offline_store import (  # noqa: E402
    OperBlockOfflineSessionMetadata,
    ensure_operblock_offline_dirs,
    has_active_local_operblock_case,
    mirror_active_operblock_cases_from_network_db,
    pending_completed_local_cases_count,
    read_operblock_offline_metadata,
    write_operblock_offline_metadata,
)


class _NetworkManagerWithoutActiveCases:
    runtime_context = SimpleNamespace(mode="network")
    db_path = "network.db"

    def fetch_all_remcard(self, _query, _params=()):
        return []


class OperBlockOfflineShadowStartupTest(unittest.TestCase):
    def test_shadow_only_active_case_does_not_block_network_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, db_path = self._prepare_store(tmp)
            self._insert_case(db_path, status="active", migration_status="shadow")
            self._insert_shadow_mapping(db_path, entity_name="operation_cases", remote_id=100, local_id=1)

            self.assertFalse(has_active_local_operblock_case(str(root)))
            metadata = read_operblock_offline_metadata(str(root))
            self.assertIsNotNone(metadata)
            self.assertIsNone(metadata.active_case_uuid)
            self.assertEqual(metadata.migration_status, "shadow")

    def test_real_local_active_case_still_blocks_network_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, db_path = self._prepare_store(tmp)
            self._insert_case(db_path, status="active", migration_status="active")

            self.assertTrue(has_active_local_operblock_case(str(root)))

    def test_shadow_case_with_local_only_rows_blocks_network_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, db_path = self._prepare_store(tmp)
            self._insert_case(db_path, status="active", migration_status="shadow")
            self._insert_shadow_mapping(db_path, entity_name="operation_cases", remote_id=100, local_id=1)
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute("INSERT INTO operblock_timeline_events (id, operation_case_id) VALUES (10, 1)")
                conn.commit()

            self.assertTrue(has_active_local_operblock_case(str(root)))

    def test_closed_shadow_case_is_not_pending_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, db_path = self._prepare_store(tmp)
            self._insert_case(db_path, status="closed", migration_status="shadow")
            self._insert_shadow_mapping(db_path, entity_name="operation_cases", remote_id=100, local_id=1)

            self.assertEqual(pending_completed_local_cases_count(str(root)), 0)
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute("UPDATE operation_cases SET migration_status = 'pending' WHERE id = 1")
                conn.commit()
            self.assertEqual(pending_completed_local_cases_count(str(root)), 1)

    def test_network_mirror_without_active_cases_discards_stale_shadow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, db_path = self._prepare_store(tmp)
            self._insert_case(db_path, status="active", migration_status="active")
            self._insert_shadow_mapping(db_path, entity_name="operation_cases", remote_id=100, local_id=1)

            self.assertEqual(
                mirror_active_operblock_cases_from_network_db(
                    _NetworkManagerWithoutActiveCases(),
                    root=str(root),
                ),
                0,
            )
            with closing(sqlite3.connect(db_path)) as conn:
                row = conn.execute("SELECT status, migration_status, excluded_from_migration FROM operation_cases").fetchone()
            self.assertEqual(row, ("cancelled", "discarded", 1))

    @staticmethod
    def _prepare_store(tmp: str) -> tuple[Path, str]:
        root = Path(tmp)
        paths = ensure_operblock_offline_dirs(str(root))
        db_path = str(Path(paths["active"]) / "operblock_local.db")
        with closing(sqlite3.connect(db_path)) as conn:
            conn.executescript(
                """
                CREATE TABLE operation_cases (
                    id INTEGER PRIMARY KEY,
                    admission_id INTEGER,
                    offline_case_uuid TEXT,
                    status TEXT,
                    migration_status TEXT,
                    migrated_at TEXT,
                    excluded_from_migration INTEGER NOT NULL DEFAULT 0,
                    last_modified_by TEXT
                );
                CREATE TABLE opblock_offline_shadow_map (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    offline_case_uuid TEXT NOT NULL,
                    entity_name TEXT NOT NULL,
                    remote_id INTEGER NOT NULL,
                    local_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now')),
                    updated_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now')),
                    UNIQUE(offline_case_uuid, entity_name, remote_id)
                );
                CREATE TABLE operation_table_assignments (id INTEGER PRIMARY KEY, operation_case_id INTEGER);
                CREATE TABLE operblock_timeline_events (id INTEGER PRIMARY KEY, operation_case_id INTEGER);
                CREATE TABLE vitals (id INTEGER PRIMARY KEY, admission_id INTEGER);
                CREATE TABLE orders (id INTEGER PRIMARY KEY, admission_id INTEGER);
                CREATE TABLE patient_status_events (id INTEGER PRIMARY KEY, admission_id INTEGER);
                """
            )
            conn.commit()
        write_operblock_offline_metadata(
            OperBlockOfflineSessionMetadata(
                offline_session_id="active",
                offline_session_uuid="test-session",
                local_db_path=db_path,
                settings_snapshot_path=str(Path(paths["active"]) / "settings" / "opblock_settings_snapshot.db"),
                created_at="2026-06-19T00:00:00+10:00",
                last_opened_at="2026-06-19T00:00:00+10:00",
                source_workstation_id="test-host",
                migration_status="active",
            ),
            str(root),
        )
        return root, db_path

    @staticmethod
    def _insert_case(db_path: str, *, status: str, migration_status: str):
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                """
                INSERT INTO operation_cases (
                    id, admission_id, offline_case_uuid, status, migration_status,
                    migrated_at, excluded_from_migration
                ) VALUES (1, 11, 'opblock:shadow-case', ?, ?, NULL, 0)
                """,
                (status, migration_status),
            )
            conn.commit()

    @staticmethod
    def _insert_shadow_mapping(db_path: str, *, entity_name: str, remote_id: int, local_id: int):
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                """
                INSERT INTO opblock_offline_shadow_map (
                    offline_case_uuid, entity_name, remote_id, local_id
                ) VALUES ('opblock:shadow-case', ?, ?, ?)
                """,
                (entity_name, int(remote_id), int(local_id)),
            )
            conn.commit()


if __name__ == "__main__":
    unittest.main()
