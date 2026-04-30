from rem_card.ui.shared.custom_message_box import CustomMessageBox
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, 
    QTableWidget, QTableWidgetItem, QHeaderView, QLineEdit, 
    QMessageBox, QFormLayout, QDialog, QDialogButtonBox,
    QComboBox, QFrame
)
from PySide6.QtCore import Qt
from rem_card.services.prescription_engine import engine

from rem_card.ui.shared.base_dialog import BaseStyledDialog

class GroupDialog(BaseStyledDialog):
    def __init__(self, key="", data=None, parent=None):
        super().__init__("Редактирование группы", parent)
        self.setFixedSize(450, 250)
        
        if data is None:
            data = {}
            
        self.key_input = QLineEdit(key)
        self.key_input.setPlaceholderText("Например: antibiotics")
        if key:
            self.key_input.setEnabled(False)
            
        self.name_input = QLineEdit(data.get("name_ru", ""))
        self.name_input.setPlaceholderText("Например: Антибиотики")
        
        # Сохраняем неиспользуемые более данные, чтобы не ломать структуру
        self.priority_level = data.get("priority_level", 3)
        self.offset_min = data.get("offset_min", 0)
        self.color = data.get("color", "#bdc3c7")
        
        form_layout = QFormLayout()
        form_layout.addRow("Ключ (lat):", self.key_input)
        form_layout.addRow("Название (ru):", self.name_input)
        
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
            "priority_level": self.priority_level,
            "offset_min": self.offset_min,
            "color": self.color
        }

class GroupsDictWidget(QWidget):
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
        
        # Заголовок
        header = QLabel("Справочник Фармакологических Групп")
        header.setProperty("heading", "true")
        header.setStyleSheet("border: none; background: transparent;")
        layout.addWidget(header)
        
        # Таблица
        self.table = QTableWidget()
        self.table.setStyleSheet("background-color: white;")
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["Ключ", "Название"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(60) # Увеличена высота строки в 2 раза
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.table)
        
        # Кнопки
        btn_layout = QHBoxLayout()
        self.btn_add = QPushButton("Добавить")
        self.btn_edit = QPushButton("Изменить")
        self.btn_delete = QPushButton("Удалить")
        
        for btn in [self.btn_add, self.btn_edit, self.btn_delete]:
            btn.setObjectName("DialogOkBtn")
            btn.setFixedHeight(35)
            btn_layout.addWidget(btn)
            
        layout.addLayout(btn_layout)
        
        # Кнопка Назад
        self.btn_back = QPushButton("← Вернуться в меню")
        self.btn_back.setObjectName("DialogOkBtn")
        self.btn_back.setFixedHeight(40)
        layout.addWidget(self.btn_back)

        main_layout.addWidget(self.frame)
        
        # Подключение сигналов
        self.btn_add.clicked.connect(self.add_group)
        self.btn_edit.clicked.connect(self.edit_group)
        self.btn_delete.clicked.connect(self.delete_group)
        
    def load_data(self):
        self.table.setRowCount(0)
        # Сортируем группы для красоты
        for row, (key, data) in enumerate(sorted(engine.groups.items(), key=lambda x: x[1].get('name_ru', x[0]))):
            self.table.insertRow(row)
            
            # Ключ
            key_item = QTableWidgetItem(key)
            key_item.setData(Qt.UserRole, key)
            self.table.setItem(row, 0, key_item)
            
            # Название
            name_item = QTableWidgetItem(data.get("name_ru", ""))
            self.table.setItem(row, 1, name_item)

    def add_group(self):
        dialog = GroupDialog(parent=self)
        if dialog.exec():
            key, data = dialog.get_data()
            if not key or not data["name_ru"]:
                CustomMessageBox.warning(self, "Ошибка", "Заполните все поля.")
                return
            if key in engine.groups:
                CustomMessageBox.warning(self, "Ошибка", "Группа с таким ключом уже существует.")
                return
            
            engine.save_custom_group(key, data)
            self.load_data()

    def edit_group(self):
        current_row = self.table.currentRow()
        if current_row < 0:
            CustomMessageBox.warning(self, "Внимание", "Выберите группу для редактирования.")
            return
            
        key = self.table.item(current_row, 0).data(Qt.UserRole)
        data = engine.groups.get(key, {})
        
        dialog = GroupDialog(key=key, data=data, parent=self)
        if dialog.exec():
            _, new_data = dialog.get_data()
            if not new_data["name_ru"]:
                CustomMessageBox.warning(self, "Ошибка", "Название не может быть пустым.")
                return
                
            engine.save_custom_group(key, new_data)
            self.load_data()

    def delete_group(self):
        current_row = self.table.currentRow()
        if current_row < 0:
            CustomMessageBox.warning(self, "Внимание", "Выберите группу для удаления.")
            return
            
        key = self.table.item(current_row, 0).data(Qt.UserRole)
        
        reply = CustomMessageBox.question(self, 'Удаление', 
                                     f"Вы уверены, что хотите удалить группу '{key}'?",
                                     CustomMessageBox.Yes | CustomMessageBox.No, CustomMessageBox.No)
                                     
        if reply == CustomMessageBox.Yes:
            engine.delete_custom_group(key)
            self.load_data()
