from __future__ import annotations

import os
from typing import Any

from PySide6.QtWidgets import QApplication

from rem_card.ui.styles.qss_builder import build_global_style
from rem_card.ui.styles.theme_presets import build_tokens
from rem_card.ui.styles.theme_storage import ThemeStorage, role_settings_from_payload
from rem_card.ui.styles.theme_tokens import default_settings_payload, normalize_role


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

    def tokens_for_role(self, role: str | None = None, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        normalized_role = normalize_role(role or self._active_role)
        if settings is None and normalized_role in self._tokens_cache:
            return dict(self._tokens_cache[normalized_role])

        role_settings = settings or self.settings_for_role(normalized_role)
        tokens = build_tokens(
            role_settings.get("preset_id"),
            role_settings.get("mode"),
            role_settings.get("overrides") or {},
        )
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
            target_app.setStyleSheet(self.build_qss(self._active_role))

    def preview_tokens(self, preset_id: str, mode: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        return build_tokens(preset_id, mode, overrides or {})

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
        payload = dict(self._payload or default_settings_payload())
        active = dict(payload.get("active") or {})
        active[normalized_role] = {
            "preset_id": preset_id,
            "mode": mode,
            "density": density,
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
