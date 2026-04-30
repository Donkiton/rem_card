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

class DiluentDialog(BaseStyledDialog):
    def __init__(self, key="", data=None, parent=None):
        super().__init__("Редактирование растворителя", parent)
        self.setFixedSize(450, 400)
        
        if data is None:
            data = {}
            
        self.key_input = QLineEdit(key)
        self.key_input.setPlaceholderText("Например: ringer")
        if key:
            self.key_input.setEnabled(False)
            
        self.latin_input = QLineEdit(data.get("latin", ""))
        self.latin_input.setPlaceholderText("Например: Solutionis Ringeri")
        
        self.display_input = QLineEdit(data.get("display", ""))
        self.display_input.setPlaceholderText("Например: Sol. Ringeri")
        
        self.short_input = QLineEdit(data.get("short", "S."))
        
        self.adds_volume_combo = QComboBox()
        self.adds_volume_combo.addItems(["Да", "Нет"])
        self.adds_volume_combo.setCurrentText("Да" if data.get("adds_volume", True) else "Нет")
        
        self.volumes_input = QLineEdit(", ".join(map(str, data.get("default_volumes", []))))
        self.volumes_input.setPlaceholderText("250, 500")
        
        form_layout = QFormLayout()
        form_layout.addRow("Ключ (lat):", self.key_input)
        form_layout.addRow("Название (Латынь):", self.latin_input)
        form_layout.addRow("Отображаемое имя:", self.display_input)
        form_layout.addRow("Сокращение:", self.short_input)
        form_layout.addRow("Добавляет объем:", self.adds_volume_combo)
        form_layout.addRow("Объемы (через запятую):", self.volumes_input)
        
        self.content_layout.addLayout(form_layout)
        
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        for btn in self.buttons.buttons():
            btn.setObjectName("DialogOkBtn")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.content_layout.addWidget(self.buttons)
        
    def get_data(self):
        vols_str = self.volumes_input.text().strip()
        vols = []
        if vols_str:
            try:
                vols = [int(v.strip()) for v in vols_str.split(",")]
            except ValueError:
                CustomMessageBox.warning(self, "Ошибка", "Объемы должны быть целыми числами.")
                return None, None
                
        return self.key_input.text().strip(), {
            "latin": self.latin_input.text().strip(),
            "display": self.display_input.text().strip(),
            "short": self.short_input.text().strip(),
            "adds_volume": self.adds_volume_combo.currentText() == "Да",
            "default_volumes": vols
        }

class DiluentsDictWidget(QWidget):
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
        
        header = QLabel("Справочник Растворителей")
        header.setProperty("heading", "true")
        header.setStyleSheet("border: none; background: transparent;")
        layout.addWidget(header)
        
        self.table = QTableWidget()
        self.table.setStyleSheet("background-color: white;")
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Название (Латынь)", "Отображение", "Объемы"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
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
        for row, (key, data) in enumerate(engine.dilutions.items()):
            self.table.insertRow(row)
            
            # Латынь + Ключ в UserRole
            latin_item = QTableWidgetItem(data.get("latin", ""))
            latin_item.setData(Qt.UserRole, key)
            self.table.setItem(row, 0, latin_item)
            
            self.table.setItem(row, 1, QTableWidgetItem(data.get("display", "")))
            
            vols = ", ".join(map(str, data.get("default_volumes", [])))
            self.table.setItem(row, 2, QTableWidgetItem(vols))

    def add_item(self):
        dialog = DiluentDialog(parent=self)
        if dialog.exec():
            key, data = dialog.get_data()
            if not key or not data["latin"]:
                CustomMessageBox.warning(self, "Ошибка", "Заполните все поля.")
                return
            if key in engine.dilutions:
                CustomMessageBox.warning(self, "Ошибка", "Растворитель с таким ключом уже существует.")
                return
            
            engine.save_custom_dilution(key, data)
            self.load_data()

    def edit_item(self):
        current_row = self.table.currentRow()
        if current_row < 0:
            CustomMessageBox.warning(self, "Внимание", "Выберите растворитель для редактирования.")
            return
            
        key = self.table.item(current_row, 0).data(Qt.UserRole)
        data = engine.dilutions.get(key, {})
        
        dialog = DiluentDialog(key=key, data=data, parent=self)
        if dialog.exec():
            _, new_data = dialog.get_data()
            if not new_data["latin"]:
                CustomMessageBox.warning(self, "Ошибка", "Название не может быть пустым.")
                return
                
            engine.save_custom_dilution(key, new_data)
            self.load_data()

    def delete_item(self):
        current_row = self.table.currentRow()
        if current_row < 0:
            CustomMessageBox.warning(self, "Внимание", "Выберите растворитель для удаления.")
            return
            
        key = self.table.item(current_row, 0).data(Qt.UserRole)
        
        reply = CustomMessageBox.question(self, 'Удаление', 
                                     f"Вы уверены, что хотите удалить '{key}'?",
                                     CustomMessageBox.Yes | CustomMessageBox.No, CustomMessageBox.No)
                                     
        if reply == CustomMessageBox.Yes:
            engine.delete_custom_dilution(key)
            self.load_data()
