from rem_card.ui.shared.custom_message_box import CustomMessageBox
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, 
    QTableWidget, QTableWidgetItem, QHeaderView, QLineEdit, 
    QMessageBox, QFormLayout, QDialog, QDialogButtonBox, QFrame
)
from PySide6.QtCore import Qt
from rem_card.services.prescription_engine import engine
from rem_card.ui.shared.base_dialog import BaseStyledDialog

class AdminTypeDialog(BaseStyledDialog):
    def __init__(self, key="", data=None, parent=None):
        super().__init__("Редактирование типа введения", parent)
        self.setFixedSize(400, 250)
        
        if data is None:
            data = {}
            
        self.key_input = QLineEdit(key)
        self.key_input.setPlaceholderText("Например: infusion")
        if key:
            self.key_input.setEnabled(False)
            
        self.name_ru_input = QLineEdit(data.get("name_ru", ""))
        self.name_ru_input.setPlaceholderText("Например: В/в капельно")
        
        form_layout = QFormLayout()
        form_layout.addRow("Ключ (англ):", self.key_input)
        form_layout.addRow("Название (RU):", self.name_ru_input)
        
        self.content_layout.addLayout(form_layout)
        
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        for btn in self.buttons.buttons():
            btn.setObjectName("DialogOkBtn")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.content_layout.addWidget(self.buttons)
        
    def get_data(self):
        return self.key_input.text().strip(), {
            "name_ru": self.name_ru_input.text().strip()
        }

class AdminTypesDictWidget(QWidget):
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
        
        header = QLabel("Справочник Типов Введения")
        header.setProperty("heading", "true")
        header.setStyleSheet("border: none; background: transparent;")
        layout.addWidget(header)
        
        self.table = QTableWidget()
        self.table.setStyleSheet("background-color: white;")
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["Ключ", "Название (RU)"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.itemDoubleClicked.connect(self.edit_item)
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
        for key, data in engine.admin_types.items():
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(key))
            self.table.setItem(row, 1, QTableWidgetItem(data.get("name_ru", "")))

    def add_item(self):
        dialog = AdminTypeDialog(parent=self)
        if dialog.exec():
            key, data = dialog.get_data()
            if not key or not data["name_ru"]:
                return
            if key in engine.admin_types:
                CustomMessageBox.warning(self, "Ошибка", "Такой ключ уже существует.")
                return
            
            engine.save_custom_admin_type(key, data)
            self.load_data()

    def edit_item(self):
        current_row = self.table.currentRow()
        if current_row < 0: return
        
        key = self.table.item(current_row, 0).text()
        data = engine.admin_types.get(key, {})
        
        dialog = AdminTypeDialog(key=key, data=data, parent=self)
        if dialog.exec():
            _, new_data = dialog.get_data()
            engine.save_custom_admin_type(key, new_data)
            self.load_data()

    def delete_item(self):
        current_row = self.table.currentRow()
        if current_row < 0: return
        
        key = self.table.item(current_row, 0).text()
        
        reply = CustomMessageBox.question(self, 'Удаление', 
                                     f"Вы уверены, что хотите удалить тип введения '{key}'?",
                                     CustomMessageBox.Yes | CustomMessageBox.No, CustomMessageBox.No)
                                     
        if reply == CustomMessageBox.Yes:
            engine.delete_custom_admin_type(key)
            self.load_data()
