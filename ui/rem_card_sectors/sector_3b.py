import os
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QLineEdit)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap, QIntValidator
from rem_card.ui.shared.base_sector import BaseSectorWidget

class Sector3b(BaseSectorWidget):
    out_values_changed = Signal(int) # Сигнал при изменении выведенного объема

    def __init__(self, parent=None):
        super().__init__("3б", parent)
        self.label.hide() # Скрываем стандартный заголовок
        self.setFrameStyle(BaseSectorWidget.NoFrame)
        self.setStyleSheet("background: transparent;")

        self.rem_card_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.icons_dir = os.path.join(self.rem_card_root, "icon")

        self.init_ui()

    def init_ui(self):
        # Общий контейнер
        self.main_container = QWidget()
        self.main_container.setObjectName("sector_3b_main_container")
        self.main_layout_v = QVBoxLayout(self.main_container)
        self.main_layout_v.setContentsMargins(5, 3, 0, 5)
        self.main_layout_v.setSpacing(0)
        
        # 1. Шапка "Выведено" (Стиль с серым фоном и верхними закруглениями)
        self.header_lbl = QLabel("Выведено")
        self.header_lbl.setObjectName("balance_header")
        self.header_lbl.setAlignment(Qt.AlignCenter)
        self.header_lbl.setFixedHeight(30)
        self.main_layout_v.addWidget(self.header_lbl)

        # 2. Область данных (Белый фон, боковые границы)
        self.data_area = QWidget()
        self.data_area.setObjectName("balance_data_area")
        self.data_layout = QVBoxLayout(self.data_area)
        self.data_layout.setContentsMargins(10, 10, 10, 10)
        self.data_layout.setSpacing(8)

        # Заголовок "Всего выведено" внутри области данных
        header_layout = QHBoxLayout()
        header_lbl = QLabel("Всего:")
        header_lbl.setStyleSheet("font-weight: bold; font-size: 14px; color: #495057; border: none; background: transparent;")
        self.total_out_val = QLabel("0 мл")
        self.total_out_val.setStyleSheet("font-weight: bold; color: #28a745; font-size: 14px; border: none; background: transparent;")
        header_layout.addWidget(header_lbl)
        header_layout.addStretch()
        header_layout.addWidget(self.total_out_val)
        self.data_layout.addLayout(header_layout)

        # Разделитель
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color: #e0e0e0; border: none; background-color: #e0e0e0; max-height: 1px;")
        self.data_layout.addWidget(line)

        # Поля данных (Редактируемые)
        self.diurez_val = self.add_balance_row("Диурез", "balans_diurez.png")
        self.drenazh_val = self.add_balance_row("Дренажи", "balans_drenazh.png")
        self.zond_val = self.add_balance_row("ЖКТ (зонд)", "balans_zond.png")
        self.rvota_val = self.add_balance_row("Рвота", "balans_rvota.png")
        self.other_val = self.add_balance_row("Другое", "drugoe.png")

        self.data_layout.addStretch()
        self.main_layout_v.addWidget(self.data_area)

        # Применяем QSS стили
        self.main_container.setStyleSheet("""
            QWidget#sector_3b_main_container {
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

    def add_balance_row(self, title, icon_name):
        row_layout = QHBoxLayout()
        
        label_container = QHBoxLayout()
        label_container.setSpacing(5)
        
        icon_lbl = QLabel()
        icon_lbl.setStyleSheet("border: none; background: transparent;")
        icon_path = os.path.join(self.icons_dir, icon_name)
        
        icon_h = 14
        if icon_name == "balans_drenazh.png":
            icon_h = 12

        if os.path.exists(icon_path):
            pixmap = QPixmap(icon_path)
            icon_lbl.setPixmap(pixmap.scaledToHeight(icon_h, Qt.SmoothTransformation))
        else:
            alt_path = os.path.join("icon", icon_name)
            if os.path.exists(alt_path):
                icon_lbl.setPixmap(QPixmap(alt_path).scaledToHeight(icon_h, Qt.SmoothTransformation))
            else:
                icon_lbl.setFixedSize(icon_h, icon_h)
            
        text_lbl = QLabel(title)
        text_lbl.setStyleSheet("font-size: 12px; color: #495057; border: none; background: transparent;")
        
        label_container.addWidget(icon_lbl)
        label_container.addWidget(text_lbl)
        
        val_lbl = QLabel("0")
        val_lbl.setStyleSheet("""
            QLabel {
                font-weight: 600; color: #495057; font-size: 12px; 
                border: none; background: transparent;
            }
        """)
        
        unit_lbl = QLabel("мл")
        unit_lbl.setStyleSheet("font-weight: 600; color: #495057; font-size: 12px; border: none; background: transparent;")
        
        row_layout.addLayout(label_container)
        row_layout.addStretch()
        row_layout.addWidget(val_lbl)
        row_layout.addWidget(unit_lbl)
        
        self.data_layout.addLayout(row_layout)
        return val_lbl

    def update_values(self, total, hour_data=None):
        """Обновление значений выведения."""
        self.total_out_val.setText(f"{int(total)} мл")
        
        if hour_data:
            self.diurez_val.setText(str(int(hour_data.get('urine', 0))))
            self.drenazh_val.setText(str(int(hour_data.get('drain_output', 0))))
            self.zond_val.setText(str(int(hour_data.get('ng_output', 0))))
            self.rvota_val.setText(str(int(hour_data.get('stool', 0))))
            self.other_val.setText(str(int(hour_data.get('other_output', 0))))

    def set_loading_state(self):
        self.total_out_val.setText("— мл")
        self.diurez_val.setText("—")
        self.drenazh_val.setText("—")
        self.zond_val.setText("—")
        self.rvota_val.setText("—")
        self.other_val.setText("—")
