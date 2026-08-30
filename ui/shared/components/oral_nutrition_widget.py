import logging
from datetime import datetime, timedelta
from typing import Optional

from PySide6.QtCore import QDateTime, QEvent, QTimer, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from rem_card.services.diet_service import diet_details, schedule_items
from rem_card.services.shift_service import ShiftService
from rem_card.ui.shared.async_call import AsyncCallThread
from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.shared.window_state import SavedFramelessDialogMixin
from rem_card.ui.styles.oral_nutrition_styles import (
    apply_oral_popup_styles,
    build_oral_nutrition_dialog_style,
    build_oral_nutrition_style,
)
from rem_card.ui.styles.theme_manager import get_theme_manager


logger = logging.getLogger(__name__)


def _qdatetime(value: datetime) -> QDateTime:
    return QDateTime.fromString(value.strftime("%Y-%m-%d %H:%M"), "yyyy-MM-dd HH:mm")


def _field_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("OralDialogFieldLabel")
    return label


def _dialog_section(title: str) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("OralDialogSection")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(12, 10, 12, 12)
    layout.setSpacing(8)
    title_label = QLabel(title)
    title_label.setObjectName("OralDialogSectionTitle")
    layout.addWidget(title_label)
    return frame, layout


class DietAssignmentDialog(SavedFramelessDialogMixin, BaseStyledDialog):
    def __init__(self, templates, version=None, parent=None):
        super().__init__("Назначение диеты", parent)
        self._init_saved_frameless_dialog(
            "oral_nutrition/diet_assignment_dialog_geometry_v1",
            drag_area_height=42,
        )
        self.setMinimumSize(820, 680)
        self.resize(900, 760)
        self.setSizeGripEnabled(True)
        self.templates = list(templates or [])
        self.version = version
        self.delete_requested = False
        self._templates_by_id = {
            int(item.id): item for item in self.templates if getattr(item, "id", None) is not None
        }
        self._build_ui()
        self._fill()
        apply_oral_popup_styles(self, get_theme_manager().current_tokens())
        self._restore_saved_geometry()

    def _build_ui(self):
        tokens = get_theme_manager().current_tokens()
        self.content_widget.setObjectName("OralNutritionDialogBody")
        self.content_widget.setStyleSheet(build_oral_nutrition_dialog_style(tokens))
        self.content_layout.setContentsMargins(14, 12, 14, 14)
        self.content_layout.setSpacing(10)

        general_frame, general_layout = _dialog_section("Основные параметры диеты")
        self.diet_parameters_widget = QWidget()
        grid = QGridLayout(self.diet_parameters_widget)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.addWidget(_field_label("Шаблон"), 0, 0)
        self.template_combo = QComboBox()
        self.template_combo.addItem("Индивидуальная диета", None)
        for template in self.templates:
            self.template_combo.addItem(template.name, int(template.id))
        grid.addWidget(self.template_combo, 0, 1)
        grid.addWidget(_field_label("Действует с"), 0, 2)
        self.effective_edit = QDateTimeEdit()
        self.effective_edit.setDisplayFormat("dd.MM.yyyy HH:mm")
        self.effective_edit.setCalendarPopup(True)
        self.effective_edit.setDateTime(_qdatetime(datetime.now().replace(second=0, microsecond=0)))
        grid.addWidget(self.effective_edit, 0, 3)

        grid.addWidget(_field_label("Название"), 1, 0)
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Например: стол № 9")
        grid.addWidget(self.name_input, 1, 1)
        grid.addWidget(_field_label("Описание"), 1, 2)
        self.text_input = QLineEdit()
        grid.addWidget(self.text_input, 1, 3)

        grid.addWidget(_field_label("Консистенция"), 2, 0)
        self.consistency_combo = QComboBox()
        self.consistency_combo.setEditable(True)
        self.consistency_combo.addItems(["", "Обычная", "Мягкая", "Протёртая", "Полужидкая", "Жидкая"])
        grid.addWidget(self.consistency_combo, 2, 1)
        grid.addWidget(_field_label("Температура"), 2, 2)
        self.temperature_combo = QComboBox()
        self.temperature_combo.setEditable(True)
        self.temperature_combo.addItems(["", "Комнатная", "Тёплая", "Холодная"])
        grid.addWidget(self.temperature_combo, 2, 3)

        grid.addWidget(_field_label("Ограничение соли"), 3, 0)
        self.salt_input = QLineEdit()
        self.salt_input.setPlaceholderText("Например: до 5 г/сут")
        grid.addWidget(self.salt_input, 3, 1)
        grid.addWidget(_field_label("Жидкость в сутки"), 3, 2)
        self.daily_fluid_spin = QSpinBox()
        self.daily_fluid_spin.setRange(0, 10000)
        self.daily_fluid_spin.setSpecialValueText("Не задана")
        self.daily_fluid_spin.setSuffix(" мл")
        grid.addWidget(self.daily_fluid_spin, 3, 3)
        general_layout.addWidget(self.diet_parameters_widget)

        checks = QHBoxLayout()
        self.fractional_check = QCheckBox("Дробно, малыми порциями")
        self.on_demand_check = QCheckBox("Питьё по требованию")
        self.no_food_check = QCheckBox("Голод")
        self.no_fluids_check = QCheckBox("Без жидкости")
        for check in (self.fractional_check, self.on_demand_check, self.no_food_check, self.no_fluids_check):
            checks.addWidget(check)
        checks.addStretch()
        general_layout.addLayout(checks)
        self.content_layout.addWidget(general_frame)

        self.schedule_frame, schedule_layout = _dialog_section("Приёмы пищи в медицинские сутки 08:00–08:00")
        self.schedule_table = QTableWidget(0, 4)
        self.schedule_table.setObjectName("OralScheduleTable")
        self.schedule_table.setAlternatingRowColors(True)
        self.schedule_table.verticalHeader().hide()
        self.schedule_table.setHorizontalHeaderLabels(["Приём пищи", "Время", "План, мл", "Примечание"])
        self.schedule_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.schedule_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.schedule_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.schedule_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.schedule_table.verticalHeader().setDefaultSectionSize(32)
        schedule_layout.addWidget(self.schedule_table, 1)

        schedule_buttons = QHBoxLayout()
        self.add_row_btn = QPushButton("+ Приём пищи")
        self.remove_row_btn = QPushButton("Удалить строку")
        self.add_row_btn.setObjectName("OralDialogSecondaryButton")
        self.remove_row_btn.setObjectName("OralDialogDangerButton")
        schedule_buttons.addWidget(self.add_row_btn)
        schedule_buttons.addWidget(self.remove_row_btn)
        schedule_buttons.addStretch()
        schedule_layout.addLayout(schedule_buttons)
        self.content_layout.addWidget(self.schedule_frame, 1)

        notes_frame, notes_layout = _dialog_section("Указания и изменение назначения")
        text_grid = QGridLayout()
        text_grid.setHorizontalSpacing(12)
        text_grid.addWidget(_field_label("Особые указания"), 0, 0)
        text_grid.addWidget(_field_label("Комментарий к назначению"), 0, 1)
        self.instructions_input = QTextEdit()
        self.instructions_input.setFixedHeight(62)
        self.change_note_input = QTextEdit()
        self.change_note_input.setFixedHeight(62)
        text_grid.addWidget(self.instructions_input, 1, 0)
        text_grid.addWidget(self.change_note_input, 1, 1)
        notes_layout.addLayout(text_grid)

        clear_row = QHBoxLayout()
        clear_row.addWidget(_field_label("Факты при изменении диеты"))
        self.clear_mode_combo = QComboBox()
        self.clear_mode_combo.addItem("Сохранить все внесённые факты", "preserve")
        self.clear_mode_combo.addItem("Удалить факты до времени изменения", "before")
        self.clear_mode_combo.addItem("Удалить все факты пациента", "all")
        clear_row.addWidget(self.clear_mode_combo)
        clear_row.addStretch()
        notes_layout.addLayout(clear_row)
        self.content_layout.addWidget(notes_frame)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.delete_button = buttons.addButton("Удалить", QDialogButtonBox.DestructiveRole)
        save_button = buttons.button(QDialogButtonBox.Save)
        cancel_button = buttons.button(QDialogButtonBox.Cancel)
        save_button.setText("Сохранить")
        cancel_button.setText("Отмена")
        save_button.setObjectName("OralDialogPrimaryButton")
        cancel_button.setObjectName("OralDialogSecondaryButton")
        self.delete_button.setObjectName("OralDialogDangerButton")
        self.delete_button.setVisible(self.version is not None)
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        self.delete_button.clicked.connect(self._confirm_delete)
        self.content_layout.addWidget(buttons)

        self.template_combo.currentIndexChanged.connect(self._template_changed)
        self.on_demand_check.toggled.connect(
            lambda checked: self._clear_conflicting_check(self.no_fluids_check, checked)
        )
        self.no_fluids_check.toggled.connect(
            lambda checked: self._clear_conflicting_check(self.on_demand_check, checked)
        )
        self.no_food_check.toggled.connect(self._sync_hunger_state)
        self.add_row_btn.clicked.connect(self._add_schedule_row)
        self.remove_row_btn.clicked.connect(self._remove_schedule_row)

    @staticmethod
    def _clear_conflicting_check(other_check: QCheckBox, checked: bool) -> None:
        if checked and other_check.isChecked():
            other_check.setChecked(False)

    def _sync_hunger_state(self, checked: bool) -> None:
        diet_enabled = not bool(checked)
        self.diet_parameters_widget.setEnabled(diet_enabled)
        self.fractional_check.setEnabled(diet_enabled)
        self.schedule_frame.setEnabled(diet_enabled)
        if checked:
            self.fractional_check.setChecked(False)

    def _fill(self):
        if self.version is not None:
            if self.version.template_id is not None:
                index = self.template_combo.findData(int(self.version.template_id))
                self.template_combo.setCurrentIndex(max(0, index))
            self.effective_edit.setDateTime(_qdatetime(self.version.effective_from))
            self._load_values(
                self.version.diet_name,
                self.version.diet_text,
                self.version.schedule_json,
                self.version.details_json,
            )
            self.change_note_input.setPlainText(self.version.change_note or "")
        elif self.templates:
            default = next((item for item in self.templates if item.is_default), self.templates[0])
            index = self.template_combo.findData(int(default.id))
            self.template_combo.setCurrentIndex(max(0, index))
            self._template_changed(self.template_combo.currentIndex())
        else:
            self._add_schedule_row("Завтрак", "08:00", 250, "")

    def _template_changed(self, _index):
        template_id = self.template_combo.currentData()
        if template_id is None:
            return
        template = self._templates_by_id.get(int(template_id))
        if template is not None:
            self._load_values(template.name, template.diet_text, template.schedule_json, template.details_json)

    def _load_values(self, name, text, schedule_json, details_json):
        self.name_input.setText(str(name or ""))
        self.text_input.setText(str(text or ""))
        details = diet_details(details_json)
        self.consistency_combo.setCurrentText(str(details.get("consistency") or ""))
        self.temperature_combo.setCurrentText(str(details.get("temperature") or ""))
        self.salt_input.setText(str(details.get("salt_limit") or ""))
        self.daily_fluid_spin.setValue(int(details.get("daily_fluid_ml") or 0))
        self.fractional_check.setChecked(bool(details.get("fractional")))
        self.on_demand_check.setChecked(bool(details.get("on_demand")))
        self.no_food_check.setChecked(bool(details.get("no_food")))
        self.no_fluids_check.setChecked(bool(details.get("no_fluids")))
        self.instructions_input.setPlainText(str(details.get("special_instructions") or ""))
        self.schedule_table.setRowCount(0)
        for item in schedule_items(schedule_json):
            self._add_schedule_row(
                str(item.get("meal") or "Приём пищи"), str(item.get("time") or "08:00"),
                int(item.get("amount") or 200), str(item.get("note") or ""), str(item.get("key") or ""),
            )

    def _add_schedule_row(self, meal="Приём пищи", time_text="08:00", amount=200, note="", item_key=""):
        row = self.schedule_table.rowCount()
        self.schedule_table.insertRow(row)
        values = (str(meal), str(time_text), str(int(amount)), str(note))
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column == 0:
                item.setData(Qt.UserRole, item_key or f"meal-{row + 1}")
            self.schedule_table.setItem(row, column, item)

    def _remove_schedule_row(self):
        row = self.schedule_table.currentRow()
        if row >= 0:
            self.schedule_table.removeRow(row)

    def _accept_if_valid(self):
        if not self.no_food_check.isChecked() and not self.name_input.text().strip():
            CustomMessageBox.warning(self, "Питание", "Укажите название диеты.")
            return
        try:
            self.data()
        except Exception as exc:
            CustomMessageBox.warning(self, "Питание", str(exc))
            return
        self.accept()

    def _confirm_delete(self):
        if self.version is None:
            return
        if CustomMessageBox.question(
            self,
            "Удаление назначения диеты",
            "Удалить выбранное изменение диеты? После удаления снова будет действовать "
            "предыдущее назначение, если оно есть.",
        ) != CustomMessageBox.Yes:
            return
        self.delete_requested = True
        self.accept()

    def data(self) -> dict:
        hunger = self.no_food_check.isChecked()
        schedule = []
        for row in range(0 if hunger else self.schedule_table.rowCount()):
            meal = self.schedule_table.item(row, 0)
            time_item = self.schedule_table.item(row, 1)
            amount_item = self.schedule_table.item(row, 2)
            note = self.schedule_table.item(row, 3)
            if not meal or not time_item or not amount_item:
                continue
            amount = int(float(amount_item.text().replace(",", ".")))
            schedule.append(
                {
                    "key": str(meal.data(Qt.UserRole) or f"meal-{row + 1}"),
                    "meal": meal.text().strip() or "Приём пищи",
                    "time": time_item.text().strip(),
                    "amount": amount,
                    "note": note.text().strip() if note else "",
                }
            )
        return {
            "template_id": None if hunger else self.template_combo.currentData(),
            "diet_name": "Голод" if hunger else self.name_input.text().strip(),
            "diet_text": "Пациент голодает" if hunger else self.text_input.text().strip(),
            "effective_from": self.effective_edit.dateTime().toPython().replace(second=0, microsecond=0),
            "schedule_json": schedule,
            "details_json": {
                "consistency": "" if hunger else self.consistency_combo.currentText().strip(),
                "temperature": "" if hunger else self.temperature_combo.currentText().strip(),
                "salt_limit": "" if hunger else self.salt_input.text().strip(),
                "fractional": False if hunger else self.fractional_check.isChecked(),
                "daily_fluid_ml": None if hunger else (self.daily_fluid_spin.value() or None),
                "special_instructions": self.instructions_input.toPlainText().strip(),
                "comment": "",
                "no_food": hunger,
                "no_fluids": self.no_fluids_check.isChecked(),
                "on_demand": self.on_demand_check.isChecked(),
            },
            "change_note": self.change_note_input.toPlainText().strip(),
            "clear_mode": self.clear_mode_combo.currentData(),
            "version_id": getattr(self.version, "id", None),
            "expected_version": getattr(self.version, "version", None),
        }


