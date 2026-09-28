from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional
from rem_card.app.patient_age import storage_age_from_birth_date
from rem_card.data.dto.remcard_dto import PatientStatus
from rem_card.services.concurrency import DATA_CONFLICT_MESSAGE, DataConflictError, assert_revision_matches
from rem_card.services.operblock_anesthesia_types import normalize_operblock_anesthesia_type_label
from rem_card.services.operblock_handoff_service import HANDOFF_ACCEPTED
from rem_card.services.patient_departments import normalize_profile_department
from .common import (
    OPERBLOCK_ROLE,
    OperBlockConflictError,
    OperBlockPatientInput,
    normalize_operblock_history_number,
    normalize_operblock_mkb_code,
    is_complete_operblock_mkb_code,
    validate_operblock_runtime_path,
    _now_text,
    _parse_dt,
    _minute_floor,
    _normalize_stage_text_list,
    _build_stage_intervals,
    _split_name,
    _to_birth_date,
    _normalize_case_text,
    _surgeons_json,
    _surgeons_from_json,
    _case_input_from_payload,
    _validate_case_vitals,
    _has_case_vitals,
    _row_to_dict,
)


class OperBlockCaseLifecycleMixin:
    def create_operation_case(self, data: OperBlockPatientInput | dict[str, Any]) -> dict[str, int]:
        validate_operblock_runtime_path(self.db)
        data = _case_input_from_payload(data)
        _validate_case_vitals(data)

        table_code = self._validate_table_code(data.table_code)
        history_number = normalize_operblock_history_number(data.history_number)
        full_name = str(data.full_name or "").strip()
        if not full_name:
            raise ValueError("ФИО пациента не заполнено.")
        diagnosis_code = normalize_operblock_mkb_code(data.diagnosis_code or "")
        if diagnosis_code and not is_complete_operblock_mkb_code(diagnosis_code):
            raise ValueError("Код МКБ-10 должен быть в формате X33, S82.0 или S82.01.")
        diagnosis_text = str(data.diagnosis_text or "").strip()
        if not diagnosis_text:
            raise ValueError("Диагноз не заполнен.")
        department_profile = normalize_profile_department(data.department_profile)

        birth_date = _to_birth_date(data.birth_date)
        now = _now_text()
        started_dt = _minute_floor(data.started_at or datetime.now())
        current_minute = _minute_floor(datetime.now())
        if data.handoff_id is None and started_dt > current_minute + timedelta(minutes=1):
            raise ValueError("Время поступления в оперблок не может быть позже текущего времени.")
        started_text = started_dt.isoformat(timespec="seconds")
        age = storage_age_from_birth_date(birth_date, started_dt)
        last_name, first_name, middle_name = _split_name(full_name)
        admission_uid = str(uuid.uuid4())
        bed_number = 0
        offline_case_uuid = self._new_case_uuid()
        offline_session_id = self._offline_session_id() if self._is_opblock_offline_runtime() else None

        def operation(cursor: sqlite3.Cursor):
            case_started_text = started_text
            case_age = age
            handoff = None
            if data.handoff_id is not None:
                handoff = cursor.execute(
                    """
                    SELECT h.*, pse.status AS current_source_status
                    FROM operblock_handoffs h
                    JOIN patient_status_events pse
                      ON pse.admission_id = h.source_admission_id
                     AND pse.end_time IS NULL
                    JOIN beds b
                      ON b.current_admission_id = h.source_admission_id
                     AND b.status = 'OCCUPIED'
                    WHERE h.id = ?
                      AND h.status = 'waiting'
                    LIMIT 1
                    """,
                    (int(data.handoff_id),),
                ).fetchone()
                if not handoff:
                    raise OperBlockConflictError(
                        "Пациент уже выбран другим рабочим местом или больше не ожидает операционную."
                    )
                if str(handoff["current_source_status"] or "") != PatientStatus.OR.value:
                    raise OperBlockConflictError(
                        "Движение пациента в РАО уже изменилось. Обновите очередь."
                    )
                source_admission_id = int(handoff["source_admission_id"])
                if (
                    data.source_rao_admission_id is not None
                    and int(data.source_rao_admission_id) != source_admission_id
                ):
                    raise OperBlockConflictError("Связь с исходной картой РАО изменилась.")
                expected_arrival = _parse_dt(handoff["expected_arrival_at"])
                if expected_arrival is None:
                    raise OperBlockConflictError("Не удалось определить время поступления из РАО.")
                dispatched_at = _parse_dt(handoff["dispatched_at"])
                if dispatched_at is None:
                    raise OperBlockConflictError("Не удалось определить время отправки пациента из РАО.")
                dispatched_at = _minute_floor(dispatched_at)
                self._validate_started_at_not_before_rao_dispatch(started_dt, dispatched_at)
                latest_allowed = max(
                    _minute_floor(expected_arrival),
                    _minute_floor(datetime.now()) + timedelta(minutes=1),
                )
                if started_dt > latest_allowed:
                    raise ValueError("Время поступления в оперблок не может быть позже текущего времени.")
                case_started_text = started_text
                case_age = storage_age_from_birth_date(birth_date, started_dt)

            existing = cursor.execute(
                "SELECT id FROM operation_cases WHERE table_code = ? AND status = 'active' LIMIT 1",
                (table_code,),
            ).fetchone()
            if existing:
                raise OperBlockConflictError("Операционный стол уже занят другим пользователем.")

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
                    created_at, updated_at, unit_scope, admission_type, is_active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    patient_id,
                    bed_number,
                    history_number,
                    case_started_text,
                    case_age["patient_age"],
                    case_age["patient_months"],
                    case_age["patient_age_unit"],
                    data.gender,
                    diagnosis_code or None,
                    diagnosis_text,
                    department_profile or None,
                    "Оперблок",
                    now,
                    now,
                    OPERBLOCK_ROLE,
                    OPERBLOCK_ROLE,
                ),
            )
            admission_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO operation_cases (
                    patient_id, admission_id, table_code, status, created_at, started_at,
                    created_by_role, created_by_client_id, last_modified_by,
                    planned_operation_name, planned_anesthesia_assistance_type,
                    planned_surgeons_json, planned_operating_nurse,
                    planned_anesthesiologist, planned_anesthetist,
                    height_cm, weight_kg, allergies, blood_group, blood_rh,
                    preop_sys, preop_dia, preop_pulse, preop_spo2, preop_save_initial_vitals,
                    offline_case_uuid, offline_session_id, migration_status
                ) VALUES (?, ?, ?, 'active', ?, ?, 'operblock', ?, 'operblock',
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    patient_id,
                    admission_id,
                    table_code,
                    now,
                    case_started_text,
                    self.client_id,
                    data.operation_name or None,
                    data.anesthesia_assistance_type or None,
                    _surgeons_json(data.surgeons),
                    data.operating_nurse or None,
                    data.anesthesiologist or None,
                    data.anesthetist or None,
                    data.height_cm,
                    data.weight_kg,
                    data.allergies or None,
                    data.blood_group or None,
                    data.blood_rh or None,
                    data.preop_sys,
                    data.preop_dia,
                    data.preop_pulse,
                    data.preop_spo2,
                    1,
                    offline_case_uuid,
                    offline_session_id,
                    "active" if self._is_opblock_offline_runtime() else None,
                ),
            )
            operation_case_id = int(cursor.lastrowid)
            if handoff is not None:
                cursor.execute(
                    """
                    UPDATE operation_cases
                    SET handoff_id = ?,
                        source_rao_admission_id = ?,
                        last_modified_by = 'operblock',
                        revision = COALESCE(revision, 0) + 1
                    WHERE id = ?
                    """,
                    (
                        int(handoff["id"]),
                        int(handoff["source_admission_id"]),
                        operation_case_id,
                    ),
                )
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
                        operation_case_id,
                        table_code,
                        now,
                        int(handoff["id"]),
                    ),
                )
                if cursor.rowcount != 1:
                    raise OperBlockConflictError(
                        "Пациент уже выбран другим рабочим местом. Обновите очередь."
                    )
            if self._is_opblock_offline_runtime():
                cursor.execute(
                    """
                    UPDATE operation_cases
                    SET original_local_id = ?,
                        migration_status = 'active'
                    WHERE id = ?
                    """,
                    (operation_case_id, operation_case_id),
                )
            cursor.execute(
                """
                INSERT INTO operation_table_assignments (
                    operation_case_id, table_code, assigned_at, status,
                    created_by_role, created_by_client_id, last_modified_by
                ) VALUES (?, ?, ?, 'active', 'operblock', ?, 'operblock')
                """,
                (operation_case_id, table_code, case_started_text, self.client_id),
            )
            cursor.execute(
                """
                INSERT INTO patient_status_events (
                    admission_id, status, reason_type, reason_text, start_time,
                    created_by, last_modified_by
                ) VALUES (?, 'OR', 'operblock', 'В операционной', ?, 'operblock', 'operblock')
                """,
                (admission_id, case_started_text),
            )
            if _has_case_vitals(data):
                self._upsert_initial_vitals_for_case(cursor, {
                    "operation_case_id": operation_case_id,
                    "admission_id": admission_id,
                    "started_at": case_started_text,
                    "ended_at": None,
                }, data)
            return {
                "patient_id": patient_id,
                "admission_id": admission_id,
                "operation_case_id": operation_case_id,
                "offline_case_uuid": offline_case_uuid,
            }

        try:
            return dict(self.db.run_write_operation(operation, source="operblock_create_operation_case"))
        except sqlite3.IntegrityError as exc:
            raise OperBlockConflictError("Операционный стол уже занят другим пользователем.") from exc

    def get_operation_case_form_data(self, operation_case_id: int) -> dict[str, Any]:
        validate_operblock_runtime_path(self.db)
        handoff_expr = self._operation_case_column_expr("handoff_id")
        source_rao_expr = self._operation_case_column_expr("source_rao_admission_id")

        def operation(cursor: sqlite3.Cursor):
            case = cursor.execute(
                f"""
                SELECT
                    oc.id AS operation_case_id,
                    oc.patient_id,
                    oc.admission_id,
                    oc.table_code,
                    oc.status AS case_status,
                    COALESCE(oc.revision, 0) AS operation_case_revision,
                    oc.started_at,
                    oc.ended_at,
                    {handoff_expr},
                    {source_rao_expr},
                    t.display_name AS table_display_name,
                    p.full_name,
                    p.birth_date,
                    a.history_number,
                    COALESCE(a.revision, 0) AS admission_revision,
                    a.patient_gender,
                    a.diagnosis_code,
                    a.diagnosis_text,
                    a.department_profile,
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
                    oc.preop_sys,
                    oc.preop_dia,
                    oc.preop_pulse,
                    oc.preop_spo2
                FROM operation_cases oc
                JOIN operating_tables t ON t.code = oc.table_code
                JOIN admissions a ON a.id = oc.admission_id
                JOIN patients p ON p.id = oc.patient_id
                WHERE oc.id = ?
                """,
                (int(operation_case_id),),
            ).fetchone()
            if not case:
                raise OperBlockConflictError("Операция не найдена.")
            data = _row_to_dict(case)
            stage_rows = self._fetch_stage_rows_for_case(cursor, int(operation_case_id))
            stage_state = _build_stage_intervals(stage_rows)
            operation_name = _normalize_case_text(
                stage_state.get("current_operation_name")
                or stage_state.get("last_operation_name")
                or stage_state.get("first_operation_name")
                or data.get("planned_operation_name")
            )
            anesthesia_assistance_type = normalize_operblock_anesthesia_type_label(
                stage_state.get("current_anesthesia_assistance_type")
                or stage_state.get("last_anesthesia_assistance_type")
                or stage_state.get("first_anesthesia_assistance_type")
                or data.get("planned_anesthesia_assistance_type")
            )
            surgeons = _normalize_stage_text_list(
                stage_state.get("current_surgeons")
                or stage_state.get("last_surgeons")
                or stage_state.get("first_surgeons")
                or _surgeons_from_json(data.get("planned_surgeons_json")),
                split_commas=True,
            )
            operating_nurse = _normalize_case_text(
                stage_state.get("current_operating_nurse")
                or stage_state.get("last_operating_nurse")
                or stage_state.get("first_operating_nurse")
                or data.get("planned_operating_nurse")
            )
            anesthesiologist = _normalize_case_text(
                stage_state.get("current_anesthesiologist")
                or stage_state.get("last_anesthesiologist")
                or stage_state.get("first_anesthesiologist")
                or data.get("planned_anesthesiologist")
            )
            anesthetist = _normalize_case_text(
                stage_state.get("current_anesthetist")
                or stage_state.get("last_anesthetist")
                or stage_state.get("first_anesthetist")
                or data.get("planned_anesthetist")
            )
            first_vital = self._first_operation_vital_row(cursor, data)
            if first_vital:
                preop_sys = first_vital.get("sys")
                preop_dia = first_vital.get("dia")
                preop_pulse = first_vital.get("pulse")
                preop_spo2 = first_vital.get("spo2")
                vitals_source = "vitals"
            else:
                preop_sys = data.get("preop_sys")
                preop_dia = data.get("preop_dia")
                preop_pulse = data.get("preop_pulse")
                preop_spo2 = data.get("preop_spo2")
                vitals_source = "case"
            started_at_edit_lock_reason = self._operation_started_at_edit_lock_reason(cursor, data)
            started_at_min = self._linked_handoff_dispatched_at(cursor, data)
            linked_started_at = started_at_min is not None
            current_started_at = _parse_dt(data.get("started_at"))
            started_at_max = None
            if linked_started_at and current_started_at is not None:
                if started_at_edit_lock_reason:
                    started_at_max = _minute_floor(current_started_at)
                else:
                    started_at_max = max(
                        _minute_floor(current_started_at),
                        _minute_floor(datetime.now()),
                    )
            return {
                "operation_case_id": int(data.get("operation_case_id") or 0),
                "operation_case_revision": int(data.get("operation_case_revision") or 0),
                "admission_revision": int(data.get("admission_revision") or 0),
                "table_code": data.get("table_code") or "",
                "table_name": data.get("table_display_name") or "",
                "started_at": data.get("started_at"),
                "started_at_min": (
                    started_at_min.isoformat(timespec="seconds")
                    if started_at_min is not None
                    else None
                ),
                "started_at_max": (
                    started_at_max.isoformat(timespec="seconds")
                    if started_at_max is not None
                    else None
                ),
                "can_edit_started_at": linked_started_at or not bool(started_at_edit_lock_reason),
                "started_at_edit_lock_reason": (
                    "" if linked_started_at else started_at_edit_lock_reason
                ),
                "history_number": data.get("history_number") or "",
                "full_name": data.get("full_name") or "",
                "gender": data.get("patient_gender") or "",
                "birth_date": data.get("birth_date"),
                "diagnosis_code": data.get("diagnosis_code") or "",
                "diagnosis_text": data.get("diagnosis_text") or "",
                "department_profile": data.get("department_profile") or "",
                "operation_name": operation_name,
                "anesthesia_assistance_type": anesthesia_assistance_type,
                "surgeons": surgeons,
                "operating_nurse": operating_nurse,
                "anesthesiologist": anesthesiologist,
                "anesthetist": anesthetist,
                "height_cm": data.get("height_cm"),
                "weight_kg": data.get("weight_kg"),
                "allergies": data.get("allergies") or "",
                "blood_group": data.get("blood_group") or "",
                "blood_rh": data.get("blood_rh") or "",
                "preop_sys": preop_sys,
                "preop_dia": preop_dia,
                "preop_pulse": preop_pulse,
                "preop_spo2": preop_spo2,
                "vitals_source": vitals_source,
            }

        return dict(self.db.run_read_operation(operation, source="operblock_get_operation_case_form_data"))

    def update_operation_case_form_data(
        self,
        operation_case_id: int,
        data: OperBlockPatientInput | dict[str, Any],
        *,
        expected_operation_case_revision: Optional[int] = None,
        expected_admission_revision: Optional[int] = None,
    ) -> dict[str, int]:
        validate_operblock_runtime_path(self.db)
        data = _case_input_from_payload(data)
        _validate_case_vitals(data)

        history_number = normalize_operblock_history_number(data.history_number)
        full_name = _normalize_case_text(data.full_name)
        if not full_name:
            raise ValueError("ФИО пациента не заполнено.")
        diagnosis_code = normalize_operblock_mkb_code(data.diagnosis_code or "")
        if diagnosis_code and not is_complete_operblock_mkb_code(diagnosis_code):
            raise ValueError("Код МКБ-10 должен быть в формате X33, S82.0 или S82.01.")
        diagnosis_text = _normalize_case_text(data.diagnosis_text)
        if not diagnosis_text:
            raise ValueError("Диагноз не заполнен.")
        department_profile = normalize_profile_department(data.department_profile)
        birth_date = _to_birth_date(data.birth_date)
        last_name, first_name, middle_name = _split_name(full_name)
        now = _now_text()
        requested_started_at = _minute_floor(data.started_at) if data.started_at is not None else None

        def operation(cursor: sqlite3.Cursor):
            case = self._assert_active_operation_case_for_update(cursor, operation_case_id)
            assert_revision_matches(case["revision"], expected_operation_case_revision)
            assert_revision_matches(case["admission_revision"], expected_admission_revision)
            patient_id = int(case["patient_id"])
            admission_id = int(case["admission_id"])
            old_started_at = _parse_dt(case.get("started_at"))
            if old_started_at is None:
                raise OperBlockConflictError("У операции не задано время поступления. Обновите список оперблока.")
            effective_started_at = requested_started_at or _minute_floor(old_started_at)
            started_at_changed = _minute_floor(old_started_at) != effective_started_at
            dispatched_at = self._linked_handoff_dispatched_at(
                cursor,
                case,
                required=case.get("handoff_id") not in (None, ""),
            )
            self._validate_started_at_not_before_rao_dispatch(effective_started_at, dispatched_at)
            if (
                started_at_changed
                and effective_started_at
                > max(_minute_floor(old_started_at), _minute_floor(datetime.now()) + timedelta(minutes=1))
            ):
                raise ValueError("Время поступления в оперблок не может быть позже текущего времени.")
            initial_vital_to_move = None
            moving_linked_case_earlier = False
            if started_at_changed:
                moving_linked_case_earlier = (
                    dispatched_at is not None
                    and effective_started_at < _minute_floor(old_started_at)
                )
                if not moving_linked_case_earlier:
                    self._assert_started_at_can_be_changed(cursor, case)
                initial_vital_to_move = self._editable_initial_vital_row_for_started_at(cursor, case)
            started_text = effective_started_at.isoformat(timespec="seconds")
            age = storage_age_from_birth_date(birth_date, effective_started_at)
            cursor.execute(
                """
                UPDATE patients
                SET full_name = ?,
                    birth_date = ?,
                    last_name = ?,
                    first_name = ?,
                    middle_name = ?
                WHERE id = ?
                """,
                (full_name, birth_date.isoformat(), last_name, first_name, middle_name, patient_id),
            )
            admission_revision_clause = ""
            admission_params: list[Any] = [
                history_number,
                started_text,
                age["patient_age"],
                age["patient_months"],
                age["patient_age_unit"],
                data.gender,
                diagnosis_code or None,
                diagnosis_text,
                department_profile or None,
                now,
                admission_id,
            ]
            if expected_admission_revision is not None:
                admission_revision_clause = " AND COALESCE(revision, 0) = ?"
                admission_params.append(int(expected_admission_revision))
            cursor.execute(
                f"""
                UPDATE admissions
                SET history_number = ?,
                    admission_datetime = ?,
                    patient_age = ?,
                    patient_months = ?,
                    patient_age_unit = ?,
                    patient_gender = ?,
                    diagnosis_code = ?,
                    diagnosis_text = ?,
                    department_profile = ?,
                    updated_at = ?,
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ?{admission_revision_clause}
                """,
                tuple(admission_params),
            )
            if cursor.rowcount != 1:
                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            case_revision_clause = ""
            case_params: list[Any] = [
                started_text,
                data.operation_name or None,
                data.anesthesia_assistance_type or None,
                _surgeons_json(data.surgeons),
                data.operating_nurse or None,
                data.anesthesiologist or None,
                data.anesthetist or None,
                data.height_cm,
                data.weight_kg,
                data.allergies or None,
                data.blood_group or None,
                data.blood_rh or None,
                data.preop_sys,
                data.preop_dia,
                data.preop_pulse,
                data.preop_spo2,
                1,
                int(operation_case_id),
            ]
            if expected_operation_case_revision is not None:
                case_revision_clause = " AND COALESCE(revision, 0) = ?"
                case_params.append(int(expected_operation_case_revision))
            cursor.execute(
                f"""
                UPDATE operation_cases
                SET started_at = ?,
                    planned_operation_name = ?,
                    planned_anesthesia_assistance_type = ?,
                    planned_surgeons_json = ?,
                    planned_operating_nurse = ?,
                    planned_anesthesiologist = ?,
                    planned_anesthetist = ?,
                    height_cm = ?,
                    weight_kg = ?,
                    allergies = ?,
                    blood_group = ?,
                    blood_rh = ?,
                    preop_sys = ?,
                    preop_dia = ?,
                    preop_pulse = ?,
                    preop_spo2 = ?,
                    preop_save_initial_vitals = ?,
                    last_modified_by = 'operblock',
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ?
                  AND status = 'active'
                  {case_revision_clause}
                """,
                tuple(case_params),
            )
            if cursor.rowcount != 1:
                if expected_operation_case_revision is None:
                    raise OperBlockConflictError("Случай уже изменён другим рабочим местом. Обновите список оперблока.")
                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            if started_at_changed:
                cursor.execute(
                    """
                    UPDATE operation_table_assignments
                    SET assigned_at = ?,
                        last_modified_by = 'operblock',
                        revision = COALESCE(revision, 0) + 1,
                        updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                    WHERE operation_case_id = ?
                      AND table_code = ?
                      AND status = 'active'
                      AND released_at IS NULL
                    """,
                    (started_text, int(operation_case_id), case.get("table_code")),
                )
                cursor.execute(
                    """
                    UPDATE patient_status_events
                    SET start_time = ?,
                        last_modified_by = 'operblock',
                        revision = COALESCE(revision, 0) + 1,
                        updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                    WHERE admission_id = ?
                      AND status = 'OR'
                      AND reason_type = 'operblock'
                      AND end_time IS NULL
                    """,
                    (started_text, admission_id),
                )
                if initial_vital_to_move is not None:
                    cursor.execute(
                        """
                        UPDATE vitals
                        SET datetime = ?,
                            last_modified_by = 'operblock',
                            revision = COALESCE(revision, 0) + 1,
                            updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                        WHERE id = ?
                        """,
                        (started_text, int(initial_vital_to_move["id"])),
                    )
            case_for_vitals = dict(case)
            case_for_vitals["started_at"] = started_text
            case_for_vitals["ended_at"] = case.get("ended_at")
            if _has_case_vitals(data) and (
                not moving_linked_case_earlier or initial_vital_to_move is not None
            ):
                self._upsert_initial_vitals_for_case(cursor, case_for_vitals, data)
            synced = self._sync_case_metadata_to_stage_payloads(
                cursor,
                int(operation_case_id),
                operation_name=data.operation_name,
                anesthesia_assistance_type=data.anesthesia_assistance_type,
                surgeons=data.surgeons,
                operating_nurse=data.operating_nurse,
                anesthesiologist=data.anesthesiologist,
                anesthetist=data.anesthetist,
            )
            return {
                "operation_case_id": int(operation_case_id),
                "admission_id": admission_id,
                "patient_id": patient_id,
                "synced_stage_events": int(synced),
            }

        return dict(self.db.run_write_operation(operation, source="operblock_update_operation_case_form_data"))

    def close_operation_case(self, operation_case_id: int) -> dict[str, int]:
        return self.release_operation_table(operation_case_id)
