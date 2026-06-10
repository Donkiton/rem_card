from __future__ import annotations

import os
from typing import Iterable

from PySide6.QtGui import QPixmap

from rem_card.app.paths import get_icon_dir
from rem_card.services.remcard_icon_defaults import (
    REMCARD_ICON_STORAGE_CATALOG_KEY,
    default_remcard_icon_file_for_key,
)


_ICON_RECORD_CACHE: dict[str, object] = {
    "version": None,
    "records": {},
}


def invalidate_remcard_icon_cache() -> None:
    _ICON_RECORD_CACHE["version"] = None
    _ICON_RECORD_CACHE["records"] = {}


def _normalized_icon_keys(icon_keys: str | Iterable[str]) -> list[str]:
    if isinstance(icon_keys, str):
        return [icon_keys] if icon_keys.strip() else []
    result: list[str] = []
    for key in icon_keys or []:
        text = str(key or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _load_icon_records_from_db() -> dict[str, dict]:
    try:
        from rem_card.services.settings.settings_service import get_settings_service

        service = get_settings_service()
        version = service.get_catalog_version(REMCARD_ICON_STORAGE_CATALOG_KEY)
        if _ICON_RECORD_CACHE.get("version") == version:
            records = _ICON_RECORD_CACHE.get("records")
            return dict(records) if isinstance(records, dict) else {}
        records = service.list_remcard_icons()
        _ICON_RECORD_CACHE["version"] = version
        _ICON_RECORD_CACHE["records"] = dict(records)
        return dict(records)
    except Exception:
        return {}


def remcard_icon_record(icon_keys: str | Iterable[str]) -> dict | None:
    records = _load_icon_records_from_db()
    for key in _normalized_icon_keys(icon_keys):
        record = records.get(key)
        if isinstance(record, dict) and record.get("image_blob"):
            return record
    return None


def fallback_icon_path(file_name: str) -> str:
    return os.path.join(get_icon_dir(), os.path.basename(str(file_name or "").strip()))


def load_remcard_icon_pixmap(
    icon_keys: str | Iterable[str],
    *,
    fallback_file: str = "",
) -> QPixmap:
    record = remcard_icon_record(icon_keys)
    if record is not None:
        pixmap = QPixmap()
        blob = record.get("image_blob")
        if isinstance(blob, (bytes, bytearray)) and pixmap.loadFromData(bytes(blob)):
            return pixmap

    keys = _normalized_icon_keys(icon_keys)
    fallback = str(fallback_file or "").strip()
    if not fallback and keys:
        fallback = default_remcard_icon_file_for_key(keys[0], "")
    if fallback:
        pixmap = QPixmap(fallback_icon_path(fallback))
        if not pixmap.isNull():
            return pixmap
    return QPixmap()


def current_remcard_icon_source(
    icon_keys: str | Iterable[str],
    *,
    fallback_file: str = "",
) -> str:
    record = remcard_icon_record(icon_keys)
    if record is not None:
        value = record.get("value") if isinstance(record.get("value"), dict) else {}
        source_file = str(value.get("source_file") or "").strip()
        return f"из БД: {source_file or record.get('image_hash') or record.get('icon_key')}"
    keys = _normalized_icon_keys(icon_keys)
    fallback = str(fallback_file or "").strip()
    if not fallback and keys:
        fallback = default_remcard_icon_file_for_key(keys[0], "")
    return fallback or "стандартная иконка"
