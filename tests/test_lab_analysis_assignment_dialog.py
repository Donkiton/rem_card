from __future__ import annotations

import os
from datetime import datetime, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings, QTime
from PySide6.QtWidgets import QApplication

from rem_card.ui.rem_card_sectors.lab_analysis_dialog import AddLabAnalysisDialog
from rem_card.ui.shared.custom_message_box import CustomMessageBox


class _LabDialogService:
    def list_lab_materials(self):
        return [
            {"code": "venous_blood", "label": "Кровь венозная"},
            {"code": "arterial_blood", "label": "Кровь артериальная"},
        ]

    def list_lab_analysis_templates(self):
        return [
            {
                "id": 1,
                "code": "glucose",
                "name": "Глюкоза крови",
                "material": "venous_blood",
                "material_label": "Кровь венозная",
                "default_times": ["10:00"],
            },
            {
                "id": 2,
                "code": "biochemistry",
                "name": "Биохимический анализ крови",
                "material": "venous_blood",
                "material_label": "Кровь венозная",
                "default_times": ["12:00"],
            },
        ]

    @staticmethod
    def get_day_period(value):
        start = value.replace(hour=8, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)


def _application():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def isolated_lab_dialog_settings(tmp_path, monkeypatch):
    settings = QSettings(str(tmp_path / "lab-dialog.ini"), QSettings.IniFormat)
    monkeypatch.setattr(AddLabAnalysisDialog, "_settings", lambda _self: settings)
    yield settings
    settings.clear()


def _dialog():
    app = _application()
    dialog = AddLabAnalysisDialog(
        _LabDialogService(),
        admission_id=17,
        card_date=datetime(2026, 8, 29, 8, 0),
    )
    dialog.show()
    app.processEvents()
    return app, dialog


def test_catalog_selection_does_not_add_order_without_confirmation():
    app, dialog = _dialog()
    try:
        dialog.catalog_list.setCurrentRow(0)
        app.processEvents()

        assert dialog._current_draft().analysis_name == "Глюкоза крови"
        assert dialog.queue_table.rowCount() == 0
        assert dialog._drafts == {}
        assert dialog.editor_action_button.text() == "Добавить назначение"
        assert dialog.editor_action_button.objectName() == "lab_dialog_tertiary"
        assert dialog.editor_action_button.parentWidget() is dialog.clear_queue_button.parentWidget()
        assert dialog.editor_action_button.parentWidget() is dialog.queue_title_label.parentWidget()

        dialog._add_or_update_current()
        app.processEvents()

        assert dialog.queue_table.rowCount() == 1
        assert list(dialog._drafts) == ["template:1"]
        assert dialog.editor_action_button.text() == "Сохранить изменения"
        assert dialog.save_button.text() == "Передать 1 назначение"
    finally:
        dialog.close()
        app.processEvents()


def test_recurrence_replaces_only_series_and_preserves_manual_times():
    app, dialog = _dialog()
    try:
        dialog.catalog_list.setCurrentRow(0)
        app.processEvents()
        dialog._clear_current_times()
        dialog._add_manual_time("10:30")
        dialog.recurrence_start_edit.setTime(QTime(11, 0))

        dialog._apply_interval_schedule(2)
        assert dialog._current_draft().times == [
            "10:30",
            "11:00",
            "13:00",
            "15:00",
            "17:00",
            "19:00",
            "21:00",
            "23:00",
            "01:00",
            "03:00",
            "05:00",
            "07:00",
        ]

        dialog._apply_interval_schedule(3)
        assert dialog._current_draft().manual_times == ["10:30"]
        assert dialog._current_draft().times == [
            "10:30",
            "11:00",
            "14:00",
            "17:00",
            "20:00",
            "23:00",
            "02:00",
            "05:00",
        ]
    finally:
        dialog.close()
        app.processEvents()


def test_removing_one_recurrence_time_keeps_the_rest_of_series():
    app, dialog = _dialog()
    try:
        dialog.catalog_list.setCurrentRow(0)
        app.processEvents()
        dialog._clear_current_times()
        dialog.recurrence_start_edit.setTime(QTime(11, 0))
        dialog._apply_interval_schedule(2)

        dialog._delete_time("15:00")

        assert "15:00" not in dialog._current_draft().times
        assert "13:00" in dialog._current_draft().times
        assert "17:00" in dialog._current_draft().times
        assert dialog._current_draft().recurrence_excluded_times == ["15:00"]
    finally:
        dialog.close()
        app.processEvents()


