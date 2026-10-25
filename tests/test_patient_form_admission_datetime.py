from __future__ import annotations

import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtCore import QDate, QDateTime, QTime, Qt  # noqa: E402
from PySide6.QtGui import QPalette  # noqa: E402
from PySide6.QtTest import QSignalSpy, QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractSpinBox, QApplication, QDateEdit, QFrame, QStyle, QStyleOptionFrame,
    QStyleOptionSpinBox, QTableView, QTimeEdit,
)

from rem_card.ui.patient_bed_management.patient_form import PatientForm  # noqa: E402
from rem_card.ui.patient_bed_management.tabs.general_tab import GeneralTabWidget  # noqa: E402
from rem_card.ui.shared.custom_message_box import CustomMessageBox  # noqa: E402
from rem_card.ui.styles import theme_runtime  # noqa: E402
from rem_card.ui.styles.theme import STYLE_PATIENT_FORM_TAB  # noqa: E402
from rem_card.ui.styles.theme_presets import build_tokens  # noqa: E402


@pytest.fixture(params=["light", "dark"])
def visible_general_tab(request):
    app = QApplication.instance() or QApplication([])
    previous_mode = theme_runtime.current_mode()
    previous_tokens = dict(theme_runtime._active_tokens)
    previous_palette = QPalette(app.palette())
    theme_runtime.install_theme_runtime(app)
    theme_runtime.refresh_registered_styles(
        request.param, force=True, tokens=build_tokens(mode=request.param),
    )
    widget = None
    try:
        widget = GeneralTabWidget()
        theme_runtime.set_widget_style(widget, STYLE_PATIENT_FORM_TAB)
        widget.admission_datetime_input.setDateTime(QDateTime(2026, 6, 24, 7, 45, 0))
        widget.show()
        app.processEvents()
        yield app, widget
    finally:
        if widget is not None:
            # Accessing popup can create it lazily; hide any already open owned window.
            for child in widget.findChildren(QFrame):
                if child.isWindow():
                    child.hide()
            widget.hide()
        theme_runtime.refresh_registered_styles(previous_mode, force=True, tokens=previous_tokens)
        app.setPalette(previous_palette)


def _assert_editor_text_fits(editor):
    line_edit = editor.lineEdit()
    option = QStyleOptionFrame()
    line_edit.initStyleOption(option)
    content = line_edit.style().subElementRect(QStyle.SE_LineEditContents, option, line_edit)
    margins = line_edit.textMargins()

    assert editor.isVisible()
    available_width = content.width() - margins.left() - margins.right()
    assert available_width >= line_edit.fontMetrics().horizontalAdvance(line_edit.text()) + 4
    assert content.height() >= line_edit.fontMetrics().height()


def test_visible_admission_datetime_text_fits_form_field(visible_general_tab):
    _app, widget = visible_general_tab
    editor = widget.admission_datetime_input
    assert editor.lineEdit().text() == "24.06.2026 07:45"
    _assert_editor_text_fits(editor)


@pytest.mark.parametrize("trigger", ["clock", "alt_down", "f4"])
def test_admission_popup_opens_from_clock_and_keyboard(visible_general_tab, trigger):
    app, widget = visible_general_tab
    editor = widget.admission_datetime_input
    field = widget.admission_datetime_field
    assert field.editor is editor
    assert not editor.calendarPopup()
    assert editor.buttonSymbols() == QAbstractSpinBox.UpDownArrows

    option = QStyleOptionSpinBox()
    editor.initStyleOption(option)
    for control in (QStyle.SC_SpinBoxUp, QStyle.SC_SpinBoxDown):
        rect = editor.style().subControlRect(QStyle.CC_SpinBox, option, control, editor)
        assert rect.isEmpty()

    if trigger == "clock":
        QTest.mouseClick(field.calendar_button, Qt.LeftButton)
    else:
        editor.setFocus()
        key, modifiers = (Qt.Key_Down, Qt.AltModifier) if trigger == "alt_down" else (Qt.Key_F4, Qt.NoModifier)
        QTest.keyClick(editor, key, modifiers)
    app.processEvents()
    popup = field.popup
    assert popup.isVisible()
    assert popup.calendar.selectedDate() == QDate(2026, 6, 24)
    assert popup.time_input.time() == QTime(7, 45)
    _assert_editor_text_fits(popup.time_input)


