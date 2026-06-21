from __future__ import annotations

from copy import deepcopy
import json
import os
import re
import tempfile
from typing import Any, Mapping

from rem_card.app.paths import USER_DICT_DIR
from rem_card.services.settings.settings_service import (
    OPERBLOCK_QUICK_ORDER_BUTTONS_APP_KEY,
    OPERBLOCK_SETTINGS_KEY,
    OPERBLOCK_SETTINGS_SCOPE,
    get_settings_service,
)


OPERBLOCK_QUICK_ORDER_BUTTONS_OVERRIDE_KEY = "operblock_quick_order_buttons"
OPERBLOCK_QUICK_ORDER_BUTTONS_VERSION = 1
OPERBLOCK_EXTRA_QUICK_TYPE_PREFIX = "extra:"

BUILTIN_OPERBLOCK_QUICK_ORDER_BUTTONS: tuple[dict[str, Any], ...] = (
    {"key": "bolus", "label": "Болюсы", "built_in": True, "sort_order": 10},
    {"key": "continuous_infusion", "label": "Дозатор", "built_in": True, "sort_order": 20},
    {"key": "timed_infusion", "label": "Капельницы", "built_in": True, "sort_order": 30},
    {"key": "gas", "label": "Газ", "built_in": True, "sort_order": 40},
    {"key": "favorite", "label": "Избранное", "built_in": True, "sort_order": 50},
)
BUILTIN_OPERBLOCK_QUICK_ORDER_BUTTON_KEYS = {
    str(item["key"]) for item in BUILTIN_OPERBLOCK_QUICK_ORDER_BUTTONS
}


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
    fd, tmp_path = tempfile.mkstemp(prefix=".operblock_quick_order_buttons_", suffix=".json", dir=directory)
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


def _as_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _slug(value: Any) -> str:
    text = _as_text(value).casefold()
    text = re.sub(r"[^0-9a-zа-яё]+", "_", text, flags=re.IGNORECASE).strip("_")
    return text or "type"


def make_operblock_extra_quick_type_key(label: str, existing_keys: set[str] | None = None) -> str:
    existing = set(existing_keys or set())
    base = f"{OPERBLOCK_EXTRA_QUICK_TYPE_PREFIX}{_slug(label)}"
    key = base
    index = 2
    while key in existing or key in BUILTIN_OPERBLOCK_QUICK_ORDER_BUTTON_KEYS:
        key = f"{base}_{index}"
        index += 1
    return key


def _normalize_extra_key(value: Any) -> str:
    text = _as_text(value)
    if not text:
        return ""
    if text.startswith(OPERBLOCK_EXTRA_QUICK_TYPE_PREFIX):
        suffix = _slug(text[len(OPERBLOCK_EXTRA_QUICK_TYPE_PREFIX):])
    else:
        suffix = _slug(text)
    return f"{OPERBLOCK_EXTRA_QUICK_TYPE_PREFIX}{suffix}"


def _normalize_custom_button(raw: Mapping[str, Any], *, existing_keys: set[str], fallback_order: int) -> dict[str, Any] | None:
    label = _as_text(raw.get("label") or raw.get("name") or raw.get("title"))
    if not label:
        return None
    raw_key = _as_text(raw.get("key") or raw.get("id") or raw.get("code"))
    key = _normalize_extra_key(raw_key) if raw_key else make_operblock_extra_quick_type_key(label, existing_keys)
    if key in BUILTIN_OPERBLOCK_QUICK_ORDER_BUTTON_KEYS or key in existing_keys:
        key = make_operblock_extra_quick_type_key(label, existing_keys)
    return {
        "key": key,
        "label": label,
        "built_in": False,
        "sort_order": _as_int(raw.get("sort_order"), fallback_order),
    }


def normalize_operblock_quick_order_buttons_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        payload = {}
    raw_items = payload.get("items") if isinstance(payload.get("items"), list) else []
    raw_by_key: dict[str, Mapping[str, Any]] = {}
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            continue
        key = _as_text(raw_item.get("key") or raw_item.get("id") or raw_item.get("code"))
        if key:
            raw_by_key[key] = raw_item

    items: list[dict[str, Any]] = []
    existing_keys: set[str] = set()
    for builtin in BUILTIN_OPERBLOCK_QUICK_ORDER_BUTTONS:
        key = str(builtin["key"])
        overlay = raw_by_key.get(key) or {}
        label = _as_text(overlay.get("label") or overlay.get("name") or overlay.get("title")) or str(builtin["label"])
        items.append(
            {
                "key": key,
                "label": label,
                "built_in": True,
                "sort_order": _as_int(overlay.get("sort_order"), int(builtin["sort_order"])),
            }
        )
        existing_keys.add(key)

    fallback_order = 100
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            continue
        raw_key = _as_text(raw_item.get("key") or raw_item.get("id") or raw_item.get("code"))
        if raw_key in BUILTIN_OPERBLOCK_QUICK_ORDER_BUTTON_KEYS:
            continue
        item = _normalize_custom_button(raw_item, existing_keys=existing_keys, fallback_order=fallback_order)
        fallback_order += 10
        if item is None:
            continue
        existing_keys.add(item["key"])
        items.append(item)

    items.sort(key=lambda item: (_as_int(item.get("sort_order"), 99_999), _as_text(item.get("label")).casefold()))
    for index, item in enumerate(items, start=1):
        item["sort_order"] = index * 10
    return {"version": OPERBLOCK_QUICK_ORDER_BUTTONS_VERSION, "items": items}