def test_queue_edits_are_committed_only_by_save_changes():
    app, dialog = _dialog()
    try:
        dialog.catalog_list.setCurrentRow(0)
        app.processEvents()
        dialog._add_or_update_current()

        dialog.comment_input.setPlainText("Контроль после коррекции")
        app.processEvents()
        assert dialog._drafts["template:1"].comment == ""

        dialog._add_or_update_current()
        assert dialog._drafts["template:1"].comment == "Контроль после коррекции"
    finally:
        dialog.close()
        app.processEvents()


def test_transfer_is_blocked_while_queue_editor_has_unsaved_changes(monkeypatch):
    app, dialog = _dialog()
    warnings = []
    monkeypatch.setattr(
        CustomMessageBox,
        "warning",
        staticmethod(lambda _parent, title, message: warnings.append((title, message))),
    )
    try:
        dialog.catalog_list.setCurrentRow(0)
        app.processEvents()
        dialog._add_or_update_current()
        dialog.comment_input.setPlainText("Несохранённая правка")
        app.processEvents()

        dialog._save()

        assert warnings == [("Анализы", "Сначала сохраните изменения выбранного назначения.")]
    finally:
        dialog.close()
        app.processEvents()


def test_after_midnight_time_belongs_to_next_part_of_card_day():
    app, dialog = _dialog()
    try:
        scheduled = dialog._scheduled_datetime("01:00")
        assert scheduled == datetime(2026, 8, 30, 1, 0)
    finally:
        dialog.close()
        app.processEvents()


def test_dialog_is_resizable_scrollable_and_restores_geometry(isolated_lab_dialog_settings):
    app, first = _dialog()
    assert first.isSizeGripEnabled() is True
    assert first.minimumWidth() == 760
    assert first.minimumHeight() == 500
    assert first.maximumWidth() > first.minimumWidth()
    assert first.maximumHeight() > first.minimumHeight()

    first.resize(760, 540)
    first.catalog_list.setCurrentRow(0)
    app.processEvents()
    assert first.editor_scroll.verticalScrollBar().maximum() > 0
    first.close()
    app.processEvents()

    _app, restored = _dialog()
    try:
        assert restored.size().width() == 760
        assert restored.size().height() == 540
        assert restored.editor_scroll.widget() is restored.details_stack
    finally:
        restored.close()
        app.processEvents()


def test_compact_editor_keeps_schedule_preview_visible_below_two_time_modes():
    app, dialog = _dialog()
    try:
        dialog.resize(760, 500)
        dialog.catalog_list.setCurrentRow(0)
        app.processEvents()

        assert dialog.material_combo.maximumWidth() == 260
        assert dialog.schedule_modes_layout.itemAt(0).widget() is dialog.quick_schedule_block
        assert dialog.schedule_modes_layout.itemAt(1).widget() is dialog.recurrence_schedule_block
        quick_position = dialog.schedule_modes_layout.getItemPosition(
            dialog.schedule_modes_layout.indexOf(dialog.quick_schedule_block)
        )
        recurrence_position = dialog.schedule_modes_layout.getItemPosition(
            dialog.schedule_modes_layout.indexOf(dialog.recurrence_schedule_block)
        )
        assert dialog._compact_editor_layout is True
        assert quick_position[:2] == (0, 0)
        assert recurrence_position[:2] == (1, 0)
        assert dialog.editor_scroll.horizontalScrollBar().maximum() == 0
        assert dialog.schedule_preview.isVisible()
        assert dialog.times_list.isVisible()
        assert dialog.editor_action_button.width() >= dialog.editor_action_button.sizeHint().width()
        assert dialog.clear_queue_button.width() >= dialog.clear_queue_button.sizeHint().width()
        preview_bottom_gap = (
            dialog.schedule_preview.parentWidget().contentsRect().bottom()
            - dialog.schedule_preview.geometry().bottom()
        )
        assert preview_bottom_gap <= 18
        assert "QScrollArea#lab_editor_scroll QScrollBar:vertical" in dialog.content_widget.styleSheet()

        dialog.resize(1040, 640)
        app.processEvents()
        recurrence_position = dialog.schedule_modes_layout.getItemPosition(
            dialog.schedule_modes_layout.indexOf(dialog.recurrence_schedule_block)
        )
        assert dialog._compact_editor_layout is False
        assert recurrence_position[:2] == (0, 1)
        assert dialog.quick_schedule_block.layout().itemAt(2).layout() is dialog.exact_time_row
        assert dialog.recurrence_schedule_block.layout().itemAt(2).layout() is dialog.recurrence_start_row
        assert dialog.times_list.minimumHeight() == 96
        assert dialog.times_list.maximumHeight() > 1000
        initial_preview_height = dialog.schedule_preview.height()

        dialog._clear_current_times()
        dialog.recurrence_start_edit.setTime(QTime(9, 0))
        dialog.every_hour_button.click()
        app.processEvents()
        assert dialog.times_list.height() >= 96

        dialog.resize(1200, 760)
        app.processEvents()
        assert initial_preview_height <= 160
        assert dialog.schedule_preview.height() <= 160
    finally:
        dialog.close()
        app.processEvents()


