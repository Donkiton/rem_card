from __future__ import annotations

import os
from typing import Iterable

from PySide6.QtGui import QPixmap

from rem_card.app.paths import get_icon_dir
from rem_card.services.operblock_icon_defaults import (
    OPERBLOCK_ICONS_KEY,
    default_icon_file_for_key,
    first_icon_key,
    slug_for_icon_key,
)


_ICON_RECORD_CACHE: dict[str, object] = {
    "version": None,
    "records": {},
}


def invalidate_operblock_icon_cache() -> None:
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
        version = service.get_catalog_version(OPERBLOCK_ICONS_KEY)
        if _ICON_RECORD_CACHE.get("version") == version:
            records = _ICON_RECORD_CACHE.get("records")
            return dict(records) if isinstance(records, dict) else {}
        records = service.list_operblock_icons()
        _ICON_RECORD_CACHE["version"] = version
        _ICON_RECORD_CACHE["records"] = dict(records)
        return dict(records)
    except Exception:
        return {}


def operblock_icon_record(icon_keys: str | Iterable[str]) -> dict | None:
    records = _load_icon_records_from_db()
    normalized_keys = _normalized_icon_keys(icon_keys)
    for key in normalized_keys:
        record = records.get(key)
        if isinstance(record, dict) and record.get("image_blob"):
            return record
    label_slugs = {
        key.split(":", 1)[1]
        for key in normalized_keys
        if key.startswith("drug-label:") and key.split(":", 1)[1]
    }
    if label_slugs:
        for record in records.values():
            if not isinstance(record, dict) or not record.get("image_blob"):
                continue
            if str(record.get("category") or "") != "drug":
                continue
            name = str(record.get("name") or "")
            for prefix in ("Иконка препарата:", "иконка препарата:"):
                if name.startswith(prefix):
                    name = name[len(prefix):].strip()
                    break
            if slug_for_icon_key(name) in label_slugs:
                return record
    return None


def fallback_icon_path(file_name: str) -> str:
    return os.path.join(get_icon_dir(), os.path.basename(str(file_name or "").strip()))


def load_operblock_icon_pixmap(
    icon_keys: str | Iterable[str],
    *,
    fallback_file: str = "",
) -> QPixmap:
    record = operblock_icon_record(icon_keys)
    if record is not None:
        pixmap = QPixmap()
        blob = record.get("image_blob")
        if isinstance(blob, (bytes, bytearray)) and pixmap.loadFromData(bytes(blob)):
            return pixmap

    fallback = str(fallback_file or "").strip()
    if not fallback:
        fallback = default_icon_file_for_key(first_icon_key(icon_keys), "")
    if fallback:
        pixmap = QPixmap(fallback_icon_path(fallback))
        if not pixmap.isNull():
            return pixmap
    return QPixmap()


def current_operblock_icon_source(
    icon_keys: str | Iterable[str],
    *,
    fallback_file: str = "",
) -> str:
    record = operblock_icon_record(icon_keys)
    if record is not None:
        value = record.get("value") if isinstance(record.get("value"), dict) else {}
        source_file = str(value.get("source_file") or "").strip()
        return f"из БД: {source_file or record.get('image_hash') or record.get('icon_key')}"
    fallback = str(fallback_file or "").strip() or default_icon_file_for_key(first_icon_key(icon_keys), "")
    return fallback or "стандартная иконка"
