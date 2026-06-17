from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.ui.shared.decor_settings import (  # noqa: E402
    DECOR_TIMEZONE,
    DecorSettingsValidationError,
    active_decor_event,
    decor_file_path,
    normalize_decor_settings_payload,
    validate_decor_settings_payload,
)


class DecorSettingsTest(unittest.TestCase):
    def test_default_new_year_snow_uses_vladivostok_time(self):
        payload = normalize_decor_settings_payload(None)
        tz = ZoneInfo(DECOR_TIMEZONE)

        dec_31 = active_decor_event(payload, datetime(2026, 12, 31, 12, 0, tzinfo=tz))
        jan_1 = active_decor_event(payload, datetime(2027, 1, 1, 23, 30, tzinfo=tz))
        jan_2 = active_decor_event(payload, datetime(2027, 1, 2, 0, 1, tzinfo=tz))

        self.assertIsNotNone(dec_31)
        self.assertEqual(dec_31["id"], "new_year_snow")
        self.assertIsNotNone(jan_1)
        self.assertEqual(jan_1["id"], "new_year_snow")
        self.assertIsNone(jan_2)

    def test_any_enabled_time_overlap_is_rejected_even_for_different_zones(self):
        payload = normalize_decor_settings_payload(
            {
                "events": [
                    {
                        "id": "snow",
                        "name": "Снег",
                        "enabled": True,
                        "zone": "all",
                        "start": "12-31",
                        "start_time": "00:00",
                        "end": "01-01",
                        "end_time": "23:59",
                        "particles": [{"file": "decor_snowflake_1.svg"}],
                    },
                    {
                        "id": "roses",
                        "name": "Розы",
                        "enabled": True,
                        "zone": "w1",
                        "start": "01-01",
                        "start_time": "12:00",
                        "end": "01-01",
                        "end_time": "14:00",
                        "particles": [{"file": "rose.png"}],
                    },
                ]
            }
        )

        with self.assertRaises(DecorSettingsValidationError):
            validate_decor_settings_payload(payload)

    def test_disabled_events_are_still_checked_for_overlap(self):
        payload = normalize_decor_settings_payload(
            {
                "events": [
                    {
                        "id": "first",
                        "name": "Первое",
                        "enabled": True,
                        "start": "03-08",
                        "start_time": "12:00",
                        "end": "03-08",
                        "end_time": "14:00",
                        "particles": [{"file": "rose.png"}],
                    },
                    {
                        "id": "disabled",
                        "name": "Выключенное",
                        "enabled": False,
                        "start": "03-08",
                        "start_time": "13:00",
                        "end": "03-08",
                        "end_time": "15:00",
                        "particles": [{"file": "rose.png"}],
                    },
                ]
            }
        )

        with self.assertRaises(DecorSettingsValidationError):
            validate_decor_settings_payload(payload)

    def test_default_snowflake_assets_are_available_from_icon_folder(self):
        self.assertTrue(os.path.isfile(decor_file_path("decor_snowflake_1.svg")))


if __name__ == "__main__":
    unittest.main()
