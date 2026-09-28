from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any
from .common import (
    OperBlockConflictError,
    _parse_dt,
    _minute_floor,
    _format_bound_time,
    format_operblock_protocol_display,
)


class OperBlockProtocolMixin:
    @staticmethod
    def _ensure_case_protocol_number(cursor: sqlite3.Cursor, case: dict[str, Any], event_dt: datetime) -> tuple[int, str]:
        operation_case_id = int(case["operation_case_id"])
        row = cursor.execute(
            """
            SELECT anesthesia_protocol_number, anesthesia_protocol_date, table_code
            FROM operation_cases
            WHERE id = ?
            """,
            (operation_case_id,),
        ).fetchone()
        if not row:
            raise OperBlockConflictError("Операция не найдена. Обновите протокол.")
        existing_display = format_operblock_protocol_display(
            row["anesthesia_protocol_number"],
            row["anesthesia_protocol_date"],
        )
        if existing_display:
            return int(row["anesthesia_protocol_number"]), str(row["anesthesia_protocol_date"])

        table_code = str(row["table_code"] or case.get("table_code") or "")
        protocol_date = _minute_floor(event_dt).date().isoformat()
        max_row = cursor.execute(
            """
            SELECT MAX(anesthesia_protocol_number) AS max_number
            FROM operation_cases
            WHERE table_code = ?
              AND anesthesia_protocol_date = ?
              AND id <> ?
            """,
            (table_code, protocol_date, operation_case_id),
        ).fetchone()
        protocol_number = int((max_row["max_number"] if max_row else 0) or 0) + 1
        cursor.execute(
            """
            UPDATE operation_cases
            SET anesthesia_protocol_number = ?,
                anesthesia_protocol_date = ?,
                last_modified_by = 'operblock',
                revision = COALESCE(revision, 0) + 1
            WHERE id = ?
              AND (anesthesia_protocol_number IS NULL OR anesthesia_protocol_date IS NULL)
            """,
            (protocol_number, protocol_date, operation_case_id),
        )
        if cursor.rowcount != 1:
            check_row = cursor.execute(
                """
                SELECT anesthesia_protocol_number, anesthesia_protocol_date
                FROM operation_cases
                WHERE id = ?
                """,
                (operation_case_id,),
            ).fetchone()
            if check_row and format_operblock_protocol_display(
                check_row["anesthesia_protocol_number"],
                check_row["anesthesia_protocol_date"],
            ):
                return int(check_row["anesthesia_protocol_number"]), str(check_row["anesthesia_protocol_date"])
            raise OperBlockConflictError("Не удалось присвоить номер протокола. Обновите протокол.")
        case["anesthesia_protocol_number"] = protocol_number
        case["anesthesia_protocol_date"] = protocol_date
        return protocol_number, protocol_date

    def _validate_medications_before_anesthesia_end(
        self,
        cursor: sqlite3.Cursor,
        case: dict[str, Any],
        start_dt: datetime,
        end_dt: datetime,
    ) -> None:
        admission_id = int(case["admission_id"])
        order_rows = cursor.execute(
            """
            SELECT id, datetime, text
            FROM orders
            WHERE admission_id = ?
              AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
            ORDER BY datetime("datetime") ASC, id ASC
            """,
            (admission_id,),
        ).fetchall()
        for row in order_rows:
            order_dt = _parse_dt(row["datetime"])
            if order_dt is None:
                continue
            order_minute = _minute_floor(order_dt)
            label = str(row["text"] or "назначение").strip()
            if order_minute < start_dt:
                continue
            if order_minute > end_dt:
                raise ValueError(
                    f"Нельзя завершить пособие в {_format_bound_time(end_dt)}: назначение «{label}» "
                    f"запланировано на {_format_bound_time(order_minute)}, то есть выходит за рамки пособия. "
                    "Исправьте время назначения или завершите пособие позже."
                )

        event_rows = cursor.execute(
            """
            SELECT id, event_type, event_time, end_time, display_label, drug_label, status
            FROM operblock_timeline_events
            WHERE operation_case_id = ?
              AND event_type IN ('infusion_start', 'infusion_change', 'infusion_stop')
              AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
            ORDER BY datetime(event_time) ASC, id ASC
            """,
            (int(case["operation_case_id"]),),
        ).fetchall()
        for row in event_rows:
            event_dt = _parse_dt(row["event_time"])
            if event_dt is None:
                continue
            event_minute = _minute_floor(event_dt)
            label = str(row["display_label"] or row["drug_label"] or "инфузия").strip()
            if event_minute < start_dt:
                continue
            if event_minute > end_dt:
                raise ValueError(
                    f"Нельзя завершить пособие в {_format_bound_time(end_dt)}: событие «{label}» "
                    f"указано на {_format_bound_time(event_minute)}, то есть выходит за рамки пособия. "
                    "Исправьте время события или завершите пособие позже."
                )
            end_value = _parse_dt(row["end_time"])
            if end_value is not None and _minute_floor(end_value) > end_dt:
                raise ValueError(
                    f"Нельзя завершить пособие в {_format_bound_time(end_dt)}: инфузия «{label}» "
                    f"завершена в {_format_bound_time(_minute_floor(end_value))}, позже конца пособия. "
                    "Исправьте время остановки инфузии или завершите пособие позже."
                )

    def _auto_stop_open_infusions(
        self,
        cursor: sqlite3.Cursor,
        case: dict[str, Any],
        end_dt: datetime,
    ) -> None:
        rows = cursor.execute(
            """
            SELECT
                id, operation_case_id, admission_id, table_code, event_time, drug_label,
                volume_ml, concentration_text, rate_value, rate_unit, route, COALESCE(revision, 0) AS revision
            FROM operblock_timeline_events
            WHERE operation_case_id = ?
              AND event_type = 'infusion_start'
              AND status = 'active'
            ORDER BY datetime(event_time) ASC, id ASC
            """,
            (int(case["operation_case_id"]),),
        ).fetchall()
        for row in rows:
            start_dt = _parse_dt(row["event_time"])
            if start_dt is None or _minute_floor(start_dt) > end_dt:
                continue
            display_label = f"{row['drug_label']} стоп".strip()
            payload_json = self._timeline_payload_json({"auto_stopped_by": "anesthesia_end"})
            cursor.execute(
                """
                INSERT INTO operblock_timeline_events (
                    operation_case_id, admission_id, table_code, event_type, event_time,
                    drug_label, display_label, raw_text, volume_ml, concentration_text, rate_value,
                    rate_unit, route, status, revision, parent_event_id, payload_json,
                    created_by_role, created_by_client_id, last_modified_by
                ) VALUES (?, ?, ?, 'infusion_stop', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, ?, ?,
                          'operblock', ?, 'operblock')
                """,
                (
                    int(row["operation_case_id"]),
                    int(row["admission_id"]),
                    row["table_code"],
                    end_dt.isoformat(timespec="seconds"),
                    row["drug_label"],
                    display_label,
                    display_label,
                    row["volume_ml"],
                    row["concentration_text"],
                    row["rate_value"],
                    row["rate_unit"],
                    row["route"],
                    int(row["id"]),
                    payload_json,
                    self.client_id,
                ),
            )
            cursor.execute(
                """
                UPDATE operblock_timeline_events
                SET status = 'stopped',
                    end_time = ?,
                    revision = COALESCE(revision, 0) + 1,
                    last_modified_by = 'operblock',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                  AND event_type = 'infusion_start'
                  AND status = 'active'
                """,
                (end_dt.isoformat(timespec="seconds"), int(row["id"])),
            )
