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

from rem_card.ui.nurse_view.nurse_remcard_layout import (  # noqa: E402
    NurseRemCardLayoutManager,
)
from rem_card.ui.shared.remcard_layout import RemCardLayoutManager  # noqa: E402


class DietSidebarTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _assert_diet_sidebar(self, layout):
        self.assertEqual(layout.set_active_tab("Диета", source="refresh"), "Диета")
        self.assertFalse(layout.sector_3_4_wrapper.isHidden())
        self.assertIs(layout.sector_7b_stack.currentWidget(), layout.sector_7diet_b)
        self.assertEqual(layout.sector_7diet_b.header_lbl.text(), "Диеты")

        layout.deleteLater()
        self.app.processEvents()

    def test_doctor_diet_tab_keeps_balance_sidebar_visible(self):
        self._assert_diet_sidebar(RemCardLayoutManager())

    def test_nurse_diet_tab_keeps_balance_sidebar_visible(self):
        self._assert_diet_sidebar(NurseRemCardLayoutManager())

    def _assert_balance_lower_edge_is_stable(self, layout):
        layout.resize(1400, 900)
        layout.show()
        self.app.processEvents()
        layout.set_active_tab("Витальные функции", source="refresh")
        self.app.processEvents()
        stack_geometry = layout.vitals_stack.geometry()

        layout.set_active_tab("Баланс жидкости", source="refresh")
        self.app.processEvents()

        self.assertEqual(layout._balance_grid_wrapper.layout().contentsMargins().bottom(), 5)
        self.assertEqual(layout.vitals_stack.geometry(), stack_geometry)

        layout.set_active_tab("Витальные функции", source="refresh")
        self.app.processEvents()
        self.assertEqual(layout.vitals_stack.geometry(), stack_geometry)
        layout.deleteLater()
        self.app.processEvents()

    def test_doctor_balance_grid_keeps_same_lower_boundary_as_other_tabs(self):
        self._assert_balance_lower_edge_is_stable(RemCardLayoutManager())

    def test_nurse_balance_grid_keeps_same_lower_boundary_as_other_tabs(self):
        self._assert_balance_lower_edge_is_stable(NurseRemCardLayoutManager())


if __name__ == "__main__":
    unittest.main()
