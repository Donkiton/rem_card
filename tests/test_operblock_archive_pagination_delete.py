from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.services.operblock_service import OperBlockService  # noqa: E402


class _DbManager:
    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def close(self):
        self.conn.close()

    def fetch_all_remcard(self, query, params=()):
        return self.conn.execute(query, tuple(params or ())).fetchall()

    def run_write_operation(self, operation, source="test"):
        cursor = self.conn.cursor()
        try:
            result = operation(cursor)
            self.conn.commit()
            return result
        except Exception:
            self.conn.rollback()
            raise


def _create_operblock_archive_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE operating_tables (
                code TEXT PRIMARY KEY,
                display_name TEXT,
                sort_order INTEGER
            );
            INSERT INTO operating_tables (code, display_name, sort_order)
            VALUES ('emergency', 'Экстренная операционная', 1),
                   ('planned', 'Плановая операционная', 2);

            CREATE TABLE patients (
                id INTEGER PRIMARY KEY,
                full_name TEXT,
                birth_date TEXT
            );
            CREATE TABLE admissions (
                id INTEGER PRIMARY KEY,
                patient_id INTEGER,
                history_number TEXT,
                admission_datetime TEXT,
                patient_gender TEXT,
                patient_age INTEGER,
                patient_months INTEGER,
                patient_age_unit TEXT,
                diagnosis_code TEXT,
                diagnosis_text TEXT,
                unit_scope TEXT,
                is_active INTEGER DEFAULT 0,
                revision INTEGER DEFAULT 0,
                updated_at TEXT
            );
            CREATE TABLE operation_cases (
                id INTEGER PRIMARY KEY,
                patient_id INTEGER,
                admission_id INTEGER,
                table_code TEXT,
                status TEXT,
                started_at TEXT,
                ended_at TEXT,
                revision INTEGER DEFAULT 0,
                last_modified_by TEXT
            );
            CREATE TABLE operation_table_assignments (
                id INTEGER PRIMARY KEY,
                operation_case_id INTEGER,
                table_code TEXT,
                assigned_at TEXT,
                released_at TEXT,
                status TEXT,
                revision INTEGER DEFAULT 0,
                last_modified_by TEXT
            );
            CREATE TABLE patient_status_events (
                id INTEGER PRIMARY KEY,
                admission_id INTEGER,
                status TEXT,
                start_time TEXT,
                end_time TEXT,
                revision INTEGER DEFAULT 0,
                last_modified_by TEXT
            );
            CREATE TABLE beds (
                bed_number INTEGER PRIMARY KEY,
                status TEXT,
                current_admission_id INTEGER
            );
            """
        )
        for idx in range(1, 6):
            if idx == 4:
                status = "active"
            elif idx == 5:
                status = "transferred_to_rao"
            else:
                status = "closed"
            ended_at = None if status == "active" else f"2026-06-01 1{idx}:00:00"
            conn.execute(
                "INSERT INTO patients (id, full_name, birth_date) VALUES (?, ?, '1980-01-01')",
                (idx, f"Пациент {idx}"),
            )
            conn.execute(
                """
                INSERT INTO admissions (
                    id, patient_id, history_number, admission_datetime, patient_gender,
                    diagnosis_code, diagnosis_text, unit_scope, is_active
                ) VALUES (?, ?, ?, ?, 'Мужской', 'S82.0', ?, 'operblock', ?)
                """,
                (
                    idx,
                    idx,
                    f"OP-{idx}",
                    f"2026-06-01 0{idx}:00:00",
                    f"Диагноз {idx}",
                    1 if status == "active" else 0,
                ),
            )
            conn.execute(
                """
                INSERT INTO operation_cases (
                    id, patient_id, admission_id, table_code, status, started_at, ended_at
                ) VALUES (?, ?, ?, 'emergency', ?, ?, ?)
                """,
                (idx, idx, idx, status, f"2026-06-01 0{idx}:00:00", ended_at),
            )
        conn.execute(
            """
            INSERT INTO operation_table_assignments (
                id, operation_case_id, table_code, assigned_at, status
            ) VALUES (1, 4, 'emergency', '2026-06-01 04:00:00', 'active')
            """
        )
        conn.execute(
            """
            INSERT INTO patient_status_events (id, admission_id, status, start_time)
            VALUES (1, 4, 'OR', '2026-06-01 04:00:00')
            """
        )
        conn.execute(
            "INSERT INTO beds (bed_number, status, current_admission_id) VALUES (0, 'OCCUPIED', 4)"
        )
        conn.commit()
    finally:
        conn.close()


def test_operblock_archive_page_returns_only_requested_page(tmp_path):
    db_path = tmp_path / "operblock.db"
    _create_operblock_archive_db(db_path)
    db = _DbManager(str(db_path))
    try:
        page_1 = OperBlockService(db).list_archived_operation_cases_page(page=1, page_size=2)
        page_2 = OperBlockService(db).list_archived_operation_cases_page(page=2, page_size=2)
    finally:
        db.close()

    assert page_1["total_count"] == 5
    assert len(page_1["records"]) == 2
    assert len(page_2["records"]) == 2
    assert any(record["status"] == "transferred_to_rao" for record in page_1["records"] + page_2["records"])


def test_delete_archived_operation_case_force_deletes_active_case_and_releases_table(tmp_path):
    db_path = tmp_path / "operblock.db"
    _create_operblock_archive_db(db_path)
    db = _DbManager(str(db_path))
    try:
        result = OperBlockService(db).delete_archived_operation_case(4)
        case_count = db.conn.execute("SELECT COUNT(*) FROM operation_cases WHERE id = 4").fetchone()[0]
        admission_count = db.conn.execute("SELECT COUNT(*) FROM admissions WHERE id = 4").fetchone()[0]
        patient_count = db.conn.execute("SELECT COUNT(*) FROM patients WHERE id = 4").fetchone()[0]
        assignment_count = db.conn.execute(
            "SELECT COUNT(*) FROM operation_table_assignments WHERE operation_case_id = 4"
        ).fetchone()[0]
        bed_row = db.conn.execute("SELECT current_admission_id FROM beds WHERE bed_number = 0").fetchone()
    finally:
        db.close()

    assert result["operation_case_id"] == 4
    assert case_count == 0
    assert admission_count == 0
    assert patient_count == 0
    assert assignment_count == 0
    assert bed_row["current_admission_id"] is None
