import os
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame)
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from rem_card.ui.shared.base_sector import BaseSectorWidget

class Sector3a(BaseSectorWidget):
    def __init__(self, parent=None):
        super().__init__("3а", parent)
        self.label.hide() # Скрываем стандартный заголовок
        self.setFrameStyle(BaseSectorWidget.NoFrame)
        self.setStyleSheet("background: transparent;")

        self.rem_card_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.icons_dir = os.path.join(self.rem_card_root, "icon")

        self.init_ui()

    def init_ui(self):
        # Общий контейнер
        self.main_container = QWidget()
        self.main_container.setObjectName("sector_3a_main_container")
        self.main_container = QWidget()
        self.main_container.setObjectName("sector_3a_main_container")
        self.main_layout_v = QVBoxLayout(self.main_container)
        self.main_layout_v.setContentsMargins(5, 1, 0, 5)
        self.main_layout_v.setSpacing(0)
        
        # 1. Шапка "Баланс жидкости" (Стиль как у "Показатели" в 2г)
        self.header_lbl = QLabel("Баланс жидкости")
        self.header_lbl.setObjectName("balance_header")
        self.header_lbl.setAlignment(Qt.AlignCenter)
        self.header_lbl.setFixedHeight(30)
        self.main_layout_v.addWidget(self.header_lbl)

        # 2. Область данных (Стиль как у легенды в 2г)
        self.data_area = QWidget()
        self.data_area.setObjectName("balance_data_area")
        self.data_layout = QVBoxLayout(self.data_area)
        self.data_layout.setContentsMargins(10, 10, 10, 10)
        self.data_layout.setSpacing(8)

        # Заголовок "Введено" внутри области данных
        header_layout = QHBoxLayout()
        header_lbl_in = QLabel("Всего:")
        header_lbl_in.setStyleSheet("font-weight: bold; font-size: 14px; color: #495057; border: none; background: transparent;")
        self.total_in_val = QLabel("0 мл")
        self.total_in_val.setStyleSheet("font-weight: bold; color: #28a745; font-size: 14px; border: none; background: transparent;")
        header_layout.addWidget(header_lbl_in)
        header_layout.addStretch()
        header_layout.addWidget(self.total_in_val)
        self.data_layout.addLayout(header_layout)

        # Разделитель
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color: #e0e0e0; border: none; background-color: #e0e0e0; max-height: 1px;")
        self.data_layout.addWidget(line)

        # Поля данных
        self.infusion_val = self.add_balance_row("Инфузии", "balans_infuzia.png", "0 мл")
        self.preparats_val = self.add_balance_row("Препараты", "balans_preparat.png", "0 мл")
        self.blood_val = self.add_balance_row("Кровь", "balans_blood.png", "0 мл")
        self.plasma_val = self.add_balance_row("Плазма", "balans_plasma.png", "0 мл")
        self.oral_val = self.add_balance_row("Перорально", "diet.png", "0 мл")

        self.data_layout.addStretch()
        self.main_layout_v.addWidget(self.data_area)

        # Применяем QSS стили, аналогичные сектору 3б (эталон)
        self.main_container.setStyleSheet("""
            QWidget#sector_3a_main_container {
                background-color: #f8f9fa !important;
            }
            QWidget#balance_header {
                font-weight: bold; 
                font-size: 15px; 
                color: #2c3e50 !important; 
                background-color: #e9ecef !important;
                border-top: 1.5px solid #bdc3c7 !important;
                border-left: 1.5px solid #bdc3c7 !important;
                border-right: 1.5px solid #bdc3c7 !important;
                border-bottom: 0.5px solid #bdc3c7 !important;
                border-top-left-radius: 5px !important;
                border-top-right-radius: 5px !important;
            }
            QWidget#balance_data_area {
                background-color: #f8f9fa !important;
                border-left: 1.5px solid #bdc3c7 !important;
                border-right: 1.5px solid #bdc3c7 !important;
                border-top: none !important;
                border-bottom: 1.5px solid #bdc3c7 !important;
                border-bottom-left-radius: 5px !important;
                border-bottom-right-radius: 5px !important;
            }
        """)

        self.set_content(self.main_container)

    def add_balance_row(self, title, icon_name, value):
        row_layout = QHBoxLayout()
        
        label_container = QHBoxLayout()
        label_container.setSpacing(5)
        
        icon_lbl = QLabel()
        icon_lbl.setStyleSheet("border: none; background: transparent;")
        icon_path = os.path.join(self.icons_dir, icon_name)
        
        if os.path.exists(icon_path):
            pixmap = QPixmap(icon_path)
            scaled_pixmap = pixmap.scaledToHeight(14, Qt.SmoothTransformation)
            icon_lbl.setPixmap(scaled_pixmap)
        else:
            alt_path = os.path.join("icon", icon_name)
            if os.path.exists(alt_path):
                pixmap = QPixmap(alt_path)
                scaled_pixmap = pixmap.scaledToHeight(14, Qt.SmoothTransformation)
                icon_lbl.setPixmap(scaled_pixmap)
            else:
                icon_lbl.setFixedSize(14, 14)
            
        text_lbl = QLabel(title)
        text_lbl.setStyleSheet("font-size: 12px; color: #495057; border: none; background: transparent;")
        
        label_container.addWidget(icon_lbl)
        label_container.addWidget(text_lbl)
        
        val_lbl = QLabel(value)
        val_lbl.setStyleSheet("font-weight: 600; color: #495057; font-size: 12px; border: none; background: transparent;")
        
        row_layout.addLayout(label_container)
        row_layout.addStretch()
        row_layout.addWidget(val_lbl)
        
        self.data_layout.addLayout(row_layout)
        return val_lbl

    def update_values(self, total, infusion, preparats, blood, plasma,
                      total_daily=0, infusion_daily=0, preparats_daily=0, blood_daily=0, plasma_daily=0,
                      oral=0, oral_daily=0):
        # Округляем для красоты
        self.total_in_val.setText(f"{int(total)}/{int(total_daily)} мл")
        self.infusion_val.setText(f"{int(infusion)}/{int(infusion_daily)} мл")
        self.preparats_val.setText(f"{int(preparats)}/{int(preparats_daily)} мл")
        self.blood_val.setText(f"{int(blood)}/{int(blood_daily)} мл")
        self.plasma_val.setText(f"{int(plasma)}/{int(plasma_daily)} мл")
        self.oral_val.setText(f"{int(oral)}/{int(oral_daily)} мл")

    def set_loading_state(self):
        self.total_in_val.setText("—/— мл")
        self.infusion_val.setText("—/— мл")
        self.preparats_val.setText("—/— мл")
        self.blood_val.setText("—/— мл")
        self.plasma_val.setText("—/— мл")
        self.oral_val.setText("—/— мл")
