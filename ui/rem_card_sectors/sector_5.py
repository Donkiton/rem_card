from PySide6.QtWidgets import (QWidget, QVBoxLayout, QScrollArea, QLabel)
from PySide6.QtCore import Qt
from rem_card.ui.shared.base_sector import BaseSectorWidget

class Sector5(BaseSectorWidget):
    def __init__(self, parent=None):
        super().__init__("5", parent)
        self.label.hide() # Скрываем стандартный заголовок
        self.setFrameStyle(BaseSectorWidget.NoFrame)
        self.setStyleSheet("background: transparent;")
        self.setup_ui()

    def setup_ui(self):
        if self.layout():
            self.layout().setContentsMargins(0, 0, 0, 0)

        self.main_container = QWidget()
        self.main_container.setObjectName("sector_5_main_container")
        self.main_layout_v = QVBoxLayout(self.main_container)
        self.main_layout_v.setContentsMargins(0, 1, 0, 5)
        self.main_layout_v.setSpacing(0)

        self.header_lbl = QLabel("Перорально")
        self.header_lbl.setObjectName("sector_header")
        self.header_lbl.setAlignment(Qt.AlignCenter)
        self.header_lbl.setFixedHeight(30)
        self.main_layout_v.addWidget(self.header_lbl)

        self.data_area = QWidget()
        self.data_area.setObjectName("sector_data_area")
        self.data_layout = QVBoxLayout(self.data_area)
        self.data_layout.setContentsMargins(5, 5, 5, 5)
        self.data_layout.setSpacing(0)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setStyleSheet("background: transparent; border: none;")

        self.scroll_content = QWidget()
        self.scroll_content.setObjectName("sector_scroll_content")
        self.main_scroll_layout = QVBoxLayout(self.scroll_content)
        self.main_scroll_layout.setContentsMargins(0, 0, 0, 0)
        self.main_scroll_layout.setSpacing(0)

        self.cards_container = QWidget()
        self.scroll_layout = QVBoxLayout(self.cards_container)
        self.scroll_layout.setContentsMargins(0, 0, 0, 0)
        self.scroll_layout.setSpacing(0)
        
        self.main_scroll_layout.addWidget(self.cards_container, 0)
        self.main_scroll_layout.addStretch(1)
        
        self.scroll_area.setWidget(self.scroll_content)
        self.data_layout.addWidget(self.scroll_area)
        self.main_layout_v.addWidget(self.data_area, 1)

        self.main_container.setStyleSheet("""
            QWidget#sector_5_main_container {
                background-color: #f8f9fa;
            }
            QLabel#sector_header {
                font-weight: bold;
                font-size: 14px;
                color: #2c3e50;
                background-color: #e9ecef;
                border-top: 1.5px solid #bdc3c7;
                border-left: 1.5px solid #bdc3c7;
                border-right: 1.5px solid #bdc3c7;
                border-bottom: 0.5px solid #bdc3c7;
                border-top-left-radius: 5px;
                border-top-right-radius: 5px;
            }
            QWidget#sector_data_area {
                background-color: #f8f9fa;
                border-left: 1.5px solid #bdc3c7;
                border-right: 1.5px solid #bdc3c7;
                border-bottom: 1.5px solid #bdc3c7;
                border-bottom-left-radius: 5px;
                border-bottom-right-radius: 5px;
                border-top: none;
            }
            QWidget#sector_scroll_content {
                background-color: transparent;
            }
        """)

        self.set_content_raw(self.main_container)

    def set_content_raw(self, widget):
        """Базовый метод установки контента в QFrame"""
        super().set_content(widget)

    def set_content(self, widget):
        """Переопределяем, чтобы виджеты добавлялись в scroll_layout"""
        # Очищаем всё в контейнере карточек
        for i in reversed(range(self.scroll_layout.count())):
            item = self.scroll_layout.itemAt(i)
            if item.widget():
                old_widget = item.widget()
                old_widget.hide()
                old_widget.setParent(None)

        if widget:
            if widget.objectName() == "diet_intake_widget" and hasattr(widget, "set_external_sector_header"):
                widget.set_external_sector_header(True)
            self.scroll_layout.addWidget(widget)

    def add_card(self, widget):
        """Добавляет карточку в начало списка."""
        self.scroll_layout.insertWidget(0, widget)
