import os
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit, QLabel)
from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QIntValidator, QIcon
from rem_card.ui.shared.base_sector import BaseSectorWidget

class Sector2d(BaseSectorWidget):
    """Панель управления балансом выведения в стиле врача."""
    undo_requested = Signal()
    delete_requested = Signal()
    save_requested = Signal(int) # Новое значение для ячейки

    def __init__(self, parent=None):
        super().__init__("2д", parent)
        self.label.hide()
        self.setFrameStyle(BaseSectorWidget.NoFrame)
        self.setStyleSheet("background: transparent;")
        
        # Определяем базовый путь к иконкам
        self.rem_card_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        self.icons_dir = os.path.join(self.rem_card_root, "icon")
        
        self.setup_ui()

    def setup_ui(self):
        # Общий контейнер
        self.main_container = QWidget()
        self.main_container.setObjectName("sector_2d_main_container")
        self.main_layout_v = QVBoxLayout(self.main_container)
        # Отступы: 3px сверху и снизу для унификации
        self.main_layout_v.setContentsMargins(4, 3, 0, 3)
        self.main_layout_v.setSpacing(0)

        # 1. Шапка (Стиль врача: серый фон, закругления)
        self.header_lbl = QLabel("Выведение")
        self.header_lbl.setObjectName("sector_header")
        self.header_lbl.setAlignment(Qt.AlignCenter)
        self.header_lbl.setFixedHeight(28)
        self.main_layout_v.addWidget(self.header_lbl)

        # 2. Область контента (Белый фон, боковые границы)
        self.content_area = QWidget()
        self.content_area.setObjectName("sector_content_area")
        self.content_layout = QVBoxLayout(self.content_area)
        self.content_layout.setContentsMargins(5, 5, 5, 5)
        self.content_layout.setSpacing(10)
        
        # Секция редактирования
        self.status_lbl = QLabel("Выберите ячейку...")
        self.status_lbl.setStyleSheet("font-weight: bold; color: #495057; font-size: 13px;")
        self.status_lbl.setAlignment(Qt.AlignCenter)
        self.content_layout.addWidget(self.status_lbl)

        input_row = QHBoxLayout()
        self.edit_input = QLineEdit()
        self.edit_input.setPlaceholderText("Объем...")
        # Строгий валидатор целых чисел
        validator = QIntValidator(0, 99999, self)
        self.edit_input.setValidator(validator)
        self.edit_input.setEnabled(False)
        self.edit_input.setFixedHeight(32)
        
        self.btn_save = QPushButton(" Добавить")
        self.btn_save.setIconSize(QSize(18, 18))
        self.btn_save.setMinimumHeight(32)
        self.btn_save.setEnabled(False)
        add_icon = os.path.join(self.icons_dir, "add_vit.png")
        if os.path.exists(add_icon):
            self.btn_save.setIcon(QIcon(add_icon))
        
        input_row.addWidget(self.edit_input)
        input_row.addWidget(self.btn_save)
        self.content_layout.addLayout(input_row)
        
        # Кнопки действий
        self.btn_undo = QPushButton(" Отменить последнее")
        self.btn_undo.setIconSize(QSize(18, 18))
        self.btn_undo.setMinimumHeight(32)
        self.btn_undo.setEnabled(False)
        undo_icon = os.path.join(self.icons_dir, "icon-cancelled.png")
        if os.path.exists(undo_icon):
            self.btn_undo.setIcon(QIcon(undo_icon))
        
        self.btn_delete = QPushButton(" Удалить значение")
        self.btn_delete.setMinimumHeight(32)
        self.btn_delete.setEnabled(False)
        # Удалить оставляем в стиле danger, так как в 1б её нет
        self.btn_delete.setStyleSheet("""
            QPushButton {
                background-color: #dc3545; color: white; font-weight: bold; 
                padding: 6px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #c82333; }
            QPushButton:disabled { background-color: #bdc3c7; color: #7f8c8d; }
        """)

        self.content_layout.addStretch()
        self.content_layout.addWidget(self.btn_undo)
        self.content_layout.addWidget(self.btn_delete)
        
        self.main_layout_v.addWidget(self.content_area)

        # Стилизация под тему врача
        self.main_container.setStyleSheet("""
            QWidget#sector_2d_main_container {
                background-color: #f8f9fa !important;
            }
            QLabel#sector_header {
                font-weight: bold; 
                font-size: 14px; 
                color: #2c3e50 !important; 
                background-color: #e9ecef !important;
                border-top: 1.5px solid #bdc3c7 !important;
                border-left: 1.5px solid #bdc3c7 !important;
                border-right: 1.5px solid #bdc3c7 !important;
                border-bottom: 0.5px solid #bdc3c7 !important;
                border-top-left-radius: 5px !important;
                border-top-right-radius: 5px !important;
            }
            QWidget#sector_content_area {
                background-color: #f8f9fa !important;
                border-left: 1.5px solid #bdc3c7 !important;
                border-right: 1.5px solid #bdc3c7 !important;
                border-bottom: 1.5px solid #bdc3c7 !important;
                border-top: none !important;
                border-bottom-left-radius: 5px !important;
                border-bottom-right-radius: 5px !important;
            }
            QLineEdit {
                font-size: 14px; 
                padding: 5px;
                border: 1px solid #ced4da;
                border-radius: 4px;
                background-color: white;
            }
            QLineEdit:focus { border: 2px solid #bdc4c8; outline: none; }
            QLineEdit:disabled { background-color: #e9ecef; }
            
            QPushButton {
                font-size: 13px; 
                font-weight: bold; 
                padding: 4px 12px; 
                background-color: #ecf0f1; 
                color: #2c3e50; 
                border-radius: 5px; 
                border: 1.5px solid #bdc3c7;
            }
            QPushButton:hover { background-color: #dcdde1; }
            QPushButton:pressed { background-color: #bdc3c7; }
            QPushButton:disabled {
                background-color: #f1f2f6;
                color: #a4b0be;
                border: 1px solid #dcdde1;
                font-weight: normal;
            }
        """)

        # Подключение сигналов
        self.btn_save.clicked.connect(self._on_save_clicked)
        self.btn_undo.clicked.connect(self._on_undo_clicked)
        self.btn_delete.clicked.connect(self._on_delete_clicked)
        self.edit_input.returnPressed.connect(self.btn_save.animateClick)

        self.set_content(self.main_container)

    def set_selection(self, label_text, current_val=None, keep_focus=True):
        self.header_lbl.setText(label_text)
        self.edit_input.setEnabled(True)
        self.btn_save.setEnabled(True)
        self.btn_delete.setEnabled(current_val is not None and current_val > 0)
        
        # Если поле в фокусе и там что-то есть, не затираем при фоновом обновлении
        if self.edit_input.hasFocus() and self.edit_input.text():
            return

        if current_val is not None:
            self.edit_input.setText(str(current_val))
        else:
            self.edit_input.clear()
            
        if keep_focus:
            self.edit_input.setFocus()

    def clear_selection(self):
        self.header_lbl.setText("Выведение")
        self.edit_input.clear()
        self.edit_input.setEnabled(False)
        self.btn_save.setEnabled(False)
        self.btn_delete.setEnabled(False)

    def set_undo_active(self, active: bool):
        """Управляет активностью кнопки отмены."""
        self.btn_undo.setEnabled(active)

    def _on_save_clicked(self):
        val_text = self.edit_input.text()
        if not val_text: return
        self.save_requested.emit(int(val_text))

    def _on_undo_clicked(self):
        # Текст подтверждения как в 1б
        if CustomMessageBox.question(self, "Подтверждение", "Вы уверены, что хотите отменить последнее внесение значений?") == CustomMessageBox.Yes:
            self.undo_requested.emit()

    def _on_delete_clicked(self):
        if self._confirm("Удалить выбранное значение?"):
            self.delete_requested.emit()

    def _confirm(self, text):
        return CustomMessageBox.question(self, "Подтверждение", text) == CustomMessageBox.Yes
