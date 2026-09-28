from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional
from rem_card.app.logger import logger
from rem_card.app.patient_age import parse_date_value, storage_age_from_birth_date
from rem_card.data.dto.remcard_dto import PatientStatus
from rem_card.services.operblock_anesthesia_types import normalize_operblock_anesthesia_type_label
from rem_card.services.operblock_handoff_service import (
    HANDOFF_ACCEPTED,
    normalize_handoff_full_name,
    normalize_handoff_history_number,
)
from rem_card.services.patient_departments import normalize_profile_department
from rem_card.services.patient_bed_management.recovery_beds import RECOVERY_BED_TRANSFER_ORDER
from .common import (
    OperBlockConflictError,
    normalize_operblock_mkb_code,
    _now_text,
    _minute_floor,
    _normalize_stage_text_list,
    _build_stage_intervals,
    _split_name,
    _normalize_case_text,
    _surgeons_from_json,
    _row_to_dict,
    _sqlite_columns,
)


class OperBlockHandoffMixin:
    def _bind_waiting_handoff_to_case(
        self,
        cursor: sqlite3.Cursor,
        case: dict[str, Any],
        handoff_id: int,
    ) -> dict[str, Any]:
        if case.get("handoff_id") is not None or case.get("source_rao_admission_id") is not None:
            if int(case.get("handoff_id") or 0) == int(handoff_id):
                return case
            raise OperBlockConflictError("Операционный случай уже связан с другой картой РАО.")
        identity = cursor.execute(
            """
            SELECT a.history_number, p.full_name, p.birth_date
            FROM admissions a
            JOIN patients p ON p.id = a.patient_id
            WHERE a.id = ?
            """,
            (int(case["admission_id"]),),
        ).fetchone()
        handoff = cursor.execute(
            """
            SELECT
                h.id, h.source_admission_id, h.patient_snapshot_json,
                a.history_number, p.full_name, p.birth_date
            FROM operblock_handoffs h
            JOIN admissions a ON a.id = h.source_admission_id
            JOIN patients p ON p.id = a.patient_id
            JOIN patient_status_events pse
              ON pse.admission_id = h.source_admission_id
             AND pse.end_time IS NULL
             AND pse.status = 'OR'
            JOIN beds b
              ON b.current_admission_id = h.source_admission_id
             AND b.status = 'OCCUPIED'
            WHERE h.id = ? AND h.status = 'waiting'
            LIMIT 1
            """,
            (int(handoff_id),),
        ).fetchone()
        if not identity or not handoff:
            raise OperBlockConflictError(
                "Пациент уже выбран другим рабочим местом или его движение в РАО изменилось."
            )
        identity_matches = (
            normalize_handoff_history_number(identity["history_number"])
            == normalize_handoff_history_number(handoff["history_number"])
            and normalize_handoff_full_name(identity["full_name"])
            == normalize_handoff_full_name(handoff["full_name"])
            and str(identity["birth_date"] or "") == str(handoff["birth_date"] or "")
        )
        if not identity_matches:
            raise OperBlockConflictError(
                "Идентификационные данные операционной карты и карты РАО различаются."
            )
        now = _now_text()
        cursor.execute(
            """
            UPDATE operation_cases
            SET handoff_id = ?,
                source_rao_admission_id = ?,
                last_modified_by = 'operblock',
                revision = COALESCE(revision, 0) + 1
            WHERE id = ?
              AND handoff_id IS NULL
              AND source_rao_admission_id IS NULL
            """,
            (int(handoff_id), int(handoff["source_admission_id"]), int(case["operation_case_id"])),
        )
        if cursor.rowcount != 1:
            raise OperBlockConflictError("Операционный случай уже связан другим пользователем.")
        cursor.execute(
            """
            UPDATE operblock_handoffs
            SET status = ?,
                operation_case_id = ?,
                accepted_table_code = ?,
                accepted_at = ?,
                last_modified_by = 'operblock',
                revision = COALESCE(revision, 0) + 1
            WHERE id = ? AND status = 'waiting'
            """,
            (
                HANDOFF_ACCEPTED,
                int(case["operation_case_id"]),
                str(case.get("table_code") or ""),
                now,
                int(handoff_id),
            ),
        )
        if cursor.rowcount != 1:
            raise OperBlockConflictError("Пациент уже выбран другим рабочим местом.")
        result = dict(case)
        result["handoff_id"] = int(handoff_id)
        result["source_rao_admission_id"] = int(handoff["source_admission_id"])
        return result

    def _maybe_create_rao_recovery_admission(
        self,
        cursor: sqlite3.Cursor,
        case: dict[str, Any],
        event_dt: datetime,
    ) -> Optional[int]:
        savepoint = "operblock_rao_auto_admission"
        cursor.execute(f"SAVEPOINT {savepoint}")
        try:
            admission_id = self._create_rao_recovery_admission(cursor, case, event_dt)
        except Exception as exc:
            try:
                cursor.execute(f"ROLLBACK TO {savepoint}")
            except Exception:
                logger.error(
                    "operblock_rao_auto_admission_rollback_failed case_id=%s",
                    case.get("operation_case_id"),
                    exc_info=True,
                )
            try:
                cursor.execute(f"RELEASE {savepoint}")
            except Exception:
                logger.error(
                    "operblock_rao_auto_admission_release_after_rollback_failed case_id=%s",
                    case.get("operation_case_id"),
                    exc_info=True,
                )
            logger.error(
                "operblock_rao_auto_admission_failed case_id=%s source_admission_id=%s: %s",
                case.get("operation_case_id"),
                case.get("admission_id"),
                exc,
                exc_info=True,
            )
            return None

        try:
            cursor.execute(f"RELEASE {savepoint}")
        except Exception:
            logger.error(
                "operblock_rao_auto_admission_release_failed case_id=%s rao_admission_id=%s",
                case.get("operation_case_id"),
                admission_id,
                exc_info=True,
            )
            return None
        return admission_id

    def _create_rao_recovery_admission(
        self,
        cursor: sqlite3.Cursor,
        case: dict[str, Any],
        event_dt: datetime,
    ) -> Optional[int]:
        operation_case_id = int(case["operation_case_id"])
        source = self._fetch_rao_transfer_source_data(cursor, operation_case_id)
        if not source:
            raise OperBlockConflictError("Не удалось найти данные операции для автоперевода в РАО.")

        existing_rao_admission_id = source.get("future_rao_admission_id")
        if existing_rao_admission_id:
            logger.info(
                "operblock_rao_auto_admission_skipped_existing case_id=%s rao_admission_id=%s",
                operation_case_id,
                existing_rao_admission_id,
            )
            return None

        admission_dt = _minute_floor(event_dt + timedelta(minutes=10))
        admission_data = self._prepare_rao_recovery_admission_data(
            operation_case_id,
            source,
            event_dt,
            admission_dt,
        )
        if admission_data is None:
            return None

        bed_number = self._select_free_recovery_bed_for_rao(cursor)
        if bed_number is None:
            logger.info(
                "operblock_rao_auto_admission_skipped_no_free_recovery_bed case_id=%s source_admission_id=%s",
                operation_case_id,
                source.get("source_admission_id"),
            )
            return None

        admission_id = self._insert_rao_recovery_patient_admission(cursor, admission_data, bed_number)
        self._occupy_rao_recovery_bed(cursor, admission_id, bed_number)
        self._insert_rao_recovery_status_event(cursor, admission_id, admission_data["admission_dt_text"])
        self._copy_latest_operblock_vitals_to_rao(
            cursor,
            int(source["source_admission_id"]),
            admission_id,
            event_dt,
            admission_dt,
        )
        if "future_rao_admission_id" in _sqlite_columns(cursor.connection, "operation_cases"):
            cursor.execute(
                """
                UPDATE operation_cases
                SET future_rao_admission_id = ?,
                    last_modified_by = 'operblock',
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ?
                """,
                (admission_id, operation_case_id),
            )
        logger.info(
            "operblock_rao_auto_admission_created case_id=%s source_admission_id=%s rao_admission_id=%s bed=%s",
            operation_case_id,
            source.get("source_admission_id"),
            admission_id,
            bed_number,
        )
        return admission_id

    def _prepare_rao_recovery_admission_data(
        self,
        operation_case_id: int,
        source: dict[str, Any],
        event_dt: datetime,
        admission_dt: datetime,
    ) -> Optional[dict[str, Any]]:
        history_number = str(source.get("history_number") or "").strip()
        full_name = _normalize_case_text(source.get("full_name"))
        diagnosis_text = _normalize_case_text(source.get("diagnosis_text"))
        birth_date = parse_date_value(source.get("birth_date"))
        missing_fields: list[str] = []
        if not history_number:
            missing_fields.append("history_number")
        if not full_name:
            missing_fields.append("full_name")
        if birth_date is None:
            missing_fields.append("birth_date")
        elif birth_date > admission_dt.date():
            missing_fields.append("birth_date_after_admission")
        if not diagnosis_text:
            missing_fields.append("diagnosis_text")
        if missing_fields:
            logger.error(
                "operblock_rao_auto_admission_required_field_missing case_id=%s source_admission_id=%s missing=%s",
                operation_case_id,
                source.get("source_admission_id"),
                ",".join(missing_fields),
            )
            return None

        assert birth_date is not None
        operation_name = _normalize_case_text(source.get("operation_name"))
        intake_extra_json = json.dumps(
            {
                "source": "operblock_rao_transfer",
                "operation_case_id": int(operation_case_id),
                "source_patient_id": source.get("source_patient_id"),
                "source_admission_id": source.get("source_admission_id"),
                "table_code": source.get("table_code") or "",
                "operation_finished_at": _minute_floor(event_dt).isoformat(timespec="seconds"),
                "transfer_department": "РАО",
                "operation_name": operation_name,
                "anesthesia_assistance_type": normalize_operblock_anesthesia_type_label(
                    source.get("anesthesia_assistance_type")
                ),
                "surgeons": source.get("surgeons") or [],
                "anesthesiologist": source.get("anesthesiologist") or "",
                "anesthetist": source.get("anesthetist") or "",
                "height_cm": source.get("height_cm"),
                "weight_kg": source.get("weight_kg"),
                "allergies": source.get("allergies") or "",
                "blood_group": source.get("blood_group") or "",
                "blood_rh": source.get("blood_rh") or "",
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        age = storage_age_from_birth_date(birth_date, admission_dt)
        return {
            "admission_dt_text": admission_dt.isoformat(timespec="seconds"),
            "birth_date": birth_date,
            "department_profile": normalize_profile_department(source.get("department_profile")) or None,
            "diagnosis_code": normalize_operblock_mkb_code(source.get("diagnosis_code") or "") or None,
            "diagnosis_text": diagnosis_text,
            "full_name": full_name,
            "history_number": history_number,
            "intake_extra_json": intake_extra_json,
            "operation_name": operation_name,
            "patient_age": age["patient_age"],
            "patient_age_unit": age["patient_age_unit"],
            "patient_gender": source.get("patient_gender") or None,
            "patient_months": age["patient_months"],
        }

    @staticmethod
    def _insert_rao_recovery_patient_admission(
        cursor: sqlite3.Cursor,
        admission_data: dict[str, Any],
        bed_number: int,
    ) -> int:
        full_name = str(admission_data["full_name"])
        birth_date = admission_data["birth_date"]
        last_name, first_name, middle_name = _split_name(full_name)
        admission_uid = str(uuid.uuid4())
        now = _now_text()
        cursor.execute(
            """
            INSERT INTO patients (
                full_name, admission_uid, birth_date, last_name, first_name, middle_name
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (full_name, admission_uid, birth_date.isoformat(), last_name, first_name, middle_name),
        )
        patient_id = int(cursor.lastrowid)
        cursor.execute(
            """
            INSERT INTO admissions (
                patient_id, bed_number, history_number, admission_datetime,
                patient_age, patient_months, patient_age_unit, patient_gender,
                diagnosis_code, diagnosis_text, department_profile, source_department,
                operation_description, intake_extra_json, recovery_bed_stay,
                created_at, updated_at, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 1)
            """,
            (
                patient_id,
                int(bed_number),
                admission_data["history_number"],
                admission_data["admission_dt_text"],
                admission_data["patient_age"],
                admission_data["patient_months"],
                admission_data["patient_age_unit"],
                admission_data["patient_gender"],
                admission_data["diagnosis_code"],
                admission_data["diagnosis_text"],
                admission_data["department_profile"],
                "Профильное отделение",
                admission_data["operation_name"] or None,
                admission_data["intake_extra_json"],
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _occupy_rao_recovery_bed(cursor: sqlite3.Cursor, admission_id: int, bed_number: int) -> None:
        cursor.execute(
            """
            UPDATE beds
            SET status = 'OCCUPIED',
                current_admission_id = ?,
                revision = COALESCE(revision, 0) + 1
            WHERE bed_number = ?
              AND status = 'FREE'
              AND current_admission_id IS NULL
            """,
            (int(admission_id), int(bed_number)),
        )
        if cursor.rowcount != 1:
            raise OperBlockConflictError("Свободная койка пробуждения была занята до автоперевода.")

    @staticmethod
    def _insert_rao_recovery_status_event(
        cursor: sqlite3.Cursor,
        admission_id: int,
        admission_dt_text: str,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO patient_status_events (
                admission_id, status, reason_type, reason_text, start_time,
                created_by, created_at, updated_at, last_modified_by
            ) VALUES (?, ?, 'operblock_rao_transfer', 'Поступил после операции из оперблока',
                      ?, 'operblock', ?, ?, 'operblock')
            """,
            (
                int(admission_id),
                PatientStatus.ACTIVE.value,
                admission_dt_text,
                admission_dt_text,
                admission_dt_text,
            ),
        )

    def _fetch_rao_transfer_source_data(
        self,
        cursor: sqlite3.Cursor,
        operation_case_id: int,
    ) -> dict[str, Any]:
        case_columns = _sqlite_columns(cursor.connection, "operation_cases")
        future_expr = "oc.future_rao_admission_id" if "future_rao_admission_id" in case_columns else "NULL"
        row = cursor.execute(
            f"""
            SELECT
                oc.id AS operation_case_id,
                oc.patient_id AS source_patient_id,
                oc.admission_id AS source_admission_id,
                oc.table_code,
                oc.started_at,
                oc.ended_at,
                {future_expr} AS future_rao_admission_id,
                oc.planned_operation_name,
                oc.planned_anesthesia_assistance_type,
                oc.planned_surgeons_json,
                oc.planned_operating_nurse,
                oc.planned_anesthesiologist,
                oc.planned_anesthetist,
                oc.height_cm,
                oc.weight_kg,
                oc.allergies,
                oc.blood_group,
                oc.blood_rh,
                p.full_name,
                p.birth_date,
                a.history_number,
                a.patient_gender,
                a.diagnosis_code,
                a.diagnosis_text,
                a.department_profile
            FROM operation_cases oc
            JOIN admissions a ON a.id = oc.admission_id
            JOIN patients p ON p.id = oc.patient_id
            WHERE oc.id = ?
            """,
            (int(operation_case_id),),
        ).fetchone()
        if not row:
            return {}

        data = _row_to_dict(row)
        stage_rows = self._fetch_stage_rows_for_case(cursor, int(operation_case_id))
        stage_state = _build_stage_intervals(stage_rows)
        data["operation_name"] = _normalize_case_text(
            stage_state.get("current_operation_name")
            or stage_state.get("last_operation_name")
            or stage_state.get("first_operation_name")
            or data.get("planned_operation_name")
        )
        data["anesthesia_assistance_type"] = normalize_operblock_anesthesia_type_label(
            stage_state.get("current_anesthesia_assistance_type")
            or stage_state.get("last_anesthesia_assistance_type")
            or stage_state.get("first_anesthesia_assistance_type")
            or data.get("planned_anesthesia_assistance_type")
        )
        data["surgeons"] = _normalize_stage_text_list(
            stage_state.get("current_surgeons")
            or stage_state.get("last_surgeons")
            or stage_state.get("first_surgeons")
            or _surgeons_from_json(data.get("planned_surgeons_json")),
            split_commas=True,
        )
        data["operating_nurse"] = _normalize_case_text(
            stage_state.get("current_operating_nurse")
            or stage_state.get("last_operating_nurse")
            or stage_state.get("first_operating_nurse")
            or data.get("planned_operating_nurse")
        )
        data["anesthesiologist"] = _normalize_case_text(
            stage_state.get("current_anesthesiologist")
            or stage_state.get("last_anesthesiologist")
            or stage_state.get("first_anesthesiologist")
            or data.get("planned_anesthesiologist")
        )
        data["anesthetist"] = _normalize_case_text(
            stage_state.get("current_anesthetist")
            or stage_state.get("last_anesthetist")
            or stage_state.get("first_anesthetist")
            or data.get("planned_anesthetist")
        )
        return data

    @staticmethod
    def _select_free_recovery_bed_for_rao(cursor: sqlite3.Cursor) -> Optional[int]:
        transfer_order = tuple(int(bed_number) for bed_number in RECOVERY_BED_TRANSFER_ORDER)
        insert_placeholders = ", ".join("(?, 'FREE', NULL, 0)" for _ in transfer_order)
        cursor.execute(
            f"""
            INSERT OR IGNORE INTO beds (bed_number, status, current_admission_id, revision)
            VALUES {insert_placeholders}
            """,
            transfer_order,
        )

        ordered_values = ", ".join("(?, ?)" for _ in transfer_order)
        ordered_params: list[int] = []
        for sort_order, bed_number in enumerate(transfer_order):
            ordered_params.extend((bed_number, sort_order))
        row = cursor.execute(
            f"""
            WITH desired_beds(bed_number, sort_order) AS (VALUES {ordered_values})
            SELECT bed_number
            FROM desired_beds
            JOIN beds USING (bed_number)
            WHERE beds.status = 'FREE'
              AND beds.current_admission_id IS NULL
            ORDER BY desired_beds.sort_order ASC
            LIMIT 1
            """,
            tuple(ordered_params),
        ).fetchone()
        return int(row["bed_number"]) if row else None

    @staticmethod
    def _copy_latest_operblock_vitals_to_rao(
        cursor: sqlite3.Cursor,
        source_admission_id: int,
        rao_admission_id: int,
        event_dt: datetime,
        admission_dt: datetime,
    ) -> int:
        event_dt_text = _minute_floor(event_dt).isoformat(timespec="seconds")
        row = cursor.execute(
            """
            SELECT sys, dia, pulse, temp, spo2, rr, cvp
            FROM vitals
            WHERE admission_id = ?
              AND DATETIME("datetime") <= DATETIME(?)
              AND (
                  sys IS NOT NULL OR dia IS NOT NULL OR pulse IS NOT NULL OR temp IS NOT NULL
                  OR spo2 IS NOT NULL OR rr IS NOT NULL OR cvp IS NOT NULL
              )
            ORDER BY DATETIME("datetime") DESC, id DESC
            LIMIT 1
            """,
            (int(source_admission_id), event_dt_text),
        ).fetchone()
        vitals = _row_to_dict(row)
        cursor.execute(
            """
            INSERT INTO vitals (
                admission_id, datetime, sys, dia, pulse, temp, spo2, rr, cvp,
                last_modified_by, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'operblock', STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
            """,
            (
                int(rao_admission_id),
                _minute_floor(admission_dt).isoformat(timespec="seconds"),
                vitals.get("sys"),
                vitals.get("dia"),
                vitals.get("pulse"),
                vitals.get("temp"),
                vitals.get("spo2"),
                vitals.get("rr"),
                vitals.get("cvp"),
            ),
        )
        return int(cursor.lastrowid)
