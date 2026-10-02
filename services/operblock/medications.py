from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any
from rem_card.services.operblock_timeline import operation_stage_kind_from_payload
from .common import validate_operblock_runtime_path, _parse_dt, _minute_floor, _parse_json_dict


class OperBlockMedicationHistoryMixin:
    def undo_last_action(self, operation_case_id: int) -> dict[str, Any]:
        validate_operblock_runtime_path(self.db)

        def operation(cursor: sqlite3.Cursor):
            case = self._assert_active_operation_case_for_update(cursor, operation_case_id)
            admission_id = int(case["admission_id"])
            candidates: list[dict[str, Any]] = []

            vital = cursor.execute(
                """
                SELECT id, datetime, updated_at, COALESCE(revision, 0) AS revision
                FROM vitals
                WHERE admission_id = ?
                ORDER BY datetime(COALESCE(updated_at, datetime)) DESC, id DESC
                LIMIT 1
                """,
                (admission_id,),
            ).fetchone()
            if vital:
                action_dt = _parse_dt(vital["updated_at"]) or _parse_dt(vital["datetime"]) or datetime.min
                candidates.append({"kind": "vital", "row": vital, "action_dt": action_dt, "id": int(vital["id"] or 0)})

            order = cursor.execute(
                """
                SELECT id, datetime, updated_at, text, COALESCE(revision, 0) AS revision
                FROM orders
                WHERE admission_id = ?
                  AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                ORDER BY datetime(COALESCE(updated_at, datetime)) DESC, id DESC
                LIMIT 1
                """,
                (admission_id,),
            ).fetchone()
            if order:
                action_dt = _parse_dt(order["updated_at"]) or _parse_dt(order["datetime"]) or datetime.min
                candidates.append({"kind": "order", "row": order, "action_dt": action_dt, "id": int(order["id"] or 0)})

            event = cursor.execute(
                """
                SELECT id, operation_case_id, event_type, event_time, created_at, updated_at,
                       display_label, drug_label, parent_event_id, payload_json, COALESCE(revision, 0) AS revision
                FROM operblock_timeline_events
                WHERE operation_case_id = ?
                  AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                  AND NOT (
                    event_type = 'infusion_stop'
                    AND COALESCE(payload_json, '') LIKE '%auto_stopped_by%'
                  )
                ORDER BY datetime(COALESCE(created_at, updated_at, event_time)) DESC, id DESC
                LIMIT 1
                """,
                (int(operation_case_id),),
            ).fetchone()
            if event:
                action_dt = _parse_dt(event["created_at"]) or _parse_dt(event["updated_at"]) or _parse_dt(event["event_time"]) or datetime.min
                candidates.append({"kind": "timeline_event", "row": event, "action_dt": action_dt, "id": int(event["id"] or 0)})

            if not candidates:
                return {"kind": "none", "message": "Нет действий для отмены."}

            latest = max(candidates, key=lambda item: (item["action_dt"], item["id"]))
            kind = latest["kind"]
            row = latest["row"]
            if kind == "vital":
                cursor.execute("DELETE FROM vitals WHERE id = ?", (int(row["id"]),))
                return {"kind": "vital", "message": "Последние витальные показатели отменены."}
            if kind == "order":
                cursor.execute(
                    """
                    UPDATE orders
                    SET status = 'deleted',
                        last_modified_by = 'operblock',
                        updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now'),
                        revision = COALESCE(revision, 0) + 1
                    WHERE id = ?
                    """,
                    (int(row["id"]),),
                )
                return {"kind": "order", "message": "Последнее назначение отменено."}

            return self._undo_timeline_event(cursor, row)

        return dict(self.db.run_write_operation(operation, source="operblock_undo_last_action"))

    def _undo_timeline_event(self, cursor: sqlite3.Cursor, row: Any) -> dict[str, Any]:
        event_id = int(row["id"])
        event_type = str(row["event_type"] or "")
        label = str(row["display_label"] or row["drug_label"] or "событие").strip()
        payload = _parse_json_dict(row["payload_json"])
        stage_kind = operation_stage_kind_from_payload(payload)

        if event_type == "infusion_start":
            child = cursor.execute(
                """
                SELECT id
                FROM operblock_timeline_events
                WHERE parent_event_id = ?
                  AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                LIMIT 1
                """,
                (event_id,),
            ).fetchone()
            if child:
                raise ValueError("Нельзя отменить старт инфузии, пока у неё есть изменения или остановка. Сначала отмените последнее событие инфузии.")

        if event_type == "infusion_stop":
            parent_id = int(row["parent_event_id"] or 0)
            cursor.execute(
                """
                UPDATE operblock_timeline_events
                SET status = 'deleted',
                    revision = COALESCE(revision, 0) + 1,
                    last_modified_by = 'operblock',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                """,
                (event_id,),
            )
            if parent_id:
                cursor.execute(
                    """
                    UPDATE operblock_timeline_events
                    SET status = 'active',
                        end_time = NULL,
                        revision = COALESCE(revision, 0) + 1,
                        last_modified_by = 'operblock',
                        updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                    WHERE id = ?
                      AND event_type = 'infusion_start'
                    """,
                    (parent_id,),
                )
            return {"kind": "timeline_event", "message": f"Последнее событие «{label}» отменено."}

        if event_type == "clinical_event" and stage_kind == "anesthesia_end":
            event_time = _parse_dt(row["event_time"])
            if event_time is not None:
                auto_stops = cursor.execute(
                    """
                    SELECT id, parent_event_id
                    FROM operblock_timeline_events
                    WHERE event_type = 'infusion_stop'
                      AND event_time = ?
                      AND operation_case_id = ?
                      AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                      AND payload_json LIKE '%auto_stopped_by%'
                    """,
                    (_minute_floor(event_time).isoformat(timespec="seconds"), int(row["operation_case_id"] or 0)),
                ).fetchall()
                for stop in auto_stops:
                    parent_id = int(stop["parent_event_id"] or 0)
                    cursor.execute(
                        """
                        UPDATE operblock_timeline_events
                        SET status = 'deleted',
                            revision = COALESCE(revision, 0) + 1,
                            last_modified_by = 'operblock',
                            updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                        WHERE id = ?
                        """,
                        (int(stop["id"]),),
                    )
                    if parent_id:
                        cursor.execute(
                            """
                            UPDATE operblock_timeline_events
                            SET status = 'active',
                                end_time = NULL,
                                revision = COALESCE(revision, 0) + 1,
                                last_modified_by = 'operblock',
                                updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                            WHERE id = ?
                              AND event_type = 'infusion_start'
                            """,
                            (parent_id,),
                        )

        cursor.execute(
            """
            UPDATE operblock_timeline_events
            SET status = 'deleted',
                revision = COALESCE(revision, 0) + 1,
                last_modified_by = 'operblock',
                updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
            WHERE id = ?
            """,
            (event_id,),
        )
        return {"kind": "timeline_event", "message": f"Последнее событие «{label}» отменено."}
