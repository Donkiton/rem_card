from datetime import datetime, timedelta
from types import SimpleNamespace
from copy import deepcopy
import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QStackedWidget, QWidget

from rem_card.data.dto.remcard_dto import VentilationEventType, VentilationMode
from rem_card.ui.rem_card_sectors.sector_ivl import SectorIvl


START = datetime(2026, 10, 3, 8)


def event(identifier, kind, minute, **kwargs):
    values = dict(id=identifier, admission_id=1, ivl_episode_id=7,
                  timestamp=START + timedelta(minutes=minute), event_type=VentilationEventType(kind),
                  mode=VentilationMode.CPAP if kind in ("START_VENT", "MODE_CHANGE") else None,
                  parameters={}, extubation_reason=None, o2_flow=None, revision=3, author="Доктор")
    values.update(kwargs)
    return SimpleNamespace(**values)


@pytest.fixture
def widget():
    app = QApplication.instance() or QApplication([])
    panel = SectorIvl()
    panel.resize(1200, 650)
    panel.show()
    app.processEvents()
    panel.remcard_service = SimpleNamespace(get_mode_fields=lambda _mode: ["PEEP", "FiO2"])
    panel.admission_id = 1
    panel._input_context = (panel.remcard_service, 1)
    panel._snapshot_admission_datetime = START
    panel.active_case_id = 7
    panel._active_case_revision = 5
    panel._history_events = [event(1, "START_VENT", 10), event(2, "MODE_CHANGE", 20), event(3, "EXTUBATION", 40)]
    panel._populate_history_table(panel._sorted_history_events())
    yield panel
    panel.close()
    app.processEvents()


def select(widget, identifier, column=3):
    row = next(i for i in range(widget.history_table.rowCount()) if widget.history_table.item(i, 0).data(Qt.UserRole) == identifier)
    widget.history_table.cellDoubleClicked.emit(row, column)


@pytest.mark.parametrize("column", range(5))
def test_double_click_any_column_prefills_selected_event_after_sort(widget, column):
    widget._history_events[1].parameters = {"PEEP": 6, "FiO2": 40}
    widget._history_events[1].extubation_reason = "Уточнение"
    widget._on_history_header_clicked(0)
    select(widget, 2, column)
    assert widget._editing_event.id == 2
    assert widget.event_time_edit.dateTime().toPython() == START + timedelta(minutes=20)
    assert widget.param_widgets["PEEP"][1].text() == "6"
    assert widget.event_indications_edit.text() == "Уточнение"
    assert widget.event_card_title.text() == "Изменить событие ИВЛ"
    assert widget.btn_add_event.text() == "Изменить событие"
    assert widget.event_type_combo.count() == 2


@pytest.mark.parametrize("identifier,kind", [(1, "START_VENT"), (3, "EXTUBATION")])
def test_start_and_extubation_type_are_fixed(widget, identifier, kind):
    select(widget, identifier)
    assert widget.event_type_combo.currentData() == kind
    assert widget.event_type_combo.count() == 1 and not widget.event_type_combo.isEnabled()


def test_closed_old_event_save_uses_original_id_revision_and_time(widget):
    widget.active_case_id = None
    captured = {}
    widget.remcard_service.edit_ventilation_event = lambda identifier, **payload: captured.update(identifier=identifier, **payload)
    widget.refresh = lambda **_kwargs: None
    select(widget, 2)
    widget.param_widgets["PEEP"][1].setText("8")
    widget._on_add_event_clicked()
    assert captured["identifier"] == 2 and captured["expected_event_revision"] == 3
    assert captured["event_time"] == START + timedelta(minutes=20)
    assert captured["parameters"] == {"PEEP": 8}
    assert widget._editing_event is None


def test_editor_survives_snapshot_actions_and_cancel_clears_inputs(widget):
    select(widget, 2)
    widget.param_widgets["PEEP"][1].setText("9")
    widget._set_actions_enabled(True, True)
    assert widget.param_widgets["PEEP"][1].text() == "9"
    assert widget.event_time_edit.dateTime().toPython() == START + timedelta(minutes=20)
    assert widget.btn_undo.isEnabled()
    widget.btn_undo.click()
    assert widget._editing_event is None and widget.event_card_title.text() == "Новое событие ИВЛ"
    assert widget.param_widgets["PEEP"][1].text() == ""
    assert widget.event_type_combo.currentData() == "MODE_CHANGE"


def test_extubation_prefills_indications_and_oxygen(widget):
    widget._history_events[2].o2_flow = 3
    widget._history_events[2].extubation_reason = "Дыхание восстановлено"
    select(widget, 3)
    assert widget.event_o2_flow_edit.text() == "3"
    assert widget.event_o2_flow_edit.isVisible()
    assert widget.event_indications_edit.text() == "Дыхание восстановлено"


def test_patient_change_cancels_editor(widget):
    select(widget, 2)
    widget.set_runtime_context(admission_id=2)
    assert widget._editing_event is None
    assert widget.event_indications_edit.text() == ""


