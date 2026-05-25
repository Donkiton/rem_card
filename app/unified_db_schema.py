import logging
import os
import sqlite3
from typing import Optional

SCHEMA_FASTPATH_META_KEY = "unified_schema_fastpath_rev"
SCHEMA_FASTPATH_REV = 17
SCHEMA_MIN_MIGRATION_VERSION = 17
SCHEMA_REQUIRED_CLIENT_VERSION = "2.0.0"
USE_META_VERSION_IN_CHANGE_TRIGGERS = os.environ.get("REMCARD_CHANGELOG_META_VERSION", "0") == "1"

_FASTPATH_REQUIRED_TABLES: tuple[str, ...] = (
    "patients",
    "admissions",
    "beds",
    "operations",
    "ivl_episodes",
    "transfusions",
    "clinical_events",
    "devices",
    "respiratory_support",
    "drugs",
    "meta",
    "change_log",
    "sync_applied_ops",
    "schema_migrations",
    "vitals",
    "vital_settings",
    "fluids",
    "orders",
    "administrations",
    "patient_status_events",
    "order_audit_log",
    "medical_audit_log",
    "diet_templates",
    "diet_plan",
    "oral_intake_events",
    "procedures",
    "lab_orders",
    "procedure_consents",
    "procedure_cvc",
    "procedure_lumbar_puncture",
    "procedure_transfusion",
)

_FASTPATH_REQUIRED_COLUMNS: dict[str, set[str]] = {
    "patients": {"admission_uid", "birth_date", "last_name", "first_name", "middle_name"},
    "admissions": {
        "death_datetime",
        "operation_description",
        "operation_description_2",
        "is_active",
        "intake_extra_json",
        "clinical_death_datetime",
        "cardiac_arrest_cause",
        "cardiac_arrest_measures_json",
        "emergency_notice_number",
        "emergency_notice_entered_at",
        "revision",
    },
    "beds": {"revision"},
    "ivl_episodes": {"start_type", "delivery_type", "is_active", "revision"},
    "clinical_events": {"ivl_episode_id", "mode", "parameters_json", "extubation_reason", "o2_flow", "revision"},
    "devices": {"ivl_episode_id", "replacement_time"},
    "respiratory_support": {"ivl_episode_id", "event_id", "parameters_json"},
    "vitals": {"rr", "gcs", "cvp", "last_modified_by", "updated_at", "revision"},
    "vital_settings": {"rr", "cvp", "updated_at", "last_modified_by"},
    "fluids": {"other_output", "last_modified_by", "updated_at", "revision"},
    "orders": {
        "drug_key",
        "latin",
        "type",
        "dose_value",
        "dose_unit",
        "is_per_kg",
        "frequency",
        "specific_times",
        "rate_ml_h",
        "volume_total",
        "duration_min",
        "sort_order",
        "draft_sort_order",
        "is_finalized",
        "is_committed",
        "revision",
        "comment",
        "last_modified_by",
        "updated_at",
    },
    "administrations": {
        "chain_id",
        "big_chain_id",
        "ui_color",
        "ui_color_until",
        "version",
        "comment",
        "dose_given",
        "volume_ml",
        "is_committed",
        "last_modified_by",
        "updated_at",
    },
    "patient_status_events": {"last_modified_by", "updated_at", "revision"},
    "transfusions": {"source", "source_order_id", "source_admin_id"},
    "diet_templates": {"name", "diet_text", "schedule_json", "is_default", "created_at", "version", "last_modified_by", "updated_at"},
    "diet_plan": {"admission_id", "shift_start", "template_id", "diet_text", "schedule_json", "created_at", "version", "last_modified_by", "updated_at"},
    "oral_intake_events": {"admission_id", "shift_start", "event_time", "amount_ml", "created_at", "version", "last_modified_by", "updated_at"},
    "procedures": {"patient_id", "admission_id", "procedure_type", "status", "patient_snapshot_json", "diagnosis_snapshot", "revision", "is_deleted"},
    "lab_orders": {
        "patient_id",
        "admission_id",
        "card_day_id",
        "analysis_code",
        "analysis_name",
        "material",
        "status",
        "created_at",
        "scheduled_at",
        "completed_at",
        "comment",
        "created_by_role",
        "created_by_user",
        "completed_by_role",
        "completed_by_user",
        "revision",
        "created_at_db",
        "updated_at",
    },
    "procedure_consents": {"procedure_id", "consent_kind", "consent_mode", "patient_signed", "consilium_json", "revision"},
    "procedure_cvc": {"procedure_id", "indications_json", "access_code", "catheter_status", "removed_or_replaced", "revision"},
    "procedure_lumbar_puncture": {"procedure_id", "indications_json", "access_code", "level_code", "result_code", "revision"},
    "procedure_transfusion": {"procedure_id", "indication_code", "request_at", "donor_code", "observation_json", "revision"},
}

_FASTPATH_REQUIRED_INDEXES: tuple[str, ...] = (
    "idx_change_log_admission_id",
    "idx_change_log_entity",
    "idx_vitals_admission_updated_id",
    "idx_fluids_admission_updated_id",
    "idx_orders_admission_updated_id",
    "idx_status_admission_updated_id",
    "idx_admin_committed_order_planned_id",
    "idx_transfusions_source_admin",
    "idx_orders_admission_status_committed_time",
    "idx_active_status",
    "idx_diet_plan_admission_shift",
    "idx_oral_intake_admission_event_time",
    "idx_oral_intake_admission_shift",
    "idx_medical_audit_admission_changed",
    "idx_medical_audit_table_row",
    "idx_medical_audit_operation",
    "idx_procedures_admission_type_time",
    "idx_lab_orders_admission_card_day",
    "idx_lab_orders_admission_scheduled",
    "idx_lab_orders_updated_at",
    "idx_procedure_consents_procedure",
    "idx_procedure_cvc_catheter_status",
    "idx_procedure_transfusion_indication",
)

_UPDATED_AT_TRIGGER_TABLES: tuple[str, ...] = (
    "vitals",
    "vital_settings",
    "fluids",
    "orders",
    "administrations",
    "patient_status_events",
    "diet_templates",
    "diet_plan",
    "oral_intake_events",
    "procedures",
    "lab_orders",
)

_CHANGE_TRIGGER_TABLES: tuple[str, ...] = (
    "vitals",
    "vital_settings",
    "fluids",
    "orders",
    "administrations",
    "patient_status_events",
    "patients",
    "admissions",
    "beds",
    "operations",
    "ivl_episodes",
    "transfusions",
    "clinical_events",
    "devices",
    "respiratory_support",
    "diet_templates",
    "diet_plan",
    "oral_intake_events",
    "procedures",
    "lab_orders",
    "procedure_consents",
    "procedure_cvc",
    "procedure_lumbar_puncture",
    "procedure_transfusion",
)

_MEDICAL_AUDIT_TABLES: tuple[str, ...] = (
    "orders",
    "administrations",
    "vitals",
    "fluids",
    "admissions",
    "beds",
    "patient_status_events",
    "ivl_episodes",
    "clinical_events",
    "diet_plan",
    "oral_intake_events",
    "lab_orders",
)

_FASTPATH_REQUIRED_TRIGGERS: tuple[str, ...] = tuple(
    [f"trg_{table}_updated_at" for table in _UPDATED_AT_TRIGGER_TABLES]
    + [f"trg_{table}_version_{suffix}" for table in _CHANGE_TRIGGER_TABLES for suffix in ("ins", "upd", "del")]
    + [f"trg_{table}_medical_audit_{suffix}" for table in _MEDICAL_AUDIT_TABLES for suffix in ("ins", "upd", "del")]
)


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return bool(row)


def _trigger_exists(conn: sqlite3.Connection, trigger_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
        (trigger_name,),
    ).fetchone()
    return bool(row)


def _index_exists(conn: sqlite3.Connection, index_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,),
    ).fetchone()
    return bool(row)


def _meta_int_value(conn: sqlite3.Connection, key: str) -> Optional[int]:
    if not _table_exists(conn, "meta"):
        return None
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if not row or row[0] is None:
        return None
    try:
        return int(row[0])
    except Exception:
        return None


def _set_meta_int_value(conn: sqlite3.Connection, key: str, value: int):
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        (str(key), int(value)),
    )


