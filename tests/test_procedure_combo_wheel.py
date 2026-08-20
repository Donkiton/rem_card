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

from PySide6.QtWidgets import QApplication, QComboBox, QVBoxLayout, QWidget  # noqa: E402

from rem_card.ui.procedures.procedure_styles import (  # noqa: E402
    _ProcedureComboWheelBlocker,
    apply_procedure_combo_style,
)


class ProcedureComboWheelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_combo_popup_view_keeps_native_wheel_scrolling(self):
        root = QWidget()
        layout = QVBoxLayout(root)
        combo = QComboBox()
        combo.addItems([f"Item {index}" for index in range(30)])
        layout.addWidget(combo)

        apply_procedure_combo_style(root)

        self.assertIsInstance(
            getattr(combo, "_procedure_combo_wheel_blocker", None),
            _ProcedureComboWheelBlocker,
        )
        self.assertFalse(hasattr(combo.view(), "_procedure_combo_wheel_blocker"))
        self.assertFalse(hasattr(combo.view().viewport(), "_procedure_combo_wheel_blocker"))
        self.assertFalse(hasattr(combo.view().verticalScrollBar(), "_procedure_combo_wheel_blocker"))


if __name__ == "__main__":
    unittest.main()
