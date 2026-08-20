from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.services.operblock_medication_presets import (  # noqa: E402
    build_operblock_preset_payload,
    normalize_operblock_medication_presets_payload,
)
from rem_card.services.operblock_service import OperBlockService  # noqa: E402


class OperBlockControlledPrescriptionsTest(unittest.TestCase):
    def test_preset_flag_survives_normalization_and_payload(self):
        payload = normalize_operblock_medication_presets_payload(
            {
                "items": [
                    {
                        "preset_id": "bolus:fentanyl",
                        "label": "Fentanyl",
                        "display_name": "Фентанил",
                        "kind": "bolus",
                        "requires_narcotic_sheet": True,
                        "enabled": True,
                    }
                ]
            }
        )

        item = payload["items"][0]
        self.assertTrue(item["requires_narcotic_sheet"])
        self.assertTrue(build_operblock_preset_payload(item)["requires_narcotic_sheet"])

    def test_controlled_rows_match_current_preset_id(self):
        rows = OperBlockService._operation_report_controlled_medications(
            {
                "bolus_events": [
                    {
                        "event_time": "2026-06-17T09:10:00",
                        "drug_label": "Фентанил",
                        "display_label": "Фентанил 0,1 мг",
                        "payload": {"preset_id": "bolus:fentanyl"},
                        "source_id": 1,
                    },
                    {
                        "event_time": "2026-06-17T09:20:00",
                        "drug_label": "Атропин",
                        "display_label": "Атропин 0,5 мг",
                        "payload": {"preset_id": "bolus:atropine"},
                        "source_id": 2,
                    },
                ],
                "infusion_intervals": [],
            },
            presets=[
                {
                    "preset_id": "bolus:fentanyl",
                    "label": "Fentanyl",
                    "display_name": "Фентанил",
                    "requires_narcotic_sheet": True,
                },
                {
                    "preset_id": "bolus:atropine",
                    "label": "Atropine",
                    "display_name": "Атропин",
                    "requires_narcotic_sheet": False,
                },
            ],
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["datetime"], "2026-06-17T09:10:00")
        self.assertEqual(rows[0]["name"], "Фентанил 0,1 мг")

    def test_current_unchecked_preset_overrides_saved_payload_flag(self):
        rows = OperBlockService._operation_report_controlled_medications(
            {
                "bolus_events": [
                    {
                        "event_time": "2026-06-17T09:10:00",
                        "drug_label": "Фентанил",
                        "display_label": "Фентанил 0,1 мг",
                        "payload": {
                            "preset_id": "bolus:fentanyl",
                            "requires_narcotic_sheet": True,
                        },
                        "source_id": 1,
                    }
                ],
                "infusion_intervals": [],
            },
            presets=[
                {
                    "preset_id": "bolus:fentanyl",
                    "label": "Fentanyl",
                    "display_name": "Фентанил",
                    "requires_narcotic_sheet": False,
                }
            ],
        )

        self.assertEqual(rows, [])

    def test_manual_row_can_match_marked_preset_by_name(self):
        rows = OperBlockService._operation_report_controlled_medications(
            {
                "bolus_events": [
                    {
                        "event_time": "2026-06-17T09:10:00",
                        "drug_label": "Фентанил",
                        "display_label": "Фентанил 0,1 мг",
                        "payload": {},
                        "source_id": 1,
                    }
                ],
                "infusion_intervals": [],
            },
            presets=[
                {
                    "preset_id": "bolus:fentanyl",
                    "label": "Fentanyl",
                    "display_name": "Фентанил",
                    "requires_narcotic_sheet": True,
                }
            ],
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Фентанил 0,1 мг")

    def test_controlled_row_adds_volume_from_concentration(self):
        rows = OperBlockService._operation_report_controlled_medications(
            {
                "bolus_events": [
                    {
                        "event_time": "2026-06-17T09:10:00",
                        "drug_label": "S. Phentanyli",
                        "display_label": "S. Phentanyli 100 мкг",
                        "dose_value": "100",
                        "dose_unit": "мкг",
                        "payload": {
                            "preset_id": "bolus:fentanyl",
                            "concentration": "0,005%",
                        },
                        "source_id": 1,
                    }
                ],
                "infusion_intervals": [],
            },
            presets=[
                {
                    "preset_id": "bolus:fentanyl",
                    "label": "S. Phentanyli",
                    "display_name": "S. Phentanyli",
                    "requires_narcotic_sheet": True,
                }
            ],
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "S. Phentanyli 100 мкг (2 мл)")


if __name__ == "__main__":
    unittest.main()
