from __future__ import annotations

import sqlite3
from datetime import datetime


SCHEMA_VERSION = 1
SCHEMA_VERSION_KEY = "schema_version"
SEED_IMPORT_VERSION = "central_settings_v1"

REQUIRED_TABLES = (
    "settings_meta",
    "settings_change_log",
    "settings_catalog_versions",
    "settings_audit_log",
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
    "app_settings",
)


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
        """
    for statement in schema_sql.split(";"):
        sql = statement.strip()
        if sql:
            conn.execute(sql)
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
