from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional

from rem_card.services.concurrency import assert_revision_matches


@dataclass
class PatientRecord:
    id: int
    full_name: str
    admission_uid: Optional[str] = None


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
        if not row:
            return None, None
        data = dict(row)
        patient = PatientRecord(
            id=int(data["p_id"]),
            full_name=str(data.get("full_name") or ""),
            admission_uid=data.get("admission_uid"),
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
        if not full_name:
            raise ValueError("ФИО пациента не заполнено.")

        def operation(cursor):
            cursor.execute(
                "INSERT INTO patients (full_name, admission_uid) VALUES (?, ?)",
                (full_name, admission_uid),
            )
            patient_id = int(cursor.lastrowid)
            now = self._now_text()
            cursor.execute(
                """
                INSERT INTO admissions (
                    patient_id, bed_number, history_number, admission_datetime,
                    patient_age, patient_months, patient_age_unit, patient_gender,
                    diagnosis_code, diagnosis_text, department_profile, source_department,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    patient_id,
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
                    now,
                    now,
                ),
            )
            admission_id = int(cursor.lastrowid)
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
                ("OCCUPIED", admission_id, int(admission_data["bed_number"])),
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
        if not full_name:
            raise ValueError("ФИО пациента не заполнено.")

        def operation(cursor):
            cursor.execute("UPDATE patients SET full_name = ? WHERE id = ?", (full_name, int(patient_id)))
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
            if not source or source["status"] == "FREE" or source["current_admission_id"] is None:
                return False
            assert_revision_matches(source["revision"] if "revision" in source.keys() else 0, expected_source_bed_revision)
            if target:
                assert_revision_matches(target["revision"] if "revision" in target.keys() else 0, expected_target_bed_revision)

            source_admission_id = int(source["current_admission_id"])
            source_admission = cursor.execute(
                "SELECT COALESCE(revision, 0) AS revision FROM admissions WHERE id = ?",
                (source_admission_id,),
            ).fetchone()
            assert_revision_matches(
                source_admission["revision"] if source_admission else 0,
                expected_source_admission_revision,
            )
            if target and target["status"] != "FREE" and target["current_admission_id"] is not None:
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
                    "UPDATE admissions SET bed_number = ?, updated_at = ?, revision = COALESCE(revision, 0) + 1 WHERE id = ?",
                    (target_bed, self._now_text(), source_admission_id),
                )
                cursor.execute(
                    "UPDATE admissions SET bed_number = ?, updated_at = ?, revision = COALESCE(revision, 0) + 1 WHERE id = ?",
                    (source_bed, self._now_text(), target_admission_id),
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
                    "UPDATE admissions SET bed_number = ?, updated_at = ?, revision = COALESCE(revision, 0) + 1 WHERE id = ?",
                    (target_bed, self._now_text(), source_admission_id),
                )
                cursor.execute(
                    "UPDATE beds SET current_admission_id = ?, status = 'OCCUPIED', revision = COALESCE(revision, 0) + 1 WHERE bed_number = ?",
                    (source_admission_id, target_bed),
                )
            return True

        return self.db.run_write_operation(operation, source="patient_bed_move_patient")

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
    def _to_sql_dt(value) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.replace(microsecond=0).isoformat(sep=" ")
        return str(value)

    @staticmethod
    def _now_text() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:23]
