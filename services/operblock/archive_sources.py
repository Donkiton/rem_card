from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Mapping
from rem_card.app.archive_schema_cache import get_archive_schema
from rem_card.app.logger import logger
from rem_card.app.operblock_archive_index_cache import (
    LEGACY_OPERBLOCK_INDEX_ALIAS,
    LEGACY_OPERBLOCK_INDEX_NAME,
    LEGACY_OPERBLOCK_INDEX_TABLE,
    attach_legacy_operblock_archive_index,
    operblock_archive_source_fingerprint,
    source_has_started_at_index,
)
from rem_card.app.sqlite_shared import configure_connection
from .common import _age_text, _sqlite_table_names, _sqlite_columns


class OperBlockArchiveSourcesMixin:
    @staticmethod
    def _db_has_operblock_cases_in_period(db_path: str, start: datetime, end: datetime) -> bool:
        from rem_card.services.operblock_service import OperBlockService

        if not db_path or not os.path.isfile(db_path):
            return False
        conn = None
        try:
            conn = sqlite3.connect(
                f"file:{os.path.abspath(db_path)}?mode=ro",
                uri=True,
                check_same_thread=False,
                isolation_level=None,
                timeout=5.0,
            )
            configure_connection(conn, readonly=True)
            tables = _sqlite_table_names(conn)
            if "operation_cases" not in tables:
                return False
            case_columns = _sqlite_columns(conn, "operation_cases")
            if "started_at" not in case_columns:
                return False
            legacy_index_attached = OperBlockService._begin_external_archive_snapshot(
                conn,
                db_path,
                use_legacy_index=True,
            )
            native_started_at_index = (
                not legacy_index_attached and source_has_started_at_index(conn)
            )
            join_sql = ""
            if legacy_index_attached:
                case_source_sql = (
                    f"{LEGACY_OPERBLOCK_INDEX_ALIAS}.{LEGACY_OPERBLOCK_INDEX_TABLE} AS legacy_idx "
                    f"INDEXED BY {LEGACY_OPERBLOCK_INDEX_NAME} "
                    "JOIN operation_cases oc ON oc.id = legacy_idx.operation_case_id"
                )
                period_column = "legacy_idx.started_at"
            else:
                case_source_sql = "operation_cases oc"
                period_column = "oc.started_at"

            # Archive selectors pass inclusive wall-clock bounds.  The old
            # DATETIME(... BETWEEN ...) predicate rounded both sides to whole
            # seconds, so a row at 23:59:59.900 belonged to an end bound of
            # 23:59:59.  Express that contract as [start, next-second) for the
            # canonical sidecar.  For whole-day native archives, date-only
            # bounds are separator-agnostic (both SQLite's ``T`` and space ISO
            # forms) and let the real started_at index participate.
            is_whole_day_period = (
                start.hour == 0
                and start.minute == 0
                and start.second == 0
                and start.microsecond == 0
                and end.hour == 23
                and end.minute == 59
                and end.second == 59
            )
            if legacy_index_attached:
                start_bound = start.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
                end_exclusive = (end.replace(microsecond=0) + timedelta(seconds=1)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                where_sql = f"WHERE {period_column} >= ? AND {period_column} < ?"
                period_params = (start_bound, end_exclusive)
            elif native_started_at_index and is_whole_day_period:
                start_bound = start.date().isoformat()
                end_exclusive = (end.date() + timedelta(days=1)).isoformat()
                where_sql = f"WHERE {period_column} >= ? AND {period_column} < ?"
                period_params = (start_bound, end_exclusive)
            else:
                # Preserve SQLite's legacy parsing semantics when no usable
                # technical/native index is available or the caller supplied
                # a sub-day interval.
                where_sql = "WHERE DATETIME(oc.started_at) BETWEEN DATETIME(?) AND DATETIME(?)"
                period_params = (
                    start.strftime("%Y-%m-%d %H:%M:%S"),
                    end.strftime("%Y-%m-%d %H:%M:%S"),
                )
            if "admissions" in tables and "admission_id" in case_columns:
                admission_columns = _sqlite_columns(conn, "admissions")
                join_sql = "LEFT JOIN admissions a ON a.id = oc.admission_id"
                if "unit_scope" in admission_columns:
                    where_sql += " AND COALESCE(a.unit_scope, '') = 'operblock'"
            if "status" in case_columns:
                where_sql += " AND COALESCE(oc.status, '') NOT IN ('cancelled', 'deleted')"
            row = conn.execute(
                f"""
                SELECT 1
                FROM {case_source_sql}
                {join_sql}
                {where_sql}
                LIMIT 1
                """,
                period_params,
            ).fetchone()
            return bool(row)
        except Exception as exc:
            logger.warning("Skipping operblock DB period check %s: %s", db_path, exc)
            return False
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _begin_external_archive_snapshot(
        conn,
        db_path: str,
        *,
        use_legacy_index: bool,
    ) -> bool:
        """Pin main DB and an optional local legacy index to the same source revision."""
        legacy_index = (
            attach_legacy_operblock_archive_index(conn, db_path)
            if use_legacy_index
            else None
        )
        conn.execute("BEGIN")
        # Read main first: an attached local database must not become the first
        # database whose snapshot is acquired for this transaction.
        conn.execute("SELECT name FROM main.sqlite_master LIMIT 1").fetchone()
        if (
            legacy_index is not None
            and operblock_archive_source_fingerprint(db_path, conn)
            != legacy_index.source_fingerprint
        ):
            conn.execute("ROLLBACK")
            conn.execute(f"DETACH DATABASE {LEGACY_OPERBLOCK_INDEX_ALIAS}")
            conn.execute("BEGIN")
            conn.execute("SELECT name FROM main.sqlite_master LIMIT 1").fetchone()
            return False
        return legacy_index is not None

    @staticmethod
    def _build_archive_cases_query(
        *,
        tables: set[str] | None = None,
        admission_columns: set[str] | None = None,
        operation_case_columns: set[str] | None = None,
        start_dt: str | None = None,
        end_dt: str | None = None,
        table_code: str | None = None,
        search_query: str = "",
        search_name: str = "",
        search_ib: str = "",
        search_diag: str = "",
        limit: int | None = 500,
        offset: int = 0,
        count_only: bool = False,
        end_exclusive: bool = False,
        legacy_index_attached: bool = False,
    ) -> tuple[str, tuple[Any, ...]]:
        tables = set(tables or {"operating_tables", "admissions", "patients"})
        admission_columns = set(admission_columns or {"unit_scope"})
        operation_case_columns = None if operation_case_columns is None else set(operation_case_columns)
        migration_status_expr = "oc.migration_status" if operation_case_columns is None or "migration_status" in operation_case_columns else "NULL"
        migrated_at_expr = "oc.migrated_at" if operation_case_columns is None or "migrated_at" in operation_case_columns else "NULL"
        table_join = "LEFT JOIN operating_tables t ON t.code = oc.table_code" if "operating_tables" in tables else ""
        table_display_expr = (
            "COALESCE(t.display_name, CASE oc.table_code WHEN 'emergency' THEN 'Экстренная операционная' "
            "WHEN 'planned' THEN 'Плановая операционная' ELSE oc.table_code END)"
            if "operating_tables" in tables
            else "CASE oc.table_code WHEN 'emergency' THEN 'Экстренная операционная' WHEN 'planned' THEN 'Плановая операционная' ELSE oc.table_code END"
        )
        unit_scope_clause = (
            "AND COALESCE(a.unit_scope, '') = 'operblock'"
            if "unit_scope" in admission_columns
            else ""
        )
        params: list[Any] = []
        case_source_sql = "operation_cases oc"
        period_column = "oc.started_at"
        if legacy_index_attached:
            case_source_sql = (
                f"{LEGACY_OPERBLOCK_INDEX_ALIAS}.{LEGACY_OPERBLOCK_INDEX_TABLE} AS legacy_idx "
                f"INDEXED BY {LEGACY_OPERBLOCK_INDEX_NAME} "
                "JOIN operation_cases oc ON oc.id = legacy_idx.operation_case_id"
            )
            period_column = "legacy_idx.started_at"
        period_clause = ""
        if start_dt and end_dt:
            if end_exclusive:
                period_clause = (
                    f"AND {period_column} >= ? "
                    f"AND {period_column} < ?"
                )
            else:
                period_clause = "AND DATETIME(oc.started_at) BETWEEN DATETIME(?) AND DATETIME(?)"
            params.extend([start_dt, end_dt])
        table_clause = ""
        clean_table_code = str(table_code or "").strip().lower()
        if clean_table_code:
            table_clause = "AND LOWER(TRIM(COALESCE(oc.table_code, ''))) = ?"
            params.append(clean_table_code)
        search_clause = ""
        clean_search = str(search_query or "").strip().casefold()
        if clean_search:
            search_expr = (
                "COALESCE(p.full_name, '') || ' ' || "
                "COALESCE(a.history_number, '') || ' ' || "
                "COALESCE(a.diagnosis_code, '') || ' ' || "
                "COALESCE(a.diagnosis_text, '') || ' ' || "
                "COALESCE(oc.table_code, '')"
            )
            search_clause = f"AND INSTR(CASEFOLD({search_expr}), ?) > 0"
            params.append(clean_search)
        field_search_clauses: list[str] = []
        clean_name = str(search_name or "").strip().casefold()
        if clean_name:
            field_search_clauses.append("AND INSTR(CASEFOLD(COALESCE(p.full_name, '')), ?) > 0")
            params.append(clean_name)
        clean_ib = str(search_ib or "").strip().casefold()
        if clean_ib:
            field_search_clauses.append("AND INSTR(CASEFOLD(COALESCE(a.history_number, '')), ?) > 0")
            params.append(clean_ib)
        clean_diag = str(search_diag or "").strip().casefold()
        if clean_diag:
            field_search_clauses.append(
                "AND INSTR(CASEFOLD(COALESCE(a.diagnosis_code, '') || ' ' || "
                "COALESCE(a.diagnosis_text, '')), ?) > 0"
            )
            params.append(clean_diag)
        field_search_sql = "\n              ".join(field_search_clauses)

        if count_only:
            query = f"""
            SELECT COUNT(*) AS total_count
            FROM {case_source_sql}
            {table_join}
            JOIN admissions a ON a.id = oc.admission_id
            JOIN patients p ON p.id = oc.patient_id
            WHERE oc.status IN ('active', 'closed', 'transferred_to_rao')
              {unit_scope_clause}
              {period_clause}
              {table_clause}
              {search_clause}
              {field_search_sql}
            """
            return query, tuple(params)

        limit_sql = ""
        if limit is not None:
            limit_sql = "LIMIT ? OFFSET ?"
            params.extend((max(1, int(limit)), max(0, int(offset or 0))))

        query = f"""
            SELECT
                oc.id AS operation_case_id,
                oc.patient_id,
                oc.admission_id,
                oc.table_code,
                oc.status AS case_status,
                {migration_status_expr} AS migration_status,
                {migrated_at_expr} AS migrated_at,
                oc.started_at,
                oc.ended_at,
                {table_display_expr} AS table_display_name,
                p.full_name,
                p.birth_date,
                a.history_number,
                a.patient_gender,
                a.patient_age,
                a.patient_months,
                a.patient_age_unit,
                a.diagnosis_code,
                a.diagnosis_text
            FROM {case_source_sql}
            {table_join}
            JOIN admissions a ON a.id = oc.admission_id
            JOIN patients p ON p.id = oc.patient_id
            WHERE oc.status IN ('active', 'closed', 'transferred_to_rao')
              {unit_scope_clause}
              {period_clause}
              {table_clause}
              {search_clause}
              {field_search_sql}
            ORDER BY
                CASE WHEN oc.status = 'active' THEN 0 ELSE 1 END,
                datetime(COALESCE(oc.ended_at, oc.started_at)) DESC,
                oc.id DESC
            {limit_sql}
            """
        return query, tuple(params)

    @staticmethod
    def _archive_case_payload(data: Mapping[str, Any] | dict[str, Any], *, db_path: str, is_external: bool) -> dict[str, Any]:
        source_path = os.path.abspath(str(db_path or "")) if db_path else ""
        operation_case_id = int((data or {}).get("operation_case_id") or 0)
        return {
            "operation_case_id": operation_case_id,
            "source_operation_case_id": operation_case_id,
            "admission_id": int((data or {}).get("admission_id") or 0),
            "source_admission_id": int((data or {}).get("admission_id") or 0),
            "patient_id": int((data or {}).get("patient_id") or 0),
            "source_patient_id": int((data or {}).get("patient_id") or 0),
            "table_code": (data or {}).get("table_code"),
            "table_display_name": (data or {}).get("table_display_name"),
            "history_number": (data or {}).get("history_number") or "",
            "full_name": (data or {}).get("full_name") or "Неизвестно",
            "age": _age_text(data),
            "gender": (data or {}).get("patient_gender") or "",
            "diagnosis_code": (data or {}).get("diagnosis_code") or "",
            "diagnosis_text": (data or {}).get("diagnosis_text") or "",
            "started_at": (data or {}).get("started_at"),
            "ended_at": (data or {}).get("ended_at"),
            "status": (data or {}).get("case_status") or "closed",
            "migration_status": (data or {}).get("migration_status") or "",
            "migrated_at": (data or {}).get("migrated_at"),
            "source_db_path": source_path,
            "source_db_name": os.path.basename(source_path) if source_path else "",
            "is_external_archive": bool(is_external),
        }

    @staticmethod
    def _fetch_archive_case_rows_from_db(
        db_path: str,
        *,
        start_dt: str | None = None,
        end_dt: str | None = None,
        table_code: str | None = None,
        search_query: str = "",
        search_name: str = "",
        search_ib: str = "",
        search_diag: str = "",
        limit: int | None = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        from rem_card.services.operblock_service import OperBlockService

        if not db_path or not os.path.isfile(db_path):
            return []
        conn = sqlite3.connect(
            f"file:{os.path.abspath(db_path)}?mode=ro",
            uri=True,
            check_same_thread=False,
            isolation_level=None,
            timeout=5.0,
        )
        try:
            configure_connection(conn, readonly=True)
            schema = get_archive_schema(
                conn,
                db_path,
                inspect_tables=("admissions", "operation_cases"),
            )
            tables = schema.tables
            if not {"operation_cases", "admissions", "patients"}.issubset(tables):
                return []
            legacy_index_attached = OperBlockService._begin_external_archive_snapshot(
                conn,
                db_path,
                use_legacy_index=bool(start_dt and end_dt),
            )
            admission_columns = schema.columns.get("admissions", frozenset())
            query, params = OperBlockService._build_archive_cases_query(
                tables=tables,
                admission_columns=admission_columns,
                operation_case_columns=schema.columns.get("operation_cases", frozenset()),
                start_dt=start_dt,
                end_dt=end_dt,
                table_code=table_code,
                search_query=search_query,
                search_name=search_name,
                search_ib=search_ib,
                search_diag=search_diag,
                limit=limit,
                offset=offset,
                legacy_index_attached=legacy_index_attached,
            )
            return [dict(row) for row in conn.execute(query, params).fetchall()]
        finally:
            conn.close()

    @staticmethod
    def _fetch_archive_case_count_from_db(
        db_path: str,
        *,
        start_dt: str | None = None,
        end_dt: str | None = None,
        table_code: str | None = None,
        search_query: str = "",
        search_name: str = "",
        search_ib: str = "",
        search_diag: str = "",
    ) -> int:
        from rem_card.services.operblock_service import OperBlockService

        if not db_path or not os.path.isfile(db_path):
            return 0
        conn = sqlite3.connect(
            f"file:{os.path.abspath(db_path)}?mode=ro",
            uri=True,
            check_same_thread=False,
            isolation_level=None,
            timeout=5.0,
        )
        try:
            configure_connection(conn, readonly=True)
            schema = get_archive_schema(
                conn,
                db_path,
                inspect_tables=("admissions", "operation_cases"),
            )
            tables = schema.tables
            if not {"operation_cases", "admissions", "patients"}.issubset(tables):
                return 0
            legacy_index_attached = OperBlockService._begin_external_archive_snapshot(
                conn,
                db_path,
                use_legacy_index=bool(start_dt and end_dt),
            )
            admission_columns = schema.columns.get("admissions", frozenset())
            query, params = OperBlockService._build_archive_cases_query(
                tables=tables,
                admission_columns=admission_columns,
                operation_case_columns=schema.columns.get("operation_cases", frozenset()),
                start_dt=start_dt,
                end_dt=end_dt,
                table_code=table_code,
                search_query=search_query,
                search_name=search_name,
                search_ib=search_ib,
                search_diag=search_diag,
                count_only=True,
                legacy_index_attached=legacy_index_attached,
            )
            row = conn.execute(query, params).fetchone()
            return int(row["total_count"] or 0) if row else 0
        finally:
            conn.close()

    @staticmethod
    def _fetch_archive_case_page_from_db(
        db_path: str,
        *,
        start_dt: str | None = None,
        end_dt: str | None = None,
        table_code: str | None = None,
        search_query: str = "",
        search_name: str = "",
        search_ib: str = "",
        search_diag: str = "",
        limit: int | None = 500,
        offset: int = 0,
    ) -> tuple[int, list[dict[str, Any]]]:
        """Fetch count and rows from one readonly archive connection."""
        from rem_card.services.operblock_service import OperBlockService

        if not db_path or not os.path.isfile(db_path):
            return 0, []
        conn = sqlite3.connect(
            f"file:{os.path.abspath(db_path)}?mode=ro",
            uri=True,
            check_same_thread=False,
            isolation_level=None,
            timeout=5.0,
        )
        try:
            configure_connection(conn, readonly=True)
            schema = get_archive_schema(
                conn,
                db_path,
                inspect_tables=("admissions", "operation_cases"),
            )
            tables = schema.tables
            if not {"operation_cases", "admissions", "patients"}.issubset(tables):
                return 0, []
            legacy_index_attached = OperBlockService._begin_external_archive_snapshot(
                conn,
                db_path,
                use_legacy_index=bool(start_dt and end_dt),
            )
            query_args = {
                "tables": tables,
                "admission_columns": schema.columns.get("admissions", frozenset()),
                "operation_case_columns": schema.columns.get("operation_cases", frozenset()),
                "start_dt": start_dt,
                "end_dt": end_dt,
                "table_code": table_code,
                "search_query": search_query,
                "search_name": search_name,
                "search_ib": search_ib,
                "search_diag": search_diag,
                "end_exclusive": True,
                "legacy_index_attached": legacy_index_attached,
            }
            count_query, count_params = OperBlockService._build_archive_cases_query(
                **query_args,
                count_only=True,
            )
            count_row = conn.execute(count_query, count_params).fetchone()
            total_count = int(count_row["total_count"] or 0) if count_row else 0
            if total_count <= 0:
                return 0, []
            rows_query, rows_params = OperBlockService._build_archive_cases_query(
                **query_args,
                limit=limit,
                offset=offset,
            )
            rows = [dict(row) for row in conn.execute(rows_query, rows_params).fetchall()]
            return total_count, rows
        finally:
            if bool(getattr(conn, "in_transaction", False)):
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            conn.close()
