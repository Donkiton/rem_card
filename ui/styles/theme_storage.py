from __future__ import annotations

from pathlib import Path
import json
import os
import time
from typing import Any

from rem_card.app.runtime_paths import get_executable_dir
from rem_card.ui.styles.theme_presets import get_preset
from rem_card.ui.styles.theme_tokens import (
    DEFAULT_MODE,
    DEFAULT_PRESET_ID,
    STYLE_SETTINGS_VERSION,
    default_role_settings,
    default_settings_payload,
    normalize_mode,
    normalize_role,
)


STYLE_SETTINGS_ENV = "REMCARD_STYLE_SETTINGS_PATH"
STYLE_SETTINGS_RELATIVE_PATH = os.path.join("settings", "color_scheme", "style_settings.json")


def _resolve_dev_settings_base_dir(start_path: Path | None = None) -> str:
    current = (start_path or Path(__file__)).resolve()
    for candidate in current.parents:
        if (candidate / "ui" / "styles").is_dir() and (candidate / "app").is_dir():
            return str(candidate)
    try:
        return str(current.parents[2])
    except IndexError:
        return os.getcwd()


def get_style_settings_path() -> str:
    override = os.environ.get(STYLE_SETTINGS_ENV)
    if override:
        return os.path.abspath(os.path.normpath(override))
    try:
        from rem_card.app.runtime_paths import is_compiled

        base_dir = get_executable_dir() if is_compiled() else _resolve_dev_settings_base_dir()
    except Exception:
        base_dir = _resolve_dev_settings_base_dir()
    return os.path.join(base_dir, STYLE_SETTINGS_RELATIVE_PATH)


class ThemeStorage:
    def __init__(self, path: str | None = None):
        self.path = os.path.abspath(path or get_style_settings_path())
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
            self.last_error = "Корневой объект настроек стиля не является JSON-объектом."
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

    def _quarantine_broken_file(self) -> None:
        if not os.path.exists(self.path):
            return
        stamp = time.strftime("%Y%m%d_%H%M%S")
        broken_path = f"{self.path}.{stamp}.broken"
        try:
            os.replace(self.path, broken_path)
        except Exception:
            pass

    def _default_and_save(self) -> dict[str, Any]:
        payload = default_settings_payload()
        try:
            self.save(payload)
        except Exception as exc:
            self.last_error = str(exc)
        return payload

    def _normalize_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = default_settings_payload()
        result["version"] = int(payload.get("version") or STYLE_SETTINGS_VERSION)
        active = payload.get("active")
        if not isinstance(active, dict):
            return result

        for role in ("doctor", "nurse"):
            role_data = active.get(role)
            result["active"][role] = self._normalize_role_settings(role_data)
        return result

    def _normalize_role_settings(self, data: Any) -> dict[str, Any]:
        normalized = default_role_settings()
        if not isinstance(data, dict):
            return normalized

        preset_id = str(data.get("preset_id") or DEFAULT_PRESET_ID)
        preset = get_preset(preset_id)
        if preset.id != preset_id:
            preset_id = DEFAULT_PRESET_ID

        mode = normalize_mode(data.get("mode") or preset.default_mode or DEFAULT_MODE)
        density = str(data.get("density") or preset.density or "normal")
        overrides = data.get("overrides")
        if not isinstance(overrides, dict):
            overrides = {}

        normalized.update(
            {
                "preset_id": preset_id,
                "mode": mode,
                "density": density,
                "overrides": overrides,
            }
        )
        return normalized


def role_settings_from_payload(payload: dict[str, Any], role: str | None) -> dict[str, Any]:
    active = payload.get("active") if isinstance(payload, dict) else None
    if not isinstance(active, dict):
        return default_role_settings()
    normalized_role = normalize_role(role)
    if normalized_role == "system":
        return default_role_settings()
    value = active.get(normalized_role)
    return value if isinstance(value, dict) else default_role_settings()