def test_time_buttons_confirm_click_without_changing_checked_button_geometry():
    app, dialog = _dialog()
    try:
        dialog.catalog_list.setCurrentRow(0)
        app.processEvents()
        dialog._clear_current_times()

        dialog.plus_one_button.click()
        app.processEvents()
        assert len(dialog._current_draft().times) == 1
        assert dialog.plus_one_button.property("labFeedback") == "true"

        initial_size_hint = dialog.every_two_hours_button.sizeHint()
        dialog.every_two_hours_button.click()
        app.processEvents()
        assert dialog.every_two_hours_button.isChecked()
        assert dialog.every_two_hours_button.property("labFeedback") == "true"
        assert dialog.every_two_hours_button.sizeHint() == initial_size_hint
        assert "QPushButton#lab_recurrence_preset:checked" not in dialog.content_widget.styleSheet()

        dialog.clear_recurrence_button.click()
        app.processEvents()
        assert dialog._current_draft().recurrence_interval_hours is None
        assert dialog.clear_recurrence_button.property("labFeedback") == "true"
    finally:
        dialog.close()
        app.processEvents()


def test_queue_rows_and_remove_button_have_readable_height():
    app, dialog = _dialog()
    try:
        dialog.catalog_list.setCurrentRow(0)
        app.processEvents()
        dialog._add_or_update_current()
        app.processEvents()

        remove_button = dialog.queue_table.cellWidget(0, 3)
        assert dialog.queue_table.height() == 84
        assert dialog.queue_table.rowHeight(0) == 48
        assert remove_button.height() >= 34
        assert remove_button.sizeHint().height() <= dialog.queue_table.rowHeight(0)
    finally:
        dialog.close()
        app.processEvents()


def test_queue_header_keeps_actions_on_one_neutral_row():
    app, dialog = _dialog()
    try:
        dialog.catalog_list.setCurrentRow(0)
        app.processEvents()
        dialog._add_or_update_current()
        app.processEvents()

        assert dialog.editor_action_button.parentWidget() is dialog.clear_queue_button.parentWidget()
        assert dialog.editor_action_button.objectName() == "lab_dialog_tertiary"
        assert dialog.clear_queue_button.objectName() == "lab_dialog_tertiary"
        assert dialog.editor_action_button.isVisible()
        assert dialog.clear_queue_button.isVisible()
        assert dialog.queue_title_label.text() == "Назначения к передаче · 1"
        assert dialog.editor_action_button.width() >= dialog.editor_action_button.sizeHint().width()
        assert dialog.clear_queue_button.width() >= dialog.clear_queue_button.sizeHint().width()
    finally:
        dialog.close()
        app.processEvents()


def test_queue_height_is_capped_so_editor_does_not_collapse():
    app, dialog = _dialog()
    try:
        dialog.resize(1040, 640)
        app.processEvents()
        dialog.catalog_list.setCurrentRow(0)
        app.processEvents()
        dialog._add_or_update_current()
        dialog.catalog_list.setCurrentRow(1)
        app.processEvents()
        dialog._add_or_update_current()
        app.processEvents()

        assert dialog.queue_table.height() == 132
        assert dialog.queue_table.height() <= 140
        assert dialog.editor_scroll.height() >= 180
        assert dialog.queue_table.rowHeight(0) == 48
        assert dialog.queue_table.rowHeight(1) == 48

        dialog.resize(760, 500)
        app.processEvents()
        assert dialog.queue_table.height() == 84
        assert dialog.editor_scroll.verticalScrollBar().maximum() >= 0
        assert dialog.schedule_preview.isVisible()
    finally:
        dialog.close()
        app.processEvents()
