from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import json
import os
import time
from typing import Any

from rem_card.ui.styles.theme_storage import get_style_settings_path


DISPLAY_SETTINGS_ENV = "REMCARD_DISPLAY_SETTINGS_PATH"
DISPLAY_SETTINGS_VERSION = 1
DISPLAY_SETTINGS_RELATIVE_PATH = os.path.join("settings", "display_settings", "display_settings.json")


SECTOR8_BUTTONS: dict[str, tuple[dict[str, Any], ...]] = {
    "doctor": (
        {"id": "archive", "label": "Архив", "default_visible": True, "can_hide": True},
        {"id": "refresh", "label": "Обновить", "default_visible": True, "can_hide": True},
        {"id": "add_patient", "label": "Добавить пациента", "default_visible": True, "can_hide": True},
        {"id": "bars", "label": "БАРС", "default_visible": True, "can_hide": True},
        {"id": "calc", "label": "Калькулятор", "default_visible": True, "can_hide": True},
        {"id": "bonus", "label": "Бонус", "default_visible": True, "can_hide": True},
        {"id": "settings", "label": "Настройки", "default_visible": True, "can_hide": False},
        {"id": "back", "label": "Назад", "default_visible": True, "can_hide": True},
        {"id": "exit", "label": "Выход", "default_visible": True, "can_hide": True},
    ),
    "nurse": (
        {"id": "refresh", "label": "Обновить", "default_visible": True, "can_hide": True},
        {"id": "add_patient", "label": "Добавить пациента", "default_visible": True, "can_hide": True},
        {"id": "bonus", "label": "Бонус", "default_visible": True, "can_hide": True},
        {"id": "style", "label": "Цветовая схема", "default_visible": False, "can_hide": True},
        {"id": "back", "label": "Назад", "default_visible": True, "can_hide": True},
        {"id": "exit", "label": "Выход", "default_visible": True, "can_hide": True},
    ),
}


REMCARD_TABS: dict[str, tuple[dict[str, Any], ...]] = {
    "doctor": (
        {"id": "vitals", "label": "Витальные функции", "default_visible": True},
        {"id": "orders", "label": "Назначения", "default_visible": True},
        {"id": "balance", "label": "Баланс жидкости", "default_visible": True},
        {"id": "events", "label": "Движение", "default_visible": True},
        {"id": "ivl", "label": "ИВЛ", "default_visible": True},
        {"id": "procedures", "label": "Процедуры", "default_visible": True},
        {"id": "labs", "label": "Анализы", "default_visible": True},
        {"id": "print", "label": "Печать", "default_visible": False},
    ),
    "nurse": (
        {"id": "vitals", "label": "Витальные функции", "default_visible": True},
        {"id": "orders", "label": "Назначения", "default_visible": True},
        {"id": "balance", "label": "Баланс жидкости", "default_visible": True},
        {"id": "events", "label": "Движение", "default_visible": True},
        {"id": "procedures", "label": "Процедуры", "default_visible": True},
        {"id": "labs", "label": "Анализы", "default_visible": True},
        {"id": "print", "label": "Печать", "default_visible": False},
    ),
}


ROLE_ALIASES = {
    "doctor": "doctor",
    "врач": "doctor",
    "nurse": "nurse",
    "медсестра": "nurse",
    "медицинская сестра": "nurse",
}


def normalize_display_role(role: str | None) -> str:
    value = str(role or "doctor").strip().lower()
    return ROLE_ALIASES.get(value, "doctor")


def get_display_settings_path() -> str:
    override = os.environ.get(DISPLAY_SETTINGS_ENV)
    if override:
        return os.path.abspath(os.path.normpath(override))

    style_path = Path(os.path.abspath(get_style_settings_path()))
    if style_path.parent.name == "color_scheme":
        settings_root = style_path.parent.parent
    else:
        settings_root = style_path.parent
    return str(settings_root / "display_settings" / "display_settings.json")


def _option_ids(options: tuple[dict[str, Any], ...]) -> list[str]:
    return [str(option["id"]) for option in options]


