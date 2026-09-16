from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.ui.shared.display_settings_storage import (  # noqa: E402
    SECTOR8_BUTTON_SIDE_LEFT,
    SECTOR8_BUTTON_SIDE_RIGHT,
    default_role_display_settings,
    normalize_role_display_settings,
    ordered_visible_ids_by_side,
)


class DisplaySettingsSector8SidesTest(unittest.TestCase):
    def test_factory_layout_is_available_without_database_or_files(self):
        from unittest.mock import patch
        expected = {
            "doctor": (["user_report", "roles", "emergency_mode"],
                       ["add_patient", "refresh", "archive", "calculations", "settings", "back", "exit"]),
            "nurse": (["user_report", "roles", "emergency_mode"],
                      ["add_patient", "refresh", "back", "exit"]),
            "operblock": (["user_report", "roles"], ["archive", "refresh", "back", "exit"]),
        }
        with patch("builtins.open", side_effect=AssertionError("Factory defaults must not read files")):
            for role, (left, right) in expected.items():
                section = default_role_display_settings(role)["sector8_buttons"]
                self.assertEqual(ordered_visible_ids_by_side(section, "left"), left)
                self.assertEqual(ordered_visible_ids_by_side(section, "right"), right)

    def test_default_report_buttons_are_on_left_side(self):
        settings = default_role_display_settings("doctor")
        section = settings["sector8_buttons"]

        self.assertEqual(section["side"]["user_report"], SECTOR8_BUTTON_SIDE_LEFT)
        self.assertEqual(section["side"]["user_reports"], SECTOR8_BUTTON_SIDE_LEFT)
        self.assertEqual(section["side"]["archive"], SECTOR8_BUTTON_SIDE_RIGHT)

    def test_old_sector8_settings_migrate_report_buttons_left(self):
        settings = normalize_role_display_settings(
            "doctor",
            {
                "sector8_buttons": {
                    "order": ["add_patient", "user_report", "archive", "user_reports"],
                    "visible": {
                        "add_patient": True,
                        "user_report": True,
                        "archive": True,
                        "user_reports": False,
                    },
                }
            },
        )
        section = settings["sector8_buttons"]

        self.assertEqual(section["side"]["user_report"], SECTOR8_BUTTON_SIDE_LEFT)
        self.assertEqual(section["side"]["user_reports"], SECTOR8_BUTTON_SIDE_LEFT)
        left_ids = ordered_visible_ids_by_side(section, SECTOR8_BUTTON_SIDE_LEFT)
        self.assertIn("emergency_mode", left_ids)
        self.assertEqual([item for item in left_ids if item not in {"emergency_mode", "roles"}], ["user_report"])
        right_ids = ordered_visible_ids_by_side(section, SECTOR8_BUTTON_SIDE_RIGHT)
        self.assertIn("add_patient", right_ids)
        self.assertIn("calculations", right_ids)
        self.assertIn("archive", right_ids)
        self.assertNotIn("user_report", right_ids)

    def test_old_settings_gain_emergency_button_for_doctor_and_nurse_only(self):
        for role in ("doctor", "nurse"):
            with self.subTest(role=role):
                settings = normalize_role_display_settings(role, {
                    "sector8_buttons": {
                        "order": ["settings", "user_report", "archive"],
                        "visible": {"archive": False},
                        "side": {"settings": "left", "user_report": "right"},
                    },
                })["sector8_buttons"]
                self.assertTrue(settings["visible"]["emergency_mode"])
                self.assertEqual(settings["side"]["emergency_mode"], "left")
                self.assertFalse(settings["visible"]["archive"])
                self.assertEqual(settings["side"]["settings"], "left")
                self.assertEqual(settings["side"]["user_report"], "right")
        self.assertNotIn("emergency_mode", default_role_display_settings("operblock")["sector8_buttons"]["order"])

    def test_legacy_doctor_calculators_merge_visibility_order_and_side(self):
        settings = normalize_role_display_settings(
            "doctor",
            {
                "sector8_buttons": {
                    "order": ["archive", "burn_calc", "calc", "settings"],
                    "visible": {"burn_calc": False, "calc": True},
                    "side": {"burn_calc": "left", "calc": "right"},
                }
            },
        )
        section = settings["sector8_buttons"]

        self.assertEqual(section["order"].count("calculations"), 1)
        self.assertNotIn("calc", section["order"])
        self.assertNotIn("burn_calc", section["order"])
        self.assertTrue(section["visible"]["calculations"])
        self.assertEqual(section["side"]["calculations"], SECTOR8_BUTTON_SIDE_LEFT)

    def test_hidden_legacy_nurse_calculator_stays_hidden(self):
        settings = normalize_role_display_settings(
            "nurse",
            {
                "sector8_buttons": {
                    "order": ["archive", "calc", "settings"],
                    "visible": {"calc": False},
                    "side": {"calc": "right"},
                }
            },
        )

        self.assertFalse(settings["sector8_buttons"]["visible"]["calculations"])

    def test_roles_action_is_registered_and_uses_per_role_visibility(self):
        for role in ("doctor", "nurse", "operblock"):
            with self.subTest(role=role):
                section = default_role_display_settings(role)["sector8_buttons"]
                self.assertIn("roles", section["order"])
                self.assertTrue(section["visible"]["roles"])

                hidden = normalize_role_display_settings(
                    role,
                    {"sector8_buttons": {"visible": {"roles": False}}},
                )["sector8_buttons"]
                self.assertFalse(hidden["visible"]["roles"])


if __name__ == "__main__":
    unittest.main()
