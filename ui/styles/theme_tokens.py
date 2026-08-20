from __future__ import annotations

from copy import deepcopy
from typing import Any


STYLE_SETTINGS_VERSION = 2
DEFAULT_PRESET_ID = "remcard_light"
DEFAULT_MODE = "light"
DEFAULT_DENSITY = "normal"
VALID_ROLES = ("doctor", "nurse", "system")
VALID_MODES = ("light", "dark")


def normalize_role(role: str | None) -> str:
    value = str(role or "system").strip().lower()
    return value if value in VALID_ROLES else "system"


def normalize_mode(mode: str | None) -> str:
    value = str(mode or DEFAULT_MODE).strip().lower()
    return value if value in VALID_MODES else DEFAULT_MODE


def default_role_settings() -> dict[str, Any]:
    return {
        "preset_id": DEFAULT_PRESET_ID,
        "mode": DEFAULT_MODE,
        "density": DEFAULT_DENSITY,
        "overrides": {},
    }


def default_settings_payload() -> dict[str, Any]:
    return {
        "version": STYLE_SETTINGS_VERSION,
        "custom_presets": {},
        "active": {
            "doctor": default_role_settings(),
            "nurse": default_role_settings(),
        },
    }


def merge_tokens(*sources: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for source in sources:
        for key, value in (source or {}).items():
            result[str(key)] = value
    return result


def copy_tokens(tokens: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(tokens)


def token(tokens: dict[str, Any], key: str, default: str = "") -> str:
    value = tokens.get(key, default)
    return str(value if value is not None else default)