def load_operblock_quick_order_buttons(*, user_dict_dir: str | None = None) -> list[dict[str, Any]]:
    if user_dict_dir is None:
        payload = get_settings_service().get_app_setting(
            OPERBLOCK_SETTINGS_SCOPE,
            OPERBLOCK_QUICK_ORDER_BUTTONS_APP_KEY,
            default={},
        )
        normalized = normalize_operblock_quick_order_buttons_payload(payload if isinstance(payload, Mapping) else {})
        return [deepcopy(item) for item in normalized["items"]]
    resolved_user_dir = user_dict_dir or USER_DICT_DIR
    overrides = _read_json_dict(os.path.join(resolved_user_dir, "user_overrides.json"))
    payload = overrides.get(OPERBLOCK_QUICK_ORDER_BUTTONS_OVERRIDE_KEY)
    normalized = normalize_operblock_quick_order_buttons_payload(payload if isinstance(payload, Mapping) else {})
    return [deepcopy(item) for item in normalized["items"]]


def save_operblock_quick_order_buttons(
    items: list[dict[str, Any]],
    *,
    user_dict_dir: str | None = None,
) -> list[dict[str, Any]]:
    payload = normalize_operblock_quick_order_buttons_payload(
        {"version": OPERBLOCK_QUICK_ORDER_BUTTONS_VERSION, "items": items}
    )
    if user_dict_dir is None:
        get_settings_service().set_app_setting(
            OPERBLOCK_SETTINGS_SCOPE,
            OPERBLOCK_QUICK_ORDER_BUTTONS_APP_KEY,
            payload,
            catalog_key=OPERBLOCK_SETTINGS_KEY,
            entity_type="operblock_quick_order_buttons",
            operation="replace",
            changed_by_role="doctor",
        )
        return [deepcopy(item) for item in payload["items"]]
    resolved_user_dir = user_dict_dir or USER_DICT_DIR
    overrides_path = os.path.join(resolved_user_dir, "user_overrides.json")
    overrides = _read_json_dict(overrides_path)
    overrides[OPERBLOCK_QUICK_ORDER_BUTTONS_OVERRIDE_KEY] = payload
    _write_json_atomic(overrides_path, overrides)
    return [deepcopy(item) for item in payload["items"]]


def operblock_extra_quick_type_buttons(buttons: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    source = buttons if buttons is not None else load_operblock_quick_order_buttons()
    return [dict(item or {}) for item in source if not bool((item or {}).get("built_in"))]


def load_operblock_extra_quick_type_buttons(*, user_dict_dir: str | None = None) -> list[dict[str, Any]]:
    return operblock_extra_quick_type_buttons(
        load_operblock_quick_order_buttons(user_dict_dir=user_dict_dir)
    )


def operblock_quick_order_button_label_map(buttons: list[dict[str, Any]] | None = None) -> dict[str, str]:
    source = buttons if buttons is not None else load_operblock_quick_order_buttons()
    result: dict[str, str] = {}
    for item in source or []:
        key = _as_text((item or {}).get("key"))
        label = _as_text((item or {}).get("label"))
        if key and label:
            result[key] = label
    return result


def normalize_operblock_extra_quick_type_keys(
    raw: Any,
    *,
    buttons: list[dict[str, Any]] | None = None,
    include_unknown: bool = True,
) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw_values = re.split(r"[;,]", raw)
    elif isinstance(raw, (list, tuple, set)):
        raw_values = list(raw)
    else:
        raw_values = []

    source_buttons = buttons if buttons is not None else load_operblock_quick_order_buttons()
    extra_buttons = operblock_extra_quick_type_buttons(source_buttons)
    allowed_keys = {_as_text(item.get("key")) for item in extra_buttons if _as_text(item.get("key"))}
    label_to_key = {
        _as_text(item.get("label")).casefold(): _as_text(item.get("key"))
        for item in extra_buttons
        if _as_text(item.get("label")) and _as_text(item.get("key"))
    }

    result: list[str] = []
    for value in raw_values:
        text = _as_text(value)
        if not text:
            continue
        key = text if text in allowed_keys else label_to_key.get(text.casefold(), "")
        if not key and include_unknown:
            normalized_key = _normalize_extra_key(text)
            if normalized_key.startswith(OPERBLOCK_EXTRA_QUICK_TYPE_PREFIX):
                key = normalized_key
        if key and key not in BUILTIN_OPERBLOCK_QUICK_ORDER_BUTTON_KEYS and key not in result:
            result.append(key)
    return result
