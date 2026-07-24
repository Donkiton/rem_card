from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys

import pytest

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.app.operblock_schema import _apply_operblock_schema, is_operblock_schema_ready
from rem_card.app.unified_db_schema import ensure_unified_schema
from rem_card.data.dao.patient_dao import PatientDAO
from rem_card.data.dao.patient_status_dao import PatientStatusDAO
from rem_card.data.dto.remcard_dto import PatientStatus
from rem_card.services.concurrency import DataConflictError
from rem_card.services.analytics.recovery_summary import fetch_recovery_bed_admission_rows
from rem_card.services.operblock_handoff_service import OperBlockHandoffService
from rem_card.services.operblock_service import OperBlockService
from rem_card.services.patient_bed_management.service import PatientBedManagementService


class _MemoryDb:
    db_path = ""
    remcard_db_path = ""
    runtime_context = None

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_unified_schema(self.conn)
        _apply_operblock_schema(self.conn.cursor())
        self.conn.commit()
        assert is_operblock_schema_ready(self.conn)

    def close(self):
        self.conn.close()

    def fetch_one_remcard(self, query, params=()):
        return self.conn.execute(query, params).fetchone()

    def fetch_all_remcard(self, query, params=()):
        return self.conn.execute(query, params).fetchall()

    def run_write_operation(self, operation, source="test"):
        cursor = self.conn.cursor()
        try:
            result = operation(cursor)
            self.conn.commit()
            return result
        except Exception:
            self.conn.rollback()
            raise

    def run_read_operation(self, operation, source="test"):
        return operation(self.conn.cursor())

    @contextmanager
    def remcard_transaction(self):
        cursor = self.conn.cursor()
        try:
            yield cursor
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise


@pytest.fixture
def db():
    value = _MemoryDb()
    try:
        yield value
    finally:
        value.close()


def _create_rao_patient(
    db: _MemoryDb,
    *,
    bed_number: int,
    history_number: str,
    full_name: str = "Иванов Иван Иванович",
    status: PatientStatus = PatientStatus.ACTIVE,
    recovery: bool = False,
    intake_extra_json: str | None = None,
) -> int:
    now = datetime.now().replace(second=0, microsecond=0).isoformat(timespec="seconds")
    cursor = db.conn.cursor()
    cursor.execute(
        """
        INSERT INTO patients (full_name, admission_uid, birth_date)
        VALUES (?, ?, '1980-01-02')
        """,
        (full_name, f"patient-{history_number}-{bed_number}"),
    )
    patient_id = int(cursor.lastrowid)
    cursor.execute(
        """
        INSERT INTO admissions (
            patient_id, bed_number, history_number, admission_datetime,
            patient_gender, diagnosis_code, diagnosis_text, department_profile,
            source_department, recovery_bed_stay, intake_extra_json, is_active
        ) VALUES (?, ?, ?, ?, 'Мужской', 'K35.8', 'Острый аппендицит',
                  'Хирургическое отделение', 'РАО', ?, ?, 1)
        """,
        (
            patient_id,
            bed_number,
            history_number,
            now,
            1 if recovery else 0,
            intake_extra_json,
        ),
    )
    admission_id = int(cursor.lastrowid)
    cursor.execute(
        """
        INSERT INTO beds (bed_number, status, current_admission_id, revision)
        VALUES (?, 'OCCUPIED', ?, 0)
        ON CONFLICT(bed_number) DO UPDATE SET
            status = 'OCCUPIED',
            current_admission_id = excluded.current_admission_id
        """,
        (bed_number, admission_id),
    )
    cursor.execute(
        """
        INSERT INTO patient_status_events (
            admission_id, status, start_time, created_by, created_at, updated_at
        ) VALUES (?, ?, ?, 'test', ?, ?)
        """,
        (admission_id, status.value, now, now, now),
    )
    db.conn.commit()
    return admission_id


def _dispatch_and_accept(
    db: _MemoryDb,
    *,
    admission_id: int,
    table_code: str,
) -> tuple[dict, dict]:
    handoff = OperBlockHandoffService(db).dispatch_from_rao(admission_id)
    service = OperBlockService(db)
    payload = service.get_rao_handoff_form_data(int(handoff["id"]), table_code)
    case = service.create_operation_case(payload)
    return handoff, case


