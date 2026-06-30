from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from rem_card.app.runtime_paths import get_resources_dir
from rem_card.app.settings_db_paths import SETTINGS_BACKGROUNDS_DIR_NAME
from rem_card.data.settings.settings_db import SettingsDatabase
from rem_card.data.settings import settings_schema
from rem_card.data.settings.settings_schema import now_text


SNAPSHOT_SCHEMA_VERSION = 1
SETTINGS_RELEASE_DIR = "settings_release"
SETTINGS_RELEASE_SNAPSHOT_FILE = "settings_release_snapshot.json"
SETTINGS_RELEASE_MANIFEST_FILE = "settings_release_manifest.json"
SETTINGS_RELEASE_MEDIA_DIR = "media"
SETTINGS_RELEASE_APPLIED_HASH_KEY = "settings_release_snapshot_applied_hash"
SETTINGS_RELEASE_APPLIED_AT_KEY = "settings_release_snapshot_applied_at"
SETTINGS_RELEASE_VERSION_KEY = "settings_release_snapshot_version"
SYSTEM_CHANGED_BY_ROLES = {"", "system"}
USER_ROW_SOURCES = {"manual", "override"}
BLOB_BASE64_MARKER = "__blob_base64__"
BLOB_FILE_MARKER = "__blob_file__"
IMAGE_BLOB_TABLES = {"ui_backgrounds", "operblock_icons"}
EXTERNAL_BLOB_COLUMNS: dict[str, set[str]] = {
    "ui_backgrounds": {"image_blob"},
    "operblock_icons": {"image_blob"},
    "print_templates": {"logo_blob"},
}


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
    "decor_settings": "display_settings",
    "style_settings": "style_settings",
    "emergency_password": "emergency_password",
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
        return {BLOB_BASE64_MARKER: base64.b64encode(value).decode("ascii")}
    return value


def _is_blob_file_ref(value: Any) -> bool:
    return isinstance(value, dict) and BLOB_FILE_MARKER in value


def _is_blob_payload(value: Any) -> bool:
    return (
        isinstance(value, (bytes, bytearray))
        or _is_blob_file_ref(value)
        or (isinstance(value, dict) and BLOB_BASE64_MARKER in value)
    )


def _safe_media_name_part(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._-")
    return text[:80] or "row"


def _safe_snapshot_child_path(root_dir: str, relative_path: str) -> str:
    normalized = str(relative_path or "").strip().replace("\\", "/")
    if not normalized or normalized.startswith("/") or ".." in normalized.split("/"):
        raise ValueError("Пакет обновления настроек поврежден: некорректный путь media-файла")
    root_abs = os.path.abspath(os.path.normpath(root_dir))
    path_abs = os.path.abspath(os.path.normpath(os.path.join(root_abs, *normalized.split("/"))))
    try:
        common = os.path.commonpath([root_abs, path_abs])
    except ValueError as exc:
        raise ValueError("Пакет обновления настроек поврежден: media-файл вне snapshot") from exc
    if common != root_abs:
        raise ValueError("Пакет обновления настроек поврежден: media-файл вне snapshot")
    return path_abs


def _snapshot_manifest_path(snapshot_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(snapshot_path)), SETTINGS_RELEASE_MANIFEST_FILE)


def _snapshot_media_dir(snapshot_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(snapshot_path)), SETTINGS_RELEASE_MEDIA_DIR)


def _write_blob_file(
    *,
    snapshot_dir: str,
    table: ReleaseTable,
    row: sqlite3.Row,
    column: str,
    value: bytes,
) -> dict[str, Any]:
    row_key_parts = [
        _safe_media_name_part(row[key_column])
        for key_column in table.key_columns
        if key_column in row.keys()
    ]
    row_key = "__".join(row_key_parts) or "row"
    digest = hashlib.sha256(value).hexdigest()
    file_name = f"{row_key}__{_safe_media_name_part(column)}__{digest[:16]}.blob"
    rel_path = "/".join((SETTINGS_RELEASE_MEDIA_DIR, table.name, file_name))
    abs_path = _safe_snapshot_child_path(snapshot_dir, rel_path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "wb") as handle:
        handle.write(value)
    return {
        BLOB_FILE_MARKER: rel_path,
        "sha256": digest,
        "size": len(value),
    }


