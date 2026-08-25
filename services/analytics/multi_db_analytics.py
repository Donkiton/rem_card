from __future__ import annotations

import os
import sqlite3
from typing import Iterable, Sequence

from rem_card.app.logger import logger
from rem_card.app.sqlite_shared import configure_connection
from rem_card.services.analytics.period import normalize_analytics_period


TABLE_SPECS: dict[str, dict[str, str | None]] = {
    "admissions": {"time_col": "admission_datetime"},
    "operations": {"time_col": "operation_datetime"},
    "transfusions": {"time_col": "datetime"},
    "ivl_episodes": {"time_col": "start_time"},
    "procedures": {"time_col": "started_at"},
    "procedure_cvc": {"time_col": None},
    "procedure_lumbar_puncture": {"time_col": None},
    "procedure_transfusion": {"time_col": None},
    # Оперблоковые таблицы нужны не для общего RAO-отчёта, а чтобы тот же
    # identity-safe memory snapshot можно было безопасно отфильтровать до
    # legacy builder ob1–ob79.
    "patients": {"time_col": None},
    "operating_tables": {"time_col": None},
    "operation_cases": {"time_col": "started_at"},
    "operblock_timeline_events": {"time_col": "event_time"},
    "orders": {"time_col": "datetime"},
    "vitals": {"time_col": "datetime"},
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
            recovery_bed_stay INTEGER DEFAULT 0,
            merged_into_admission_id INTEGER
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
    "patients": """CREATE TABLE IF NOT EXISTS patients (id INTEGER, full_name TEXT, birth_date TEXT)""",
    "operating_tables": """CREATE TABLE IF NOT EXISTS operating_tables (code TEXT, display_name TEXT)""",
    "operation_cases": """
        CREATE TABLE IF NOT EXISTS operation_cases (
            id INTEGER, patient_id INTEGER, admission_id INTEGER, table_code TEXT,
            status TEXT, created_at TEXT, started_at TEXT, ended_at TEXT,
            planned_operation_name TEXT, planned_surgeons_json TEXT,
            planned_operating_nurse TEXT, planned_anesthesiologist TEXT,
            planned_anesthetist TEXT, height_cm REAL, weight_kg REAL,
            allergies TEXT, blood_group TEXT, blood_rh TEXT, preop_sys REAL,
            preop_dia REAL, preop_pulse REAL, preop_spo2 REAL,
            anesthesia_protocol_number TEXT, anesthesia_protocol_date TEXT,
            transfer_department TEXT, is_deleted INTEGER
        )
    """,
    "operblock_timeline_events": """CREATE TABLE IF NOT EXISTS operblock_timeline_events (id INTEGER, operation_case_id INTEGER, event_time TEXT, event_type TEXT, status TEXT)""",
    "orders": """CREATE TABLE IF NOT EXISTS orders (id INTEGER, admission_id INTEGER, datetime TEXT, text TEXT, drug_key TEXT, status TEXT, comment TEXT)""",
    "vitals": """CREATE TABLE IF NOT EXISTS vitals (id INTEGER, admission_id INTEGER, datetime TEXT, sys REAL, dia REAL, pulse REAL, temp REAL, spo2 REAL, rr REAL, cvp REAL)""",
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

    if start_dt and end_dt:
        period = normalize_analytics_period(start_dt, end_dt)
        start_dt, end_dt = period.sql_bounds

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
            if not _table_exists(conn, table_name):
                continue
            _ensure_column(conn, table_name, "analytics_source_id", "TEXT")
            if table_name == "admissions":
                _ensure_column(conn, "admissions", "recovery_bed_stay", "INTEGER DEFAULT 0")
                _ensure_column(conn, "admissions", "merged_into_admission_id", "INTEGER")
            for source_index, alias in enumerate(aliases):
                if not _table_exists(conn, table_name, schema=alias):
                    continue
                _copy_table_rows(
                    conn,
                    schema=alias,
                    table_name=table_name,
                    time_col=str(spec.get("time_col") or ""),
                    start_dt=start_dt,
                    end_dt=end_dt,
                    source_id=f"db{source_index}",
                    id_offset=source_index * 1_000_000_000,
                )

        _create_light_indexes(conn)
        # Reference dictionaries are joined by a natural code, not by source
        # identity.  Keeping one identical operating-table code prevents an
        # accidental many-to-one join from doubling operation_cases in ob1.
        if _table_exists(conn, "operating_tables") and "code" in _get_columns(conn, "operating_tables"):
            conn.execute(
                'DELETE FROM "operating_tables" WHERE rowid NOT IN '
                '(SELECT MIN(rowid) FROM "operating_tables" GROUP BY "code")'
            )
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
    source_id: str,
    id_offset: int,
):
    target_cols = _get_columns(conn, table_name, schema="main")
    source_cols = _get_columns(conn, table_name, schema=schema)
    if not target_cols or not source_cols:
        return

    common_cols = [col for col in target_cols if col in source_cols and col != "analytics_source_id"]
    if not common_cols:
        return

    insert_cols = ", ".join([*(f'"{col}"' for col in common_cols), '"analytics_source_id"'])
    select_cols = ", ".join([*(f'"{col}"' for col in common_cols), '?'])
    query = (
        f'INSERT INTO "{table_name}" ({insert_cols}) '
        f'SELECT {select_cols} FROM {schema}."{table_name}"'
    )
    where_parts: list[str] = []
    params_list: list[object] = [source_id]
    if start_dt and end_dt and time_col and time_col in source_cols:
        if table_name == "ivl_episodes" and "end_time" in source_cols:
            # ИВЛ — это интервал, а не событие старта.  Эпизод, начавшийся
            # до границы периода, но продолжающийся внутри него, должен
            # попасть в aggregate snapshot: далее engine ограничит его
            # фактическим полуоткрытым интервалом отчёта.  Нельзя применять
            # для него общий фильтр start_time >= start_dt.
            where_parts.append('DATETIME("start_time") < DATETIME(?)')
            params_list.append(end_dt)
            where_parts.append(
                '("end_time" IS NULL OR DATETIME("end_time") > DATETIME(?))'
            )
            params_list.append(start_dt)
        elif table_name == "admissions":
            # Keep admissions that overlap the report period, including patients
            # admitted before its first day and still present at the boundary.
            where_parts.append(f'DATETIME("{time_col}") < DATETIME(?)')
            params_list.append(end_dt)
            if "transfer_datetime" in source_cols and "death_datetime" in source_cols:
                where_parts.append(
                    """
                    (
                        ("transfer_datetime" IS NULL AND "death_datetime" IS NULL)
                        OR DATETIME(
                            CASE
                                WHEN "transfer_datetime" IS NULL THEN "death_datetime"
                                WHEN "death_datetime" IS NULL THEN "transfer_datetime"
                                WHEN DATETIME("transfer_datetime") <= DATETIME("death_datetime")
                                    THEN "transfer_datetime"
                                ELSE "death_datetime"
                            END
                        ) > DATETIME(?)
                    )
                    """.strip()
                )
                params_list.append(start_dt)
        elif table_name == "operblock_timeline_events" and _table_exists(conn, "operation_cases", schema=schema):
            where_parts.append(
                f'"operation_case_id" IN (SELECT "id" FROM {schema}."operation_cases" '
                'WHERE DATETIME("started_at") >= DATETIME(?) AND DATETIME("started_at") < DATETIME(?) '
                'AND COALESCE("status", \'\') NOT IN (\'cancelled\', \'deleted\'))'
            )
            params_list.extend((start_dt, end_dt))
        elif table_name in {"orders", "vitals"} and _table_exists(conn, "operation_cases", schema=schema):
            where_parts.append(
                f'((DATETIME("{time_col}") >= DATETIME(?) AND DATETIME("{time_col}") < DATETIME(?)) OR '
                f'"admission_id" IN (SELECT "admission_id" FROM {schema}."operation_cases" '
                'WHERE DATETIME("started_at") >= DATETIME(?) AND DATETIME("started_at") < DATETIME(?) '
                'AND COALESCE("status", \'\') NOT IN (\'cancelled\', \'deleted\')))'
            )
            params_list.extend((start_dt, end_dt, start_dt, end_dt))
        else:
            where_parts.append(f'"{time_col}" >= ? AND "{time_col}" < ?')
            params_list.extend((start_dt, end_dt))
    if table_name == "admissions":
        scope_parts = []
        if "unit_scope" in source_cols:
            scope_parts.append('LOWER(TRIM(COALESCE("unit_scope", \'\'))) <> \'operblock\'')
        if "admission_type" in source_cols:
            scope_parts.append('LOWER(TRIM(COALESCE("admission_type", \'\'))) <> \'operblock\'')
        if scope_parts:
            scope_clause = " AND ".join(scope_parts)
            operation_columns = set(_get_columns(conn, "operation_cases", schema=schema))
            if {"admission_id", "started_at"}.issubset(operation_columns) and start_dt and end_dt:
                status_clause = (
                    ' AND COALESCE("status", \'\') NOT IN (\'cancelled\', \'deleted\')'
                    if "status" in operation_columns else ""
                )
                where_parts.append(
                    f'(({scope_clause}) OR "id" IN (SELECT "admission_id" FROM {schema}."operation_cases" '
                    f'WHERE DATETIME("started_at") >= DATETIME(?) AND DATETIME("started_at") < DATETIME(?)'
                    f'{status_clause}))'
                )
                params_list.extend((start_dt, end_dt))
            else:
                where_parts.append(scope_clause)
        if "merged_into_admission_id" in source_cols:
            where_parts.append('"merged_into_admission_id" IS NULL')
    if where_parts:
        query += " WHERE " + " AND ".join(where_parts)

    # The literal source id is first in SELECT, while WHERE placeholders follow
    # it only when a range is present. SQLite binds in statement order.
    if where_parts:
        params = [source_id, *params_list[1:]]
    else:
        params = [source_id]
    conn.execute(query, tuple(params))
    _remap_source_ids(conn, table_name, source_id, id_offset)


