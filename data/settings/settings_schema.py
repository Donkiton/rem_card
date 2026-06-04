from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime


SCHEMA_VERSION = 3
SCHEMA_VERSION_KEY = "schema_version"
SEED_IMPORT_VERSION = "central_settings_v1"

TECHNICAL_TABLES = (
    "settings_meta",
    "settings_change_log",
    "settings_catalog_versions",
    "settings_audit_log",
)

CATALOG_TABLES = (
    "drug_groups",
    "dosage_forms",
    "administration_routes",
    "solvents",
    "drugs",
    "order_templates",
    "doctors",
    "diet_templates",
    "lab_analysis_templates",
    "ui_backgrounds",
    "print_templates",
    "operblock_icons",
    "app_settings",
)

REQUIRED_INDEXES = (
    "idx_drugs_enabled_sort",
    "idx_drugs_group_enabled",
    "idx_order_templates_scope_enabled_sort",
    "idx_lab_analysis_enabled_frequent_sort",
    "idx_lab_analysis_category_enabled",
    "idx_doctors_enabled_sort",
    "idx_diet_templates_enabled_sort",
    "idx_app_settings_scope_key",
    "idx_settings_change_log_id",
    "idx_settings_change_log_entity_changed_at",
    "idx_operblock_icons_category_target",
)

REQUIRED_UNIQUE_CONSTRAINTS = (
    ("settings_meta", ("key",)),
    ("settings_catalog_versions", ("catalog_key",)),
    ("drug_groups", ("code",)),
    ("dosage_forms", ("code",)),
    ("administration_routes", ("code",)),
    ("solvents", ("code",)),
    ("drugs", ("code",)),
    ("order_templates", ("template_key",)),
    ("doctors", ("code",)),
    ("diet_templates", ("template_key",)),
    ("lab_analysis_templates", ("analysis_code",)),
    ("ui_backgrounds", ("background_key",)),
    ("print_templates", ("template_key",)),
    ("operblock_icons", ("icon_key",)),
    ("app_settings", ("scope", "key")),
)

REQUIRED_CATALOG_KEYS = (
    "drug_catalog",
    "order_templates",
    "lab_analysis",
    "diet_templates",
    "doctors",
    "print_settings",
    "display_settings",
    "background_settings",
    "operblock_icons",
    "style_settings",
)

MIGRATION_MARKER_KEYS = (
    "settings_schema_migration_pending",
    "settings_schema_migration_failed",
)

REQUIRED_TABLES = (
    *TECHNICAL_TABLES,
    *CATALOG_TABLES,
)


@dataclass(frozen=True)
class SettingsSchemaStatus:
    fastpath_ready: bool
    reason: str
    schema_version: int = 0
    missing_tables: tuple[str, ...] = ()
    missing_indexes: tuple[str, ...] = ()
    missing_unique_constraints: tuple[str, ...] = ()
    missing_catalog_keys: tuple[str, ...] = ()
    active_migration_markers: tuple[str, ...] = ()


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO settings_meta (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, str(value), now_text()),
    )


def get_schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT value FROM settings_meta WHERE key = ?", (SCHEMA_VERSION_KEY,)).fetchone()
    except sqlite3.OperationalError:
        return 0
    if not row:
        return 0
    try:
        return int(row[0])
    except Exception:
        return 0


def _quote_identifier(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise ValueError(f"unsafe SQLite identifier: {name!r}")
    return f'"{name}"'


def _row_value(row, key: str, index: int):
    try:
        return row[key]
    except Exception:
        return row[index]


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    table_sql = _quote_identifier(table)
    return {
        str(_row_value(row, "name", 1) or "")
        for row in conn.execute(f"PRAGMA table_info({table_sql})").fetchall()
    }


def _index_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
    }


