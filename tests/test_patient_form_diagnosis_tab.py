from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


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

    def test_manual_diagnosis_is_locked_until_mkb_code_is_checked(self):
        widget = DiagnosisTabWidget(DummyMKBService(), show_operations=False)

        self.assertTrue(widget.diagnosis_text_input.isReadOnly())
        self.assertEqual(widget.diagnosis_text_input.toPlainText(), "")

    def test_valid_mkb_code_fills_and_locks_diagnosis_text(self):
        widget = DiagnosisTabWidget(DummyMKBService(), show_operations=False)
        widget.diagnosis_code_input.setText("A00")

        is_valid = widget._on_diagnosis_code_validation()
        data = widget.get_data()

        self.assertTrue(is_valid)
        self.assertTrue(widget.diagnosis_text_input.isReadOnly())
        self.assertEqual(widget.diagnosis_text_input.toPlainText(), "Тестовый диагноз")
        self.assertEqual(data["diagnosis_code"], "A00")
        self.assertEqual(data["diagnosis_text"], "Тестовый диагноз")

    def test_unknown_mkb_code_enables_manual_diagnosis(self):
        widget = DiagnosisTabWidget(DummyMKBService(), show_operations=False)
        widget.diagnosis_code_input.setText("B99")

        is_valid = widget._on_diagnosis_code_validation()
        widget.diagnosis_text_input.setPlainText("Диагноз введен вручную")
        data = widget.get_data()

        self.assertFalse(is_valid)
        self.assertFalse(widget.diagnosis_text_input.isReadOnly())
        self.assertEqual(data["diagnosis_code"], "B99")
        self.assertEqual(data["diagnosis_text"], "Диагноз введен вручную")

    def test_repeated_unknown_code_validation_keeps_manual_diagnosis(self):
        widget = DiagnosisTabWidget(DummyMKBService(), show_operations=False)
        widget.diagnosis_code_input.setText("B99")
        widget._on_diagnosis_code_validation()
        widget.diagnosis_text_input.setPlainText("Диагноз введен вручную")

        widget._on_diagnosis_code_validation()

        self.assertFalse(widget.diagnosis_text_input.isReadOnly())
        self.assertEqual(widget.diagnosis_text_input.toPlainText(), "Диагноз введен вручную")

    def test_code_change_clears_and_locks_previous_diagnosis(self):
        widget = DiagnosisTabWidget(DummyMKBService(), show_operations=False)
        widget.diagnosis_code_input.setText("B99")
        widget._on_diagnosis_code_validation()
        widget.diagnosis_text_input.setPlainText("Диагноз введен вручную")

        widget.diagnosis_code_input.setText("A00")

        self.assertTrue(widget.diagnosis_text_input.isReadOnly())
        self.assertEqual(widget.diagnosis_text_input.toPlainText(), "")

        widget._on_diagnosis_code_validation()

        self.assertEqual(widget.diagnosis_text_input.toPlainText(), "Тестовый диагноз")

    def test_existing_manual_diagnosis_without_code_remains_editable(self):
        widget = DiagnosisTabWidget(DummyMKBService(), show_operations=False)
        admission = SimpleNamespace(
            diagnosis_code=None,
            diagnosis_text="Сохраненный ручной диагноз",
        )

        widget.set_data(admission, [])

        self.assertFalse(widget.diagnosis_text_input.isReadOnly())
        self.assertEqual(widget.diagnosis_text_input.toPlainText(), "Сохраненный ручной диагноз")

        widget._on_diagnosis_code_validation()

        self.assertFalse(widget.diagnosis_text_input.isReadOnly())
        self.assertEqual(widget.diagnosis_text_input.toPlainText(), "Сохраненный ручной диагноз")

    def test_existing_valid_code_uses_current_mkb_diagnosis(self):
        widget = DiagnosisTabWidget(DummyMKBService(), show_operations=False)
        admission = SimpleNamespace(
            diagnosis_code="A00",
            diagnosis_text="Старый текст диагноза",
        )

        widget.set_data(admission, [])

        self.assertTrue(widget.diagnosis_text_input.isReadOnly())
        self.assertEqual(widget.diagnosis_text_input.toPlainText(), "Тестовый диагноз")

    def test_existing_unknown_code_keeps_manual_diagnosis_editable(self):
        widget = DiagnosisTabWidget(DummyMKBService(), show_operations=False)
        admission = SimpleNamespace(
            diagnosis_code="B99",
            diagnosis_text="Сохраненный ручной диагноз",
        )

        widget.set_data(admission, [])

        self.assertFalse(widget.diagnosis_text_input.isReadOnly())
        self.assertEqual(widget.diagnosis_text_input.toPlainText(), "Сохраненный ручной диагноз")

    def test_manual_diagnosis_is_limited_to_500_characters(self):
        widget = DiagnosisTabWidget(DummyMKBService(), show_operations=False)
        widget.diagnosis_code_input.setText("B99")
        widget._on_diagnosis_code_validation()

        widget.diagnosis_text_input.setPlainText("а" * 520)

        text = widget.diagnosis_text_input.toPlainText()
        self.assertEqual(len(text), 500)
        self.assertEqual(widget.manual_counter_label.text(), "500 / 500")


if __name__ == "__main__":
    unittest.main()

