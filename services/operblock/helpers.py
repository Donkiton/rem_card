from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from typing import Any, Mapping, Optional
from rem_card.services.concurrency import DATA_CONFLICT_MESSAGE, DataConflictError
from .common import (
    OPERBLOCK_TABLES,
    OperBlockConflictError,
    validate_operblock_runtime_path,
    _parse_dt,
    _minute_floor,
    _format_bound_time,
    _parse_json_dict,
    _stage_rows_from_timeline_rows,
    _build_stage_intervals,
    _row_to_dict,
    _sqlite_columns,
)


class OperBlockHelpersMixin:
    def assert_vital_write_allowed(self, admission_id: int, timestamp: datetime) -> None:
        validate_operblock_runtime_path(self.db)

        def operation(cursor: sqlite3.Cursor):
            case = self._assert_active_operation_for_admission(cursor, admission_id)
            self._assert_datetime_in_operation_bounds(timestamp, case, entity_label="Время витальных функций")
            return None

        self.db.run_write_operation(operation, source="operblock_assert_vital_write_allowed")

    def _fetch_stage_rows_for_case(self, cursor: sqlite3.Cursor, operation_case_id: int) -> list[dict[str, Any]]:
        rows = cursor.execute(
            """
            SELECT
                id, operation_case_id, admission_id, table_code, event_type, event_time, end_time,
                drug_label, display_label, raw_text, status, COALESCE(revision, 0) AS revision,
                parent_event_id, payload_json, created_at, updated_at
            FROM operblock_timeline_events
            WHERE operation_case_id = ?
              AND event_type = 'clinical_event'
              AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
            ORDER BY datetime(event_time) ASC, id ASC
            """,
            (int(operation_case_id),),
        ).fetchall()
        return _stage_rows_from_timeline_rows([_row_to_dict(row) for row in rows])

    @staticmethod
    def _active_anesthesia_interval(stage_rows: list[dict[str, Any]]) -> tuple[datetime, datetime | None] | None:
        intervals = _build_stage_intervals(stage_rows).get("anesthesia_intervals") or []
        for interval in reversed(intervals):
            start = _parse_dt((interval or {}).get("start"))
            end = _parse_dt((interval or {}).get("end"))
            if start is not None and end is None:
                return _minute_floor(start), None
        return None

    @staticmethod
    def _all_anesthesia_intervals(stage_rows: list[dict[str, Any]]) -> list[tuple[datetime, datetime | None]]:
        result: list[tuple[datetime, datetime | None]] = []
        for interval in _build_stage_intervals(stage_rows).get("anesthesia_intervals") or []:
            start = _parse_dt((interval or {}).get("start"))
            if start is None:
                continue
            result.append((_minute_floor(start), _minute_floor(_parse_dt((interval or {}).get("end"))) if (interval or {}).get("end") else None))
        return result

    def _require_active_anesthesia_interval(
        self,
        cursor: sqlite3.Cursor,
        case: dict[str, Any],
        *,
        entity_label: str,
    ) -> tuple[datetime, datetime | None]:
        stage_rows = self._fetch_stage_rows_for_case(cursor, int(case["operation_case_id"]))
        interval = self._active_anesthesia_interval(stage_rows)
        if interval is None:
            raise ValueError(
                f"{entity_label}: сначала нажмите «Начать пособие». "
                "До начала анестезиологического пособия назначения недоступны."
            )
        return interval

    def _assert_datetime_in_active_anesthesia_bounds(
        self,
        cursor: sqlite3.Cursor,
        value: Any,
        case: dict[str, Any],
        *,
        entity_label: str,
    ) -> None:
        timestamp = _parse_dt(value)
        if timestamp is None:
            raise ValueError(f"{entity_label}: укажите корректное время.")
        self._assert_datetime_in_operation_bounds(timestamp, case, entity_label=entity_label)
        start, end = self._require_active_anesthesia_interval(cursor, case, entity_label=entity_label)
        timestamp_minute = _minute_floor(timestamp)
        if timestamp_minute < start:
            raise ValueError(
                f"{entity_label} не может быть раньше начала пособия: {_format_bound_time(start)}."
            )
        if end is not None and timestamp_minute > end:
            raise ValueError(
                f"{entity_label} не может быть позже окончания пособия: {_format_bound_time(end)}."
            )

    @staticmethod
    def _stage_state_from_cursor_rows(stage_rows: list[dict[str, Any]]) -> dict[str, Any]:
        return _build_stage_intervals(stage_rows)

    def _fetch_operblock_timeline_event_rows(
        self,
        admission_id: int,
        *,
        operation_case_id: int | None = None,
    ) -> list[dict[str, Any]]:
        if not self._operblock_timeline_events_table_exists():
            return []
        params: list[Any] = [int(admission_id)]
        case_clause = ""
        if operation_case_id:
            case_clause = "AND operation_case_id = ?"
            params.append(int(operation_case_id))
        rows = self.db.fetch_all_remcard(
            f"""
            SELECT
                id, operation_case_id, admission_id, table_code, event_type, event_time, end_time,
                drug_label, display_label, raw_text, dose_value, dose_unit, volume_ml,
                concentration_text, rate_value, rate_unit, route, status, COALESCE(revision, 0) AS revision,
                source_order_id, parent_event_id, payload_json, created_at, updated_at
            FROM operblock_timeline_events
            WHERE admission_id = ?
              {case_clause}
              AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
            ORDER BY datetime(event_time) ASC, id ASC
            """,
            tuple(params),
        )
        return [_row_to_dict(row) for row in rows]

    def _operblock_timeline_events_table_exists(self) -> bool:
        cached = getattr(self, "_timeline_events_table_exists_cache", None)
        if cached is not None:
            return bool(cached)
        row = self.db.fetch_one_remcard(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operblock_timeline_events'"
        )
        exists = bool(row)
        self._timeline_events_table_exists_cache = exists
        return exists

    @staticmethod
    def _normalize_timeline_event_datetime(value: Any) -> datetime:
        parsed = _parse_dt(value)
        if parsed is None:
            raise ValueError("Укажите корректное время события.")
        return _minute_floor(parsed)

    @staticmethod
    def _timeline_payload_json(payload: Optional[dict[str, Any]]) -> str | None:
        if not payload:
            return None
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)

    @staticmethod
    def _payload_is_gas(payload: Mapping[str, Any] | dict[str, Any] | None) -> bool:
        if not isinstance(payload, Mapping):
            return False
        return str(payload.get("kind") or "").strip().casefold() == "gas"

    @staticmethod
    def _text_is_oxygen(value: Any) -> bool:
        text = str(value or "").strip().casefold().replace("ё", "е")
        if not text:
            return False
        if "кислород" in text or re.search(r"(?<![0-9a-zа-я])oxygen(?![0-9a-zа-я])", text):
            return True
        return bool(re.search(r"(?<![0-9a-zа-я])(?:o|о)\s*2(?![0-9a-zа-я])", text))

    @classmethod
    def _payload_is_oxygen(cls, payload: Mapping[str, Any] | dict[str, Any] | None, *texts: Any) -> bool:
        data = payload if isinstance(payload, Mapping) else {}
        subtype = str(data.get("gas_subtype") or data.get("gas_kind") or data.get("subtype") or "").strip()
        if subtype and cls._text_is_oxygen(subtype):
            return True
        if isinstance(data.get("is_oxygen"), bool) and data.get("is_oxygen"):
            return True
        for key in (
            "preset_id",
            "source_drug_id",
            "label",
            "display_name",
            "latin",
            "drug_label",
            "display_label",
            "raw_text",
        ):
            if cls._text_is_oxygen(data.get(key)):
                return True
        return any(cls._text_is_oxygen(text) for text in texts)

    def _active_gas_start_for_case(
        self,
        cursor: sqlite3.Cursor,
        admission_id: int,
        operation_case_id: int,
        *,
        oxygen: bool | None = None,
    ):
        rows = cursor.execute(
            """
            SELECT
                id, operation_case_id, admission_id, table_code, event_time, drug_label,
                display_label, volume_ml, concentration_text, rate_value, rate_unit, route, status,
                payload_json, COALESCE(revision, 0) AS revision
            FROM operblock_timeline_events
            WHERE admission_id = ?
              AND operation_case_id = ?
              AND event_type = 'infusion_start'
              AND status = 'active'
            ORDER BY datetime(event_time) ASC, id ASC
            """,
            (int(admission_id), int(operation_case_id)),
        ).fetchall()
        for row in rows:
            payload = _parse_json_dict(row["payload_json"])
            if not self._payload_is_gas(payload):
                continue
            if oxygen is not None and self._payload_is_oxygen(payload, row["drug_label"], row["display_label"]) != bool(oxygen):
                continue
            return row
        return None

    def _assert_active_operation_for_case_admission(
        self,
        cursor: sqlite3.Cursor,
        admission_id: int,
        operation_case_id: int,
    ) -> dict[str, Any]:
        case = self._assert_active_operation_for_admission(cursor, admission_id)
        if int(case.get("operation_case_id") or 0) != int(operation_case_id):
            raise OperBlockConflictError("Операция изменена другим рабочим местом. Обновите протокол.")
        return case

    def _assert_active_operation_case_for_update(
        self,
        cursor: sqlite3.Cursor,
        operation_case_id: int,
    ) -> dict[str, Any]:
        handoff_expr = self._operation_case_column_expr("handoff_id")
        source_rao_expr = self._operation_case_column_expr("source_rao_admission_id")
        resolved_rao_expr = self._operation_case_column_expr("resolved_rao_admission_id")
        row = cursor.execute(
            f"""
            SELECT
                oc.id AS operation_case_id,
                oc.patient_id,
                oc.admission_id,
                oc.table_code,
                oc.status AS case_status,
                oc.started_at,
                oc.ended_at,
                oc.anesthesia_protocol_number,
                oc.anesthesia_protocol_date,
                oc.transfer_department,
                {handoff_expr},
                {source_rao_expr},
                {resolved_rao_expr},
                a.department_profile,
                COALESCE(a.revision, 0) AS admission_revision,
                COALESCE(oc.revision, 0) AS revision
            FROM operation_cases oc
            JOIN admissions a ON a.id = oc.admission_id
            WHERE oc.id = ?
              AND oc.status = 'active'
            """,
            (int(operation_case_id),),
        ).fetchone()
        if not row:
            raise OperBlockConflictError("Активный случай в операционной не найден. Обновите список оперблока.")
        case = _row_to_dict(row)
        table_code = self._validate_table_code(str(case.get("table_code") or ""))
        assignment = cursor.execute(
            """
            SELECT id
            FROM operation_table_assignments
            WHERE operation_case_id = ?
              AND table_code = ?
              AND status = 'active'
              AND released_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(case["operation_case_id"]), table_code),
        ).fetchone()
        if not assignment:
            raise OperBlockConflictError("Операционный стол уже освобождён другим рабочим местом. Обновите протокол.")
        case["table_code"] = table_code
        return case

    @staticmethod
    def _assert_infusion_event_not_before_start(event_dt: datetime, start_row: Any) -> None:
        start_dt = _parse_dt(start_row["event_time"])
        if start_dt is None:
            raise OperBlockConflictError("Не удалось определить начало инфузии. Обновите протокол.")
        if _minute_floor(event_dt) < _minute_floor(start_dt):
            raise ValueError("Время события инфузии не может быть раньше старта инфузии.")

    @staticmethod
    def _assert_infusion_stop_not_before_latest_change(
        cursor: sqlite3.Cursor,
        start_event_id: int,
        event_dt: datetime,
    ) -> None:
        latest_change = cursor.execute(
            """
            SELECT event_time
            FROM operblock_timeline_events
            WHERE parent_event_id = ?
              AND event_type = 'infusion_change'
              AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
            ORDER BY datetime(event_time) DESC, id DESC
            LIMIT 1
            """,
            (int(start_event_id),),
        ).fetchone()
        latest_dt = _parse_dt(latest_change["event_time"]) if latest_change else None
        if latest_dt is not None and _minute_floor(event_dt) < _minute_floor(latest_dt):
            raise ValueError(
                "Время остановки инфузии не может быть раньше последнего изменения: "
                f"{_format_bound_time(_minute_floor(latest_dt))}."
            )

    def _get_infusion_start_for_update(
        self,
        cursor: sqlite3.Cursor,
        start_event_id: int,
        *,
        allowed_statuses: set[str],
    ):
        statuses = tuple(sorted(str(status) for status in allowed_statuses if str(status)))
        if not statuses:
            raise ValueError("Не указан допустимый статус инфузии.")
        placeholders = ", ".join("?" for _ in statuses)
        row = cursor.execute(
            f"""
            SELECT
                id, operation_case_id, admission_id, table_code, event_time, drug_label,
                volume_ml, concentration_text, rate_value, rate_unit, route, status,
                payload_json, COALESCE(revision, 0) AS revision
            FROM operblock_timeline_events
            WHERE id = ?
              AND event_type = 'infusion_start'
              AND status IN ({placeholders})
            """,
            (int(start_event_id), *statuses),
        ).fetchone()
        if not row:
            raise OperBlockConflictError("Инфузия не найдена или уже удалена. Обновите протокол.")
        return row

    def _get_active_infusion_start_for_update(self, cursor: sqlite3.Cursor, start_event_id: int):
        try:
            return self._get_infusion_start_for_update(cursor, start_event_id, allowed_statuses={"active"})
        except OperBlockConflictError as exc:
            raise OperBlockConflictError("Активная инфузия не найдена или уже остановлена. Обновите протокол.") from exc

    @staticmethod
    def _bump_infusion_start_revision(
        cursor: sqlite3.Cursor,
        start_event_id: int,
        expected_revision: int,
    ) -> None:
        cursor.execute(
            """
            UPDATE operblock_timeline_events
            SET revision = COALESCE(revision, 0) + 1,
                last_modified_by = 'operblock',
                updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
            WHERE id = ?
              AND event_type = 'infusion_start'
              AND status = 'active'
              AND COALESCE(revision, 0) = ?
            """,
            (int(start_event_id), int(expected_revision)),
        )
        if cursor.rowcount != 1:
            raise DataConflictError(DATA_CONFLICT_MESSAGE)

    def _operation_case_columns(self) -> set[str]:
        cached = getattr(self, "_operation_case_columns_cache", None)
        if cached is not None:
            return set(cached)
        columns: set[str] = set()
        try:
            columns = {
                str(row[1])
                for row in self.db.fetch_all_remcard('PRAGMA table_info("operation_cases")')
                if row and row[1]
            }
        except Exception:
            columns = set()
        if not columns:
            conn = getattr(self.db, "_remcard_conn", None)
            try:
                if conn is not None:
                    columns = _sqlite_columns(conn, "operation_cases")
            except Exception:
                columns = set()
        self._operation_case_columns_cache = set(columns)
        return set(columns)

    def _operation_case_column_expr(self, column_name: str, *, alias: str | None = None) -> str:
        clean_column = re.sub(r"[^0-9A-Za-z_]+", "", str(column_name or ""))
        clean_alias = re.sub(r"[^0-9A-Za-z_]+", "", str(alias or clean_column))
        if not clean_column or not clean_alias:
            return "NULL"
        if clean_column in self._operation_case_columns():
            return f"oc.{clean_column}"
        return f"NULL AS {clean_alias}"

    def _get_case_row(self, operation_case_id: int) -> dict[str, Any]:
        validate_operblock_runtime_path(self.db)
        planned_assistance_expr = self._operation_case_column_expr("planned_anesthesia_assistance_type")
        row = self.db.fetch_one_remcard(
            f"""
            SELECT
                oc.id AS operation_case_id,
                oc.patient_id,
                oc.admission_id,
                oc.table_code,
                oc.status AS case_status,
                oc.started_at,
                oc.ended_at,
                t.display_name AS table_display_name,
                p.full_name,
                p.birth_date,
                a.history_number,
                a.patient_gender,
                a.patient_age,
                a.patient_months,
                a.patient_age_unit,
                a.diagnosis_code,
                a.diagnosis_text,
                oc.planned_operation_name,
                {planned_assistance_expr},
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
                oc.preop_spo2,
                COALESCE(oc.preop_save_initial_vitals, 1) AS preop_save_initial_vitals,
                oc.anesthesia_protocol_number,
                oc.anesthesia_protocol_date,
                oc.transfer_department,
                a.department_profile
            FROM operation_cases oc
            JOIN operating_tables t ON t.code = oc.table_code
            JOIN admissions a ON a.id = oc.admission_id
            JOIN patients p ON p.id = oc.patient_id
            WHERE oc.id = ?
            """,
            (int(operation_case_id),),
        )
        if not row:
            raise OperBlockConflictError("Операция не найдена.")
        return _row_to_dict(row)

    def get_latest_vital_values(self, admission_id: int) -> dict[str, Any]:
        row = self.db.fetch_one_remcard(
            """
            SELECT sys, dia, pulse, spo2, datetime
            FROM vitals
            WHERE admission_id = ?
              AND (sys IS NOT NULL OR dia IS NOT NULL OR pulse IS NOT NULL OR spo2 IS NOT NULL)
            ORDER BY CAST(STRFTIME('%s', datetime) AS INTEGER) DESC, id DESC
            LIMIT 1
            """,
            (int(admission_id),),
        )
        data = _row_to_dict(row)
        return {
            "ad": self._format_ad(data.get("sys"), data.get("dia")),
            "pulse": data.get("pulse"),
            "spo2": data.get("spo2"),
            "datetime": data.get("datetime"),
        }

    def _get_latest_case_row_for_admission(self, admission_id: int) -> dict[str, Any] | None:
        validate_operblock_runtime_path(self.db)
        row = self.db.fetch_one_remcard(
            """
            SELECT
                oc.id AS operation_case_id,
                oc.patient_id,
                oc.admission_id,
                oc.table_code,
                oc.status AS case_status,
                oc.started_at,
                oc.ended_at,
                t.display_name AS table_display_name
            FROM operation_cases oc
            JOIN operating_tables t ON t.code = oc.table_code
            WHERE oc.admission_id = ?
            ORDER BY CASE WHEN oc.status = 'active' THEN 0 ELSE 1 END, oc.id DESC
            LIMIT 1
            """,
            (int(admission_id),),
        )
        return _row_to_dict(row) if row else None

    @staticmethod
    def _format_ad(sys_value: Any, dia_value: Any) -> str:
        if sys_value is None and dia_value is None:
            return ""
        left = "" if sys_value is None else str(sys_value)
        right = "" if dia_value is None else str(dia_value)
        return f"{left}/{right}".strip("/")

    @staticmethod
    def _validate_table_code(table_code: str) -> str:
        code = str(table_code or "").strip().lower()
        allowed = {table["code"] for table in OPERBLOCK_TABLES}
        if code not in allowed:
            raise ValueError("Неизвестный операционный стол.")
        return code

    def _assert_active_operation_for_admission(
        self,
        cursor: sqlite3.Cursor,
        admission_id: int,
    ) -> dict[str, Any]:
        row = cursor.execute(
            """
            SELECT
                id AS operation_case_id,
                admission_id,
                table_code,
                status AS case_status,
                started_at,
                ended_at,
                COALESCE(revision, 0) AS revision
            FROM operation_cases
            WHERE admission_id = ?
              AND status = 'active'
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(admission_id),),
        ).fetchone()
        if not row:
            latest = cursor.execute(
                """
                SELECT status, ended_at
                FROM operation_cases
                WHERE admission_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(admission_id),),
            ).fetchone()
            if latest and str(latest["status"] or "") == "closed":
                raise OperBlockConflictError("Случай в операционной уже закрыт другим рабочим местом. Обновите протокол.")
            raise OperBlockConflictError("Активная операция для пациента не найдена. Обновите список оперблока.")

        case = _row_to_dict(row)
        table_code = self._validate_table_code(str(case.get("table_code") or ""))
        assignment = cursor.execute(
            """
            SELECT id
            FROM operation_table_assignments
            WHERE operation_case_id = ?
              AND table_code = ?
              AND status = 'active'
              AND released_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(case["operation_case_id"]), table_code),
        ).fetchone()
        if not assignment:
            raise OperBlockConflictError("Операционный стол уже освобождён другим рабочим местом. Обновите протокол.")
        case["table_code"] = table_code
        return case

    def _assert_datetime_in_operation_bounds(
        self,
        value: Any,
        case: dict[str, Any],
        *,
        entity_label: str,
    ) -> None:
        timestamp = _parse_dt(value)
        if timestamp is None:
            raise ValueError(f"{entity_label}: укажите корректное время.")
        started_at = _parse_dt(case.get("started_at"))
        ended_at = _parse_dt(case.get("ended_at"))
        if started_at is None:
            raise OperBlockConflictError("У операции не задано время начала. Обновите протокол.")
        timestamp_minute = _minute_floor(timestamp)
        start_minute = _minute_floor(started_at)
        if timestamp_minute < start_minute:
            raise ValueError(
                f"{entity_label} не может быть раньше поступления пациента в операционную: {_format_bound_time(start_minute)}."
            )
        if ended_at is not None:
            end_minute = _minute_floor(ended_at)
            if timestamp_minute > end_minute:
                raise ValueError(
                    f"{entity_label} не может быть позже закрытия случая в операционной: {_format_bound_time(end_minute)}."
                )