class OralFactDialog(BaseStyledDialog):
    EARLY_ENTRY_WINDOW = timedelta(hours=3)

    def __init__(self, planned_item=None, event=None, shift_date=None, parent=None):
        super().__init__("Фактическое потребление", parent)
        self.setMinimumWidth(500)
        self.planned_item = planned_item or {}
        self.event = event
        self.shift_date = shift_date
        tokens = get_theme_manager().current_tokens()
        self.content_widget.setObjectName("OralNutritionDialogBody")
        self.content_widget.setStyleSheet(build_oral_nutrition_dialog_style(tokens))
        self.content_layout.setContentsMargins(14, 12, 14, 14)
        fact_frame, fact_layout = _dialog_section("Фактические данные")
        layout = QGridLayout()
        layout.setHorizontalSpacing(14)
        layout.setVerticalSpacing(9)
        layout.addWidget(_field_label("Приём / описание"), 0, 0)
        self.meal_input = QLineEdit()
        self.meal_input.setText(
            getattr(event, "meal_name", "") or str(self.planned_item.get("meal") or "Питьё / питание")
        )
        layout.addWidget(self.meal_input, 0, 1)
        layout.addWidget(_field_label("Фактическое время"), 1, 0)
        self.time_edit = QDateTimeEdit()
        self.time_edit.setDisplayFormat("dd.MM.yyyy HH:mm")
        self.time_edit.setCalendarPopup(True)
        initial_time = getattr(event, "event_time", None) or self.planned_item.get("planned_dt") or datetime.now()
        self.allowed_start, self.allowed_end = self._time_bounds(initial_time)
        self.time_edit.setMinimumDateTime(_qdatetime(self.allowed_start))
        self.time_edit.setMaximumDateTime(_qdatetime(self.allowed_end))
        self.time_edit.setDateTime(_qdatetime(initial_time))
        layout.addWidget(self.time_edit, 1, 1)
        layout.addWidget(_field_label("Количество"), 2, 0)
        self.amount_spin = QSpinBox()
        self.amount_spin.setRange(1, 10000)
        self.amount_spin.setSuffix(" мл")
        initial_amount = getattr(event, "amount_ml", None) or self.planned_item.get("amount") or 100
        self.amount_spin.setValue(int(initial_amount))
        layout.addWidget(self.amount_spin, 2, 1)
        layout.addWidget(_field_label("Примечание"), 3, 0)
        self.note_input = QTextEdit()
        self.note_input.setFixedHeight(72)
        self.note_input.setPlainText(getattr(event, "note", "") or "")
        layout.addWidget(self.note_input, 3, 1)
        fact_layout.addLayout(layout)
        self.content_layout.addWidget(fact_frame)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        save_button = buttons.button(QDialogButtonBox.Save)
        cancel_button = buttons.button(QDialogButtonBox.Cancel)
        save_button.setText("Сохранить")
        cancel_button.setText("Отмена")
        save_button.setObjectName("OralDialogPrimaryButton")
        cancel_button.setObjectName("OralDialogSecondaryButton")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.content_layout.addWidget(buttons)
        apply_oral_popup_styles(self, tokens)

    def _time_bounds(self, initial_time: datetime) -> tuple[datetime, datetime]:
        planned_time = self.planned_item.get("planned_dt")
        reference = planned_time or self.shift_date or initial_time
        shift_start, shift_end = ShiftService.get_day_period(reference)
        last_minute = shift_end - timedelta(minutes=1)
        if planned_time is not None:
            return max(shift_start, planned_time - self.EARLY_ENTRY_WINDOW), last_minute
        current_limit = min(last_minute, datetime.now().replace(second=0, microsecond=0))
        return shift_start, max(shift_start, current_limit)

    def data(self) -> dict:
        return {
            "event_time": self.time_edit.dateTime().toPython().replace(second=0, microsecond=0),
            "amount_ml": int(self.amount_spin.value()),
            "meal_name": self.meal_input.text().strip(),
            "note": self.note_input.toPlainText().strip(),
        }


