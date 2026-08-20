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

from PySide6.QtWidgets import QApplication, QComboBox, QSizePolicy  # noqa: E402

from rem_card.ui.shared.duration_combo import (  # noqa: E402
    END_OF_DAY_DURATION_TEXT,
    apply_compact_duration_combo_style,
    configure_duration_combo,
    set_end_of_day_duration_text,
)


class DurationComboTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_end_of_day_text_is_not_scrolled_to_tail(self):
        combo = QComboBox()
        combo.setEditable(True)

        configure_duration_combo(combo)
        apply_compact_duration_combo_style(combo)
        combo.setEnabled(False)
        set_end_of_day_duration_text(combo)
        margins = combo.lineEdit().textMargins()

        self.assertEqual(combo.currentText(), END_OF_DAY_DURATION_TEXT)
        self.assertEqual(combo.minimumWidth(), 0)
        self.assertEqual(combo.minimumContentsLength(), len(END_OF_DAY_DURATION_TEXT))
        self.assertEqual(combo.sizePolicy().horizontalPolicy(), QSizePolicy.Expanding)
        self.assertEqual(combo.lineEdit().cursorPosition(), 0)
        self.assertEqual((margins.left(), margins.top(), margins.right(), margins.bottom()), (0, 0, 0, 0))
        self.assertIn("QComboBox::drop-down", combo.styleSheet())


if __name__ == "__main__":
    unittest.main()