def test_admission_popup_only_applies_selected_datetime_on_confirmation(visible_general_tab):
    app, widget = visible_general_tab
    field = widget.admission_datetime_field
    editor = widget.admission_datetime_input
    changed = QSignalSpy(editor.dateTimeChanged)
    QTest.mouseClick(field.calendar_button, Qt.LeftButton)
    app.processEvents()
    popup = field.popup

    calendar_view = popup.calendar.findChild(QTableView)
    assert calendar_view is not None
    QTest.keyClick(calendar_view, Qt.Key_Right)
    QTest.keyClick(calendar_view, Qt.Key_Return)
    popup.time_input.setCurrentSection(QTimeEdit.MinuteSection)
    QTest.keyClick(popup.time_input, Qt.Key_Up)
    app.processEvents()

    assert popup.isVisible()
    assert popup.calendar.selectedDate() == QDate(2026, 6, 25)
    assert popup.time_input.time() == QTime(7, 46)
    assert changed.count() == 0
    assert widget.get_data()["admission_datetime"] == datetime(2026, 6, 24, 7, 45)

    QTest.mouseClick(popup.apply_button, Qt.LeftButton)
    app.processEvents()

    assert not popup.isVisible()
    assert changed.count() == 1
    assert widget.get_data()["admission_datetime"] == datetime(2026, 6, 25, 7, 46)
    assert widget.admission_date_input.date() == QDate(2026, 6, 25)
    assert widget.admission_time_input.time() == QTime(7, 46)

    editor.setCurrentSection(QTimeEdit.MinuteSection)
    QTest.keyClick(editor, Qt.Key_Up)
    app.processEvents()
    assert widget.get_data()["admission_datetime"] == datetime(2026, 6, 25, 7, 47)


@pytest.mark.parametrize("dismissal", ["cancel", "escape", "hide"])
def test_admission_popup_discards_draft_and_reopens_current_legacy_date(visible_general_tab, dismissal):
    app, widget = visible_general_tab
    field = widget.admission_datetime_field
    widget.set_data(None, SimpleNamespace(
        history_number="", admission_datetime="1946-02-17 05:20:00",
        patient_gender="", patient_age=None, patient_age_unit=None, patient_months=None,
        department_profile="", source_department="",
    ))
    original = widget.get_data()["admission_datetime"]
    field.open_popup()
    app.processEvents()
    popup = field.popup
    assert popup.calendar.selectedDate() == QDate(1946, 2, 17)
    assert popup.time_input.time() == QTime(5, 20)
    popup.calendar.setSelectedDate(QDate(2027, 7, 1))
    popup.time_input.setTime(QTime(22, 15))

    if dismissal == "cancel":
        QTest.mouseClick(popup.cancel_button, Qt.LeftButton)
    elif dismissal == "escape":
        QTest.keyClick(popup.time_input, Qt.Key_Escape)
    else:
        popup.hide()
    app.processEvents()

    assert not popup.isVisible()
    assert widget.get_data()["admission_datetime"] == original
    widget.admission_time_input.setTime(QTime(6, 10))
    field.open_popup()
    app.processEvents()

    assert popup.calendar.selectedDate() == QDate(1946, 2, 17)
    assert popup.time_input.time() == QTime(6, 10)


