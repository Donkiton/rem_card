from __future__ import annotations

import os
import sqlite3
from typing import Iterable, Sequence

from rem_card.app.logger import logger
from rem_card.app.sqlite_shared import configure_connection


TABLE_SPECS: dict[str, dict[str, str | None]] = {
    "admissions": {"time_col": "admission_datetime"},
    "operations": {"time_col": "operation_datetime"},
    "transfusions": {"time_col": "datetime"},
    "ivl_episodes": {"time_col": "start_time"},
    "procedures": {"time_col": "started_at"},
    "procedure_cvc": {"time_col": None},
    "procedure_lumbar_puncture": {"time_col": None},
    "procedure_transfusion": {"time_col": None},
}


FALLBACK_DDL: dict[str, str] = {
    "admissions": """
        CREATE TABLE IF NOT EXISTS admissions (
            id INTEGER,
            patient_id INTEGER,
            admission_datetime TEXT,
            transfer_datetime TEXT,
            death_datetime TEXT,
            outcome TEXT,
            patient_age REAL,
            patient_age_unit TEXT,
            patient_gender TEXT,
            source_department TEXT,
            diagnosis_code TEXT,
            diagnosis_text TEXT,
            bed_number INTEGER,
            recovery_bed_stay INTEGER DEFAULT 0
        )
    """,
    "operations": """
        CREATE TABLE IF NOT EXISTS operations (
            id INTEGER,
            admission_id INTEGER,
            operation_datetime TEXT,
            description TEXT
        )
    """,
    "transfusions": """
        CREATE TABLE IF NOT EXISTS transfusions (
            id INTEGER,
            admission_id INTEGER,
            datetime TEXT,
            type TEXT,
            volume_ml REAL,
            source TEXT,
            source_order_id INTEGER,
            source_admin_id INTEGER
        )
    """,
    "ivl_episodes": """
        CREATE TABLE IF NOT EXISTS ivl_episodes (
            id INTEGER,
            admission_id INTEGER,
            start_time TEXT,
            end_time TEXT
        )
    """,
    "procedures": """
        CREATE TABLE IF NOT EXISTS procedures (
            id INTEGER,
            patient_id INTEGER,
            admission_id INTEGER,
            procedure_type TEXT,
            status TEXT,
            created_at TEXT,
            updated_at TEXT,
            started_at TEXT,
            finished_at TEXT,
            duration_minutes INTEGER,
            doctor_id INTEGER,
            doctor_name_snapshot TEXT,
            department_snapshot TEXT,
            patient_snapshot_json TEXT,
            diagnosis_snapshot TEXT,
            notes TEXT,
            created_by TEXT,
            updated_by TEXT,
            revision INTEGER,
            is_deleted INTEGER
        )
    """,
    "procedure_cvc": """
        CREATE TABLE IF NOT EXISTS procedure_cvc (
            procedure_id INTEGER,
            access_code TEXT,
            access_other TEXT,
            attempts_count INTEGER,
            diameter_f REAL,
            length_cm REAL,
            lumens_count INTEGER,
            technical_difficulty_code TEXT,
            technical_difficulty_description TEXT,
            usage_complications_code TEXT,
            usage_complications_description TEXT,
            catheter_status TEXT,
            removed_or_replaced TEXT,
            removed_at TEXT,
            operator_doctor_name TEXT,
            removal_doctor_name TEXT
        )
    """,
    "procedure_lumbar_puncture": """
        CREATE TABLE IF NOT EXISTS procedure_lumbar_puncture (
            procedure_id INTEGER,
            access_code TEXT,
            access_other TEXT,
            level_code TEXT,
            level_other TEXT,
            technical_difficulty_code TEXT,
            technical_difficulty_description TEXT,
            result_code TEXT,
            operator_doctor_name TEXT
        )
    """,
    "procedure_transfusion": """
        CREATE TABLE IF NOT EXISTS procedure_transfusion (
            procedure_id INTEGER,
            indication_code TEXT,
            donor_component_name TEXT,
            volume_ml REAL,
            reaction_symptoms TEXT,
            reaction_severity TEXT,
            operator_doctor_name TEXT
        )
    """,
}


class AnalyticsConnectionManager:
    """
    Адаптер под интерфейс DBManager, достаточный для GraphsDialog/ReportDialog.
    """

    def __init__(self, conn: sqlite3.Connection, *, db_path: str):
        self._conn = conn
        self.db_path = db_path

    def get_connection(self):
        return self._conn

    def close_connection(self):
        if self._conn:
            self._conn.close()
            self._conn = None


def create_readonly_analytics_manager(db_path: str) -> AnalyticsConnectionManager:
    abs_path = os.path.abspath(str(db_path or ""))
    if not os.path.isfile(abs_path):
        raise ValueError(f"Analytics DB path is unavailable: {db_path}")
    uri = f"file:{abs_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False, isolation_level=None, timeout=10.0)
    configure_connection(conn, readonly=True)
    return AnalyticsConnectionManager(conn, db_path=abs_path)


