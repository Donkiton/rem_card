"""Editor state for an existing event in the IVL journal."""
from datetime import datetime

from PySide6.QtCore import QDateTime, Qt
from PySide6.QtWidgets import QSizePolicy

from rem_card.ui.shared.custom_message_box import CustomMessageBox


class IvlEventEditorMixin:
    def _on_history_double_clicked(self, row, _column):
        if self._ivl_write_pending:
            return
        item = self.history_table.item(row, 0)
        event_id = item.data(Qt.UserRole) if item else None
        event = next((event for event in self._history_events if event.id == event_id), None)
        if event is None:
            return
        self._editing_event = event
        self._editing_context = (self.remcard_service, self.admission_id)
        self._editing_case_revision = (
            self._active_case_revision if event.ivl_episode_id == self.active_case_id
            else self._latest_case_revision
            if self._snapshot_latest_case and event.ivl_episode_id == self._snapshot_latest_case.id
            else None
        )
        code = event.event_type.value
        self._configure_event_editor()
        self._restore_combo_data(self.event_type_combo, code)
        self.mode_combo.setCurrentIndex(0)
        if event.mode:
            self._restore_combo_data(self.mode_combo, event.mode.value)
        for name, (_label, edit) in self.param_widgets.items():
            value = (event.parameters or {}).get(name)
            edit.setText(str(value) if value is not None else "")
        self.event_indications_edit.setText(event.extubation_reason or "")
        self.event_o2_flow_edit.setText(str(event.o2_flow) if event.o2_flow is not None else "")
        self.event_time_edit.setDateTime(QDateTime(event.timestamp))

    def _configure_event_editor(self):
        event = self._editing_event
        if event is None:
            return
        self.event_card_title.setText("Изменить событие ИВЛ")
        self.btn_add_event.setText("Изменить событие")
        self.btn_undo.setEnabled(not self._ivl_write_pending)
        self.btn_undo.setToolTip("Отменить редактирование события ИВЛ")
        for button in (self.btn_create_case, self.btn_replace_tube, self.btn_close_case):
            button.setEnabled(False)
        self.btn_add_event.setEnabled(not self._ivl_write_pending)
        code = event.event_type.value
        codes = self.ACTIVE_EVENT_CODES if code in self.ACTIVE_EVENT_CODES else (code,)
        # Do not reset the mode/parameters on background snapshot refreshes.
        if tuple(self.event_type_combo.itemData(i) for i in range(self.event_type_combo.count())) != codes:
            self._populate_event_types(codes)
        self.event_type_combo.setEnabled(len(codes) > 1)
        fixed_time = getattr(event, "author", None) == "SYSTEM_OUTCOME"
        case = self._snapshot_latest_case
        if code == "START_VENT" and case and case.id == event.ivl_episode_id:
            fixed_time |= getattr(getattr(case, "start_type", None), "value", None) == "ADMISSION"
        self.event_time_edit.setEnabled(not fixed_time)
        self.event_time_edit.setToolTip("Время задаётся исходом пациента" if getattr(event, "author", None) == "SYSTEM_OUTCOME" else "")
        self._apply_event_time_constraints()
        self._on_event_type_changed()

    def _reset_event_editor(self):
        self._editing_event = None
        self._editing_context = None
        self._editing_case_revision = None
        self.event_card_title.setText("Новое событие ИВЛ")
        self.btn_add_event.setText("⊕  Добавить событие")
        self.btn_undo.setToolTip("Отменить последнее действие ИВЛ")
        self.event_time_edit.setToolTip("")
        self.event_time_edit.clearMaximumDateTime()
        self.event_time_edit.clearMinimumDateTime()
        self.event_o2_flow_edit.clear()
        self.mode_combo.setCurrentIndex(0)
        self.event_indications_edit.clear()
        for _label, edit in self.param_widgets.values():
            edit.clear()
        self._set_actions_enabled(bool(self.active_case_id), bool(self._history_events))

    def _cancel_event_edit(self):
        if not self._ivl_write_pending:
            self.cancel_event_edit()

    def cancel_event_edit(self):
        """Discard editor input on navigation; never undo a clinical record."""
        if self._editing_event is not None:
            self._reset_event_editor()

    def _apply_event_editor_layout(self, extubation):
        grid = self.event_grid
        if extubation:
            self.event_time_edit.setFixedWidth(158)
            self.event_type_combo.setFixedWidth(138)
            self.event_time_edit.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            self.event_type_combo.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            grid.setColumnMinimumWidth(0, 158)
            grid.setColumnMinimumWidth(1, 138)
            self.event_indications_edit.setMaximumWidth(16777215)
            grid.addWidget(self.lbl_event_o2_flow, 0, 4)
            grid.addWidget(self.event_o2_flow_edit, 1, 4)
            grid.addWidget(self.btn_add_event, 1, 5)
            grid.setColumnStretch(3, 1)
        else:
            for field, minimum in ((self.event_time_edit, 136), (self.event_type_combo, 108)):
                field.setMinimumWidth(minimum)
                field.setMaximumWidth(16777215)
                field.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            grid.setColumnMinimumWidth(0, 0)
            grid.setColumnMinimumWidth(1, 0)
            self.event_indications_edit.setMaximumWidth(360)
            grid.addWidget(self.btn_add_event, 1, 4)
            grid.addWidget(self.lbl_event_o2_flow, 0, 5)
            grid.addWidget(self.event_o2_flow_edit, 1, 5)
            grid.setColumnStretch(3, 0)

    def _apply_edit_time_constraints(self):
        event = self._editing_event
        events = sorted(
            (item for item in self._history_events if item.ivl_episode_id == event.ivl_episode_id),
            key=lambda item: (item.timestamp, item.id),
        )
        index = next((i for i, item in enumerate(events) if item.id == event.id), None)
        minimum = events[index - 1].timestamp if index else self._get_admission_datetime() or datetime(1752, 9, 14)
        maximum = events[index + 1].timestamp if index is not None and index + 1 < len(events) else datetime(9999, 12, 31)
        self.event_time_edit.blockSignals(True)
        self.event_time_edit.setDateTimeRange(QDateTime(minimum), QDateTime(maximum))
        self.event_time_edit.blockSignals(False)

    def _save_event_edit(self):
        event = self._editing_event
        context = self._editing_context
        if event is None or context != (self.remcard_service, self.admission_id):
            return
        try:
            event_type = self.event_type_combo.currentData()
            mode = self.mode_combo.currentData() if event_type in ("START_VENT", "MODE_CHANGE") else None
            payload = dict(
                admission_id=int(self.admission_id),
                event_time=self.event_time_edit.dateTime().toPython(),
                event_type=event_type, mode=mode,
                parameters=self._collect_mode_parameters() if mode else {},
                extubation_reason=self.event_indications_edit.text().strip() or None,
                o2_flow=float(self.event_o2_flow_edit.text().replace(",", "."))
                if event_type == "EXTUBATION" and self.event_o2_flow_edit.text().strip() else None,
                expected_event_revision=int(getattr(event, "revision", 0) or 0),
                expected_case_revision=self._editing_case_revision,
            )
        except Exception as exc:
            CustomMessageBox.warning(self, "Ошибка изменения события ИВЛ", str(exc))
            return
        service = self.remcard_service

        def on_success(_result):
            if context == (self.remcard_service, self.admission_id) and self._editing_event is event:
                self._reset_event_editor()

        self._enqueue_ivl_write(
            f"ivl_edit_event:{event.id}",
            lambda: service.edit_ventilation_event(event.id, **payload),
            pending_text="Случай: изменение события сохраняется...",
            error_title="Ошибка изменения события ИВЛ", on_success=on_success,
            focused=True,
        )