def test_linked_return_uses_source_admission_after_bed_move_and_has_no_duplicate_status(db):
    admission_id = _create_rao_patient(db, bed_number=1, history_number="100/1")
    now = datetime.now().replace(second=0, microsecond=0).isoformat(timespec="seconds")
    db.conn.execute(
        """
        INSERT INTO vitals (admission_id, datetime, sys, dia, pulse, spo2)
        VALUES (?, ?, 121, 78, 73, 98)
        """,
        (admission_id, now),
    )
    db.conn.commit()

    handoff, case = _dispatch_and_accept(db, admission_id=admission_id, table_code="emergency")
    old_or = db.fetch_one_remcard(
        "SELECT id FROM patient_status_events WHERE admission_id = ? AND end_time IS NULL",
        (admission_id,),
    )
    assert int(handoff["bed_number_at_dispatch"]) == 1
    assert OperBlockHandoffService(db).list_waiting() == []

    db.conn.execute(
        "INSERT INTO beds (bed_number, status, current_admission_id, revision) VALUES (2, 'FREE', NULL, 0)"
    )
    db.conn.execute(
        "UPDATE beds SET status = 'FREE', current_admission_id = NULL WHERE bed_number = 1"
    )
    db.conn.execute(
        "UPDATE beds SET status = 'OCCUPIED', current_admission_id = ? WHERE bed_number = 2",
        (admission_id,),
    )
    db.conn.execute("UPDATE admissions SET bed_number = 2 WHERE id = ?", (admission_id,))
    db.conn.execute(
        "UPDATE operation_cases SET transfer_department = 'РАО' WHERE id = ?",
        (case["operation_case_id"],),
    )
    db.conn.commit()

    OperBlockService(db).release_operation_table(case["operation_case_id"])
    events = db.fetch_all_remcard(
        """
        SELECT id, status, start_time, end_time
        FROM patient_status_events
        WHERE admission_id = ?
        ORDER BY id
        """,
        (admission_id,),
    )
    assert [row["status"] for row in events] == ["ACTIVE", "OR", "ACTIVE"]
    assert events[-2]["end_time"] == events[-1]["start_time"]
    assert db.fetch_one_remcard(
        "SELECT current_admission_id FROM beds WHERE bed_number = 2"
    )["current_admission_id"] == admission_id
    assert db.fetch_one_remcard(
        "SELECT status FROM operblock_handoffs WHERE id = ?",
        (handoff["id"],),
    )["status"] == "returned_to_rao"

    dao = PatientStatusDAO(db)
    with pytest.raises(DataConflictError):
        dao.change_status(
            admission_id,
            PatientStatus.ACTIVE,
            expected_active_event_id=int(old_or["id"]),
        )
    active = dao.get_active_event(admission_id)
    assert active is not None
    assert dao.change_status(
        admission_id,
        PatientStatus.ACTIVE,
        expected_active_event_id=active.id,
        expected_active_revision=active.revision,
    )
    assert db.fetch_one_remcard(
        """
        SELECT COUNT(*) AS count
        FROM patient_status_events
        WHERE admission_id = ? AND status = 'ACTIVE'
        """,
        (admission_id,),
    )["count"] == 2


def test_linked_transfer_to_other_department_does_not_change_source_movement(db):
    admission_id = _create_rao_patient(db, bed_number=3, history_number="100/2")
    handoff, case = _dispatch_and_accept(db, admission_id=admission_id, table_code="planned")
    db.conn.execute(
        "UPDATE operation_cases SET transfer_department = 'Хирургическое отделение' WHERE id = ?",
        (case["operation_case_id"],),
    )
    db.conn.commit()

    OperBlockService(db).release_operation_table(case["operation_case_id"])
    active = db.fetch_one_remcard(
        """
        SELECT status FROM patient_status_events
        WHERE admission_id = ? AND end_time IS NULL
        """,
        (admission_id,),
    )
    assert active["status"] == "OR"
    assert db.fetch_one_remcard(
        "SELECT status FROM operblock_handoffs WHERE id = ?",
        (handoff["id"],),
    )["status"] == "completed_non_rao"


def test_manual_operation_case_can_be_late_bound_on_release(db):
    admission_id = _create_rao_patient(db, bed_number=5, history_number="300/1")
    handoff = OperBlockHandoffService(db).dispatch_from_rao(admission_id)
    service = OperBlockService(db)
    case = service.create_operation_case(
        {
            "table_code": "emergency",
            "history_number": "300/1",
            "full_name": "Иванов Иван Иванович",
            "gender": "Мужской",
            "birth_date": "1980-01-02",
            "diagnosis_code": "K35.8",
            "diagnosis_text": "Острый аппендицит",
            "department_profile": "Хирургическое отделение",
        }
    )
    db.conn.execute(
        "UPDATE operation_cases SET transfer_department = 'РАО' WHERE id = ?",
        (case["operation_case_id"],),
    )
    db.conn.commit()

    candidates = service.find_late_binding_candidates(case["operation_case_id"])
    assert [int(item["id"]) for item in candidates] == [int(handoff["id"])]
    service.release_operation_table(
        case["operation_case_id"],
        handoff_id=int(handoff["id"]),
    )

    linked_case = db.fetch_one_remcard(
        """
        SELECT source_rao_admission_id, resolved_rao_admission_id
        FROM operation_cases
        WHERE id = ?
        """,
        (case["operation_case_id"],),
    )
    assert tuple(linked_case) == (admission_id, admission_id)
    assert db.fetch_one_remcard(
        """
        SELECT status FROM patient_status_events
        WHERE admission_id = ? AND end_time IS NULL
        """,
        (admission_id,),
    )["status"] == "ACTIVE"


