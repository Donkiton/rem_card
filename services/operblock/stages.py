from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Optional
from rem_card.app.logger import logger
from rem_card.data.dto.remcard_dto import PatientStatus
from rem_card.services.concurrency import DATA_CONFLICT_MESSAGE, DataConflictError, assert_revision_matches
from rem_card.services.operblock_anesthesia_types import normalize_operblock_anesthesia_type_label
from rem_card.services.operblock_handoff_service import HANDOFF_COMPLETED_NON_RAO, HANDOFF_RETURNED_TO_RAO
from rem_card.services.operblock_timeline import OPERBLOCK_STAGE_KIND_LABELS, operation_stage_kind_from_payload
from .common import (
    OperBlockConflictError,
    OperBlockSourceMovementChangedError,
    validate_operblock_runtime_path,
    _parse_dt,
    _minute_floor,
    _format_bound_time,
    normalize_operblock_transfer_department,
    _is_rao_transfer_department,
    transfer_department_target_text,
    operblock_transfer_stage_label,
    _parse_json_dict,
    _stage_label,
    _normalize_operation_stage_label,
    _stage_rows_from_timeline_rows,
    _operation_stage_window_is_active,
    _normalize_stage_text_list,
    _build_stage_intervals,
    _surgeons_json,
    _row_to_dict,
)


