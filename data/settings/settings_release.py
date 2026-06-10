from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from rem_card.app.runtime_paths import get_resources_dir
from rem_card.data.settings.settings_db import SettingsDatabase
from rem_card.data.settings import settings_schema
from rem_card.data.settings.settings_schema import now_text


SNAPSHOT_SCHEMA_VERSION = 1
SETTINGS_RELEASE_DIR = "settings_release"
SETTINGS_RELEASE_SNAPSHOT_FILE = "settings_release_snapshot.json"
SETTINGS_RELEASE_APPLIED_HASH_KEY = "settings_release_snapshot_applied_hash"
SETTINGS_RELEASE_APPLIED_AT_KEY = "settings_release_snapshot_applied_at"
SETTINGS_RELEASE_VERSION_KEY = "settings_release_snapshot_version"


@dataclass(frozen=True)
class ReleaseTable:
    name: str
    key_columns: tuple[str, ...]
    order_by: str


RELEASE_TABLES: tuple[ReleaseTable, ...] = (
    ReleaseTable("drug_groups", ("code",), "sort_order ASC, code ASC"),
    ReleaseTable("dosage_forms", ("code",), "sort_order ASC, code ASC"),
    ReleaseTable("administration_routes", ("code",), "sort_order ASC, code ASC"),
    ReleaseTable("solvents", ("code",), "sort_order ASC, code ASC"),
    ReleaseTable("drugs", ("code",), "sort_order ASC, code ASC"),
    ReleaseTable("order_templates", ("template_key",), "sort_order ASC, template_key ASC"),
    ReleaseTable("doctors", ("code",), "sort_order ASC, code ASC, full_name ASC"),
    ReleaseTable("diet_templates", ("template_key",), "sort_order ASC, template_key ASC"),
    ReleaseTable("lab_analysis_templates", ("analysis_code",), "sort_order ASC, analysis_code ASC"),
    ReleaseTable("ui_backgrounds", ("background_key",), "active DESC, background_key ASC"),
    ReleaseTable("print_templates", ("template_key",), "active DESC, template_key ASC"),
    ReleaseTable("operblock_icons", ("icon_key",), "sort_order ASC, icon_key ASC"),
    ReleaseTable("app_settings", ("scope", "key"), "scope ASC, key ASC"),
)


TABLE_CATALOG_KEYS: dict[str, str] = {
    "drug_groups": "drug_catalog",
    "dosage_forms": "drug_catalog",
    "administration_routes": "drug_catalog",
    "solvents": "drug_catalog",
    "drugs": "drug_catalog",
    "order_templates": "order_templates",
    "doctors": "doctors",
    "diet_templates": "diet_templates",
    "lab_analysis_templates": "lab_analysis",
    "ui_backgrounds": "background_settings",
    "print_templates": "print_settings",
    "operblock_icons": "operblock_icons",
}


APP_SETTING_CATALOG_KEYS: dict[str, str] = {
    "lab_materials": "lab_analysis",
    "print_config": "print_settings",
    "display_settings": "display_settings",
    "lab_orders_columns": "display_settings",
    "style_settings": "style_settings",
    "background_settings": "background_settings",
    "operblock:group_routes": "operblock_settings",
    "operblock:team": "operblock_settings",
    "operblock:anesthesia_types": "operblock_settings",
    "operblock:quick_order_buttons": "operblock_settings",
    "operblock:quick_orders": "operblock_settings",
    "operblock:medication_presets": "operblock_settings",
}


TECHNICAL_APP_SETTINGS = {"settings_import_report"}


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _content_hash(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _encode_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"__blob_base64__": base64.b64encode(value).decode("ascii")}
    return value


def _decode_value(value: Any) -> Any:
    if isinstance(value, dict) and set(value.keys()) == {"__blob_base64__"}:
        return base64.b64decode(str(value["__blob_base64__"]).encode("ascii"))
    return value


def _encode_row(row: sqlite3.Row, columns: list[str]) -> dict[str, Any]:
    return {column: _encode_value(row[column]) for column in columns}


def _decode_row(row: dict[str, Any]) -> dict[str, Any]:
    return {column: _decode_value(value) for column, value in row.items()}


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [str(row[1]) for row in rows]


def _export_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [column for column in _table_columns(conn, table) if column != "id"]


def _snapshot_payload_for_hash(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": snapshot.get("schema_version"),
        "tables": snapshot.get("tables") or {},
    }


def _catalog_key_for_row(table: str, row: dict[str, Any]) -> str | None:
    if table == "app_settings":
        key = str(row.get("key") or "")
        scope_key = f"{str(row.get('scope') or '')}:{key}"
        if scope_key in APP_SETTING_CATALOG_KEYS:
            return APP_SETTING_CATALOG_KEYS.get(scope_key)
        return APP_SETTING_CATALOG_KEYS.get(key)
    return TABLE_CATALOG_KEYS.get(table)


def release_snapshot_resource_path() -> str:
    return os.path.join(get_resources_dir(), "rem_card", SETTINGS_RELEASE_DIR, SETTINGS_RELEASE_SNAPSHOT_FILE)


