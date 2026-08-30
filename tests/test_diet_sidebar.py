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


if __name__ == "__main__":
    unittest.main()