def create_multi_db_analytics_manager(
    db_paths: Sequence[str],
    *,
    start_dt: str | None = None,
    end_dt: str | None = None,
) -> AnalyticsConnectionManager:
    valid_paths = _normalize_db_paths(db_paths)
    if not valid_paths:
        raise ValueError("No valid DB paths provided for analytics")

    conn = sqlite3.connect(
        ":memory:",
        check_same_thread=False,
        isolation_level=None,
        timeout=10.0,
    )
    configure_connection(conn, readonly=False)

    aliases = []
    for idx, db_path in enumerate(valid_paths):
        alias = f"db{idx}"
        conn.execute(f"ATTACH DATABASE ? AS {alias}", (db_path,))
        aliases.append(alias)

    try:
        for table_name, spec in TABLE_SPECS.items():
            _prepare_target_table(conn, aliases, table_name)
            if table_name == "admissions":
                _ensure_column(conn, "admissions", "recovery_bed_stay", "INTEGER DEFAULT 0")
            for alias in aliases:
                if not _table_exists(conn, table_name, schema=alias):
                    continue
                _copy_table_rows(
                    conn,
                    schema=alias,
                    table_name=table_name,
                    time_col=str(spec.get("time_col") or ""),
                    start_dt=start_dt,
                    end_dt=end_dt,
                )

        _create_light_indexes(conn)
    finally:
        for alias in aliases:
            try:
                conn.execute(f"DETACH DATABASE {alias}")
            except Exception:
                # DETACH может не сработать, если остались активные курсоры;
                # для in-memory manager это некритично.
                pass

    label = f"multi_db_analytics[{len(valid_paths)}]"
    logger.info("Built multi-DB analytics snapshot (%s DB files)", len(valid_paths))
    return AnalyticsConnectionManager(conn, db_path=label)


def _normalize_db_paths(db_paths: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in db_paths:
        if not raw:
            continue
        abs_path = os.path.abspath(str(raw))
        if not os.path.isfile(abs_path):
            continue
        key = os.path.normcase(abs_path)
        if key in seen:
            continue
        seen.add(key)
        result.append(abs_path)
    return result


def _table_exists(conn: sqlite3.Connection, table_name: str, *, schema: str = "main") -> bool:
    row = conn.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return bool(row)


def _prepare_target_table(conn: sqlite3.Connection, aliases: Sequence[str], table_name: str):
    for alias in aliases:
        if not _table_exists(conn, table_name, schema=alias):
            continue
        conn.execute(f'DROP TABLE IF EXISTS main."{table_name}"')
        conn.execute(
            f'CREATE TABLE main."{table_name}" AS SELECT * FROM {alias}."{table_name}" WHERE 0'
        )
        return
    ddl = FALLBACK_DDL.get(table_name)
    if ddl:
        conn.execute(ddl)


def _ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, column_def: str):
    if column_name in _get_columns(conn, table_name, schema="main"):
        return
    conn.execute(f'ALTER TABLE main."{table_name}" ADD COLUMN "{column_name}" {column_def}')


def _get_columns(conn: sqlite3.Connection, table_name: str, *, schema: str = "main") -> list[str]:
    rows = conn.execute(f'PRAGMA {schema}.table_info("{table_name}")').fetchall()
    return [str(row[1]) for row in rows if row and row[1]]


def _copy_table_rows(
    conn: sqlite3.Connection,
    *,
    schema: str,
    table_name: str,
    time_col: str,
    start_dt: str | None,
    end_dt: str | None,
):
    target_cols = _get_columns(conn, table_name, schema="main")
    source_cols = _get_columns(conn, table_name, schema=schema)
    if not target_cols or not source_cols:
        return

    common_cols = [col for col in target_cols if col in source_cols]
    if not common_cols:
        return

    insert_cols = ", ".join(f'"{col}"' for col in common_cols)
    select_cols = ", ".join(f'"{col}"' for col in common_cols)
    query = (
        f'INSERT INTO "{table_name}" ({insert_cols}) '
        f'SELECT {select_cols} FROM {schema}."{table_name}"'
    )
    params: tuple[object, ...] = ()
    if start_dt and end_dt and time_col and time_col in source_cols:
        query += f' WHERE "{time_col}" BETWEEN ? AND ?'
        params = (start_dt, end_dt)

    conn.execute(query, params)


def _create_light_indexes(conn: sqlite3.Connection):
    indexed = {
        "admissions": ("admission_datetime",),
        "operations": ("operation_datetime",),
        "transfusions": ("datetime",),
        "ivl_episodes": ("start_time",),
        "procedures": ("started_at",),
    }
    for table_name, cols in indexed.items():
        if not _table_exists(conn, table_name, schema="main"):
            continue
        existing_cols = set(_get_columns(conn, table_name, schema="main"))
        for col in cols:
            if col not in existing_cols:
                continue
            idx_name = f'idx_{table_name}_{col}'
            conn.execute(
                f'CREATE INDEX IF NOT EXISTS "{idx_name}" ON "{table_name}"("{col}")'
            )
