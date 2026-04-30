from rem_card.ui.shared.custom_message_box import CustomMessageBox
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, 
    QTableWidget, QTableWidgetItem, QHeaderView, QLineEdit, 
    QMessageBox, QFormLayout, QDialog, QDialogButtonBox,
    QComboBox, QCheckBox, QFrame
)
from PySide6.QtCore import Qt
from rem_card.services.prescription_engine import engine
from rem_card.ui.shared.base_dialog import BaseStyledDialog

class FormDialog(BaseStyledDialog):
    def __init__(self, key="", data=None, parent=None):
        super().__init__("Редактирование лекарственной формы", parent)
        self.setFixedSize(400, 300)
        
        if data is None: data = {}
            
        self.key_input = QLineEdit(key)
        self.key_input.setPlaceholderText("Например: tablet")
        if key: self.key_input.setEnabled(False)
            
        self.name_input = QLineEdit(data.get("name_ru", ""))
        self.name_input.setPlaceholderText("Например: Таблетки")
        
        self.abbr_input = QLineEdit(data.get("latin_abbr", ""))
        self.abbr_input.setPlaceholderText("Например: S. или Tab.")
        
        self.dilute_check = QCheckBox("Можно разводить (инфузия)")
        self.dilute_check.setChecked(data.get("can_dilute", False))
        
        form_layout = QFormLayout()
        form_layout.addRow("Ключ (англ):", self.key_input)
        form_layout.addRow("Название (рус):", self.name_input)
        form_layout.addRow("Вариант сокращения:", self.abbr_input)
        form_layout.addRow("", self.dilute_check)
        
        self.content_layout.addLayout(form_layout)
        
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        for btn in self.buttons.buttons():
            btn.setObjectName("DialogOkBtn")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.content_layout.addWidget(self.buttons)
        
    def get_data(self):
        return self.key_input.text().strip(), {
            "name_ru": self.name_input.text().strip(),
            "latin_abbr": self.abbr_input.text().strip(),
            "can_dilute": self.dilute_check.isChecked()
        }

class FormsDictWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_ui()
        self.load_data()

    def setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(3, 3, 3, 3)

        self.frame = QFrame()
        self.frame.setObjectName("adminDictFrame")
        self.frame.setStyleSheet("""
            QFrame#adminDictFrame {
                border: 1.5px solid #bdc3c7;
                border-radius: 5px;
                background-color: transparent;
            }
        """)
        layout = QVBoxLayout(self.frame)
        
        header = QLabel("Справочник Лекарственных Форм")
        header.setProperty("heading", "true")
        header.setStyleSheet("border: none; background: transparent;")
        layout.addWidget(header)
        
        self.table = QTableWidget()
        self.table.setStyleSheet("background-color: white;")
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["Название", "Разведение"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(60)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.table)
        
        btn_layout = QHBoxLayout()
        self.btn_add = QPushButton("Добавить")
        self.btn_edit = QPushButton("Изменить")
        self.btn_delete = QPushButton("Удалить")
        for btn in [self.btn_add, self.btn_edit, self.btn_delete]:
            btn.setObjectName("DialogOkBtn")
            btn.setFixedHeight(35)
            btn_layout.addWidget(btn)
        layout.addLayout(btn_layout)
        
        self.btn_back = QPushButton("← Вернуться в меню")
        self.btn_back.setObjectName("DialogOkBtn")
        self.btn_back.setFixedHeight(40)
        layout.addWidget(self.btn_back)

        main_layout.addWidget(self.frame)
        
        self.btn_add.clicked.connect(self.add_item)
        self.btn_edit.clicked.connect(self.edit_item)
        self.btn_delete.clicked.connect(self.delete_item)
        
    def load_data(self):
        self.table.setRowCount(0)
        for row, (key, data) in enumerate(engine.forms.items()):
            self.table.insertRow(row)
            name_item = QTableWidgetItem(data.get("name_ru", key))
            name_item.setData(Qt.UserRole, key)
            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, QTableWidgetItem("Да" if data.get("can_dilute") else "Нет"))

    def add_item(self):
        dialog = FormDialog(parent=self)
        if dialog.exec():
            key, data = dialog.get_data()
            if not key or not data["name_ru"]: return
            engine.save_custom_form(key, data)
            self.load_data()

    def edit_item(self):
        row = self.table.currentRow()
        if row < 0: return
        key = self.table.item(row, 0).data(Qt.UserRole)
        dialog = FormDialog(key=key, data=engine.forms.get(key), parent=self)
        if dialog.exec():
            _, new_data = dialog.get_data()
            engine.save_custom_form(key, new_data)
            self.load_data()

    def delete_item(self):
        row = self.table.currentRow()
        if row < 0: return
        key = self.table.item(row, 0).data(Qt.UserRole)
        if CustomMessageBox.question(self, 'Удаление', f"Удалить форму '{key}'?") == CustomMessageBox.Yes:
            engine.delete_custom_form(key)
            self.load_data()
