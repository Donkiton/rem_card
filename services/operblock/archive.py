from __future__ import annotations

import os
import sqlite3
from contextlib import nullcontext
from datetime import datetime
from typing import Any
from rem_card.app.logger import logger
from .common import OperBlockConflictError, validate_operblock_runtime_path, _now_text, _parse_dt, _row_to_dict


class OperBlockArchiveLifecycleMixin:
    def list_archived_operation_cases(
        self,
        start_dt: str | None = None,
        end_dt: str | None = None,
    ) -> list[dict[str, Any]]:
        validate_operblock_runtime_path(self.db)
        result: list[dict[str, Any]] = []
        current_db_path = self._current_db_path()
        current_key = os.path.normcase(current_db_path) if current_db_path else ""
        db_paths = self.get_archive_db_paths_for_period(start_dt, end_dt) if start_dt and end_dt else self._iter_archive_db_paths(include_current=True)
        if not db_paths and current_db_path:
            db_paths = [current_db_path]

        for db_path in db_paths:
            abs_path = os.path.abspath(str(db_path or ""))
            is_current = bool(current_key) and os.path.normcase(abs_path) == current_key
            try:
                if is_current:
                    query, params = self._build_archive_cases_query(
                        operation_case_columns=self._current_archive_table_columns("operation_cases"),
                        start_dt=start_dt,
                        end_dt=end_dt,
                    )
                    rows = [_row_to_dict(row) for row in self.db.fetch_all_remcard(query, params)]
                else:
                    rows = self._fetch_archive_case_rows_from_db(abs_path, start_dt=start_dt, end_dt=end_dt)
                for row in rows:
                    result.append(
                        self._archive_case_payload(
                            row,
                            db_path=abs_path,
                            is_external=not is_current,
                        )
                    )
            except Exception as exc:
                logger.warning("Skipping operblock archive DB %s due to read error: %s", abs_path, exc)

        result.sort(
            key=lambda item: (
                0 if str(item.get("status") or "").strip().lower() == "active" else 1,
                _parse_dt(item.get("ended_at")) or _parse_dt(item.get("started_at")) or datetime.min,
                int(item.get("source_operation_case_id") or item.get("operation_case_id") or 0),
            ),
            reverse=False,
        )
        active = [item for item in result if str(item.get("status") or "").strip().lower() == "active"]
        archived = [item for item in result if str(item.get("status") or "").strip().lower() != "active"]
        archived.sort(
            key=lambda item: (
                _parse_dt(item.get("ended_at")) or _parse_dt(item.get("started_at")) or datetime.min,
                int(item.get("source_operation_case_id") or item.get("operation_case_id") or 0),
            ),
            reverse=True,
        )
        return active + archived

    def list_archived_operation_cases_page(
        self,
        *,
        start_dt: str | None = None,
        end_dt: str | None = None,
        page: int = 1,
        page_size: int = 50,
        table_code: str | None = None,
        search_query: str = "",
        search_name: str = "",
        search_ib: str = "",
        search_diag: str = "",
    ) -> dict[str, Any]:
        validate_operblock_runtime_path(self.db)
        page_size = max(1, int(page_size or 50))
        page = max(1, int(page or 1))
        offset = (page - 1) * page_size
        fetch_limit = offset + page_size
        clean_table_code = self._validate_table_code(table_code) if table_code else None

        result: list[dict[str, Any]] = []
        total_count = 0
        current_db_path = self._current_db_path()
        current_key = os.path.normcase(current_db_path) if current_db_path else ""
        # Period filtering happens in the count/page SQL.  Running the
        # preselector here would open every archive once for EXISTS and then
        # reopen matching files for the actual page.
        db_paths = self._iter_archive_db_paths(include_current=True)
        if not db_paths and current_db_path:
            db_paths = [current_db_path]
        use_global_merge = len(db_paths) > 1

        for db_path in db_paths:
            abs_path = os.path.abspath(str(db_path or ""))
            is_current = bool(current_key) and os.path.normcase(abs_path) == current_key
            try:
                if is_current:
                    scope_factory = getattr(self.db, "central_read_snapshot_scope", None)
                    if not callable(scope_factory):
                        scope_factory = getattr(self.db, "central_read_scope", None)
                    read_scope = (
                        scope_factory("operblock_archive_page")
                        if callable(scope_factory)
                        else nullcontext()
                    )
                    with read_scope:
                        tables = self._current_archive_table_names()
                        admission_columns = self._current_archive_table_columns("admissions")
                        operation_case_columns = self._current_archive_table_columns("operation_cases")
                        count_query, count_params = self._build_archive_cases_query(
                            tables=tables,
                            admission_columns=admission_columns,
                            operation_case_columns=operation_case_columns,
                            start_dt=start_dt,
                            end_dt=end_dt,
                            table_code=clean_table_code,
                            search_query=search_query,
                            search_name=search_name,
                            search_ib=search_ib,
                            search_diag=search_diag,
                            count_only=True,
                            end_exclusive=True,
                        )
                        total_count += self._fetch_count_from_manager(count_query, count_params)
                        query, params = self._build_archive_cases_query(
                            tables=tables,
                            admission_columns=admission_columns,
                            operation_case_columns=operation_case_columns,
                            start_dt=start_dt,
                            end_dt=end_dt,
                            table_code=clean_table_code,
                            search_query=search_query,
                            search_name=search_name,
                            search_ib=search_ib,
                            search_diag=search_diag,
                            limit=fetch_limit if use_global_merge else page_size,
                            offset=0 if use_global_merge else offset,
                            end_exclusive=True,
                        )
                        rows = [_row_to_dict(row) for row in self.db.fetch_all_remcard(query, params)]
                else:
                    archived_count, rows = self._fetch_archive_case_page_from_db(
                        abs_path,
                        start_dt=start_dt,
                        end_dt=end_dt,
                        table_code=clean_table_code,
                        search_query=search_query,
                        search_name=search_name,
                        search_ib=search_ib,
                        search_diag=search_diag,
                        limit=fetch_limit if use_global_merge else page_size,
                        offset=0 if use_global_merge else offset,
                    )
                    total_count += archived_count
                for row in rows:
                    result.append(
                        self._archive_case_payload(
                            row,
                            db_path=abs_path,
                            is_external=not is_current,
                        )
                    )
            except Exception as exc:
                logger.warning("Skipping operblock archive DB %s due to paged read error: %s", abs_path, exc)

        records = self._sort_archive_case_payloads(result)
        if use_global_merge:
            records = records[offset : offset + page_size]
        return {
            "records": records,
            "total_count": int(total_count),
            "page": page,
            "page_size": page_size,
        }

    def _fetch_count_from_manager(self, query: str, params: tuple[Any, ...]) -> int:
        rows = self.db.fetch_all_remcard(query, params)
        if not rows:
            return 0
        row = rows[0]
        try:
            return int(row["total_count"] or 0)
        except Exception:
            try:
                return int(row[0] or 0)
            except Exception:
                return 0

    def _current_archive_table_names(self) -> set[str]:
        try:
            rows = self.db.fetch_all_remcard(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        except Exception:
            return {"operating_tables", "admissions", "patients", "operation_cases"}
        result: set[str] = set()
        for row in rows or []:
            try:
                name = row["name"]
            except Exception:
                try:
                    name = row[0]
                except Exception:
                    name = None
            if name:
                result.add(str(name))
        return result

    def _current_archive_table_columns(self, table_name: str) -> set[str]:
        # The archive reader supports older rotated files.  Inspect each
        # requested table rather than assuming operation_cases evolved with
        # admissions, so optional transfer fields can be projected as NULL.
        if table_name not in {"admissions", "operation_cases"}:
            return set()
        try:
            rows = self.db.fetch_all_remcard(f"PRAGMA table_info({table_name})")
        except Exception:
            return {"unit_scope"} if table_name == "admissions" else set()
        result: set[str] = set()
        for row in rows or []:
            try:
                name = row["name"]
            except Exception:
                try:
                    name = row[1]
                except Exception:
                    name = None
            if name:
                result.add(str(name))
        return result

    @staticmethod
    def _sort_archive_case_payloads(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        active = [
            item
            for item in items
            if str(item.get("status") or "").strip().lower() == "active"
        ]
        archived = [
            item
            for item in items
            if str(item.get("status") or "").strip().lower() != "active"
        ]
        active.sort(
            key=lambda item: (
                _parse_dt(item.get("started_at")) or datetime.min,
                int(item.get("source_operation_case_id") or item.get("operation_case_id") or 0),
            ),
            reverse=True,
        )
        archived.sort(
            key=lambda item: (
                _parse_dt(item.get("ended_at")) or _parse_dt(item.get("started_at")) or datetime.min,
                int(item.get("source_operation_case_id") or item.get("operation_case_id") or 0),
            ),
            reverse=True,
        )
        return active + archived

    def restore_archived_operation_case(self, operation_case_id: int) -> dict[str, int]:
        validate_operblock_runtime_path(self.db)
        now = _now_text()

        def operation(cursor: sqlite3.Cursor):
            case = cursor.execute(
                """
                SELECT id, admission_id, table_code, status
                FROM operation_cases
                WHERE id = ?
                """,
                (int(operation_case_id),),
            ).fetchone()
            if not case:
                raise OperBlockConflictError("Архивный случай не найден.")
            if str(case["status"] or "") != "closed":
                raise OperBlockConflictError("Случай уже активен или не может быть восстановлен.")
            table_code = self._validate_table_code(str(case["table_code"] or ""))
            occupied = cursor.execute(
                """
                SELECT id
                FROM operation_cases
                WHERE table_code = ?
                  AND status = 'active'
                LIMIT 1
                """,
                (table_code,),
            ).fetchone()
            if occupied:
                raise OperBlockConflictError("Нельзя вернуть пациента: операционный стол сейчас занят.")
            cursor.execute(
                """
                UPDATE operation_cases
                SET status = 'active',
                    ended_at = NULL,
                    last_modified_by = 'operblock',
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ?
                  AND status = 'closed'
                """,
                (int(operation_case_id),),
            )
            if cursor.rowcount != 1:
                raise OperBlockConflictError("Случай изменён другим рабочим местом. Обновите архив.")
            cursor.execute(
                """
                INSERT INTO operation_table_assignments (
                    operation_case_id, table_code, assigned_at, status,
                    created_by_role, created_by_client_id, last_modified_by
                ) VALUES (?, ?, ?, 'active', 'operblock', ?, 'operblock')
                """,
                (int(operation_case_id), table_code, now, self.client_id),
            )
            admission_id = int(case["admission_id"])
            cursor.execute(
                """
                UPDATE admissions
                SET is_active = 1,
                    updated_at = ?,
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ?
                """,
                (now, admission_id),
            )
            cursor.execute(
                """
                INSERT INTO patient_status_events (
                    admission_id, status, reason_type, reason_text, start_time,
                    created_by, last_modified_by
                ) VALUES (?, 'OR', 'operblock_restore', 'Возврат в операционную из архива', ?, 'operblock', 'operblock')
                """,
                (admission_id, now),
            )
            return {"operation_case_id": int(operation_case_id), "admission_id": admission_id}

        return dict(self.db.run_write_operation(operation, source="operblock_restore_archived_case"))

    @staticmethod
    def _table_exists_for_delete(cursor: sqlite3.Cursor, table_name: str) -> bool:
        row = cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
            (table_name,),
        ).fetchone()
        return row is not None

    @classmethod
    def _delete_where_in(cls, cursor: sqlite3.Cursor, table_name: str, column_name: str, values: list[int]) -> int:
        clean_values = [int(value) for value in values if value is not None]
        if not clean_values or not cls._table_exists_for_delete(cursor, table_name):
            return 0
        placeholders = ", ".join("?" for _ in clean_values)
        cursor.execute(
            f"DELETE FROM {table_name} WHERE {column_name} IN ({placeholders})",
            tuple(clean_values),
        )
        return int(cursor.rowcount or 0)

    @classmethod
    def _hard_delete_archived_operation_cases(
        cls,
        cursor: sqlite3.Cursor,
        case_rows: list[sqlite3.Row],
    ) -> dict[str, int]:
        case_ids = sorted({int(row["id"]) for row in case_rows if row["id"] is not None})
        admission_ids = sorted({int(row["admission_id"]) for row in case_rows if row["admission_id"] is not None})
        patient_ids = sorted({int(row["patient_id"]) for row in case_rows if row["patient_id"] is not None})
        if not case_ids:
            return {"deleted": 0}

        cls._delete_where_in(cursor, "operblock_timeline_events", "operation_case_id", case_ids)
        cls._delete_where_in(cursor, "operation_table_assignments", "operation_case_id", case_ids)

        deleted_cases = cls._delete_where_in(cursor, "operation_cases", "id", case_ids)

        removable_admission_ids: list[int] = []
        if admission_ids:
            placeholders = ", ".join("?" for _ in admission_ids)
            remaining_rows = cursor.execute(
                f"""
                SELECT DISTINCT admission_id
                FROM operation_cases
                WHERE admission_id IN ({placeholders})
                """,
                tuple(admission_ids),
            ).fetchall()
            remaining_admission_ids = {int(row["admission_id"]) for row in remaining_rows}
            removable_admission_ids = [admission_id for admission_id in admission_ids if admission_id not in remaining_admission_ids]

        if removable_admission_ids:
            if cls._table_exists_for_delete(cursor, "procedures"):
                placeholders = ", ".join("?" for _ in removable_admission_ids)
                procedure_rows = cursor.execute(
                    f"SELECT id FROM procedures WHERE admission_id IN ({placeholders})",
                    tuple(removable_admission_ids),
                ).fetchall()
                procedure_ids = [int(row["id"]) for row in procedure_rows]
                for table_name in (
                    "procedure_consents",
                    "procedure_cvc",
                    "procedure_lumbar_puncture",
                    "procedure_transfusion",
                ):
                    cls._delete_where_in(cursor, table_name, "procedure_id", procedure_ids)
                cls._delete_where_in(cursor, "procedures", "id", procedure_ids)

            order_ids: list[int] = []
            if cls._table_exists_for_delete(cursor, "orders"):
                placeholders = ", ".join("?" for _ in removable_admission_ids)
                order_rows = cursor.execute(
                    f"SELECT id FROM orders WHERE admission_id IN ({placeholders})",
                    tuple(removable_admission_ids),
                ).fetchall()
                order_ids = [int(row["id"]) for row in order_rows]
            cls._delete_where_in(cursor, "administrations", "order_id", order_ids)
            cls._delete_where_in(cursor, "orders", "id", order_ids)

            for table_name in (
                "order_audit_log",
                "change_log",
                "medical_audit_log",
                "patient_status_events",
                "vital_settings",
                "vitals",
                "fluids",
                "operations",
                "ivl_episodes",
                "transfusions",
                "clinical_events",
                "devices",
                "respiratory_support",
                "lab_data",
                "lab_orders",
                "diet_plan",
                "diet_plan_versions",
                "oral_intake_events",
            ):
                cls._delete_where_in(cursor, table_name, "admission_id", removable_admission_ids)

            if cls._table_exists_for_delete(cursor, "beds"):
                placeholders = ", ".join("?" for _ in removable_admission_ids)
                cursor.execute(
                    f"UPDATE beds SET current_admission_id = NULL WHERE current_admission_id IN ({placeholders})",
                    tuple(removable_admission_ids),
                )
            cls._delete_where_in(cursor, "admissions", "id", removable_admission_ids)

        removable_patient_ids: list[int] = []
        if patient_ids:
            placeholders = ", ".join("?" for _ in patient_ids)
            remaining_rows = cursor.execute(
                f"""
                SELECT DISTINCT patient_id
                FROM admissions
                WHERE patient_id IN ({placeholders})
                """,
                tuple(patient_ids),
            ).fetchall()
            remaining_patient_ids = {int(row["patient_id"]) for row in remaining_rows}
            removable_patient_ids = [patient_id for patient_id in patient_ids if patient_id not in remaining_patient_ids]
        cls._delete_where_in(cursor, "patients", "id", removable_patient_ids)

        return {"deleted": deleted_cases}

    def delete_archived_operation_case(self, operation_case_id: int) -> dict[str, int]:
        validate_operblock_runtime_path(self.db)
        now = _now_text()

        def operation(cursor: sqlite3.Cursor):
            case = cursor.execute(
                """
                SELECT oc.id, oc.patient_id, oc.admission_id, oc.status
                FROM operation_cases oc
                JOIN admissions a ON a.id = oc.admission_id
                WHERE oc.id = ?
                  AND COALESCE(a.unit_scope, '') = 'operblock'
                """,
                (int(operation_case_id),),
            ).fetchone()
            if not case:
                raise OperBlockConflictError("Архивный случай не найден.")
            case_status = str(case["status"] or "").strip().lower()
            if case_status not in {"active", "closed", "cancelled", "transferred_to_rao"}:
                raise OperBlockConflictError("Случай не может быть удалён из архива оперблока.")
            if case_status == "active":
                cursor.execute(
                    """
                    UPDATE operation_cases
                    SET status = 'closed',
                        ended_at = COALESCE(ended_at, ?),
                        last_modified_by = 'operblock',
                        revision = COALESCE(revision, 0) + 1
                    WHERE id = ?
                      AND status = 'active'
                    """,
                    (now, int(operation_case_id)),
                )
                cursor.execute(
                    """
                    UPDATE operation_table_assignments
                    SET status = 'released',
                        released_at = COALESCE(released_at, ?),
                        last_modified_by = 'operblock',
                        revision = COALESCE(revision, 0) + 1
                    WHERE operation_case_id = ?
                      AND status = 'active'
                      AND released_at IS NULL
                    """,
                    (now, int(operation_case_id)),
                )
                cursor.execute(
                    """
                    UPDATE patient_status_events
                    SET end_time = COALESCE(end_time, ?),
                        last_modified_by = 'operblock',
                        revision = COALESCE(revision, 0) + 1
                    WHERE admission_id = ?
                      AND end_time IS NULL
                    """,
                    (now, int(case["admission_id"])),
                )
                cursor.execute(
                    """
                    UPDATE admissions
                    SET is_active = 0,
                        updated_at = ?,
                        revision = COALESCE(revision, 0) + 1
                    WHERE id = ?
                    """,
                    (now, int(case["admission_id"])),
                )
            result = self._hard_delete_archived_operation_cases(cursor, [case])
            if int(result.get("deleted") or 0) != 1:
                raise OperBlockConflictError("Случай изменён другим рабочим местом. Обновите архив.")
            return {"operation_case_id": int(operation_case_id), "admission_id": int(case["admission_id"])}

        return dict(self.db.run_write_operation(operation, source="operblock_delete_archived_case"))

    def delete_all_archived_operation_cases(self, table_code: str | None = None) -> dict[str, int]:
        validate_operblock_runtime_path(self.db)
        clean_table_code = self._validate_table_code(table_code) if table_code else None

        def operation(cursor: sqlite3.Cursor):
            table_clause = ""
            params: list[Any] = []
            if clean_table_code:
                table_clause = "AND oc.table_code = ?"
                params.append(clean_table_code)
            rows = cursor.execute(
                f"""
                SELECT oc.id, oc.patient_id, oc.admission_id
                FROM operation_cases oc
                JOIN admissions a ON a.id = oc.admission_id
                WHERE oc.status IN ('closed', 'cancelled')
                  AND COALESCE(a.unit_scope, '') = 'operblock'
                  {table_clause}
                """,
                tuple(params),
            ).fetchall()
            if not rows:
                return {"deleted": 0}
            return self._hard_delete_archived_operation_cases(cursor, list(rows))

        return dict(self.db.run_write_operation(operation, source="operblock_delete_all_archived_cases"))