class OperBlockStagesMixin:
    def release_operation_table(
        self,
        operation_case_id: int,
        *,
        handoff_id: int | None = None,
        preserve_source_movement: bool = False,
    ) -> dict[str, Any]:
        validate_operblock_runtime_path(self.db)
        released_dt = _minute_floor(datetime.now())
        now = released_dt.isoformat(timespec="seconds")
        handoff_expr = self._operation_case_column_expr("handoff_id")
        source_rao_expr = self._operation_case_column_expr("source_rao_admission_id")

        def operation(cursor: sqlite3.Cursor):
            if handoff_id is not None:
                active_case = self._assert_active_operation_case_for_update(
                    cursor,
                    int(operation_case_id),
                )
                self._bind_waiting_handoff_to_case(cursor, active_case, int(handoff_id))
            row = cursor.execute(
                f"""
                SELECT
                    oc.id, oc.admission_id, oc.table_code, oc.started_at, oc.transfer_department,
                    {handoff_expr}, {source_rao_expr}
                FROM operation_cases oc
                WHERE oc.id = ? AND oc.status = 'active'
                """,
                (int(operation_case_id),),
            ).fetchone()
            if not row:
                raise OperBlockConflictError("Случай уже закрыт или не найден.")
            admission_id = int(row["admission_id"])
            stage_rows = self._fetch_stage_rows_for_case(cursor, int(operation_case_id))
            if self._active_anesthesia_interval(stage_rows) is not None:
                raise ValueError("Перед освобождением стола завершите анестезиологическое пособие.")
            linked_handoff_id = int(row["handoff_id"]) if row["handoff_id"] is not None else None
            source_rao_admission_id = (
                int(row["source_rao_admission_id"])
                if row["source_rao_admission_id"] is not None
                else None
            )
            transfer_department = normalize_operblock_transfer_department(row["transfer_department"])
            case_started_dt = _parse_dt(row["started_at"])
            case_closed_dt = max(released_dt, _minute_floor(case_started_dt)) if case_started_dt else released_dt
            case_closed_text = case_closed_dt.isoformat(timespec="seconds")
            return_to_rao = bool(
                linked_handoff_id is not None
                and source_rao_admission_id is not None
                and _is_rao_transfer_department(transfer_department)
            )
            effective_return_dt = released_dt + timedelta(minutes=5)
            effective_return_text = effective_return_dt.isoformat(timespec="seconds")
            source_event = None
            source_movement_preserved = False
            if return_to_rao:
                source_event = cursor.execute(
                    """
                    SELECT id, status, start_time
                    FROM patient_status_events
                    WHERE admission_id = ? AND end_time IS NULL
                    LIMIT 1
                    """,
                    (source_rao_admission_id,),
                ).fetchone()
                if not source_event or str(source_event["status"] or "") != PatientStatus.OR.value:
                    if not preserve_source_movement:
                        raise OperBlockSourceMovementChangedError(
                            "Движение пациента в исходной карте РАО уже изменено. "
                            "Стол будет освобождён без изменения движения пациента "
                            "в исходной карте."
                        )
                    source_movement_preserved = True
            cursor.execute(
                """
                UPDATE operation_cases
                SET status = 'closed',
                    ended_at = ?,
                    migration_status = CASE
                        WHEN COALESCE(offline_session_id, '') <> '' THEN 'pending'
                        ELSE migration_status
                    END,
                    last_modified_by = 'operblock',
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ? AND status = 'active'
                """,
                (case_closed_text, int(operation_case_id)),
            )
            if cursor.rowcount != 1:
                raise OperBlockConflictError("Случай уже закрыт другим пользователем.")
            cursor.execute(
                """
                UPDATE operation_table_assignments
                SET status = 'released',
                    released_at = ?,
                    last_modified_by = 'operblock',
                    revision = COALESCE(revision, 0) + 1
                WHERE operation_case_id = ?
                  AND status = 'active'
                  AND released_at IS NULL
                """,
                (case_closed_text, int(operation_case_id)),
            )
            cursor.execute(
                """
                UPDATE patient_status_events
                SET end_time = ?,
                    last_modified_by = 'operblock',
                    revision = COALESCE(revision, 0) + 1
                WHERE admission_id = ?
                  AND end_time IS NULL
                """,
                (case_closed_text, admission_id),
            )
            cursor.execute(
                """
                UPDATE admissions
                SET is_active = 0,
                    updated_at = ?,
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ?
                """,
                (now, admission_id),
            )
            if linked_handoff_id is not None and source_rao_admission_id is not None:
                if return_to_rao:
                    if source_movement_preserved:
                        effective_return_text = (
                            str(source_event["start_time"] or "") or None
                            if source_event is not None
                            else None
                        )
                    else:
                        cursor.execute(
                            """
                            UPDATE patient_status_events
                            SET end_time = ?,
                                updated_at = ?,
                                last_modified_by = 'operblock',
                                revision = COALESCE(revision, 0) + 1
                            WHERE admission_id = ?
                              AND status = 'OR'
                              AND end_time IS NULL
                            """,
                            (effective_return_text, now, source_rao_admission_id),
                        )
                        if cursor.rowcount != 1:
                            raise OperBlockConflictError(
                                "Движение исходной карты РАО изменилось другим пользователем."
                            )
                        cursor.execute(
                            """
                            INSERT INTO patient_status_events (
                                admission_id, status, reason_type, reason_text, start_time,
                                created_by, created_at, updated_at, last_modified_by
                            ) VALUES (?, ?, 'operblock_return', ?, ?, 'operblock', ?, ?, 'operblock')
                            """,
                            (
                                source_rao_admission_id,
                                PatientStatus.ACTIVE.value,
                                "Возврат из операционной",
                                effective_return_text,
                                now,
                                now,
                            ),
                        )
                    cursor.execute(
                        """
                        UPDATE operation_cases
                        SET resolved_rao_admission_id = ?,
                            last_modified_by = 'operblock',
                            revision = COALESCE(revision, 0) + 1
                        WHERE id = ?
                        """,
                        (source_rao_admission_id, int(operation_case_id)),
                    )
                    handoff_status = HANDOFF_RETURNED_TO_RAO
                else:
                    handoff_status = HANDOFF_COMPLETED_NON_RAO
                cursor.execute(
                    """
                    UPDATE operblock_handoffs
                    SET status = ?,
                        transfer_department = ?,
                        released_at = ?,
                        effective_return_at = ?,
                        source_movement_preserved = ?,
                        last_modified_by = 'operblock',
                        revision = COALESCE(revision, 0) + 1
                    WHERE id = ?
                      AND operation_case_id = ?
                      AND status = 'accepted'
                    """,
                    (
                        handoff_status,
                        transfer_department or None,
                        now,
                        effective_return_text if return_to_rao else None,
                        1 if source_movement_preserved else 0,
                        linked_handoff_id,
                        int(operation_case_id),
                    ),
                )
                if cursor.rowcount != 1:
                    raise OperBlockConflictError(
                        "Связь с исходной картой РАО уже изменена другим пользователем."
                    )
            return {
                "operation_case_id": int(operation_case_id),
                "admission_id": admission_id,
                "source_movement_preserved": source_movement_preserved,
            }

        return dict(self.db.run_write_operation(operation, source="operblock_release_operation_table"))

    def start_anesthesia(
        self,
        operation_case_id: int,
        assistance_type: str | None = None,
        *,
        anesthesiologist: str | None = None,
        anesthetist: str | None = None,
        event_time: Any = None,
    ) -> int:
        return self._add_stage_event(
            operation_case_id,
            "anesthesia_start",
            assistance_type=assistance_type,
            anesthesiologist=anesthesiologist,
            anesthetist=anesthetist,
            event_time=event_time,
        )

    def end_anesthesia(self, operation_case_id: int, *, event_time: Any = None) -> int:
        return self._add_stage_event(operation_case_id, "anesthesia_end", event_time=event_time)

    def end_anesthesia_with_transfer(
        self,
        operation_case_id: int,
        transfer_department: str,
        *,
        event_time: Any = None,
        handoff_id: int | None = None,
    ) -> int:
        return self._add_stage_event(
            operation_case_id,
            "anesthesia_end",
            transfer_department=transfer_department,
            event_time=event_time,
            handoff_id=handoff_id,
        )

    def start_surgery(
        self,
        operation_case_id: int,
        *,
        operation_name: str | None = None,
        surgeons: list[str] | None = None,
        surgeon: str | None = None,
        operating_nurse: str | None = None,
        event_time: Any = None,
    ) -> int:
        return self._add_stage_event(
            operation_case_id,
            "surgery_start",
            operation_name=operation_name,
            surgeons=surgeons,
            surgeon=surgeon,
            operating_nurse=operating_nurse,
            event_time=event_time,
        )

    def end_surgery(self, operation_case_id: int, *, event_time: Any = None) -> int:
        return self._add_stage_event(operation_case_id, "surgery_end", event_time=event_time)

    def add_operation_stage(
        self,
        operation_case_id: int,
        label: str,
        *,
        event_time: Any = None,
    ) -> dict[str, Any]:
        validate_operblock_runtime_path(self.db)
        clean_label = _normalize_operation_stage_label(label)
        if not clean_label:
            raise ValueError("Укажите название этапа операции.")
        event_dt = (
            self._normalize_timeline_event_datetime(event_time)
            if event_time is not None
            else _minute_floor(datetime.now())
        )

        def operation(cursor: sqlite3.Cursor):
            case = self._assert_active_operation_case_for_update(cursor, operation_case_id)
            stage_rows = self._fetch_stage_rows_for_case(cursor, int(operation_case_id))
            state = self._stage_state_from_cursor_rows(stage_rows)
            if not _operation_stage_window_is_active(state):
                raise ValueError(
                    "Этапы доступны после начала и до завершения пособия."
                )
            self._assert_datetime_in_active_anesthesia_bounds(
                cursor,
                event_dt,
                case,
                entity_label="Этап",
            )
            payload = {"stage_kind": "custom", "label": clean_label}
            payload_json = self._timeline_payload_json(payload)
            cursor.execute(
                """
                INSERT INTO operblock_timeline_events (
                    operation_case_id, admission_id, table_code, event_type, event_time,
                    drug_label, display_label, raw_text, status, revision, payload_json,
                    created_by_role, created_by_client_id, last_modified_by
                ) VALUES (?, ?, ?, 'clinical_event', ?, ?, ?, ?, 'active', 1, ?,
                          'operblock', ?, 'operblock')
                """,
                (
                    int(case["operation_case_id"]),
                    int(case["admission_id"]),
                    case.get("table_code"),
                    event_dt.isoformat(timespec="seconds"),
                    clean_label,
                    clean_label,
                    clean_label,
                    payload_json,
                    self.client_id,
                ),
            )
            return self._operation_stage_event_result(
                event_id=int(cursor.lastrowid),
                case=case,
                label=clean_label,
                event_dt=event_dt,
                revision=1,
                payload=payload,
            )

        return dict(self.db.run_write_operation(operation, source="operblock_add_operation_stage"))

    def update_operation_stage(
        self,
        event_id: int,
        label: str,
        *,
        expected_revision: Optional[int] = None,
        event_time: Any = None,
    ) -> dict[str, Any]:
        validate_operblock_runtime_path(self.db)
        clean_label = _normalize_operation_stage_label(label)
        if not clean_label:
            raise ValueError("Укажите название этапа операции.")
        new_event_dt = self._normalize_timeline_event_datetime(event_time) if event_time is not None else None

        def operation(cursor: sqlite3.Cursor):
            row = cursor.execute(
                """
                SELECT
                    id, operation_case_id, admission_id, table_code, event_type, event_time,
                    display_label, raw_text, payload_json, COALESCE(revision, 0) AS revision
                FROM operblock_timeline_events
                WHERE id = ?
                  AND event_type = 'clinical_event'
                  AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                """,
                (int(event_id),),
            ).fetchone()
            if not row:
                raise OperBlockConflictError("Этап операции не найден или уже удалён. Обновите протокол.")
            payload = _parse_json_dict(row["payload_json"])
            if operation_stage_kind_from_payload(payload) != "custom":
                raise ValueError("Автоматические этапы операции нельзя редактировать.")
            assert_revision_matches(row["revision"], expected_revision)
            case = self._assert_active_operation_case_for_update(cursor, int(row["operation_case_id"]))
            if int(case["admission_id"]) != int(row["admission_id"]):
                raise OperBlockConflictError("Этап операции не принадлежит текущему случаю. Обновите протокол.")
            stage_rows = self._fetch_stage_rows_for_case(cursor, int(case["operation_case_id"]))
            state = self._stage_state_from_cursor_rows(stage_rows)
            if not _operation_stage_window_is_active(state):
                raise ValueError(
                    "Этапы доступны после начала и до завершения пособия."
                )
            event_dt = _parse_dt(row["event_time"])
            if event_dt is None:
                raise OperBlockConflictError("Не удалось определить время этапа. Обновите протокол.")
            effective_event_dt = _minute_floor(new_event_dt or event_dt)
            self._assert_datetime_in_active_anesthesia_bounds(
                cursor,
                effective_event_dt,
                case,
                entity_label="Время этапа",
            )
            payload["label"] = clean_label
            payload_json = self._timeline_payload_json(payload)
            if (
                str(row["display_label"] or "").strip() == clean_label
                and str(row["raw_text"] or "").strip() == clean_label
                and str(row["payload_json"] or "") == str(payload_json or "")
                and _minute_floor(event_dt) == effective_event_dt
            ):
                return self._operation_stage_event_result(
                    event_id=int(row["id"]),
                    case=case,
                    label=clean_label,
                    event_dt=effective_event_dt,
                    revision=int(row["revision"] or 0),
                    payload=payload,
                )
            revision_clause = ""
            params: list[Any] = [
                clean_label,
                clean_label,
                clean_label,
                effective_event_dt.isoformat(timespec="seconds"),
                payload_json,
                int(row["id"]),
            ]
            if expected_revision is not None:
                revision_clause = "AND COALESCE(revision, 0) = ?"
                params.append(int(expected_revision))
            cursor.execute(
                f"""
                UPDATE operblock_timeline_events
                SET drug_label = ?,
                    display_label = ?,
                    raw_text = ?,
                    event_time = ?,
                    payload_json = ?,
                    revision = COALESCE(revision, 0) + 1,
                    last_modified_by = 'operblock',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                  AND event_type = 'clinical_event'
                  AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                  {revision_clause}
                """,
                tuple(params),
            )
            if cursor.rowcount != 1:
                assert_revision_matches(None, expected_revision)
                raise OperBlockConflictError("Этап операции уже изменён другим пользователем. Обновите протокол.")
            return self._operation_stage_event_result(
                event_id=int(row["id"]),
                case=case,
                label=clean_label,
                event_dt=effective_event_dt,
                revision=int(row["revision"] or 0) + 1,
                payload=payload,
            )

        return dict(self.db.run_write_operation(operation, source="operblock_update_operation_stage"))

    def update_operation_staff(
        self,
        operation_case_id: int,
        *,
        surgeons: list[str] | None = None,
        operating_nurse: str | None = None,
        anesthesiologist: str | None = None,
        anesthetist: str | None = None,
    ) -> dict[str, Any]:
        validate_operblock_runtime_path(self.db)
        clean_surgeons = _normalize_stage_text_list(surgeons)
        clean_operating_nurse = re.sub(r"\s+", " ", str(operating_nurse or "").strip())
        clean_anesthesiologist = re.sub(r"\s+", " ", str(anesthesiologist or "").strip())
        clean_anesthetist = re.sub(r"\s+", " ", str(anesthetist or "").strip())

        def latest_stage_row(rows: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
            for row in reversed(rows):
                payload = _parse_json_dict(row.get("payload_json"))
                if operation_stage_kind_from_payload(payload) == kind:
                    return row
            return None

        def set_text(payload: dict[str, Any], key: str, value: str) -> None:
            if value:
                payload[key] = value
            else:
                payload.pop(key, None)

        def update_payload(cursor: sqlite3.Cursor, row: dict[str, Any], payload: dict[str, Any]) -> bool:
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

        def operation(cursor: sqlite3.Cursor):
            case = self._assert_active_operation_case_for_update(cursor, operation_case_id)
            rows = [
                _row_to_dict(row)
                for row in cursor.execute(
                    """
                    SELECT id, operation_case_id, admission_id, table_code, event_type, event_time, end_time,
                           drug_label, display_label, raw_text, status, COALESCE(revision, 0) AS revision,
                           parent_event_id, payload_json, created_at, updated_at
                    FROM operblock_timeline_events
                    WHERE operation_case_id = ?
                      AND event_type = 'clinical_event'
                      AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                    ORDER BY datetime(event_time) ASC, id ASC
                    """,
                    (int(case["operation_case_id"]),),
                ).fetchall()
            ]
            stage_state = _build_stage_intervals(_stage_rows_from_timeline_rows(rows))
            updated = 0

            if surgeons is not None or operating_nurse is not None:
                if not stage_state.get("surgery_active"):
                    raise ValueError("Операция ещё не начата или уже завершена.")
                surgery_row = latest_stage_row(rows, "surgery_start")
                if surgery_row is None:
                    raise OperBlockConflictError("Начало операции не найдено. Обновите протокол.")
                payload = _parse_json_dict(surgery_row.get("payload_json"))
                if surgeons is not None:
                    if clean_surgeons:
                        payload["surgeons"] = clean_surgeons
                        payload["surgeon"] = ", ".join(clean_surgeons)
                    else:
                        payload.pop("surgeons", None)
                        payload.pop("surgeon", None)
                if operating_nurse is not None:
                    set_text(payload, "operating_nurse", clean_operating_nurse)
                if update_payload(cursor, surgery_row, payload):
                    updated += 1
                if surgeons is not None or operating_nurse is not None:
                    case_updates: list[str] = []
                    case_params: list[Any] = []
                    if surgeons is not None:
                        case_updates.append("planned_surgeons_json = ?")
                        case_params.append(_surgeons_json(clean_surgeons))
                    if operating_nurse is not None:
                        case_updates.append("planned_operating_nurse = ?")
                        case_params.append(clean_operating_nurse or None)
                    cursor.execute(
                        f"""
                        UPDATE operation_cases
                        SET {", ".join(case_updates)},
                            last_modified_by = 'operblock',
                            revision = COALESCE(revision, 0) + 1
                        WHERE id = ?
                        """,
                        (*case_params, int(operation_case_id)),
                    )

            if anesthesiologist is not None or anesthetist is not None:
                if not stage_state.get("anesthesia_active"):
                    raise ValueError("Анестезиологическое пособие ещё не начато или уже завершено.")
                anesthesia_row = latest_stage_row(rows, "anesthesia_start")
                if anesthesia_row is None:
                    raise OperBlockConflictError("Начало пособия не найдено. Обновите протокол.")
                payload = _parse_json_dict(anesthesia_row.get("payload_json"))
                if anesthesiologist is not None:
                    set_text(payload, "anesthesiologist", clean_anesthesiologist)
                if anesthetist is not None:
                    set_text(payload, "anesthetist", clean_anesthetist)
                if update_payload(cursor, anesthesia_row, payload):
                    updated += 1
                case_updates: list[str] = []
                case_params: list[Any] = []
                if anesthesiologist is not None:
                    case_updates.append("planned_anesthesiologist = ?")
                    case_params.append(clean_anesthesiologist or None)
                if anesthetist is not None:
                    case_updates.append("planned_anesthetist = ?")
                    case_params.append(clean_anesthetist or None)
                if case_updates:
                    cursor.execute(
                        f"""
                        UPDATE operation_cases
                        SET {", ".join(case_updates)},
                            last_modified_by = 'operblock',
                            revision = COALESCE(revision, 0) + 1
                        WHERE id = ?
                        """,
                        (*case_params, int(operation_case_id)),
                    )

            if updated == 0:
                return {"operation_case_id": int(operation_case_id), "updated": 0}
            return {"operation_case_id": int(operation_case_id), "updated": updated}

        return dict(self.db.run_write_operation(operation, source="operblock_update_operation_staff"))

    def _add_stage_event(
        self,
        operation_case_id: int,
        stage_kind: str,
        *,
        assistance_type: str | None = None,
        anesthesiologist: str | None = None,
        anesthetist: str | None = None,
        operation_name: str | None = None,
        surgeons: list[str] | None = None,
        surgeon: str | None = None,
        operating_nurse: str | None = None,
        transfer_department: str | None = None,
        event_time: Any = None,
        handoff_id: int | None = None,
    ) -> int:
        validate_operblock_runtime_path(self.db)
        clean_kind = str(stage_kind or "").strip()
        if clean_kind not in OPERBLOCK_STAGE_KIND_LABELS:
            raise ValueError("Неизвестный этап операции.")
        event_dt = (
            self._normalize_timeline_event_datetime(event_time)
            if event_time is not None
            else _minute_floor(datetime.now())
        )
        clean_assistance_type = normalize_operblock_anesthesia_type_label(assistance_type)
        clean_anesthesiologist = re.sub(r"\s+", " ", str(anesthesiologist or "").strip())
        clean_anesthetist = re.sub(r"\s+", " ", str(anesthetist or "").strip())
        clean_operation_name = re.sub(r"\s+", " ", str(operation_name or "").strip())
        clean_surgeons = _normalize_stage_text_list(surgeons)
        clean_surgeon = re.sub(r"\s+", " ", str(surgeon or "").strip())
        if clean_surgeon and clean_surgeon.casefold() not in {item.casefold() for item in clean_surgeons}:
            clean_surgeons.append(clean_surgeon)
        clean_operating_nurse = re.sub(r"\s+", " ", str(operating_nurse or "").strip())
        clean_transfer_department = normalize_operblock_transfer_department(transfer_department)

        def operation(cursor: sqlite3.Cursor):
            nonlocal clean_transfer_department
            case = self._assert_active_operation_case_for_update(cursor, operation_case_id)
            if handoff_id is not None:
                case = self._bind_waiting_handoff_to_case(cursor, case, int(handoff_id))
            self._assert_datetime_in_operation_bounds(event_dt, case, entity_label=_stage_label(clean_kind))
            stage_rows = self._fetch_stage_rows_for_case(cursor, int(operation_case_id))
            state = self._stage_state_from_cursor_rows(stage_rows)
            anesthesia_active = bool(state.get("anesthesia_active"))
            surgery_active = bool(state.get("surgery_active"))

            if clean_kind == "anesthesia_start":
                if anesthesia_active:
                    raise ValueError("Анестезиологическое пособие уже начато.")
                started_at = _parse_dt(case.get("started_at"))
                if started_at is None:
                    raise OperBlockConflictError("У операции не задано время начала. Обновите протокол.")
                if not self._operation_has_vitals_between(
                    int(case["admission_id"]),
                    _minute_floor(started_at),
                    cursor=cursor,
                ):
                    raise ValueError("Перед началом пособия введите исходные витальные показатели.")
            elif clean_kind == "anesthesia_end":
                if not anesthesia_active:
                    raise ValueError("Анестезиологическое пособие ещё не начато.")
                if surgery_active:
                    raise ValueError("Перед окончанием пособия завершите операцию.")
                active_start = _parse_dt(state.get("current_anesthesia_start"))
                if active_start is None:
                    raise OperBlockConflictError("Не удалось определить начало пособия. Обновите протокол.")
                if event_dt < _minute_floor(active_start):
                    raise ValueError(
                        f"Конец пособия не может быть раньше начала пособия: "
                        f"{_format_bound_time(_minute_floor(active_start))}."
                    )
                last_surgery_end = _parse_dt(state.get("last_surgery_end"))
                if last_surgery_end is not None and event_dt < _minute_floor(last_surgery_end):
                    raise ValueError(
                        f"Конец пособия не может быть раньше окончания операции: "
                        f"{_format_bound_time(_minute_floor(last_surgery_end))}."
                    )
                self._validate_medications_before_anesthesia_end(
                    cursor,
                    case,
                    _minute_floor(active_start),
                    event_dt,
                )
                self._auto_stop_open_infusions(cursor, case, event_dt)
                if not clean_transfer_department:
                    clean_transfer_department = normalize_operblock_transfer_department(
                        case.get("department_profile")
                    )
                if not clean_transfer_department:
                    raise ValueError("Укажите отделение, куда переводится пациент.")
            elif clean_kind == "surgery_start":
                if not anesthesia_active:
                    raise ValueError("Начать операцию можно только после начала пособия.")
                if surgery_active:
                    raise ValueError("Операция уже начата.")
                active_anesthesia_start = _parse_dt(state.get("current_anesthesia_start"))
                latest_custom_stage = next(
                    (
                        row
                        for row in reversed(stage_rows)
                        if str(row.get("stage_kind") or "") == "custom"
                        and (
                            active_anesthesia_start is None
                            or _minute_floor(row["event_dt"]) >= _minute_floor(active_anesthesia_start)
                        )
                    ),
                    None,
                )
                if latest_custom_stage is not None:
                    latest_custom_dt = _minute_floor(latest_custom_stage["event_dt"])
                    if event_dt < latest_custom_dt:
                        latest_custom_label = (
                            _normalize_operation_stage_label(latest_custom_stage.get("stage_label"))
                            or "предыдущего этапа"
                        )
                        raise ValueError(
                            f"Начало операции не может быть раньше этапа «{latest_custom_label}»: "
                            f"{_format_bound_time(latest_custom_dt)}."
                        )
                self._assert_datetime_in_active_anesthesia_bounds(
                    cursor,
                    event_dt,
                    case,
                    entity_label="Начало операции",
                )
            elif clean_kind == "surgery_end":
                if not surgery_active:
                    raise ValueError("Операция ещё не начата.")
                surgery_start = _parse_dt(state.get("current_surgery_start"))
                if surgery_start is not None and event_dt < _minute_floor(surgery_start):
                    raise ValueError(
                        f"Конец операции не может быть раньше начала операции: "
                        f"{_format_bound_time(_minute_floor(surgery_start))}."
                    )
                self._assert_datetime_in_active_anesthesia_bounds(
                    cursor,
                    event_dt,
                    case,
                    entity_label="Конец операции",
                )

            event_id = self._insert_stage_event(
                cursor,
                case,
                clean_kind,
                event_dt,
                assistance_type=clean_assistance_type,
                anesthesiologist=clean_anesthesiologist,
                anesthetist=clean_anesthetist,
                operation_name=clean_operation_name,
                surgeons=clean_surgeons,
                operating_nurse=clean_operating_nurse,
                transfer_department=clean_transfer_department,
            )
            if (
                clean_kind == "anesthesia_end"
                and _is_rao_transfer_department(clean_transfer_department)
                and case.get("source_rao_admission_id") is None
            ):
                # A post-operative RAO move is now an explicit doctor decision.
                # The local export worker creates a central invitation only after
                # this final case is released and only while the clinical transfer
                # is fresh.  Never occupy a recovery bed or create an admission at
                # the moment anaesthesia ends.
                logger.info(
                    "operblock_rao_transfer_waits_for_central_doctor_acceptance case_id=%s runtime=%s",
                    case.get("operation_case_id"),
                    getattr(getattr(self.db, "runtime_context", None), "mode", ""),
                )
            return event_id

        return int(self.db.run_write_operation(operation, source=f"operblock_stage_{clean_kind}"))

    @staticmethod
    def _operation_stage_event_result(
        *,
        event_id: int,
        case: dict[str, Any],
        label: str,
        event_dt: datetime,
        revision: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        clean_label = _normalize_operation_stage_label(label) or _stage_label(str(payload.get("stage_kind") or ""))
        event_time = _minute_floor(event_dt).isoformat(timespec="seconds")
        return {
            "id": f"timeline_event:{int(event_id)}",
            "source": "timeline_event",
            "source_id": int(event_id),
            "admission_id": int(case["admission_id"]),
            "operation_case_id": int(case["operation_case_id"]),
            "table_code": str(case.get("table_code") or "") or None,
            "event_time": event_time,
            "end_time": None,
            "event_type": "clinical_event",
            "drug_label": clean_label,
            "display_label": clean_label,
            "raw_text": clean_label,
            "dose_value": None,
            "dose_unit": None,
            "volume_ml": None,
            "concentration_text": None,
            "rate_value": None,
            "rate_unit": None,
            "route": None,
            "status": "active",
            "revision": int(revision or 0),
            "created_at": None,
            "updated_at": None,
            "payload": dict(payload or {}),
        }

    def _insert_stage_event(
        self,
        cursor: sqlite3.Cursor,
        case: dict[str, Any],
        stage_kind: str,
        event_dt: datetime,
        *,
        assistance_type: str | None = None,
        anesthesiologist: str | None = None,
        anesthetist: str | None = None,
        operation_name: str | None = None,
        surgeons: list[str] | None = None,
        surgeon: str | None = None,
        operating_nurse: str | None = None,
        transfer_department: str | None = None,
    ) -> int:
        clean_transfer_department = normalize_operblock_transfer_department(transfer_department)
        label = operblock_transfer_stage_label(clean_transfer_department) if stage_kind == "anesthesia_end" else _stage_label(stage_kind)
        payload = {"stage_kind": stage_kind, "label": label}
        if stage_kind == "anesthesia_start":
            self._ensure_case_protocol_number(cursor, case, event_dt)
        if stage_kind == "anesthesia_end" and clean_transfer_department:
            payload["transfer_department"] = clean_transfer_department
            payload["transfer_department_target"] = transfer_department_target_text(clean_transfer_department)
        clean_assistance_type = normalize_operblock_anesthesia_type_label(assistance_type)
        if stage_kind == "anesthesia_start" and clean_assistance_type:
            payload["anesthesia_assistance_type"] = clean_assistance_type
        clean_anesthesiologist = re.sub(r"\s+", " ", str(anesthesiologist or "").strip())
        clean_anesthetist = re.sub(r"\s+", " ", str(anesthetist or "").strip())
        if stage_kind == "anesthesia_start" and clean_anesthesiologist:
            payload["anesthesiologist"] = clean_anesthesiologist
        if stage_kind == "anesthesia_start" and clean_anesthetist:
            payload["anesthetist"] = clean_anesthetist
        clean_operation_name = re.sub(r"\s+", " ", str(operation_name or "").strip())
        clean_surgeons = _normalize_stage_text_list(surgeons)
        clean_surgeon = re.sub(r"\s+", " ", str(surgeon or "").strip())
        if clean_surgeon and clean_surgeon.casefold() not in {item.casefold() for item in clean_surgeons}:
            clean_surgeons.append(clean_surgeon)
        clean_operating_nurse = re.sub(r"\s+", " ", str(operating_nurse or "").strip())
        if stage_kind == "surgery_start" and clean_operation_name:
            payload["operation_name"] = clean_operation_name
        if stage_kind == "surgery_start" and clean_surgeons:
            payload["surgeons"] = clean_surgeons
            payload["surgeon"] = ", ".join(clean_surgeons)
        if stage_kind == "surgery_start" and clean_operating_nurse:
            payload["operating_nurse"] = clean_operating_nurse
        payload_json = self._timeline_payload_json(payload)
        cursor.execute(
            """
            INSERT INTO operblock_timeline_events (
                operation_case_id, admission_id, table_code, event_type, event_time,
                drug_label, display_label, raw_text, status, revision, payload_json,
                created_by_role, created_by_client_id, last_modified_by
            ) VALUES (?, ?, ?, 'clinical_event', ?, ?, ?, ?, 'active', 1, ?,
                      'operblock', ?, 'operblock')
            """,
            (
                int(case["operation_case_id"]),
                int(case["admission_id"]),
                case.get("table_code"),
                event_dt.isoformat(timespec="seconds"),
                label,
                label,
                label,
                payload_json,
                self.client_id,
            ),
        )
        event_id = int(cursor.lastrowid)
        if stage_kind == "surgery_start":
            cursor.execute(
                """
                UPDATE operation_cases
                SET planned_operation_name = ?,
                    planned_surgeons_json = ?,
                    planned_operating_nurse = ?,
                    last_modified_by = 'operblock',
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ?
                """,
                (
                    clean_operation_name or None,
                    _surgeons_json(clean_surgeons),
                    clean_operating_nurse or None,
                    int(case["operation_case_id"]),
                ),
            )
        elif stage_kind == "anesthesia_start":
            cursor.execute(
                """
                UPDATE operation_cases
                SET planned_anesthesia_assistance_type = ?,
                    planned_anesthesiologist = ?,
                    planned_anesthetist = ?,
                    last_modified_by = 'operblock',
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ?
                """,
                (
                    clean_assistance_type or None,
                    clean_anesthesiologist or None,
                    clean_anesthetist or None,
                    int(case["operation_case_id"]),
                ),
            )
        elif stage_kind == "anesthesia_end":
            cursor.execute(
                """
                UPDATE operation_cases
                SET transfer_department = ?,
                    last_modified_by = 'operblock',
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ?
                """,
                (clean_transfer_department or None, int(case["operation_case_id"])),
            )
        return event_id
