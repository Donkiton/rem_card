from __future__ import annotations

import json
import os
import re
import tempfile
from typing import Any, Mapping

from rem_card.app.paths import USER_DICT_DIR


OPERBLOCK_ANESTHESIA_TYPES_OVERRIDE_KEY = "operblock_anesthesia_types"
OPERBLOCK_ANESTHESIA_TYPES_VERSION = 1


def _read_json_dict(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_atomic(path: str, payload: dict[str, Any]) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".operblock_anesthesia_types_", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def normalize_operblock_anesthesia_type_label(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _slug(value: Any) -> str:
    text = normalize_operblock_anesthesia_type_label(value).casefold()
    text = re.sub(r"[^0-9a-zа-яё]+", "_", text, flags=re.IGNORECASE).strip("_")
    return text or "type"


def _normalize_item(raw: Any, index: int, used_ids: set[str], used_labels: set[str]) -> dict[str, Any] | None:
    if isinstance(raw, str):
        raw = {"label": raw}
    if not isinstance(raw, Mapping):
        return None
    label = normalize_operblock_anesthesia_type_label(raw.get("label") or raw.get("name") or raw.get("title"))
    if not label:
        return None
    label_key = label.casefold()
    if label_key in used_labels:
        return None
    used_labels.add(label_key)

    raw_id = str(raw.get("id") or "").strip()
    item_id = re.sub(r"[^0-9A-Za-zа-яА-ЯёЁ_-]+", "_", raw_id).strip("_") if raw_id else ""
    if not item_id:
        item_id = f"type_{_slug(label)}"
    base_id = item_id
    suffix = 2
    while item_id in used_ids:
        item_id = f"{base_id}_{suffix}"
        suffix += 1
    used_ids.add(item_id)

    try:
        sort_order = int(raw.get("sort_order"))
    except (TypeError, ValueError):
        sort_order = int(index) * 10

    return {
        "id": item_id,
        "label": label,
        "sort_order": sort_order,
    }


def normalize_operblock_anesthesia_types_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, list):
        raw_items = payload
    elif isinstance(payload, Mapping):
        raw_items = payload.get("items") if isinstance(payload.get("items"), list) else []
    else:
        raw_items = []

    items: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    used_labels: set[str] = set()
    for index, raw_item in enumerate(raw_items, start=1):
        item = _normalize_item(raw_item, index, used_ids, used_labels)
        if item:
            items.append(item)
    items.sort(key=lambda item: (int(item.get("sort_order") or 0), str(item.get("label") or "").casefold()))
    for index, item in enumerate(items, start=1):
        item["sort_order"] = index * 10
    return {"version": OPERBLOCK_ANESTHESIA_TYPES_VERSION, "items": items}


def load_operblock_anesthesia_types(*, user_dict_dir: str | None = None) -> list[dict[str, Any]]:
    resolved_user_dir = user_dict_dir or USER_DICT_DIR
    overrides_path = os.path.join(resolved_user_dir, "user_overrides.json")
    overrides = _read_json_dict(overrides_path)
    payload = normalize_operblock_anesthesia_types_payload(overrides.get(OPERBLOCK_ANESTHESIA_TYPES_OVERRIDE_KEY))
    return list(payload["items"])


def save_operblock_anesthesia_types(
    items: list[dict[str, Any]],
    *,
    user_dict_dir: str | None = None,
) -> list[dict[str, Any]]:
    payload = normalize_operblock_anesthesia_types_payload(
        {"version": OPERBLOCK_ANESTHESIA_TYPES_VERSION, "items": items}
    )
    resolved_user_dir = user_dict_dir or USER_DICT_DIR
    overrides_path = os.path.join(resolved_user_dir, "user_overrides.json")
    overrides = _read_json_dict(overrides_path)
    overrides[OPERBLOCK_ANESTHESIA_TYPES_OVERRIDE_KEY] = payload
    _write_json_atomic(overrides_path, overrides)
    return list(payload["items"])