def find_release_snapshot_path() -> str:
    env_path = os.environ.get("REMCARD_SETTINGS_RELEASE_SNAPSHOT")
    if env_path:
        return os.path.abspath(env_path)
    path = release_snapshot_resource_path()
    return path if os.path.isfile(path) else ""


def export_settings_release_snapshot(
    source_baza_dir: str,
    output_path: str,
    *,
    release_version: str = "",
    release_commit: str = "",
) -> dict[str, Any]:
    db = SettingsDatabase(baza_dir=source_baza_dir)
    if not os.path.isfile(db.db_path):
        raise FileNotFoundError(
            f"Dev settings DB не найдена для release snapshot: {db.db_path}"
        )
    tables_payload: dict[str, list[dict[str, Any]]] = {}
    row_counts: dict[str, int] = {}
    with db.read_connection() as conn:
        schema_version = settings_schema.get_schema_version(conn)
        if schema_version != settings_schema.SCHEMA_VERSION:
            raise RuntimeError(
                "Dev settings DB должна быть подготовлена перед сборкой release snapshot: "
                f"{db.db_path} schema_version={schema_version}, expected={settings_schema.SCHEMA_VERSION}"
            )
        for table in RELEASE_TABLES:
            columns = _export_columns(conn, table.name)
            sql = f"SELECT {', '.join(columns)} FROM {table.name} ORDER BY {table.order_by}"
            rows = [_encode_row(row, columns) for row in conn.execute(sql).fetchall()]
            if table.name == "app_settings":
                rows = [
                    row for row in rows
                    if str(row.get("key") or "") not in TECHNICAL_APP_SETTINGS
                ]
            tables_payload[table.name] = rows
            row_counts[table.name] = len(rows)

    snapshot: dict[str, Any] = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "release_version": str(release_version or ""),
        "release_commit": str(release_commit or ""),
        "exported_at": now_text(),
        "source": "dev_settings_db",
        "source_baza_dir": os.path.abspath(os.path.normpath(source_baza_dir)),
        "tables": tables_payload,
        "row_counts": row_counts,
    }
    snapshot["content_hash"] = _content_hash(_snapshot_payload_for_hash(snapshot))
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    return {
        "snapshot_path": os.path.abspath(output_path),
        "content_hash": snapshot["content_hash"],
        "row_counts": row_counts,
        "release_version": snapshot["release_version"],
    }