def test_admission_popup_month_navigation_preserves_form_value(visible_general_tab):
    app, widget = visible_general_tab
    field = widget.admission_datetime_field
    original = widget.get_data()["admission_datetime"]
    field.open_popup()
    app.processEvents()
    popup = field.popup
    month_text = popup.month_label.text()

    QTest.mouseClick(popup.next_button, Qt.LeftButton)
    app.processEvents()
    assert (popup.calendar.yearShown(), popup.calendar.monthShown()) == (2026, 7)
    assert popup.month_label.text() != month_text
    QTest.mouseClick(popup.previous_button, Qt.LeftButton)
    app.processEvents()

    assert (popup.calendar.yearShown(), popup.calendar.monthShown()) == (2026, 6)
    assert popup.month_label.text() == month_text
    assert widget.get_data()["admission_datetime"] == original


def test_admission_popup_updates_open_and_hidden_surfaces_with_theme(visible_general_tab):
    app, widget = visible_general_tab
    field = widget.admission_datetime_field
    field.open_popup()
    app.processEvents()
    popup = field.popup
    original_mode = theme_runtime.current_mode()
    original_style = popup.styleSheet()
    original_palette = QPalette(popup.calendar.palette())
    popup.calendar.setSelectedDate(QDate(2026, 7, 4))
    popup.time_input.setTime(QTime(10, 30))
    other_mode = "dark" if original_mode == "light" else "light"

    theme_runtime.refresh_registered_styles(other_mode, force=True, tokens=build_tokens(mode=other_mode))
    app.processEvents()

    assert popup.isVisible()
    assert popup.styleSheet() != original_style
    assert popup.calendar.palette().color(QPalette.Base) != original_palette.color(QPalette.Base)
    assert popup.calendar.palette().color(QPalette.Text) != original_palette.color(QPalette.Text)
    assert popup.calendar.selectedDate() == QDate(2026, 7, 4)
    assert popup.time_input.time() == QTime(10, 30)
    assert widget.get_data()["admission_datetime"] == datetime(2026, 6, 24, 7, 45)

    QTest.mouseClick(popup.cancel_button, Qt.LeftButton)
    theme_runtime.refresh_registered_styles(original_mode, force=True, tokens=build_tokens(mode=original_mode))
    field.open_popup()
    app.processEvents()

    assert popup.styleSheet() == original_style
    assert popup.calendar.palette().color(QPalette.Base) == original_palette.color(QPalette.Base)
    assert popup.calendar.selectedDate() == QDate(2026, 6, 24)


def test_admission_popup_stays_inside_available_screen(visible_general_tab):
    app, widget = visible_general_tab
    available = widget.screen().availableGeometry()
    widget.move(available.right() - widget.width() + 1, available.bottom() - widget.height() + 1)
    widget.admission_datetime_field.open_popup()
    app.processEvents()

    popup = widget.admission_datetime_field.popup
    assert popup.isVisible()
    assert available.contains(popup.frameGeometry())


def test_deleting_parent_with_open_admission_popup_exits_cleanly():
    script = r'''
import sys
sys.path.insert(0, sys.argv[1])
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDialog, QVBoxLayout
from shiboken6 import isValid
from rem_card.ui.patient_bed_management.admission_datetime_field import AdmissionDateTimeField

app = QApplication([])
parent = QDialog()
layout = QVBoxLayout(parent)
field = AdmissionDateTimeField(parent)
layout.addWidget(field)
parent.show()
app.processEvents()
field.open_popup()
app.processEvents()
assert field.popup.isVisible()

parent.deleteLater()
QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
app.processEvents()
assert not isValid(parent)
assert not isValid(field)
print("ADMISSION_POPUP_PARENT_DESTROYED")
'''
    environment = dict(os.environ)
    environment.update({"QT_QPA_PLATFORM": "offscreen", "PYTHONDONTWRITEBYTECODE": "1"})
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(PROJECT_DIR)],
        cwd=PROJECT_DIR, env=environment, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=20,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ADMISSION_POPUP_PARENT_DESTROYED" in result.stdout


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