def test_failed_edit_preserves_draft_and_original_revision(widget, monkeypatch):
    from rem_card.ui.shared.custom_message_box import CustomMessageBox
    monkeypatch.setattr(CustomMessageBox, "warning", lambda *_args: None)
    select(widget, 2)
    widget.param_widgets["PEEP"][1].setText("9")
    callbacks = {}
    widget.remcard_service.enqueue_write = lambda **kwargs: callbacks.update(kwargs)
    widget._on_add_event_clicked()
    assert widget._ivl_write_pending
    callbacks["on_error"](ValueError("conflict"))
    assert widget._editing_event.id == 2 and widget._editing_event.revision == 3
    assert widget.param_widgets["PEEP"][1].text() == "9"
    assert widget.event_time_edit.dateTime().toPython() == START + timedelta(minutes=20)


def test_delayed_success_does_not_clear_another_patients_inputs(widget):
    select(widget, 2)
    callbacks = {}
    widget.remcard_service.enqueue_write = lambda **kwargs: callbacks.update(kwargs)
    widget._on_add_event_clicked()
    widget.set_runtime_context(admission_id=2)
    widget.event_indications_edit.setText("Другой пациент")
    callbacks["on_success"](None)
    assert widget._editing_event is None
    assert widget.event_indications_edit.text() == "Другой пациент"


def test_parent_patient_change_and_clear_cancel_editor_without_explicit_setter(widget):
    parent = QWidget()
    parent.remcard_service = widget.remcard_service
    parent.current_admission_id = 1
    widget.setParent(parent)
    # Reparenting hides the widget; start a fresh edit in the new context.
    select(widget, 2)
    parent.current_admission_id = 2
    widget.refresh()
    assert widget._editing_event is None
    assert widget.event_card_title.text() == "Новое событие ИВЛ"
    assert widget.event_indications_edit.text() == ""
    widget._refresh_timer.stop()
    widget.admission_id = parent.current_admission_id = 1
    widget._input_context = (widget.remcard_service, 1)
    widget._history_events = [event(2, "MODE_CHANGE", 20)]
    widget._populate_history_table(widget._history_events)
    select(widget, 2)
    parent.current_admission_id = None
    widget.refresh()
    assert widget._editing_event is None and widget.admission_id is None
    widget.setParent(None)


def test_leaving_tab_or_card_discards_editor(widget):
    stack = QStackedWidget()
    stack.addWidget(widget)
    other = QWidget()
    stack.addWidget(other)
    stack.show()
    QApplication.processEvents()
    widget._history_events = [event(2, "MODE_CHANGE", 20)]
    widget._populate_history_table(widget._history_events)
    select(widget, 2)
    stack.setCurrentWidget(other)
    assert widget._editing_event is None
    stack.setCurrentWidget(widget)
    widget._history_events = [event(2, "MODE_CHANGE", 20)]
    widget._populate_history_table(widget._history_events)
    select(widget, 2)
    stack.hide()
    assert widget._editing_event is None
    widget.setParent(None)
    stack.close()


@pytest.mark.parametrize("width", [900, 1200])
def test_extubation_fields_share_one_row_with_compact_time_type_and_oxygen(widget, width):
    widget.resize(width, 650)
    select(widget, 3)
    QApplication.processEvents()
    assert widget.event_time_edit.width() == 158
    assert widget.event_type_combo.width() == 138
    assert widget.event_indications_edit.width() > widget.event_time_edit.width()
    positions = [widget.event_time_edit, widget.event_type_combo, widget.event_indications_edit,
                 widget.event_o2_flow_edit, widget.btn_add_event]
    assert max(field.y() for field in positions) - min(field.y() for field in positions) <= 2
    assert widget.event_indications_edit.x() < widget.event_o2_flow_edit.x() < widget.btn_add_event.x()
    for before, after in zip(positions, positions[1:]):
        assert before.geometry().right() < after.geometry().left()


def test_upper_undo_discards_edit_without_removing_record_then_restores_normal_undo(widget):
    writes = []
    widget.remcard_service.enqueue_write = lambda **kwargs: writes.append(kwargs)
    select(widget, 2)
    widget.btn_undo.click()
    assert not writes
    assert len(widget._history_events) == 3
    assert widget._editing_event is None
    widget.btn_undo.click()
    assert writes[0]["description"] == "ivl_rollback_last_action:7"


@pytest.mark.parametrize("tab", ["ИВЛ", "Процедуры"])
def test_real_card_navigation_cancels_edit_on_click_but_preserves_refresh(widget, tab):
    from rem_card.ui.shared.remcard_layout import RemCardLayoutManager
    layout = RemCardLayoutManager()
    # Reuse the editor fixture inside the actual card's tab-navigation path.
    widget.refresh = lambda **_kwargs: None
    layout.current_admission_id = 1
    layout.sector_ivl = widget
    layout._ivl_initialized = True
    layout._ivl_layout.addWidget(widget)
    try:
        layout.set_active_tab("ИВЛ", source="refresh")
        select(widget, 2)
        layout.set_active_tab("ИВЛ", source="refresh")
        assert widget._editing_event is not None
        layout.set_active_tab(tab, source="click")
        assert widget._editing_event is None
        assert widget.event_card_title.text() == "Новое событие ИВЛ"
    finally:
        widget.setParent(None)
        layout.close()
        layout.deleteLater()