def _default_section(options: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    return {
        "order": _option_ids(options),
        "visible": {
            str(option["id"]): bool(option.get("default_visible", True))
            for option in options
        },
    }


def default_role_display_settings(role: str | None) -> dict[str, Any]:
    role_key = normalize_display_role(role)
    return {
        "sector8_buttons": _default_section(SECTOR8_BUTTONS[role_key]),
        "remcard_tabs": _default_section(REMCARD_TABS[role_key]),
    }


def default_display_settings_payload() -> dict[str, Any]:
    return {
        "version": DISPLAY_SETTINGS_VERSION,
        "active": {
            "doctor": default_role_display_settings("doctor"),
            "nurse": default_role_display_settings("nurse"),
        },
    }


def _normalize_order(raw_order: Any, ids: list[str]) -> list[str]:
    result: list[str] = []
    if isinstance(raw_order, list):
        for raw_id in raw_order:
            item_id = str(raw_id)
            if item_id in ids and item_id not in result:
                result.append(item_id)
    for item_id in ids:
        if item_id not in result:
            result.append(item_id)
    return result


def _normalize_section(
    data: Any,
    options: tuple[dict[str, Any], ...],
    *,
    require_one_visible: bool = False,
) -> dict[str, Any]:
    default = _default_section(options)
    if not isinstance(data, dict):
        data = {}

    ids = _option_ids(options)
    order = _normalize_order(data.get("order"), ids)
    raw_visible = data.get("visible")
    if not isinstance(raw_visible, dict):
        raw_visible = {}

    visible: dict[str, bool] = {}
    for option in options:
        item_id = str(option["id"])
        can_hide = bool(option.get("can_hide", True))
        default_visible = bool(default["visible"].get(item_id, True))
        item_visible = bool(raw_visible.get(item_id, default_visible))
        visible[item_id] = item_visible if can_hide else True

    if require_one_visible and not any(visible.values()) and order:
        visible[order[0]] = True

    return {"order": order, "visible": visible}


def normalize_role_display_settings(role: str | None, data: Any) -> dict[str, Any]:
    role_key = normalize_display_role(role)
    if not isinstance(data, dict):
        data = {}
    return {
        "sector8_buttons": _normalize_section(data.get("sector8_buttons"), SECTOR8_BUTTONS[role_key]),
        "remcard_tabs": _normalize_section(
            data.get("remcard_tabs"),
            REMCARD_TABS[role_key],
            require_one_visible=True,
        ),
    }


def role_display_settings_from_payload(payload: dict[str, Any], role: str | None) -> dict[str, Any]:
    role_key = normalize_display_role(role)
    active = payload.get("active") if isinstance(payload, dict) else None
    role_data = active.get(role_key) if isinstance(active, dict) else None
    return normalize_role_display_settings(role_key, role_data)


def sector8_button_options(role: str | None) -> list[dict[str, Any]]:
    return deepcopy(list(SECTOR8_BUTTONS[normalize_display_role(role)]))


def remcard_tab_options(role: str | None) -> list[dict[str, Any]]:
    return deepcopy(list(REMCARD_TABS[normalize_display_role(role)]))


def ordered_visible_ids(section: dict[str, Any]) -> list[str]:
    order = section.get("order") if isinstance(section, dict) else []
    visible = section.get("visible") if isinstance(section, dict) else {}
    if not isinstance(order, list) or not isinstance(visible, dict):
        return []
    return [str(item_id) for item_id in order if bool(visible.get(str(item_id), False))]


class DisplaySettingsStorage:
    def __init__(self, path: str | None = None):
        self.path = os.path.abspath(path or get_display_settings_path())
        self.last_error: str | None = None

    def load(self) -> dict[str, Any]:
        self.last_error = None
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except FileNotFoundError:
            return self._default_and_save()
        except Exception as exc:
            self.last_error = str(exc)
            self._quarantine_broken_file()
            return self._default_and_save()

        if not isinstance(payload, dict):
            self.last_error = "Корневой объект настроек отображения не является JSON-объектом."
            self._quarantine_broken_file()
            return self._default_and_save()

        return self._normalize_payload(payload)

    def save(self, payload: dict[str, Any]) -> None:
        normalized = self._normalize_payload(payload)
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(normalized, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, self.path)

    def save_role_settings(self, role: str | None, settings: dict[str, Any]) -> None:
        role_key = normalize_display_role(role)
        payload = self.load()
        payload.setdefault("active", {})
        payload["active"][role_key] = normalize_role_display_settings(role_key, settings)
        self.save(payload)

    def _normalize_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = default_display_settings_payload()
        result["version"] = DISPLAY_SETTINGS_VERSION
        active = payload.get("active")
        if not isinstance(active, dict):
            return result
        for role in ("doctor", "nurse"):
            result["active"][role] = normalize_role_display_settings(role, active.get(role))
        return result

    def _default_and_save(self) -> dict[str, Any]:
        payload = default_display_settings_payload()
        try:
            self.save(payload)
        except Exception as exc:
            self.last_error = str(exc)
        return payload

    def _quarantine_broken_file(self) -> None:
        if not os.path.exists(self.path):
            return
        stamp = time.strftime("%Y%m%d_%H%M%S")
        broken_path = f"{self.path}.{stamp}.broken"
        try:
            os.replace(self.path, broken_path)
        except Exception:
            pass
