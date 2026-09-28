from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


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
import rem_card.app.operblock_offline_store as offline_store  # noqa: E402


class _NetworkManagerWithoutActiveCases:
    runtime_context = SimpleNamespace(mode="network")
    db_path = "network.db"

    def fetch_all_remcard(self, _query, _params=()):
        return []


class _SqliteNetworkManager:
    runtime_context = SimpleNamespace(mode="network")
    db_path = "network.db"

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE patients (id INTEGER PRIMARY KEY, full_name TEXT);
            CREATE TABLE admissions (
                id INTEGER PRIMARY KEY,
                patient_id INTEGER,
                unit_scope TEXT,
                admission_type TEXT
            );
            CREATE TABLE operation_cases (
                id INTEGER PRIMARY KEY,
                patient_id INTEGER,
                admission_id INTEGER,
                table_code TEXT,
                status TEXT,
                offline_case_uuid TEXT
            );
            CREATE TABLE operation_table_assignments (
                id INTEGER PRIMARY KEY,
                operation_case_id INTEGER,
                admission_id INTEGER,
                table_code TEXT,
                status TEXT,
                released_at TEXT
            );
            CREATE TABLE operblock_timeline_events (
                id INTEGER PRIMARY KEY,
                operation_case_id INTEGER,
                admission_id INTEGER,
                parent_event_id INTEGER,
                source_order_id INTEGER
            );
            CREATE TABLE vitals (id INTEGER PRIMARY KEY, admission_id INTEGER);
            CREATE TABLE orders (id INTEGER PRIMARY KEY, admission_id INTEGER);
            CREATE TABLE patient_status_events (id INTEGER PRIMARY KEY, admission_id INTEGER);
            INSERT INTO patients (id, full_name) VALUES (20, 'Новый пациент');
            INSERT INTO admissions (id, patient_id) VALUES (21, 20);
            INSERT INTO operation_cases (
                id, patient_id, admission_id, table_code, status, offline_case_uuid
            ) VALUES (200, 20, 21, 'emergency', 'active', 'opblock:new-case');
            """
        )

    def fetch_all_remcard(self, query, params=()):
        return self.conn.execute(query, params).fetchall()

    def fetch_one_remcard(self, query, params=()):
        return self.conn.execute(query, params).fetchone()

    def close(self):
        self.conn.close()


class OperBlockOfflineShadowStartupTest(unittest.TestCase):
    def test_permanent_local_start_retires_only_untouched_legacy_shadow(self):
        from rem_card.app.operblock_offline_store import start_or_resume_operblock_offline_session
        for local_changes in (False, True):
            with self.subTest(local_changes=local_changes), tempfile.TemporaryDirectory() as tmp:
                root, db_path = self._prepare_store(tmp)
                self._insert_case(db_path, status="active", migration_status="shadow")
                self._insert_shadow_mapping(db_path, entity_name="operation_cases", remote_id=100, local_id=1)
                if local_changes:
                    with closing(sqlite3.connect(db_path)) as conn:
                        conn.execute("INSERT INTO operblock_timeline_events (id, operation_case_id) VALUES (10, 1)")
                        conn.commit()
                start_or_resume_operblock_offline_session(reason="unified_per_workstation_local", root=str(root))
                with closing(sqlite3.connect(db_path)) as conn:
                    row = conn.execute("SELECT status, migration_status FROM operation_cases WHERE id=1").fetchone()
                self.assertEqual(row, ("active", "shadow") if local_changes else ("cancelled", "discarded"))

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

    def test_network_mirror_replaces_stale_shadow_before_unique_table_insert(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, db_path = self._prepare_store(tmp)
            self._insert_case(db_path, status="active", migration_status="shadow")
            self._insert_shadow_mapping(db_path, entity_name="operation_cases", remote_id=100, local_id=1)
            manager = _SqliteNetworkManager()
            try:
                with patch.object(
                    offline_store,
                    "_ensure_local_runtime_schema_ready",
                    return_value=SimpleNamespace(medical_db_path=db_path),
                ):
                    mirrored = mirror_active_operblock_cases_from_network_db(
                        manager,
                        reason="operblock_update_order_route:40:504",
                        root=str(root),
                    )
            finally:
                manager.close()

            self.assertEqual(mirrored, 1)
            with closing(sqlite3.connect(db_path)) as conn:
                rows = conn.execute(
                    """
                    SELECT offline_case_uuid, table_code, status, migration_status, excluded_from_migration
                    FROM operation_cases
                    ORDER BY id
                    """
                ).fetchall()
            self.assertEqual(
                rows,
                [
                    ("opblock:shadow-case", "emergency", "cancelled", "discarded", 1),
                    ("opblock:new-case", "emergency", "active", "shadow", 0),
                ],
            )

    def test_network_mirror_does_not_discard_unmapped_local_case_on_table_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, db_path = self._prepare_store(tmp)
            self._insert_case(db_path, status="active", migration_status="active")
            manager = _SqliteNetworkManager()
            try:
                with patch.object(
                    offline_store,
                    "_ensure_local_runtime_schema_ready",
                    return_value=SimpleNamespace(medical_db_path=db_path),
                ):
                    with self.assertRaises(sqlite3.IntegrityError):
                        mirror_active_operblock_cases_from_network_db(
                            manager,
                            reason="operblock_update_order_route:40:504",
                            root=str(root),
                        )
            finally:
                manager.close()

            with closing(sqlite3.connect(db_path)) as conn:
                case_rows = conn.execute(
                    "SELECT offline_case_uuid, status, migration_status FROM operation_cases ORDER BY id"
                ).fetchall()
                copied_patients = int(conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0])
            self.assertEqual(case_rows, [("opblock:shadow-case", "active", "active")])
            self.assertEqual(copied_patients, 0)

    def test_network_mirror_rolls_back_stale_shadow_discard_when_copy_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, db_path = self._prepare_store(tmp)
            self._insert_case(db_path, status="active", migration_status="shadow")
            self._insert_shadow_mapping(db_path, entity_name="operation_cases", remote_id=100, local_id=1)
            manager = _SqliteNetworkManager()
            manager.conn.execute("DELETE FROM patients WHERE id = 20")
            manager.conn.commit()
            try:
                with patch.object(
                    offline_store,
                    "_ensure_local_runtime_schema_ready",
                    return_value=SimpleNamespace(medical_db_path=db_path),
                ):
                    with self.assertRaisesRegex(RuntimeError, "пациент не найден"):
                        mirror_active_operblock_cases_from_network_db(
                            manager,
                            reason="operblock_update_order_route:40:504",
                            root=str(root),
                        )
            finally:
                manager.close()

            with closing(sqlite3.connect(db_path)) as conn:
                row = conn.execute(
                    """
                    SELECT status, migration_status, excluded_from_migration
                    FROM operation_cases
                    WHERE id = 1
                    """
                ).fetchone()
            self.assertEqual(row, ("active", "shadow", 0))

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
                    patient_id INTEGER,
                    admission_id INTEGER,
                    table_code TEXT,
                    offline_case_uuid TEXT,
                    status TEXT,
                    migration_status TEXT,
                    migrated_at TEXT,
                    migrated_remote_id INTEGER,
                    original_local_id INTEGER,
                    excluded_from_migration INTEGER NOT NULL DEFAULT 0,
                    offline_session_id TEXT,
                    last_modified_by TEXT
                );
                CREATE UNIQUE INDEX idx_operation_cases_one_active_per_table
                ON operation_cases(table_code)
                WHERE status = 'active';
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
                CREATE TABLE patients (id INTEGER PRIMARY KEY, full_name TEXT);
                CREATE TABLE admissions (
                    id INTEGER PRIMARY KEY,
                    patient_id INTEGER,
                    unit_scope TEXT,
                    admission_type TEXT
                );
                CREATE TABLE operation_table_assignments (
                    id INTEGER PRIMARY KEY,
                    operation_case_id INTEGER,
                    admission_id INTEGER,
                    table_code TEXT,
                    status TEXT,
                    released_at TEXT,
                    last_modified_by TEXT,
                    updated_at TEXT
                );
                CREATE TABLE operblock_timeline_events (
                    id INTEGER PRIMARY KEY,
                    operation_case_id INTEGER,
                    admission_id INTEGER,
                    parent_event_id INTEGER,
                    source_order_id INTEGER
                );
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
                    id, patient_id, admission_id, table_code, offline_case_uuid, status, migration_status,
                    migrated_at, excluded_from_migration
                ) VALUES (1, 10, 11, 'emergency', 'opblock:shadow-case', ?, ?, NULL, 0)
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
