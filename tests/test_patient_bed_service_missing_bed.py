from __future__ import annotations

import sqlite3
import sys
import unittest
from datetime import date, datetime
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.services.patient_bed_management.service import PatientBedManagementService  # noqa: E402


class SQLiteWriteDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(
            """
            CREATE TABLE patients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                admission_uid TEXT,
                birth_date TEXT
            );
            CREATE TABLE admissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id INTEGER NOT NULL,
                bed_number INTEGER NOT NULL,
                history_number TEXT NOT NULL,
                admission_datetime TEXT NOT NULL,
                patient_age INTEGER,
                patient_months INTEGER,
                patient_age_unit TEXT,
                patient_gender TEXT,
                diagnosis_code TEXT,
                diagnosis_text TEXT,
                department_profile TEXT,
                source_department TEXT,
                recovery_bed_stay INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT,
                revision INTEGER DEFAULT 0,
                FOREIGN KEY (patient_id) REFERENCES patients(id)
            );
            CREATE TABLE beds (
                bed_number INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                current_admission_id INTEGER,
                revision INTEGER DEFAULT 0,
                FOREIGN KEY (current_admission_id) REFERENCES admissions(id)
            );
            """
        )

    def run_write_operation(self, operation, source: str = ""):
        del source
        cursor = self.conn.cursor()
        try:
            result = operation(cursor)
            self.conn.commit()
            return result
        except Exception:
            self.conn.rollback()
            raise


def admission_data(bed_number: int) -> dict:
    return {
        "bed_number": bed_number,
        "history_number": "ИБ-1",
        "admission_datetime": datetime(2026, 7, 13, 8, 30),
        "patient_age": 40,
        "patient_months": None,
        "patient_age_unit": "years",
        "patient_gender": "male",
        "diagnosis_code": "A00",
        "diagnosis_text": "Холера",
        "department_profile": "ОАР",
        "source_department": "Приёмное отделение",
    }


class PatientBedServiceMissingBedTest(unittest.TestCase):
    def setUp(self):
        self.db = SQLiteWriteDB()
        self.service = PatientBedManagementService(self.db)

    def tearDown(self):
        self.db.conn.close()

    def test_create_admission_initializes_missing_bed(self):
        admission_id = self.service.create_patient_and_admission(
            {"full_name": "Новый пациент", "birth_date": date(1986, 1, 1)},
            admission_data(2),
        )

        bed = self.db.conn.execute(
            "SELECT status, current_admission_id, revision FROM beds WHERE bed_number = 2"
        ).fetchone()

        self.assertIsNotNone(bed)
        self.assertEqual(bed["status"], "OCCUPIED")
        self.assertEqual(bed["current_admission_id"], admission_id)
        self.assertEqual(bed["revision"], 1)

    def test_move_admission_initializes_missing_target_bed(self):
        admission_id = self.service.create_patient_and_admission(
            {"full_name": "Перемещаемый пациент", "birth_date": date(1986, 1, 1)},
            admission_data(1),
        )

        moved = self.service.move_patient(
            1,
            2,
            expected_source_bed_revision=1,
            expected_target_bed_revision=0,
            expected_source_admission_revision=0,
        )

        source_bed = self.db.conn.execute(
            "SELECT status, current_admission_id FROM beds WHERE bed_number = 1"
        ).fetchone()
        target_bed = self.db.conn.execute(
            "SELECT status, current_admission_id FROM beds WHERE bed_number = 2"
        ).fetchone()
        admission = self.db.conn.execute(
            "SELECT bed_number FROM admissions WHERE id = ?",
            (admission_id,),
        ).fetchone()

        self.assertTrue(moved)
        self.assertEqual(dict(source_bed), {"status": "FREE", "current_admission_id": None})
        self.assertEqual(dict(target_bed), {"status": "OCCUPIED", "current_admission_id": admission_id})
        self.assertEqual(admission["bed_number"], 2)

    def test_create_admission_still_rejects_occupied_bed(self):
        patient_id = self.db.conn.execute(
            "INSERT INTO patients (full_name) VALUES ('Текущий пациент')"
        ).lastrowid
        current_admission_id = self.db.conn.execute(
            """
            INSERT INTO admissions (
                patient_id, bed_number, history_number, admission_datetime
            ) VALUES (?, 2, 'ИБ-0', '2026-07-13 08:00:00')
            """,
            (patient_id,),
        ).lastrowid
        self.db.conn.execute(
            """
            INSERT INTO beds (bed_number, status, current_admission_id, revision)
            VALUES (2, 'OCCUPIED', ?, 4)
            """,
            (current_admission_id,),
        )
        self.db.conn.commit()

        with self.assertRaisesRegex(RuntimeError, "Койка 2 уже занята"):
            self.service.create_patient_and_admission(
                {"full_name": "Другой пациент", "birth_date": date(1990, 1, 1)},
                admission_data(2),
            )

        counts = {
            table: self.db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("patients", "admissions", "beds")
        }
        self.assertEqual(counts, {"patients": 1, "admissions": 1, "beds": 1})


if __name__ == "__main__":
    unittest.main()
