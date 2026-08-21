from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtWidgets import QApplication, QSizePolicy  # noqa: E402

from rem_card.ui.rem_card_sectors.sector_ivl import SectorIvl  # noqa: E402


class SectorIvlModeParameterLayoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widget = SectorIvl()
        self.widget.resize(1200, 560)
        self.widget.show()
        self.app.processEvents()

    def tearDown(self):
        self.widget.close()

    def test_visible_mode_parameters_are_repacked_without_gaps_after_mode_switch(self):
        self.widget._apply_mode_fields(["Phigh", "Plow", "Thigh", "Tlow", "FiO2"])
        self.app.processEvents()

        self.widget._apply_mode_fields(["PEEP", "FiO2"])
        self.app.processEvents()

        xs = [self.widget.param_field_widgets[name].x() for name in ["PEEP", "FiO2"]]
        self.assertEqual(xs, [0, 130])
        self.assertTrue(self.widget.param_field_widgets["PEEP"].isVisible())
        self.assertTrue(self.widget.param_field_widgets["FiO2"].isVisible())
        self.assertFalse(self.widget.param_field_widgets["Phigh"].isVisible())

    def test_parameter_label_and_input_are_aligned_in_the_same_compact_field(self):
        self.widget._apply_mode_fields(["PS", "PEEP", "FiO2"])
        self.app.processEvents()

        xs = [self.widget.param_field_widgets[name].x() for name in ["PS", "PEEP", "FiO2"]]
        self.assertEqual(xs, [0, 130, 260])
        for name in ["PS", "PEEP", "FiO2"]:
            label, edit = self.widget.param_widgets[name]
            self.assertEqual(edit.x() - (label.x() + label.width()), 4)

    def test_active_case_header_uses_separate_start_datetime_line(self):
        active_case = SimpleNamespace(
            id=1,
            episode_number=1,
            revision=1,
            start_time=datetime(2026, 6, 27, 16, 15),
        )

        self.widget._apply_snapshot(
            {
                "summary": {
                    "active_case": active_case,
                    "case_duration_seconds": 8760,
                    "tube_duration_seconds": 8760,
                    "tube_alert": False,
                    "total_duration_seconds": 8760,
                },
                "timeline": [],
                "latest_case": None,
            }
        )
        self.app.processEvents()

        self.assertEqual(self.widget.lbl_case_status.text(), "Случай #1. Активен с:")
        self.assertEqual(self.widget.lbl_case_start.text(), "27.06.2026 16:15")
        self.assertEqual(self.widget.lbl_case_duration.text(), "Длительность случая: 02:26")

    def test_top_action_buttons_fit_bold_labels(self):
        for button in (
            self.widget.btn_create_case,
            self.widget.btn_close_case,
            self.widget.btn_replace_tube,
            self.widget.btn_undo,
        ):
            self.assertGreaterEqual(button.width(), button.sizeHint().width(), button.text())

    def test_extubation_datetime_is_left_of_indications(self):
        self.assertLess(
            self.widget.extubation_dt_edit.parentWidget().width(),
            self.widget.start_dt_edit.parentWidget().width(),
        )

        self.widget.resize(900, 560)
        self.app.processEvents()

        self.assertLess(self.widget.extubation_dt_edit.geometry().right(), self.widget.extubation_time_spacer.x())
        self.assertLess(self.widget.extubation_time_spacer.geometry().right(), self.widget.extubation_reason_edit.x())
        self.assertGreaterEqual(
            self.widget.extubation_reason_edit.x() - self.widget.extubation_dt_edit.geometry().right(),
            20,
        )
        self.assertLess(self.widget.extubation_reason_edit.width(), self.widget.extubation_dt_edit.width())
        self.assertLess(self.widget.extubation_reason_edit.x(), self.widget.extubation_o2_flow_edit.x())
        self.assertEqual(self.widget.extubation_dt_edit.sizePolicy().horizontalPolicy(), QSizePolicy.Fixed)
        placeholder = self.widget.extubation_reason_edit.lineEdit().placeholderText()
        placeholder_width = self.widget.extubation_reason_edit.lineEdit().fontMetrics().horizontalAdvance(placeholder) + 48
        self.assertGreaterEqual(self.widget.extubation_reason_edit.width(), placeholder_width)

    def test_extubation_datetime_defaults_to_now_and_respects_active_case_start(self):
        case_start = datetime(2026, 6, 27, 16, 15)
        active_case = SimpleNamespace(
            id=7,
            episode_number=2,
            revision=1,
            start_time=case_start,
        )
        snapshot = {
            "summary": {
                "active_case": active_case,
                "case_duration_seconds": 60,
                "tube_duration_seconds": 60,
                "tube_alert": False,
                "total_duration_seconds": 60,
            },
            "timeline": [],
            "latest_case": None,
        }
        self.widget.admission_id = 42

        before = datetime.now()
        self.widget._apply_snapshot(snapshot)
        after = datetime.now()
        selected = self.widget.extubation_dt_edit.dateTime().toPython()

        self.assertLess(abs((selected - before).total_seconds()), 1)
        self.assertLessEqual(selected, after)
        self.assertEqual(self.widget.extubation_dt_edit.minimumDateTime().toPython(), case_start)

        edited = datetime(2026, 6, 28, 9, 30)
        self.widget.extubation_dt_edit.setDateTime(edited)
        self.widget._apply_snapshot(snapshot)
        self.assertEqual(self.widget.extubation_dt_edit.dateTime().toPython(), edited)

        self.widget.extubation_dt_edit.setDateTime(datetime(2026, 6, 27, 15, 0))
        self.assertEqual(self.widget.extubation_dt_edit.dateTime().toPython(), case_start)

    def test_close_case_uses_selected_extubation_datetime(self):
        captured = {}

        class Service:
            @staticmethod
            def enqueue_write(**kwargs):
                captured.update(kwargs)
                return True

            @staticmethod
            def close_case(case_id, **kwargs):
                captured["closed_case_id"] = case_id
                captured["close_kwargs"] = kwargs
                return None

        selected = datetime(2026, 6, 28, 9, 30)
        self.widget.remcard_service = Service()
        self.widget.admission_id = 42
        self.widget.active_case_id = 7
        self.widget._active_case_revision = 3
        self.widget._extubation_min_datetime = datetime(2026, 6, 27, 16, 15)
        self.widget.extubation_dt_edit.setMinimumDateTime(self.widget.start_dt_edit.minimumDateTime())
        self.widget.extubation_dt_edit.setDateTime(selected)
        self.widget.extubation_reason_edit.setEditText("В сознании, стабилен")

        self.widget._on_close_case_clicked()
        captured["operation"]()

        self.assertEqual(captured["closed_case_id"], 7)
        self.assertEqual(captured["close_kwargs"]["end_time"], selected)
        self.assertEqual(captured["close_kwargs"]["expected_case_revision"], 3)

    def test_ivl_write_is_single_attempt_and_shows_loading_state(self):
        captured = {}

        class Service:
            @staticmethod
            def enqueue_write(**kwargs):
                captured.update(kwargs)
                return True

        self.widget.show_loading_indicator = lambda *_args, **_kwargs: "ivl-test"
        self.widget.hide_loading_indicator = lambda *_args, **_kwargs: None
        self.widget.remcard_service = Service()
        self.widget.admission_id = 164
        self.widget._enqueue_ivl_write(
            "ivl_create_case:164",
            lambda: None,
            pending_text="Случай: открытие сохраняется...",
            error_title="Ошибка",
        )
        self.app.processEvents()

        self.assertTrue(self.widget._ivl_write_pending)
        self.assertIsNotNone(self.widget._ivl_loading_key)
        self.assertFalse(self.widget.btn_create_case.isEnabled())
        self.assertEqual(captured["write_metadata"]["role"], "doctor")
        self.assertEqual(captured["write_metadata"]["queue_retryable"], False)
        self.assertEqual(captured["write_metadata"]["timeout_ms"], 5000)
        self.widget._ivl_write_pending = False
        self.widget._hide_ivl_write_loading()


if __name__ == "__main__":
    unittest.main()
