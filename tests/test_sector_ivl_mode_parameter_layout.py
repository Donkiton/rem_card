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

from PySide6.QtWidgets import QApplication  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