def _remap_source_ids(conn: sqlite3.Connection, table_name: str, source_id: str, offset: int):
    """Делает legacy aggregate snapshot identity-safe без изменения source DB."""
    columns = set(_get_columns(conn, table_name, schema="main"))
    targets = []
    if "id" in columns: targets.append("id")
    if table_name == "admissions":
        targets.extend(name for name in ("patient_id", "merged_into_admission_id") if name in columns)
    if table_name == "operation_cases":
        targets.extend(name for name in ("patient_id", "admission_id", "source_rao_admission_id", "resolved_rao_admission_id", "future_rao_admission_id") if name in columns)
    if table_name == "operblock_timeline_events" and "operation_case_id" in columns:
        targets.append("operation_case_id")
    if table_name in {"operations", "transfusions", "ivl_episodes", "procedures", "orders", "vitals"} and "admission_id" in columns:
        targets.append("admission_id")
    if table_name in {"orders", "vitals"} and "admission_id" in columns:
        targets.append("admission_id")
    if table_name in {"procedure_cvc", "procedure_lumbar_puncture", "procedure_transfusion"} and "procedure_id" in columns:
        targets.append("procedure_id")
    for column in targets:
        conn.execute(f'UPDATE "{table_name}" SET "{column}" = "{column}" + ? WHERE analytics_source_id = ? AND "{column}" IS NOT NULL', (offset, source_id))


def _create_light_indexes(conn: sqlite3.Connection):
    indexed = {
        "admissions": ("admission_datetime",),
        "operations": ("operation_datetime",),
        "transfusions": ("datetime",),
        "ivl_episodes": ("start_time",),
        "procedures": ("started_at",),
        "operation_cases": ("started_at",),
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
