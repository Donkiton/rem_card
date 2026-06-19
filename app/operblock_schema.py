from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import sqlite3
from typing import Any

from rem_card.app.logger import logger
from rem_card.app.sqlite_shared import run_integrity_check, run_quick_check
from rem_card.app.unified_db_schema import (
    _create_change_triggers,
    _create_updated_at_trigger,
    _ensure_column,
    _mark_schema_migration,
)


OPERBLOCK_SCHEMA_VERSION = 1009
OPERBLOCK_TABLE_CODES = ("emergency", "planned")


@dataclass(frozen=True)
class OperBlockSchemaResult:
    migrated: bool
    backup_path: str = ""
    quick_check: str = ""
    integrity_check: str = ""


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return bool(row)


def _index_exists(conn: sqlite3.Connection, index_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,),
    ).fetchone()
    return bool(row)


def _columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def is_operblock_schema_ready(conn: sqlite3.Connection) -> bool:
    required_tables = {"operating_tables", "operation_cases", "operation_table_assignments"}
    required_tables.add("operblock_timeline_events")
    if not all(_table_exists(conn, table_name) for table_name in required_tables):
        return False
    admission_columns = _columns(conn, "admissions")
    if not {"unit_scope", "admission_type"}.issubset(admission_columns):
        return False
    case_columns = _columns(conn, "operation_cases")
    if not {
        "id",
        "patient_id",
        "admission_id",
        "table_code",
        "status",
        "created_at",
        "started_at",
        "ended_at",
        "created_by_role",
        "created_by_client_id",
        "revision",
        "planned_operation_name",
        "planned_anesthesia_assistance_type",
        "planned_surgeons_json",
        "planned_operating_nurse",
        "planned_anesthesiologist",
        "planned_anesthetist",
        "height_cm",
        "weight_kg",
        "allergies",
        "blood_group",
        "blood_rh",
        "preop_sys",
        "preop_dia",
        "preop_pulse",
        "preop_spo2",
        "preop_save_initial_vitals",
        "anesthesia_protocol_number",
        "anesthesia_protocol_date",
        "transfer_department",
        "future_rao_admission_id",
        "offline_case_uuid",
        "offline_session_id",
        "migration_status",
        "migrated_at",
        "migrated_remote_id",
        "original_local_id",
        "original_protocol_number",
        "excluded_from_migration",
    }.issubset(case_columns):
        return False
    if not _index_exists(conn, "idx_operation_cases_one_active_per_table"):
        return False
    if not _index_exists(conn, "idx_operation_cases_protocol_sequence"):
        return False
    if not _index_exists(conn, "idx_operation_assignments_one_active_per_table"):
        return False
    timeline_columns = _columns(conn, "operblock_timeline_events")
    if not {
        "id",
        "operation_case_id",
        "admission_id",
        "event_type",
        "event_time",
        "status",
        "revision",
        "parent_event_id",
    }.issubset(timeline_columns):
        return False
    for index_name in (
        "idx_operblock_timeline_admission_time",
        "idx_operblock_timeline_case_time",
        "idx_operblock_timeline_status",
        "idx_operblock_timeline_event_type",
        "idx_operblock_timeline_parent",
    ):
        if not _index_exists(conn, index_name):
            return False
    row = conn.execute(
        "SELECT COUNT(*) FROM operating_tables WHERE code IN ('emergency', 'planned')",
    ).fetchone()
    return bool(row and int(row[0] or 0) == 2)


