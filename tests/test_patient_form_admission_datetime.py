from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtCore import QDate, QDateTime, QPoint, QTime, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QDateEdit, QStyle, QStyleOptionFrame, QTableView, QTimeEdit,
)

from rem_card.ui.patient_bed_management.patient_form import PatientForm  # noqa: E402
from rem_card.ui.patient_bed_management.tabs.general_tab import GeneralTabWidget  # noqa: E402
from rem_card.ui.shared.custom_message_box import CustomMessageBox  # noqa: E402
from rem_card.ui.styles import theme_runtime  # noqa: E402
from rem_card.ui.styles.theme import STYLE_PATIENT_FORM_TAB  # noqa: E402


@pytest.fixture(params=["light", "dark"])
def visible_general_tab(request):
    app = QApplication.instance() or QApplication([])
    with patch.object(theme_runtime, "_mode", request.param):
        widget = GeneralTabWidget()
        theme_runtime.set_widget_style(widget, STYLE_PATIENT_FORM_TAB)
        widget.admission_datetime_input.setDateTime(QDateTime(2026, 6, 24, 7, 45, 0))
        widget.show()
        app.processEvents()
        try:
            yield app, widget
        finally:
            widget.hide()


def test_visible_admission_datetime_text_fits_form_field(visible_general_tab):
    _app, widget = visible_general_tab
    editor = widget.admission_datetime_input
    line_edit = editor.lineEdit()
    option = QStyleOptionFrame()
    line_edit.initStyleOption(option)
    content = line_edit.style().subElementRect(QStyle.SE_LineEditContents, option, line_edit)
    margins = line_edit.textMargins()

    assert editor.isVisible()
    assert line_edit.text() == "24.06.2026 07:45"
    available_width = content.width() - margins.left() - margins.right()
    assert available_width >= line_edit.fontMetrics().horizontalAdvance(line_edit.text()) + 4
    assert content.height() >= line_edit.fontMetrics().height()


def test_visible_admission_calendar_and_time_edit_update_form_data(visible_general_tab):
    app, widget = visible_general_tab
    editor = widget.admission_datetime_input
    assert editor.calendarPopup()

    QTest.mouseClick(editor, Qt.LeftButton, Qt.NoModifier, QPoint(editor.width() - 8, editor.height() // 2))
    app.processEvents()
    calendar = editor.calendarWidget()
    assert calendar.isVisible()
    assert calendar.selectedDate() == QDate(2026, 6, 24)

    calendar_view = calendar.findChild(QTableView)
    assert calendar_view is not None
    QTest.keyClick(calendar_view, Qt.Key_Right)
    QTest.keyClick(calendar_view, Qt.Key_Return)
    app.processEvents()

    assert not calendar.isVisible()
    assert widget.get_data()["admission_datetime"] == datetime(2026, 6, 25, 7, 45)

    line_edit = editor.lineEdit()
    minute_x = line_edit.fontMetrics().horizontalAdvance(line_edit.text()[:14]) + 3
    QTest.mouseClick(line_edit, Qt.LeftButton, Qt.NoModifier, QPoint(minute_x, line_edit.height() // 2))
    QTest.keyClick(line_edit, Qt.Key_Up)
    app.processEvents()

    assert widget.get_data()["admission_datetime"] == datetime(2026, 6, 25, 7, 46)


class PatientFormAdmissionDateTimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_legacy_admission_date_and_time_fields_keep_data_contract(self):
        widget = GeneralTabWidget()
        widget.admission_date_input.setDate(QDate(2026, 6, 24))
        widget.admission_time_input.setTime(QTime(7, 45))

        data = widget.get_data()

        self.assertIsInstance(widget.admission_date_input, QDateEdit)
        self.assertIsInstance(widget.admission_time_input, QTimeEdit)
        self.assertEqual(data["admission_datetime"], datetime(2026, 6, 24, 7, 45))
        self.assertIn("combo_arrow_down.svg", widget.admission_date_input.styleSheet())
        self.assertIn("spin_arrow_up.svg", widget.admission_time_input.styleSheet())

    def test_loaded_long_patient_name_is_positioned_at_start(self):
        widget = GeneralTabWidget()
        patient = SimpleNamespace(
            full_name="Оченьдлиннаяфамилия-Составная Иван Иванович-Сергеевич",
            birth_date=None,
        )

        widget.set_data(patient, None)

        self.assertEqual(widget.full_name_input.cursorPosition(), 0)

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
