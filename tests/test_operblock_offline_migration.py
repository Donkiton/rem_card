from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.app.operblock_offline_migration import _migrate_one_case, run_pending_operblock_offline_migration
from rem_card.app.operblock_offline_transfer import build_case_payload, local_installation_origin_id, payload_hash
from rem_card.app.operblock_offline_store import (
    OperBlockOfflineSessionMetadata,
    ensure_operblock_offline_dirs,
    write_operblock_offline_metadata,
)
from rem_card.app.operblock_schema import _apply_operblock_schema
from rem_card.app.unified_db_schema import ensure_unified_schema


class _Central:
    runtime_context = type("Runtime", (), {"mode": "network"})()
    db_path = "test-central.db"

    def __init__(self):
        self._remcard_conn = sqlite3.connect(":memory:")
        self._remcard_conn.row_factory = sqlite3.Row
        ensure_unified_schema(self._remcard_conn)
        _apply_operblock_schema(self._remcard_conn.cursor())
        self._remcard_conn.commit()

    def run_write_operation(self, operation, source=""):
        cursor = self._remcard_conn.cursor()
        try:
            value = operation(cursor)
            self._remcard_conn.commit()
            return value
        except Exception:
            self._remcard_conn.rollback()
            raise

    def run_read_operation(self, operation, source=""):
        return operation(self._remcard_conn.cursor())

    def create_validated_backup(self, **_kwargs):
        return "memory-backup"

    def close(self):
        self._remcard_conn.close()


class OperBlockOfflineMigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "offline"
        ensure_operblock_offline_dirs(str(self.root))
        self.local_path = self.root / "active" / "operblock_local.db"
        self.local = sqlite3.connect(self.local_path)
        self.local.row_factory = sqlite3.Row
        ensure_unified_schema(self.local)
        _apply_operblock_schema(self.local.cursor())
        self.local.commit()
        write_operblock_offline_metadata(
            OperBlockOfflineSessionMetadata(
                offline_session_id="active", offline_session_uuid="session-1",
                local_db_path=str(self.local_path), settings_snapshot_path="",
                created_at="2026-09-28T00:00:00+00:00", last_opened_at="2026-09-28T00:00:00+00:00",
                source_workstation_id="test-workstation",
            ),
            str(self.root),
        )
        self.central = _Central()

    def tearDown(self):
        self.local.close()
        self.central.close()
        self.tmp.cleanup()

    def _case(self, *, status="closed", protocol=17, title="Аппендэктомия"):
        cursor = self.local.cursor()
        cursor.execute("INSERT INTO patients (full_name, birth_date) VALUES (?, ?)", ("Иванов Иван", "1980-01-01"))
        patient_id = cursor.lastrowid
        cursor.execute(
            """INSERT INTO admissions (patient_id, bed_number, history_number, admission_datetime,
               unit_scope, admission_type, is_active) VALUES (?, 1, ?, ?, 'operblock', 'operblock', 1)""",
            (patient_id, "H-1", "2026-09-28T08:00:00"),
        )
        admission_id = cursor.lastrowid
        cursor.execute(
            """INSERT INTO operation_cases
               (patient_id, admission_id, table_code, status, started_at, ended_at,
                planned_operation_name, anesthesia_protocol_date, anesthesia_protocol_number, revision)
               VALUES (?, ?, 'emergency', ?, ?, ?, ?, '2026-09-28', ?, 0)""",
            (patient_id, admission_id, status, "2026-09-28T08:00:00",
             "2026-09-28T09:00:00" if status == "closed" else None, title, protocol),
        )
        self.local.commit()
        return int(cursor.lastrowid)

    def _run(self):
        return run_pending_operblock_offline_migration(self.central, root=str(self.root))

    def test_exports_closed_case_while_another_case_is_active_and_preserves_protocol(self):
        closed_id = self._case(protocol=7)
        self._case(status="active", protocol=7, title="Активная операция")
        # Same printed number was already archived by another computer.
        self.central._remcard_conn.execute(
            """INSERT INTO patients (full_name) VALUES ('Другой пациент')"""
        )
        patient_id = self.central._remcard_conn.execute("SELECT MAX(id) FROM patients").fetchone()[0]
        self.central._remcard_conn.execute(
            """INSERT INTO admissions (patient_id, bed_number, history_number, admission_datetime)
               VALUES (?, 2, 'H-2', '2026-09-28T08:00:00')""", (patient_id,)
        )
        admission_id = self.central._remcard_conn.execute("SELECT MAX(id) FROM admissions").fetchone()[0]
        self.central._remcard_conn.execute(
            """INSERT INTO operation_cases (patient_id, admission_id, table_code, status, started_at,
               ended_at, anesthesia_protocol_date, anesthesia_protocol_number)
               VALUES (?, ?, 'emergency', 'closed', '2026-09-28T06:00:00', '2026-09-28T07:00:00', '2026-09-28', 7)""",
            (patient_id, admission_id),
        )
        self.central._remcard_conn.commit()

        result = self._run()

        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.migrated_cases, 1)
        row = self.local.execute("SELECT migration_status FROM operation_cases WHERE id=?", (closed_id,)).fetchone()
        self.assertEqual(row[0], "verified")
        protocols = self.central._remcard_conn.execute(
            "SELECT anesthesia_protocol_number FROM operation_cases WHERE anesthesia_protocol_number=7"
        ).fetchall()
        self.assertEqual(len(protocols), 2)

    def test_retry_after_lost_local_ack_verifies_receipt_without_duplicate(self):
        case_id = self._case()
        first = self._run()
        self.assertTrue(first.ok, first.reason)
        self.local.execute(
            "UPDATE operation_cases SET migration_status='pending', migrated_at=NULL, migrated_remote_id=NULL WHERE id=?",
            (case_id,),
        )
        self.local.commit()

        retry = self._run()

        self.assertTrue(retry.ok, retry.reason)
        self.assertEqual(retry.migrated_cases, 1)
        self.assertEqual(self.central._remcard_conn.execute("SELECT COUNT(*) FROM operation_cases").fetchone()[0], 1)
        self.assertEqual(self.local.execute("SELECT migration_status FROM operation_cases WHERE id=?", (case_id,)).fetchone()[0], "verified")

    def test_central_archive_edit_is_never_overwritten_by_pending_local_copy(self):
        case_id = self._case(title="Исходная операция")
        self.assertTrue(self._run().ok)
        remote_id = self.central._remcard_conn.execute("SELECT id FROM operation_cases").fetchone()[0]
        self.central._remcard_conn.execute(
            "UPDATE operation_cases SET planned_operation_name='Правка архива', revision=1 WHERE id=?", (remote_id,)
        )
        self.central._remcard_conn.commit()
        self.local.execute(
            "UPDATE operation_cases SET migration_status='pending', migrated_at=NULL, migrated_remote_id=NULL WHERE id=?",
            (case_id,),
        )
        self.local.commit()

        result = self._run()

        self.assertFalse(result.ok)
        self.assertEqual(result.migrated_cases, 0)
        self.assertEqual(
            self.central._remcard_conn.execute("SELECT planned_operation_name FROM operation_cases WHERE id=?", (remote_id,)).fetchone()[0],
            "Правка архива",
        )
        self.assertEqual(self.local.execute("SELECT migration_status FROM operation_cases WHERE id=?", (case_id,)).fetchone()[0], "pending")

    def test_child_edit_after_central_commit_is_not_acknowledged(self):
        case_id = self._case()
        admission_id = self.local.execute(
            "SELECT admission_id FROM operation_cases WHERE id=?", (case_id,)
        ).fetchone()[0]
        original_write = self.central.run_write_operation

        def write_then_change_child(operation, source=""):
            value = original_write(operation, source)
            if source == "opblock_offline_migration_case":
                self.local.execute(
                    "INSERT INTO orders (admission_id, datetime, text) VALUES (?, ?, ?)",
                    (admission_id, "2026-09-28T09:01:00", "Запоздалая локальная правка"),
                )
                self.local.commit()
            return value

        self.central.run_write_operation = write_then_change_child
        result = self._run()

        self.assertFalse(result.ok)
        self.assertTrue("изменён во время" in result.reason or "locked" in result.reason)
        self.assertEqual(result.migrated_cases, 0)
        self.assertEqual(self.central._remcard_conn.execute("SELECT COUNT(*) FROM operation_cases").fetchone()[0], 1)
        self.assertNotEqual(
            self.local.execute("SELECT migration_status FROM operation_cases WHERE id=?", (case_id,)).fetchone()[0],
            "verified",
        )

    def test_closed_case_with_unreleased_table_assignment_is_not_exported(self):
        case_id = self._case()
        self.local.execute(
            """INSERT INTO operation_table_assignments
               (operation_case_id, table_code, assigned_at, status)
               VALUES (?, 'emergency', '2026-09-28T08:00:00', 'active')""",
            (case_id,),
        )
        self.local.commit()

        result = self._run()

        self.assertFalse(result.ok)
        self.assertIn("всё ещё занимает", result.reason)
        self.assertEqual(result.migrated_cases, 0)
        self.assertEqual(self.central._remcard_conn.execute("SELECT COUNT(*) FROM operation_cases").fetchone()[0], 0)

    def test_frozen_case_that_was_restored_active_is_never_exported(self):
        case_id = self._case()
        self.local.execute(
            "UPDATE operation_cases SET offline_case_uuid=?, status='active', ended_at=NULL WHERE id=?",
            ("restored-before-snapshot", case_id),
        )
        self.local.commit()
        frozen_case = dict(self.local.execute("SELECT * FROM operation_cases WHERE id=?", (case_id,)).fetchone())
        installation_origin_id = local_installation_origin_id(self.local)
        content_hash = payload_hash(build_case_payload(
            self.local, frozen_case, installation_origin_id=installation_origin_id,
        ))

        with self.assertRaisesRegex(RuntimeError, "больше не завершён"):
            self.central.run_write_operation(
                lambda cursor: _migrate_one_case(
                    cursor, self.local, local_case=frozen_case, session_id="active",
                    installation_origin_id=installation_origin_id,
                    expected_content_hash=content_hash,
                )
            )
        self.assertEqual(self.central._remcard_conn.execute("SELECT COUNT(*) FROM operation_cases").fetchone()[0], 0)

    def test_bound_local_store_rejects_a_different_central_archive(self):
        self._case()
        self.assertTrue(self._run().ok)
        other = _Central()
        try:
            result = run_pending_operblock_offline_migration(other, root=str(self.root))
        finally:
            other.close()
        self.assertEqual(result.reason, "no_pending_cases")
        # A new pending case makes the target mismatch visible before export.
        self._case(protocol=18)
        other = _Central()
        try:
            result = run_pending_operblock_offline_migration(other, root=str(self.root))
        finally:
            other.close()
        self.assertFalse(result.ok)
        self.assertIn("не совпадает", result.reason)