def cached_snapshot(widget, *, closed=False):
    case = SimpleNamespace(id=7, episode_number=1, revision=5, start_time=START + timedelta(minutes=10),
                           end_time=START + timedelta(minutes=40) if closed else None)
    events = list(widget._history_events if closed else widget._history_events[:2])
    summary = dict(case_duration_seconds=1800, total_duration_seconds=1800, tube_duration_seconds=1800, tube_alert=False)
    snapshot = widget._make_snapshot(summary=summary, timeline=events, latest_case=case,
                                    active_case=None if closed else case,
                                    active_case_events=[] if closed else events, admission_datetime=START)
    widget._store_snapshot(snapshot)
    widget._apply_snapshot(snapshot)
    return snapshot


def test_parameter_refresh_changes_one_cell_without_recreating_rows(widget):
    snapshot = cached_snapshot(widget)
    table = widget.history_table
    table.selectRow(0)
    items = [[table.item(row, col) for col in range(5)] for row in range(table.rowCount())]
    changes = []
    table.itemChanged.connect(lambda item: changes.append((item.row(), item.column())))
    updated = deepcopy(snapshot)
    updated["timeline"][1].parameters = {"PEEP": 8}
    widget._apply_snapshot(updated, preserve_inputs=True)
    assert changes == [(0, 3)]
    assert table.currentRow() == 0
    assert all(table.item(row, col) is item for row, cells in enumerate(items) for col, item in enumerate(cells))


def test_time_refresh_updates_time_and_derived_counters_without_recreating_rows(widget):
    snapshot = cached_snapshot(widget)
    items = [widget.history_table.item(1, col) for col in range(5)]
    changes = []
    widget.history_table.itemChanged.connect(lambda item: changes.append((item.row(), item.column())))
    updated = deepcopy(snapshot)
    moved = START + timedelta(minutes=5)
    updated["timeline"][0].timestamp = moved
    updated["active_case"].start_time = moved
    updated["summary"].update(case_duration_seconds=2100, total_duration_seconds=2100, tube_duration_seconds=900, tube_alert=True)
    widget._apply_snapshot(updated, preserve_inputs=True)
    assert changes == [(1, 0)]
    assert all(widget.history_table.item(1, col) is item for col, item in enumerate(items))
    assert widget.lbl_case_start.text() == moved.strftime("%d.%m.%Y %H:%M")
    assert widget.lbl_total_duration.text() == "00:35"
    assert "00:15" in widget.lbl_tube_duration.text()


def test_saved_edit_keeps_table_visible_and_cache_until_background_read_completes(widget, monkeypatch):
    import rem_card.ui.rem_card_sectors.sector_ivl as module
    snapshot = cached_snapshot(widget)
    original_item = widget.history_table.item(0, 3)
    updated = deepcopy(snapshot)
    updated["timeline"][1].parameters = {"PEEP": 9}
    updated["timeline"][1].revision = 4
    updated["active_case"].revision = 6
    widget.remcard_service.build_ivl_snapshot = lambda *_args, **_kwargs: updated
    writes = []
    widget.remcard_service.enqueue_write = lambda **kwargs: writes.append(kwargs)
    monkeypatch.setattr(module, "show_app_loading", lambda *_args, **_kwargs: pytest.fail("Global overlay during edit"))
    monkeypatch.setattr(widget, "set_loading_state", lambda *_args, **_kwargs: pytest.fail("Table cleared during edit"))
    select(widget, 2)
    widget.param_widgets["PEEP"][1].setText("9")
    widget._on_add_event_clicked()
    assert widget.history_table.item(0, 3) is original_item
    assert widget.btn_add_event.text() == "Сохранение…"
    writes[0]["on_success"](updated["timeline"][1])
    assert widget._editing_event is None and widget._get_cached_snapshot() is snapshot
    assert widget.event_card_title.text() == "Новое событие ИВЛ"
    deadline = time.monotonic() + 2
    while (widget._refresh_pending or widget._refresh_worker) and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.005)
    assert not widget._refresh_pending and widget._refresh_worker is None
    assert widget.history_table.item(0, 3) is original_item
    assert original_item.text() == "PEEP=9"
    assert widget._active_case_revision == 6


def test_same_snapshot_preserves_unrelated_new_event_input_and_restores_disabled_actions(widget):
    snapshot = cached_snapshot(widget)
    widget.event_time_edit.setDateTime(START + timedelta(minutes=25))
    widget.event_indications_edit.setText("Новый ввод")
    widget._set_ivl_write_controls_enabled(False)
    state = widget._capture_input_state()
    widget._apply_snapshot(snapshot, preserve_inputs=True)
    widget._restore_input_state(state)
    assert widget.btn_add_event.isEnabled()
    assert widget.event_time_edit.dateTime().toPython() == START + timedelta(minutes=25)
    assert widget.event_indications_edit.text() == "Новый ввод"
