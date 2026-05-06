from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame)
from PySide6.QtCore import Qt
from rem_card.ui.shared.base_sector import BaseSectorWidget
from rem_card.ui.styles.theme import COLOR_DANGER, COLOR_PRIMARY

class Sector4a(BaseSectorWidget):
    def __init__(self, parent=None):
        super().__init__("4а", parent)
        self.label.hide() # Скрываем стандартный заголовок
        self.setFrameStyle(BaseSectorWidget.NoFrame)
        self.setStyleSheet("background: transparent;")

        self.init_ui()

    def init_ui(self):
        # Общий контейнер
        self.main_container = QWidget()
        self.main_container.setObjectName("sector_4a_main_container")
        self.main_layout_v = QVBoxLayout(self.main_container)
        self.main_layout_v.setContentsMargins(5, 3, 3, 5) 
        self.main_layout_v.setSpacing(0)
        
        # Область данных (Единая замкнутая рамка)
        self.data_area = QWidget()
        self.data_area.setObjectName("balance_data_area")
        self.data_layout = QVBoxLayout(self.data_area)
        self.data_layout.setContentsMargins(15, 10, 15, 10)
        self.data_layout.setSpacing(8)

        # ИТОГОВЫЙ БАЛАНС
        balance_layout = QHBoxLayout()
        balance_layout.setSpacing(0)
        
        balance_lbl = QLabel("Баланс:")
        balance_lbl.setStyleSheet("font-weight: bold; font-size: 14px; color: #2c3e50; border: none; background: transparent;")
        
        self.balance_val = QLabel("0 мл")
        self.balance_val.setStyleSheet(f"font-weight: bold; font-size: 14px; color: {COLOR_PRIMARY}; border: none; background: transparent;")
        
        balance_layout.addWidget(balance_lbl)
        balance_layout.addWidget(self.balance_val)
        balance_layout.addStretch()
        
        self.data_layout.addLayout(balance_layout)
        self.main_layout_v.addWidget(self.data_area)

        # Применяем QSS стили для полностью замкнутой рамки с закруглениями со всех сторон
        self.main_container.setStyleSheet("""
            QWidget#sector_4a_main_container {
                background-color: #f8f9fa !important;
            }
            QWidget#balance_data_area {
                background-color: #f8f9fa !important;
                border: 1.5px solid #bdc3c7 !important;
                border-radius: 5px !important;
            }
        """)

        self.set_content(self.main_container)

    def update_balance(self, total_in, total_out, total_in_daily=None, total_out_daily=None):
        """Обновление баланса в формате 'На текущий час / На конец суток'."""
        balance_cur = int(round(total_in - total_out))
        
        # Если суточные данные не переданы, используем текущие
        if total_in_daily is None: total_in_daily = total_in
        if total_out_daily is None: total_out_daily = total_out
        
        balance_day = int(round(total_in_daily - total_out_daily))
        
        prefix_cur = "+" if balance_cur > 0 else ""
        prefix_day = "+" if balance_day > 0 else ""
        
        color_cur = COLOR_PRIMARY if balance_cur >= 0 else COLOR_DANGER
        # Для суточного баланса тоже можно менять цвет, но обычно ориентируемся на текущий
        
        # Убран пробел после : (в setText) и шрифт уменьшен до 14px
        self.balance_val.setText(f"{prefix_cur}{balance_cur}/{prefix_day}{balance_day} мл")
        self.balance_val.setStyleSheet(f"font-weight: bold; font-size: 14px; color: {color_cur}; border: none; background: transparent;")

    def set_loading_state(self):
        self.balance_val.setText("—/— мл")
        self.balance_val.setStyleSheet(f"font-weight: bold; font-size: 14px; color: {COLOR_PRIMARY}; border: none; background: transparent;")
