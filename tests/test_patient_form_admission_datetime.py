from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtCore import QDate, QTime  # noqa: E402
from PySide6.QtWidgets import QApplication, QDateEdit, QTimeEdit  # noqa: E402

from rem_card.ui.patient_bed_management.patient_form import PatientForm  # noqa: E402
from rem_card.ui.patient_bed_management.tabs.general_tab import GeneralTabWidget  # noqa: E402
from rem_card.ui.shared.custom_message_box import CustomMessageBox  # noqa: E402


class PatientFormAdmissionDateTimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_admission_date_and_time_are_separate_fields_with_same_data_contract(self):
        widget = GeneralTabWidget()
        widget.admission_date_input.setDate(QDate(2026, 6, 24))
        widget.admission_time_input.setTime(QTime(7, 45))

        data = widget.get_data()

        self.assertIsInstance(widget.admission_date_input, QDateEdit)
        self.assertIsInstance(widget.admission_time_input, QTimeEdit)
        self.assertEqual(data["admission_datetime"], datetime(2026, 6, 24, 7, 45))
        self.assertIn("combo_arrow_down.svg", widget.admission_date_input.styleSheet())
        self.assertIn("decor_arrow_up.svg", widget.admission_time_input.styleSheet())

    def test_non_today_admission_warning_can_keep_form_open_for_editing_date(self):
        original_warning_with_actions = CustomMessageBox.__dict__["warning_with_actions"]
        calls = []

        def fake_warning_with_actions(cls, parent, title, message, action_buttons):
            calls.append(
                {
                    "parent": parent,
                    "title": title,
                    "message": message,
                    "action_buttons": action_buttons,
                }
            )
            return CustomMessageBox.No

        CustomMessageBox.warning_with_actions = classmethod(fake_warning_with_actions)
        try:
            form = PatientForm.__new__(PatientForm)
            form.is_new_admission = False
            admission_datetime = datetime.now() - timedelta(days=1)

            confirmed = form._confirm_non_today_admission_date(admission_datetime)
        finally:
            CustomMessageBox.warning_with_actions = original_warning_with_actions

        self.assertFalse(confirmed)
        self.assertEqual(calls[0]["title"], "Проверка даты поступления")
        self.assertIn("Продолжить редактирование", [text for text, _code in calls[0]["action_buttons"]])
        self.assertIn("Изменить дату", [text for text, _code in calls[0]["action_buttons"]])


if __name__ == "__main__":
    unittest.main()
