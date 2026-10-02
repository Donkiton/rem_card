from __future__ import annotations

import sqlite3
from contextlib import nullcontext
from datetime import datetime
from typing import Any, Mapping
from rem_card.services.concurrency import DATA_CONFLICT_MESSAGE, DataConflictError
from rem_card.services.operblock_anesthesia_types import normalize_operblock_anesthesia_type_label
from rem_card.services.operblock_timeline import operation_stage_kind_from_payload
from .common import (
    OperBlockConflictError,
    OperBlockPatientInput,
    validate_operblock_runtime_path,
    _parse_dt,
    _minute_floor,
    normalize_operblock_transfer_department,
    _is_rao_transfer_department,
    _parse_json_dict,
    _normalize_case_text,
    _normalize_case_surgeons,
    _has_case_vitals,
    _row_to_dict,
)


class OperBlockCaseContextMixin:
    def get_start_anesthesia_context(self, operation_case_id: int) -> dict[str, Any]:
        """Load all case-derived defaults for the start dialog from one snapshot."""
        scope_factory = getattr(self.db, "central_read_snapshot_scope", None)
        if not callable(scope_factory):
            scope_factory = getattr(self.db, "central_read_scope", None)
        read_scope = (
            scope_factory("operblock_start_anesthesia_context")
            if callable(scope_factory)
            else nullcontext()
        )
        with read_scope:
            defaults = self.get_operation_case_form_data(int(operation_case_id))
            vitals = self.list_operation_vitals(int(operation_case_id))

        latest_vital_at = max(
            (
                _minute_floor(vital.timestamp)
                for vital in vitals
                if isinstance(getattr(vital, "timestamp", None), datetime)
            ),
            default=None,
        )
        return {
            "operation_case_id": int(operation_case_id),
            "has_initial_vitals": bool(vitals),
            "latest_vital_at": latest_vital_at,
            "defaults": dict(defaults or {}),
        }

    def _first_operation_vital_row(
        self,
        cursor: sqlite3.Cursor,
        case: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        started_at = _parse_dt(case.get("started_at"))
        if started_at is None:
            return None
        params: list[Any] = [int(case["admission_id"]), _minute_floor(started_at).isoformat()]
        end_clause = ""
        ended_at = _parse_dt(case.get("ended_at")) if case.get("ended_at") else None
        if ended_at is not None:
            end_clause = 'AND datetime("datetime") <= datetime(?)'
            params.append(_minute_floor(ended_at).isoformat())
        row = cursor.execute(
            f"""
            SELECT id, admission_id, datetime, sys, dia, pulse, spo2, COALESCE(revision, 0) AS revision
            FROM vitals
            WHERE admission_id = ?
              AND datetime("datetime") >= datetime(?)
              {end_clause}
              AND (sys IS NOT NULL OR dia IS NOT NULL OR pulse IS NOT NULL OR spo2 IS NOT NULL)
            ORDER BY datetime("datetime") ASC, id ASC
            LIMIT 1
            """,
            tuple(params),
        ).fetchone()
        return _row_to_dict(row) if row else None

    def _upsert_initial_vitals_for_case(
        self,
        cursor: sqlite3.Cursor,
        case: Mapping[str, Any],
        data: OperBlockPatientInput,
    ) -> int | None:
        if not _has_case_vitals(data):
            return None
        started_at = _parse_dt(case.get("started_at"))
        if started_at is None:
            raise OperBlockConflictError("У операции не задано время начала. Обновите протокол.")
        existing = self._first_operation_vital_row(cursor, case)
        if existing:
            cursor.execute(
                """
                UPDATE vitals
                SET sys = ?,
                    dia = ?,
                    pulse = ?,
                    spo2 = ?,
                    last_modified_by = 'operblock',
                    revision = COALESCE(revision, 0) + 1,
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                """,
                (data.preop_sys, data.preop_dia, data.preop_pulse, data.preop_spo2, int(existing["id"])),
            )
            return int(existing["id"])
        cursor.execute(
            """
            INSERT INTO vitals (
                admission_id, datetime, sys, dia, pulse, spo2, last_modified_by, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'operblock', STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
            """,
            (
                int(case["admission_id"]),
                _minute_floor(started_at).isoformat(timespec="seconds"),
                data.preop_sys,
                data.preop_dia,
                data.preop_pulse,
                data.preop_spo2,
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _same_minute(left: datetime | None, right: datetime | None) -> bool:
        if left is None or right is None:
            return False
        return _minute_floor(left) == _minute_floor(right)

    def _started_at_edit_vital_rows(
        self,
        cursor: sqlite3.Cursor,
        admission_id: int,
    ) -> list[dict[str, Any]]:
        rows = cursor.execute(
            """
            SELECT id, admission_id, datetime, sys, dia, pulse, temp, spo2, rr, cvp,
                   COALESCE(revision, 0) AS revision
            FROM vitals
            WHERE admission_id = ?
            ORDER BY datetime("datetime") ASC, id ASC
            LIMIT 2
            """,
            (int(admission_id),),
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def _editable_initial_vital_row_for_started_at(
        self,
        cursor: sqlite3.Cursor,
        case: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        rows = self._started_at_edit_vital_rows(cursor, int(case["admission_id"]))
        if not rows:
            return None
        if len(rows) != 1:
            return None
        row = rows[0]
        started_at = _parse_dt(case.get("started_at"))
        vital_dt = _parse_dt(row.get("datetime"))
        if not self._same_minute(started_at, vital_dt):
            return None
        if any(row.get(field) is not None for field in ("temp", "rr", "cvp")):
            return None
        return row

    def _operation_started_at_edit_lock_reason(
        self,
        cursor: sqlite3.Cursor,
        case: Mapping[str, Any],
    ) -> str:
        operation_case_id = int(case["operation_case_id"])
        admission_id = int(case["admission_id"])
        event = cursor.execute(
            """
            SELECT id
            FROM operblock_timeline_events
            WHERE operation_case_id = ?
              AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
            LIMIT 1
            """,
            (operation_case_id,),
        ).fetchone()
        if event:
            return "В карте уже есть этапы, пособие, операция или события введения препаратов."

        order = cursor.execute(
            """
            SELECT id
            FROM orders
            WHERE admission_id = ?
              AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
            LIMIT 1
            """,
            (admission_id,),
        ).fetchone()
        if order:
            return "В карте уже есть назначения."

        vital_rows = self._started_at_edit_vital_rows(cursor, admission_id)
        if not vital_rows:
            return ""
        if len(vital_rows) > 1:
            return "В карте уже есть витальные показатели."
        if self._editable_initial_vital_row_for_started_at(cursor, case) is None:
            return "В карте уже есть витальные показатели."
        return ""

    def _assert_started_at_can_be_changed(
        self,
        cursor: sqlite3.Cursor,
        case: Mapping[str, Any],
    ) -> None:
        reason = self._operation_started_at_edit_lock_reason(cursor, case)
        if reason:
            raise ValueError(
                "Время поступления в оперблок можно изменить только до внесения данных в карту. "
                "Отмените внесённые изменения и повторите попытку. "
                f"Причина: {reason}"
            )

    @staticmethod
    def _linked_handoff_dispatched_at(
        cursor: sqlite3.Cursor,
        case: Mapping[str, Any],
        *,
        required: bool = False,
    ) -> datetime | None:
        handoff_id = case.get("handoff_id")
        if handoff_id in (None, ""):
            return None
        source_rao_admission_id = case.get("source_rao_admission_id")
        row = cursor.execute(
            """
            SELECT dispatched_at
            FROM operblock_handoffs
            WHERE id = ?
              AND (? IS NULL OR source_admission_id = ?)
            LIMIT 1
            """,
            (
                int(handoff_id),
                source_rao_admission_id,
                source_rao_admission_id,
            ),
        ).fetchone()
        dispatched_at = _parse_dt(row["dispatched_at"]) if row else None
        if required and dispatched_at is None:
            raise OperBlockConflictError(
                "Не удалось определить время отправки пациента из РАО. Обновите данные оперблока."
            )
        return _minute_floor(dispatched_at) if dispatched_at is not None else None

    @staticmethod
    def _validate_started_at_not_before_rao_dispatch(
        started_at: datetime,
        dispatched_at: datetime | None,
    ) -> None:
        if dispatched_at is None or _minute_floor(started_at) >= _minute_floor(dispatched_at):
            return
        raise ValueError(
            "Время поступления в оперблок не может быть раньше времени отправки из РАО "
            f"({dispatched_at.strftime('%H:%M')})."
        )

    @staticmethod
    def _set_stage_payload_text(payload: dict[str, Any], key: str, value: str) -> None:
        if value:
            payload[key] = value
        else:
            payload.pop(key, None)

    def _sync_case_metadata_to_stage_payloads(
        self,
        cursor: sqlite3.Cursor,
        operation_case_id: int,
        *,
        operation_name: str | None = None,
        anesthesia_assistance_type: str | None = None,
        surgeons: tuple[str, ...] | list[str] | None = None,
        operating_nurse: str | None = None,
        anesthesiologist: str | None = None,
        anesthetist: str | None = None,
    ) -> int:
        rows = [
            _row_to_dict(row)
            for row in cursor.execute(
                """
                SELECT id, payload_json
                FROM operblock_timeline_events
                WHERE operation_case_id = ?
                  AND event_type = 'clinical_event'
                  AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                ORDER BY datetime(event_time) ASC, id ASC
                """,
                (int(operation_case_id),),
            ).fetchall()
        ]

        def latest_stage_row(kind: str) -> dict[str, Any] | None:
            for row in reversed(rows):
                payload = _parse_json_dict(row.get("payload_json"))
                if operation_stage_kind_from_payload(payload) == kind:
                    return row
            return None

        def update_payload(row: dict[str, Any], payload: dict[str, Any]) -> bool:
            payload_json = self._timeline_payload_json(payload)
            if payload_json == row.get("payload_json"):
                return False
            cursor.execute(
                """
                UPDATE operblock_timeline_events
                SET payload_json = ?,
                    revision = COALESCE(revision, 0) + 1,
                    last_modified_by = 'operblock',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                  AND event_type = 'clinical_event'
                  AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                """,
                (payload_json, int(row["id"])),
            )
            if cursor.rowcount != 1:
                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            return True

        updated = 0
        if operation_name is not None or surgeons is not None or operating_nurse is not None:
            surgery_row = latest_stage_row("surgery_start")
            if surgery_row is not None:
                payload = _parse_json_dict(surgery_row.get("payload_json"))
                if operation_name is not None:
                    self._set_stage_payload_text(payload, "operation_name", _normalize_case_text(operation_name))
                if surgeons is not None:
                    clean_surgeons = list(_normalize_case_surgeons(surgeons))
                    if clean_surgeons:
                        payload["surgeons"] = clean_surgeons
                        payload["surgeon"] = ", ".join(clean_surgeons)
                    else:
                        payload.pop("surgeons", None)
                        payload.pop("surgeon", None)
                if operating_nurse is not None:
                    self._set_stage_payload_text(payload, "operating_nurse", _normalize_case_text(operating_nurse))
                if update_payload(surgery_row, payload):
                    updated += 1

        if anesthesia_assistance_type is not None or anesthesiologist is not None or anesthetist is not None:
            anesthesia_row = latest_stage_row("anesthesia_start")
            if anesthesia_row is not None:
                payload = _parse_json_dict(anesthesia_row.get("payload_json"))
                if anesthesia_assistance_type is not None:
                    self._set_stage_payload_text(
                        payload,
                        "anesthesia_assistance_type",
                        normalize_operblock_anesthesia_type_label(anesthesia_assistance_type),
                    )
                if anesthesiologist is not None:
                    self._set_stage_payload_text(payload, "anesthesiologist", _normalize_case_text(anesthesiologist))
                if anesthetist is not None:
                    self._set_stage_payload_text(payload, "anesthetist", _normalize_case_text(anesthetist))
                if update_payload(anesthesia_row, payload):
                    updated += 1
        return updated

    def list_waiting_rao_handoffs(self) -> list[dict[str, Any]]:
        validate_operblock_runtime_path(self.db)
        return self.handoff_service.list_waiting()

    def get_rao_handoff_form_data(self, handoff_id: int, table_code: str) -> dict[str, Any]:
        validate_operblock_runtime_path(self.db)
        table_code = self._validate_table_code(table_code)
        handoff = self.handoff_service.get_waiting(int(handoff_id))
        if not handoff:
            raise OperBlockConflictError(
                "Пациент уже выбран другим рабочим местом или больше не ожидает операционную."
            )
        patient = dict(handoff.get("patient_snapshot") or {})
        vitals = dict(handoff.get("vitals_snapshot") or {})
        return {
            "table_code": table_code,
            "handoff_id": int(handoff["id"]),
            "source_rao_admission_id": int(handoff["source_admission_id"]),
            "history_number": str(patient.get("history_number") or ""),
            "full_name": str(patient.get("full_name") or ""),
            "gender": str(patient.get("gender") or ""),
            "birth_date": patient.get("birth_date"),
            "diagnosis_code": patient.get("diagnosis_code"),
            "diagnosis_text": str(patient.get("diagnosis_text") or ""),
            "department_profile": str(patient.get("department_profile") or ""),
            "started_at": handoff.get("expected_arrival_at"),
            "started_at_min": handoff.get("dispatched_at"),
            "started_at_max": max(
                filter(
                    None,
                    (
                        _parse_dt(handoff.get("expected_arrival_at")),
                        _minute_floor(datetime.now()),
                    ),
                )
            ).isoformat(timespec="seconds"),
            "can_edit_started_at": True,
            "started_at_edit_lock_reason": "",
            "preop_sys": vitals.get("sys"),
            "preop_dia": vitals.get("dia"),
            "preop_pulse": vitals.get("pulse"),
            "preop_spo2": vitals.get("spo2"),
        }

    def find_late_binding_candidates(
        self,
        operation_case_id: int,
        *,
        target_department: str | None = None,
    ) -> list[dict[str, Any]]:
        validate_operblock_runtime_path(self.db)
        handoff_expr = self._operation_case_column_expr("handoff_id")
        source_expr = self._operation_case_column_expr("source_rao_admission_id")
        future_rao_expr = self._operation_case_column_expr("future_rao_admission_id")
        case = self.db.fetch_one_remcard(
            f"""
            SELECT
                oc.id AS operation_case_id,
                {handoff_expr},
                {source_expr},
                {future_rao_expr},
                oc.transfer_department,
                a.history_number,
                p.full_name,
                p.birth_date
            FROM operation_cases oc
            JOIN admissions a ON a.id = oc.admission_id
            JOIN patients p ON p.id = oc.patient_id
            WHERE oc.id = ? AND oc.status = 'active'
            """,
            (int(operation_case_id),),
        )
        effective_department = (
            normalize_operblock_transfer_department(target_department)
            if target_department is not None
            else normalize_operblock_transfer_department(case["transfer_department"] if case else None)
        )
        if (
            not case
            or case["handoff_id"] is not None
            or case["source_rao_admission_id"] is not None
            or case["future_rao_admission_id"] is not None
            or not _is_rao_transfer_department(effective_department)
        ):
            return []
        candidates = self.handoff_service.find_waiting_candidates(
            history_number=case["history_number"],
            full_name=case["full_name"],
            birth_date=case["birth_date"],
        )
        return [
            candidate
            for candidate in candidates
            if candidate.get("full_name_matches") and candidate.get("birth_date_matches")
        ]
