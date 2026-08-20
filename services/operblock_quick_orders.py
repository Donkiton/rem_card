from __future__ import annotations

from copy import deepcopy
import json
import os
import tempfile
from typing import Any, Mapping

from rem_card.app.paths import SEED_DIR, USER_DICT_DIR
from rem_card.services.settings.settings_service import (
    OPERBLOCK_QUICK_ORDERS_APP_KEY,
    OPERBLOCK_SETTINGS_KEY,
    OPERBLOCK_SETTINGS_SCOPE,
    get_settings_service,
)


OPERBLOCK_QUICK_ORDERS_FILE = "operblock_quick_orders.seed.json"
OPERBLOCK_QUICK_ORDERS_OVERRIDE_KEY = "operblock_quick_orders"
OPERBLOCK_QUICK_ORDERS_VERSION = 1
MAX_QUICK_ORDER_DOSES = 4
MAX_QUICK_ORDER_GROUP = 4
OPERBLOCK_QUICK_ORDER_KINDS = {"bolus", "infusion", "gas"}

DEFAULT_OPERBLOCK_QUICK_ORDERS: dict[str, Any] = {
    "version": OPERBLOCK_QUICK_ORDERS_VERSION,
    "items": [
        {
            "drug_name": "S. Ketamini 5%",
            "group": 1,
            "kind": "bolus",
            "doses": ["0,25 мл", "0,5 мл", "1 мл", "2 мл"],
        },
        {
            "drug_name": "Noradrenalini",
            "label": "Noradrenalini",
            "group": 2,
            "kind": "infusion",
            "rates": ["1 мл/час", "2 мл/час", "5 мл/час", "10 мл/час"],
            "concentration": "4 мг / 50 мл",
        }
    ],
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
    fd, tmp_path = tempfile.mkstemp(prefix=".operblock_quick_orders_", suffix=".json", dir=directory)
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


def _normalize_doses(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    doses: list[str] = []
    for value in raw:
        text = str(value or "").strip()
        if text:
            doses.append(text)
        if len(doses) >= MAX_QUICK_ORDER_DOSES:
            break
    return doses


def _normalize_rates(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    rates: list[str] = []
    for value in raw:
        text = str(value or "").strip()
        if text:
            rates.append(text)
        if len(rates) >= MAX_QUICK_ORDER_DOSES:
            break
    return rates


def normalize_operblock_quick_order_kind(raw: Any) -> str:
    kind = str(raw or "bolus").strip().casefold()
    kind = {
        "continuous": "infusion",
        "continuous_infusion": "infusion",
        "timed_infusion": "infusion",
        "дозатор": "infusion",
        "капельница": "infusion",
        "gas": "gas",
        "газ": "gas",
        "газы": "gas",
        "ингаляция": "gas",
        "ингаляции": "gas",
        "inhalation": "gas",
    }.get(kind, kind)
    return kind if kind in OPERBLOCK_QUICK_ORDER_KINDS else "bolus"


def normalize_operblock_quick_order_group(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 1
    return max(1, min(MAX_QUICK_ORDER_GROUP, value))


def _normalize_item(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    drug_name = str(raw.get("drug_name") or raw.get("drug") or raw.get("label") or "").strip()
    if not drug_name:
        return None
    item = dict(raw)
    item.update(
        {
            "drug_name": drug_name,
            "label": str(raw.get("label") or drug_name).strip() or drug_name,
            "group": normalize_operblock_quick_order_group(raw.get("group")),
            "kind": normalize_operblock_quick_order_kind(raw.get("kind")),
            "doses": _normalize_doses(raw.get("doses")),
            "rates": _normalize_rates(raw.get("rates")),
        }
    )
    return item


def normalize_operblock_quick_orders_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {}
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_item in payload.get("items") or []:
        item = _normalize_item(raw_item)
        if not item:
            continue
        key = item["drug_name"].casefold()
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
    return {"version": OPERBLOCK_QUICK_ORDERS_VERSION, "items": items}


def load_operblock_quick_orders(
    *,
    seed_dir: str | None = None,
    user_dict_dir: str | None = None,
) -> list[dict[str, Any]]:
    if seed_dir is None and user_dict_dir is None:
        payload = get_settings_service().get_app_setting(
            OPERBLOCK_SETTINGS_SCOPE,
            OPERBLOCK_QUICK_ORDERS_APP_KEY,
            default={},
        )
        if isinstance(payload, Mapping):
            normalized = normalize_operblock_quick_orders_payload(payload)
            return list(normalized["items"])
        return list(normalize_operblock_quick_orders_payload(DEFAULT_OPERBLOCK_QUICK_ORDERS)["items"])

    resolved_seed_dir = seed_dir or SEED_DIR
    resolved_user_dir = user_dict_dir or USER_DICT_DIR
    seed_path = os.path.join(resolved_seed_dir, OPERBLOCK_QUICK_ORDERS_FILE)
    seed_payload = _read_json_dict(seed_path) or deepcopy(DEFAULT_OPERBLOCK_QUICK_ORDERS)
    payload = normalize_operblock_quick_orders_payload(seed_payload)

    overrides_path = os.path.join(resolved_user_dir, "user_overrides.json")
    overrides = _read_json_dict(overrides_path)
    override_payload = overrides.get(OPERBLOCK_QUICK_ORDERS_OVERRIDE_KEY)
    if isinstance(override_payload, dict):
        payload = normalize_operblock_quick_orders_payload(override_payload)

    return list(payload["items"])


def save_operblock_quick_orders(
    items: list[dict[str, Any]],
    *,
    user_dict_dir: str | None = None,
) -> list[dict[str, Any]]:
    payload = normalize_operblock_quick_orders_payload(
        {"version": OPERBLOCK_QUICK_ORDERS_VERSION, "items": items}
    )
    if user_dict_dir is None:
        get_settings_service().set_app_setting(
            OPERBLOCK_SETTINGS_SCOPE,
            OPERBLOCK_QUICK_ORDERS_APP_KEY,
            payload,
            catalog_key=OPERBLOCK_SETTINGS_KEY,
            entity_type="operblock_quick_orders",
            operation="replace",
            changed_by_role="doctor",
        )
        return list(payload["items"])
    resolved_user_dir = user_dict_dir or USER_DICT_DIR
    overrides_path = os.path.join(resolved_user_dir, "user_overrides.json")
    overrides = _read_json_dict(overrides_path)
    overrides[OPERBLOCK_QUICK_ORDERS_OVERRIDE_KEY] = payload
    _write_json_atomic(overrides_path, overrides)
    return list(payload["items"])


def build_operblock_quick_order_text(drug_name: str, dose: str | None = None) -> str:
    clean_drug = str(drug_name or "").strip()
    clean_dose = str(dose or "").strip()
    if clean_dose:
        return f"{clean_drug} - {clean_dose}"
    return clean_drug

