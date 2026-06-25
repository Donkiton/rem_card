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

from rem_card.ui.patient_bed_management.tabs.diagnosis_tab import DiagnosisTabWidget  # noqa: E402


class DummyMKBService:
    def get_diagnosis_by_code(self, code: str):
        return "Тестовый диагноз" if str(code).strip().upper() == "A00" else None


class PatientFormDiagnosisTabTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_manual_diagnosis_can_be_saved_without_mkb_code(self):
        widget = DiagnosisTabWidget(DummyMKBService(), show_operations=False)
        widget.diagnosis_text_input.setPlainText("Диагноз введен вручную")

        data = widget.get_data()

        self.assertIsNone(data["diagnosis_code"])
        self.assertEqual(data["diagnosis_text"], "Диагноз введен вручную")

    def test_manual_diagnosis_is_limited_to_500_characters(self):
        widget = DiagnosisTabWidget(DummyMKBService(), show_operations=False)

        widget.diagnosis_text_input.setPlainText("а" * 520)

        text = widget.diagnosis_text_input.toPlainText()
        self.assertEqual(len(text), 500)
        self.assertEqual(widget.manual_counter_label.text(), "500 / 500")


if __name__ == "__main__":
    unittest.main()

