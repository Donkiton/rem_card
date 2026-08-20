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

from rem_card.data.dto.remcard_dto import OrderDTO  # noqa: E402
from rem_card.ui.doctor_view.administration_dialog import DrugCharacteristicsDialog  # noqa: E402


class AdministrationDialogFormRestoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_editing_uses_order_form_prefix_before_drug_default(self):
        order = OrderDTO(
            drug_key="amlodipine",
            latin="S. Amlodipini",
            dose_value=5,
            dose_unit="mg",
            comment="[ROUTE:болюс] [DUR:0]",
        )

        dialog = DrugCharacteristicsDialog("amlodipine", parent=None, initial_order=order)
        try:
            self.assertEqual(dialog.form_combo.currentData(), "solution_iv")
        finally:
            dialog.close()
            dialog.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
