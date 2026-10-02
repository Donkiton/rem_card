from __future__ import annotations

import sqlite3
from typing import Any, Mapping, Optional
from rem_card.services.concurrency import assert_revision_matches
from .common import (
    OperBlockConflictError,
    _operblock_order_comment_with_route,
    validate_operblock_runtime_path,
    _now_text,
    _normalize_order_datetime_text,
    _row_to_dict,
)


class OperBlockOrdersMixin:
    def add_order(
        self,
        admission_id: int,
        text: str,
        *,
        preset_payload: Optional[dict[str, Any]] = None,
        route: str | None = None,
        return_row: bool = False,
    ) -> int | dict[str, Any]:
        validate_operblock_runtime_path(self.db)
        clean_text = str(text or "").strip()
        if not clean_text:
            raise ValueError("Текст назначения не заполнен.")
        preset_id = ""
        if isinstance(preset_payload, dict):
            preset_id = str(preset_payload.get("preset_id") or "").strip()
        comment_text = _operblock_order_comment_with_route("", route) if route is not None else ""
        now = _now_text()

        def operation(cursor: sqlite3.Cursor):
            case = self._assert_active_operation_for_admission(cursor, admission_id)
            self._assert_datetime_in_active_anesthesia_bounds(cursor, now, case, entity_label="Время назначения")
            row = cursor.execute(
                """
                SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_sort_order
                FROM orders
                WHERE admission_id = ?
                """,
                (int(admission_id),),
            ).fetchone()
            sort_order = int(row["next_sort_order"] if row and row["next_sort_order"] is not None else 0)
            cursor.execute(
                """
                INSERT INTO orders (
                    admission_id, datetime, text, drug_key, latin, type, status, dose_value, dose_unit,
                    frequency, specific_times, sort_order, draft_sort_order, is_finalized,
                    is_committed, created_at, comment, last_modified_by, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'observation', 'active', 0, '', 1, '[]', ?, ?, 1, 1, ?, ?, 'operblock',
                          STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
                """,
                (
                    int(admission_id),
                    now,
                    clean_text,
                    preset_id or None,
                    clean_text,
                    sort_order,
                    sort_order,
                    now,
                    comment_text,
                ),
            )
            order_id = int(cursor.lastrowid)
            if not return_row:
                return order_id
            row = cursor.execute(
                """
                SELECT
                    id,
                    datetime,
                    text,
                    drug_key,
                    status,
                    is_committed,
                    comment,
                    created_at,
                    updated_at,
                    COALESCE(revision, 0) AS revision
                FROM orders
                WHERE id = ?
                """,
                (order_id,),
            ).fetchone()
            data = _row_to_dict(row)
            display_name = ""
            if isinstance(preset_payload, Mapping):
                display_name = str(
                    preset_payload.get("display_name")
                    or preset_payload.get("label")
                    or preset_payload.get("latin")
                    or ""
                ).strip()
            if display_name:
                data["drug_display_name"] = display_name
            return data

        result = self.db.run_write_operation(operation, source="operblock_add_order")
        return dict(result or {}) if return_row else int(result)

    def update_order_text(
        self,
        admission_id: int,
        order_id: int,
        text: str,
        datetime_text: str | None = None,
        *,
        expected_revision: Optional[int] = None,
        route: str | None = None,
    ) -> int:
        validate_operblock_runtime_path(self.db)
        clean_text = str(text or "").strip()
        if not clean_text:
            raise ValueError("Текст назначения не заполнен.")
        clean_datetime = _normalize_order_datetime_text(datetime_text) if datetime_text is not None else None

        def operation(cursor: sqlite3.Cursor):
            case = self._assert_active_operation_for_admission(cursor, admission_id)
            row = cursor.execute(
                """
                SELECT id, datetime, text, comment, COALESCE(revision, 0) AS revision
                FROM orders
                WHERE id = ?
                  AND admission_id = ?
                  AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                """,
                (int(order_id), int(admission_id)),
            ).fetchone()
            if not row:
                raise OperBlockConflictError("Назначение не найдено или уже удалено.")
            assert_revision_matches(row["revision"], expected_revision)
            effective_datetime = clean_datetime if clean_datetime is not None else row["datetime"]
            self._assert_datetime_in_active_anesthesia_bounds(
                cursor,
                effective_datetime,
                case,
                entity_label="Время назначения",
            )
            revision_clause = ""
            comment_text = (
                _operblock_order_comment_with_route(row["comment"], route)
                if route is not None
                else str(row["comment"] or "")
            )
            if (
                str(row["text"] or "").strip() == clean_text
                and str(row["datetime"] or "") == str(effective_datetime or "")
                and str(row["comment"] or "") == comment_text
            ):
                return int(order_id)
            params: list[Any] = [clean_text, clean_text, clean_datetime, comment_text, int(order_id), int(admission_id)]
            if expected_revision is not None:
                revision_clause = " AND COALESCE(revision, 0) = ?"
                params.append(int(expected_revision))
            cursor.execute(
                f"""
                UPDATE orders
                SET text = ?,
                    latin = ?,
                    "datetime" = COALESCE(?, "datetime"),
                    comment = ?,
                    last_modified_by = 'operblock',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now'),
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ?
                  AND admission_id = ?
                  {revision_clause}
                """,
                tuple(params),
            )
            if cursor.rowcount != 1:
                assert_revision_matches(None, expected_revision)
                raise OperBlockConflictError("Назначение не найдено или уже изменено другим пользователем.")
            return int(order_id)

        return int(self.db.run_write_operation(operation, source="operblock_update_order_text"))

    def delete_order(self, admission_id: int, order_id: int, *, expected_revision: Optional[int] = None) -> int:
        validate_operblock_runtime_path(self.db)

        def operation(cursor: sqlite3.Cursor):
            case = self._assert_active_operation_for_admission(cursor, admission_id)
            self._require_active_anesthesia_interval(cursor, case, entity_label="Удаление назначения")
            row = cursor.execute(
                """
                SELECT id, COALESCE(revision, 0) AS revision
                FROM orders
                WHERE id = ?
                  AND admission_id = ?
                  AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                """,
                (int(order_id), int(admission_id)),
            ).fetchone()
            if not row:
                raise OperBlockConflictError("Назначение не найдено или уже удалено.")
            assert_revision_matches(row["revision"], expected_revision)
            revision_clause = ""
            params: list[Any] = [int(order_id), int(admission_id)]
            if expected_revision is not None:
                revision_clause = " AND COALESCE(revision, 0) = ?"
                params.append(int(expected_revision))
            cursor.execute(
                f"""
                UPDATE orders
                SET status = 'deleted',
                    last_modified_by = 'operblock',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now'),
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ?
                  AND admission_id = ?
                  {revision_clause}
                """,
                tuple(params),
            )
            if cursor.rowcount != 1:
                assert_revision_matches(None, expected_revision)
                raise OperBlockConflictError("Назначение не найдено или уже изменено другим пользователем.")
            return int(order_id)

        return int(self.db.run_write_operation(operation, source="operblock_delete_order"))
