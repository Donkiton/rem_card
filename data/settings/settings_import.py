from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from rem_card.data.settings.settings_db import SettingsDatabase
from rem_card.data.settings import settings_schema
from rem_card.data.settings.settings_release import (
    APP_SETTING_CATALOG_KEYS,
    RELEASE_TABLES,
    TABLE_CATALOG_KEYS,
    TECHNICAL_APP_SETTINGS,
    ReleaseTable,
)


SETTINGS_IMPORT_SOURCE_CLIENT_ID = "settings_dev_import_from_network"


@dataclass(frozen=True)
class SettingsImportChange:
    change_id: str
    operation: str
    table: str
    table_label: str
    key_values: tuple[Any, ...]
    key_label: str
    title: str
    before_row: dict[str, Any] | None
    after_row: dict[str, Any] | None
    catalog_key: str | None


@dataclass(frozen=True)
class SettingsImportPreview:
    source_path: str
    source_db_path: str
    target_db_path: str
    changes: tuple[SettingsImportChange, ...]
    row_counts: dict[str, int]


TABLE_LABELS: dict[str, str] = {
    "drug_groups": "Группы препаратов",
    "dosage_forms": "Лекарственные формы",
    "administration_routes": "Типы введения",
    "solvents": "Растворители",
    "drugs": "Препараты",
    "order_templates": "Шаблоны назначений",
    "doctors": "Список врачей",
    "diet_templates": "Шаблоны питания",
    "lab_analysis_templates": "Справочник анализов",
    "ui_backgrounds": "Фоны",
    "print_templates": "Печать",
    "operblock_icons": "Иконки",
    "app_settings": "Настройки программы",
}

APP_SETTING_LABELS: dict[str, str] = {
    "lab_materials": "Материалы анализов",
    "print_config": "Параметры печати",
    "display_settings": "Отображение",
    "lab_orders_columns": "Колонки анализов",
    "decor_settings": "Настройка декора",
    "style_settings": "Цветовая схема",
    "emergency_password": "Аварийный пароль",
    "background_settings": "Настройки фона",
    "operblock:group_routes": "Оперблок - пути введения",
    "operblock:team": "Опер. бригада",
    "operblock:anesthesia_types": "Виды пособия",
    "operblock:quick_order_buttons": "Кнопки быстрых назначений",
    "operblock:quick_orders": "Быстрые назначения",
    "operblock:medication_presets": "Настройки препаратов оперблока",
}


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _encode_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"__blob_base64__": base64.b64encode(value).decode("ascii")}
    return value


