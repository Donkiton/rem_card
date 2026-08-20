from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from rem_card.services.order_service import CVP_QUICK_ORDER_KEY, CVP_QUICK_ORDER_TEXT  # noqa: E402
from rem_card.ui.shared.components.nurse_order_card import NurseOrderCard  # noqa: E402


class NurseOrderCardCvpDisplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _card(self, *, drug_key: str, latin: str, order_type: str = "medication") -> NurseOrderCard:
        return NurseOrderCard(
            {
                "id": 1,
                "admin_id": 1,
                "order_id": 1,
                "planned_time": datetime(2025, 1, 1, 12, 0).isoformat(),
                "actual_time": "",
                "status": "planned",
                "comment": "",
                "cell_role": "single",
                "expected_revision": 0,
                "latin": latin,
                "drug_key": drug_key,
                "dose_value": 0,
                "dose_unit": "",
                "order_comment": "",
                "order_type": order_type,
                "duration_min": 0,
                "allow_not_done": True,
            }
        )

    def test_quick_cvp_does_not_get_solution_prefix(self):
        card = self._card(drug_key=CVP_QUICK_ORDER_KEY, latin=CVP_QUICK_ORDER_TEXT)

        self.assertEqual(card.lbl_line1.text(), CVP_QUICK_ORDER_TEXT)

    def test_regular_medication_still_gets_solution_prefix(self):
        card = self._card(drug_key="test_drug", latin="Natrii chloridi")

        self.assertEqual(card.lbl_line1.text(), "S. Natrii chloridi")


if __name__ == "__main__":
    unittest.main()