class OralNutritionWidget(QWidget):
    data_changed = Signal()
    _write_success_ready = Signal(object)
    _write_failure_ready = Signal(object)

    def __init__(self, service=None, *, role="doctor", parent=None):
        super().__init__(parent)
        self.service = service
        self.role = "nurse" if str(role).lower() in {"nurse", "медсестра"} else "doctor"
        self.admission_id: Optional[int] = None
        self.shift_date: Optional[datetime] = None
        self.read_only = False
        self._snapshot = {}
        self._refresh_generation = 0
        self._refresh_worker = None
        self._refresh_pending = False
        self._write_pending = False
        self._local_undo = []
        self._owned_modal_dialogs = {}
        self._write_success_ready.connect(self._apply_write_success, Qt.QueuedConnection)
        self._write_failure_ready.connect(self._apply_write_failure, Qt.QueuedConnection)
        self._build_ui()

    def _build_ui(self):
        self.setObjectName("OralNutritionRoot")
        page_layout = QVBoxLayout(self)
        page_layout.setContentsMargins(0, 3, 0, 5)
        page_layout.setSpacing(0)

        self.outer_frame = QFrame()
        self.outer_frame.setObjectName("OralNutritionOuterFrame")
        outer_layout = QVBoxLayout(self.outer_frame)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        self.outer_header = QLabel("Пероральное питание")
        self.outer_header.setObjectName("OralNutritionOuterHeader")
        self.outer_header.setAlignment(Qt.AlignCenter)
        self.outer_header.setFixedHeight(30)
        outer_layout.addWidget(self.outer_header)

        self.outer_body = QWidget()
        self.outer_body.setObjectName("OralNutritionOuterBody")
        root = QVBoxLayout(self.outer_body)
        root.setContentsMargins(8, 7, 8, 8)
        root.setSpacing(8)
        outer_layout.addWidget(self.outer_body, 1)
        page_layout.addWidget(self.outer_frame, 1)
        self.setStyleSheet(build_oral_nutrition_style(get_theme_manager().current_tokens()))

        self.summary_frame = QFrame()
        self.summary_frame.setObjectName("OralNutritionSummary")
        summary_layout = QHBoxLayout(self.summary_frame)
        summary_layout.setContentsMargins(14, 9, 10, 9)
        summary_layout.setSpacing(8)
        self.active_title = QLabel("Диета не назначена")
        self.active_title.setObjectName("OralNutritionTitle")
        self.active_meta = QLabel("—")
        self.active_meta.setObjectName("OralNutritionMeta")
        title_col = QVBoxLayout()
        title_col.addWidget(self.active_title)
        title_col.addWidget(self.active_meta)
        summary_layout.addLayout(title_col, 1)
        self.assign_btn = QPushButton("Назначить / изменить диету")
        self.edit_version_btn = QPushButton("Изменить выбранное")
        self.clear_btn = QPushButton("Очистить факты")
        self.undo_btn = QPushButton("Отменить последнее действие")
        self.assign_btn.setObjectName("OralPrimaryButton")
        self.edit_version_btn.setObjectName("OralSecondaryButton")
        self.clear_btn.setObjectName("OralDangerButton")
        self.undo_btn.setObjectName("OralSecondaryButton")
        for button in (self.assign_btn, self.edit_version_btn, self.clear_btn, self.undo_btn):
            button.setFixedHeight(34)
            summary_layout.addWidget(button)
        root.addWidget(self.summary_frame)

        plan_frame, plan_layout = self._section("План и фактическое потребление")
        toolbar = QHBoxLayout()
        toolbar.setSpacing(7)
        self.add_planned_fact_btn = QPushButton("Внести факт по выбранному приёму")
        self.add_unplanned_btn = QPushButton("+ Внеплановое питьё / питание")
        self.edit_fact_btn = QPushButton("Изменить выбранный факт")
        self.delete_fact_btn = QPushButton("Удалить выбранный факт")
        self.add_planned_fact_btn.setObjectName("OralPrimaryButton")
        self.add_unplanned_btn.setObjectName("OralSecondaryButton")
        self.edit_fact_btn.setObjectName("OralSecondaryButton")
        self.delete_fact_btn.setObjectName("OralDangerButton")
        for button in (self.add_planned_fact_btn, self.add_unplanned_btn, self.edit_fact_btn, self.delete_fact_btn):
            button.setFixedHeight(33)
            toolbar.addWidget(button)
        toolbar.addStretch()
        plan_layout.addLayout(toolbar)

        self.intake_table = QTableWidget(0, 8)
        self.intake_table.setObjectName("OralIntakeTable")
        self.intake_table.setAlternatingRowColors(True)
        self.intake_table.verticalHeader().hide()
        self.intake_table.setHorizontalHeaderLabels(
            ["Приём", "Плановое время", "План, мл", "Факт, мл", "Фактическое время", "%", "Примечание", "Диета"]
        )
        self.intake_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.intake_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.intake_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.intake_table.verticalHeader().setDefaultSectionSize(31)
        for column in (0, 1, 2, 3, 4, 5):
            self.intake_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.intake_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.Stretch)
        self.intake_table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeToContents)
        plan_layout.addWidget(self.intake_table, 1)
        root.addWidget(plan_frame, 3)

        lower = QHBoxLayout()
        lower.setSpacing(8)
        self.history_frame, history_box = self._section("Изменения диеты")
        self.version_table = QTableWidget(0, 3)
        self.version_table.setObjectName("OralVersionTable")
        self.version_table.setAlternatingRowColors(True)
        self.version_table.verticalHeader().hide()
        self.version_table.setHorizontalHeaderLabels(["Действует с", "Диета", "Комментарий"])
        self.version_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.version_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.version_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.version_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.version_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.version_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        history_box.addWidget(self.version_table)
        lower.addWidget(self.history_frame, 1)

        self.totals_frame, totals_box = self._section("Суточные итоги за пребывание")
        self.totals_table = QTableWidget(0, 4)
        self.totals_table.setObjectName("OralTotalsTable")
        self.totals_table.setAlternatingRowColors(True)
        self.totals_table.verticalHeader().hide()
        self.totals_table.setHorizontalHeaderLabels(["Медицинские сутки", "План, мл", "Факт, мл", "%"])
        self.totals_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.totals_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        totals_box.addWidget(self.totals_table)
        lower.addWidget(self.totals_frame, 1)
        self.lower_layout = lower
        root.addLayout(lower, 2)

        self.status_label = QLabel("")
        self.status_label.setObjectName("OralNutritionStatus")
        self.status_label.hide()
        root.addWidget(self.status_label)

        self.assign_btn.clicked.connect(lambda: self._open_assignment(None))
        self.edit_version_btn.clicked.connect(self._edit_selected_version)
        self.clear_btn.clicked.connect(self._clear_facts)
        self.undo_btn.clicked.connect(self._undo_last)
        self.add_planned_fact_btn.clicked.connect(self._add_planned_fact)
        self.add_unplanned_btn.clicked.connect(lambda: self._open_fact_dialog(None, None))
        self.edit_fact_btn.clicked.connect(self._edit_selected_fact)
        self.delete_fact_btn.clicked.connect(self._delete_selected_fact)
        self.intake_table.itemDoubleClicked.connect(self._edit_intake_from_double_click)
        self.version_table.itemDoubleClicked.connect(lambda *_: self._edit_selected_version())
        self.intake_table.itemSelectionChanged.connect(self._update_actions)
        self.version_table.itemSelectionChanged.connect(self._update_actions)
        self._install_version_selection_clear_filters()
        self._update_actions()

    def _install_version_selection_clear_filters(self):
        application = QApplication.instance()
        if application is not None:
            application.installEventFilter(self)

    def _clear_version_selection(self):
        if not self.version_table.selectionModel().hasSelection() and self.version_table.currentRow() < 0:
            return
        self.version_table.clearSelection()
        self.version_table.setCurrentCell(-1, -1)
        self._update_actions()

    def eventFilter(self, obj, event):
        if event.type() == QEvent.MouseButtonPress:
            global_position = None
            if hasattr(event, "globalPosition"):
                global_position = event.globalPosition().toPoint()

            on_edit_button = self._event_hits_widget(
                obj,
                global_position,
                self.edit_version_btn,
            )
            on_selected_version = False
            viewport = self.version_table.viewport()
            if self._event_hits_widget(obj, global_position, viewport):
                if global_position is not None:
                    position = viewport.mapFromGlobal(global_position)
                else:
                    position = event.position().toPoint() if hasattr(event, "position") else event.pos()
                on_selected_version = self.version_table.itemAt(position) is not None
            if not on_edit_button and not on_selected_version:
                self._clear_version_selection()
        return super().eventFilter(obj, event)

    @staticmethod
    def _event_hits_widget(obj, global_position, widget):
        if global_position is not None and widget.isVisible():
            return widget.rect().contains(widget.mapFromGlobal(global_position))
        return isinstance(obj, QWidget) and (obj is widget or widget.isAncestorOf(obj))

    def mousePressEvent(self, event):
        child = self.childAt(event.position().toPoint()) if hasattr(event, "position") else self.childAt(event.pos())
        in_version_table = child is not None and (
            child is self.version_table or self.version_table.isAncestorOf(child)
        )
        on_edit_button = child is not None and (
            child is self.edit_version_btn or self.edit_version_btn.isAncestorOf(child)
        )
        if not in_version_table and not on_edit_button:
            self._clear_version_selection()
        super().mousePressEvent(event)

    @staticmethod
    def _section(title: str) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("OralNutritionSectionCard")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(9, 7, 9, 9)
        layout.setSpacing(6)
        label = QLabel(title)
        label.setObjectName("OralNutritionSectionTitle")
        layout.addWidget(label)
        return frame, layout

    def set_service(self, service):
        self.service = service
        self.refresh_data()

    def set_read_only(self, read_only: bool):
        self.read_only = bool(read_only)
        self._update_actions()

    def set_context(self, admission_id: Optional[int], shift_date: Optional[datetime]):
        if self.admission_id != admission_id or self.shift_date != shift_date:
            self._local_undo = []
        self.admission_id = int(admission_id) if admission_id else None
        self.shift_date = shift_date
        self.refresh_data()

    def handle_data_changes(self, payload: dict):
        changed = set(payload.get("changed_entities") or [])
        if payload.get("forced") or changed.intersection({"diet_templates", "diet_plan", "diet_plan_versions", "oral_intake_events"}):
            self.refresh_data()

    def refresh_data(self, *_, **__):
        self._refresh_generation += 1
        generation = self._refresh_generation
        if not self.service or not self.admission_id or not self.shift_date:
            self._refresh_pending = False
            self._snapshot = {}
            self._render()
            return
        if self._refresh_worker is not None and self._refresh_worker.isRunning():
            self._refresh_pending = True
            return

        admission_id = int(self.admission_id)
        shift_date = self.shift_date
        service = self.service
        self._set_status_message("Загрузка питания…")

        worker = AsyncCallThread(
            lambda: service.build_oral_nutrition_snapshot(admission_id, shift_date), parent=self
        )
        worker._oral_refresh_generation = generation
        self._refresh_worker = worker
        worker.succeeded.connect(self._apply_snapshot)
        worker.failed.connect(self._refresh_failed)
        worker.finished.connect(self._refresh_finished)
        worker.start()

    def _refresh_finished(self, worker=None):
        worker = worker or self.sender()
        if self._refresh_worker is not worker:
            return
        self._refresh_worker = None
        if not self._refresh_pending:
            return
        self._refresh_pending = False
        QTimer.singleShot(0, self.refresh_data)

    def _apply_snapshot(self, result, generation=None):
        if generation is None:
            generation = getattr(self.sender(), "_oral_refresh_generation", -1)
        if generation != self._refresh_generation or not isinstance(result, dict):
            return
        self._snapshot = result
        self._render()

    def _refresh_failed(self, exc, generation=None):
        if generation is None:
            generation = getattr(self.sender(), "_oral_refresh_generation", -1)
        if generation != self._refresh_generation:
            return
        logger.warning("Oral nutrition refresh failed: %s", exc, exc_info=True)
        self._set_status_message(f"Не удалось загрузить питание: {exc}")

    def _set_status_message(self, message: str):
        text = str(message or "")
        self.status_label.setText(text)
        self.status_label.setVisible(bool(text))

    def _render(self):
        active = self._snapshot.get("active")
        if active is None:
            self.active_title.setText("Диета не назначена")
            self.active_meta.setText("Врач может назначить шаблон или индивидуальную диету")
        else:
            details = diet_details(active.details_json)
            tags = [
                str(details.get("consistency") or ""),
                str(details.get("temperature") or ""),
                str(details.get("salt_limit") or ""),
            ]
            frequency = len(schedule_items(active.schedule_json))
            if frequency:
                tags.append(f"кратность {frequency} раз/сут")
            if details.get("fractional"):
                tags.append("дробно")
            if details.get("daily_fluid_ml"):
                tags.append(f"жидкость {details['daily_fluid_ml']} мл/сут")
            if details.get("no_food"):
                tags.append("ГОЛОД")
            if details.get("no_fluids"):
                tags.append("БЕЗ ЖИДКОСТИ")
            self.active_title.setText(active.diet_name or active.diet_text or "Назначенная диета")
            self.active_meta.setText(
                f"с {active.effective_from:%d.%m.%Y %H:%M}" + (f" · {' · '.join(tag for tag in tags if tag)}" if any(tags) else "")
            )

        self.intake_table.setRowCount(0)
        planned_rows = list(self._snapshot.get("planned_rows") or [])
        for record in planned_rows:
            facts = list(record.get("facts") or [])
            note_parts = [str(record.get("note") or "")]
            note_parts.extend(event.note for event in facts if event.note)
            actual_times = ", ".join(event.event_time.strftime("%H:%M") for event in facts)
            self._append_intake_row(
                record,
                facts[0] if len(facts) == 1 else None,
                [
                    str(record.get("meal") or "Приём пищи"),
                    record.get("planned_dt").strftime("%H:%M") if record.get("planned_dt") else "—",
                    self._number(record.get("amount")),
                    self._number(record.get("fact_total")),
                    actual_times or "—",
                    f"{record['percent']:.1f}%" if record.get("percent") is not None else "—",
                    " · ".join(part for part in note_parts if part),
                    str(record.get("diet_name") or ""),
                ],
            )
            if len(facts) > 1:
                for event in facts:
                    self._append_intake_row(
                        None,
                        event,
                        [
                            f"↳ {event.meal_name or record.get('meal') or 'Факт'}",
                            "—", "—", self._number(event.amount_ml), event.event_time.strftime("%H:%M"),
                            "—", event.note or "", "Отдельная запись",
                        ],
                    )
        planned_event_ids = {
            int(event.id) for record in planned_rows for event in record.get("facts") or [] if event.id is not None
        }
        for event in self._snapshot.get("events") or []:
            if event.id is not None and int(event.id) in planned_event_ids:
                continue
            self._append_intake_row(
                None,
                event,
                [event.meal_name or "Внепланово", "—", "—", self._number(event.amount_ml),
                 event.event_time.strftime("%H:%M"), "—", event.note or "", "Вне плана"],
            )

        self.version_table.setRowCount(0)
        for version in reversed(list(self._snapshot.get("versions") or [])):
            row = self.version_table.rowCount()
            self.version_table.insertRow(row)
            first = QTableWidgetItem(version.effective_from.strftime("%d.%m.%Y %H:%M"))
            first.setData(Qt.UserRole, version)
            self.version_table.setItem(row, 0, first)
            self.version_table.setItem(row, 1, QTableWidgetItem(version.diet_name or version.diet_text))
            self.version_table.setItem(row, 2, QTableWidgetItem(version.change_note or ""))

        self.totals_table.setRowCount(0)
        for item in self._snapshot.get("history") or []:
            row = self.totals_table.rowCount()
            self.totals_table.insertRow(row)
            values = [
                item["shift_start"].strftime("%d.%m.%Y 08:00"),
                self._number(item.get("planned_ml")), self._number(item.get("fact_ml")),
                f"{item['percent']:.1f}%" if item.get("percent") is not None else "—",
            ]
            for column, value in enumerate(values):
                self.totals_table.setItem(row, column, QTableWidgetItem(value))
        self._set_status_message("" if self.admission_id else "Нет пациента")
        self._update_actions()

    def _append_intake_row(self, planned_item, event, values):
        row = self.intake_table.rowCount()
        self.intake_table.insertRow(row)
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value or ""))
            if column == 0:
                item.setData(Qt.UserRole, planned_item)
                item.setData(Qt.UserRole + 1, event)
            if column == 5 and str(value).endswith("%"):
                try:
                    percent = float(str(value).rstrip("%"))
                    if percent < 50:
                        item.setForeground(QColor("#c0392b"))
                    elif percent >= 80:
                        item.setForeground(QColor("#1e8449"))
                except ValueError:
                    pass
            self.intake_table.setItem(row, column, item)

    @staticmethod
    def _number(value) -> str:
        if value in (None, ""):
            return "—"
        number = float(value)
        return str(int(number)) if number.is_integer() else f"{number:.1f}"

    def _selected_intake(self):
        row = self.intake_table.currentRow()
        item = self.intake_table.item(row, 0) if row >= 0 else None
        return (
            item.data(Qt.UserRole) if item else None,
            item.data(Qt.UserRole + 1) if item else None,
        )

    def _selected_version(self):
        selected_rows = self.version_table.selectionModel().selectedRows()
        row = selected_rows[0].row() if selected_rows else -1
        item = self.version_table.item(row, 0) if row >= 0 else None
        return item.data(Qt.UserRole) if item else None

    def _update_actions(self):
        writable = bool(self.admission_id and not self.read_only and not self._write_pending)
        doctor = self.role == "doctor"
        planned, event = self._selected_intake()
        complete_oral_restriction = self._has_complete_oral_restriction()
        self.assign_btn.setVisible(doctor)
        self.edit_version_btn.setVisible(doctor)
        self.clear_btn.setVisible(doctor)
        self.assign_btn.setEnabled(writable and doctor)
        self.edit_version_btn.setEnabled(writable and doctor and self._selected_version() is not None)
        self.clear_btn.setEnabled(writable and doctor and bool(self._snapshot.get("events")))
        self.undo_btn.setEnabled(writable)
        self.add_planned_fact_btn.setEnabled(writable and planned is not None)
        self.add_unplanned_btn.setEnabled(writable and not complete_oral_restriction)
        self.add_unplanned_btn.setToolTip(
            "Внеплановое потребление недоступно: пациенту назначены голод и отсутствие жидкости"
            if complete_oral_restriction
            else ""
        )
        self.edit_fact_btn.setVisible(doctor)
        self.delete_fact_btn.setVisible(doctor)
        self.edit_fact_btn.setEnabled(writable and doctor and event is not None)
        self.delete_fact_btn.setEnabled(writable and doctor and event is not None)

    def _has_complete_oral_restriction(self) -> bool:
        active = self._snapshot.get("active")
        if active is None:
            return False
        details = diet_details(getattr(active, "details_json", None))
        return bool(details.get("no_food") and details.get("no_fluids"))

    def _open_assignment(self, version):
        if self.role != "doctor" or self.read_only:
            return
        dialog = self._own_modal_dialog(
            DietAssignmentDialog(self._snapshot.get("templates") or [], version=version, parent=self)
        )
        try:
            if not dialog.exec():
                return
            delete_requested = bool(dialog.delete_requested)
            data = None if delete_requested else dialog.data()
        finally:
            self._dispose_modal_dialog(dialog)
        if delete_requested:
            if version is None or getattr(version, "id", None) is None:
                return
            self._enqueue_write(
                "diet_version_delete",
                lambda: self.service.delete_diet_version(
                    self.admission_id,
                    int(version.id),
                    expected_version=version.version,
                ),
                after_success=lambda _result: self._local_undo.clear(),
            )
            return
        clear_mode = data.pop("clear_mode")
        effective = data["effective_from"]
        if clear_mode != "preserve":
            text = "Будут удалены все факты питания пациента." if clear_mode == "all" else "Будут удалены факты до времени изменения диеты."
            if CustomMessageBox.question(self, "Очистка фактов", text + " Продолжить?") != CustomMessageBox.Yes:
                return

        def operation():
            result = self.service.assign_diet_version(self.admission_id, **data)
            if clear_mode == "all":
                self.service.clear_oral_intake_facts(self.admission_id)
            elif clear_mode == "before":
                self.service.clear_oral_intake_facts(self.admission_id, before=effective)
            return result

        self._enqueue_write(
            "diet_version_assign",
            operation,
            after_success=(lambda _result: self._local_undo.clear()) if clear_mode != "preserve" else None,
        )

    def _edit_selected_version(self):
        version = self._selected_version()
        if version is not None:
            self._open_assignment(version)

    def _add_planned_fact(self):
        planned, _event = self._selected_intake()
        if planned is not None:
            self._open_fact_dialog(planned, None)

    def _edit_intake_from_double_click(self, item):
        if item is not None:
            self.intake_table.setCurrentItem(item)
            self.intake_table.selectRow(item.row())
        planned, event = self._selected_intake()
        if event is not None:
            self._edit_selected_fact()
        elif planned is not None and not planned.get("facts"):
            self._add_planned_fact()

    def _open_fact_dialog(self, planned, event):
        if self.read_only:
            return
        if planned is None and event is None and self._has_complete_oral_restriction():
            return
        dialog = None
        try:
            dialog = self._own_modal_dialog(
                OralFactDialog(
                    planned_item=planned,
                    event=event,
                    shift_date=self.shift_date,
                    parent=self,
                )
            )
            if not dialog.exec():
                return
            data = dialog.data()
        except Exception as exc:
            logger.exception("Failed to open oral fact dialog for role=%s", self.role)
            CustomMessageBox.warning(self, "Питание", f"Не удалось открыть окно фактического потребления: {exc}")
            return
        finally:
            if dialog is not None:
                self._dispose_modal_dialog(dialog)
        restrictions = self._restrictions_at(data["event_time"])
        if restrictions.get("no_food") or restrictions.get("no_fluids"):
            diet_name = restrictions.get("diet_name") or "ограничение питания"
            if CustomMessageBox.question(
                self,
                "Несоответствие назначению",
                f"На это время действует «{diet_name}». Всё равно внести фактическое потребление?",
            ) != CustomMessageBox.Yes:
                return
        if event is not None:
            self._enqueue_write(
                "oral_fact_update",
                lambda: self.service.update_oral_intake_fact(
                    int(event.id),
                    actor=self.role,
                    expected_version=event.version,
                    planned_time=planned.get("planned_dt") if planned else None,
                    **data,
                ),
                after_success=lambda result, before=event, planned_item=planned: self._remember_undo(
                    {
                        "kind": "restore",
                        "before": before,
                        "expected_version": result.version,
                        "planned_time": planned_item.get("planned_dt") if planned_item else None,
                    }
                ),
            )
            return
        kwargs = {
            **data,
            "actor": self.role,
            "entry_kind": "planned" if planned else "unplanned",
            "plan_version_id": planned.get("plan_version_id") if planned else None,
            "planned_item_key": planned.get("key") if planned else None,
            "planned_time": planned.get("planned_dt") if planned else None,
        }
        self._enqueue_write(
            "oral_fact_create",
            lambda: self.service.create_oral_intake_fact(self.admission_id, **kwargs),
            after_success=lambda result: self._remember_undo(
                {"kind": "delete", "event_id": result.id, "expected_version": result.version}
            ),
        )

    def _own_modal_dialog(self, dialog):
        dialog_key = id(dialog)
        self._owned_modal_dialogs[dialog_key] = dialog
        destroyed = getattr(dialog, "destroyed", None)
        if destroyed is not None and hasattr(destroyed, "connect"):
            destroyed.connect(
                lambda *_args, key=dialog_key: self._owned_modal_dialogs.pop(key, None)
            )
        return dialog

    @staticmethod
    def _dispose_modal_dialog(dialog):
        # Ensure QDateTimeEdit/QCalendarWidget timers and native popup windows
        # are destroyed by Qt on the GUI event loop. Python's cyclic collector
        # may otherwise finalize their wrappers on a database/cache thread.
        try:
            delete_later = getattr(dialog, "deleteLater", None)
            if delete_later is not None:
                delete_later()
        except RuntimeError:
            pass

    def _restrictions_at(self, moment: datetime) -> dict:
        versions = sorted(
            self._snapshot.get("versions") or [], key=lambda item: (item.effective_from, int(item.id or 0))
        )
        active = None
        for version in versions:
            if version.effective_from <= moment:
                active = version
        if active is None:
            return {}
        details = diet_details(active.details_json)
        return {"diet_name": active.diet_name, **details}

    def _edit_selected_fact(self):
        planned, event = self._selected_intake()
        if event is not None:
            if planned is None:
                planned = self._planned_item_for_event(event)
            self._open_fact_dialog(planned, event)

    def _planned_item_for_event(self, event):
        for planned in self._snapshot.get("planned_rows") or []:
            if (
                planned.get("plan_version_id") == getattr(event, "plan_version_id", None)
                and str(planned.get("key") or "") == str(getattr(event, "planned_item_key", "") or "")
            ):
                return planned
        return None

    def _delete_selected_fact(self):
        planned, event = self._selected_intake()
        if self.role != "doctor" or event is None:
            return
        if planned is None:
            planned = self._planned_item_for_event(event)
        if CustomMessageBox.question(self, "Удаление факта", "Удалить выбранный факт питания?") != CustomMessageBox.Yes:
            return
        self._enqueue_write(
            "oral_fact_delete",
            lambda: self.service.delete_oral_intake_fact(int(event.id), expected_version=event.version),
            after_success=lambda _result, before=event, planned_item=planned: self._remember_undo(
                {
                    "kind": "recreate",
                    "before": before,
                    "planned_time": planned_item.get("planned_dt") if planned_item else None,
                }
            ),
        )

    def _undo_last(self):
        if not self.admission_id:
            return
        if self._local_undo:
            action = self._local_undo.pop()
            if action["kind"] == "delete":
                def operation():
                    return self.service.delete_oral_intake_fact(
                        int(action["event_id"]), expected_version=action.get("expected_version")
                    )
            elif action["kind"] == "restore":
                before = action["before"]

                def operation():
                    return self.service.update_oral_intake_fact(
                        int(before.id), before.event_time, before.amount_ml,
                        note=before.note, meal_name=before.meal_name, actor=self.role,
                        planned_time=action.get("planned_time"),
                        expected_version=action.get("expected_version"),
                    )
            else:
                before = action["before"]

                def operation():
                    return self.service.create_oral_intake_fact(
                        self.admission_id, before.event_time, before.amount_ml,
                        plan_version_id=before.plan_version_id,
                        planned_item_key=before.planned_item_key,
                        planned_time=action.get("planned_time"),
                        entry_kind=before.entry_kind,
                        meal_name=before.meal_name,
                        note=before.note,
                        actor=self.role,
                    )
            self._enqueue_write("oral_fact_undo_local", operation)
            return
        if self.role == "doctor":
            self._set_status_message("Нет действия текущего сеанса для отмены")
            return
        self._enqueue_write(
            "oral_fact_undo_last",
            lambda: self.service.undo_last_oral_intake_action(self.admission_id, self.role),
        )

    def _clear_facts(self):
        if self.role != "doctor" or not self._snapshot.get("events"):
            return
        if CustomMessageBox.question(
            self, "Очистка фактов", "Удалить все фактические записи питания пациента?"
        ) != CustomMessageBox.Yes:
            return
        self._enqueue_write(
            "oral_facts_clear",
            lambda: self.service.clear_oral_intake_facts(self.admission_id),
            after_success=lambda _result: self._local_undo.clear(),
        )

    def _remember_undo(self, action):
        self._local_undo.append(action)
        self._local_undo = self._local_undo[-20:]

    def _enqueue_write(self, description, operation, *, after_success=None):
        if not self.service or self._write_pending:
            return
        admission_id = self.admission_id
        shift_date = self.shift_date
        self._write_pending = True
        self._update_actions()
        self._set_status_message("Сохранение…")

        def success(_result=None):
            try:
                self._write_success_ready.emit(
                    {
                        "result": _result,
                        "after_success": after_success,
                        "admission_id": admission_id,
                        "shift_date": shift_date,
                    }
                )
            except RuntimeError:
                pass

        def failure(exc):
            try:
                self._write_failure_ready.emit({"description": description, "exception": exc})
            except RuntimeError:
                pass

        if hasattr(self.service, "enqueue_write"):
            try:
                self.service.enqueue_write(description, operation, on_success=success, on_error=failure)
            except Exception as exc:
                failure(exc)
            return
        try:
            result = operation()
        except Exception as exc:
            failure(exc)
            return
        success(result)

    def _apply_write_success(self, payload):
        payload = dict(payload or {})
        self._write_pending = False
        after_success = payload.get("after_success")
        if after_success is not None:
            after_success(payload.get("result"))
        if (
            self.admission_id == payload.get("admission_id")
            and self.shift_date == payload.get("shift_date")
        ):
            self.refresh_data()
            self.data_changed.emit()

    def _apply_write_failure(self, payload):
        payload = dict(payload or {})
        description = str(payload.get("description") or "oral_nutrition_write")
        exc = payload.get("exception") or RuntimeError("Неизвестная ошибка записи")
        self._write_pending = False
        self._update_actions()
        logger.warning("Oral nutrition write failed for %s: %s", description, exc, exc_info=True)
        CustomMessageBox.warning(self, "Питание", f"Не удалось сохранить данные: {exc}")