def _decode_blob_file_ref(value: dict[str, Any], snapshot_dir: str) -> bytes:
    rel_path = str(value.get(BLOB_FILE_MARKER) or "")
    path = _safe_snapshot_child_path(snapshot_dir, rel_path)
    with open(path, "rb") as handle:
        blob = handle.read()
    expected_size = value.get("size")
    if expected_size is not None and int(expected_size) != len(blob):
        raise ValueError("Пакет обновления настроек поврежден: размер media-файла не совпадает")
    expected_hash = str(value.get("sha256") or "")
    if expected_hash and hashlib.sha256(blob).hexdigest() != expected_hash:
        raise ValueError("Пакет обновления настроек поврежден: sha256 media-файла не совпадает")
    return blob


def _decode_value(value: Any, *, snapshot_dir: str = "", decode_blob_files: bool = True) -> Any:
    if isinstance(value, dict) and set(value.keys()) == {BLOB_BASE64_MARKER}:
        return base64.b64decode(str(value[BLOB_BASE64_MARKER]).encode("ascii"))
    if _is_blob_file_ref(value) and decode_blob_files:
        if not snapshot_dir:
            raise ValueError("Пакет обновления настроек поврежден: media-файл без базового пути")
        return _decode_blob_file_ref(value, snapshot_dir)
    return value


def _encode_row(
    row: sqlite3.Row,
    columns: list[str],
    *,
    table: ReleaseTable,
    snapshot_dir: str,
) -> dict[str, Any]:
    blob_columns = EXTERNAL_BLOB_COLUMNS.get(table.name) or set()
    encoded: dict[str, Any] = {}
    for column in columns:
        value = row[column]
        if isinstance(value, bytes) and value and column in blob_columns:
            encoded[column] = _write_blob_file(
                snapshot_dir=snapshot_dir,
                table=table,
                row=row,
                column=column,
                value=value,
            )
        else:
            encoded[column] = _encode_value(value)
    return encoded


def _decode_row(
    row: dict[str, Any],
    *,
    snapshot_dir: str = "",
    decode_blob_files: bool = True,
) -> dict[str, Any]:
    return {
        column: _decode_value(value, snapshot_dir=snapshot_dir, decode_blob_files=decode_blob_files)
        for column, value in row.items()
    }


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


def _release_manifest(snapshot: dict[str, Any], *, snapshot_file: str = SETTINGS_RELEASE_SNAPSHOT_FILE) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "snapshot_schema_version": snapshot.get("schema_version"),
        "snapshot_file": snapshot_file,
        "content_hash": snapshot.get("content_hash") or "",
        "release_version": snapshot.get("release_version") or "",
        "release_commit": snapshot.get("release_commit") or "",
        "exported_at": snapshot.get("exported_at") or "",
        "row_counts": snapshot.get("row_counts") or {},
    }


def _reset_snapshot_media_dir(snapshot_dir: str) -> str:
    snapshot_dir_abs = os.path.abspath(os.path.normpath(snapshot_dir))
    media_dir = os.path.abspath(os.path.normpath(os.path.join(snapshot_dir_abs, SETTINGS_RELEASE_MEDIA_DIR)))
    try:
        common = os.path.commonpath([snapshot_dir_abs, media_dir])
    except ValueError:
        common = ""
    if common == snapshot_dir_abs and os.path.basename(media_dir) == SETTINGS_RELEASE_MEDIA_DIR:
        shutil.rmtree(media_dir, ignore_errors=True)
    os.makedirs(media_dir, exist_ok=True)
    return media_dir


