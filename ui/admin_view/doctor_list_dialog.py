from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
)

from rem_card.services.doctor_list_service import DoctorListStore
from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.shared.custom_message_box import CustomMessageBox


class DoctorListDialog(BaseStyledDialog):
    def __init__(self, parent=None, store: DoctorListStore | None = None):
        super().__init__("Список врачей", parent)
        self.store = store or DoctorListStore()
        self._edit_row: int | None = None
        self.setFixedSize(640, 500)
        self._init_ui()
        self._load()

    def _init_ui(self):
        self.content_layout.setSpacing(10)
        label = QLabel("Врачи для протоколов и процедур")
        label.setWordWrap(True)
        self.content_layout.addWidget(label)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["ФИО", "Должность"])
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.itemDoubleClicked.connect(lambda *_args: self._edit_selected())
        self.content_layout.addWidget(self.table, 1)

        input_row = QHBoxLayout()
        input_row.setSpacing(8)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("ФИО врача")
        self.name_edit.returnPressed.connect(self._add_or_update_doctor)
        self.position_edit = QLineEdit()
        self.position_edit.setPlaceholderText("Должность")
        self.position_edit.returnPressed.connect(self._add_or_update_doctor)
        self.add_btn = QPushButton("Добавить")
        self.add_btn.setObjectName("DialogOkBtn")
        self.add_btn.clicked.connect(self._add_or_update_doctor)
        self.cancel_edit_btn = QPushButton("Отмена правки")
        self.cancel_edit_btn.setObjectName("DialogOkBtn")
        self.cancel_edit_btn.clicked.connect(self._cancel_edit)
        self.cancel_edit_btn.setVisible(False)
        input_row.addWidget(self.name_edit, 2)
        input_row.addWidget(self.position_edit, 2)
        input_row.addWidget(self.add_btn)
        input_row.addWidget(self.cancel_edit_btn)
        self.content_layout.addLayout(input_row)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self.edit_btn = QPushButton("Редактировать")
        self.edit_btn.setObjectName("DialogOkBtn")
        self.edit_btn.clicked.connect(self._edit_selected)
        self.delete_btn = QPushButton("Удалить")
        self.delete_btn.setObjectName("DialogOkBtn")
        self.delete_btn.clicked.connect(self._delete_selected)
        self.save_btn = QPushButton("Сохранить")
        self.save_btn.setObjectName("DialogOkBtn")
        self.save_btn.clicked.connect(self._save)
        self.close_btn = QPushButton("Закрыть")
        self.close_btn.setObjectName("DialogOkBtn")
        self.close_btn.clicked.connect(self.reject)
        buttons.addWidget(self.edit_btn)
        buttons.addWidget(self.delete_btn)
        buttons.addWidget(self.save_btn)
        buttons.addWidget(self.close_btn)
        self.content_layout.addLayout(buttons)

    def _load(self):
        try:
            doctors = self.store.load_doctor_records()
        except Exception as exc:
            doctors = []
            CustomMessageBox.warning(self, "Ошибка", f"Не удалось загрузить список врачей: {exc}")
        self.table.setRowCount(0)
        for doctor in doctors:
            self._append_row(doctor)
        if self.table.rowCount() > 0:
            self.table.selectRow(0)

    def _current_items(self):
        items = []
        for row in range(self.table.rowCount()):
            full_name = self._cell_text(row, 0)
            position = self._cell_text(row, 1)
            if full_name:
                items.append({"full_name": full_name, "position": position})
        return items

    def _append_row(self, doctor: dict[str, str]):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._set_row(row, doctor)

    def _set_row(self, row: int, doctor: dict[str, str]):
        full_name = " ".join(str(doctor.get("full_name") or "").split())
        position = " ".join(str(doctor.get("position") or "").split())
        self.table.setItem(row, 0, QTableWidgetItem(full_name))
        self.table.setItem(row, 1, QTableWidgetItem(position))

    def _cell_text(self, row: int, column: int) -> str:
        item = self.table.item(row, column)
        return " ".join(item.text().split()) if item is not None else ""

    def _input_record(self) -> dict[str, str]:
        return {
            "full_name": " ".join(self.name_edit.text().split()),
            "position": " ".join(self.position_edit.text().split()),
        }

    def _add_or_update_doctor(self):
        record = self._input_record()
        full_name = record["full_name"]
        if not full_name:
            return
        edit_row = self._edit_row
        existing = {
            self._cell_text(row, 0).lower()
            for row in range(self.table.rowCount())
            if row != edit_row
        }
        if full_name.lower() in existing:
            CustomMessageBox.warning(self, "Список врачей", "Такой врач уже есть в списке.")
            return
        if edit_row is not None and 0 <= edit_row < self.table.rowCount():
            self._set_row(edit_row, record)
            self.table.selectRow(edit_row)
            self._cancel_edit(clear_inputs=True)
            return
        self._append_row(record)
        self.table.selectRow(self.table.rowCount() - 1)
        self._clear_inputs()
        self.name_edit.setFocus(Qt.OtherFocusReason)

    def _edit_selected(self):
        row = self.table.currentRow()
        if row < 0:
            return
        self._edit_row = row
        self.name_edit.setText(self._cell_text(row, 0))
        self.position_edit.setText(self._cell_text(row, 1))
        self.add_btn.setText("Применить")
        self.cancel_edit_btn.setVisible(True)
        self.name_edit.setFocus(Qt.OtherFocusReason)

    def _cancel_edit(self, clear_inputs: bool = True):
        self._edit_row = None
        self.add_btn.setText("Добавить")
        self.cancel_edit_btn.setVisible(False)
        if clear_inputs:
            self._clear_inputs()

    def _clear_inputs(self):
        self.name_edit.clear()
        self.position_edit.clear()

    def _delete_selected(self):
        row = self.table.currentRow()
        if row < 0:
            return
        self.table.removeRow(row)
        if self._edit_row == row:
            self._cancel_edit()
        elif self._edit_row is not None and self._edit_row > row:
            self._edit_row -= 1
        if self.table.rowCount() > 0:
            self.table.selectRow(min(row, self.table.rowCount() - 1))

    def _save(self):
        try:
            self.store.save_doctor_records(self._current_items())
        except Exception as exc:
            CustomMessageBox.warning(self, "Ошибка", f"Не удалось сохранить список врачей: {exc}")
            return
        CustomMessageBox.information(self, "Список врачей", "Список врачей сохранен.")
        self.accept()
