from __future__ import annotations

import os
import sys
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


class OperblockStaticThemeTest(unittest.TestCase):
    def test_operblock_theme_does_not_open_dynamic_storage(self):
        for role in ("operblock", "operblock_planned", "operblock_emergency"):
            with (
                self.subTest(role=role),
                patch.dict(os.environ, {"REMCARD_UI_ROLE": role}),
                patch.object(ThemeStorage, "load", side_effect=AssertionError("dynamic storage must not be read")),
            ):
                manager = ThemeManager()

                self.assertTrue(manager.is_static_operblock)
                self.assertIsNone(manager.storage)
                self.assertEqual(manager.current_tokens()["meta.preset_id"], "remcard_light")
                self.assertEqual(manager.current_tokens()["meta.mode"], "light")

    def test_operblock_theme_cannot_be_changed_dynamically(self):
        with patch.dict(os.environ, {"REMCARD_UI_ROLE": "operblock"}):
            manager = ThemeManager()

            manager.set_theme("doctor", preset_id="remcard_dark", mode="dark", save=True)

            self.assertEqual(manager.current_tokens()["meta.preset_id"], "remcard_light")
            self.assertEqual(manager.preview_tokens("remcard_dark", "dark")["meta.mode"], "light")
            self.assertEqual([option["id"] for option in manager.theme_options()], ["remcard_light"])
            with self.assertRaises(RuntimeError):
                manager.create_custom_preset(
                    name="Недопустимая тема",
                    base_preset_id="remcard_dark",
                    mode="dark",
                )

    def test_full_runtime_theme_flag_still_uses_basic_operblock_palette(self):
        app = Mock()
        with (
            patch.dict(os.environ, {"REMCARD_FULL_RUNTIME_THEME": "1"}),
            patch("rem_card.app.main._install_no_button_focus_rect_style"),
            patch("rem_card.app.main._apply_basic_app_theme") as apply_basic,
        ):
            _apply_app_theme(app, "operblock_planned")

        apply_basic.assert_called_once_with(app)


if __name__ == "__main__":
    unittest.main()
