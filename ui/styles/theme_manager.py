from __future__ import annotations

import os
from copy import deepcopy
from typing import Any
from uuid import uuid4

from PySide6.QtWidgets import QApplication, QWidget

from rem_card.ui.styles.qss_builder import build_global_style
from rem_card.ui.styles.theme_presets import build_tokens, get_preset, list_presets
from rem_card.ui.styles.theme_storage import ThemeStorage, role_settings_from_payload
from rem_card.ui.styles.theme_tokens import default_role_settings, default_settings_payload, normalize_mode, normalize_role
from rem_card.ui.styles.tooltip_style import apply_tooltip_palette


class ThemeManager:
    def __init__(self, storage: ThemeStorage | None = None):
        self.storage = storage or ThemeStorage()
        self._payload = self.storage.load()
        self._active_role = normalize_role(os.environ.get("REMCARD_UI_ROLE"))
        self._tokens_cache: dict[str, dict[str, Any]] = {}

    @property
    def active_role(self) -> str:
        return self._active_role

    @property
    def settings_path(self) -> str:
        return self.storage.path

    def load(self, role: str | None = None) -> dict[str, Any]:
        self._payload = self.storage.load()
        self._tokens_cache.clear()
        if role:
            self._active_role = normalize_role(role)
            os.environ["REMCARD_UI_ROLE"] = self._active_role
        return self._payload

    def settings_for_role(self, role: str | None = None) -> dict[str, Any]:
        return dict(role_settings_from_payload(self._payload, role or self._active_role))

    def custom_presets(self) -> dict[str, dict[str, Any]]:
        value = self._payload.get("custom_presets") if isinstance(self._payload, dict) else None
        return deepcopy(value) if isinstance(value, dict) else {}

    def theme_options(self) -> list[dict[str, Any]]:
        options: list[dict[str, Any]] = []
        for preset in list_presets():
            options.append(
                {
                    "id": preset.id,
                    "name": preset.name,
                    "description": preset.description,
                    "base_preset_id": preset.id,
                    "mode": preset.default_mode,
                    "density": preset.density,
                    "is_custom": False,
                    "deletable": False,
                }
            )

        for custom in self.custom_presets().values():
            base_preset = get_preset(custom.get("base_preset_id"))
            options.append(
                {
                    "id": str(custom.get("id") or ""),
                    "name": str(custom.get("name") or "Пользовательская тема"),
                    "description": str(custom.get("description") or "Пользовательская тема."),
                    "base_preset_id": base_preset.id,
                    "mode": normalize_mode(custom.get("mode") or base_preset.default_mode),
                    "density": str(custom.get("density") or base_preset.density or "normal"),
                    "is_custom": True,
                    "deletable": True,
                }
            )
        return options

    def theme_option(self, preset_id: str | None) -> dict[str, Any]:
        requested = str(preset_id or "")
        custom = self.custom_presets().get(requested)
        if custom:
            base_preset = get_preset(custom.get("base_preset_id"))
            return {
                "id": str(custom.get("id") or requested),
                "name": str(custom.get("name") or "Пользовательская тема"),
                "description": str(custom.get("description") or "Пользовательская тема."),
                "base_preset_id": base_preset.id,
                "mode": normalize_mode(custom.get("mode") or base_preset.default_mode),
                "density": str(custom.get("density") or base_preset.density or "normal"),
                "is_custom": True,
                "deletable": True,
            }

        preset = get_preset(requested)
        return {
            "id": preset.id,
            "name": preset.name,
            "description": preset.description,
            "base_preset_id": preset.id,
            "mode": preset.default_mode,
            "density": preset.density,
            "is_custom": False,
            "deletable": False,
        }

    def tokens_for_role(self, role: str | None = None, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        normalized_role = normalize_role(role or self._active_role)
        if settings is None and normalized_role in self._tokens_cache:
            return dict(self._tokens_cache[normalized_role])

        role_settings = settings or self.settings_for_role(normalized_role)
        tokens = self.preview_tokens(role_settings.get("preset_id"), role_settings.get("mode"), role_settings.get("overrides") or {})
        if settings is None:
            self._tokens_cache[normalized_role] = dict(tokens)
        return tokens

    def current_tokens(self) -> dict[str, Any]:
        return self.tokens_for_role(self._active_role)

    def build_qss(self, role: str | None = None) -> str:
        return build_global_style(self.tokens_for_role(role))

    def apply_to_app(self, app: QApplication | None = None, role: str | None = None) -> None:
        if role:
            self._active_role = normalize_role(role)
            os.environ["REMCARD_UI_ROLE"] = self._active_role
        target_app = app or QApplication.instance()
        if target_app is not None:
            tokens = self.tokens_for_role(self._active_role)
            target_app.setStyleSheet(build_global_style(tokens))
            apply_tooltip_palette(target_app)
            from rem_card.ui.styles.context_menu_style import install_global_text_edit_context_menus

            install_global_text_edit_context_menus(target_app)
            self._apply_runtime_container_styles(target_app, tokens)

    def _apply_runtime_container_styles(self, app: QApplication, tokens: dict[str, Any]) -> None:
        surface = str(tokens.get("surface.window") or "#f8f9fa")
        border = str(tokens.get("border.default") or "#bdc3c7")
        radius = str(tokens.get("radius.dialog") or "5px")

        for top_widget in app.topLevelWidgets():
            containers = []
            main_container = getattr(top_widget, "main_container", None)
            if main_container is not None:
                containers.append(main_container)
            try:
                containers.extend(top_widget.findChildren(QWidget))
            except Exception:
                continue

            for widget in containers:
                try:
                    object_name = widget.objectName()
                except Exception:
                    continue
                if object_name == "MainFrame":
                    is_main_window_frame = widget is main_container
                    is_maximized = bool(getattr(top_widget, "_is_custom_maximized", False)) if is_main_window_frame else False
                    widget.setStyleSheet(
                        f"""
                        QFrame#MainFrame {{
                            background-color: {surface} !important;
                            border: {'none' if is_maximized else f'1px solid {border}'};
                            border-radius: {'0px' if is_maximized else radius};
                        }}
                        """
                    )
                elif object_name in {"bg_container", "container", "patient_bed_root"}:
                    widget.setStyleSheet(
                        f"""
                        QWidget#{object_name} {{
                            background-color: {surface} !important;
                            border: 1px solid {border};
                            border-radius: {radius};
                        }}
                        """
                    )

    def preview_tokens(self, preset_id: str, mode: str | None = None, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        option = self.theme_option(preset_id)
        selected_mode = normalize_mode(mode or option["mode"])
        if option["is_custom"]:
            custom = self.custom_presets().get(option["id"], {})
            combined_overrides: dict[str, Any] = {}
            combined_overrides.update(custom.get("overrides") or {})
            combined_overrides.update(overrides or {})
            tokens = build_tokens(option["base_preset_id"], selected_mode, combined_overrides)
            tokens.update(
                {
                    "meta.preset_id": option["id"],
                    "meta.preset_name": option["name"],
                    "meta.mode": selected_mode,
                    "meta.density": option["density"],
                }
            )
            return tokens
        return build_tokens(option["id"], option["mode"], overrides or {})

    def create_custom_preset(
        self,
        *,
        name: str,
        base_preset_id: str,
        mode: str,
        density: str = "normal",
        overrides: dict[str, Any] | None = None,
        save: bool = True,
    ) -> str:
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("Название темы не может быть пустым.")

        base_preset = get_preset(base_preset_id)
        preset_id = f"custom_{uuid4().hex[:12]}"
        payload = deepcopy(self._payload or default_settings_payload())
        custom_presets = dict(payload.get("custom_presets") or {})
        custom_presets[preset_id] = {
            "id": preset_id,
            "name": clean_name,
            "description": "Пользовательская тема.",
            "base_preset_id": base_preset.id,
            "mode": normalize_mode(mode or base_preset.default_mode),
            "density": str(density or base_preset.density or "normal"),
            "overrides": dict(overrides or {}),
        }
        payload["custom_presets"] = custom_presets
        self._payload = self.storage._normalize_payload(payload)
        self._tokens_cache.clear()
        if save:
            self.save()
        return preset_id

    def update_custom_preset(
        self,
        preset_id: str,
        *,
        name: str | None = None,
        base_preset_id: str | None = None,
        mode: str | None = None,
        density: str | None = None,
        overrides: dict[str, Any] | None = None,
        save: bool = True,
    ) -> None:
        requested = str(preset_id or "")
        custom_presets = self.custom_presets()
        if requested not in custom_presets:
            return

        current = dict(custom_presets[requested])
        if name is not None:
            clean_name = str(name or "").strip()
            if clean_name:
                current["name"] = clean_name
        if base_preset_id is not None:
            current["base_preset_id"] = get_preset(base_preset_id).id
        if mode is not None:
            current["mode"] = normalize_mode(mode)
        if density is not None:
            current["density"] = str(density or "normal")
        if overrides is not None:
            current["overrides"] = dict(overrides or {})

        custom_presets[requested] = current
        payload = deepcopy(self._payload or default_settings_payload())
        payload["custom_presets"] = custom_presets
        self._payload = self.storage._normalize_payload(payload)
        self._tokens_cache.clear()
        if save:
            self.save()

    def delete_custom_preset(self, preset_id: str, *, save: bool = True) -> None:
        requested = str(preset_id or "")
        custom_presets = self.custom_presets()
        if requested not in custom_presets:
            return

        custom_presets.pop(requested, None)
        payload = deepcopy(self._payload or default_settings_payload())
        payload["custom_presets"] = custom_presets

        active = dict(payload.get("active") or {})
        for role in ("doctor", "nurse"):
            role_settings = active.get(role)
            if isinstance(role_settings, dict) and role_settings.get("preset_id") == requested:
                active[role] = default_role_settings()
        payload["active"] = active

        self._payload = self.storage._normalize_payload(payload)
        self._tokens_cache.clear()
        if save:
            self.save()

    def set_theme(
        self,
        role: str,
        *,
        preset_id: str,
        mode: str,
        density: str = "normal",
        overrides: dict[str, Any] | None = None,
        save: bool = False,
    ) -> None:
        normalized_role = normalize_role(role)
        if normalized_role == "system":
            return
        option = self.theme_option(preset_id)
        payload = deepcopy(self._payload or default_settings_payload())
        active = dict(payload.get("active") or {})
        active[normalized_role] = {
            "preset_id": option["id"],
            "mode": option["mode"],
            "density": str(density or option["density"] or "normal"),
            "overrides": overrides or {},
        }
        payload["active"] = active
        self._payload = self.storage._normalize_payload(payload)
        self._tokens_cache.pop(normalized_role, None)
        self._active_role = normalized_role
        os.environ["REMCARD_UI_ROLE"] = normalized_role
        if save:
            self.save()

    def reset_role(self, role: str, *, save: bool = True) -> None:
        normalized_role = normalize_role(role)
        if normalized_role == "system":
            return
        payload = dict(self._payload or default_settings_payload())
        active = dict(payload.get("active") or {})
        active[normalized_role] = {
            "preset_id": "remcard_light",
            "mode": "light",
            "density": "normal",
            "overrides": {},
        }
        payload["active"] = active
        self._payload = self.storage._normalize_payload(payload)
        self._tokens_cache.pop(normalized_role, None)
        if save:
            self.save()

    def save(self) -> None:
        self.storage.save(self._payload)


_THEME_MANAGER: ThemeManager | None = None


def get_theme_manager() -> ThemeManager:
    global _THEME_MANAGER
    if _THEME_MANAGER is None:
        _THEME_MANAGER = ThemeManager()
    return _THEME_MANAGER
