from __future__ import annotations

import json
import os
import sqlite3
import sys
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from rem_card.data.dao.patient_status_dao import PatientStatusDAO  # noqa: E402
from rem_card.data.dto.remcard_dto import PatientStatus  # noqa: E402
from rem_card.services.patient_status_service import PatientStatusService  # noqa: E402
from rem_card.ui.rem_card_sectors.outcome_dialogs import (  # noqa: E402
    DEATH_OUTCOME_RECOVERY,
    DeathOutcomeDialog,
)
from rem_card.ui.rem_card_sectors.s_print.death_outcome import build_death_outcome_struct  # noqa: E402
from rem_card.ui.rem_card_sectors.s_print.movement import build_full_movement_struct  # noqa: E402


class _MemoryDb:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE patients (
                id INTEGER PRIMARY KEY,
                birth_date TEXT,
                last_name TEXT,
                first_name TEXT,
                middle_name TEXT,
                full_name TEXT
            );
            CREATE TABLE admissions (
                id INTEGER PRIMARY KEY,
                patient_id INTEGER,
                admission_datetime TEXT,
                department_profile TEXT,
                source_department TEXT,
                history_number TEXT,
                patient_age INTEGER,
                patient_months INTEGER,
                patient_age_unit TEXT,
                patient_gender TEXT,
                transfer_datetime TEXT,
                transfer_department TEXT,
                transfer_lpu TEXT,
                transfer_lpu_other TEXT,
                death_datetime TEXT,
                clinical_death_datetime TEXT,
                cardiac_arrest_cause TEXT,
                cardiac_arrest_measures_json TEXT,
                outcome TEXT,
                updated_at TEXT,
                revision INTEGER DEFAULT 0
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
                created_at TEXT,
                updated_at TEXT,
                last_modified_by TEXT,
                revision INTEGER DEFAULT 0
            );
            CREATE TABLE procedures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admission_id INTEGER,
                procedure_type TEXT,
                status TEXT,
                is_deleted INTEGER DEFAULT 0,
                started_at TEXT,
                finished_at TEXT,
                duration_minutes INTEGER,
                updated_by TEXT,
                updated_at TEXT,
                revision INTEGER DEFAULT 0
            );
            CREATE TABLE procedure_cvc (
                procedure_id INTEGER PRIMARY KEY,
                catheter_status TEXT,
                removed_or_replaced TEXT,
                removed_at TEXT,
                revision INTEGER DEFAULT 0
            );
            """
        )
        self.conn.execute(
            """
            INSERT INTO patients (id, last_name, first_name, middle_name, full_name, birth_date)
            VALUES (1, 'Иванов', 'Иван', 'Иванович', 'Иванов Иван Иванович', '1980-01-01')
            """
        )
        self.conn.execute(
            """
            INSERT INTO admissions (
                id, patient_id, admission_datetime, history_number, patient_age,
                patient_age_unit, patient_gender, outcome, revision
            )
            VALUES (1, 1, '2025-01-01T08:00:00', '42', 45, 'л', 'мужской', NULL, 0)
            """
        )
        self.conn.execute(
            """
            INSERT INTO patient_status_events (
                admission_id, status, start_time, end_time, created_by, created_at, updated_at, revision
            )
            VALUES (1, 'ACTIVE', '2025-01-01T08:00:00', NULL, 'SYSTEM', '2025-01-01T08:00:00', '2025-01-01T08:00:00', 0)
            """
        )
        self.conn.commit()

    def close(self):
        self.conn.close()

    @contextmanager
    def remcard_transaction(self, source="test"):
        cursor = self.conn.cursor()
        try:
            yield cursor
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def fetch_one_remcard(self, query, params=()):
        return self.conn.execute(query, params).fetchone()

    def fetch_all_remcard(self, query, params=()):
        return self.conn.execute(query, params).fetchall()


class _FakeRemcardService:
    def __init__(self, status_service):
        self.status_service = status_service


class CprRecoveryOutcomeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.db = _MemoryDb()
        self.dao = PatientStatusDAO(self.db)
        self.service = PatientStatusService(self.dao)

    def tearDown(self):
        self.db.close()

    def _payload(self):
        return {
            "outcome_type": DEATH_OUTCOME_RECOVERY,
            "clinical_death_datetime": "2025-01-01T09:45:00",
            "recovery_datetime": "2025-01-01T09:50:00",
            "cardiac_arrest_cause": "Асистолия",
            "measures": [{"name": "СЛР", "value": "Компрессии грудной клетки."}],
            "comment": "Спустя 6 минут проведения СЛР, произошло восстановление спонтанной сердечной деятельности.",
            "doctor": "Дежурный врач",
        }

    def _payload_at(self, clinical_dt, recovery_dt, *, cause="Асистолия", doctor="Дежурный врач"):
        minutes = int((recovery_dt - clinical_dt).total_seconds() // 60)
        return {
            "outcome_type": DEATH_OUTCOME_RECOVERY,
            "clinical_death_datetime": clinical_dt.isoformat(),
            "recovery_datetime": recovery_dt.isoformat(),
            "cardiac_arrest_cause": cause,
            "measures": [{"name": "СЛР", "value": "Компрессии грудной клетки."}],
            "comment": f"Спустя {minutes} минут проведения СЛР, произошло восстановление спонтанной сердечной деятельности.",
            "doctor": doctor,
        }

    def _death_payload(self, clinical_dt, biological_dt):
        return {
            "outcome_type": "biological_death",
            "clinical_death_datetime": clinical_dt.isoformat(),
            "biological_death_datetime": biological_dt.isoformat(),
            "cardiac_arrest_cause": "Фибрилляция желудочков",
            "measures": [{"name": "СЛР", "value": "Компрессии, ИВЛ, дефибрилляция."}],
            "comment": "Несмотря на проводимую терапию, восстановить сердечную деятельность не удалось.",
            "doctor": "Дежурный врач",
            "death_protocol": {
                "doctor": "Дежурный врач",
                "signature_doctor": "Дежурный врач",
                "biological_death_date": biological_dt.strftime("%d.%m.%Y"),
                "biological_death_time": biological_dt.strftime("%H:%M"),
            },
        }

    def _record_cpr(self, clinical_dt, recovery_dt):
        payload = self._payload_at(clinical_dt, recovery_dt)
        return self.service.record_cpr_recovery(
            1,
            clinical_dt,
            recovery_dt,
            reason_text=payload["comment"],
            user_id="USER",
            admission_details={
                "cardiac_arrest_cause": payload["cardiac_arrest_cause"],
                "cardiac_arrest_measures_json": json.dumps(payload, ensure_ascii=False),
            },
        )

    def _record_death(self, clinical_dt, biological_dt):
        payload = self._death_payload(clinical_dt, biological_dt)
        return self.service.change_status_with_outcome_details(
            1,
            PatientStatus.DEAD,
            biological_dt,
            reason_type="outcome",
            reason_text=f"Биологическая смерть: {biological_dt.strftime('%d.%m.%Y %H:%M')}",
            user_id="USER",
            admission_details={
                "clinical_death_datetime": clinical_dt,
                "cardiac_arrest_cause": payload["cardiac_arrest_cause"],
                "cardiac_arrest_measures_json": json.dumps(payload, ensure_ascii=False),
            },
        )

    def test_record_cpr_recovery_does_not_finish_admission_and_is_not_printed_as_movement(self):
        ok = self.service.record_cpr_recovery(
            1,
            datetime(2025, 1, 1, 9, 45),
            datetime(2025, 1, 1, 9, 50),
            reason_text=self._payload()["comment"],
            user_id="USER",
            admission_details={
                "clinical_death_datetime": datetime(2025, 1, 1, 9, 45),
                "cardiac_arrest_cause": "Асистолия",
                "cardiac_arrest_measures_json": json.dumps(self._payload(), ensure_ascii=False),
            },
            expected_active_event_id=1,
            expected_active_revision=0,
            expected_admission_revision=0,
        )

        self.assertTrue(ok)
        current = self.service.get_current_status(1)
        self.assertEqual(current.status, PatientStatus.ACTIVE)

        admission = self.db.fetch_one_remcard("SELECT outcome, death_datetime, clinical_death_datetime FROM admissions WHERE id = 1")
        self.assertIsNone(admission["outcome"])
        self.assertIsNone(admission["death_datetime"])
        self.assertEqual(admission["clinical_death_datetime"], "2025-01-01T09:45:00")

        events = self.service.get_events(1)
        self.assertEqual([event.status for event in events], [PatientStatus.ACTIVE, PatientStatus.CPR])
        self.assertEqual(build_full_movement_struct(events)[0]["status"], "В отделении")
        self.assertEqual(len(build_full_movement_struct(events)), 1)

    def test_recovery_print_struct_uses_recovery_fields_and_no_death_protocol(self):
        self.service.record_cpr_recovery(
            1,
            datetime(2025, 1, 1, 9, 45),
            datetime(2025, 1, 1, 9, 50),
            reason_text=self._payload()["comment"],
            user_id="USER",
            admission_details={
                "cardiac_arrest_cause": "Асистолия",
                "cardiac_arrest_measures_json": json.dumps(self._payload(), ensure_ascii=False),
            },
        )

        struct = build_death_outcome_struct(
            _FakeRemcardService(self.service),
            1,
            datetime(2025, 1, 1, 8, 0),
            datetime(2025, 1, 2, 8, 0),
        )

        self.assertEqual(struct["outcome_kind"], "recovery")
        self.assertIn("ВОССТАНОВЛЕНИЕ СПОНТАННОГО КРОВООБРАЩЕНИЯ", struct["title"])
        self.assertEqual(struct["clinical_time"], "01.01.2025 09:45")
        self.assertEqual(struct["recovery_time"], "01.01.2025 09:50")
        self.assertEqual(struct["cpr_duration"], "5 мин")
        self.assertEqual(struct["protocol"], {})
        self.assertEqual(len(struct["items"]), 1)

    def test_multiple_cpr_recoveries_are_printed_as_separate_items(self):
        self.assertTrue(self._record_cpr(datetime(2025, 1, 1, 9, 45), datetime(2025, 1, 1, 9, 50)))
        self.assertTrue(self._record_cpr(datetime(2025, 1, 1, 10, 5), datetime(2025, 1, 1, 10, 9)))

        struct = build_death_outcome_struct(
            _FakeRemcardService(self.service),
            1,
            datetime(2025, 1, 1, 8, 0),
            datetime(2025, 1, 2, 8, 0),
        )

        self.assertEqual([item["outcome_kind"] for item in struct["items"]], ["recovery", "recovery"])
        self.assertEqual([item["clinical_time"] for item in struct["items"]], ["01.01.2025 09:45", "01.01.2025 10:05"])
        self.assertEqual([item["recovery_time"] for item in struct["items"]], ["01.01.2025 09:50", "01.01.2025 10:09"])

    def test_cpr_recovery_then_death_prints_cpr_and_final_outcome(self):
        self.assertTrue(self._record_cpr(datetime(2025, 1, 1, 9, 45), datetime(2025, 1, 1, 9, 50)))
        self.assertTrue(self._record_death(datetime(2025, 1, 1, 10, 15), datetime(2025, 1, 1, 10, 45)))

        struct = build_death_outcome_struct(
            _FakeRemcardService(self.service),
            1,
            datetime(2025, 1, 1, 8, 0),
            datetime(2025, 1, 2, 8, 0),
        )

        self.assertEqual([item["outcome_kind"] for item in struct["items"]], ["recovery", "death"])
        self.assertEqual(struct["items"][0]["clinical_time"], "01.01.2025 09:45")
        self.assertEqual(struct["items"][1]["biological_time"], "01.01.2025 10:45")
        self.assertIn("doctor", struct["items"][1]["protocol"])

    def test_overlapping_cpr_recovery_is_rejected(self):
        self.assertTrue(self._record_cpr(datetime(2025, 1, 1, 9, 45), datetime(2025, 1, 1, 9, 50)))

        self.assertFalse(self._record_cpr(datetime(2025, 1, 1, 9, 49), datetime(2025, 1, 1, 9, 55)))

        events = [event for event in self.service.get_events(1) if event.status == PatientStatus.CPR]
        self.assertEqual(len(events), 1)

    def test_death_inside_cpr_interval_is_rejected(self):
        self.assertTrue(self._record_cpr(datetime(2025, 1, 1, 9, 45), datetime(2025, 1, 1, 9, 50)))

        self.assertFalse(self._record_death(datetime(2025, 1, 1, 9, 48), datetime(2025, 1, 1, 10, 20)))

        events = self.service.get_events(1)
        self.assertNotIn(PatientStatus.DEAD, [event.status for event in events])

    def test_editing_cpr_to_overlap_another_cpr_is_rejected(self):
        self.assertTrue(self._record_cpr(datetime(2025, 1, 1, 9, 45), datetime(2025, 1, 1, 9, 50)))
        self.assertTrue(self._record_cpr(datetime(2025, 1, 1, 10, 0), datetime(2025, 1, 1, 10, 5)))
        second = [event for event in self.service.get_events(1) if event.status == PatientStatus.CPR][1]

        self.assertFalse(
            self.service.update_event_bounds(
                second.id,
                datetime(2025, 1, 1, 9, 49),
                datetime(2025, 1, 1, 10, 4),
                "Пересечение",
                expected_revision=second.revision,
            )
        )

        unchanged = self.dao.get_event_by_id(second.id)
        self.assertEqual(unchanged.start_time, datetime(2025, 1, 1, 10, 0))
        self.assertEqual(unchanged.end_time, datetime(2025, 1, 1, 10, 5))

    def test_editing_death_to_overlap_cpr_is_rejected(self):
        self.assertTrue(self._record_cpr(datetime(2025, 1, 1, 9, 45), datetime(2025, 1, 1, 9, 50)))
        self.assertTrue(self._record_death(datetime(2025, 1, 1, 10, 15), datetime(2025, 1, 1, 10, 45)))
        death_event = next(event for event in self.service.get_events(1) if event.status == PatientStatus.DEAD)

        self.assertFalse(
            self.service.update_event_bounds(
                death_event.id,
                datetime(2025, 1, 1, 9, 48),
                None,
                death_event.reason_text,
                expected_revision=death_event.revision,
            )
        )

        unchanged = self.dao.get_event_by_id(death_event.id)
        self.assertEqual(unchanged.start_time, datetime(2025, 1, 1, 10, 45))

    def test_cpr_event_does_not_replace_location_status_for_status_checks(self):
        self.db.conn.execute("UPDATE patient_status_events SET status = 'OUT' WHERE id = 1")
        self.db.conn.commit()

        self.service.record_cpr_recovery(
            1,
            datetime(2025, 1, 1, 9, 45),
            datetime(2025, 1, 1, 9, 50),
            reason_text=self._payload()["comment"],
            user_id="USER",
            admission_details={
                "cardiac_arrest_cause": "Асистолия",
                "cardiac_arrest_measures_json": json.dumps(self._payload(), ensure_ascii=False),
            },
        )

        default_event = self.service.get_event_at(1, datetime(2025, 1, 1, 9, 46))
        self.assertEqual(default_event.status, PatientStatus.OUT)

        cpr_event = self.service.get_event_at(1, datetime(2025, 1, 1, 9, 46), include_cpr=True)
        self.assertEqual(cpr_event.status, PatientStatus.CPR)

    def test_update_and_rollback_cpr_recovery_are_independent_from_active_status(self):
        self.service.record_cpr_recovery(
            1,
            datetime(2025, 1, 1, 9, 45),
            datetime(2025, 1, 1, 9, 50),
            reason_text=self._payload()["comment"],
            user_id="USER",
            admission_details={
                "cardiac_arrest_cause": "Асистолия",
                "cardiac_arrest_measures_json": json.dumps(self._payload(), ensure_ascii=False),
            },
        )
        cpr_event = next(event for event in self.service.get_events(1) if event.status == PatientStatus.CPR)

        self.assertTrue(
            self.service.update_event_bounds(
                cpr_event.id,
                datetime(2025, 1, 1, 9, 44),
                datetime(2025, 1, 1, 9, 52),
                "Комментарий изменен",
                expected_revision=cpr_event.revision,
            )
        )
        admission = self.db.fetch_one_remcard("SELECT clinical_death_datetime, cardiac_arrest_measures_json FROM admissions WHERE id = 1")
        payload = json.loads(admission["cardiac_arrest_measures_json"])
        self.assertEqual(admission["clinical_death_datetime"], "2025-01-01T09:44:00")
        self.assertEqual(payload["recovery_datetime"], "2025-01-01T09:52:00")
        self.assertEqual(payload["comment"], "Комментарий изменен")

        self.assertTrue(self.service.rollback_last_status(1))
        self.assertEqual(self.service.get_current_status(1).status, PatientStatus.ACTIVE)
        self.assertEqual([event.status for event in self.service.get_events(1)], [PatientStatus.ACTIVE])
        admission = self.db.fetch_one_remcard("SELECT clinical_death_datetime, cardiac_arrest_measures_json FROM admissions WHERE id = 1")
        self.assertIsNone(admission["clinical_death_datetime"])
        self.assertIsNone(admission["cardiac_arrest_measures_json"])

    def test_dialog_recovery_mode_hides_protocol_and_builds_recovery_payload(self):
        dialog = DeathOutcomeDialog({}, datetime(2025, 1, 1, 12, 0))
        dialog.clinical_time_picker.set_time("09:45")
        dialog.biological_time_picker.set_time("09:50")
        dialog.outcome_combo.setCurrentIndex(dialog.outcome_combo.findData(DEATH_OUTCOME_RECOVERY))

        self.assertTrue(dialog.protocol_frame.isHidden())
        self.assertIn("Восстановление", dialog.biological_title_label.text())
        self.assertIn("Спустя 6 минут", dialog.comment_edit.text())

        dialog._on_accept()

        self.assertEqual(dialog.result_data["record_kind"], DEATH_OUTCOME_RECOVERY)
        self.assertEqual(dialog.result_data["clinical_time"], datetime(2025, 1, 1, 9, 45))
        self.assertEqual(dialog.result_data["recovery_time"], datetime(2025, 1, 1, 9, 50))
        payload = json.loads(dialog.result_data["admission_details"]["cardiac_arrest_measures_json"])
        self.assertEqual(payload["outcome_type"], DEATH_OUTCOME_RECOVERY)
        self.assertNotIn("death_protocol", payload)


if __name__ == "__main__":
    unittest.main()
