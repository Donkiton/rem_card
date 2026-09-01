from PySide6.QtCore import Qt, QTime
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from rem_card.services.diet_service import diet_details, schedule_items
from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.admin_view.dictionary_page_chrome import apply_dictionary_page_chrome
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.shared.window_state import SavedFramelessDialogMixin


class DietTemplateDialog(SavedFramelessDialogMixin, BaseStyledDialog):
    def __init__(self, template=None, parent=None):
        title = "Редактирование шаблона питания" if template else "Новый шаблон питания"
        super().__init__(title, parent)
        self._init_saved_frameless_dialog(
            "admin/diet_template_dialog_geometry_v2",
            drag_area_height=42,
        )
        self.setMinimumSize(900, 650)
        self.resize(900, 760)
        self.setSizeGripEnabled(True)
        self.template = template
        self.setup_ui()
        self.fill_data()
        self._restore_saved_geometry()

    def setup_ui(self):
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Название шаблона")

        self.text_input = QLineEdit()
        self.text_input.setPlaceholderText("Описание питания")

        self.default_check = QCheckBox("Шаблон по умолчанию")
        self.fractional_check = QCheckBox("Дробное питание малыми порциями")
        self.no_food_check = QCheckBox("Голод — пища не назначена")
        self.no_fluids_check = QCheckBox("Полное ограничение жидкости")
        self.on_demand_check = QCheckBox("Питьё по требованию")

        self.consistency_combo = QComboBox()
        self.consistency_combo.setEditable(True)
        self.consistency_combo.addItems(["", "Обычная", "Мягкая", "Протёртая", "Полужидкая", "Жидкая"])
        self.consistency_combo.lineEdit().setAlignment(Qt.AlignCenter)
        self.consistency_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.consistency_combo.setMinimumWidth(125)
        self.temperature_combo = QComboBox()
        self.temperature_combo.setEditable(True)
        self.temperature_combo.addItems(["", "Комнатная", "Тёплая", "Холодная"])
        self.temperature_combo.lineEdit().setAlignment(Qt.AlignCenter)
        self.temperature_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.temperature_combo.setMinimumWidth(125)
        self.salt_input = QLineEdit()
        self.salt_input.setPlaceholderText("Например: до 5 г/сут")
        self.salt_input.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.salt_input.setMinimumWidth(125)
        self.daily_fluid_spin = QSpinBox()
        self.daily_fluid_spin.setRange(0, 10000)
        self.daily_fluid_spin.setSpecialValueText("Не задан")
        self.daily_fluid_spin.setSuffix(" мл/сут")
        self.daily_fluid_spin.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.daily_fluid_spin.setMinimumWidth(125)
        self.instructions_input = QTextEdit()
        self.instructions_input.setPlaceholderText("Особые указания по кормлению")
        self.instructions_input.setFixedHeight(60)
        self.instructions_input.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.instructions_input.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.comment_input = QTextEdit()
        self.comment_input.setPlaceholderText("Общий комментарий к диете")
        self.comment_input.setFixedHeight(52)
        self.comment_input.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.comment_input.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.schedule_table = QTableWidget()
        self.schedule_table.setObjectName("DietTemplateScheduleTable")
        self.schedule_table.setColumnCount(4)
        self.schedule_table.setHorizontalHeaderLabels(["Приём пищи", "Время", "Объём, мл", "Примечание"])
        self.schedule_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.schedule_table.verticalHeader().setDefaultSectionSize(48)
        self.schedule_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.schedule_table.setSelectionMode(QTableWidget.SingleSelection)
        self.schedule_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.schedule_table.setStyleSheet(
            """
            QTableWidget#DietTemplateScheduleTable {
                outline: none;
                selection-background-color: transparent;
            }
            QTableWidget#DietTemplateScheduleTable::item:selected {
                background-color: transparent;
            }
            """
        )

        form = QVBoxLayout()
        form.addWidget(QLabel("Название"))
        form.addWidget(self.name_input)
        form.addWidget(QLabel("Описание"))
        form.addWidget(self.text_input)
        form.addWidget(self.default_check)
        form.addWidget(QLabel("Параметры диеты"))
        parameters = QGridLayout()
        parameters.setHorizontalSpacing(12)
        parameters.setVerticalSpacing(8)
        parameters.addWidget(QLabel("Консистенция"), 0, 0)
        parameters.addWidget(self.consistency_combo, 0, 1)
        parameters.addWidget(QLabel("Температура"), 0, 2)
        parameters.addWidget(self.temperature_combo, 0, 3)
        parameters.addWidget(QLabel("Соль"), 0, 4)
        parameters.addWidget(self.salt_input, 0, 5)
        parameters.addWidget(QLabel("Жидкость"), 0, 6)
        parameters.addWidget(self.daily_fluid_spin, 0, 7)
        parameters.setColumnStretch(1, 1)
        parameters.setColumnStretch(3, 1)
        parameters.setColumnStretch(5, 1)
        parameters.setColumnStretch(7, 1)
        form.addLayout(parameters)
        checks = QHBoxLayout()
        for check in (self.fractional_check, self.on_demand_check, self.no_food_check, self.no_fluids_check):
            checks.addWidget(check)
        checks.addStretch()
        form.addLayout(checks)
        form.addWidget(self.instructions_input)
        form.addWidget(self.comment_input)
        form.addWidget(QLabel("Расписание"))
        form.addWidget(self.schedule_table)

        row_buttons = QHBoxLayout()
        self.btn_add_row = QPushButton("+ время")
        self.btn_delete_row = QPushButton("Удалить строку")
        for btn in (self.btn_add_row, self.btn_delete_row):
            btn.setObjectName("DialogOkBtn")
            btn.setFixedHeight(32)
            row_buttons.addWidget(btn)
        row_buttons.addStretch()
        form.addLayout(row_buttons)

        self.content_layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        save_button = buttons.button(QDialogButtonBox.Ok)
        cancel_button = buttons.button(QDialogButtonBox.Cancel)
        save_button.setText("Сохранить")
        cancel_button.setText("Отмена")
        for button in (save_button, cancel_button):
            button.setObjectName("DialogOkBtn")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.content_layout.addWidget(buttons)

        self.on_demand_check.toggled.connect(
            lambda checked: self._clear_conflicting_check(self.no_fluids_check, checked)
        )
        self.no_fluids_check.toggled.connect(
            lambda checked: self._clear_conflicting_check(self.on_demand_check, checked)
        )
        self.no_fluids_check.toggled.connect(self._sync_no_fluids_state)
        self.no_food_check.toggled.connect(self._sync_hunger_state)
        self.btn_add_row.clicked.connect(lambda: self.add_schedule_row())
        self.btn_delete_row.clicked.connect(self.delete_selected_row)

    @staticmethod
    def _clear_conflicting_check(other_check: QCheckBox, checked: bool) -> None:
        if checked and other_check.isChecked():
            other_check.setChecked(False)

    def _sync_no_fluids_state(self, checked: bool) -> None:
        self.daily_fluid_spin.setEnabled(not bool(checked))
        if checked:
            self.daily_fluid_spin.setValue(0)

    def _sync_hunger_state(self, checked: bool) -> None:
        schedule_enabled = not bool(checked)
        self.fractional_check.setEnabled(schedule_enabled)
        self.schedule_table.setEnabled(schedule_enabled)
        self.btn_add_row.setEnabled(schedule_enabled)
        self.btn_delete_row.setEnabled(schedule_enabled)
        if checked:
            self.fractional_check.setChecked(False)
        elif self.schedule_table.rowCount() == 0:
            self.add_schedule_row("09:00", 200)

    def fill_data(self):
        if self.template:
            self.name_input.setText(self.template.name or "")
            self.text_input.setText(self.template.diet_text or "")
            self.default_check.setChecked(bool(self.template.is_default))
            details = diet_details(getattr(self.template, "details_json", "{}"))
            self.consistency_combo.setCurrentText(str(details.get("consistency") or ""))
            self.temperature_combo.setCurrentText(str(details.get("temperature") or ""))
            self.salt_input.setText(str(details.get("salt_limit") or ""))
            self.daily_fluid_spin.setValue(int(details.get("daily_fluid_ml") or 0))
            self.fractional_check.setChecked(bool(details.get("fractional")))
            self.no_food_check.setChecked(bool(details.get("no_food")))
            self.no_fluids_check.setChecked(bool(details.get("no_fluids")))
            self.on_demand_check.setChecked(bool(details.get("on_demand")))
            self.instructions_input.setPlainText(str(details.get("special_instructions") or ""))
            self.comment_input.setPlainText(str(details.get("comment") or ""))
            for item in schedule_items(self.template.schedule_json):
                self.add_schedule_row(
                    item.get("time", "09:00"), item.get("amount", 200),
                    item.get("meal", "Приём пищи"), item.get("note", ""), item.get("key"),
                )
        if self.schedule_table.rowCount() == 0 and not self.no_food_check.isChecked():
            self.add_schedule_row("09:00", 200)

    @staticmethod
    def _schedule_cell(editor: QWidget) -> QWidget:
        container = QWidget()
        container.setObjectName("DietTemplateScheduleCell")
        cell_layout = QHBoxLayout(container)
        cell_layout.setContentsMargins(4, 4, 4, 4)
        cell_layout.setSpacing(0)
        editor.setProperty("settingsSurfaceControl", True)
        editor.setMinimumHeight(36)
        editor.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        cell_layout.addWidget(editor)
        return container

    @staticmethod
    def _schedule_editor(table: QTableWidget, row: int, column: int):
        container = table.cellWidget(row, column)
        if container is None:
            return None
        layout = container.layout()
        if layout is None or layout.count() == 0:
            return container
        return layout.itemAt(0).widget()

    @staticmethod
    def _repolish_schedule_editor(editor: QWidget) -> None:
        editor.style().unpolish(editor)
        editor.style().polish(editor)
        editor.update()

    def add_schedule_row(self, time_text="09:00", amount=200, meal="Приём пищи", note="", item_key=None):
        row = self.schedule_table.rowCount()
        self.schedule_table.insertRow(row)

        time_edit = QTimeEdit()
        time_edit.setDisplayFormat("HH:mm")
        parsed = QTime.fromString(str(time_text or "09:00"), "HH:mm")
        time_edit.setTime(parsed if parsed.isValid() else QTime(9, 0))

        amount_spin = QSpinBox()
        amount_spin.setRange(1, 5000)
        amount_spin.setSuffix(" мл")
        amount_spin.setValue(int(amount or 200))

        meal_input = QLineEdit(str(meal or "Приём пищи"))
        meal_input.setProperty("item_key", str(item_key or ""))
        note_input = QLineEdit(str(note or ""))
        note_input.setPlaceholderText("Например: после ФГДС")
        editors = (meal_input, time_edit, amount_spin, note_input)
        for column, editor in enumerate(editors):
            self.schedule_table.setCellWidget(row, column, self._schedule_cell(editor))
            self._repolish_schedule_editor(editor)
        self.schedule_table.setRowHeight(row, 48)

    def delete_selected_row(self):
        row = self.schedule_table.currentRow()
        if row >= 0:
            self.schedule_table.removeRow(row)

    def get_data(self):
        name = self.name_input.text().strip()
        if not name:
            CustomMessageBox.warning(self, "Ошибка", "Укажите название шаблона.")
            return None

        no_food = self.no_food_check.isChecked()
        no_fluids = self.no_fluids_check.isChecked()
        schedule = []
        for row in range(0 if no_food else self.schedule_table.rowCount()):
            meal_input = self._schedule_editor(self.schedule_table, row, 0)
            time_edit = self._schedule_editor(self.schedule_table, row, 1)
            amount_spin = self._schedule_editor(self.schedule_table, row, 2)
            note_input = self._schedule_editor(self.schedule_table, row, 3)
            if not meal_input or not time_edit or not amount_spin:
                continue
            schedule.append(
                {
                    "key": meal_input.property("item_key") or f"{time_edit.time().toString('HH:mm')}-{row + 1}",
                    "meal": meal_input.text().strip() or "Приём пищи",
                    "time": time_edit.time().toString("HH:mm"),
                    "amount": int(amount_spin.value()),
                    "note": note_input.text().strip() if note_input else "",
                }
            )

        return {
            "name": name,
            "diet_text": self.text_input.text().strip(),
            "schedule_json": schedule,
            "details_json": {
                "consistency": self.consistency_combo.currentText().strip(),
                "temperature": self.temperature_combo.currentText().strip(),
                "salt_limit": self.salt_input.text().strip(),
                "fractional": self.fractional_check.isChecked(),
                "daily_fluid_ml": None if no_fluids else (self.daily_fluid_spin.value() or None),
                "special_instructions": self.instructions_input.toPlainText().strip(),
                "comment": self.comment_input.toPlainText().strip(),
                "no_food": no_food,
                "no_fluids": no_fluids,
                "on_demand": self.on_demand_check.isChecked(),
            },
            "is_default": self.default_check.isChecked(),
            "version": getattr(self.template, "version", None),
        }