def _normalized_for_compare(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    ignored = {"id", "created_at", "updated_at", "revision"}
    return {
        key: _encode_value(value)
        for key, value in row.items()
        if key not in ignored
    }


def _rows_equal(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
    if left is None or right is None:
        return left is right
    return _stable_json(_normalized_for_compare(left)) == _stable_json(_normalized_for_compare(right))


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [str(row[1]) for row in rows]


def _export_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [column for column in _table_columns(conn, table) if column != "id"]


def _row_key(row: dict[str, Any], key_columns: tuple[str, ...]) -> tuple[Any, ...]:
    return tuple(row.get(column) for column in key_columns)


def _key_label(table: ReleaseTable, key_values: tuple[Any, ...]) -> str:
    if table.name == "app_settings" and len(key_values) >= 2:
        return f"{key_values[0]}:{key_values[1]}"
    if len(key_values) == 1:
        return str(key_values[0])
    return " / ".join(str(value) for value in key_values)


def _change_id(table: ReleaseTable, key_values: tuple[Any, ...]) -> str:
    payload = _stable_json([table.name, list(key_values)])
    return f"{table.name}:{_hash_text(payload)[:16]}"


def _catalog_key_for_row(table_name: str, row: dict[str, Any] | None) -> str | None:
    if row is None:
        return TABLE_CATALOG_KEYS.get(table_name)
    if table_name == "app_settings":
        key = str(row.get("key") or "")
        scope_key = f"{str(row.get('scope') or '')}:{key}"
        if scope_key in APP_SETTING_CATALOG_KEYS:
            return APP_SETTING_CATALOG_KEYS[scope_key]
        return APP_SETTING_CATALOG_KEYS.get(key)
    return TABLE_CATALOG_KEYS.get(table_name)


def _row_title(table_name: str, row: dict[str, Any] | None, key_label: str) -> str:
    if not row:
        return key_label
    if table_name == "app_settings":
        key = str(row.get("key") or "")
        scope_key = f"{str(row.get('scope') or '')}:{key}"
        return APP_SETTING_LABELS.get(scope_key) or APP_SETTING_LABELS.get(key) or key_label
    for column in ("display_name", "name", "full_name", "latin", "template_key", "analysis_code", "icon_key"):
        value = str(row.get(column) or "").strip()
        if value:
            return value
    return key_label


def _rows_by_key(conn: sqlite3.Connection, table: ReleaseTable) -> dict[tuple[Any, ...], dict[str, Any]]:
    columns = _export_columns(conn, table.name)
    rows = conn.execute(
        f"SELECT {', '.join(columns)} FROM {table.name} ORDER BY {table.order_by}"
    ).fetchall()
    result: dict[tuple[Any, ...], dict[str, Any]] = {}
    for raw_row in rows:
        row = dict(raw_row)
        if table.name == "app_settings" and str(row.get("key") or "") in TECHNICAL_APP_SETTINGS:
            continue
        key = _row_key(row, table.key_columns)
        if any(value is None or str(value) == "" for value in key):
            continue
        result[key] = row
    return result


def _same_file_path(left: str, right: str) -> bool:
    try:
        return os.path.samefile(left, right)
    except Exception:
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def resolve_settings_db_path(path: str) -> str:
    raw_path = str(path or "").strip().strip('"')
    if not raw_path:
        raise ValueError("Путь к базе настроек не задан.")
    normalized = os.path.abspath(os.path.normpath(raw_path))
    if os.path.isfile(normalized):
        if os.path.basename(normalized).lower() != "remcard_settings.db":
            raise ValueError("Выбранный файл не является remcard_settings.db.")
        return normalized
    if not os.path.isdir(normalized):
        raise FileNotFoundError(f"Путь недоступен: {normalized}")

    direct_candidates = [
        os.path.join(normalized, "settings", "remcard_settings.db"),
        os.path.join(normalized, "remcard_settings.db"),
    ]
    for candidate in direct_candidates:
        if os.path.isfile(candidate):
            return os.path.abspath(os.path.normpath(candidate))

    matches: list[str] = []
    skipped_dirs = {".git", "__pycache__", "backups", "backup_health", "corrupted_db", "quarantine"}
    for root, dirs, files in os.walk(normalized):
        dirs[:] = [name for name in dirs if name not in skipped_dirs]
        if "remcard_settings.db" in files:
            matches.append(os.path.join(root, "remcard_settings.db"))
        if len(matches) > 20:
            break
    if not matches:
        raise FileNotFoundError(
            "Не удалось найти remcard_settings.db. Укажите папку базы RemCard или файл settings/remcard_settings.db."
        )
    matches.sort(key=lambda item: (0 if os.path.basename(os.path.dirname(item)).lower() == "settings" else 1, len(item), item))
    return os.path.abspath(os.path.normpath(matches[0]))


def _build_preview_from_connections(
    *,
    source_path: str,
    source_db_path: str,
    target_db_path: str,
    source_conn: sqlite3.Connection,
    target_conn: sqlite3.Connection,
) -> SettingsImportPreview:
    changes: list[SettingsImportChange] = []
    row_counts: dict[str, int] = {}
    for table in RELEASE_TABLES:
        source_rows = _rows_by_key(source_conn, table)
        target_rows = _rows_by_key(target_conn, table)
        row_counts[table.name] = len(source_rows)
        all_keys = sorted(set(source_rows) | set(target_rows), key=lambda values: tuple(str(value) for value in values))
        for key_values in all_keys:
            source_row = source_rows.get(key_values)
            target_row = target_rows.get(key_values)
            if _rows_equal(target_row, source_row):
                continue
            operation = "insert" if target_row is None else "delete" if source_row is None else "update"
            active_row = source_row or target_row or {}
            key_label = _key_label(table, key_values)
            changes.append(
                SettingsImportChange(
                    change_id=_change_id(table, key_values),
                    operation=operation,
                    table=table.name,
                    table_label=TABLE_LABELS.get(table.name, table.name),
                    key_values=tuple(key_values),
                    key_label=key_label,
                    title=_row_title(table.name, active_row, key_label),
                    before_row=target_row,
                    after_row=source_row,
                    catalog_key=_catalog_key_for_row(table.name, active_row),
                )
            )
    return SettingsImportPreview(
        source_path=os.path.abspath(os.path.normpath(source_path)),
        source_db_path=source_db_path,
        target_db_path=target_db_path,
        changes=tuple(changes),
        row_counts=row_counts,
    )


def preview_settings_import(target_db: SettingsDatabase, source_path: str) -> SettingsImportPreview:
    source_db_path = resolve_settings_db_path(source_path)
    target_db_path = os.path.abspath(os.path.normpath(target_db.db_path))
    if _same_file_path(source_db_path, target_db_path):
        raise ValueError("Источник совпадает с текущей dev БД настроек.")

    source_db = SettingsDatabase(settings_db_path=source_db_path, readonly=True)
    source_db.ensure_ready()
    target_db.ensure_ready()
    with source_db.read_connection() as source_conn, target_db.read_connection() as target_conn:
        source_schema = settings_schema.get_schema_version(source_conn)
        target_schema = settings_schema.get_schema_version(target_conn)
        if source_schema != target_schema:
            raise ValueError(
                f"Версии схем settings DB отличаются: источник={source_schema}, dev={target_schema}."
            )
        return _build_preview_from_connections(
            source_path=source_path,
            source_db_path=source_db_path,
            target_db_path=target_db_path,
            source_conn=source_conn,
            target_conn=target_conn,
        )


def _upsert_row(cursor: sqlite3.Cursor, table: ReleaseTable, row: dict[str, Any]) -> None:
    columns = list(row.keys())
    if not columns:
        return
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


def _delete_row(cursor: sqlite3.Cursor, table: ReleaseTable, key_values: tuple[Any, ...]) -> None:
    where = " AND ".join(f"{column} = ?" for column in table.key_columns)
    cursor.execute(f"DELETE FROM {table.name} WHERE {where}", tuple(key_values))


def apply_settings_import(
    target_db: SettingsDatabase,
    source_path: str,
    selected_change_ids: Iterable[str],
    *,
    bump_catalog_version: Callable[..., tuple[int, str]],
) -> dict[str, Any]:
    preview = preview_settings_import(target_db, source_path)
    selected_ids = {str(change_id) for change_id in selected_change_ids or []}
    selected_changes = [change for change in preview.changes if change.change_id in selected_ids]
    if not selected_changes:
        return {
            "applied": False,
            "reason": "no_selected_changes",
            "source_db_path": preview.source_db_path,
            "changed_rows": 0,
            "changed_catalogs": [],
        }

    table_by_name = {table.name: table for table in RELEASE_TABLES}
    changed_catalogs: set[str] = set()
    counts = {"insert": 0, "update": 0, "delete": 0}
    with target_db.transaction("settings_dev_import_from_network") as cursor:
        for change in selected_changes:
            table = table_by_name.get(change.table)
            if table is None:
                continue
            if change.operation in {"insert", "update"} and change.after_row is not None:
                _upsert_row(cursor, table, dict(change.after_row))
            elif change.operation == "delete":
                _delete_row(cursor, table, change.key_values)
            else:
                continue
            counts[change.operation] = counts.get(change.operation, 0) + 1
            if change.catalog_key:
                changed_catalogs.add(change.catalog_key)

        after_payload = {
            "source_db_path": preview.source_db_path,
            "selected_change_ids": sorted(selected_ids),
            "counts": dict(counts),
        }
        for catalog_key in sorted(changed_catalogs):
            bump_catalog_version(
                cursor,
                catalog_key,
                "settings_dev_import",
                None,
                "import_from_network",
                changed_by_role="system",
                source_client_id=SETTINGS_IMPORT_SOURCE_CLIENT_ID,
                before=None,
                after=after_payload,
            )

    return {
        "applied": True,
        "source_db_path": preview.source_db_path,
        "target_db_path": preview.target_db_path,
        "changed_rows": sum(counts.values()),
        "counts": counts,
        "changed_catalogs": sorted(changed_catalogs),
    }
