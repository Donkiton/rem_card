from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from rem_card.services.analytics.operblock_statistics_service import (  # noqa: E402
    OperBlockStatisticsReportBuilder,
)
from rem_card.services.operblock_timeline import (  # noqa: E402
    timeline_event_row_to_medication_event,
)
from rem_card.ui.operblock_view.operblock_main_widget import OperBlockMainWidget  # noqa: E402


class _TimelineHarness:
    _current_timeline_snapshot: dict = {}
    _timeline_event_numeric_id = staticmethod(OperBlockMainWidget._timeline_event_numeric_id)
    _infusion_identity = staticmethod(OperBlockMainWidget._infusion_identity)
    _timeline_order_events = OperBlockMainWidget._timeline_order_events
    _timeline_infusion_change_events = OperBlockMainWidget._timeline_infusion_change_events
    _timeline_events_for_infusion = OperBlockMainWidget._timeline_events_for_infusion


class OperBlockTimelineMappingTests(unittest.TestCase):
    def test_rejects_invalid_identity_and_event_type(self):
        self.assertIsNone(timeline_event_row_to_medication_event({"id": 0, "event_time": "2026-07-30 10:00"}))
        self.assertIsNone(
            timeline_event_row_to_medication_event(
                {"id": 1, "event_time": "bad-time", "event_type": "bolus"}
            )
        )
        self.assertIsNone(
            timeline_event_row_to_medication_event(
                {"id": 1, "event_time": "2026-07-30 10:00", "event_type": "unknown"}
            )
        )

    def test_stage_labels_and_parent_payload_are_preserved(self):
        stage = timeline_event_row_to_medication_event(
            {
                "id": 11,
                "event_time": "2026-07-30 10:00",
                "event_type": "clinical_event",
                "payload_json": json.dumps({"stage_kind": "surgery_start"}),
            }
        )
        self.assertIsNotNone(stage)
        self.assertEqual(stage.drug_label, "Начало операции")
        self.assertEqual(stage.display_label, "Начало операции")

        stopped = timeline_event_row_to_medication_event(
            {
                "id": 12,
                "event_time": "2026-07-30 10:30",
                "event_type": "infusion_stop",
                "parent_event_id": 11,
                "payload_json": {"display_name": "Пропофол"},
            }
        )
        self.assertIsNotNone(stopped)
        self.assertEqual(stopped.drug_label, "Пропофол")
        self.assertEqual(stopped.display_label, "Пропофол стоп")
        self.assertEqual(stopped.payload["parent_event_id"], 11)


class OperBlockStageStateTests(unittest.TestCase):
    def test_stage_state_keeps_closed_and_open_intervals(self):
        rows = [
            {
                "event_time": "2026-07-30 10:00",
                "payload_json": {
                    "stage_kind": "anesthesia_start",
                    "anesthesia_assistance_type": "ОА",
                    "anesthesiologist": "Врач А",
                },
            },
            {
                "event_time": "2026-07-30 10:05",
                "payload_json": {
                    "stage_kind": "surgery_start",
                    "operation_name": "Операция 1",
                    "surgeons": ["Хирург 1", "Хирург 2"],
                },
            },
            {
                "event_time": "2026-07-30 10:20",
                "payload_json": {"stage_kind": "custom", "label": "Ревизия"},
            },
            {
                "event_time": "2026-07-30 11:00",
                "payload_json": {"stage_kind": "surgery_end"},
            },
            {
                "event_time": "2026-07-30 11:10",
                "payload_json": {
                    "stage_kind": "anesthesia_end",
                    "transfer_department": "РАО",
                },
            },
            {
                "event_time": "2026-07-30 12:00",
                "payload_json": {
                    "stage_kind": "surgery_start",
                    "operation_name": "Операция 2",
                    "surgeon": "Хирург 3",
                },
            },
            {"event_time": "bad-time", "payload_json": {"stage_kind": "custom"}},
        ]

        state = OperBlockStatisticsReportBuilder._stage_state(rows)

        self.assertEqual(state["custom_events"], 1)
        self.assertEqual(len(state["anesthesia_intervals"]), 1)
        self.assertEqual(state["anesthesia_intervals"][0]["end"], datetime(2026, 7, 30, 11, 10))
        self.assertEqual(len(state["surgery_intervals"]), 2)
        self.assertIsNone(state["surgery_intervals"][1]["end"])
        self.assertEqual(state["anesthesia_type"], "ОА")
        self.assertEqual(state["anesthesiologist"], "Врач А")
        self.assertEqual(state["operation_name"], "Операция 2")
        self.assertEqual(state["surgeons"], ["Хирург 3"])
        self.assertEqual(state["transfer_department"], "РАО")


class OperBlockTimelineUiTests(unittest.TestCase):
    def test_infusion_events_keep_roles_labels_and_reverse_order(self):
        harness = _TimelineHarness()
        harness._current_timeline_snapshot = {
            "infusion_intervals": [
                {
                    "interval_id": "infusion:11",
                    "status": "stopped",
                    "start_time": "2026-07-30 10:00",
                    "end_time": "2026-07-30 10:30",
                    "current_rate_value": "5",
                    "current_rate_unit": "мл/ч",
                    "payload": {
                        "start_event_id": 11,
                        "display_name": "Пропофол",
                    },
                    "rate_history": [
                        {
                            "event_id": "timeline_event:11",
                            "event_time": "2026-07-30 10:00",
                            "rate_value": "5",
                            "rate_unit": "мл/ч",
                        },
                        {
                            "event_id": "timeline_event:12",
                            "event_time": "2026-07-30 10:15",
                            "rate_value": "6",
                            "rate_unit": "мл/ч",
                        },
                    ],
                }
            ]
        }

        events = OperBlockMainWidget._build_timeline_events(harness, [])

        self.assertEqual([event["role"] for event in events], ["stop", "change", "start"])
        self.assertEqual([event["badge"] for event in events], ["Стоп", "Изм. скорость", "Дозатор"])
        self.assertEqual(events[1]["detail"], "скорость 6 мл/час")
        self.assertEqual(events[2]["detail"], "старт 5 мл/час")
        self.assertTrue(all(event["drug"] == "Пропофол" for event in events))


if __name__ == "__main__":
    unittest.main()
