from __future__ import annotations

from pathlib import Path
import json
import logging
import os
import time
from typing import Any

from rem_card.app.runtime_paths import get_executable_dir
from rem_card.ui.styles.theme_presets import BUILTIN_PRESET_IDS, get_preset
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
logger = logging.getLogger(__name__)


def _is_theme_storage_source_path(path: Path) -> bool:
    return path.name == "theme_storage.py" and path.parent.name == "styles" and path.parent.parent.name == "ui"


def _resolve_dev_settings_base_dir(start_path: Path | None = None) -> str:
    current = (start_path or Path(__file__)).resolve()
    for candidate in current.parents:
        if (candidate / "ui" / "styles").is_dir() and (candidate / "app").is_dir():
            return str(candidate)

    if _is_theme_storage_source_path(current):
        fallback_root = current.parents[2]
        logger.warning(
            "Не найден marker-root цветовой схемы по ui/styles + app; используется fallback от theme_storage.py: %s",
            fallback_root,
        )
        return str(fallback_root)

    raise RuntimeError(
        "Не удалось определить dev-root для настроек цветовой схемы: "
        "не найден marker-root с ui/styles и app."
    )


def get_style_settings_path() -> str:
    override = os.environ.get(STYLE_SETTINGS_ENV)
    if override:
        return os.path.abspath(os.path.normpath(override))
    try:
        from rem_card.app.runtime_paths import is_compiled
        compiled = is_compiled()
    except Exception:
        logger.warning("Не удалось определить режим запуска для настроек цветовой схемы.", exc_info=True)
        compiled = False
    base_dir = get_executable_dir() if compiled else _resolve_dev_settings_base_dir()
    return os.path.join(base_dir, STYLE_SETTINGS_RELATIVE_PATH)


class ThemeStorage:
    def __init__(self, path: str | None = None):
        self._file_mode = path is not None or bool(os.environ.get(STYLE_SETTINGS_ENV))
        self.path = os.path.abspath(path or get_style_settings_path())
        self.last_error: str | None = None

    def load(self) -> dict[str, Any]:
        self.last_error = None
        if not self._file_mode:
            try:
                from rem_card.services.settings.settings_service import get_settings_service

                payload = get_settings_service().get_app_setting("shared", "style_settings", default=None)
                if isinstance(payload, dict):
                    return self._normalize_payload(payload)
                return self._default_and_save()
            except Exception as exc:
                self.last_error = str(exc)
                raise
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
        if not self._file_mode:
            from rem_card.services.settings.settings_service import STYLE_SETTINGS_KEY, get_settings_service

            get_settings_service().set_app_setting(
                "shared",
                "style_settings",
                normalized,
                catalog_key=STYLE_SETTINGS_KEY,
                entity_type="style_settings",
                operation="update",
            )
            return
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
        result["version"] = STYLE_SETTINGS_VERSION
        result["custom_presets"] = self._normalize_custom_presets(payload.get("custom_presets"))
        active = payload.get("active")
        if not isinstance(active, dict):
            return result

        available_preset_ids = set(BUILTIN_PRESET_IDS) | set(result["custom_presets"])
        for role in ("doctor", "nurse"):
            role_data = active.get(role)
            result["active"][role] = self._normalize_role_settings(
                role_data,
                available_preset_ids=available_preset_ids,
                custom_presets=result["custom_presets"],
            )
        return result

    def _normalize_custom_presets(self, data: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(data, dict):
            return {}

        normalized: dict[str, dict[str, Any]] = {}
        for raw_id, raw_data in data.items():
            if not isinstance(raw_data, dict):
                continue
            preset_id = str(raw_data.get("id") or raw_id or "").strip()
            if not preset_id.startswith("custom_"):
                continue
            name = str(raw_data.get("name") or "").strip()
            if not name:
                continue

            base_preset_id = str(raw_data.get("base_preset_id") or DEFAULT_PRESET_ID)
            if base_preset_id not in BUILTIN_PRESET_IDS:
                base_preset_id = DEFAULT_PRESET_ID
            base_preset = get_preset(base_preset_id)

            mode = normalize_mode(raw_data.get("mode") or base_preset.default_mode or DEFAULT_MODE)
            density = str(raw_data.get("density") or base_preset.density or "normal")
            overrides = raw_data.get("overrides")
            if not isinstance(overrides, dict):
                overrides = {}

            normalized[preset_id] = {
                "id": preset_id,
                "name": name,
                "description": str(raw_data.get("description") or "Пользовательская тема."),
                "base_preset_id": base_preset_id,
                "mode": mode,
                "density": density,
                "overrides": dict(overrides),
            }
        return normalized

    def _normalize_role_settings(
        self,
        data: Any,
        *,
        available_preset_ids: set[str] | None = None,
        custom_presets: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        normalized = default_role_settings()
        if not isinstance(data, dict):
            return normalized

        preset_id = str(data.get("preset_id") or DEFAULT_PRESET_ID)
        available = available_preset_ids or set(BUILTIN_PRESET_IDS)
        custom_presets = custom_presets or {}
        if preset_id not in available:
            preset_id = DEFAULT_PRESET_ID

        custom = custom_presets.get(preset_id)
        if custom:
            mode = normalize_mode(custom.get("mode") or DEFAULT_MODE)
            density = str(custom.get("density") or "normal")
        else:
            preset = get_preset(preset_id)
            mode = normalize_mode(preset.default_mode or DEFAULT_MODE)
            density = str(preset.density or "normal")

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
