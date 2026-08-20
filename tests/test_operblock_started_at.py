from __future__ import annotations

import json
import os
import sqlite3
import sys
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.services.operblock_service import (  # noqa: E402
    OperBlockService,
    OperBlockSourceMovementChangedError,
)
from rem_card.ui.operblock_view.operblock_main_widget import (  # noqa: E402
    OperBlockAdmissionTimeInput,
    OperBlockMainWidget,
    OperationStagesDialog,
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
        self.read_scope_sources: list[str] = []
        self.read_operation_sources: list[str] = []
        self.write_operation_sources: list[str] = []
        self._prepare_schema()

    def close(self):
        self.conn.close()

    @contextmanager
    def central_read_scope(self, source: str = "snapshot"):
        self.read_scope_sources.append(str(source))
        yield self

    def run_write_operation(self, operation, source="test"):
        self.write_operation_sources.append(str(source))
        cursor = self.conn.cursor()
        try:
            result = operation(cursor)
            self.conn.commit()
            return result
        except Exception:
            self.conn.rollback()
            raise

    def run_read_operation(self, operation, source="test"):
        self.read_operation_sources.append(str(source))
        cursor = self.conn.cursor()
        try:
            return operation(cursor)
        finally:
            cursor.close()

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
                drug_key TEXT,
                comment TEXT,
                status TEXT,
                is_committed INTEGER DEFAULT 1,
                created_at TEXT,
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
                last_modified_by TEXT,
                created_by_role TEXT,
                created_by_client_id TEXT
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

    def test_operation_case_form_uses_read_snapshot_without_write_transaction(self):
        started_at = datetime.now().replace(second=0, microsecond=0) - timedelta(hours=2)
        result = self.service.create_operation_case(self._base_payload(started_at))
        self.db.read_operation_sources.clear()
        self.db.write_operation_sources.clear()

        form_data = self.service.get_operation_case_form_data(result["operation_case_id"])

        self.assertEqual(form_data["operation_case_id"], result["operation_case_id"])
        self.assertEqual(
            self.db.read_operation_sources,
            ["operblock_get_operation_case_form_data"],
        )
        self.assertEqual(self.db.write_operation_sources, [])

    def test_start_anesthesia_context_reuses_one_read_scope(self):
        started_at = datetime.now().replace(second=0, microsecond=0) - timedelta(hours=2)
        result = self.service.create_operation_case(self._base_payload(started_at))
        self.db.read_scope_sources.clear()

        context = self.service.get_start_anesthesia_context(result["operation_case_id"])

        self.assertTrue(context["has_initial_vitals"])
        self.assertEqual(context["latest_vital_at"], started_at)
        self.assertEqual(
            self.db.read_scope_sources,
            ["operblock_start_anesthesia_context"],
        )

    def test_operation_report_context_uses_central_read_scope(self):
        started_at = datetime.now().replace(second=0, microsecond=0) - timedelta(hours=2)
        result = self.service.create_operation_case(self._base_payload(started_at))
        self.db.read_scope_sources.clear()

        context = self.service.build_operation_report_context(result["operation_case_id"])

        self.assertEqual(context["operation_case_id"], result["operation_case_id"])
        self.assertEqual(self.db.read_scope_sources, ["operblock_report_context"])

    def test_operation_report_read_retries_database_locked(self):
        calls = {"count": 0}

        def operation():
            calls["count"] += 1
            if calls["count"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        with patch("rem_card.services.operblock_service.time.sleep") as sleep_mock:
            result = self.service._run_report_read_operation("operblock_report_context", operation)

        self.assertEqual(result, "ok")
        self.assertEqual(calls["count"], 2)
        self.assertEqual(self.db.read_scope_sources[-2:], ["operblock_report_context", "operblock_report_context"])
        sleep_mock.assert_called_once()


class OperBlockStagesTest(unittest.TestCase):
    def setUp(self):
        self.db = _MemoryDb()
        self.service = OperBlockService(self.db)
        self.started_at = datetime.now().replace(second=0, microsecond=0) - timedelta(hours=2)
        self.case = self.service.create_operation_case(OperBlockStartedAtTest._base_payload(self.started_at))

    def tearDown(self):
        self.db.close()

    def test_custom_stage_requires_active_anesthesia_and_respects_its_start(self):
        stage_time = self.started_at + timedelta(minutes=20)

        with self.assertRaisesRegex(ValueError, "после начала"):
            self.service.add_operation_stage(
                self.case["operation_case_id"],
                "Установка катетера",
                event_time=stage_time,
            )

        anesthesia_start = self.started_at + timedelta(minutes=10)
        self.service.start_anesthesia(
            self.case["operation_case_id"],
            "Общая анестезия",
            event_time=anesthesia_start,
        )

        with self.assertRaisesRegex(ValueError, "раньше начала пособия"):
            self.service.add_operation_stage(
                self.case["operation_case_id"],
                "Слишком ранний этап",
                event_time=anesthesia_start - timedelta(minutes=1),
            )

    def test_custom_stage_can_be_added_and_edited_until_anesthesia_ends(self):
        anesthesia_start = self.started_at + timedelta(minutes=10)
        intermediate_time = self.started_at + timedelta(minutes=20)
        surgery_start = self.started_at + timedelta(minutes=25)
        case_id = self.case["operation_case_id"]
        self.service.start_anesthesia(case_id, "Общая анестезия", event_time=anesthesia_start)

        stage = self.service.add_operation_stage(
            case_id,
            "Укладка пациента",
            event_time=intermediate_time,
        )

        self.assertEqual(stage["payload"]["stage_kind"], "custom")
        self.assertEqual(stage["event_time"], intermediate_time.isoformat(timespec="seconds"))

        with self.assertRaisesRegex(ValueError, "раньше этапа «Укладка пациента»"):
            self.service.start_surgery(
                case_id,
                operation_name="Остеосинтез",
                event_time=intermediate_time - timedelta(minutes=1),
            )

        self.service.start_surgery(
            case_id,
            operation_name="Остеосинтез",
            event_time=surgery_start,
        )
        updated = self.service.update_operation_stage(
            stage["source_id"],
            "Обработка операционного поля",
            expected_revision=stage["revision"],
            event_time=intermediate_time + timedelta(minutes=1),
        )

        self.assertEqual(updated["display_label"], "Обработка операционного поля")
        self.assertEqual(updated["revision"], 2)

        rows = self.db.conn.execute(
            """
            SELECT event_time, payload_json
            FROM operblock_timeline_events
            WHERE operation_case_id = ?
              AND event_type = 'clinical_event'
              AND status = 'active'
            ORDER BY datetime(event_time) ASC, id ASC
            """,
            (case_id,),
        ).fetchall()
        stages = [
            (json.loads(row["payload_json"])["stage_kind"], row["event_time"])
            for row in rows
        ]
        self.assertEqual(
            stages,
            [
                ("anesthesia_start", anesthesia_start.isoformat(timespec="seconds")),
                ("custom", (intermediate_time + timedelta(minutes=1)).isoformat(timespec="seconds")),
                ("surgery_start", surgery_start.isoformat(timespec="seconds")),
            ],
        )

        surgery_end = surgery_start + timedelta(minutes=10)
        late_stage_time = surgery_end + timedelta(minutes=1)
        self.service.end_surgery(case_id, event_time=surgery_end)
        late_stage = self.service.add_operation_stage(
            case_id,
            "Контроль гемостаза после операции",
            event_time=late_stage_time,
        )
        updated_late_stage = self.service.update_operation_stage(
            late_stage["source_id"],
            "Финальный контроль гемостаза",
            expected_revision=late_stage["revision"],
            event_time=late_stage_time + timedelta(minutes=1),
        )

        self.assertEqual(updated_late_stage["display_label"], "Финальный контроль гемостаза")
        self.assertEqual(updated_late_stage["revision"], 2)

        anesthesia_end = late_stage_time + timedelta(minutes=5)
        self.service.end_anesthesia_with_transfer(
            case_id,
            "Хирургия",
            event_time=anesthesia_end,
        )
        with self.assertRaisesRegex(ValueError, "до завершения пособия"):
            self.service.add_operation_stage(
                case_id,
                "Этап после завершения пособия",
                event_time=anesthesia_end + timedelta(minutes=1),
            )
        with self.assertRaisesRegex(ValueError, "до завершения пособия"):
            self.service.update_operation_stage(
                late_stage["source_id"],
                "Изменение после завершения пособия",
                expected_revision=updated_late_stage["revision"],
                event_time=late_stage_time + timedelta(minutes=2),
            )

    def test_dialog_orders_automatic_and_custom_stages_by_time(self):
        anesthesia_start = self.started_at + timedelta(minutes=10)
        intermediate_time = self.started_at + timedelta(minutes=20)
        surgery_start = self.started_at + timedelta(minutes=25)

        rows = OperationStagesDialog._normalized_stage_rows(
            [
                {
                    "kind": "surgery_start",
                    "label": "Начало операции",
                    "event_id": 3,
                    "event_time": surgery_start.isoformat(timespec="seconds"),
                },
                {
                    "kind": "custom",
                    "label": "Обработка операционного поля",
                    "event_id": 2,
                    "event_time": intermediate_time.isoformat(timespec="seconds"),
                },
                {
                    "kind": "anesthesia_start",
                    "label": "Начало пособия",
                    "event_id": 1,
                    "event_time": anesthesia_start.isoformat(timespec="seconds"),
                },
            ]
        )

        self.assertEqual(
            [row["kind"] for row in rows],
            ["anesthesia_start", "custom", "surgery_start"],
        )
        self.assertTrue(rows[0]["readonly"])
        self.assertFalse(rows[1]["readonly"])
        self.assertTrue(rows[2]["readonly"])

    def test_ui_stage_window_is_open_until_anesthesia_ends(self):
        anesthesia_start = self.started_at + timedelta(minutes=10)
        surgery_start = self.started_at + timedelta(minutes=25)
        surgery_end = surgery_start + timedelta(minutes=10)
        widget = SimpleNamespace(
            _current_stage_state={
                "anesthesia_active": True,
                "surgery_active": False,
                "current_anesthesia_start": anesthesia_start.isoformat(timespec="seconds"),
                "last_surgery_end": None,
            }
        )

        self.assertTrue(OperBlockMainWidget._operation_stages_available(widget))

        widget._current_stage_state.update(
            {
                "surgery_active": True,
                "current_surgery_start": surgery_start.isoformat(timespec="seconds"),
            }
        )
        self.assertTrue(OperBlockMainWidget._operation_stages_available(widget))

        widget._current_stage_state.update(
            {
                "surgery_active": False,
                "last_surgery_end": surgery_end.isoformat(timespec="seconds"),
            }
        )
        self.assertTrue(OperBlockMainWidget._operation_stages_available(widget))

        widget._current_stage_state.update(
            {
                "anesthesia_active": False,
                "last_anesthesia_end": (surgery_end + timedelta(minutes=5)).isoformat(timespec="seconds"),
            }
        )
        self.assertFalse(OperBlockMainWidget._operation_stages_available(widget))


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

    def test_rao_time_bounds_clamp_keyboard_and_stepper_changes(self):
        minimum = datetime.now().replace(second=0, microsecond=0) - timedelta(hours=2)
        maximum = minimum + timedelta(hours=1)
        widget = OperBlockAdmissionTimeInput(minimum + timedelta(minutes=30))
        widget.set_bounds(minimum, maximum)

        widget._step_time(-60)
        self.assertEqual(widget.datetime_value(), minimum)

        widget._step_time(120)
        self.assertEqual(widget.datetime_value(), maximum)

    def test_source_movement_warning_retries_release_with_preservation(self):
        retries = []
        widget = SimpleNamespace(
            _write_pending=True,
            _enqueue_release_case=lambda operation_case_id, **kwargs: retries.append(
                (operation_case_id, kwargs)
            ),
        )
        error = OperBlockSourceMovementChangedError(
            "Движение пациента в исходной карте РАО уже изменено. "
            "Стол будет освобождён без изменения движения пациента в исходной карте."
        )

        with patch(
            "rem_card.ui.operblock_view.operblock_main_widget.CustomMessageBox.warning"
        ) as warning:
            OperBlockMainWidget._on_release_case_error(
                widget,
                17,
                23,
                False,
                error,
            )

        self.assertFalse(widget._write_pending)
        warning.assert_called_once()
        self.assertIn("Стол будет освобождён", warning.call_args.args[2])
        self.assertEqual(
            retries,
            [
                (
                    17,
                    {
                        "handoff_id": 23,
                        "preserve_source_movement": True,
                    },
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
