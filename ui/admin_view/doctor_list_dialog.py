from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QVBoxLayout,
)

from rem_card.services.doctor_list_service import DoctorListStore
from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.shared.custom_message_box import CustomMessageBox


class DoctorListDialog(BaseStyledDialog):
    def __init__(self, parent=None, store: DoctorListStore | None = None):
        super().__init__("Список врачей", parent)
        self.store = store or DoctorListStore()
        self.setFixedSize(440, 420)
        self._init_ui()
        self._load()

    def _init_ui(self):
        self.content_layout.setSpacing(10)
        label = QLabel("Врачи для протокола установления смерти человека")
        label.setWordWrap(True)
        self.content_layout.addWidget(label)

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QListWidget.SingleSelection)
        self.content_layout.addWidget(self.list_widget, 1)

        input_row = QHBoxLayout()
        input_row.setSpacing(8)
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("ФИО врача")
        self.input_edit.returnPressed.connect(self._add_doctor)
        self.add_btn = QPushButton("Добавить")
        self.add_btn.setObjectName("DialogOkBtn")
        self.add_btn.clicked.connect(self._add_doctor)
        input_row.addWidget(self.input_edit, 1)
        input_row.addWidget(self.add_btn)
        self.content_layout.addLayout(input_row)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self.delete_btn = QPushButton("Удалить")
        self.delete_btn.setObjectName("DialogOkBtn")
        self.delete_btn.clicked.connect(self._delete_selected)
        self.save_btn = QPushButton("Сохранить")
        self.save_btn.setObjectName("DialogOkBtn")
        self.save_btn.clicked.connect(self._save)
        self.close_btn = QPushButton("Закрыть")
        self.close_btn.setObjectName("DialogOkBtn")
        self.close_btn.clicked.connect(self.reject)
        buttons.addWidget(self.delete_btn)
        buttons.addWidget(self.save_btn)
        buttons.addWidget(self.close_btn)
        self.content_layout.addLayout(buttons)

    def _load(self):
        try:
            doctors = self.store.load_doctors()
        except Exception as exc:
            doctors = []
            CustomMessageBox.warning(self, "Ошибка", f"Не удалось загрузить список врачей: {exc}")
        self.list_widget.clear()
        self.list_widget.addItems(doctors)
        if self.list_widget.count() > 0:
            self.list_widget.setCurrentRow(0)

    def _current_items(self):
        return [self.list_widget.item(index).text().strip() for index in range(self.list_widget.count())]

    def _add_doctor(self):
        text = " ".join(self.input_edit.text().split())
        if not text:
            return
        existing = {item.lower() for item in self._current_items()}
        if text.lower() in existing:
            CustomMessageBox.warning(self, "Список врачей", "Такой врач уже есть в списке.")
            return
        self.list_widget.addItem(text)
        self.list_widget.setCurrentRow(self.list_widget.count() - 1)
        self.input_edit.clear()
        self.input_edit.setFocus(Qt.OtherFocusReason)

    def _delete_selected(self):
        row = self.list_widget.currentRow()
        if row < 0:
            return
        item = self.list_widget.takeItem(row)
        if item is not None:
            del item
        if self.list_widget.count() > 0:
            self.list_widget.setCurrentRow(min(row, self.list_widget.count() - 1))

    def _save(self):
        try:
            self.store.save_doctors(self._current_items())
        except Exception as exc:
            CustomMessageBox.warning(self, "Ошибка", f"Не удалось сохранить список врачей: {exc}")
            return
        CustomMessageBox.information(self, "Список врачей", "Список врачей сохранен.")
        self.accept()
