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
        self.assertEqual(ordered_visible_ids_by_side(section, SECTOR8_BUTTON_SIDE_LEFT), ["user_report"])
        right_ids = ordered_visible_ids_by_side(section, SECTOR8_BUTTON_SIDE_RIGHT)
        self.assertIn("add_patient", right_ids)
        self.assertIn("calculations", right_ids)
        self.assertIn("archive", right_ids)
        self.assertNotIn("user_report", right_ids)

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


if __name__ == "__main__":
    unittest.main()