def _unique_constraint_exists(conn: sqlite3.Connection, table: str, columns: tuple[str, ...]) -> bool:
    table_sql = _quote_identifier(table)
    for index_row in conn.execute(f"PRAGMA index_list({table_sql})").fetchall():
        try:
            unique = int(_row_value(index_row, "unique", 2) or 0)
            index_name = str(_row_value(index_row, "name", 1) or "")
        except Exception:
            continue
        if not unique or not index_name:
            continue
        index_sql = _quote_identifier(index_name)
        index_columns = tuple(
            str(_row_value(info_row, "name", 2) or "")
            for info_row in conn.execute(f"PRAGMA index_info({index_sql})").fetchall()
        )
        if index_columns == columns:
            return True
    return False


def _active_marker_value(value: object) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized not in {"", "0", "false", "ok", "done", "complete", "completed"}


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition_sql: str) -> None:
    if column in _table_columns(conn, table):
        return
    table_sql = _quote_identifier(table)
    column_sql = _quote_identifier(column)
    conn.execute(f"ALTER TABLE {table_sql} ADD COLUMN {column_sql} {definition_sql}")


def inspect_schema_status(conn: sqlite3.Connection) -> SettingsSchemaStatus:
    tables = _table_names(conn)
    missing_tables = tuple(table for table in REQUIRED_TABLES if table not in tables)
    if missing_tables:
        return SettingsSchemaStatus(False, "missing_tables", missing_tables=missing_tables)

    row = conn.execute(
        "SELECT value FROM settings_meta WHERE key = ?",
        (SCHEMA_VERSION_KEY,),
    ).fetchone()
    if not row:
        return SettingsSchemaStatus(False, "missing_schema_version")
    raw_version = row[0]
    try:
        schema_version = int(raw_version)
    except Exception:
        return SettingsSchemaStatus(False, "invalid_schema_version")
    if schema_version != SCHEMA_VERSION:
        reason = "schema_version_newer" if schema_version > SCHEMA_VERSION else "schema_version_outdated"
        return SettingsSchemaStatus(False, reason, schema_version=schema_version)

    indexes = _index_names(conn)
    missing_indexes = tuple(index for index in REQUIRED_INDEXES if index not in indexes)
    if missing_indexes:
        return SettingsSchemaStatus(
            False,
            "missing_indexes",
            schema_version=schema_version,
            missing_indexes=missing_indexes,
        )

    missing_unique_constraints = tuple(
        f"{table}({','.join(columns)})"
        for table, columns in REQUIRED_UNIQUE_CONSTRAINTS
        if not _unique_constraint_exists(conn, table, columns)
    )
    if missing_unique_constraints:
        return SettingsSchemaStatus(
            False,
            "missing_unique_constraints",
            schema_version=schema_version,
            missing_unique_constraints=missing_unique_constraints,
        )

    placeholders = ",".join("?" for _ in REQUIRED_CATALOG_KEYS)
    catalog_rows = conn.execute(
        f"SELECT catalog_key FROM settings_catalog_versions WHERE catalog_key IN ({placeholders})",
        tuple(REQUIRED_CATALOG_KEYS),
    ).fetchall()
    present_catalog_keys = {str(row[0]) for row in catalog_rows}
    missing_catalog_keys = tuple(
        key for key in REQUIRED_CATALOG_KEYS if key not in present_catalog_keys
    )
    if missing_catalog_keys:
        return SettingsSchemaStatus(
            False,
            "missing_catalog_versions",
            schema_version=schema_version,
            missing_catalog_keys=missing_catalog_keys,
        )

    marker_placeholders = ",".join("?" for _ in MIGRATION_MARKER_KEYS)
    marker_rows = conn.execute(
        f"SELECT key, value FROM settings_meta WHERE key IN ({marker_placeholders})",
        tuple(MIGRATION_MARKER_KEYS),
    ).fetchall()
    active_markers = tuple(
        str(row[0])
        for row in marker_rows
        if _active_marker_value(row[1])
    )
    if active_markers:
        return SettingsSchemaStatus(
            False,
            "active_migration_marker",
            schema_version=schema_version,
            active_migration_markers=active_markers,
        )

    return SettingsSchemaStatus(True, "current", schema_version=schema_version)