class DietTemplatesWidget(QWidget):
    def __init__(self, service=None, role="admin", parent=None):
        super().__init__(parent)
        self.service = service
        self.role = role
        self._templates_by_id = {}
        self.setup_ui()
        self.load_data()

    def setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(3, 3, 3, 3)

        self.frame = QFrame()
        self.frame.setObjectName("adminDictFrame")
        self.frame.setStyleSheet(
            """
            QFrame#adminDictFrame {
                border: 1.5px solid #bdc3c7;
                border-radius: 5px;
                background-color: transparent;
            }
            """
        )
        layout = QVBoxLayout(self.frame)

        header = QLabel("Шаблоны питания")
        header.setProperty("heading", "true")
        header.setStyleSheet("border: none; background: transparent;")
        layout.addWidget(header)

        self.table = QTableWidget()
        self.table.setStyleSheet("background-color: white;")
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Название", "Описание", "Расписание", "По умолчанию"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.table)

        btn_layout = QHBoxLayout()
        self.btn_move_up = QPushButton("↑")
        self.btn_move_down = QPushButton("↓")
        self.btn_add = QPushButton("Добавить")
        self.btn_edit = QPushButton("Изменить")
        self.btn_delete = QPushButton("Удалить")
        for btn in (self.btn_move_up, self.btn_move_down, self.btn_add, self.btn_edit, self.btn_delete):
            btn.setObjectName("DialogOkBtn")
            btn.setFixedHeight(35)
            btn_layout.addWidget(btn)
        self.btn_move_up.setFixedWidth(45)
        self.btn_move_down.setFixedWidth(45)
        layout.addLayout(btn_layout)

        self.btn_back = QPushButton("← Вернуться в меню")
        self.btn_back.setObjectName("DialogOkBtn")
        self.btn_back.setFixedHeight(40)
        layout.addWidget(self.btn_back)

        main_layout.addWidget(self.frame)

        apply_dictionary_page_chrome(
            self,
            frame=self.frame,
            header_label=header,
            table=self.table,
            back_button=self.btn_back,
            title="Шаблоны питания",
            description=(
                "Типовые схемы питания, расписание и шаблон "
                "по умолчанию для назначений."
            ),
            primary_buttons=(self.btn_add,),
            secondary_buttons=(self.btn_edit,),
            danger_buttons=(self.btn_delete,),
            icon_buttons=(self.btn_move_up, self.btn_move_down),
        )

        self.table.itemSelectionChanged.connect(self._update_reorder_buttons)
        self.btn_move_up.clicked.connect(self.move_selected_template_up)
        self.btn_move_down.clicked.connect(self.move_selected_template_down)
        self.btn_add.clicked.connect(self.add_template)
        self.btn_edit.clicked.connect(self.edit_template)
        self.btn_delete.clicked.connect(self.delete_template)
        self._update_reorder_buttons()

    def set_service(self, service):
        self.service = service
        self.load_data()

    def can_edit(self):
        return self.role in ("admin", "doctor", "Врач")

    def load_data(self, selected_template_id=None):
        self.table.setRowCount(0)
        self._templates_by_id = {}
        can_edit = self.can_edit() and bool(self.service)
        for btn in (self.btn_add, self.btn_edit, self.btn_delete):
            btn.setEnabled(can_edit)
        self._update_reorder_buttons()

        if not self.service or not hasattr(self.service, "list_diet_templates"):
            return

        try:
            templates = self.service.list_diet_templates()
        except Exception as exc:
            CustomMessageBox.warning(self, "Предупреждение", f"Не удалось загрузить шаблоны питания: {exc}")
            return

        for row, tpl in enumerate(templates):
            self._templates_by_id[int(tpl.id)] = tpl
            self.table.insertRow(row)
            schedule = ", ".join(f"{item['time']} - {item['amount']} мл" for item in schedule_items(tpl.schedule_json))

            name_item = QTableWidgetItem(tpl.name or "")
            name_item.setData(Qt.UserRole, int(tpl.id))
            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, QTableWidgetItem(tpl.diet_text or ""))
            self.table.setItem(row, 2, QTableWidgetItem(schedule))
            self.table.setItem(row, 3, QTableWidgetItem("Да" if tpl.is_default else "Нет"))
            if selected_template_id is not None and int(tpl.id) == int(selected_template_id):
                self.table.setCurrentCell(row, 0)
                self.table.selectRow(row)
        self._update_reorder_buttons()

    def current_template(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        if item is None:
            return None
        return self._templates_by_id.get(int(item.data(Qt.UserRole)))

    def add_template(self):
        if not self._ensure_service():
            return
        dialog = DietTemplateDialog(parent=self)
        if dialog.exec():
            data = dialog.get_data()
            if data:
                self._enqueue_write("diet_template_create", lambda: self.service.create_diet_template(**data_without_version(data)))

    def edit_template(self):
        if not self._ensure_service():
            return
        template = self.current_template()
        if not template:
            return
        dialog = DietTemplateDialog(template=template, parent=self)
        if dialog.exec():
            data = dialog.get_data()
            if data:
                self._enqueue_write(
                    "diet_template_update",
                    lambda: self.service.update_diet_template(
                        template.id,
                        name=data["name"],
                        diet_text=data["diet_text"],
                        schedule_json=data["schedule_json"],
                        is_default=data["is_default"],
                        details_json=data["details_json"],
                        expected_version=data["version"],
                    ),
                )

    def delete_template(self):
        if not self._ensure_service():
            return
        template = self.current_template()
        if not template:
            return
        if CustomMessageBox.question(self, "Удаление", f"Удалить шаблон '{template.name}'?") != CustomMessageBox.Yes:
            return
        self._enqueue_write(
            "diet_template_delete",
            lambda: self.service.delete_diet_template(template.id, expected_version=template.version),
        )

    def _template_ids_in_table(self):
        ids = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is None:
                continue
            ids.append(int(item.data(Qt.UserRole)))
        return ids

    def _update_reorder_buttons(self):
        row = self.table.currentRow() if hasattr(self, "table") else -1
        row_count = self.table.rowCount() if hasattr(self, "table") else 0
        can_reorder = (
            self.can_edit()
            and bool(self.service)
            and hasattr(self.service, "reorder_diet_templates")
            and row_count > 0
            and 0 <= row < row_count
        )
        if hasattr(self, "btn_move_up"):
            self.btn_move_up.setEnabled(can_reorder and row > 0)
        if hasattr(self, "btn_move_down"):
            self.btn_move_down.setEnabled(can_reorder and row < row_count - 1)

    def _move_selected_template(self, target_row: int):
        if not self._ensure_service():
            return
        if not hasattr(self.service, "reorder_diet_templates"):
            CustomMessageBox.warning(self, "Предупреждение", "Сервис не поддерживает изменение порядка шаблонов питания.")
            return
        row = self.table.currentRow()
        ids = self._template_ids_in_table()
        if row < 0 or row >= len(ids) or target_row < 0 or target_row >= len(ids):
            return
        selected_id = ids[row]
        ids[row], ids[target_row] = ids[target_row], ids[row]
        self._enqueue_write(
            "diet_template_reorder",
            lambda order=ids: self.service.reorder_diet_templates(order),
            selected_template_id=selected_id,
        )

    def move_selected_template_up(self):
        self._move_selected_template(self.table.currentRow() - 1)

    def move_selected_template_down(self):
        self._move_selected_template(self.table.currentRow() + 1)

    def _ensure_service(self):
        if not self.service:
            CustomMessageBox.warning(self, "Предупреждение", "Сервис шаблонов питания недоступен.")
            return False
        if not self.can_edit():
            CustomMessageBox.warning(self, "Предупреждение", "Редактирование шаблонов питания недоступно для этой роли.")
            return False
        return True

    def _enqueue_write(self, description, operation, selected_template_id=None):
        def reload_after_write(result=None):
            target_id = selected_template_id
            if target_id is None:
                try:
                    target_id = int(result)
                except (TypeError, ValueError):
                    target_id = None
            self.load_data(selected_template_id=target_id)

        if hasattr(self.service, "enqueue_write"):
            self.service.enqueue_write(
                description=description,
                operation=operation,
                on_success=reload_after_write,
                on_error=lambda exc: CustomMessageBox.warning(self, "Предупреждение", f"Ошибка сохранения шаблона: {exc}"),
            )
            return
        try:
            result = operation()
            reload_after_write(result)
        except Exception as exc:
            CustomMessageBox.warning(self, "Предупреждение", f"Ошибка сохранения шаблона: {exc}")


def data_without_version(data):
    return {k: v for k, v in data.items() if k != "version"}
