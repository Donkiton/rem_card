from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.services.operblock_service import OperBlockService  # noqa: E402
from rem_card.ui.operblock_view.operblock_main_widget import (  # noqa: E402
    OperBlockAdmissionTimeInput,
    _operblock_format_time_edit_text,
    _operblock_time_minutes_from_text,
)
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


class _MemoryDb:
    db_path = ""
    remcard_db_path = ""
    runtime_context = None

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self._prepare_schema()

    def close(self):
        self.conn.close()

    def run_write_operation(self, operation, source="test"):
        cursor = self.conn.cursor()
        try:
            result = operation(cursor)
            self.conn.commit()
            return result
        except Exception:
            self.conn.rollback()
            raise

    def fetch_one_remcard(self, query, params=()):
        return self.conn.execute(query, params).fetchone()

    def fetch_all_remcard(self, query, params=()):
        return self.conn.execute(query, params).fetchall()

    def _prepare_schema(self):
        self.conn.executescript(
            """
            CREATE TABLE operating_tables (
                code TEXT PRIMARY KEY,
                display_name TEXT,
                sort_order INTEGER,
                revision INTEGER DEFAULT 0,
                last_modified_by TEXT
            );
            INSERT INTO operating_tables (code, display_name, sort_order)
            VALUES ('emergency', 'Экстренная операционная', 1),
                   ('planned', 'Плановая операционная', 2);

            CREATE TABLE patients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT,
                admission_uid TEXT,
                birth_date TEXT,
                last_name TEXT,
                first_name TEXT,
                middle_name TEXT
            );

            CREATE TABLE admissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id INTEGER,
                bed_number INTEGER,
                history_number TEXT,
                admission_datetime TEXT,
                patient_age INTEGER,
                patient_months INTEGER,
                patient_age_unit TEXT,
                patient_gender TEXT,
                diagnosis_code TEXT,
                diagnosis_text TEXT,
                department_profile TEXT,
                source_department TEXT,
                created_at TEXT,
                updated_at TEXT,
                unit_scope TEXT,
                admission_type TEXT,
                is_active INTEGER DEFAULT 1,
                revision INTEGER DEFAULT 0
            );

            CREATE TABLE operation_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id INTEGER,
                admission_id INTEGER,
                table_code TEXT,
                status TEXT,
                created_at TEXT,
                started_at TEXT,
                ended_at TEXT,
                created_by_role TEXT,
                created_by_client_id TEXT,
                revision INTEGER DEFAULT 0,
                updated_at TEXT,
                last_modified_by TEXT,
                planned_operation_name TEXT,
                planned_anesthesia_assistance_type TEXT,
                planned_surgeons_json TEXT,
                planned_operating_nurse TEXT,
                planned_anesthesiologist TEXT,
                planned_anesthetist TEXT,
                height_cm INTEGER,
                weight_kg REAL,
                allergies TEXT,
                blood_group TEXT,
                blood_rh TEXT,
                preop_sys INTEGER,
                preop_dia INTEGER,
                preop_pulse INTEGER,
                preop_spo2 INTEGER,
                preop_save_initial_vitals INTEGER DEFAULT 1,
                anesthesia_protocol_number INTEGER,
                anesthesia_protocol_date TEXT,
                transfer_department TEXT,
                offline_case_uuid TEXT,
                offline_session_id TEXT,
                migration_status TEXT,
                original_local_id INTEGER
            );

            CREATE TABLE operation_table_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_case_id INTEGER,
                table_code TEXT,
                assigned_at TEXT,
                released_at TEXT,
                status TEXT,
                created_by_role TEXT,
                created_by_client_id TEXT,
                revision INTEGER DEFAULT 0,
                updated_at TEXT,
                last_modified_by TEXT
            );

            CREATE TABLE patient_status_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admission_id INTEGER,
                status TEXT,
                reason_type TEXT,
                reason_text TEXT,
                start_time TEXT,
                end_time TEXT,
                created_by TEXT,
                revision INTEGER DEFAULT 0,
                updated_at TEXT,
                last_modified_by TEXT
            );

            CREATE TABLE vitals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admission_id INTEGER,
                datetime TEXT,
                sys INTEGER,
                dia INTEGER,
                pulse INTEGER,
                temp REAL,
                spo2 INTEGER,
                rr INTEGER,
                cvp INTEGER,
                last_modified_by TEXT,
                updated_at TEXT,
                revision INTEGER DEFAULT 0
            );

            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admission_id INTEGER,
                datetime TEXT,
                text TEXT,
                comment TEXT,
                status TEXT,
                updated_at TEXT,
                last_modified_by TEXT,
                revision INTEGER DEFAULT 0
            );

            CREATE TABLE operblock_timeline_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_case_id INTEGER,
                admission_id INTEGER,
                table_code TEXT,
                event_type TEXT,
                event_time TEXT,
                end_time TEXT,
                drug_label TEXT,
                display_label TEXT,
                raw_text TEXT,
                dose_value TEXT,
                dose_unit TEXT,
                volume_ml TEXT,
                concentration_text TEXT,
                rate_value TEXT,
                rate_unit TEXT,
                route TEXT,
                status TEXT DEFAULT 'active',
                revision INTEGER DEFAULT 1,
                source_order_id INTEGER,
                parent_event_id INTEGER,
                payload_json TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_modified_by TEXT
            );
            """
        )
        self.conn.commit()