def apply_schema(conn: sqlite3.Connection) -> None:
    schema_sql = """
        CREATE TABLE IF NOT EXISTS settings_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings_change_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id TEXT,
            operation TEXT NOT NULL,
            scope TEXT,
            version INTEGER NOT NULL,
            changed_at TEXT NOT NULL,
            changed_by_role TEXT,
            changed_by_user TEXT,
            source_client_id TEXT,
            content_hash TEXT
        );

        CREATE TABLE IF NOT EXISTS settings_catalog_versions (
            catalog_key TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by_role TEXT,
            updated_by_user TEXT
        );

        CREATE TABLE IF NOT EXISTS settings_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id TEXT,
            operation TEXT NOT NULL,
            before_json TEXT,
            after_json TEXT,
            changed_at TEXT NOT NULL,
            changed_by_role TEXT,
            changed_by_user TEXT,
            source_client_id TEXT
        );

        CREATE TABLE IF NOT EXISTS drug_groups (
            id INTEGER PRIMARY KEY,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            display_name TEXT,
            sort_order INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1,
            color TEXT,
            icon TEXT,
            revision INTEGER DEFAULT 1,
            payload_json TEXT,
            source TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS dosage_forms (
            id INTEGER PRIMARY KEY,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            display_name TEXT,
            enabled INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0,
            revision INTEGER DEFAULT 1,
            payload_json TEXT,
            source TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS administration_routes (
            id INTEGER PRIMARY KEY,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            display_name TEXT,
            enabled INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0,
            revision INTEGER DEFAULT 1,
            payload_json TEXT,
            source TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS solvents (
            id INTEGER PRIMARY KEY,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            display_name TEXT,
            default_volume_ml REAL,
            unit TEXT DEFAULT 'ml',
            enabled INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0,
            revision INTEGER DEFAULT 1,
            payload_json TEXT,
            source TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS drugs (
            id INTEGER PRIMARY KEY,
            code TEXT UNIQUE NOT NULL,
            latin TEXT,
            name TEXT NOT NULL,
            display_name TEXT,
            aliases_json TEXT,
            group_code TEXT,
            form_code TEXT,
            route_code TEXT,
            unit TEXT,
            default_dose TEXT,
            default_dilution_json TEXT,
            admin_type TEXT,
            duration_min INTEGER,
            uses_line INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0,
            source TEXT,
            revision INTEGER DEFAULT 1,
            payload_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS order_templates (
            id INTEGER PRIMARY KEY,
            template_key TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            display_name TEXT,
            scope TEXT NOT NULL,
            drug_code TEXT,
            group_code TEXT,
            route_code TEXT,
            form_code TEXT,
            solvent_code TEXT,
            template_type TEXT NOT NULL,
            text_template TEXT,
            params_json TEXT,
            enabled INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0,
            revision INTEGER DEFAULT 1,
            source TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS doctors (
            id INTEGER PRIMARY KEY,
            code TEXT UNIQUE,
            full_name TEXT NOT NULL,
            short_name TEXT,
            position TEXT,
            department TEXT,
            signature_text TEXT,
            enabled INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0,
            revision INTEGER DEFAULT 1,
            payload_json TEXT,
            source TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS diet_templates (
            id INTEGER PRIMARY KEY,
            template_key TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            display_name TEXT,
            description TEXT,
            payload_json TEXT,
            enabled INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0,
            revision INTEGER DEFAULT 1,
            source TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS lab_analysis_templates (
            id INTEGER PRIMARY KEY,
            analysis_code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            display_name TEXT,
            category TEXT,
            material TEXT,
            aliases_json TEXT,
            default_priority TEXT,
            fasting_required INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1,
            frequent INTEGER DEFAULT 0,
            sort_order INTEGER DEFAULT 0,
            payload_json TEXT,
            revision INTEGER DEFAULT 1,
            source TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ui_backgrounds (
            id INTEGER PRIMARY KEY,
            background_key TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            scope TEXT NOT NULL,
            kind TEXT NOT NULL,
            value_json TEXT,
            image_blob BLOB,
            image_mime TEXT,
            image_hash TEXT,
            enabled INTEGER DEFAULT 1,
            active INTEGER DEFAULT 0,
            revision INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS print_templates (
            id INTEGER PRIMARY KEY,
            template_key TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            scope TEXT NOT NULL,
            template_type TEXT NOT NULL,
            html_template TEXT,
            css_template TEXT,
            params_json TEXT,
            header_json TEXT,
            footer_json TEXT,
            logo_blob BLOB,
            logo_mime TEXT,
            enabled INTEGER DEFAULT 1,
            active INTEGER DEFAULT 0,
            revision INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS operblock_icons (
            id INTEGER PRIMARY KEY,
            icon_key TEXT UNIQUE NOT NULL,
            category TEXT NOT NULL,
            target_key TEXT NOT NULL,
            name TEXT NOT NULL,
            default_file TEXT NOT NULL,
            value_json TEXT,
            image_blob BLOB,
            image_mime TEXT,
            image_hash TEXT,
            enabled INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0,
            revision INTEGER DEFAULT 1,
            source TEXT DEFAULT 'seed',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS app_settings (
            id INTEGER PRIMARY KEY,
            scope TEXT NOT NULL,
            key TEXT NOT NULL,
            value_json TEXT NOT NULL,
            revision INTEGER DEFAULT 1,
            updated_at TEXT NOT NULL,
            updated_by_role TEXT,
            updated_by_user TEXT,
            UNIQUE(scope, key)
        );

        CREATE INDEX IF NOT EXISTS idx_drugs_enabled_sort ON drugs(enabled, sort_order);
        CREATE INDEX IF NOT EXISTS idx_drugs_group_enabled ON drugs(group_code, enabled);
        CREATE INDEX IF NOT EXISTS idx_order_templates_scope_enabled_sort ON order_templates(scope, enabled, sort_order);
        CREATE INDEX IF NOT EXISTS idx_lab_analysis_enabled_frequent_sort ON lab_analysis_templates(enabled, frequent, sort_order);
        CREATE INDEX IF NOT EXISTS idx_lab_analysis_category_enabled ON lab_analysis_templates(category, enabled);
        CREATE INDEX IF NOT EXISTS idx_doctors_enabled_sort ON doctors(enabled, sort_order);
        CREATE INDEX IF NOT EXISTS idx_diet_templates_enabled_sort ON diet_templates(enabled, sort_order);
        CREATE INDEX IF NOT EXISTS idx_app_settings_scope_key ON app_settings(scope, key);
        CREATE INDEX IF NOT EXISTS idx_settings_change_log_id ON settings_change_log(id);
        CREATE INDEX IF NOT EXISTS idx_settings_change_log_entity_changed_at ON settings_change_log(entity_type, changed_at);
        CREATE INDEX IF NOT EXISTS idx_operblock_icons_category_target ON operblock_icons(category, target_key);
        """
    for statement in schema_sql.split(";"):
        sql = statement.strip()
        if sql:
            conn.execute(sql)
    _ensure_column(conn, "operblock_icons", "source", "TEXT DEFAULT 'seed'")
    now = now_text()
    conn.execute(
        """
        INSERT OR IGNORE INTO settings_meta (key, value, updated_at)
        VALUES ('settings_db_created_at', ?, ?)
        """,
        (now, now),
    )
    _set_meta(conn, SCHEMA_VERSION_KEY, str(SCHEMA_VERSION))
    _set_meta(conn, "last_migration_at", now)
    for catalog_key in REQUIRED_CATALOG_KEYS:
        conn.execute(
            """
            INSERT OR IGNORE INTO settings_catalog_versions (
                catalog_key, version, content_hash, updated_at, updated_by_role, updated_by_user
            )
            VALUES (?, 0, '', ?, 'system', NULL)
            """,
            (catalog_key, now),
        )
