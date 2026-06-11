from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from typing import Iterable

from rem_card.services.patient_bed_management.recovery_beds import RECOVERY_BED_NUMBERS


FILTERED_RELATED_TABLES = ("operations", "transfusions", "ivl_episodes", "procedures")


@contextmanager
def recovery_bed_analytics_filter(conn: sqlite3.Connection, *, include_recovery_beds: bool):
    if include_recovery_beds or not _table_exists(conn, "admissions"):
        yield
        return

    original_query_only = _query_only(conn)
    created_tables: list[str] = []
    try:
        _set_query_only(conn, False)
        _create_filtered_admissions(conn)
        created_tables.append("admissions")
        for table_name in FILTERED_RELATED_TABLES:
            if _table_exists(conn, table_name) and "admission_id" in _columns(conn, table_name):
                _create_filtered_related_table(conn, table_name)
                created_tables.append(table_name)
        yield
    finally:
        try:
            _set_query_only(conn, False)
            for table_name in reversed(created_tables):
                conn.execute(f'DROP TABLE IF EXISTS temp."{table_name}"')
        finally:
            _set_query_only(conn, original_query_only)


def _create_filtered_admissions(conn: sqlite3.Connection) -> None:
    columns = set(_columns(conn, "admissions"))
    recovery_numbers = ", ".join(str(int(number)) for number in sorted(RECOVERY_BED_NUMBERS))
    if "recovery_bed_stay" in columns and "bed_number" in columns:
        where_clause = (
            f"COALESCE(recovery_bed_stay, 0) = 0 "
            f"AND COALESCE(bed_number, 0) NOT IN ({recovery_numbers})"
        )
    elif "recovery_bed_stay" in columns:
        where_clause = "COALESCE(recovery_bed_stay, 0) = 0"
    elif "bed_number" in columns:
        where_clause = f"COALESCE(bed_number, 0) NOT IN ({recovery_numbers})"
    else:
        where_clause = "1 = 1"

    conn.execute("DROP TABLE IF EXISTS temp.admissions")
    conn.execute(f"CREATE TEMP TABLE admissions AS SELECT * FROM main.admissions WHERE {where_clause}")


def _create_filtered_related_table(conn: sqlite3.Connection, table_name: str) -> None:
    conn.execute(f'DROP TABLE IF EXISTS temp."{table_name}"')
    conn.execute(
        f"""
        CREATE TEMP TABLE "{table_name}" AS
        SELECT *
        FROM main."{table_name}"
        WHERE admission_id IN (SELECT id FROM temp.admissions)
        """
    )


def _query_only(conn: sqlite3.Connection) -> bool:
    row = conn.execute("PRAGMA query_only").fetchone()
    return bool(row and int(row[0] or 0))


def _set_query_only(conn: sqlite3.Connection, value: bool) -> None:
    conn.execute(f"PRAGMA query_only = {'ON' if value else 'OFF'}")


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return bool(
        conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
            UNION ALL
            SELECT 1
            FROM sqlite_temp_master
            WHERE type = 'table' AND name = ?
            LIMIT 1
            """,
            (table_name, table_name),
        ).fetchone()
    )


def _columns(conn: sqlite3.Connection, table_name: str) -> Iterable[str]:
    return [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()]