def _schema_migration_applied(conn: sqlite3.Connection, min_version: int) -> bool:
    if not _table_exists(conn, "schema_migrations"):
        return False
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    try:
        return bool(row and row[0] is not None and int(row[0]) >= int(min_version))
    except Exception:
        return False


def _all_sqlite_master_objects_exist(conn: sqlite3.Connection, obj_type: str, names: tuple[str, ...]) -> bool:
    if not names:
        return True
    placeholders = ",".join("?" for _ in names)
    rows = conn.execute(
        f"SELECT name FROM sqlite_master WHERE type = ? AND name IN ({placeholders})",
        (obj_type, *names),
    ).fetchall()
    existing = {row[0] for row in rows}
    return all(name in existing for name in names)


def _schema_contract_satisfied(conn: sqlite3.Connection, deep_column_check: bool) -> bool:
    if not _all_sqlite_master_objects_exist(conn, "table", _FASTPATH_REQUIRED_TABLES):
        return False

    if not _schema_migration_applied(conn, SCHEMA_MIN_MIGRATION_VERSION):
        return False

    if not all(_index_exists(conn, name) for name in _FASTPATH_REQUIRED_INDEXES):
        return False

    if not _all_sqlite_master_objects_exist(conn, "trigger", _FASTPATH_REQUIRED_TRIGGERS):
        return False

    if not deep_column_check:
        return True

    for table_name, required_columns in _FASTPATH_REQUIRED_COLUMNS.items():
        if not required_columns.issubset(_get_columns(conn, table_name)):
            return False

    return True


def _is_fastpath_schema_ready(conn: sqlite3.Connection) -> bool:
    # Fast route for subsequent startups: marker + lightweight object contract.
    if _meta_int_value(conn, SCHEMA_FASTPATH_META_KEY) == SCHEMA_FASTPATH_REV:
        return _schema_contract_satisfied(conn, deep_column_check=False)

    # Compatibility route: older DBs may already be fully migrated but without marker.
    if _schema_contract_satisfied(conn, deep_column_check=True):
        _set_meta_int_value(conn, SCHEMA_FASTPATH_META_KEY, SCHEMA_FASTPATH_REV)
        return True

    return False


def is_unified_schema_ready(conn: sqlite3.Connection) -> bool:
    return _is_fastpath_schema_ready(conn)


def _get_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _ensure_column(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
    definition: str,
    logger: Optional[logging.Logger] = None,
):
    logger = logger or logging.getLogger(__name__)
    columns = _get_columns(conn, table_name)
    if column_name in columns:
        return
    logger.info("Adding missing column %s.%s", table_name, column_name)
    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def _drop_trigger(conn: sqlite3.Connection, trigger_name: str):
    if _trigger_exists(conn, trigger_name):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")


def _create_updated_at_trigger(conn: sqlite3.Connection, table_name: str):
    trigger_name = f"trg_{table_name}_updated_at"
    _drop_trigger(conn, trigger_name)
    conn.execute(
        f"""
        CREATE TRIGGER {trigger_name}
        AFTER UPDATE ON {table_name}
        WHEN OLD.updated_at = NEW.updated_at OR NEW.updated_at IS NULL
        BEGIN
            UPDATE {table_name}
            SET updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
            WHERE rowid = NEW.rowid;
        END;
        """
    )


def _mark_schema_migration(conn: sqlite3.Connection, version: int, note: str):
    conn.execute(
        """
        INSERT OR IGNORE INTO schema_migrations (version, note)
        VALUES (?, ?)
        """,
        (int(version), str(note or "")),
    )


def _create_change_triggers(
    conn: sqlite3.Connection,
    table_name: str,
    entity_id_expr_new: str,
    entity_id_expr_old: str,
    admission_id_expr_new: str,
    admission_id_expr_old: str,
    changed_by_expr_new: str,
    changed_by_expr_old: str,
    use_updated_at_gate: bool = False,
):
    trigger_insert = f"trg_{table_name}_version_ins"
    trigger_update = f"trg_{table_name}_version_upd"
    trigger_delete = f"trg_{table_name}_version_del"

    _drop_trigger(conn, trigger_insert)
    _drop_trigger(conn, trigger_update)
    _drop_trigger(conn, trigger_delete)

    version_bump_sql = "UPDATE meta SET value = value + 1 WHERE key = 'app_data_version';" if USE_META_VERSION_IN_CHANGE_TRIGGERS else ""
    version_expr = "(SELECT value FROM meta WHERE key = 'app_data_version')" if USE_META_VERSION_IN_CHANGE_TRIGGERS else "NULL"

    conn.execute(
        f"""
        CREATE TRIGGER {trigger_insert}
        AFTER INSERT ON {table_name}
        BEGIN
            {version_bump_sql}
            INSERT INTO change_log (
                entity_name, entity_id, admission_id, action, changed_by, version
            )
            VALUES (
                '{table_name}',
                {entity_id_expr_new},
                {admission_id_expr_new},
                'insert',
                {changed_by_expr_new},
                {version_expr}
            );
        END;
        """
    )

    when_clause = ""
    if use_updated_at_gate:
        when_clause = "WHEN OLD.updated_at != NEW.updated_at"

    conn.execute(
        f"""
        CREATE TRIGGER {trigger_update}
        AFTER UPDATE ON {table_name}
        {when_clause}
        BEGIN
            {version_bump_sql}
            INSERT INTO change_log (
                entity_name, entity_id, admission_id, action, changed_by, version
            )
            VALUES (
                '{table_name}',
                {entity_id_expr_new},
                {admission_id_expr_new},
                'update',
                {changed_by_expr_new},
                {version_expr}
            );
        END;
        """
    )

    conn.execute(
        f"""
        CREATE TRIGGER {trigger_delete}
        AFTER DELETE ON {table_name}
        BEGIN
            {version_bump_sql}
            INSERT INTO change_log (
                entity_name, entity_id, admission_id, action, changed_by, version
            )
            VALUES (
                '{table_name}',
                {entity_id_expr_old},
                {admission_id_expr_old},
                'delete',
                {changed_by_expr_old},
                {version_expr}
            );
        END;
        """
    )


def _json_object_expr(alias: str, fields: tuple[str, ...]) -> str:
    parts = []
    for field in fields:
        parts.append(f"'{field}'")
        parts.append(f"{alias}.{field}")
    return f"json_object({', '.join(parts)})"


def _create_medical_audit_triggers(
    conn: sqlite3.Connection,
    table_name: str,
    entity_id_expr_new: str,
    entity_id_expr_old: str,
    admission_id_expr_new: str,
    admission_id_expr_old: str,
    changed_by_expr_new: str,
    changed_by_expr_old: str,
    fields: tuple[str, ...],
    *,
    use_updated_at_gate: bool = False,
):
    trigger_insert = f"trg_{table_name}_medical_audit_ins"
    trigger_update = f"trg_{table_name}_medical_audit_upd"
    trigger_delete = f"trg_{table_name}_medical_audit_del"

    _drop_trigger(conn, trigger_insert)
    _drop_trigger(conn, trigger_update)
    _drop_trigger(conn, trigger_delete)

    payload_new = _json_object_expr("NEW", fields)
    payload_old = _json_object_expr("OLD", fields)
    update_when = ""
    if use_updated_at_gate:
        compared_fields = tuple(field for field in fields if field != "updated_at")
        update_when = "WHEN " + " OR ".join(f"OLD.{field} IS NOT NEW.{field}" for field in compared_fields)

    conn.execute(
        f"""
        CREATE TRIGGER {trigger_insert}
        AFTER INSERT ON {table_name}
        BEGIN
            INSERT INTO medical_audit_log (
                operation_id, table_name, row_id, admission_id, action_type, changed_by, before_json, after_json
            )
            VALUES (
                LOWER(HEX(RANDOMBLOB(16))),
                '{table_name}',
                {entity_id_expr_new},
                {admission_id_expr_new},
                'insert',
                {changed_by_expr_new},
                NULL,
                {payload_new}
            );
        END;
        """
    )

    conn.execute(
        f"""
        CREATE TRIGGER {trigger_update}
        AFTER UPDATE ON {table_name}
        {update_when}
        BEGIN
            INSERT INTO medical_audit_log (
                operation_id, table_name, row_id, admission_id, action_type, changed_by, before_json, after_json
            )
            VALUES (
                LOWER(HEX(RANDOMBLOB(16))),
                '{table_name}',
                {entity_id_expr_new},
                {admission_id_expr_new},
                'update',
                {changed_by_expr_new},
                {payload_old},
                {payload_new}
            );
        END;
        """
    )

    conn.execute(
        f"""
        CREATE TRIGGER {trigger_delete}
        AFTER DELETE ON {table_name}
        BEGIN
            INSERT INTO medical_audit_log (
                operation_id, table_name, row_id, admission_id, action_type, changed_by, before_json, after_json
            )
            VALUES (
                LOWER(HEX(RANDOMBLOB(16))),
                '{table_name}',
                {entity_id_expr_old},
                {admission_id_expr_old},
                'delete',
                {changed_by_expr_old},
                {payload_old},
                NULL
            );
        END;
        """
    )


