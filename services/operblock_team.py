from __future__ import annotations

import json
import os
import re
import tempfile
from typing import Any, Mapping

from rem_card.app.paths import USER_DICT_DIR
from rem_card.services.doctor_list_service import DoctorListStore


OPERBLOCK_TEAM_OVERRIDE_KEY = "operblock_team"
OPERBLOCK_TEAM_VERSION = 1
OPERBLOCK_TEAM_DEFAULT_POSITIONS = (
    "Хирург",
    "Гинеколог",
    "Лор",
    "Травматолог",
    "Операционная медсестра",
    "Анестезистка",
)


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
    fd, tmp_path = tempfile.mkstemp(prefix=".operblock_team_", suffix=".json", dir=directory)
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


def normalize_operblock_team_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _slug(value: Any) -> str:
    text = normalize_operblock_team_text(value).casefold()
    text = re.sub(r"[^0-9a-zа-яё]+", "_", text, flags=re.IGNORECASE).strip("_")
    return text or "member"


def _normalize_item(raw: Any, index: int, used_ids: set[str], used_keys: set[tuple[str, str]]) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    name = normalize_operblock_team_text(raw.get("name") or raw.get("full_name") or raw.get("doctor") or raw.get("label"))
    position = normalize_operblock_team_text(raw.get("position") or raw.get("role") or raw.get("role_label"))
    if not name or not position:
        return None
    key = (name.casefold(), position.casefold())
    if key in used_keys:
        return None
    used_keys.add(key)

    raw_id = str(raw.get("id") or "").strip()
    item_id = re.sub(r"[^0-9A-Za-zа-яА-ЯёЁ_-]+", "_", raw_id).strip("_") if raw_id else ""
    if not item_id:
        item_id = f"member_{_slug(name)}_{_slug(position)}"
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
        "name": name,
        "position": position,
        "sort_order": sort_order,
    }


def normalize_operblock_team_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, list):
        raw_items = payload
    elif isinstance(payload, Mapping):
        raw_items = payload.get("items") if isinstance(payload.get("items"), list) else []
    else:
        raw_items = []

    items: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    used_keys: set[tuple[str, str]] = set()
    for index, raw_item in enumerate(raw_items, start=1):
        item = _normalize_item(raw_item, index, used_ids, used_keys)
        if item:
            items.append(item)
    items.sort(
        key=lambda item: (
            int(item.get("sort_order") or 0),
            str(item.get("position") or "").casefold(),
            str(item.get("name") or "").casefold(),
        )
    )
    for index, item in enumerate(items, start=1):
        item["sort_order"] = index * 10
    return {"version": OPERBLOCK_TEAM_VERSION, "items": items}


def load_operblock_team(*, user_dict_dir: str | None = None) -> list[dict[str, Any]]:
    resolved_user_dir = user_dict_dir or USER_DICT_DIR
    overrides_path = os.path.join(resolved_user_dir, "user_overrides.json")
    overrides = _read_json_dict(overrides_path)
    payload = normalize_operblock_team_payload(overrides.get(OPERBLOCK_TEAM_OVERRIDE_KEY))
    return list(payload["items"])


def save_operblock_team(
    items: list[dict[str, Any]],
    *,
    user_dict_dir: str | None = None,
) -> list[dict[str, Any]]:
    payload = normalize_operblock_team_payload({"version": OPERBLOCK_TEAM_VERSION, "items": items})
    resolved_user_dir = user_dict_dir or USER_DICT_DIR
    overrides_path = os.path.join(resolved_user_dir, "user_overrides.json")
    overrides = _read_json_dict(overrides_path)
    overrides[OPERBLOCK_TEAM_OVERRIDE_KEY] = payload
    _write_json_atomic(overrides_path, overrides)
    return list(payload["items"])


def _load_operblock_team_names_by_position(
    matcher,
    *,
    user_dict_dir: str | None = None,
) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in load_operblock_team(user_dict_dir=user_dict_dir):
        position = normalize_operblock_team_text((item or {}).get("position")).casefold()
        if not matcher(position):
            continue
        name = normalize_operblock_team_text((item or {}).get("name"))
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        result.append(name)
    return result


def load_operblock_anesthesiologists() -> list[str]:
    return DoctorListStore().load_doctors()


def load_operblock_anesthetists(*, user_dict_dir: str | None = None) -> list[str]:
    return _load_operblock_team_names_by_position(
        lambda position: "анестезист" in position,
        user_dict_dir=user_dict_dir,
    )


def load_operblock_surgeons(*, user_dict_dir: str | None = None) -> list[str]:
    surgical_markers = (
        "хирург",
        "травматолог",
        "ортопед",
        "гинеколог",
        "акушер",
        "лор",
        "оториноларинголог",
        "уролог",
        "нейрохирург",
        "колопроктолог",
        "проктолог",
        "сосудист",
        "эндоскопист",
        "офтальмолог",
        "челюстно",
        "стоматолог",
        "онколог",
    )
    return _load_operblock_team_names_by_position(
        lambda position: any(marker in position for marker in surgical_markers),
        user_dict_dir=user_dict_dir,
    )


def load_operblock_operating_nurses(*, user_dict_dir: str | None = None) -> list[str]:
    return _load_operblock_team_names_by_position(
        lambda position: "операцион" in position and "медсестр" in position,
        user_dict_dir=user_dict_dir,
    )

