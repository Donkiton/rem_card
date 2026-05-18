from PySide6.QtCore import Qt, QTime
from PySide6.QtWidgets import (
    QCheckBox,
    QDialogButtonBox,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from rem_card.services.diet_service import schedule_items
from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.shared.custom_message_box import CustomMessageBox


class DietTemplateDialog(BaseStyledDialog):
    def __init__(self, template=None, parent=None):
        title = "Редактирование шаблона питания" if template else "Новый шаблон питания"
        super().__init__(title, parent)
        self.setMinimumSize(520, 430)
        self.template = template
        self.setup_ui()
        self.fill_data()

    def setup_ui(self):
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Название шаблона")

        self.text_input = QLineEdit()
        self.text_input.setPlaceholderText("Описание питания")

        self.default_check = QCheckBox("Шаблон по умолчанию")

        self.schedule_table = QTableWidget()
        self.schedule_table.setColumnCount(2)
        self.schedule_table.setHorizontalHeaderLabels(["Время", "Объем, мл"])
        self.schedule_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.schedule_table.verticalHeader().setDefaultSectionSize(34)

        form = QVBoxLayout()
        form.addWidget(QLabel("Название"))
        form.addWidget(self.name_input)
        form.addWidget(QLabel("Описание"))
        form.addWidget(self.text_input)
        form.addWidget(self.default_check)
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
        for btn in buttons.buttons():
            btn.setObjectName("DialogOkBtn")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.content_layout.addWidget(buttons)

        self.btn_add_row.clicked.connect(lambda: self.add_schedule_row())
        self.btn_delete_row.clicked.connect(self.delete_selected_row)

    def fill_data(self):
        if self.template:
            self.name_input.setText(self.template.name or "")
            self.text_input.setText(self.template.diet_text or "")
            self.default_check.setChecked(bool(self.template.is_default))
            for item in schedule_items(self.template.schedule_json):
                self.add_schedule_row(item.get("time", "09:00"), item.get("amount", 200))
        if self.schedule_table.rowCount() == 0:
            self.add_schedule_row("09:00", 200)

    def add_schedule_row(self, time_text="09:00", amount=200):
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

        self.schedule_table.setCellWidget(row, 0, time_edit)
        self.schedule_table.setCellWidget(row, 1, amount_spin)

    def delete_selected_row(self):
        row = self.schedule_table.currentRow()
        if row >= 0:
            self.schedule_table.removeRow(row)

    def get_data(self):
        name = self.name_input.text().strip()
        if not name:
            CustomMessageBox.warning(self, "Ошибка", "Укажите название шаблона.")
            return None

        schedule = []
        for row in range(self.schedule_table.rowCount()):
            time_edit = self.schedule_table.cellWidget(row, 0)
            amount_spin = self.schedule_table.cellWidget(row, 1)
            if not time_edit or not amount_spin:
                continue
            schedule.append(
                {
                    "time": time_edit.time().toString("HH:mm"),
                    "amount": int(amount_spin.value()),
                }
            )

        return {
            "name": name,
            "diet_text": self.text_input.text().strip(),
            "schedule_json": schedule,
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

        frame = QFrame()
        frame.setObjectName("adminDictFrame")
        frame.setStyleSheet(
            """
            QFrame#adminDictFrame {
                border: 1.5px solid #bdc3c7;
                border-radius: 5px;
                background-color: transparent;
            }
            """
        )
        layout = QVBoxLayout(frame)

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

        main_layout.addWidget(frame)

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