def ensure_unified_schema(conn: sqlite3.Connection, logger: Optional[logging.Logger] = None):
    logger = logger or logging.getLogger(__name__)
    if _is_fastpath_schema_ready(conn):
        return

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS patients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            admission_uid TEXT,
            birth_date TEXT,
            last_name TEXT,
            first_name TEXT,
            middle_name TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS admissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            bed_number INTEGER NOT NULL,
            history_number TEXT NOT NULL,
            admission_datetime DATETIME NOT NULL,
            patient_age INTEGER,
            patient_months INTEGER,
            patient_age_unit TEXT,
            patient_gender TEXT,
            diagnosis_code TEXT,
            diagnosis_text TEXT,
            department_profile TEXT,
            source_department TEXT,
            transfer_datetime DATETIME,
            transfer_department TEXT,
            outcome TEXT,
            transfer_lpu TEXT,
            transfer_lpu_other TEXT,
            death_datetime DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            operation_description TEXT,
            operation_description_2 TEXT,
            is_active BOOLEAN DEFAULT 1,
            intake_extra_json TEXT,
            clinical_death_datetime DATETIME,
            cardiac_arrest_cause TEXT,
            cardiac_arrest_measures_json TEXT,
            emergency_notice_number TEXT,
            emergency_notice_entered_at DATETIME,
            revision INTEGER DEFAULT 0,
            FOREIGN KEY (patient_id) REFERENCES patients(id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS beds (
            bed_number INTEGER PRIMARY KEY,
            status TEXT NOT NULL,
            current_admission_id INTEGER,
            revision INTEGER DEFAULT 0,
            FOREIGN KEY (current_admission_id) REFERENCES admissions(id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admission_id INTEGER NOT NULL,
            operation_number INTEGER NOT NULL,
            description TEXT NOT NULL,
            operation_datetime DATETIME NOT NULL,
            FOREIGN KEY (admission_id) REFERENCES admissions(id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ivl_episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admission_id INTEGER NOT NULL,
            episode_number INTEGER NOT NULL,
            start_time DATETIME NOT NULL,
            end_time DATETIME,
            type TEXT NOT NULL,
            revision INTEGER DEFAULT 0,
            FOREIGN KEY (admission_id) REFERENCES admissions(id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transfusions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admission_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            volume_ml INTEGER NOT NULL,
            datetime DATETIME NOT NULL,
            source TEXT DEFAULT 'journal',
            source_order_id INTEGER,
            source_admin_id INTEGER,
            FOREIGN KEY (admission_id) REFERENCES admissions(id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS clinical_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admission_id INTEGER NOT NULL,
            timestamp DATETIME NOT NULL,
            event_type TEXT NOT NULL,
            author TEXT,
            data TEXT,
            revision INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (admission_id) REFERENCES admissions(id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admission_id INTEGER NOT NULL,
            device_type TEXT NOT NULL,
            insertion_date DATETIME,
            removal_date DATETIME,
            location TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (admission_id) REFERENCES admissions(id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS respiratory_support (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admission_id INTEGER NOT NULL,
            datetime DATETIME NOT NULL,
            mode TEXT,
            fio2 REAL,
            peep REAL,
            tv REAL,
            rr INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (admission_id) REFERENCES admissions(id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lab_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admission_id INTEGER NOT NULL,
            datetime DATETIME NOT NULL,
            platelets REAL,
            bilirubin REAL,
            creatinine REAL,
            lactate REAL,
            pao2 REAL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (admission_id) REFERENCES admissions(id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS drugs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            template TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value INTEGER
        )
        """
    )
    conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('app_data_version', 1)")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS change_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_name TEXT NOT NULL,
            entity_id INTEGER,
            admission_id INTEGER,
            action TEXT NOT NULL,
            changed_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now')),
            changed_by TEXT,
            version INTEGER
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_applied_ops (
            op_id TEXT PRIMARY KEY,
            source TEXT,
            node_id TEXT,
            applied_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now')),
            note TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vitals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admission_id INTEGER NOT NULL,
            datetime DATETIME NOT NULL,
            sys INTEGER,
            dia INTEGER,
            pulse INTEGER,
            temp REAL,
            spo2 INTEGER,
            rr INTEGER,
            gcs INTEGER,
            cvp INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_modified_by TEXT,
            updated_at TEXT,
            revision INTEGER DEFAULT 0,
            FOREIGN KEY (admission_id) REFERENCES admissions(id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vital_settings (
            admission_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            ad INTEGER DEFAULT 1,
            pulse INTEGER DEFAULT 1,
            temp INTEGER DEFAULT 1,
            spo2 INTEGER DEFAULT 1,
            rr INTEGER DEFAULT 0,
            cvp INTEGER DEFAULT 0,
            updated_at TEXT,
            last_modified_by TEXT,
            PRIMARY KEY (admission_id, date),
            FOREIGN KEY (admission_id) REFERENCES admissions(id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fluids (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admission_id INTEGER NOT NULL,
            datetime DATETIME NOT NULL,
            iv_input REAL DEFAULT 0,
            oral_input REAL DEFAULT 0,
            food REAL DEFAULT 0,
            urine REAL DEFAULT 0,
            ng_output REAL DEFAULT 0,
            drain_output REAL DEFAULT 0,
            stool REAL DEFAULT 0,
            other_output REAL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_modified_by TEXT,
            updated_at TEXT,
            revision INTEGER DEFAULT 0,
            FOREIGN KEY (admission_id) REFERENCES admissions(id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admission_id INTEGER NOT NULL,
            datetime DATETIME NOT NULL,
            text TEXT NOT NULL,
            drug_key TEXT,
            latin TEXT,
            type TEXT,
            status TEXT DEFAULT 'active',
            dose_value REAL,
            dose_unit TEXT,
            is_per_kg BOOLEAN,
            frequency INTEGER,
            specific_times TEXT,
            rate_ml_h REAL,
            volume_total REAL,
            duration_min INTEGER,
            sort_order INTEGER DEFAULT 0,
            draft_sort_order INTEGER,
            is_finalized BOOLEAN DEFAULT 0,
            is_committed INTEGER DEFAULT 0,
            revision INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            comment TEXT,
            last_modified_by TEXT,
            updated_at TEXT,
            FOREIGN KEY (admission_id) REFERENCES admissions(id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS administrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            chain_id TEXT,
            big_chain_id TEXT,
            cell_role TEXT NOT NULL,
            planned_time DATETIME NOT NULL,
            actual_time DATETIME,
            performer_id INTEGER,
            status TEXT NOT NULL,
            ui_color TEXT,
            ui_color_until DATETIME,
            version INTEGER DEFAULT 0,
            comment TEXT,
            dose_given REAL,
            volume_ml REAL DEFAULT 0,
            is_committed INTEGER DEFAULT 0,
            last_modified_by TEXT,
            updated_at TEXT,
            FOREIGN KEY (order_id) REFERENCES orders(id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS patient_status_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admission_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            reason_type TEXT,
            reason_text TEXT,
            start_time DATETIME NOT NULL,
            end_time DATETIME,
            created_by TEXT,
            created_at DATETIME DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now')),
            updated_at DATETIME DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now')),
            last_modified_by TEXT,
            revision INTEGER DEFAULT 0,
            FOREIGN KEY (admission_id) REFERENCES admissions(id),
            CHECK (end_time IS NULL OR end_time >= start_time)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS order_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admission_id INTEGER NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            action_type TEXT NOT NULL,
            payload TEXT,
            is_undone BOOLEAN DEFAULT 0,
            FOREIGN KEY (admission_id) REFERENCES admissions(id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS procedures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER,
            admission_id INTEGER NOT NULL,
            procedure_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            created_at DATETIME DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now')),
            updated_at DATETIME DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now')),
            started_at DATETIME,
            finished_at DATETIME,
            duration_minutes INTEGER,
            doctor_id INTEGER,
            doctor_name_snapshot TEXT,
            department_snapshot TEXT,
            patient_snapshot_json TEXT,
            diagnosis_snapshot TEXT,
            notes TEXT,
            created_by TEXT,
            updated_by TEXT,
            revision INTEGER DEFAULT 0,
            is_deleted INTEGER DEFAULT 0,
            FOREIGN KEY (patient_id) REFERENCES patients(id),
            FOREIGN KEY (admission_id) REFERENCES admissions(id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lab_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            admission_id INTEGER NOT NULL,
            card_day_id TEXT,
            analysis_code TEXT NOT NULL,
            analysis_name TEXT NOT NULL,
            material TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'assigned',
            created_at DATETIME NOT NULL,
            scheduled_at DATETIME NOT NULL,
            completed_at DATETIME,
            comment TEXT,
            created_by_role TEXT NOT NULL DEFAULT 'doctor',
            created_by_user TEXT,
            completed_by_role TEXT,
            completed_by_user TEXT,
            revision INTEGER DEFAULT 0,
            created_at_db DATETIME DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now')),
            updated_at DATETIME DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now')),
            FOREIGN KEY (patient_id) REFERENCES patients(id),
            FOREIGN KEY (admission_id) REFERENCES admissions(id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS procedure_consents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            procedure_id INTEGER NOT NULL,
            consent_kind TEXT NOT NULL,
            consent_mode TEXT NOT NULL DEFAULT 'patient',
            patient_signed INTEGER DEFAULT 1,
            representative_name TEXT,
            representative_details TEXT,
            diagnosis_snapshot TEXT,
            doctor_name_snapshot TEXT,
            consilium_json TEXT,
            emergency_reason TEXT,
            created_at DATETIME DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now')),
            printed_at DATETIME,
            revision INTEGER DEFAULT 0,
            FOREIGN KEY (procedure_id) REFERENCES procedures(id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS procedure_cvc (
            procedure_id INTEGER PRIMARY KEY,
            cvc_code_main_selected INTEGER DEFAULT 1,
            cvc_code_tunneled_selected INTEGER DEFAULT 0,
            indications_json TEXT,
            procedure_place_code TEXT,
            procedure_place_other TEXT,
            anesthesia_code TEXT,
            anesthesia_other TEXT,
            access_code TEXT,
            access_other TEXT,
            method_code TEXT,
            method_other TEXT,
            ultrasound_control_json TEXT,
            attempts_count INTEGER,
            diameter_f REAL,
            length_cm REAL,
            lumens_count INTEGER,
            fixation_json TEXT,
            fixation_other TEXT,
            position_confirmed_at DATETIME,
            position_confirmation_json TEXT,
            technical_difficulty_code TEXT,
            technical_difficulty_description TEXT,
            actions_taken TEXT,
            catheter_status TEXT,
            removed_or_replaced TEXT,
            removed_at DATETIME,
            usage_complications_code TEXT,
            usage_complications_description TEXT,
            additional_treatment TEXT,
            operator_doctor_name TEXT,
            removal_doctor_name TEXT,
            revision INTEGER DEFAULT 0,
            FOREIGN KEY (procedure_id) REFERENCES procedures(id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS procedure_lumbar_puncture (
            procedure_id INTEGER PRIMARY KEY,
            indications_json TEXT,
            procedure_place_code TEXT,
            procedure_place_other TEXT,
            anesthesia_code TEXT,
            anesthesia_other TEXT,
            access_code TEXT,
            access_other TEXT,
            level_code TEXT,
            level_other TEXT,
            technical_difficulty_code TEXT,
            technical_difficulty_description TEXT,
            actions_taken TEXT,
            result_code TEXT,
            csf_characteristics TEXT,
            result_notes TEXT,
            operator_doctor_name TEXT,
            revision INTEGER DEFAULT 0,
            FOREIGN KEY (procedure_id) REFERENCES procedures(id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS procedure_transfusion (
            procedure_id INTEGER PRIMARY KEY,
            request_at DATETIME,
            indication_code TEXT,
            recipient_abo TEXT,
            recipient_rh TEXT,
            recipient_antigens TEXT,
            alloimmune_antibodies TEXT,
            transfusions_history TEXT,
            reactions_history TEXT,
            reactions_history_details TEXT,
            individual_selection_history TEXT,
            donor_component_name TEXT,
            procurement_org TEXT,
            donor_abo TEXT,
            donor_rh TEXT,
            donor_antigens TEXT,
            donor_code TEXT,
            unit_number TEXT,
            volume_ml INTEGER,
            collection_date TEXT,
            expiration_date TEXT,
            selection_medical_org TEXT,
            selection_study_date TEXT,
            selection_responsible_name TEXT,
            selection_conclusion TEXT,
            reagent_anti_a_series TEXT,
            reagent_anti_a_expiration TEXT,
            reagent_anti_b_series TEXT,
            reagent_anti_b_expiration TEXT,
            reagent_anti_d_series TEXT,
            reagent_anti_d_expiration TEXT,
            plane_compatibility TEXT,
            biological_test TEXT,
            reaction_symptoms TEXT,
            reaction_severity TEXT,
            observation_json TEXT,
            operator_doctor_name TEXT,
            revision INTEGER DEFAULT 0,
            FOREIGN KEY (procedure_id) REFERENCES procedures(id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS medical_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_id TEXT NOT NULL,
            table_name TEXT NOT NULL,
            row_id INTEGER,
            admission_id INTEGER,
            action_type TEXT NOT NULL,
            changed_at DATETIME DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now')),
            changed_by TEXT,
            before_json TEXT,
            after_json TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS diet_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            diet_text TEXT,
            schedule_json TEXT NOT NULL DEFAULT '[]',
            is_default INTEGER DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now')),
            updated_at TEXT,
            version INTEGER DEFAULT 1,
            last_modified_by TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS diet_plan (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admission_id INTEGER NOT NULL,
            shift_start TEXT NOT NULL,
            template_id INTEGER,
            diet_text TEXT,
            schedule_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now')),
            updated_at TEXT,
            version INTEGER DEFAULT 1,
            last_modified_by TEXT,
            UNIQUE(admission_id, shift_start),
            FOREIGN KEY (admission_id) REFERENCES admissions(id) ON DELETE CASCADE,
            FOREIGN KEY (template_id) REFERENCES diet_templates(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS oral_intake_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admission_id INTEGER NOT NULL,
            shift_start TEXT NOT NULL,
            event_time TEXT NOT NULL,
            amount_ml REAL NOT NULL CHECK(amount_ml > 0),
            created_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now')),
            updated_at TEXT,
            version INTEGER DEFAULT 1,
            last_modified_by TEXT,
            UNIQUE(admission_id, event_time),
            FOREIGN KEY (admission_id) REFERENCES admissions(id) ON DELETE CASCADE
        )
        """
    )

    _ensure_column(conn, "patients", "admission_uid", "TEXT", logger)
    _ensure_column(conn, "patients", "birth_date", "TEXT", logger)
    _ensure_column(conn, "patients", "last_name", "TEXT", logger)
    _ensure_column(conn, "patients", "first_name", "TEXT", logger)
    _ensure_column(conn, "patients", "middle_name", "TEXT", logger)

    _ensure_column(conn, "admissions", "death_datetime", "DATETIME", logger)
    _ensure_column(conn, "admissions", "operation_description", "TEXT", logger)
    _ensure_column(conn, "admissions", "operation_description_2", "TEXT", logger)
    _ensure_column(conn, "admissions", "is_active", "BOOLEAN DEFAULT 1", logger)
    _ensure_column(conn, "admissions", "intake_extra_json", "TEXT", logger)
    _ensure_column(conn, "admissions", "clinical_death_datetime", "DATETIME", logger)
    _ensure_column(conn, "admissions", "cardiac_arrest_cause", "TEXT", logger)
    _ensure_column(conn, "admissions", "cardiac_arrest_measures_json", "TEXT", logger)
    _ensure_column(conn, "admissions", "emergency_notice_number", "TEXT", logger)
    _ensure_column(conn, "admissions", "emergency_notice_entered_at", "DATETIME", logger)
    _ensure_column(conn, "admissions", "revision", "INTEGER DEFAULT 0", logger)

    _ensure_column(conn, "beds", "revision", "INTEGER DEFAULT 0", logger)

    _ensure_column(conn, "ivl_episodes", "start_type", "TEXT", logger)
    _ensure_column(conn, "ivl_episodes", "delivery_type", "TEXT", logger)
    _ensure_column(conn, "ivl_episodes", "is_active", "INTEGER DEFAULT 1", logger)
    _ensure_column(conn, "ivl_episodes", "revision", "INTEGER DEFAULT 0", logger)

    _ensure_column(conn, "clinical_events", "ivl_episode_id", "INTEGER", logger)
    _ensure_column(conn, "clinical_events", "mode", "TEXT", logger)
    _ensure_column(conn, "clinical_events", "parameters_json", "TEXT", logger)
    _ensure_column(conn, "clinical_events", "extubation_reason", "TEXT", logger)
    _ensure_column(conn, "clinical_events", "o2_flow", "REAL", logger)
    _ensure_column(conn, "clinical_events", "revision", "INTEGER DEFAULT 0", logger)

    _ensure_column(conn, "devices", "ivl_episode_id", "INTEGER", logger)
    _ensure_column(conn, "devices", "replacement_time", "DATETIME", logger)

    _ensure_column(conn, "respiratory_support", "ivl_episode_id", "INTEGER", logger)
    _ensure_column(conn, "respiratory_support", "event_id", "INTEGER", logger)
    _ensure_column(conn, "respiratory_support", "parameters_json", "TEXT", logger)

    _ensure_column(conn, "vitals", "rr", "INTEGER", logger)
    _ensure_column(conn, "vitals", "gcs", "INTEGER", logger)
    _ensure_column(conn, "vitals", "cvp", "INTEGER", logger)
    _ensure_column(conn, "vitals", "last_modified_by", "TEXT", logger)
    _ensure_column(conn, "vitals", "updated_at", "TEXT", logger)
    _ensure_column(conn, "vitals", "revision", "INTEGER DEFAULT 0", logger)

    _ensure_column(conn, "vital_settings", "rr", "INTEGER DEFAULT 0", logger)
    _ensure_column(conn, "vital_settings", "cvp", "INTEGER DEFAULT 0", logger)
    _ensure_column(conn, "vital_settings", "updated_at", "TEXT", logger)
    _ensure_column(conn, "vital_settings", "last_modified_by", "TEXT", logger)

    _ensure_column(conn, "fluids", "other_output", "REAL DEFAULT 0", logger)
    _ensure_column(conn, "fluids", "last_modified_by", "TEXT", logger)
    _ensure_column(conn, "fluids", "updated_at", "TEXT", logger)
    _ensure_column(conn, "fluids", "revision", "INTEGER DEFAULT 0", logger)

    _ensure_column(conn, "orders", "drug_key", "TEXT", logger)
    _ensure_column(conn, "orders", "latin", "TEXT", logger)
    _ensure_column(conn, "orders", "type", "TEXT", logger)
    _ensure_column(conn, "orders", "dose_value", "REAL", logger)
    _ensure_column(conn, "orders", "dose_unit", "TEXT", logger)
    _ensure_column(conn, "orders", "is_per_kg", "BOOLEAN", logger)
    _ensure_column(conn, "orders", "frequency", "INTEGER", logger)
    _ensure_column(conn, "orders", "specific_times", "TEXT", logger)
    _ensure_column(conn, "orders", "rate_ml_h", "REAL", logger)
    _ensure_column(conn, "orders", "volume_total", "REAL", logger)
    _ensure_column(conn, "orders", "duration_min", "INTEGER", logger)
    _ensure_column(conn, "orders", "sort_order", "INTEGER DEFAULT 0", logger)
    _ensure_column(conn, "orders", "draft_sort_order", "INTEGER", logger)
    _ensure_column(conn, "orders", "is_finalized", "BOOLEAN DEFAULT 0", logger)
    _ensure_column(conn, "orders", "is_committed", "INTEGER DEFAULT 0", logger)
    _ensure_column(conn, "orders", "revision", "INTEGER DEFAULT 0", logger)
    _ensure_column(conn, "orders", "comment", "TEXT", logger)
    _ensure_column(conn, "orders", "last_modified_by", "TEXT", logger)
    _ensure_column(conn, "orders", "updated_at", "TEXT", logger)

    _ensure_column(conn, "administrations", "chain_id", "TEXT", logger)
    _ensure_column(conn, "administrations", "big_chain_id", "TEXT", logger)
    _ensure_column(conn, "administrations", "ui_color", "TEXT", logger)
    _ensure_column(conn, "administrations", "ui_color_until", "DATETIME", logger)
    _ensure_column(conn, "administrations", "version", "INTEGER DEFAULT 0", logger)
    _ensure_column(conn, "administrations", "comment", "TEXT", logger)
    _ensure_column(conn, "administrations", "dose_given", "REAL", logger)
    _ensure_column(conn, "administrations", "volume_ml", "REAL DEFAULT 0", logger)
    _ensure_column(conn, "administrations", "is_committed", "INTEGER DEFAULT 0", logger)
    _ensure_column(conn, "administrations", "last_modified_by", "TEXT", logger)
    _ensure_column(conn, "administrations", "updated_at", "TEXT", logger)

    _ensure_column(conn, "transfusions", "source", "TEXT DEFAULT 'journal'", logger)
    _ensure_column(conn, "transfusions", "source_order_id", "INTEGER", logger)
    _ensure_column(conn, "transfusions", "source_admin_id", "INTEGER", logger)

    _ensure_column(conn, "diet_templates", "diet_text", "TEXT", logger)
    _ensure_column(conn, "diet_templates", "schedule_json", "TEXT NOT NULL DEFAULT '[]'", logger)
    _ensure_column(conn, "diet_templates", "is_default", "INTEGER DEFAULT 0", logger)
    _ensure_column(conn, "diet_templates", "created_at", "TEXT", logger)
    _ensure_column(conn, "diet_templates", "updated_at", "TEXT", logger)
    _ensure_column(conn, "diet_templates", "version", "INTEGER DEFAULT 1", logger)
    _ensure_column(conn, "diet_templates", "last_modified_by", "TEXT", logger)

    _ensure_column(conn, "diet_plan", "shift_start", "TEXT", logger)
    _ensure_column(conn, "diet_plan", "template_id", "INTEGER", logger)
    _ensure_column(conn, "diet_plan", "diet_text", "TEXT", logger)
    _ensure_column(conn, "diet_plan", "schedule_json", "TEXT NOT NULL DEFAULT '[]'", logger)
    _ensure_column(conn, "diet_plan", "created_at", "TEXT", logger)
    _ensure_column(conn, "diet_plan", "updated_at", "TEXT", logger)
    _ensure_column(conn, "diet_plan", "version", "INTEGER DEFAULT 1", logger)
    _ensure_column(conn, "diet_plan", "last_modified_by", "TEXT", logger)

    _ensure_column(conn, "oral_intake_events", "shift_start", "TEXT", logger)
    _ensure_column(conn, "oral_intake_events", "event_time", "TEXT", logger)
    _ensure_column(conn, "oral_intake_events", "amount_ml", "REAL", logger)
    _ensure_column(conn, "oral_intake_events", "created_at", "TEXT", logger)
    _ensure_column(conn, "oral_intake_events", "updated_at", "TEXT", logger)
    _ensure_column(conn, "oral_intake_events", "version", "INTEGER DEFAULT 1", logger)
    _ensure_column(conn, "oral_intake_events", "last_modified_by", "TEXT", logger)

    _ensure_column(conn, "patient_status_events", "last_modified_by", "TEXT", logger)
    _ensure_column(conn, "patient_status_events", "updated_at", "DATETIME", logger)
    _ensure_column(conn, "patient_status_events", "revision", "INTEGER DEFAULT 0", logger)
    _ensure_column(conn, "lab_orders", "patient_id", "INTEGER", logger)
    _ensure_column(conn, "lab_orders", "admission_id", "INTEGER", logger)
    _ensure_column(conn, "lab_orders", "card_day_id", "TEXT", logger)
    _ensure_column(conn, "lab_orders", "analysis_code", "TEXT", logger)
    _ensure_column(conn, "lab_orders", "analysis_name", "TEXT", logger)
    _ensure_column(conn, "lab_orders", "material", "TEXT", logger)
    _ensure_column(conn, "lab_orders", "status", "TEXT DEFAULT 'assigned'", logger)
    _ensure_column(conn, "lab_orders", "created_at", "DATETIME", logger)
    _ensure_column(conn, "lab_orders", "scheduled_at", "DATETIME", logger)
    _ensure_column(conn, "lab_orders", "completed_at", "DATETIME", logger)
    _ensure_column(conn, "lab_orders", "comment", "TEXT", logger)
    _ensure_column(conn, "lab_orders", "created_by_role", "TEXT DEFAULT 'doctor'", logger)
    _ensure_column(conn, "lab_orders", "created_by_user", "TEXT", logger)
    _ensure_column(conn, "lab_orders", "completed_by_role", "TEXT", logger)
    _ensure_column(conn, "lab_orders", "completed_by_user", "TEXT", logger)
    _ensure_column(conn, "lab_orders", "revision", "INTEGER DEFAULT 0", logger)
    _ensure_column(conn, "lab_orders", "created_at_db", "DATETIME", logger)
    _ensure_column(conn, "lab_orders", "updated_at", "DATETIME", logger)
    _ensure_column(conn, "procedure_transfusion", "request_at", "DATETIME", logger)
    _ensure_column(conn, "procedure_transfusion", "indication_code", "TEXT", logger)
    _ensure_column(conn, "procedure_transfusion", "donor_code", "TEXT", logger)
    _ensure_column(conn, "procedure_transfusion", "observation_json", "TEXT", logger)
    _ensure_column(conn, "procedure_transfusion", "revision", "INTEGER DEFAULT 0", logger)

    conn.execute("UPDATE orders SET latin = COALESCE(latin, text) WHERE latin IS NULL")
    conn.execute("UPDATE orders SET type = COALESCE(type, 'medication') WHERE type IS NULL")
    conn.execute("UPDATE orders SET status = CASE WHEN status IS NULL OR status = '' OR status = 'pending' THEN 'active' ELSE status END")
    conn.execute("UPDATE orders SET specific_times = COALESCE(specific_times, '[]') WHERE specific_times IS NULL")
    conn.execute("UPDATE orders SET revision = COALESCE(revision, 0) WHERE revision IS NULL")
    conn.execute("UPDATE admissions SET revision = COALESCE(revision, 0) WHERE revision IS NULL")
    conn.execute("UPDATE beds SET revision = COALESCE(revision, 0) WHERE revision IS NULL")
    conn.execute("UPDATE ivl_episodes SET revision = COALESCE(revision, 0) WHERE revision IS NULL")
    conn.execute("UPDATE clinical_events SET revision = COALESCE(revision, 0) WHERE revision IS NULL")
    conn.execute("UPDATE vitals SET revision = COALESCE(revision, 0) WHERE revision IS NULL")
    conn.execute("UPDATE fluids SET revision = COALESCE(revision, 0) WHERE revision IS NULL")
    conn.execute("UPDATE patient_status_events SET revision = COALESCE(revision, 0) WHERE revision IS NULL")
    conn.execute("UPDATE orders SET updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now') WHERE updated_at IS NULL")
    conn.execute("UPDATE vitals SET updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now') WHERE updated_at IS NULL")
    conn.execute("UPDATE fluids SET updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now') WHERE updated_at IS NULL")
    conn.execute("UPDATE vital_settings SET updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now') WHERE updated_at IS NULL")
    conn.execute("UPDATE administrations SET updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now') WHERE updated_at IS NULL")
    conn.execute("UPDATE transfusions SET source = COALESCE(source, 'journal')")
    conn.execute("UPDATE patient_status_events SET updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now') WHERE updated_at IS NULL")
    conn.execute("UPDATE diet_templates SET updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now') WHERE updated_at IS NULL")
    conn.execute("UPDATE diet_plan SET updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now') WHERE updated_at IS NULL")
    conn.execute("UPDATE oral_intake_events SET updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now') WHERE updated_at IS NULL")
    conn.execute("UPDATE lab_orders SET status = COALESCE(status, 'assigned') WHERE status IS NULL OR status = ''")
    conn.execute("UPDATE lab_orders SET revision = COALESCE(revision, 0) WHERE revision IS NULL")
    conn.execute("UPDATE lab_orders SET created_at_db = STRFTIME('%Y-%m-%d %H:%M:%f', 'now') WHERE created_at_db IS NULL")
    conn.execute("UPDATE lab_orders SET updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now') WHERE updated_at IS NULL")
    conn.execute(
        """
        UPDATE ivl_episodes
        SET start_type = CASE
            WHEN start_type IS NOT NULL THEN start_type
            WHEN LOWER(COALESCE(type, '')) = 'delivery' THEN 'ADMISSION'
            ELSE 'IN_DEPARTMENT'
        END
        """
    )
    conn.execute("UPDATE ivl_episodes SET delivery_type = COALESCE(delivery_type, 'UNKNOWN')")
    conn.execute("UPDATE ivl_episodes SET is_active = CASE WHEN end_time IS NULL THEN 1 ELSE 0 END")

    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_admission_uid ON patients(admission_uid)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_patients_full_name ON patients(full_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_admissions_patient ON admissions(patient_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_admissions_history_number ON admissions(history_number)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_admissions_admission_datetime ON admissions(admission_datetime)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_beds_current_admission ON beds(current_admission_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_operations_admission ON operations(admission_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ivl_admission ON ivl_episodes(admission_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ivl_admission_active_time ON ivl_episodes(admission_id, is_active, start_time)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_transfusions_admission ON transfusions(admission_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_transfusions_source_admin ON transfusions(source, source_admin_id) WHERE source_admin_id IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_clinical_events_admission_type_time ON clinical_events(admission_id, event_type, timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_clinical_events_ivl_case_time ON clinical_events(ivl_episode_id, timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_devices_ivl_case_time ON devices(ivl_episode_id, insertion_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_resp_support_ivl_case_time ON respiratory_support(ivl_episode_id, datetime)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_resp_support_event ON respiratory_support(event_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vitals_admission ON vitals(admission_id, datetime)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fluids_admission ON fluids(admission_id, datetime)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_admission ON orders(admission_id, datetime)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vitals_updated_at ON vitals(updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fluids_updated_at ON fluids(updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_updated_at ON orders(updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_administrations_updated_at ON administrations(updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vitals_admission_updated_id ON vitals(admission_id, updated_at, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fluids_admission_updated_id ON fluids(admission_id, updated_at, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_admission_updated_id ON orders(admission_id, updated_at, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status_admission_updated_id ON patient_status_events(admission_id, updated_at, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_big_chain ON administrations(big_chain_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_order_planned_id ON administrations(order_id, planned_time, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_committed_order_planned_id ON administrations(is_committed, order_id, planned_time, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_planned_status_id ON administrations(planned_time, status, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_admission_status_committed_time ON orders(admission_id, status, is_committed, datetime)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status_admission_time ON patient_status_events(admission_id, start_time)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status_admission_end ON patient_status_events(admission_id, end_time)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status_updated_at ON patient_status_events(updated_at)")
    conn.execute("DROP INDEX IF EXISTS idx_active_status")
    conn.execute("CREATE INDEX idx_active_status ON patient_status_events(admission_id) WHERE end_time IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_diet_templates_updated_at ON diet_templates(updated_at)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_diet_plan_admission_shift ON diet_plan(admission_id, shift_start)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_diet_plan_updated_at ON diet_plan(updated_at)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_oral_intake_admission_event_time ON oral_intake_events(admission_id, event_time)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_oral_intake_admission_shift ON oral_intake_events(admission_id, shift_start)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_oral_intake_updated_at ON oral_intake_events(updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_change_log_admission_id ON change_log(admission_id, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_change_log_entity ON change_log(entity_name, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_change_log_changed_at ON change_log(changed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_medical_audit_admission_changed ON medical_audit_log(admission_id, changed_at, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_medical_audit_table_row ON medical_audit_log(table_name, row_id, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_medical_audit_operation ON medical_audit_log(operation_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_applied_ops_applied_at ON sync_applied_ops(applied_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_schema_migrations_applied_at ON schema_migrations(applied_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_procedures_admission_type_time ON procedures(admission_id, procedure_type, started_at, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lab_orders_admission_card_day ON lab_orders(admission_id, card_day_id, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lab_orders_admission_scheduled ON lab_orders(admission_id, scheduled_at, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lab_orders_updated_at ON lab_orders(updated_at, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_procedure_consents_procedure ON procedure_consents(procedure_id, consent_kind)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_procedure_cvc_catheter_status ON procedure_cvc(catheter_status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_procedure_transfusion_indication ON procedure_transfusion(indication_code)")

    _mark_schema_migration(conn, 1, "unified schema baseline")
    _mark_schema_migration(conn, 2, "admissions.intake_extra_json for extensible intake fields")
    _mark_schema_migration(conn, 3, "ventilation normalized metadata for episodes/events/devices")
    _mark_schema_migration(conn, 4, "diet templates, diet plans and oral intake events")
    _mark_schema_migration(conn, 5, "remcard blood/plasma administrations mirrored into transfusions")
    _mark_schema_migration(conn, 6, "structured outcome details for transfers and death records")
    _mark_schema_migration(conn, 7, "shared-db safety fastpath contract")
    _mark_schema_migration(conn, 8, "orders optimistic lock revision")
    _mark_schema_migration(conn, 9, "medical audit log foundation")
    _mark_schema_migration(conn, 10, "optimistic lock revisions for clinical domains")
    _mark_schema_migration(conn, 11, "patients.birth_date for calculated age")
    _mark_schema_migration(conn, 12, "patient medical procedures prototype")
    _mark_schema_migration(conn, 13, "lumbar puncture procedure")
    _mark_schema_migration(conn, 14, "transfusion procedure")
    _mark_schema_migration(conn, 15, "schema contract satisfied")
    _mark_schema_migration(conn, 16, "admissions emergency notice fields")

    for table in (
        "vitals",
        "vital_settings",
        "fluids",
        "orders",
        "administrations",
        "patient_status_events",
        "diet_templates",
        "diet_plan",
        "oral_intake_events",
        "procedures",
        "lab_orders",
    ):
        _create_updated_at_trigger(conn, table)

    _create_change_triggers(
        conn,
        "vitals",
        "NEW.id",
        "OLD.id",
        "NEW.admission_id",
        "OLD.admission_id",
        "COALESCE(NEW.last_modified_by, 'system')",
        "COALESCE(OLD.last_modified_by, 'system')",
        use_updated_at_gate=False,
    )
    _create_change_triggers(
        conn,
        "vital_settings",
        "NEW.rowid",
        "OLD.rowid",
        "NEW.admission_id",
        "OLD.admission_id",
        "COALESCE(NEW.last_modified_by, 'system')",
        "COALESCE(OLD.last_modified_by, 'system')",
        use_updated_at_gate=False,
    )
    _create_change_triggers(
        conn,
        "fluids",
        "NEW.id",
        "OLD.id",
        "NEW.admission_id",
        "OLD.admission_id",
        "COALESCE(NEW.last_modified_by, 'system')",
        "COALESCE(OLD.last_modified_by, 'system')",
        use_updated_at_gate=False,
    )
    _create_change_triggers(
        conn,
        "orders",
        "NEW.id",
        "OLD.id",
        "NEW.admission_id",
        "OLD.admission_id",
        "COALESCE(NEW.last_modified_by, 'doctor')",
        "COALESCE(OLD.last_modified_by, 'doctor')",
        use_updated_at_gate=False,
    )
    _create_change_triggers(
        conn,
        "administrations",
        "NEW.id",
        "OLD.id",
        "(SELECT admission_id FROM orders WHERE id = NEW.order_id)",
        "(SELECT admission_id FROM orders WHERE id = OLD.order_id)",
        "COALESCE(NEW.last_modified_by, 'system')",
        "COALESCE(OLD.last_modified_by, 'system')",
        use_updated_at_gate=False,
    )
    _create_change_triggers(
        conn,
        "patient_status_events",
        "NEW.id",
        "OLD.id",
        "NEW.admission_id",
        "OLD.admission_id",
        "COALESCE(NEW.last_modified_by, NEW.created_by, 'system')",
        "COALESCE(OLD.last_modified_by, OLD.created_by, 'system')",
        use_updated_at_gate=False,
    )

    _create_change_triggers(
        conn,
        "diet_templates",
        "NEW.id",
        "OLD.id",
        "NULL",
        "NULL",
        "COALESCE(NEW.last_modified_by, 'doctor')",
        "COALESCE(OLD.last_modified_by, 'doctor')",
        use_updated_at_gate=False,
    )
    _create_change_triggers(
        conn,
        "diet_plan",
        "NEW.id",
        "OLD.id",
        "NEW.admission_id",
        "OLD.admission_id",
        "COALESCE(NEW.last_modified_by, 'doctor')",
        "COALESCE(OLD.last_modified_by, 'doctor')",
        use_updated_at_gate=False,
    )
    _create_change_triggers(
        conn,
        "oral_intake_events",
        "NEW.id",
        "OLD.id",
        "NEW.admission_id",
        "OLD.admission_id",
        "COALESCE(NEW.last_modified_by, 'nurse')",
        "COALESCE(OLD.last_modified_by, 'nurse')",
        use_updated_at_gate=False,
    )
    _create_change_triggers(
        conn,
        "procedures",
        "NEW.id",
        "OLD.id",
        "NEW.admission_id",
        "OLD.admission_id",
        "COALESCE(NEW.updated_by, NEW.created_by, 'doctor')",
        "COALESCE(OLD.updated_by, OLD.created_by, 'doctor')",
        use_updated_at_gate=False,
    )
    _create_change_triggers(
        conn,
        "lab_orders",
        "NEW.id",
        "OLD.id",
        "NEW.admission_id",
        "OLD.admission_id",
        "COALESCE(NEW.completed_by_role, NEW.created_by_role, 'doctor')",
        "COALESCE(OLD.completed_by_role, OLD.created_by_role, 'doctor')",
        use_updated_at_gate=False,
    )
    _create_change_triggers(
        conn,
        "procedure_consents",
        "NEW.id",
        "OLD.id",
        "(SELECT admission_id FROM procedures WHERE id = NEW.procedure_id)",
        "(SELECT admission_id FROM procedures WHERE id = OLD.procedure_id)",
        "COALESCE(NEW.doctor_name_snapshot, 'doctor')",
        "COALESCE(OLD.doctor_name_snapshot, 'doctor')",
        use_updated_at_gate=False,
    )
    _create_change_triggers(
        conn,
        "procedure_cvc",
        "NEW.procedure_id",
        "OLD.procedure_id",
        "(SELECT admission_id FROM procedures WHERE id = NEW.procedure_id)",
        "(SELECT admission_id FROM procedures WHERE id = OLD.procedure_id)",
        "COALESCE(NEW.operator_doctor_name, NEW.removal_doctor_name, 'doctor')",
        "COALESCE(OLD.operator_doctor_name, OLD.removal_doctor_name, 'doctor')",
        use_updated_at_gate=False,
    )
    _create_change_triggers(
        conn,
        "procedure_lumbar_puncture",
        "NEW.procedure_id",
        "OLD.procedure_id",
        "(SELECT admission_id FROM procedures WHERE id = NEW.procedure_id)",
        "(SELECT admission_id FROM procedures WHERE id = OLD.procedure_id)",
        "COALESCE(NEW.operator_doctor_name, 'doctor')",
        "COALESCE(OLD.operator_doctor_name, 'doctor')",
        use_updated_at_gate=False,
    )
    _create_change_triggers(
        conn,
        "procedure_transfusion",
        "NEW.procedure_id",
        "OLD.procedure_id",
        "(SELECT admission_id FROM procedures WHERE id = NEW.procedure_id)",
        "(SELECT admission_id FROM procedures WHERE id = OLD.procedure_id)",
        "COALESCE(NEW.operator_doctor_name, 'doctor')",
        "COALESCE(OLD.operator_doctor_name, 'doctor')",
        use_updated_at_gate=False,
    )

    _create_change_triggers(
        conn,
        "patients",
        "NEW.id",
        "OLD.id",
        "NULL",
        "NULL",
        "'journal'",
        "'journal'",
    )
    _create_change_triggers(
        conn,
        "admissions",
        "NEW.id",
        "OLD.id",
        "NEW.id",
        "OLD.id",
        "'journal'",
        "'journal'",
    )
    _create_change_triggers(
        conn,
        "beds",
        "NEW.bed_number",
        "OLD.bed_number",
        "NEW.current_admission_id",
        "OLD.current_admission_id",
        "'journal'",
        "'journal'",
    )
    _create_change_triggers(
        conn,
        "operations",
        "NEW.id",
        "OLD.id",
        "NEW.admission_id",
        "OLD.admission_id",
        "'journal'",
        "'journal'",
    )
    _create_change_triggers(
        conn,
        "ivl_episodes",
        "NEW.id",
        "OLD.id",
        "NEW.admission_id",
        "OLD.admission_id",
        "'journal'",
        "'journal'",
    )
    _create_change_triggers(
        conn,
        "transfusions",
        "NEW.id",
        "OLD.id",
        "NEW.admission_id",
        "OLD.admission_id",
        "'journal'",
        "'journal'",
    )
    _create_change_triggers(
        conn,
        "clinical_events",
        "NEW.id",
        "OLD.id",
        "NEW.admission_id",
        "OLD.admission_id",
        "'journal'",
        "'journal'",
    )
    _create_change_triggers(
        conn,
        "devices",
        "NEW.id",
        "OLD.id",
        "NEW.admission_id",
        "OLD.admission_id",
        "'journal'",
        "'journal'",
    )
    _create_change_triggers(
        conn,
        "respiratory_support",
        "NEW.id",
        "OLD.id",
        "NEW.admission_id",
        "OLD.admission_id",
        "'journal'",
        "'journal'",
    )

    _create_medical_audit_triggers(
        conn,
        "orders",
        "NEW.id",
        "OLD.id",
        "NEW.admission_id",
        "OLD.admission_id",
        "COALESCE(NEW.last_modified_by, 'doctor')",
        "COALESCE(OLD.last_modified_by, 'doctor')",
        ("id", "admission_id", "latin", "drug_key", "status", "is_committed", "revision", "updated_at", "last_modified_by"),
        use_updated_at_gate=True,
    )
    _create_medical_audit_triggers(
        conn,
        "administrations",
        "NEW.id",
        "OLD.id",
        "(SELECT admission_id FROM orders WHERE id = NEW.order_id)",
        "(SELECT admission_id FROM orders WHERE id = OLD.order_id)",
        "COALESCE(NEW.last_modified_by, 'nurse')",
        "COALESCE(OLD.last_modified_by, 'nurse')",
        ("id", "order_id", "planned_time", "cell_role", "status", "is_committed", "version", "comment", "updated_at", "last_modified_by"),
        use_updated_at_gate=True,
    )
    _create_medical_audit_triggers(
        conn,
        "vitals",
        "NEW.id",
        "OLD.id",
        "NEW.admission_id",
        "OLD.admission_id",
        "COALESCE(NEW.last_modified_by, 'doctor')",
        "COALESCE(OLD.last_modified_by, 'doctor')",
        ("id", "admission_id", "datetime", "sys", "dia", "pulse", "temp", "spo2", "rr", "cvp", "revision", "updated_at", "last_modified_by"),
        use_updated_at_gate=True,
    )
    _create_medical_audit_triggers(
        conn,
        "fluids",
        "NEW.id",
        "OLD.id",
        "NEW.admission_id",
        "OLD.admission_id",
        "COALESCE(NEW.last_modified_by, 'doctor')",
        "COALESCE(OLD.last_modified_by, 'doctor')",
        ("id", "admission_id", "datetime", "iv_input", "oral_input", "food", "urine", "ng_output", "drain_output", "stool", "other_output", "revision", "updated_at", "last_modified_by"),
        use_updated_at_gate=True,
    )
    _create_medical_audit_triggers(
        conn,
        "admissions",
        "NEW.id",
        "OLD.id",
        "NEW.id",
        "OLD.id",
        "'journal'",
        "'journal'",
        ("id", "patient_id", "bed_number", "history_number", "admission_datetime", "outcome", "transfer_datetime", "death_datetime", "emergency_notice_number", "emergency_notice_entered_at", "revision", "updated_at"),
        use_updated_at_gate=True,
    )
    _create_medical_audit_triggers(
        conn,
        "beds",
        "NEW.bed_number",
        "OLD.bed_number",
        "NEW.current_admission_id",
        "OLD.current_admission_id",
        "'journal'",
        "'journal'",
        ("bed_number", "status", "current_admission_id", "revision"),
    )
    _create_medical_audit_triggers(
        conn,
        "patient_status_events",
        "NEW.id",
        "OLD.id",
        "NEW.admission_id",
        "OLD.admission_id",
        "COALESCE(NEW.last_modified_by, NEW.created_by, 'doctor')",
        "COALESCE(OLD.last_modified_by, OLD.created_by, 'doctor')",
        ("id", "admission_id", "status", "reason_text", "start_time", "end_time", "created_by", "last_modified_by", "revision", "updated_at"),
        use_updated_at_gate=True,
    )
    _create_medical_audit_triggers(
        conn,
        "ivl_episodes",
        "NEW.id",
        "OLD.id",
        "NEW.admission_id",
        "OLD.admission_id",
        "'doctor'",
        "'doctor'",
        ("id", "admission_id", "episode_number", "start_time", "end_time", "type", "start_type", "delivery_type", "is_active", "revision"),
    )
    _create_medical_audit_triggers(
        conn,
        "clinical_events",
        "NEW.id",
        "OLD.id",
        "NEW.admission_id",
        "OLD.admission_id",
        "COALESCE(NEW.author, 'doctor')",
        "COALESCE(OLD.author, 'doctor')",
        ("id", "admission_id", "timestamp", "event_type", "author", "mode", "parameters_json", "extubation_reason", "o2_flow", "revision"),
    )
    _create_medical_audit_triggers(
        conn,
        "diet_plan",
        "NEW.id",
        "OLD.id",
        "NEW.admission_id",
        "OLD.admission_id",
        "COALESCE(NEW.last_modified_by, 'doctor')",
        "COALESCE(OLD.last_modified_by, 'doctor')",
        ("id", "admission_id", "shift_start", "template_id", "diet_text", "version", "updated_at", "last_modified_by"),
        use_updated_at_gate=True,
    )
    _create_medical_audit_triggers(
        conn,
        "oral_intake_events",
        "NEW.id",
        "OLD.id",
        "NEW.admission_id",
        "OLD.admission_id",
        "COALESCE(NEW.last_modified_by, 'nurse')",
        "COALESCE(OLD.last_modified_by, 'nurse')",
        ("id", "admission_id", "shift_start", "event_time", "amount_ml", "version", "updated_at", "last_modified_by"),
        use_updated_at_gate=True,
    )
    _create_medical_audit_triggers(
        conn,
        "lab_orders",
        "NEW.id",
        "OLD.id",
        "NEW.admission_id",
        "OLD.admission_id",
        "COALESCE(NEW.completed_by_role, NEW.created_by_role, 'doctor')",
        "COALESCE(OLD.completed_by_role, OLD.created_by_role, 'doctor')",
        (
            "id",
            "patient_id",
            "admission_id",
            "card_day_id",
            "analysis_code",
            "analysis_name",
            "material",
            "status",
            "created_at",
            "scheduled_at",
            "completed_at",
            "comment",
            "revision",
            "updated_at",
        ),
        use_updated_at_gate=True,
    )
    _mark_schema_migration(conn, SCHEMA_MIN_MIGRATION_VERSION, "lab orders worklist")
    _set_meta_int_value(conn, SCHEMA_FASTPATH_META_KEY, SCHEMA_FASTPATH_REV)