def test_recovery_merge_keeps_target_card_and_excludes_source(db):
    target_id = _create_rao_patient(
        db,
        bed_number=4,
        history_number="200/1",
        status=PatientStatus.OR,
    )
    cursor = db.conn.cursor()
    target = cursor.execute(
        "SELECT patient_id FROM admissions WHERE id = ?",
        (target_id,),
    ).fetchone()
    cursor.execute(
        """
        INSERT INTO operation_cases (
            patient_id, admission_id, table_code, status, created_at, started_at,
            transfer_department
        ) VALUES (?, ?, 'emergency', 'closed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'РАО')
        """,
        (target["patient_id"], target_id),
    )
    operation_case_id = int(cursor.lastrowid)
    source_id = _create_rao_patient(
        db,
        bed_number=10,
        history_number="ДРУГАЯ-ИБ",
        full_name="Иванов Иван Иванович",
        recovery=True,
        intake_extra_json=json.dumps(
            {
                "source": "operblock_rao_transfer",
                "operation_case_id": operation_case_id,
            },
            ensure_ascii=False,
        ),
    )
    cursor = db.conn.cursor()
    cursor.execute(
        """
        UPDATE operation_cases
        SET future_rao_admission_id = ?
        WHERE id = ?
        """,
        (source_id, operation_case_id),
    )
    cursor.execute(
        """
        INSERT INTO vitals (admission_id, datetime, sys, dia, pulse, spo2)
        VALUES (?, CURRENT_TIMESTAMP, 118, 74, 70, 99)
        """,
        (source_id,),
    )
    db.conn.commit()

    service = PatientBedManagementService(db)
    preview = service.get_recovery_merge_preview(10, 4)
    assert preview["history_number_matches"] is False
    with pytest.raises(RuntimeError, match="отдельное подтверждение"):
        service.merge_recovery_admission(
            10,
            4,
            expected_source_bed_revision=preview["source_bed_revision"],
            expected_target_bed_revision=preview["target_bed_revision"],
            expected_source_admission_revision=preview["source_admission_revision"],
            expected_target_admission_revision=preview["target_admission_revision"],
        )
    result = service.merge_recovery_admission(
        10,
        4,
        expected_source_bed_revision=preview["source_bed_revision"],
        expected_target_bed_revision=preview["target_bed_revision"],
        expected_source_admission_revision=preview["source_admission_revision"],
        expected_target_admission_revision=preview["target_admission_revision"],
        allow_identity_mismatch=True,
    )
    assert result["target_admission_id"] == target_id
    assert result["movement_changed"] is True
    assert db.fetch_one_remcard(
        "SELECT status, current_admission_id FROM beds WHERE bed_number = 10"
    )["status"] == "FREE"
    source = db.fetch_one_remcard(
        "SELECT merged_into_admission_id, is_active FROM admissions WHERE id = ?",
        (source_id,),
    )
    assert source["merged_into_admission_id"] == target_id
    assert source["is_active"] == 0
    assert db.fetch_one_remcard(
        """
        SELECT status FROM patient_status_events
        WHERE admission_id = ? AND end_time IS NULL
        """,
        (target_id,),
    )["status"] == "ACTIVE"
    assert db.fetch_one_remcard(
        "SELECT resolved_rao_admission_id FROM operation_cases WHERE id = ?",
        (operation_case_id,),
    )["resolved_rao_admission_id"] == target_id
    today = datetime.now().date().isoformat()
    assert fetch_recovery_bed_admission_rows(db.conn, today, today) == []
    patient_columns = {
        row["name"] for row in db.conn.execute("PRAGMA table_info(patients)").fetchall()
    }
    admission_columns = {
        row["name"] for row in db.conn.execute("PRAGMA table_info(admissions)").fetchall()
    }
    archive_query, archive_params = PatientDAO._build_archived_patients_query(
        patient_columns=patient_columns,
        admission_columns=admission_columns,
        has_operations_table=True,
        has_operation_cases_table=True,
    )
    archive_ids = {
        int(row["admission_id"])
        for row in db.conn.execute(archive_query, archive_params).fetchall()
    }
    assert source_id not in archive_ids
