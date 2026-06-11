from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Optional

from rem_card.app.patient_age import parse_date_value
from rem_card.services.concurrency import assert_revision_matches
from rem_card.services.patient_bed_management.recovery_beds import is_recovery_bed_number
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
                p.full_name,
                a.history_number,
                a.diagnosis_text,
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

    def get_patient_with_current_admission(self, bed_number: int) -> tuple[Optional[PatientRecord], Optional[AdmissionRecord]]:
        row = self.db.fetch_one_remcard(
            """
            SELECT
                p.id AS p_id,
                p.full_name,
                p.admission_uid,
                p.birth_date,
                a.*
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

    def get_patient_with_admission(self, admission_id: int) -> tuple[Optional[PatientRecord], Optional[AdmissionRecord]]:
        row = self.db.fetch_one_remcard(
            """
            SELECT
                p.id AS p_id,
                p.full_name,
                p.admission_uid,
                p.birth_date,
                a.*
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
                    int(admission_data["bed_number"]),
                    str(admission_data.get("history_number") or "").strip(),
                    self._to_sql_dt(admission_data.get("admission_datetime")),
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
            target = cursor.execute("SELECT * FROM beds WHERE bed_number = ?", (target_bed,)).fetchone()
            if not source or not target or source["status"] == "FREE" or source["current_admission_id"] is None:
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
                WHERE admission_id = ? AND datetime >= ? AND datetime < ?
            )
            OR EXISTS (
                SELECT 1 FROM fluids
                WHERE admission_id = ? AND datetime >= ? AND datetime < ?
            )
            OR EXISTS (
                SELECT 1 FROM orders
                WHERE admission_id = ? AND datetime >= ? AND datetime < ?
            )
            OR EXISTS (
                SELECT 1 FROM diet_plan
                WHERE admission_id = ? AND shift_start >= ? AND shift_start < ?
            )
            OR EXISTS (
                SELECT 1 FROM oral_intake_events
                WHERE admission_id = ? AND event_time >= ? AND event_time < ?
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
