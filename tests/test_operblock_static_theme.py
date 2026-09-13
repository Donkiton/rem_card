from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.app.main import _apply_app_theme  # noqa: E402
from rem_card.ui.styles.theme_manager import ThemeManager  # noqa: E402
from rem_card.ui.styles.theme_storage import ThemeStorage  # noqa: E402


class OperblockRuntimeThemeTest(unittest.TestCase):
    def test_operblock_loads_same_local_dark_mode_as_other_roles(self):
        for role in ("operblock", "operblock_planned", "operblock_emergency"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as temp_dir:
                storage = ThemeStorage(str(Path(temp_dir) / "theme.json"))
                payload = storage.load()
                payload["mode"] = "dark"
                storage.save(payload)
                with patch.dict(os.environ, {"REMCARD_FULL_RUNTIME_THEME": "1", "REMCARD_UI_ROLE": role}):
                    manager = ThemeManager(storage)
                    manager.load(role)

                self.assertFalse(manager.is_static_operblock)
                self.assertEqual(manager.mode, "dark")
                self.assertEqual(manager.current_tokens()["meta.mode"], "dark")

    def test_changing_role_keeps_workstation_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = ThemeStorage(str(Path(temp_dir) / "theme.json"))
            with (
                patch.dict(os.environ, {"REMCARD_FULL_RUNTIME_THEME": "1", "REMCARD_UI_ROLE": "operblock"}),
                patch("rem_card.ui.styles.theme_manager.QApplication.instance", return_value=None),
            ):
                manager = ThemeManager(storage)
                manager.load("operblock")
                manager.set_mode("dark", save=True)
                manager.load("doctor")

            self.assertEqual(manager.mode, "dark")
            self.assertEqual(manager.current_tokens()["meta.mode"], "dark")

    def test_disabled_guard_keeps_operblock_light_without_storage(self):
        storage = Mock()
        with patch.dict(os.environ, {"REMCARD_FULL_RUNTIME_THEME": "0", "REMCARD_UI_ROLE": "operblock"}):
            manager = ThemeManager(storage)
            manager.load("operblock")
            manager.set_mode("dark", save=True)

        self.assertFalse(manager.enabled)
        self.assertIsNone(manager.storage)
        self.assertEqual(manager.mode, "light")
        storage.load.assert_not_called()
        storage.save.assert_not_called()

    def test_enabled_startup_applies_runtime_theme_to_operblock(self):
        app = Mock()
        manager = Mock()
        with (
            patch.dict(os.environ, {"REMCARD_FULL_RUNTIME_THEME": "1"}),
            patch("rem_card.app.main._install_no_button_focus_rect_style"),
            patch("rem_card.app.main._apply_basic_app_theme") as apply_basic,
            patch("rem_card.ui.styles.theme_manager.get_theme_manager", return_value=manager),
            patch("rem_card.ui.styles.context_menu_style.install_global_text_edit_context_menus"),
        ):
            _apply_app_theme(app, "operblock_planned")

        manager.load.assert_called_once_with("operblock_planned")
        manager.apply_to_app.assert_called_once_with(app, "operblock_planned")
        apply_basic.assert_not_called()

    def test_disabled_startup_uses_basic_operblock_palette(self):
        app = Mock()
        with (
            patch.dict(os.environ, {"REMCARD_FULL_RUNTIME_THEME": "0"}),
            patch("rem_card.app.main._install_no_button_focus_rect_style"),
            patch("rem_card.app.main._apply_basic_app_theme") as apply_basic,
        ):
            _apply_app_theme(app, "operblock_planned")

        apply_basic.assert_called_once_with(app)

    def test_runtime_failure_resets_registered_styles_before_basic_fallback(self):
        app = Mock()
        manager = Mock()
        manager.apply_to_app.side_effect = RuntimeError("palette failed")
        order = []
        manager.disable_runtime.side_effect = lambda: order.append("disable")

        def fail_refresh(_mode):
            order.append("refresh")
            raise RuntimeError("callback failed")

        with (
            patch.dict(os.environ, {"REMCARD_FULL_RUNTIME_THEME": "1"}),
            patch("rem_card.app.main._install_no_button_focus_rect_style"),
            patch("rem_card.app.main._apply_basic_app_theme") as apply_basic,
            patch("rem_card.ui.styles.theme_manager.get_theme_manager", return_value=manager),
            patch("rem_card.ui.styles.context_menu_style.install_global_text_edit_context_menus"),
            patch("rem_card.ui.styles.focus_rect_style.apply_application_theme_style") as apply_style,
            patch(
                "rem_card.ui.styles.theme_runtime.refresh_registered_styles",
                side_effect=fail_refresh,
            ) as refresh,
        ):
            _apply_app_theme(app, "operblock")

        manager.disable_runtime.assert_called_once_with()
        apply_style.assert_called_once_with(app, "light")
        refresh.assert_called_once_with("light")
        apply_basic.assert_called_once_with(app)
        self.assertEqual(order, ["disable", "refresh"])


if __name__ == "__main__":
    unittest.main()
