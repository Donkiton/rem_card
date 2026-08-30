from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Optional

from rem_card.app.patient_age import parse_date_value
from rem_card.services.concurrency import assert_revision_matches
from rem_card.services.patient_bed_management.recovery_beds import is_recovery_bed_number
from rem_card.services.operblock_handoff_service import (
    HANDOFF_RETURNED_TO_RAO,
    normalize_handoff_full_name,
    normalize_handoff_history_number,
)
from rem_card.services.shift_service import ShiftService
from rem_card.data.dto.remcard_dto import PatientStatus


@dataclass
class PatientRecord:
    id: int
    full_name: str
    admission_uid: Optional[str] = None
    birth_date: Optional[date] = None


@dataclass
class AdmissionRecord:
    id: Optional[int]
    patient_id: int
    bed_number: int
    history_number: str
    admission_datetime: Optional[datetime] = None
    patient_age: Optional[int] = None
    patient_months: Optional[int] = None
    patient_age_unit: Optional[str] = None
    patient_gender: Optional[str] = None
    current_status: Optional[str] = None
    diagnosis_code: Optional[str] = None
    diagnosis_text: Optional[str] = None
    department_profile: Optional[str] = None
    source_department: Optional[str] = None
    revision: int = 0


class PatientBedManagementService:
    def __init__(self, db_manager, data_service=None):
        self.db = db_manager
        self.data_service = data_service

    def enqueue_write(self, description: str, operation: Callable[[], Any], on_success=None, on_error=None):
        if self.data_service:
            self.data_service.enqueue_write(
                description=description,
                operation=operation,
                on_success=on_success,
                on_error=on_error,
            )
            return
        try:
            result = operation()
        except Exception as exc:
            if on_error:
                on_error(exc)
                return
            raise
        if on_success:
            on_success(result)

    def get_beds_snapshot(self):
        return self.db.fetch_all_remcard(
            """
            SELECT
                b.bed_number,
                b.status,
                b.current_admission_id,
                p.id AS p_id,
                p.full_name,
                p.admission_uid,
                p.birth_date,
                a.patient_id,
                a.history_number,
                a.admission_datetime,
                a.patient_age,
                a.patient_months,
                a.patient_age_unit,
                a.patient_gender,
                a.diagnosis_code,
                a.diagnosis_text,
                a.department_profile,
                a.source_department,
                (
                    SELECT pse.status
                    FROM patient_status_events pse
                    WHERE pse.admission_id = a.id
                      AND pse.end_time IS NULL
                    ORDER BY datetime(pse.start_time) DESC, pse.id DESC
                    LIMIT 1
                ) AS current_status,
                COALESCE(b.revision, 0) AS bed_revision,
                COALESCE(a.revision, 0) AS admission_revision
            FROM beds b
            LEFT JOIN admissions a ON a.id = b.current_admission_id
            LEFT JOIN patients p ON p.id = a.patient_id
            ORDER BY b.bed_number
            """
        )

    def get_bed_by_number(self, bed_number: int):
        return self.db.fetch_one_remcard("SELECT * FROM beds WHERE bed_number = ?", (int(bed_number),))

    @staticmethod
    def _ensure_bed_exists(cursor, bed_number: int) -> None:
        cursor.execute(
            """
            INSERT OR IGNORE INTO beds (
                bed_number, status, current_admission_id, revision
            ) VALUES (?, 'FREE', NULL, 0)
            """,
            (int(bed_number),),
        )

    def get_patient_with_current_admission(self, bed_number: int) -> tuple[Optional[PatientRecord], Optional[AdmissionRecord]]:
        row = self.db.fetch_one_remcard(
            """
            SELECT
                p.id AS p_id,
                p.full_name,
                p.admission_uid,
                p.birth_date,
                a.*,
                (
                    SELECT pse.status
                    FROM patient_status_events pse
                    WHERE pse.admission_id = a.id
                      AND pse.end_time IS NULL
                    ORDER BY datetime(pse.start_time) DESC, pse.id DESC
                    LIMIT 1
                ) AS current_status
            FROM beds b
            JOIN admissions a ON a.id = b.current_admission_id
            JOIN patients p ON p.id = a.patient_id
            WHERE b.bed_number = ?
              AND b.status = 'OCCUPIED'
              AND b.current_admission_id IS NOT NULL
            """,
            (int(bed_number),),
        )
        return self._records_from_admission_row(row)

    def records_from_bed_snapshot_row(self, row) -> tuple[Optional[PatientRecord], Optional[AdmissionRecord]]:
        if not row:
            return None, None
        data = dict(row)
        if data.get("current_admission_id") is None or str(data.get("status") or "").upper() == "FREE":
            return None, None
        if data.get("p_id") is None or data.get("patient_id") is None:
            return None, None
        patient = PatientRecord(
            id=int(data["p_id"]),
            full_name=str(data.get("full_name") or ""),
            admission_uid=data.get("admission_uid"),
            birth_date=self._parse_date(data.get("birth_date")),
        )
        admission = AdmissionRecord(
            id=int(data["current_admission_id"]) if data.get("current_admission_id") is not None else None,
            patient_id=int(data["patient_id"]),
            bed_number=int(data["bed_number"]),
            history_number=str(data.get("history_number") or ""),
            admission_datetime=self._parse_dt(data.get("admission_datetime")),
            patient_age=self._safe_int_or_none(data.get("patient_age")),
            patient_months=self._safe_int_or_none(data.get("patient_months")),
            patient_age_unit=data.get("patient_age_unit"),
            patient_gender=data.get("patient_gender"),
            current_status=data.get("current_status"),
            diagnosis_code=data.get("diagnosis_code"),
            diagnosis_text=data.get("diagnosis_text"),
            department_profile=data.get("department_profile"),
            source_department=data.get("source_department"),
            revision=int(data.get("admission_revision") or 0),
        )
        return patient, admission

    def get_patient_with_admission(self, admission_id: int) -> tuple[Optional[PatientRecord], Optional[AdmissionRecord]]:
        row = self.db.fetch_one_remcard(
            """
            SELECT
                p.id AS p_id,
                p.full_name,
                p.admission_uid,
                p.birth_date,
                a.*,
                (
                    SELECT pse.status
                    FROM patient_status_events pse
                    WHERE pse.admission_id = a.id
                      AND pse.end_time IS NULL
                    ORDER BY datetime(pse.start_time) DESC, pse.id DESC
                    LIMIT 1
                ) AS current_status
            FROM admissions a
            JOIN patients p ON p.id = a.patient_id
            WHERE a.id = ?
            """,
            (int(admission_id),),
        )
        return self._records_from_admission_row(row)

    def _records_from_admission_row(self, row) -> tuple[Optional[PatientRecord], Optional[AdmissionRecord]]:
        if not row:
            return None, None
        data = dict(row)
        patient = PatientRecord(
            id=int(data["p_id"]),
            full_name=str(data.get("full_name") or ""),
            admission_uid=data.get("admission_uid"),
            birth_date=self._parse_date(data.get("birth_date")),
        )
        admission = AdmissionRecord(
            id=int(data["id"]) if data.get("id") is not None else None,
            patient_id=int(data["patient_id"]),
            bed_number=int(data["bed_number"]),
            history_number=str(data.get("history_number") or ""),
            admission_datetime=self._parse_dt(data.get("admission_datetime")),
            patient_age=self._safe_int_or_none(data.get("patient_age")),
            patient_months=self._safe_int_or_none(data.get("patient_months")),
            patient_age_unit=data.get("patient_age_unit"),
            patient_gender=data.get("patient_gender"),
            current_status=data.get("current_status"),
            diagnosis_code=data.get("diagnosis_code"),
            diagnosis_text=data.get("diagnosis_text"),
            department_profile=data.get("department_profile"),
            source_department=data.get("source_department"),
            revision=int(data.get("revision") or 0),
        )
        return patient, admission

    def create_patient_and_admission(self, patient_data: dict[str, Any], admission_data: dict[str, Any]) -> int:
        admission_uid = str(patient_data.get("admission_uid") or uuid.uuid4())
        full_name = str(patient_data.get("full_name") or "").strip()
        birth_date = self._to_sql_date(patient_data.get("birth_date"))
        if not full_name:
            raise ValueError("ФИО пациента не заполнено.")

        def operation(cursor):
            bed_number = int(admission_data["bed_number"])
            admission_dt_text = self._to_sql_dt(admission_data.get("admission_datetime"))
            recovery_bed_stay = 1 if is_recovery_bed_number(bed_number) else 0
            self._ensure_bed_exists(cursor, bed_number)
            cursor.execute(
                "INSERT INTO patients (full_name, admission_uid, birth_date) VALUES (?, ?, ?)",
                (full_name, admission_uid, birth_date),
            )
            patient_id = int(cursor.lastrowid)
            now = self._now_text()
            cursor.execute(
                """
                INSERT INTO admissions (
                    patient_id, bed_number, history_number, admission_datetime,
                    patient_age, patient_months, patient_age_unit, patient_gender,
                    diagnosis_code, diagnosis_text, department_profile, source_department,
                    recovery_bed_stay, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    patient_id,
                    bed_number,
                    str(admission_data.get("history_number") or "").strip(),
                    admission_dt_text,
                    self._safe_int_or_none(admission_data.get("patient_age")),
                    self._safe_int_or_none(admission_data.get("patient_months")),
                    admission_data.get("patient_age_unit"),
                    admission_data.get("patient_gender"),
                    admission_data.get("diagnosis_code"),
                    admission_data.get("diagnosis_text"),
                    admission_data.get("department_profile"),
                    admission_data.get("source_department"),
                    recovery_bed_stay,
                    now,
                    now,
                ),
            )
            admission_id = int(cursor.lastrowid)
            if recovery_bed_stay:
                self._insert_initial_active_status(cursor, admission_id, admission_dt_text)
            cursor.execute(
                """
                UPDATE beds
                SET status = ?,
                    current_admission_id = ?,
                    revision = COALESCE(revision, 0) + 1
                WHERE bed_number = ?
                  AND status = 'FREE'
                  AND current_admission_id IS NULL
                """,
                ("OCCUPIED", admission_id, bed_number),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"Койка {admission_data['bed_number']} уже занята другим пользователем.")
            return admission_id

        return int(self.db.run_write_operation(operation, source="patient_bed_create_admission"))

    def update_patient_and_admission(
        self,
        patient_id: int,
        admission_id: int,
        patient_data: dict[str, Any],
        admission_data: dict[str, Any],
        expected_admission_revision: Optional[int] = None,
    ) -> bool:
        full_name = str(patient_data.get("full_name") or "").strip()
        birth_date = self._to_sql_date(patient_data.get("birth_date"))
        if not full_name:
            raise ValueError("ФИО пациента не заполнено.")

        def operation(cursor):
            cursor.execute(
                "SELECT admission_datetime FROM admissions WHERE id = ?",
                (int(admission_id),),
            )
            old_admission = cursor.fetchone()
            old_admission_dt = self._parse_dt(old_admission["admission_datetime"]) if old_admission else None
            new_admission_dt_text = self._to_sql_dt(admission_data.get("admission_datetime"))
            new_admission_dt = self._parse_dt(new_admission_dt_text)
            bed_number = int(admission_data["bed_number"])

            cursor.execute(
                "UPDATE patients SET full_name = ?, birth_date = ? WHERE id = ?",
                (full_name, birth_date, int(patient_id)),
            )
            cursor.execute(
                """
                UPDATE admissions
                SET bed_number = ?,
                    history_number = ?,
                    admission_datetime = ?,
                    patient_age = ?,
                    patient_months = ?,
                    patient_age_unit = ?,
                    patient_gender = ?,
                    diagnosis_code = ?,
                    diagnosis_text = ?,
                    department_profile = ?,
                    source_department = ?,
                    recovery_bed_stay = CASE WHEN ? = 1 THEN 1 ELSE COALESCE(recovery_bed_stay, 0) END,
                    updated_at = ?,
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ?
                  AND (? IS NULL OR COALESCE(revision, 0) = ?)
                """,
                (
                    bed_number,
                    str(admission_data.get("history_number") or "").strip(),
                    new_admission_dt_text,
                    self._safe_int_or_none(admission_data.get("patient_age")),
                    self._safe_int_or_none(admission_data.get("patient_months")),
                    admission_data.get("patient_age_unit"),
                    admission_data.get("patient_gender"),
                    admission_data.get("diagnosis_code"),
                    admission_data.get("diagnosis_text"),
                    admission_data.get("department_profile"),
                    admission_data.get("source_department"),
                    1 if is_recovery_bed_number(admission_data["bed_number"]) else 0,
                    self._now_text(),
                    int(admission_id),
                    expected_admission_revision,
                    expected_admission_revision,
                ),
            )
            if cursor.rowcount != 1:
                from rem_card.services.concurrency import DataConflictError, DATA_CONFLICT_MESSAGE

                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            self._sync_admission_datetime_dependents(
                cursor,
                int(admission_id),
                old_admission_dt,
                new_admission_dt,
                bed_number=bed_number,
            )
            return True

        return bool(self.db.run_write_operation(operation, source="patient_bed_update_admission"))

    def move_patient(
        self,
        source_bed: int,
        target_bed: int,
        *,
        expected_source_bed_revision: Optional[int] = None,
        expected_target_bed_revision: Optional[int] = None,
        expected_source_admission_revision: Optional[int] = None,
        expected_target_admission_revision: Optional[int] = None,
    ):
        source_bed = int(source_bed)
        target_bed = int(target_bed)

        def operation(cursor):
            source = cursor.execute("SELECT * FROM beds WHERE bed_number = ?", (source_bed,)).fetchone()
            if not source or source["status"] == "FREE" or source["current_admission_id"] is None:
                return False
            self._ensure_bed_exists(cursor, target_bed)
            target = cursor.execute("SELECT * FROM beds WHERE bed_number = ?", (target_bed,)).fetchone()
            if not target:
                return False
            source_is_recovery = is_recovery_bed_number(source_bed)
            target_is_recovery = is_recovery_bed_number(target_bed)
            target_is_occupied = bool(target["status"] != "FREE" and target["current_admission_id"] is not None)
            if not source_is_recovery and target_is_recovery:
                raise RuntimeError("Пациента с обычной койки нельзя перенести на койку пробуждения.")
            if source_is_recovery and target_is_occupied:
                raise RuntimeError("Пациента с койки пробуждения можно перенести только на свободную койку.")

            assert_revision_matches(source["revision"] if "revision" in source.keys() else 0, expected_source_bed_revision)
            assert_revision_matches(target["revision"] if "revision" in target.keys() else 0, expected_target_bed_revision)

            source_admission_id = int(source["current_admission_id"])
            source_admission = cursor.execute(
                "SELECT id, admission_datetime, COALESCE(revision, 0) AS revision FROM admissions WHERE id = ?",
                (source_admission_id,),
            ).fetchone()
            assert_revision_matches(
                source_admission["revision"] if source_admission else 0,
                expected_source_admission_revision,
            )
            if target_is_occupied:
                target_admission_id = int(target["current_admission_id"])
                target_admission = cursor.execute(
                    "SELECT COALESCE(revision, 0) AS revision FROM admissions WHERE id = ?",
                    (target_admission_id,),
                ).fetchone()
                assert_revision_matches(
                    target_admission["revision"] if target_admission else 0,
                    expected_target_admission_revision,
                )
                cursor.execute(
                    """
                    UPDATE beds
                    SET current_admission_id = NULL,
                        status = 'FREE',
                        revision = COALESCE(revision, 0) + 1
                    WHERE bed_number IN (?, ?)
                    """,
                    (source_bed, target_bed),
                )
                cursor.execute(
                    """
                    UPDATE admissions
                    SET bed_number = ?,
                        recovery_bed_stay = CASE WHEN ? = 1 THEN 1 ELSE COALESCE(recovery_bed_stay, 0) END,
                        updated_at = ?,
                        revision = COALESCE(revision, 0) + 1
                    WHERE id = ?
                    """,
                    (
                        target_bed,
                        1 if source_is_recovery or target_is_recovery else 0,
                        self._now_text(),
                        source_admission_id,
                    ),
                )
                cursor.execute(
                    """
                    UPDATE admissions
                    SET bed_number = ?,
                        recovery_bed_stay = CASE WHEN ? = 1 THEN 1 ELSE COALESCE(recovery_bed_stay, 0) END,
                        updated_at = ?,
                        revision = COALESCE(revision, 0) + 1
                    WHERE id = ?
                    """,
                    (
                        source_bed,
                        1 if target_is_recovery else 0,
                        self._now_text(),
                        target_admission_id,
                    ),
                )
                cursor.execute(
                    "UPDATE beds SET current_admission_id = ?, status = 'OCCUPIED', revision = COALESCE(revision, 0) + 1 WHERE bed_number = ?",
                    (source_admission_id, target_bed),
                )
                cursor.execute(
                    "UPDATE beds SET current_admission_id = ?, status = 'OCCUPIED', revision = COALESCE(revision, 0) + 1 WHERE bed_number = ?",
                    (target_admission_id, source_bed),
                )
            else:
                cursor.execute(
                    "UPDATE beds SET current_admission_id = NULL, status = 'FREE', revision = COALESCE(revision, 0) + 1 WHERE bed_number = ?",
                    (source_bed,),
                )
                cursor.execute(
                    """
                    UPDATE admissions
                    SET bed_number = ?,
                        recovery_bed_stay = CASE WHEN ? = 1 THEN 1 ELSE COALESCE(recovery_bed_stay, 0) END,
                        updated_at = ?,
                        revision = COALESCE(revision, 0) + 1
                    WHERE id = ?
                    """,
                    (
                        target_bed,
                        1 if source_is_recovery or target_is_recovery else 0,
                        self._now_text(),
                        source_admission_id,
                    ),
                )
                cursor.execute(
                    "UPDATE beds SET current_admission_id = ?, status = 'OCCUPIED', revision = COALESCE(revision, 0) + 1 WHERE bed_number = ?",
                    (source_admission_id, target_bed),
                )
                if source_is_recovery and not target_is_recovery:
                    self._ensure_recovery_release_card(cursor, source_admission_id, source_admission)
            return True

        return self.db.run_write_operation(operation, source="patient_bed_move_patient")

    def get_recovery_merge_preview(self, source_bed: int, target_bed: int) -> dict[str, Any]:
        source_bed = int(source_bed)
        target_bed = int(target_bed)
        if not is_recovery_bed_number(source_bed) or is_recovery_bed_number(target_bed):
            raise RuntimeError("Слияние доступно только с койки пробуждения на обычную койку.")
        rows = self.db.fetch_all_remcard(
            """
            SELECT
                b.bed_number,
                b.status AS bed_status,
                COALESCE(b.revision, 0) AS bed_revision,
                a.id AS admission_id,
                a.history_number,
                a.admission_datetime,
                a.intake_extra_json,
                COALESCE(a.recovery_bed_stay, 0) AS recovery_bed_stay,
                COALESCE(a.revision, 0) AS admission_revision,
                p.full_name,
                p.birth_date
            FROM beds b
            JOIN admissions a ON a.id = b.current_admission_id
            JOIN patients p ON p.id = a.patient_id
            WHERE b.bed_number IN (?, ?)
            """,
            (source_bed, target_bed),
        )
        by_bed = {int(row["bed_number"]): dict(row) for row in rows}
        source = by_bed.get(source_bed)
        target = by_bed.get(target_bed)
        if not source or str(source.get("bed_status") or "") == "FREE":
            raise RuntimeError("Койка пробуждения уже свободна.")
        if not target or str(target.get("bed_status") or "") == "FREE":
            raise RuntimeError("Целевая койка уже свободна; используйте обычный перенос.")
        try:
            intake = json.loads(str(source.get("intake_extra_json") or "{}"))
        except Exception:
            intake = {}
        operation_case_id = int(intake.get("operation_case_id") or 0) if isinstance(intake, dict) else 0
        if not source.get("recovery_bed_stay") or not operation_case_id:
            raise RuntimeError(
                "Исходная карта не подтверждена как поступившая на койку пробуждения из оперблока."
            )
        history_matches = (
            normalize_handoff_history_number(source.get("history_number"))
            == normalize_handoff_history_number(target.get("history_number"))
        )
        full_name_matches = (
            normalize_handoff_full_name(source.get("full_name"))
            == normalize_handoff_full_name(target.get("full_name"))
        )
        birth_date_matches = str(source.get("birth_date") or "") == str(target.get("birth_date") or "")
        return {
            "source_bed": source_bed,
            "target_bed": target_bed,
            "source_admission_id": int(source["admission_id"]),
            "target_admission_id": int(target["admission_id"]),
            "operation_case_id": operation_case_id,
            "source_full_name": str(source.get("full_name") or ""),
            "target_full_name": str(target.get("full_name") or ""),
            "source_history_number": str(source.get("history_number") or ""),
            "target_history_number": str(target.get("history_number") or ""),
            "history_number_matches": history_matches,
            "full_name_matches": full_name_matches,
            "birth_date_matches": birth_date_matches,
            "source_bed_revision": int(source.get("bed_revision") or 0),
            "target_bed_revision": int(target.get("bed_revision") or 0),
            "source_admission_revision": int(source.get("admission_revision") or 0),
            "target_admission_revision": int(target.get("admission_revision") or 0),
        }

    def merge_recovery_admission(
        self,
        source_bed: int,
        target_bed: int,
        *,
        expected_source_bed_revision: int | None = None,
        expected_target_bed_revision: int | None = None,
        expected_source_admission_revision: int | None = None,
        expected_target_admission_revision: int | None = None,
        user_id: str | None = None,
        allow_identity_mismatch: bool = False,
    ) -> dict[str, Any]:
        source_bed = int(source_bed)
        target_bed = int(target_bed)
        if not is_recovery_bed_number(source_bed) or is_recovery_bed_number(target_bed):
            raise RuntimeError("Недопустимое направление слияния карт.")
        actor = str(user_id or "USER")

        def operation(cursor):
            source_bed_row = cursor.execute(
                "SELECT * FROM beds WHERE bed_number = ?",
                (source_bed,),
            ).fetchone()
            target_bed_row = cursor.execute(
                "SELECT * FROM beds WHERE bed_number = ?",
                (target_bed,),
            ).fetchone()
            if (
                not source_bed_row
                or source_bed_row["status"] == "FREE"
                or source_bed_row["current_admission_id"] is None
                or not target_bed_row
                or target_bed_row["status"] == "FREE"
                or target_bed_row["current_admission_id"] is None
            ):
                raise RuntimeError("Состав коек изменился. Обновите окно движения пациентов.")
            assert_revision_matches(source_bed_row["revision"], expected_source_bed_revision)
            assert_revision_matches(target_bed_row["revision"], expected_target_bed_revision)
            source_admission_id = int(source_bed_row["current_admission_id"])
            target_admission_id = int(target_bed_row["current_admission_id"])
            source = cursor.execute(
                """
                SELECT a.*, p.full_name, p.birth_date
                FROM admissions a
                JOIN patients p ON p.id = a.patient_id
                WHERE a.id = ?
                """,
                (source_admission_id,),
            ).fetchone()
            target = cursor.execute(
                """
                SELECT a.*, p.full_name, p.birth_date
                FROM admissions a
                JOIN patients p ON p.id = a.patient_id
                WHERE a.id = ?
                """,
                (target_admission_id,),
            ).fetchone()
            if not source or not target:
                raise RuntimeError("Одна из карт пациента уже недоступна.")
            assert_revision_matches(source["revision"], expected_source_admission_revision)
            assert_revision_matches(target["revision"], expected_target_admission_revision)
            if source["merged_into_admission_id"] is not None:
                raise RuntimeError("Карта с койки пробуждения уже была объединена.")
            if target["merged_into_admission_id"] is not None:
                raise RuntimeError("Целевая карта уже является частью другого слияния.")
            try:
                intake = json.loads(str(source["intake_extra_json"] or "{}"))
            except Exception:
                intake = {}
            operation_case_id = int(intake.get("operation_case_id") or 0) if isinstance(intake, dict) else 0
            if not int(source["recovery_bed_stay"] or 0) or not operation_case_id:
                raise RuntimeError("Карта не подтверждена как поступившая из оперблока.")
            operation_case = cursor.execute(
                """
                SELECT id, handoff_id, source_rao_admission_id
                FROM operation_cases
                WHERE id = ? AND future_rao_admission_id = ?
                """,
                (operation_case_id, source_admission_id),
            ).fetchone()
            if not operation_case:
                raise RuntimeError("Не удалось подтвердить операционный случай для слияния.")
            linked_source_id = operation_case["source_rao_admission_id"]
            if linked_source_id is not None and int(linked_source_id) != target_admission_id:
                raise RuntimeError("Операционный случай уже связан с другой исходной картой РАО.")
            target_open_status = cursor.execute(
                """
                SELECT status
                FROM patient_status_events
                WHERE admission_id = ? AND end_time IS NULL
                LIMIT 1
                """,
                (target_admission_id,),
            ).fetchone()
            if target_open_status and str(target_open_status["status"] or "") in {
                PatientStatus.TRANSFERRED.value,
                PatientStatus.DEAD.value,
            }:
                raise RuntimeError("Карту с финальным исходом нельзя использовать как главную для слияния.")

            history_matches = (
                normalize_handoff_history_number(source["history_number"])
                == normalize_handoff_history_number(target["history_number"])
            )
            full_name_matches = (
                normalize_handoff_full_name(source["full_name"])
                == normalize_handoff_full_name(target["full_name"])
            )
            birth_date_matches = str(source["birth_date"] or "") == str(target["birth_date"] or "")
            if (
                not allow_identity_mismatch
                and not (history_matches and full_name_matches and birth_date_matches)
            ):
                raise RuntimeError(
                    "Идентификационные данные различаются. "
                    "Для слияния требуется отдельное подтверждение пользователя."
                )
            now = self._now_text()
            comparison = {
                "source_history_number": source["history_number"],
                "target_history_number": target["history_number"],
                "source_full_name": source["full_name"],
                "target_full_name": target["full_name"],
                "source_birth_date": source["birth_date"],
                "target_birth_date": target["birth_date"],
                "birth_date_matches": birth_date_matches,
            }
            cursor.execute(
                """
                INSERT INTO admission_merges (
                    source_admission_id, target_admission_id, operation_case_id,
                    source_bed_number, target_bed_number,
                    history_number_matches, full_name_matches, comparison_json,
                    merged_at, merged_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_admission_id,
                    target_admission_id,
                    operation_case_id,
                    source_bed,
                    target_bed,
                    1 if history_matches else 0,
                    1 if full_name_matches else 0,
                    json.dumps(comparison, ensure_ascii=False, sort_keys=True, default=str),
                    now,
                    actor,
                ),
            )
            cursor.execute(
                """
                UPDATE admissions
                SET merged_into_admission_id = ?,
                    merged_at = ?,
                    is_active = 0,
                    updated_at = ?,
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ? AND merged_into_admission_id IS NULL
                """,
                (target_admission_id, now, now, source_admission_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Карта с койки пробуждения уже была объединена.")
            cursor.execute(
                """
                UPDATE beds
                SET current_admission_id = NULL,
                    status = 'FREE',
                    revision = COALESCE(revision, 0) + 1
                WHERE bed_number = ? AND current_admission_id = ?
                """,
                (source_bed, source_admission_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Койка пробуждения изменилась другим пользователем.")

            arrival_dt = self._parse_dt(source["admission_datetime"]) or datetime.now()
            target_status = cursor.execute(
                """
                SELECT id, status, start_time
                FROM patient_status_events
                WHERE admission_id = ? AND end_time IS NULL
                LIMIT 1
                """,
                (target_admission_id,),
            ).fetchone()
            movement_changed = False
            if target_status and str(target_status["status"] or "") == PatientStatus.OR.value:
                status_start = self._parse_dt(target_status["start_time"])
                effective_dt = max(
                    arrival_dt.replace(microsecond=0),
                    status_start.replace(microsecond=0) if status_start else arrival_dt.replace(microsecond=0),
                )
                effective_text = effective_dt.isoformat(timespec="seconds")
                cursor.execute(
                    """
                    UPDATE patient_status_events
                    SET end_time = ?, updated_at = ?, last_modified_by = ?,
                        revision = COALESCE(revision, 0) + 1
                    WHERE id = ? AND end_time IS NULL
                    """,
                    (effective_text, now, actor, int(target_status["id"])),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("Движение целевой карты изменилось другим пользователем.")
                cursor.execute(
                    """
                    INSERT INTO patient_status_events (
                        admission_id, status, reason_type, reason_text, start_time,
                        created_by, created_at, updated_at, last_modified_by
                    ) VALUES (?, ?, 'operblock_merge', 'Возврат из операционной после слияния',
                              ?, ?, ?, ?, ?)
                    """,
                    (
                        target_admission_id,
                        PatientStatus.ACTIVE.value,
                        effective_text,
                        actor,
                        now,
                        now,
                        actor,
                    ),
                )
                movement_changed = True

            source_status = cursor.execute(
                """
                SELECT id, start_time
                FROM patient_status_events
                WHERE admission_id = ? AND end_time IS NULL
                LIMIT 1
                """,
                (source_admission_id,),
            ).fetchone()
            if source_status:
                source_start = self._parse_dt(source_status["start_time"])
                source_end = max(datetime.now(), source_start) if source_start else datetime.now()
                cursor.execute(
                    """
                    UPDATE patient_status_events
                    SET end_time = ?, updated_at = ?, last_modified_by = ?,
                        revision = COALESCE(revision, 0) + 1
                    WHERE id = ? AND end_time IS NULL
                    """,
                    (
                        source_end.replace(microsecond=0).isoformat(timespec="seconds"),
                        now,
                        actor,
                        int(source_status["id"]),
                    ),
                )

            latest_vital = cursor.execute(
                """
                SELECT sys, dia, pulse, temp, spo2, rr, cvp
                FROM vitals
                WHERE admission_id = ?
                ORDER BY DATETIME(datetime) DESC, id DESC
                LIMIT 1
                """,
                (source_admission_id,),
            ).fetchone()
            if latest_vital:
                cursor.execute(
                    """
                    INSERT INTO vitals (
                        admission_id, datetime, sys, dia, pulse, temp, spo2, rr, cvp,
                        created_at, updated_at, last_modified_by
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        target_admission_id,
                        arrival_dt.replace(microsecond=0).isoformat(timespec="seconds"),
                        latest_vital["sys"],
                        latest_vital["dia"],
                        latest_vital["pulse"],
                        latest_vital["temp"],
                        latest_vital["spo2"],
                        latest_vital["rr"],
                        latest_vital["cvp"],
                        now,
                        now,
                        actor,
                    ),
                )
            cursor.execute(
                """
                UPDATE admissions
                SET operation_description = COALESCE(
                        NULLIF(TRIM(operation_description), ''),
                        (SELECT planned_operation_name FROM operation_cases WHERE id = ?)
                    ),
                    updated_at = ?,
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ?
                """,
                (operation_case_id, now, target_admission_id),
            )
            cursor.execute(
                """
                UPDATE operation_cases
                SET source_rao_admission_id = COALESCE(source_rao_admission_id, ?),
                    resolved_rao_admission_id = ?,
                    last_modified_by = ?,
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ?
                """,
                (target_admission_id, target_admission_id, actor, operation_case_id),
            )
            handoff = cursor.execute(
                """
                SELECT id
                FROM operblock_handoffs
                WHERE source_admission_id = ?
                  AND status IN ('waiting', 'accepted')
                ORDER BY id DESC
                LIMIT 1
                """,
                (target_admission_id,),
            ).fetchone()
            if handoff:
                cursor.execute(
                    """
                    UPDATE operblock_handoffs
                    SET status = ?,
                        operation_case_id = ?,
                        transfer_department = 'РАО',
                        released_at = COALESCE(released_at, ?),
                        effective_return_at = COALESCE(effective_return_at, ?),
                        last_modified_by = ?,
                        revision = COALESCE(revision, 0) + 1
                    WHERE id = ?
                    """,
                    (
                        HANDOFF_RETURNED_TO_RAO,
                        operation_case_id,
                        now,
                        arrival_dt.replace(microsecond=0).isoformat(timespec="seconds"),
                        actor,
                        int(handoff["id"]),
                    ),
                )
                cursor.execute(
                    """
                    UPDATE operation_cases
                    SET handoff_id = COALESCE(handoff_id, ?)
                    WHERE id = ?
                    """,
                    (int(handoff["id"]), operation_case_id),
                )
            return {
                "source_admission_id": source_admission_id,
                "target_admission_id": target_admission_id,
                "operation_case_id": operation_case_id,
                "movement_changed": movement_changed,
            }

        return dict(self.db.run_write_operation(operation, source="patient_bed_merge_recovery"))

    def _insert_initial_active_status(self, cursor, admission_id: int, admission_datetime) -> None:
        cursor.execute("SELECT COUNT(*) AS cnt FROM patient_status_events WHERE admission_id = ?", (int(admission_id),))
        row = cursor.fetchone()
        if row and int(row["cnt"] or 0) > 0:
            return

        start_dt = self._parse_dt(admission_datetime) or datetime.now()
        start_text = start_dt.replace(microsecond=0).isoformat()
        cursor.execute(
            """
            INSERT INTO patient_status_events
            (admission_id, status, start_time, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (int(admission_id), PatientStatus.ACTIVE.value, start_text, "SYSTEM", start_text, start_text),
        )

    def _sync_admission_datetime_dependents(
        self,
        cursor,
        admission_id: int,
        old_admission_dt: Optional[datetime],
        new_admission_dt: Optional[datetime],
        *,
        bed_number: int,
    ) -> None:
        if new_admission_dt is None:
            return

        new_start = new_admission_dt.replace(second=0, microsecond=0)
        old_start = old_admission_dt.replace(second=0, microsecond=0) if old_admission_dt else None
        if old_start != new_start:
            self._relocate_initial_active_status(cursor, admission_id, old_start, new_start)
            self._relocate_empty_admission_vital(cursor, admission_id, old_start, new_start)

        if is_recovery_bed_number(bed_number) or self._has_any_card_record(cursor, admission_id):
            self._ensure_status_for_existing_card(cursor, admission_id, new_start)

    def _relocate_initial_active_status(
        self,
        cursor,
        admission_id: int,
        old_start: Optional[datetime],
        new_start: datetime,
    ) -> bool:
        if not self._table_exists(cursor, "patient_status_events"):
            return False

        cursor.execute(
            """
            SELECT id, status, start_time, end_time, created_at
            FROM patient_status_events
            WHERE admission_id = ?
            ORDER BY datetime(start_time) ASC, id ASC
            """,
            (int(admission_id),),
        )
        events = cursor.fetchall()
        if not events:
            return False

        first = events[0]
        if first["status"] != PatientStatus.ACTIVE.value:
            return False

        first_start = self._parse_dt(first["start_time"])
        if first_start is None:
            return False
        first_start = first_start.replace(second=0, microsecond=0)

        end_dt = self._parse_dt(first["end_time"])
        if end_dt is not None and new_start >= end_dt.replace(second=0, microsecond=0):
            return False

        starts_at_old_admission = old_start is not None and first_start == old_start
        starts_after_new_admission = first_start > new_start and not self._status_covers_time(
            cursor,
            admission_id,
            new_start,
        )
        if not starts_at_old_admission and not starts_after_new_admission:
            return False

        old_text = first_start.isoformat()
        new_text = new_start.isoformat()
        now_text = self._now_text()
        cursor.execute(
            """
            UPDATE patient_status_events
            SET start_time = ?,
                created_at = CASE
                    WHEN created_at IS NULL OR datetime(created_at) = datetime(?) THEN ?
                    ELSE created_at
                END,
                updated_at = ?,
                last_modified_by = ?,
                revision = COALESCE(revision, 0) + 1
            WHERE id = ?
            """,
            (new_text, old_text, new_text, now_text, "SYSTEM", int(first["id"])),
        )
        return cursor.rowcount == 1

    def _ensure_status_for_existing_card(self, cursor, admission_id: int, admission_start: datetime) -> None:
        if not self._table_exists(cursor, "patient_status_events"):
            return
        if self._status_covers_time(cursor, admission_id, admission_start):
            return

        cursor.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM patient_status_events
            WHERE admission_id = ?
            """,
            (int(admission_id),),
        )
        row = cursor.fetchone()
        if row and int(row["cnt"] or 0) > 0:
            return
        self._insert_initial_active_status(cursor, admission_id, admission_start)

    def _relocate_empty_admission_vital(
        self,
        cursor,
        admission_id: int,
        old_start: Optional[datetime],
        new_start: datetime,
    ) -> None:
        if old_start is None or old_start == new_start or not self._table_exists(cursor, "vitals"):
            return

        columns = self._table_columns(cursor, "vitals")
        value_columns = [name for name in ("sys", "dia", "pulse", "temp", "spo2", "rr", "cvp", "gcs") if name in columns]
        if not value_columns:
            return

        empty_condition = " AND ".join(f"{name} IS NULL" for name in value_columns)
        old_minute = old_start.strftime("%Y-%m-%d %H:%M")
        new_minute = new_start.strftime("%Y-%m-%d %H:%M")
        cursor.execute(
            f"""
            SELECT id
            FROM vitals
            WHERE admission_id = ?
              AND strftime('%Y-%m-%d %H:%M', datetime) = ?
              AND {empty_condition}
            ORDER BY id ASC
            LIMIT 1
            """,
            (int(admission_id), old_minute),
        )
        old_vital = cursor.fetchone()
        if not old_vital:
            return

        cursor.execute(
            """
            SELECT id
            FROM vitals
            WHERE admission_id = ?
              AND strftime('%Y-%m-%d %H:%M', datetime) = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (int(admission_id), new_minute),
        )
        existing_new = cursor.fetchone()
        if existing_new and int(existing_new["id"]) != int(old_vital["id"]):
            cursor.execute("DELETE FROM vitals WHERE id = ?", (int(old_vital["id"]),))
            return

        cursor.execute(
            """
            UPDATE vitals
            SET datetime = ?,
                updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now'),
                last_modified_by = COALESCE(last_modified_by, ?),
                revision = COALESCE(revision, 0) + 1
            WHERE id = ?
            """,
            (new_start.isoformat(), "SYSTEM", int(old_vital["id"])),
        )

    def _has_any_card_record(self, cursor, admission_id: int) -> bool:
        checks = (
            ("vitals", "admission_id"),
            ("fluids", "admission_id"),
            ("orders", "admission_id"),
            ("lab_orders", "admission_id"),
            ("diet_plan_versions", "admission_id"),
            ("oral_intake_events", "admission_id"),
        )
        for table_name, column_name in checks:
            if not self._table_exists(cursor, table_name):
                continue
            if column_name not in self._table_columns(cursor, table_name):
                continue
            cursor.execute(
                f'SELECT 1 FROM "{table_name}" WHERE "{column_name}" = ? LIMIT 1',
                (int(admission_id),),
            )
            if cursor.fetchone():
                return True
        return False

    def _status_covers_time(self, cursor, admission_id: int, timestamp: datetime) -> bool:
        cursor.execute(
            """
            SELECT 1
            FROM patient_status_events
            WHERE admission_id = ?
              AND datetime(start_time) <= datetime(?)
              AND (end_time IS NULL OR datetime(end_time) >= datetime(?))
            LIMIT 1
            """,
            (int(admission_id), timestamp.isoformat(), timestamp.isoformat()),
        )
        return bool(cursor.fetchone())

    @staticmethod
    def _table_exists(cursor, table_name: str) -> bool:
        cursor.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
            LIMIT 1
            """,
            (table_name,),
        )
        return bool(cursor.fetchone())

    @staticmethod
    def _table_columns(cursor, table_name: str) -> set[str]:
        cursor.execute(f'PRAGMA table_info("{table_name}")')
        return {str(row["name"] if hasattr(row, "keys") else row[1]) for row in cursor.fetchall()}

    def _ensure_recovery_release_card(self, cursor, admission_id: int, admission_row) -> None:
        active_start = self._first_active_status_start(cursor, admission_id)
        if active_start is None:
            admission_dt = admission_row["admission_datetime"] if admission_row else None
            active_start = self._parse_dt(admission_dt) or datetime.now()
            self._insert_initial_active_status(cursor, admission_id, active_start)

        active_start = active_start.replace(second=0, microsecond=0)
        shift_start, shift_end = ShiftService.get_day_period(active_start)
        if self._has_any_card_record_in_shift(cursor, admission_id, shift_start, shift_end):
            return

        cursor.execute(
            """
            INSERT INTO vitals (admission_id, datetime, last_modified_by, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                int(admission_id),
                active_start.isoformat(),
                "SYSTEM",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:23],
            ),
        )

    def _first_active_status_start(self, cursor, admission_id: int) -> Optional[datetime]:
        row = cursor.execute(
            """
            SELECT start_time
            FROM patient_status_events
            WHERE admission_id = ?
              AND status = ?
            ORDER BY datetime(start_time) ASC, id ASC
            LIMIT 1
            """,
            (int(admission_id), PatientStatus.ACTIVE.value),
        ).fetchone()
        if not row:
            return None
        return self._parse_dt(row["start_time"])

    @staticmethod
    def _has_any_card_record_in_shift(cursor, admission_id: int, shift_start: datetime, shift_end: datetime) -> bool:
        admission_id = int(admission_id)
        start_iso = shift_start.isoformat()
        end_iso = shift_end.isoformat()
        start_min = shift_start.isoformat(timespec="minutes").replace("T", " ")
        end_min = shift_end.isoformat(timespec="minutes").replace("T", " ")
        row = cursor.execute(
            """
            SELECT 1
            WHERE EXISTS (
                SELECT 1 FROM vitals
                WHERE admission_id = ? AND DATETIME(datetime) >= DATETIME(?) AND DATETIME(datetime) < DATETIME(?)
            )
            OR EXISTS (
                SELECT 1 FROM fluids
                WHERE admission_id = ? AND DATETIME(datetime) >= DATETIME(?) AND DATETIME(datetime) < DATETIME(?)
            )
            OR EXISTS (
                SELECT 1 FROM orders
                WHERE admission_id = ? AND DATETIME(datetime) >= DATETIME(?) AND DATETIME(datetime) < DATETIME(?)
            )
            OR EXISTS (
                SELECT 1 FROM diet_plan
                WHERE admission_id = ? AND DATETIME(shift_start) >= DATETIME(?) AND DATETIME(shift_start) < DATETIME(?)
            )
            OR EXISTS (
                SELECT 1 FROM oral_intake_events
                WHERE admission_id = ? AND DATETIME(event_time) >= DATETIME(?) AND DATETIME(event_time) < DATETIME(?)
            )
            LIMIT 1
            """,
            (
                admission_id,
                start_iso,
                end_iso,
                admission_id,
                start_iso,
                end_iso,
                admission_id,
                start_iso,
                end_iso,
                admission_id,
                start_min,
                end_min,
                admission_id,
                start_min,
                end_min,
            ),
        ).fetchone()
        return bool(row)

    @staticmethod
    def _safe_int_or_none(value):
        if value is None or value == "":
            return None
        try:
            return int(value)
        except Exception:
            return None

    @staticmethod
    def _parse_dt(value) -> Optional[datetime]:
        if value is None or isinstance(value, datetime):
            return value
        text = str(value).strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None

    @staticmethod
    def _parse_date(value) -> Optional[date]:
        return parse_date_value(value)

    @staticmethod
    def _to_sql_dt(value) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.replace(microsecond=0).isoformat(sep=" ")
        return str(value)

    @staticmethod
    def _to_sql_date(value) -> Optional[str]:
        parsed = parse_date_value(value)
        return parsed.isoformat() if parsed else None

    @staticmethod
    def _now_text() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:23]