class OperBlockStartedAtTest(unittest.TestCase):
    def setUp(self):
        self.db = _MemoryDb()
        self.service = OperBlockService(self.db)

    def tearDown(self):
        self.db.close()

    @staticmethod
    def _base_payload(started_at: datetime) -> dict:
        return {
            "table_code": "emergency",
            "history_number": "12345",
            "full_name": "Иванов Иван Иванович",
            "gender": "Мужской",
            "birth_date": date(1980, 1, 1),
            "started_at": started_at,
            "diagnosis_code": "S82.0",
            "diagnosis_text": "Перелом надколенника",
            "operation_name": "Остеосинтез",
            "preop_sys": 120,
            "preop_dia": 80,
            "preop_pulse": 70,
            "preop_spo2": 98,
        }

    def test_create_uses_selected_started_at_for_case_and_initial_vitals(self):
        started_at = datetime.now().replace(second=0, microsecond=0) - timedelta(hours=2)

        result = self.service.create_operation_case(self._base_payload(started_at))

        case = self.db.fetch_one_remcard("SELECT started_at FROM operation_cases WHERE id = ?", (result["operation_case_id"],))
        admission = self.db.fetch_one_remcard("SELECT admission_datetime FROM admissions WHERE id = ?", (result["admission_id"],))
        vital = self.db.fetch_one_remcard("SELECT datetime FROM vitals WHERE admission_id = ?", (result["admission_id"],))
        self.assertEqual(case["started_at"], started_at.isoformat(timespec="seconds"))
        self.assertEqual(admission["admission_datetime"], started_at.isoformat(timespec="seconds"))
        self.assertEqual(vital["datetime"], started_at.isoformat(timespec="seconds"))

    def test_started_at_can_move_before_card_has_clinical_changes(self):
        started_at = datetime.now().replace(second=0, microsecond=0) - timedelta(hours=2)
        new_started_at = started_at - timedelta(minutes=35)
        result = self.service.create_operation_case(self._base_payload(started_at))
        payload = self._base_payload(new_started_at)

        self.service.update_operation_case_form_data(result["operation_case_id"], payload)

        case = self.db.fetch_one_remcard("SELECT started_at FROM operation_cases WHERE id = ?", (result["operation_case_id"],))
        assignment = self.db.fetch_one_remcard(
            "SELECT assigned_at FROM operation_table_assignments WHERE operation_case_id = ?",
            (result["operation_case_id"],),
        )
        status = self.db.fetch_one_remcard("SELECT start_time FROM patient_status_events WHERE admission_id = ?", (result["admission_id"],))
        vital = self.db.fetch_one_remcard("SELECT datetime, sys, dia, pulse, spo2 FROM vitals WHERE admission_id = ?", (result["admission_id"],))
        expected = new_started_at.isoformat(timespec="seconds")
        self.assertEqual(case["started_at"], expected)
        self.assertEqual(assignment["assigned_at"], expected)
        self.assertEqual(status["start_time"], expected)
        self.assertEqual(vital["datetime"], expected)
        self.assertEqual((vital["sys"], vital["dia"], vital["pulse"], vital["spo2"]), (120, 80, 70, 98))

    def test_started_at_is_locked_after_timeline_event(self):
        started_at = datetime.now().replace(second=0, microsecond=0) - timedelta(hours=2)
        result = self.service.create_operation_case(self._base_payload(started_at))
        self.db.conn.execute(
            """
            INSERT INTO operblock_timeline_events (
                operation_case_id, admission_id, table_code, event_type, event_time,
                display_label, status, payload_json
            ) VALUES (?, ?, 'emergency', 'clinical_event', ?, 'Начало пособия', 'active', '{"stage_kind":"anesthesia_start"}')
            """,
            (result["operation_case_id"], result["admission_id"], (started_at + timedelta(minutes=10)).isoformat(timespec="seconds")),
        )
        self.db.conn.commit()

        form_data = self.service.get_operation_case_form_data(result["operation_case_id"])
        self.assertFalse(form_data["can_edit_started_at"])
        with self.assertRaisesRegex(ValueError, "Время поступления в оперблок можно изменить"):
            payload = self._base_payload(started_at - timedelta(minutes=20))
            self.service.update_operation_case_form_data(result["operation_case_id"], payload)


class OperBlockTimeParserTest(unittest.TestCase):
    def test_time_parser_accepts_short_and_full_24h_input(self):
        self.assertEqual(_operblock_format_time_edit_text("640"), "06:40")
        self.assertEqual(_operblock_time_minutes_from_text("06:40"), 6 * 60 + 40)
        self.assertEqual(_operblock_format_time_edit_text("1540"), "15:40")
        self.assertEqual(_operblock_time_minutes_from_text("15:40"), 15 * 60 + 40)
        self.assertEqual(_operblock_time_minutes_from_text("9:5"), 9 * 60 + 5)
        self.assertEqual(_operblock_time_minutes_from_text("06:"), 6 * 60)


class OperBlockAdmissionTimeInputWidgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_keyboard_input_inserts_colon_while_typing(self):
        widget = OperBlockAdmissionTimeInput(datetime.now().replace(second=0, microsecond=0) - timedelta(hours=1))
        widget.resize(640, 54)
        widget.show()
        self._app.processEvents()
        self.assertGreater(widget.note_label.geometry().left(), widget.time_frame.geometry().right())
        widget.time_input.setFocus()

        widget.time_input.clear()
        QTest.keyClicks(widget.time_input, "640")
        self.assertEqual(widget.time_input.text(), "06:40")

        widget.time_input.clear()
        QTest.keyClicks(widget.time_input, "1540")
        self.assertEqual(widget.time_input.text(), "15:40")


if __name__ == "__main__":
    unittest.main()