def _apply_operblock_schema(cursor: sqlite3.Cursor) -> None:
    conn = cursor.connection

    _ensure_column(conn, "admissions", "unit_scope", "TEXT", logger)
    _ensure_column(conn, "admissions", "admission_type", "TEXT", logger)

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS operating_tables (
            code TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            sort_order INTEGER NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now')),
            updated_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now')),
            last_modified_by TEXT,
            revision INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS operation_cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            admission_id INTEGER NOT NULL,
            table_code TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now')),
            started_at TEXT NOT NULL,
            ended_at TEXT,
            created_by_role TEXT NOT NULL DEFAULT 'operblock',
            created_by_client_id TEXT,
            revision INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now')),
            last_modified_by TEXT,
            future_rao_admission_id INTEGER,
            planned_operation_name TEXT,
            planned_anesthesia_assistance_type TEXT,
            planned_surgeons_json TEXT,
            planned_operating_nurse TEXT,
            planned_anesthesiologist TEXT,
            planned_anesthetist TEXT,
            height_cm INTEGER,
            weight_kg REAL,
            allergies TEXT,
            blood_group TEXT,
            blood_rh TEXT,
            preop_sys INTEGER,
            preop_dia INTEGER,
            preop_pulse INTEGER,
            preop_spo2 INTEGER,
            preop_save_initial_vitals INTEGER NOT NULL DEFAULT 1,
            anesthesia_protocol_number INTEGER,
            anesthesia_protocol_date TEXT,
            transfer_department TEXT,
            offline_case_uuid TEXT,
            offline_session_id TEXT,
            migration_status TEXT,
            migrated_at TEXT,
            migrated_remote_id INTEGER,
            original_local_id INTEGER,
            original_protocol_number INTEGER,
            excluded_from_migration INTEGER NOT NULL DEFAULT 0,
            CHECK (table_code IN ('emergency', 'planned')),
            CHECK (status IN ('active', 'closed', 'transferred_to_rao', 'cancelled')),
            CHECK (ended_at IS NULL OR ended_at >= started_at),
            FOREIGN KEY (patient_id) REFERENCES patients(id),
            FOREIGN KEY (admission_id) REFERENCES admissions(id),
            FOREIGN KEY (table_code) REFERENCES operating_tables(code),
            FOREIGN KEY (future_rao_admission_id) REFERENCES admissions(id)
        )
        """
    )
    _ensure_column(conn, "operation_cases", "planned_operation_name", "TEXT", logger)
    _ensure_column(conn, "operation_cases", "planned_anesthesia_assistance_type", "TEXT", logger)
    _ensure_column(conn, "operation_cases", "planned_surgeons_json", "TEXT", logger)
    _ensure_column(conn, "operation_cases", "planned_operating_nurse", "TEXT", logger)
    _ensure_column(conn, "operation_cases", "planned_anesthesiologist", "TEXT", logger)
    _ensure_column(conn, "operation_cases", "planned_anesthetist", "TEXT", logger)
    _ensure_column(conn, "operation_cases", "height_cm", "INTEGER", logger)
    _ensure_column(conn, "operation_cases", "weight_kg", "REAL", logger)
    _ensure_column(conn, "operation_cases", "allergies", "TEXT", logger)
    _ensure_column(conn, "operation_cases", "blood_group", "TEXT", logger)
    _ensure_column(conn, "operation_cases", "blood_rh", "TEXT", logger)
    _ensure_column(conn, "operation_cases", "preop_sys", "INTEGER", logger)
    _ensure_column(conn, "operation_cases", "preop_dia", "INTEGER", logger)
    _ensure_column(conn, "operation_cases", "preop_pulse", "INTEGER", logger)
    _ensure_column(conn, "operation_cases", "preop_spo2", "INTEGER", logger)
    _ensure_column(conn, "operation_cases", "preop_save_initial_vitals", "INTEGER NOT NULL DEFAULT 1", logger)
    _ensure_column(conn, "operation_cases", "anesthesia_protocol_number", "INTEGER", logger)
    _ensure_column(conn, "operation_cases", "anesthesia_protocol_date", "TEXT", logger)
    _ensure_column(conn, "operation_cases", "transfer_department", "TEXT", logger)
    _ensure_column(conn, "operation_cases", "future_rao_admission_id", "INTEGER", logger)
    _ensure_column(conn, "operation_cases", "offline_case_uuid", "TEXT", logger)
    _ensure_column(conn, "operation_cases", "offline_session_id", "TEXT", logger)
    _ensure_column(conn, "operation_cases", "migration_status", "TEXT", logger)
    _ensure_column(conn, "operation_cases", "migrated_at", "TEXT", logger)
    _ensure_column(conn, "operation_cases", "migrated_remote_id", "INTEGER", logger)
    _ensure_column(conn, "operation_cases", "original_local_id", "INTEGER", logger)
    _ensure_column(conn, "operation_cases", "original_protocol_number", "INTEGER", logger)
    _ensure_column(conn, "operation_cases", "excluded_from_migration", "INTEGER NOT NULL DEFAULT 0", logger)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS operation_table_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_case_id INTEGER NOT NULL,
            table_code TEXT NOT NULL,
            assigned_at TEXT NOT NULL,
            released_at TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_by_role TEXT NOT NULL DEFAULT 'operblock',
            created_by_client_id TEXT,
            revision INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now')),
            last_modified_by TEXT,
            CHECK (table_code IN ('emergency', 'planned')),
            CHECK (status IN ('active', 'released', 'cancelled')),
            CHECK (released_at IS NULL OR released_at >= assigned_at),
            FOREIGN KEY (operation_case_id) REFERENCES operation_cases(id),
            FOREIGN KEY (table_code) REFERENCES operating_tables(code)
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS operblock_timeline_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_case_id INTEGER NOT NULL,
            admission_id INTEGER NOT NULL,
            table_code TEXT,
            event_type TEXT NOT NULL,
            event_time TEXT NOT NULL,
            end_time TEXT,
            drug_label TEXT,
            display_label TEXT,
            raw_text TEXT,
            dose_value TEXT,
            dose_unit TEXT,
            volume_ml TEXT,
            concentration_text TEXT,
            rate_value TEXT,
            rate_unit TEXT,
            route TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            revision INTEGER NOT NULL DEFAULT 1,
            source_order_id INTEGER,
            parent_event_id INTEGER,
            payload_json TEXT,
            created_by_role TEXT,
            created_by_client_id TEXT,
            created_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now')),
            updated_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now')),
            last_modified_by TEXT,
            CHECK (event_type IN ('bolus', 'infusion_start', 'infusion_change', 'infusion_stop', 'clinical_event', 'note')),
            CHECK (status IN ('active', 'stopped', 'deleted', 'cancelled')),
            CHECK (table_code IS NULL OR table_code IN ('emergency', 'planned')),
            CHECK (end_time IS NULL OR end_time >= event_time),
            FOREIGN KEY (operation_case_id) REFERENCES operation_cases(id),
            FOREIGN KEY (admission_id) REFERENCES admissions(id),
            FOREIGN KEY (source_order_id) REFERENCES orders(id),
            FOREIGN KEY (parent_event_id) REFERENCES operblock_timeline_events(id)
        )
        """
    )
    cursor.execute(
        """
        INSERT INTO operating_tables (code, display_name, sort_order, last_modified_by)
        VALUES
            ('emergency', 'Экстренная операционная', 1, 'operblock'),
            ('planned', 'Плановая операционная', 2, 'operblock')
        ON CONFLICT(code) DO UPDATE SET
            display_name = excluded.display_name,
            sort_order = excluded.sort_order,
            is_active = 1,
            last_modified_by = 'operblock',
            revision = COALESCE(operating_tables.revision, 0) + 1
        """
    )
    cursor.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_operation_cases_one_active_per_table
        ON operation_cases(table_code)
        WHERE status = 'active'
        """
    )
    cursor.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_operation_assignments_one_active_per_table
        ON operation_table_assignments(table_code)
        WHERE status = 'active' AND released_at IS NULL
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_operation_cases_admission ON operation_cases(admission_id, status, id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_operation_cases_patient ON operation_cases(patient_id, status, id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_operation_cases_updated ON operation_cases(updated_at, id)"
    )
    _backfill_anesthesia_protocol_numbers(cursor)
    cursor.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_operation_cases_protocol_sequence
        ON operation_cases(table_code, anesthesia_protocol_date, anesthesia_protocol_number)
        WHERE anesthesia_protocol_number IS NOT NULL
          AND anesthesia_protocol_date IS NOT NULL
        """
    )
    cursor.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_operation_cases_offline_uuid
        ON operation_cases(offline_case_uuid)
        WHERE offline_case_uuid IS NOT NULL AND offline_case_uuid <> ''
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_operation_cases_migration_status
        ON operation_cases(migration_status, migrated_at, status)
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_operation_assignments_case ON operation_table_assignments(operation_case_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_admissions_scope_type ON admissions(unit_scope, admission_type, is_active)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_operblock_timeline_admission_time ON operblock_timeline_events(admission_id, event_time)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_operblock_timeline_case_time ON operblock_timeline_events(operation_case_id, event_time)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_operblock_timeline_status ON operblock_timeline_events(status)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_operblock_timeline_event_type ON operblock_timeline_events(event_type)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_operblock_timeline_parent ON operblock_timeline_events(parent_event_id)"
    )

    _create_updated_at_trigger(conn, "operating_tables")
    _create_updated_at_trigger(conn, "operation_cases")
    _create_updated_at_trigger(conn, "operation_table_assignments")
    _create_updated_at_trigger(conn, "operblock_timeline_events")
    _create_change_triggers(
        conn,
        "operating_tables",
        "NEW.rowid",
        "OLD.rowid",
        "NULL",
        "NULL",
        "COALESCE(NEW.last_modified_by, 'operblock')",
        "COALESCE(OLD.last_modified_by, 'operblock')",
        use_updated_at_gate=False,
    )
    _create_change_triggers(
        conn,
        "operation_cases",
        "NEW.id",
        "OLD.id",
        "NEW.admission_id",
        "OLD.admission_id",
        "COALESCE(NEW.last_modified_by, NEW.created_by_role, 'operblock')",
        "COALESCE(OLD.last_modified_by, OLD.created_by_role, 'operblock')",
        use_updated_at_gate=False,
    )
    _create_change_triggers(
        conn,
        "operation_table_assignments",
        "NEW.id",
        "OLD.id",
        "(SELECT admission_id FROM operation_cases WHERE id = NEW.operation_case_id)",
        "(SELECT admission_id FROM operation_cases WHERE id = OLD.operation_case_id)",
        "COALESCE(NEW.last_modified_by, NEW.created_by_role, 'operblock')",
        "COALESCE(OLD.last_modified_by, OLD.created_by_role, 'operblock')",
        use_updated_at_gate=False,
    )
    _create_change_triggers(
        conn,
        "operblock_timeline_events",
        "NEW.id",
        "OLD.id",
        "NEW.admission_id",
        "OLD.admission_id",
        "COALESCE(NEW.last_modified_by, NEW.created_by_role, 'operblock')",
        "COALESCE(OLD.last_modified_by, OLD.created_by_role, 'operblock')",
        use_updated_at_gate=False,
    )
    _mark_schema_migration(conn, 1001, "operblock operation cases and table assignments")
    _mark_schema_migration(conn, 1006, "operblock anesthesia protocol numbers and transfer target")
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS opblock_offline_case_map (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            offline_case_uuid TEXT NOT NULL,
            offline_session_id TEXT,
            local_operation_case_id INTEGER,
            remote_operation_case_id INTEGER,
            original_protocol_number INTEGER,
            network_protocol_number INTEGER,
            content_hash TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now')),
            updated_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now')),
            UNIQUE(offline_case_uuid)
        )
        """
    )
    _create_updated_at_trigger(conn, "opblock_offline_case_map")
    _mark_schema_migration(conn, OPERBLOCK_SCHEMA_VERSION, "operblock planned anesthesia assistance type")


def _backfill_anesthesia_protocol_numbers(cursor: sqlite3.Cursor) -> None:
    conn = cursor.connection
    if not _table_exists(conn, "operation_cases") or not _table_exists(conn, "operblock_timeline_events"):
        return
    columns = _columns(conn, "operation_cases")
    if not {"anesthesia_protocol_number", "anesthesia_protocol_date"}.issubset(columns):
        return

    next_numbers: dict[tuple[str, str], int] = {}
    for row in cursor.execute(
        """
        SELECT table_code, anesthesia_protocol_date, MAX(anesthesia_protocol_number) AS max_number
        FROM operation_cases
        WHERE anesthesia_protocol_number IS NOT NULL
          AND anesthesia_protocol_date IS NOT NULL
        GROUP BY table_code, anesthesia_protocol_date
        """
    ).fetchall():
        key = (str(row["table_code"] or ""), str(row["anesthesia_protocol_date"] or ""))
        next_numbers[key] = int(row["max_number"] or 0) + 1

    rows = cursor.execute(
        """
        SELECT
            oc.id,
            oc.table_code,
            MIN(ote.event_time) AS first_anesthesia_start
        FROM operation_cases oc
        JOIN operblock_timeline_events ote ON ote.operation_case_id = oc.id
        WHERE (oc.anesthesia_protocol_number IS NULL OR oc.anesthesia_protocol_date IS NULL)
          AND ote.event_type = 'clinical_event'
          AND COALESCE(ote.status, '') NOT IN ('deleted', 'cancelled')
          AND COALESCE(ote.payload_json, '') LIKE '%anesthesia_start%'
        GROUP BY oc.id, oc.table_code
        ORDER BY oc.table_code, datetime(MIN(ote.event_time)), oc.id
        """
    ).fetchall()

    candidates: list[tuple[str, str, str, int]] = []
    for row in rows:
        raw_started_at = str(row["first_anesthesia_start"] or "")
        try:
            started_at = datetime.fromisoformat(raw_started_at.replace(" ", "T"))
        except Exception:
            continue
        candidates.append(
            (
                str(row["table_code"] or ""),
                started_at.date().isoformat(),
                started_at.isoformat(timespec="seconds"),
                int(row["id"]),
            )
        )
    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]))

    for table_code, protocol_date, _started_at, case_id in candidates:
        key = (table_code, protocol_date)
        protocol_number = int(next_numbers.get(key, 1))
        next_numbers[key] = protocol_number + 1
        cursor.execute(
            """
            UPDATE operation_cases
            SET anesthesia_protocol_number = ?,
                anesthesia_protocol_date = ?,
                revision = COALESCE(revision, 0) + 1,
                last_modified_by = 'operblock'
            WHERE id = ?
              AND (anesthesia_protocol_number IS NULL OR anesthesia_protocol_date IS NULL)
            """,
            (protocol_number, protocol_date, case_id),
        )


def _run_checks(db_manager: Any) -> tuple[str, str]:
    conn = getattr(db_manager, "_remcard_conn", None)
    controller = getattr(db_manager, "write_controller", None)
    if conn is None or controller is None:
        raise RuntimeError("Соединение с БД оперблока не готово для проверки схемы.")
    with controller.connection_guard(conn):
        quick_ok, quick_result = run_quick_check(conn)
        integrity_ok, integrity_result = run_integrity_check(conn)
    if not quick_ok:
        raise sqlite3.DatabaseError(f"quick_check после миграции оперблока не прошёл: {quick_result}")
    if not integrity_ok:
        raise sqlite3.DatabaseError(f"integrity_check после миграции оперблока не прошёл: {integrity_result}")
    return str(quick_result), str(integrity_result)


def ensure_operblock_schema(db_manager: Any) -> OperBlockSchemaResult:
    conn = getattr(db_manager, "_remcard_conn", None)
    controller = getattr(db_manager, "write_controller", None)
    if conn is None or controller is None:
        raise RuntimeError("Соединение с БД оперблока не готово.")

    with controller.connection_guard(conn):
        if is_operblock_schema_ready(conn):
            return OperBlockSchemaResult(migrated=False)

    backup_path = ""
    create_backup = getattr(db_manager, "create_validated_backup", None)
    if callable(create_backup):
        backup_path = str(create_backup(prefix="operblock_pre_migration", source="operblock_schema") or "")
    if not backup_path:
        raise RuntimeError("Не удалось создать backup перед миграцией схемы оперблока.")

    db_manager.run_write_operation(_apply_operblock_schema, source="operblock_schema_migration")

    with controller.connection_guard(conn):
        if not is_operblock_schema_ready(conn):
            raise RuntimeError("Миграция схемы оперблока завершилась, но schema contract не выполнен.")

    quick_check, integrity_check = _run_checks(db_manager)
    logger.info(
        "Operblock schema migration complete migrated=true backup_path=%s quick_check=%s integrity_check=%s",
        backup_path,
        quick_check,
        integrity_check,
    )
    return OperBlockSchemaResult(
        migrated=True,
        backup_path=backup_path,
        quick_check=quick_check,
        integrity_check=integrity_check,
    )

