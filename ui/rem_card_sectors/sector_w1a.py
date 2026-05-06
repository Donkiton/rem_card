from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton)
from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import QIcon
import os
from rem_card.ui.shared.base_sector import BaseSectorWidget

class SectorW1a(BaseSectorWidget):
    """Сектор W1а, отображаемый в режиме списка коек (вместо 1а)."""
    open_statistics_requested = Signal()

    def __init__(self, parent=None):
        super().__init__("W1а", parent)
        self.label.hide()
        self.setFrameStyle(BaseSectorWidget.NoFrame)
        self.setStyleSheet("background: transparent;")
        self.init_ui()

    def init_ui(self):
        self.main_container = QWidget(self)
        self.main_container.setObjectName("sector_w1a_main_container")
        self.main_layout_v = QVBoxLayout(self.main_container)
        # Отступы для унификации
        self.main_layout_v.setContentsMargins(3, 5, 5, 4)
        
        self.btn_stats = QPushButton("Статистика по препаратам", self.main_container)
        from rem_card.app.paths import get_icon_dir
        icon_path = os.path.join(get_icon_dir(), "drugs-nurse.png")
        if os.path.exists(icon_path):
            self.btn_stats.setIcon(QIcon(icon_path))
            self.btn_stats.setIconSize(QSize(24, 24))
        
        self.btn_stats.setStyleSheet("""
            QPushButton {
                background-color: #e9ecef;
                border: 1px solid #ced4da;
                border-radius: 4px;
                padding: 5px;
                color: black;
            }
            QPushButton:hover {
                background-color: #dee2e6;
            }
        """)
        self.btn_stats.clicked.connect(self.open_statistics_requested.emit)
        self.btn_stats.hide() # По умолчанию скрыта, включаем только для медсестры
        self.main_layout_v.addWidget(self.btn_stats)
        
        # W1а в данном контексте остается пустым согласно ТЗ
        self.empty_label = QLabel("Сектор W1а", self.main_container)
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setStyleSheet("color: #bdc3c7; font-style: italic;")
        self.main_layout_v.addWidget(self.empty_label)
        
        self.main_layout_v.addStretch()

        self.main_container.setStyleSheet("""
            QWidget#sector_w1a_main_container {
                background-color: #f8f9fa;
                border: 1.5px solid #bdc3c7;
                border-radius: 5px;
            }
        """)

        self.set_content(self.main_container)

    def set_stats_button_visible(self, visible: bool):
        if hasattr(self, 'btn_stats'):
            self.btn_stats.setVisible(visible)
