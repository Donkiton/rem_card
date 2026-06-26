from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
