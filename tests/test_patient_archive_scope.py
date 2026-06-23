from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.data.dao.patient_dao import PatientDAO  # noqa: E402


class _DbManager:
    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def close(self):
        self.conn.close()

    def fetch_all_remcard(self, query, params=()):
        return self.conn.execute(query, tuple(params or ())).fetchall()


def _create_archive_scope_db(db_path: Path, *, include_scope_columns: bool) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        scope_columns = ", unit_scope TEXT, admission_type TEXT" if include_scope_columns else ""
        conn.executescript(
            f"""
            CREATE TABLE patients (
                id INTEGER PRIMARY KEY,
                last_name TEXT,
                first_name TEXT,
                middle_name TEXT,
                full_name TEXT,
                birth_date TEXT
            );
            CREATE TABLE admissions (
                id INTEGER PRIMARY KEY,
                patient_id INTEGER,
                history_number TEXT,
                bed_number INTEGER,
                admission_datetime TEXT,
                transfer_datetime TEXT,
                death_datetime TEXT,
                diagnosis_text TEXT,
                patient_age INTEGER,
                patient_months INTEGER,
                patient_age_unit TEXT,
                patient_gender TEXT,
                diagnosis_code TEXT,
                operation_description TEXT,
                emergency_notice_number TEXT,
                emergency_notice_entered_at TEXT,
                intake_extra_json TEXT
                {scope_columns}
            );
            CREATE TABLE operation_cases (
                id INTEGER PRIMARY KEY,
                patient_id INTEGER,
                admission_id INTEGER,
                status TEXT,
                future_rao_admission_id INTEGER
            );
            """
        )
        conn.executemany(
            """
            INSERT INTO patients (id, last_name, first_name, middle_name, full_name, birth_date)
            VALUES (?, ?, ?, '', ?, '1980-01-01')
            """,
            [
                (1, "Рао", "Обычный", "Рао Обычный"),
                (2, "Опер", "Источник", "Опер Источник"),
                (3, "Рао", "Послеоперационный", "Рао Послеоперационный"),
            ],
        )
        base_columns = """
            id, patient_id, history_number, bed_number, admission_datetime,
            transfer_datetime, diagnosis_text, patient_age, patient_age_unit, intake_extra_json
        """
        if include_scope_columns:
            conn.executemany(
                f"""
                INSERT INTO admissions ({base_columns}, unit_scope, admission_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (1, 1, "RAO-1", 1, "2026-06-01 08:00:00", "2026-06-02 08:00:00", "РАО", 46, "л", None, None, None),
                    (2, 2, "OP-1", 0, "2026-06-03 08:00:00", "2026-06-03 10:00:00", "Оперблок", 46, "л", None, "operblock", "operblock"),
                    (
                        3,
                        3,
                        "RAO-POST",
                        10,
                        "2026-06-03 10:10:00",
                        None,
                        "После операции",
                        46,
                        "л",
                        '{"source": "operblock_rao_transfer", "source_admission_id": 2}',
                        "rao",
                        "rao_recovery",
                    ),
                ],
            )
        else:
            conn.executemany(
                f"""
                INSERT INTO admissions ({base_columns})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (1, 1, "RAO-1", 1, "2026-06-01 08:00:00", "2026-06-02 08:00:00", "РАО", 46, "л", None),
                    (2, 2, "OP-1", 0, "2026-06-03 08:00:00", "2026-06-03 10:00:00", "Оперблок", 46, "л", None),
                    (
                        3,
                        3,
                        "RAO-POST",
                        10,
                        "2026-06-03 10:10:00",
                        None,
                        "После операции",
                        46,
                        "л",
                        '{"source": "operblock_rao_transfer", "source_admission_id": 2}',
                    ),
                ],
            )
        conn.execute(
            """
            INSERT INTO operation_cases (id, patient_id, admission_id, status, future_rao_admission_id)
            VALUES (1, 2, 2, 'closed', 3)
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_rao_archive_hides_operblock_admission_but_keeps_recreated_rao_card(tmp_path):
    db_path = tmp_path / "current.db"
    _create_archive_scope_db(db_path, include_scope_columns=True)
    manager = _DbManager(str(db_path))
    try:
        patients = PatientDAO(manager).get_archived_patients()
    finally:
        manager.close()

    history_numbers = {patient.history_number for patient in patients}
    assert history_numbers == {"RAO-1", "RAO-POST"}
    assert "OP-1" not in history_numbers


def test_legacy_archive_hides_operation_case_source_without_unit_scope(tmp_path):
    db_path = tmp_path / "legacy.db"
    _create_archive_scope_db(db_path, include_scope_columns=False)

    dao = object.__new__(PatientDAO)
    rows = dao._fetch_archived_rows_from_db(str(db_path))

    history_numbers = {row.get("history_number") for row in rows}
    assert history_numbers == {"RAO-1", "RAO-POST"}
    assert "OP-1" not in history_numbers
