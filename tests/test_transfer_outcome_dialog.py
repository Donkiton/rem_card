from datetime import datetime

import pytest
from PySide6.QtCore import QDate
from PySide6.QtWidgets import QApplication

from rem_card.ui.rem_card_sectors import outcome_dialogs as dialogs


@pytest.fixture
def dialog_factory(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(dialogs.TransferOutcomeDialog, "_restore_last_position", lambda self: None)
    monkeypatch.setattr(dialogs.TransferOutcomeDialog, "_save_last_position", lambda self: None)
    windows = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 24, 7, 0)

    monkeypatch.setattr(dialogs, "datetime", Clock)

    def make(context=None):
        dialog = dialogs.TransferOutcomeDialog(context or {}, datetime(2026, 9, 23, 8))
        windows.append(dialog)
        return dialog

    yield make
    for window in windows:
        window.hide()
        window.deleteLater()
    app.processEvents()


def test_previous_evening_is_saved_without_calendar_rollover(dialog_factory):
    dialog = dialog_factory()
    dialog.time_picker.set_time("19:00")
    dialog._on_accept()
    assert dialog.result_data["event_time"] == datetime(2026, 9, 23, 19)


def test_date_and_time_controls_are_independent(dialog_factory):
    dialog = dialog_factory()
    dialog.date_edit.setDate(QDate(2026, 9, 24))
    dialog.time_picker._hour_buttons[0].click()
    dialog.time_picker._minute_buttons[30].click()
    assert dialog._transfer_datetime() == datetime(2026, 9, 24, 0, 30)
    dialog.card_date_button.click()
    assert dialog._transfer_datetime() == datetime(2026, 9, 23, 0, 30)
    dialog.now_button.click()
    assert dialog._transfer_datetime() == datetime(2026, 9, 23, 7)


@pytest.mark.parametrize("key", ["latest_activity_datetime", "admission_datetime", "current_status_start_time"])
def test_conflict_does_not_silently_change_date(dialog_factory, monkeypatch, key):
    dialog = dialog_factory({key: "2026-09-23T22:00:00"})
    warnings = []
    monkeypatch.setattr(dialogs.CustomMessageBox, "warning", lambda *args: warnings.append(args))
    dialog.time_picker.set_time("19:00")
    dialog._on_accept()
    assert not dialog.result_data
    assert "23.09.2026 22:00" in warnings[0][-1]
    assert dialog.date_edit.date() == QDate(2026, 9, 23)


def test_late_card_deadline_still_applies_to_explicit_date(dialog_factory, monkeypatch):
    dialog = dialog_factory({
        "late_card_outcome": True,
        "late_card_shift_start": "2026-09-23T08:00:00",
        "late_outcome_deadline": "2026-09-24T10:00:00",
    })
    warnings = []
    monkeypatch.setattr(dialogs.CustomMessageBox, "warning", lambda *args: warnings.append(args))
    dialog.date_edit.setDate(QDate(2026, 9, 24))
    dialog.time_picker.set_time("10:01")
    dialog._on_accept()
    assert not dialog.result_data
    assert warnings
    dialog.time_picker.set_time("10:00")
    dialog._on_accept()
    assert dialog.result_data["event_time"] == datetime(2026, 9, 24, 10)


def test_destination_fields_expand_above_comment_and_clear_payload(dialog_factory):
    dialog = dialog_factory()
    dialog.show()
    QApplication.processEvents()
    assert dialog.lpu_combo.isHidden()
    assert dialog.lpu_other.isHidden()
    initial_y = dialog.comment_edit.y()
    dialog.department_combo.setCurrentText("Другое ЛПУ")
    dialog.lpu_combo.setCurrentText("Другое ЛПУ")
    QApplication.processEvents()
    assert not dialog.lpu_combo.isHidden()
    assert not dialog.lpu_other.isHidden()
    assert dialog.comment_edit.y() > initial_y
    assert dialog.comment_edit.y() >= dialog.lpu_other.geometry().bottom()
    dialog.lpu_other.setText("Тестовое ЛПУ")
    dialog.department_combo.setCurrentText("Хирургия")
    dialog.time_picker.set_time("19:00")
    dialog.comment_edit.setPlainText("Комментарий")
    dialog._on_accept()
    details = dialog.result_data["admission_details"]
    assert details["transfer_lpu"] is None
    assert details["transfer_lpu_other"] is None
    assert dialog.result_data["reason_text"].endswith("Комментарий")


def test_invalid_time_is_not_saved_as_previous_value(dialog_factory, monkeypatch):
    dialog = dialog_factory()
    monkeypatch.setattr(dialogs.CustomMessageBox, "warning", lambda *args: None)
    dialog.time_picker.input.setText("99:99")
    dialog.time_picker._commit_input()
    dialog._on_accept()
    assert not dialog.result_data