def load_settings_release_manifest(snapshot_path: str) -> dict[str, Any] | None:
    manifest_path = _snapshot_manifest_path(snapshot_path)
    if not os.path.isfile(manifest_path):
        return None
    with open(manifest_path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if not isinstance(manifest, dict):
        return None
    if int(manifest.get("schema_version") or 0) != 1:
        return None
    content_hash = str(manifest.get("content_hash") or "")
    if not content_hash:
        return None
    return manifest


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
    output_abs = os.path.abspath(output_path)
    snapshot_dir = os.path.dirname(output_abs)
    os.makedirs(snapshot_dir, exist_ok=True)
    _reset_snapshot_media_dir(snapshot_dir)

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
            rows = [
                _encode_row(row, columns, table=table, snapshot_dir=snapshot_dir)
                for row in conn.execute(sql).fetchall()
            ]
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
    with open(output_abs, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    manifest_path = _snapshot_manifest_path(output_abs)
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(
            _release_manifest(snapshot, snapshot_file=os.path.basename(output_abs)),
            handle,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        handle.write("\n")
    media_files = 0
    media_bytes = 0
    media_dir = _snapshot_media_dir(output_abs)
    if os.path.isdir(media_dir):
        for root, _dirs, files in os.walk(media_dir):
            for file_name in files:
                media_files += 1
                try:
                    media_bytes += os.path.getsize(os.path.join(root, file_name))
                except OSError:
                    pass
    return {
        "snapshot_path": output_abs,
        "manifest_path": manifest_path,
        "media_dir": media_dir,
        "media_files": media_files,
        "media_bytes": media_bytes,
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


def _comparable_row(row: dict[str, Any], *, ignored_columns: set[str] | None = None) -> dict[str, Any]:
    ignored = {"id", "created_at", "updated_at", "revision"}
    if ignored_columns:
        ignored.update(ignored_columns)
    return {
        key: _encode_value(value)
        for key, value in row.items()
        if key not in ignored
    }


def _rows_equal(left: dict[str, Any] | None, right: dict[str, Any], *, table_name: str = "") -> bool:
    if left is None:
        return False
    ignored_columns: set[str] = set()
    if table_name in IMAGE_BLOB_TABLES:
        left_hash = str(left.get("image_hash") or "")
        right_hash = str(right.get("image_hash") or "")
        if left_hash and left_hash == right_hash:
            if table_name == "ui_backgrounds" or left.get("image_blob") is not None or not _is_blob_payload(right.get("image_blob")):
                ignored_columns.add("image_blob")
    return _stable_json(_comparable_row(left, ignored_columns=ignored_columns)) == _stable_json(
        _comparable_row(right, ignored_columns=ignored_columns)
    )


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
    if str(existing.get("source") or "").strip().lower() not in USER_ROW_SOURCES:
        return False
    existing_updated_at = _parse_settings_datetime(existing.get("updated_at"))
    snapshot_time = _parse_settings_datetime(snapshot_exported_at)
    return bool(existing_updated_at and snapshot_time and existing_updated_at > snapshot_time)


def _app_setting_is_newer_user_edit_than_snapshot(existing: dict[str, Any] | None, snapshot_exported_at: Any) -> bool:
    if not existing:
        return False
    role = str(existing.get("updated_by_role") or "").strip().lower()
    if role in SYSTEM_CHANGED_BY_ROLES:
        return False
    existing_updated_at = _parse_settings_datetime(existing.get("updated_at"))
    snapshot_time = _parse_settings_datetime(snapshot_exported_at)
    return bool(existing_updated_at and snapshot_time and existing_updated_at > snapshot_time)


def _app_setting_is_user_edit(existing: dict[str, Any] | None) -> bool:
    if not existing:
        return False
    role = str(existing.get("updated_by_role") or "").strip().lower()
    return role not in SYSTEM_CHANGED_BY_ROLES


def _row_entity_id_candidates(table: ReleaseTable, existing: dict[str, Any], row: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for value in (existing.get("id"), row.get("id")):
        if value is not None and str(value) != "":
            candidates.append(str(value))
    for column in table.key_columns:
        for payload in (existing, row):
            value = payload.get(column)
            if value is not None and str(value) != "":
                candidates.append(str(value))
    result: list[str] = []
    seen: set[str] = set()
    for value in candidates:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _row_has_user_change_log(
    cursor: sqlite3.Cursor,
    table: ReleaseTable,
    existing: dict[str, Any] | None,
    row: dict[str, Any],
) -> bool:
    if not existing:
        return False
    candidates = _row_entity_id_candidates(table, existing, row)
    entity_filter = "entity_id IS NULL"
    params: list[Any] = [table.name]
    if candidates:
        placeholders = ", ".join("?" for _ in candidates)
        entity_filter = f"(entity_id IS NULL OR entity_id IN ({placeholders}))"
        params.extend(candidates)
    try:
        found = cursor.execute(
            f"""
            SELECT 1
            FROM settings_change_log
            WHERE entity_type = ?
              AND LOWER(COALESCE(changed_by_role, '')) NOT IN ('', 'system')
              AND {entity_filter}
            LIMIT 1
            """,
            tuple(params),
        ).fetchone()
    except sqlite3.Error:
        return False
    return bool(found)


def _release_row_for_apply(row: dict[str, Any]) -> dict[str, Any]:
    applied = dict(row)
    if "source" in applied:
        applied["source"] = "release"
    return applied


def _safe_file_name(value: Any) -> str:
    return os.path.basename(str(value or "").strip().replace("\\", os.sep).replace("/", os.sep))


def _background_file_name_from_row(row: dict[str, Any]) -> str:
    try:
        value = json.loads(str(row.get("value_json") or "{}"))
    except Exception:
        value = {}
    if not isinstance(value, dict):
        return ""
    return _safe_file_name(value.get("file"))


def _copy_blob_atomic(blob: bytes, target_path: str) -> None:
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    tmp_path = f"{target_path}.{os.getpid()}.tmp"
    try:
        with open(tmp_path, "wb") as handle:
            handle.write(blob)
        os.replace(tmp_path, target_path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def _prepare_row_media_for_apply(
    row: dict[str, Any],
    *,
    table_name: str,
    snapshot_dir: str,
    db: SettingsDatabase,
) -> dict[str, Any]:
    prepared = dict(row)
    blob_columns = EXTERNAL_BLOB_COLUMNS.get(table_name) or set()
    for column in blob_columns:
        value = prepared.get(column)
        if _is_blob_file_ref(value):
            prepared[column] = _decode_blob_file_ref(value, snapshot_dir)

    if table_name == "ui_backgrounds" and isinstance(prepared.get("image_blob"), (bytes, bytearray)):
        file_name = _background_file_name_from_row(prepared)
        if file_name:
            target_path = os.path.join(db.settings_dir, SETTINGS_BACKGROUNDS_DIR_NAME, file_name)
            try:
                _copy_blob_atomic(bytes(prepared["image_blob"]), target_path)
                prepared["image_blob"] = None
            except Exception:
                pass
    return prepared


def _upsert_release_row(
    cursor: sqlite3.Cursor,
    table: ReleaseTable,
    row: dict[str, Any],
    *,
    snapshot_exported_at: Any,
    snapshot_dir: str,
    db: SettingsDatabase,
    preserve_existing_background_rows: bool = False,
) -> str:
    apply_row = _release_row_for_apply(row)
    values = _row_key(apply_row, table.key_columns)
    if any(value is None or str(value) == "" for value in values):
        return "skipped"
    existing = _fetch_existing_row(cursor, table, apply_row)
    if _rows_equal(existing, apply_row, table_name=table.name):
        return "unchanged"
    if table.name == "ui_backgrounds" and preserve_existing_background_rows and existing is not None:
        return "preserved"
    if table.name == "app_settings" and _app_setting_is_user_edit(existing):
        return "preserved"
    if _row_has_user_change_log(cursor, table, existing, apply_row):
        return "preserved"
    if _manual_row_is_newer_than_snapshot(existing, snapshot_exported_at):
        return "preserved"
    if table.name == "app_settings" and _app_setting_is_newer_user_edit_than_snapshot(existing, snapshot_exported_at):
        return "preserved"

    apply_row = _prepare_row_media_for_apply(
        apply_row,
        table_name=table.name,
        snapshot_dir=snapshot_dir,
        db=db,
    )
    columns = list(apply_row.keys())
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
        tuple(apply_row[column] for column in columns),
    )
    return "inserted" if existing is None else "updated"


def apply_settings_release_snapshot(
    db: SettingsDatabase,
    snapshot_path: str,
    *,
    bump_catalog_version: Callable[..., tuple[int, str]],
) -> dict[str, Any]:
    snapshot_dir = os.path.dirname(os.path.abspath(snapshot_path))
    try:
        manifest = load_settings_release_manifest(snapshot_path)
    except Exception:
        manifest = None
    if manifest:
        manifest_hash = str(manifest.get("content_hash") or "")
        with db.read_connection() as conn:
            row = conn.execute(
                "SELECT value FROM settings_meta WHERE key = ?",
                (SETTINGS_RELEASE_APPLIED_HASH_KEY,),
            ).fetchone()
            if row and str(row["value"] or "") == manifest_hash:
                return {
                    "applied": False,
                    "reason": "already_applied",
                    "snapshot_hash": manifest_hash,
                    "release_version": str(manifest.get("release_version") or ""),
                    "changed_rows": 0,
                    "fast_path": "manifest",
                }

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
        existing_background_setting = cursor.execute(
            "SELECT * FROM app_settings WHERE scope = 'shared' AND key = 'background_settings'"
        ).fetchone()
        preserve_existing_background_rows = _app_setting_is_user_edit(
            dict(existing_background_setting) if existing_background_setting else None
        )
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
                row = _decode_row(raw_row, snapshot_dir=snapshot_dir, decode_blob_files=False)
                status = _upsert_release_row(
                    cursor,
                    table,
                    row,
                    snapshot_exported_at=snapshot_exported_at,
                    snapshot_dir=snapshot_dir,
                    db=db,
                    preserve_existing_background_rows=preserve_existing_background_rows,
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
