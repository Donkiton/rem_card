import os
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame)
from PySide6.QtCore import Qt
from rem_card.ui.shared.base_sector import BaseSectorWidget
from rem_card.ui.styles.context_menu_style import install_russian_context_menus

class Sector1b(BaseSectorWidget):
    def __init__(self, parent=None):
        super().__init__("1б", parent)
        self.label.hide() # Скрываем стандартный заголовок
        self.setFrameStyle(BaseSectorWidget.NoFrame)
        self.setStyleSheet("background: transparent;")

        self.init_ui()

    def init_ui(self):
        if self.layout():
            self.layout().setContentsMargins(3, 0, 0, 0)

        # Общий контейнер
        self.main_container = QWidget()
        self.main_container.setObjectName("sector_1b_main_container")
        self.main_layout_v = QVBoxLayout(self.main_container)
        self.main_layout_v.setContentsMargins(0, 2, 5, 5) # Слева 0, сверху 2 (близко к 2.5)
        self.main_layout_v.setSpacing(0)
        
        # 1. Шапка (Стиль с серым фоном и верхними закруглениями)
        self.header_lbl = QLabel("Ввод данных")
        self.header_lbl.setObjectName("sector_header")
        self.header_lbl.setAlignment(Qt.AlignCenter)
        self.header_lbl.setFixedHeight(28) # Было 30
        self.main_layout_v.addWidget(self.header_lbl)

        # 2. Область контента (Белый фон, боковые границы)
        self.content_area = QWidget()
        self.content_area.setObjectName("sector_content_area")
        self.content_layout = QVBoxLayout(self.content_area)
        self.content_layout.setContentsMargins(0, 0, 0, 0) # Контент сам управляет отступами
        self.content_layout.setSpacing(0)
        self.main_layout_v.addWidget(self.content_area)

        # Применяем QSS стили в стиле сектора 3б
        self.main_container.setStyleSheet("""
            QWidget#sector_1b_main_container {
                background-color: #f8f9fa !important;
            }
            QWidget#sector_header {
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
            QLineEdit:focus {
                border: 2px solid #bdc4c8; 
                outline: none;
            }
            QComboBox {
                font-size: 14px; 
                padding: 4px;
                border: 1px solid #ced4da;
                border-radius: 4px;
                background-color: white;
            }
            QComboBox:focus {
                border: 2px solid #bdc4c8;
            }
        """)

        self.set_content_raw(self.main_container)

    def set_content_raw(self, widget):
        """Базовый метод установки контента в QFrame"""
        super().set_content(widget)

    def set_content(self, widget):
        """Переопределяем, чтобы виджет вставлялся во внутреннюю область данных"""
        # Очищаем внутренний лейаут
        for i in reversed(range(self.content_layout.count())): 
            item = self.content_layout.itemAt(i)
            if item.widget():
                item.widget().setParent(None)
        
        if widget:
            self.content_layout.addWidget(widget)
            install_russian_context_menus(widget)