def load_settings_release_snapshot(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        snapshot = json.load(handle)
    if not isinstance(snapshot, dict):
        raise ValueError("Пакет обновления настроек поврежден: ожидался JSON-объект")
    if int(snapshot.get("schema_version") or 0) != SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("Пакет обновления настроек имеет неподдерживаемую версию схемы")
    expected_hash = str(snapshot.get("content_hash") or "")
    actual_hash = _content_hash(_snapshot_payload_for_hash(snapshot))
    if not expected_hash or expected_hash != actual_hash:
        raise ValueError("Пакет обновления настроек поврежден: content_hash не совпадает")
    return snapshot


def _row_key(row: dict[str, Any], key_columns: tuple[str, ...]) -> tuple[Any, ...]:
    return tuple(row.get(column) for column in key_columns)


def _fetch_existing_row(cursor: sqlite3.Cursor, table: ReleaseTable, row: dict[str, Any]) -> dict[str, Any] | None:
    where = " AND ".join(f"{column} = ?" for column in table.key_columns)
    values = _row_key(row, table.key_columns)
    if any(value is None or str(value) == "" for value in values):
        return None
    existing = cursor.execute(f"SELECT * FROM {table.name} WHERE {where}", values).fetchone()
    return dict(existing) if existing else None


def _comparable_row(row: dict[str, Any]) -> dict[str, Any]:
    ignored = {"id", "created_at", "updated_at", "revision"}
    return {
        key: _encode_value(value)
        for key, value in row.items()
        if key not in ignored
    }


def _rows_equal(left: dict[str, Any] | None, right: dict[str, Any]) -> bool:
    if left is None:
        return False
    return _stable_json(_comparable_row(left)) == _stable_json(_comparable_row(right))


def _parse_settings_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("T", " ").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _manual_row_is_newer_than_snapshot(existing: dict[str, Any] | None, snapshot_exported_at: Any) -> bool:
    if not existing:
        return False
    if str(existing.get("source") or "") != "manual":
        return False
    existing_updated_at = _parse_settings_datetime(existing.get("updated_at"))
    snapshot_time = _parse_settings_datetime(snapshot_exported_at)
    return bool(existing_updated_at and snapshot_time and existing_updated_at > snapshot_time)


def _app_setting_is_newer_user_edit_than_snapshot(existing: dict[str, Any] | None, snapshot_exported_at: Any) -> bool:
    if not existing:
        return False
    role = str(existing.get("updated_by_role") or "").strip().lower()
    if not role or role == "system":
        return False
    existing_updated_at = _parse_settings_datetime(existing.get("updated_at"))
    snapshot_time = _parse_settings_datetime(snapshot_exported_at)
    return bool(existing_updated_at and snapshot_time and existing_updated_at > snapshot_time)


def _upsert_release_row(
    cursor: sqlite3.Cursor,
    table: ReleaseTable,
    row: dict[str, Any],
    *,
    snapshot_exported_at: Any,
) -> str:
    values = _row_key(row, table.key_columns)
    if any(value is None or str(value) == "" for value in values):
        return "skipped"
    existing = _fetch_existing_row(cursor, table, row)
    if _rows_equal(existing, row):
        return "unchanged"
    if _manual_row_is_newer_than_snapshot(existing, snapshot_exported_at):
        return "preserved"
    if table.name == "app_settings" and _app_setting_is_newer_user_edit_than_snapshot(existing, snapshot_exported_at):
        return "preserved"

    columns = list(row.keys())
    placeholders = ", ".join("?" for _ in columns)
    update_columns = [column for column in columns if column not in table.key_columns]
    update_sql = ", ".join(f"{column} = excluded.{column}" for column in update_columns)
    conflict_columns = ", ".join(table.key_columns)
    cursor.execute(
        f"""
        INSERT INTO {table.name} ({", ".join(columns)})
        VALUES ({placeholders})
        ON CONFLICT({conflict_columns}) DO UPDATE SET {update_sql}
        """,
        tuple(row[column] for column in columns),
    )
    return "inserted" if existing is None else "updated"


def apply_settings_release_snapshot(
    db: SettingsDatabase,
    snapshot_path: str,
    *,
    bump_catalog_version: Callable[..., tuple[int, str]],
) -> dict[str, Any]:
    snapshot = load_settings_release_snapshot(snapshot_path)
    snapshot_hash = str(snapshot["content_hash"])
    release_version = str(snapshot.get("release_version") or "")
    tables_raw = snapshot.get("tables") or {}
    if not isinstance(tables_raw, dict):
        raise ValueError("Пакет обновления настроек поврежден: tables должен быть объектом")

    with db.read_connection() as conn:
        row = conn.execute(
            "SELECT value FROM settings_meta WHERE key = ?",
            (SETTINGS_RELEASE_APPLIED_HASH_KEY,),
        ).fetchone()
        if row and str(row["value"] or "") == snapshot_hash:
            return {
                "applied": False,
                "reason": "already_applied",
                "snapshot_hash": snapshot_hash,
                "release_version": release_version,
                "changed_rows": 0,
            }

    changed_catalogs: set[str] = set()
    report: dict[str, Any] = {
        "applied": True,
        "snapshot_hash": snapshot_hash,
        "release_version": release_version,
        "tables": {},
        "changed_rows": 0,
        "skipped_rows": 0,
        "preserved_rows": 0,
    }
    snapshot_exported_at = snapshot.get("exported_at")
    table_by_name = {table.name: table for table in RELEASE_TABLES}
    with db.transaction("settings_release_snapshot_apply") as cursor:
        for table_name, rows_raw in tables_raw.items():
            table = table_by_name.get(str(table_name))
            if table is None:
                continue
            rows = rows_raw if isinstance(rows_raw, list) else []
            table_report = {"inserted": 0, "updated": 0, "unchanged": 0, "skipped": 0, "preserved": 0}
            for raw_row in rows:
                if not isinstance(raw_row, dict):
                    table_report["skipped"] += 1
                    continue
                row = _decode_row(raw_row)
                status = _upsert_release_row(
                    cursor,
                    table,
                    row,
                    snapshot_exported_at=snapshot_exported_at,
                )
                table_report[status] += 1
                if status in {"inserted", "updated"}:
                    catalog_key = _catalog_key_for_row(table.name, row)
                    if catalog_key:
                        changed_catalogs.add(catalog_key)
                    report["changed_rows"] += 1
                elif status == "skipped":
                    report["skipped_rows"] += 1
                elif status == "preserved":
                    report["preserved_rows"] += 1
            report["tables"][table.name] = table_report

        now = now_text()
        for key, value in (
            (SETTINGS_RELEASE_APPLIED_HASH_KEY, snapshot_hash),
            (SETTINGS_RELEASE_APPLIED_AT_KEY, now),
            (SETTINGS_RELEASE_VERSION_KEY, release_version),
        ):
            cursor.execute(
                """
                INSERT INTO settings_meta (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, now),
            )

        for catalog_key in sorted(changed_catalogs):
            bump_catalog_version(
                cursor,
                catalog_key,
                "settings_release_snapshot",
                snapshot_hash[:16],
                "release_apply",
                changed_by_role="system",
                source_client_id=f"settings_release:{snapshot_hash[:16]}",
                before=None,
                after={
                    "release_version": release_version,
                    "snapshot_hash": snapshot_hash,
                    "changed_rows": report["changed_rows"],
                },
            )
    report["changed_catalogs"] = sorted(changed_catalogs)
    return report
