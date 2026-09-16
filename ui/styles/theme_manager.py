from __future__ import annotations

import os
from copy import deepcopy
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from rem_card.ui.styles.qss_builder import build_global_style
from rem_card.ui.styles.theme_presets import build_tokens, get_preset, list_presets
from rem_card.ui.styles.theme_storage import ThemeStorage, role_settings_from_payload
from rem_card.ui.styles.theme_tokens import default_role_settings, default_settings_payload, normalize_mode, normalize_role
from rem_card.ui.styles.tooltip_style import apply_tooltip_palette


FULL_RUNTIME_THEME_ENV = "REMCARD_FULL_RUNTIME_THEME"
_FALSE_VALUES = {"0", "false", "no", "off"}


def full_runtime_theme_enabled() -> bool:
    """Runtime theming is enabled unless the feature flag explicitly disables it."""
    return str(os.environ.get(FULL_RUNTIME_THEME_ENV, "1")).strip().lower() not in _FALSE_VALUES


def _is_static_operblock_role(role: str | None = None) -> bool:
    """Compatibility shim: operblock now supports the shared runtime theme."""
    return False


class ThemeManager(QObject):
    theme_changed = Signal(str)

    def __init__(self, storage: ThemeStorage | None = None):
        super().__init__()
        startup_role = str(os.environ.get("REMCARD_UI_ROLE") or "").strip().lower()
        self._enabled = full_runtime_theme_enabled()
        self._disabled_after_failure = False
        self.storage = (storage or ThemeStorage()) if self._enabled else None
        self._payload = default_settings_payload()
        self._mode = "light"
        self._loaded = False
        self._active_role = normalize_role(startup_role)
        self._runtime_role = startup_role or self._active_role
        self._tokens_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._applied_profile: tuple[Any, ...] | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def is_static_operblock(self) -> bool:
        return False

    @property
    def active_role(self) -> str:
        return self._active_role

    @property
    def mode(self) -> str:
        return self._mode if self._enabled else "light"

    @property
    def settings_path(self) -> str:
        return self.storage.path if self.storage is not None else ""

    def _set_active_role(self, role: str | None) -> None:
        if not role:
            return
        self._runtime_role = str(role).strip().lower()
        self._active_role = normalize_role(role)
        os.environ["REMCARD_UI_ROLE"] = self._runtime_role

    def _ensure_loaded(self) -> None:
        if self._enabled and not self._loaded:
            self.load()

    def load(self, role: str | None = None) -> dict[str, Any]:
        self._set_active_role(role)
        if not self._enabled or self.storage is None:
            self._payload = default_settings_payload()
            self._mode = "light"
            self._loaded = True
            self._tokens_cache.clear()
            return deepcopy(self._payload)
        self._payload = self.storage.load()
        self._mode = normalize_mode(self._payload.get("mode"))
        self._loaded = True
        self._tokens_cache.clear()
        return deepcopy(self._payload)

    def settings_for_role(self, role: str | None = None) -> dict[str, Any]:
        self._ensure_loaded()
        settings = dict(role_settings_from_payload(self._payload, role or self._active_role))
        settings["mode"] = self.mode
        if settings.get("preset_id") in {"remcard_light", "remcard_dark"}:
            settings["preset_id"] = f"remcard_{self.mode}"
        return settings

    def custom_presets(self) -> dict[str, dict[str, Any]]:
        if not self._enabled:
            return {}
        self._ensure_loaded()
        value = self._payload.get("custom_presets") if isinstance(self._payload, dict) else None
        return deepcopy(value) if isinstance(value, dict) else {}

    @staticmethod
    def _builtin_option(preset) -> dict[str, Any]:
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

    def theme_options(self) -> list[dict[str, Any]]:
        if not self._enabled:
            return [self._builtin_option(get_preset("remcard_light"))]
        options = [self._builtin_option(preset) for preset in list_presets()]
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
        if not self._enabled:
            return self._builtin_option(get_preset("remcard_light"))
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
        return self._builtin_option(get_preset(requested))

    def tokens_for_role(
        self,
        role: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self._enabled:
            return build_tokens("remcard_light", "light", {})
        self._ensure_loaded()
        normalized_role = normalize_role(role or self._active_role)
        cache_key = (normalized_role, self.mode)
        if settings is None and cache_key in self._tokens_cache:
            return dict(self._tokens_cache[cache_key])
        role_settings = dict(settings or self.settings_for_role(normalized_role))
        tokens = self.preview_tokens(
            role_settings.get("preset_id"),
            self.mode,
            role_settings.get("overrides") or {},
        )
        if settings is None:
            self._tokens_cache[cache_key] = dict(tokens)
        return tokens

    def current_tokens(self) -> dict[str, Any]:
        # theme.py reads this during import. Keep that import pure and light.
        if not self._loaded:
            return build_tokens("remcard_light", "light", {})
        return self.tokens_for_role(self._active_role)

    def build_qss(self, role: str | None = None) -> str:
        return build_global_style(self.tokens_for_role(role))

    def set_mode(self, mode: str, *, save: bool = True) -> None:
        if not self._enabled:
            return
        self._ensure_loaded()
        normalized = normalize_mode(mode)
        if normalized == self._mode:
            return
        previous_mode = self._mode
        previous_payload = deepcopy(self._payload)
        self._mode = normalized
        self._payload["mode"] = normalized
        active = dict(self._payload.get("active") or {})
        for role_name in ("doctor", "nurse"):
            role_settings = dict(active.get(role_name) or default_role_settings())
            role_settings["mode"] = normalized
            if role_settings.get("preset_id") in {"remcard_light", "remcard_dark"}:
                role_settings["preset_id"] = f"remcard_{normalized}"
            active[role_name] = role_settings
        self._payload["active"] = active
        self._tokens_cache.clear()
        if save:
            try:
                self.save()
            except Exception:
                self._mode = previous_mode
                self._payload = previous_payload
                self._tokens_cache.clear()
                raise
        target_app = QApplication.instance()
        if target_app is not None:
            self.apply_to_app(target_app)

    def disable_runtime(self) -> None:
        """Fall back to light in memory after a runtime application failure."""
        self._enabled = False
        self._disabled_after_failure = True
        self.storage = None
        self._payload = default_settings_payload()
        self._mode = "light"
        self._loaded = True
        self._tokens_cache.clear()
        self._applied_profile = None

    def apply_to_app(self, app: QApplication | None = None, role: str | None = None) -> None:
        self._set_active_role(role)
        target_app = app or QApplication.instance()
        if target_app is None or not self._enabled:
            return
        self._ensure_loaded()
        tokens = self.current_tokens()
        token_fingerprint = tuple(sorted((str(key), repr(value)) for key, value in tokens.items()))
        profile = (id(target_app), self._runtime_role, self.mode, token_fingerprint)
        if self._applied_profile == profile:
            return

        from rem_card.ui.styles import theme_runtime
        from rem_card.ui.styles.focus_rect_style import apply_application_theme_style

        theme_runtime.install_theme_runtime(target_app)
        apply_application_theme_style(target_app, self.mode)
        target_app.setPalette(self._build_palette(tokens))
        theme_runtime.refresh_registered_styles(mode=self.mode, force=True, tokens=tokens,
                                                defer_hidden=True)
        apply_tooltip_palette(target_app)
        self._applied_profile = profile
        self.theme_changed.emit(self.mode)

    @staticmethod
    def _build_palette(tokens: dict[str, Any]) -> QPalette:
        palette = QPalette()
        role_colors = {
            QPalette.ColorRole.Window: tokens["surface.window"],
            QPalette.ColorRole.WindowText: tokens["text.primary"],
            QPalette.ColorRole.Base: tokens["field.bg"],
            QPalette.ColorRole.AlternateBase: tokens["table.row_alt_bg"],
            QPalette.ColorRole.ToolTipBase: tokens["surface.card"],
            QPalette.ColorRole.ToolTipText: tokens["text.primary"],
            QPalette.ColorRole.Text: tokens["field.text"],
            QPalette.ColorRole.Button: tokens["surface.panel"],
            QPalette.ColorRole.ButtonText: tokens["text.primary"],
            QPalette.ColorRole.BrightText: tokens["text.inverse"],
            QPalette.ColorRole.Light: tokens["surface.hover"],
            QPalette.ColorRole.Midlight: tokens["surface.subtle"],
            QPalette.ColorRole.Dark: tokens["border.default"],
            QPalette.ColorRole.Mid: tokens["border.subtle"],
            QPalette.ColorRole.Shadow: tokens["surface.window"],
            QPalette.ColorRole.Highlight: tokens["surface.selected"],
            QPalette.ColorRole.HighlightedText: tokens["text.inverse"],
            QPalette.ColorRole.Link: tokens["state.info"],
            QPalette.ColorRole.LinkVisited: tokens["chart.palette.7"],
            QPalette.ColorRole.PlaceholderText: tokens["field.placeholder"],
        }
        accent_role = getattr(QPalette.ColorRole, "Accent", None)
        if accent_role is not None:
            role_colors[accent_role] = tokens["border.focus"]
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
            for color_role, color in role_colors.items():
                palette.setColor(group, color_role, QColor(str(color)))
        disabled = dict(role_colors)
        disabled.update(
            {
                QPalette.ColorRole.WindowText: tokens["text.disabled"],
                QPalette.ColorRole.Text: tokens["text.disabled"],
                QPalette.ColorRole.ButtonText: tokens["text.disabled"],
                QPalette.ColorRole.PlaceholderText: tokens["text.disabled"],
                QPalette.ColorRole.HighlightedText: tokens["text.disabled"],
            }
        )
        for color_role, color in disabled.items():
            palette.setColor(QPalette.ColorGroup.Disabled, color_role, QColor(str(color)))
        return palette

    def preview_tokens(
        self,
        preset_id: str,
        mode: str | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self._enabled:
            return build_tokens("remcard_light", "light", {})
        option = self.theme_option(preset_id)
        selected_mode = normalize_mode(mode or option["mode"])
        if option["is_custom"]:
            custom = self.custom_presets().get(option["id"], {})
            combined_overrides = dict(custom.get("overrides") or {})
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
        return build_tokens(option["id"], selected_mode, overrides or {})

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
        if not self._enabled:
            raise RuntimeError("Динамическая тема отключена.")
        self._ensure_loaded()
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("Название темы не может быть пустым.")
        base_preset = get_preset(base_preset_id)
        preset_id = f"custom_{uuid4().hex[:12]}"
        payload = deepcopy(self._payload)
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
        if not self._enabled:
            return
        requested = str(preset_id or "")
        custom_presets = self.custom_presets()
        if requested not in custom_presets:
            return
        current = dict(custom_presets[requested])
        if name is not None and str(name).strip():
            current["name"] = str(name).strip()
        if base_preset_id is not None:
            current["base_preset_id"] = get_preset(base_preset_id).id
        if mode is not None:
            current["mode"] = normalize_mode(mode)
        if density is not None:
            current["density"] = str(density or "normal")
        if overrides is not None:
            current["overrides"] = dict(overrides or {})
        custom_presets[requested] = current
        payload = deepcopy(self._payload)
        payload["custom_presets"] = custom_presets
        self._payload = self.storage._normalize_payload(payload)
        self._tokens_cache.clear()
        if save:
            self.save()

    def delete_custom_preset(self, preset_id: str, *, save: bool = True) -> None:
        if not self._enabled:
            return
        requested = str(preset_id or "")
        custom_presets = self.custom_presets()
        if requested not in custom_presets:
            return
        custom_presets.pop(requested, None)
        payload = deepcopy(self._payload)
        payload["custom_presets"] = custom_presets
        active = dict(payload.get("active") or {})
        for role_name in ("doctor", "nurse"):
            role_settings = active.get(role_name)
            if isinstance(role_settings, dict) and role_settings.get("preset_id") == requested:
                active[role_name] = default_role_settings()
                active[role_name]["mode"] = self.mode
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
        if not self._enabled:
            return
        self._ensure_loaded()
        normalized_role = normalize_role(role)
        if normalized_role == "system":
            return
        option = self.theme_option(preset_id)
        selected_mode = normalize_mode(mode or option["mode"])
        payload = deepcopy(self._payload)
        active = dict(payload.get("active") or {})
        active[normalized_role] = {
            "preset_id": option["id"],
            "mode": selected_mode,
            "density": str(density or option["density"] or "normal"),
            "overrides": dict(overrides or {}),
        }
        payload["active"] = active
        payload["mode"] = selected_mode
        self._payload = self.storage._normalize_payload(payload)
        self._mode = selected_mode
        self._tokens_cache.clear()
        self._set_active_role(role)
        if save:
            self.save()

    def reset_role(self, role: str, *, save: bool = True) -> None:
        if not self._enabled:
            return
        self._ensure_loaded()
        normalized_role = normalize_role(role)
        if normalized_role == "system":
            return
        payload = deepcopy(self._payload)
        active = dict(payload.get("active") or {})
        active[normalized_role] = default_role_settings()
        payload["active"] = active
        payload["mode"] = "light"
        self._payload = self.storage._normalize_payload(payload)
        self._mode = "light"
        self._tokens_cache.clear()
        if save:
            self.save()

    def save(self) -> None:
        if not self._enabled or self.storage is None:
            return
        self._payload["mode"] = self.mode
        self.storage.save(self._payload)


_THEME_MANAGER: ThemeManager | None = None


def get_theme_manager() -> ThemeManager:
    global _THEME_MANAGER
    enabled = full_runtime_theme_enabled()
    if _THEME_MANAGER is None or (
        not _THEME_MANAGER._disabled_after_failure and _THEME_MANAGER.enabled != enabled
    ):
        _THEME_MANAGER = ThemeManager()
    return _THEME_MANAGER
