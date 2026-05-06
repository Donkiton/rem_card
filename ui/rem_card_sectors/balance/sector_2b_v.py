import os
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QLineEdit, QGridLayout)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap, QIntValidator
from rem_card.ui.styles.theme import COLOR_DANGER, COLOR_PRIMARY

class Sector2b_v(QWidget):
    """Сектор баланса жидкости: Выведено и Итог (правая часть)"""
    out_values_changed = Signal(int) # Сигнал при изменении выведенного объема

    def __init__(self, parent=None):
        super().__init__(parent)
        # Определяем базовый путь к иконкам: rem_card/icon
        self.rem_card_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        self.icons_dir = os.path.join(self.rem_card_root, "icon")
        
        self.init_ui()

    def init_ui(self):
        # Общий контейнер
        self.main_container = QWidget()
        self.main_layout_v = QVBoxLayout(self.main_container)
        # Отступы: 3px сверху и снизу для унификации
        self.main_layout_v.setContentsMargins(2, 3, 0, 3) 
        self.main_layout_v.setSpacing(0)
        
        self.val_style = "font-weight: bold; color: #28a745; font-size: 14px; border: none; background: transparent;"
        self.label_style = "font-size: 13px; color: #495057; border: none; background: transparent;"

        # 1. Шапка "Выведено"
        self.header_lbl = QLabel("Выведение:")
        self.header_lbl.setObjectName("balance_header")
        self.header_lbl.setAlignment(Qt.AlignCenter)
        self.header_lbl.setFixedHeight(30)
        self.main_layout_v.addWidget(self.header_lbl)

        # 2. Область данных
        self.data_area = QWidget()
        self.data_area.setObjectName("balance_data_area")
        self.data_layout = QVBoxLayout(self.data_area)
        self.data_layout.setContentsMargins(10, 5, 10, 5)
        self.data_layout.setSpacing(5)
        
        # Заголовок с общим объемом
        header_layout = QHBoxLayout()
        header_lbl = QLabel("Всего выведено:")
        header_lbl.setStyleSheet("font-weight: bold; font-size: 14px; color: #495057; border: none; background: transparent;")
        self.total_out_val = QLabel("0 мл")
        self.total_out_val.setStyleSheet(self.val_style)
        header_layout.addWidget(header_lbl)
        header_layout.addStretch()
        header_layout.addWidget(self.total_out_val)
        self.data_layout.addLayout(header_layout)
        
        # Разделитель
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color: #e0e0e0; border: none; background-color: #e0e0e0; max-height: 1px;")
        self.data_layout.addWidget(line)
        
        # Сетка для полей данных (обеспечивает выравнивание)
        self.grid_layout = QGridLayout()
        self.grid_layout.setContentsMargins(0, 0, 0, 0)
        self.grid_layout.setHorizontalSpacing(8)
        self.grid_layout.setVerticalSpacing(6)
        
        # Поля данных
        self.diurez_val, self.diurez_x = self._add_grid_row(0, "Диурез", "balans_diurez.png")
        self.drenazh_val, self.drenazh_x = self._add_grid_row(1, "Дренажи", "balans_drenazh.png")
        self.zond_val, self.zond_x = self._add_grid_row(2, "ЖКТ (зонд)", "balans_zond.png")
        self.rvota_val, self.rvota_x = self._add_grid_row(3, "Рвота", "balans_rvota.png")
        self.other_val, self.other_x = self._add_grid_row(4, "Другое", "drugoe.png")
        
        # Колонка названий должна растягиваться
        self.grid_layout.setColumnStretch(1, 1)
        
        self.data_layout.addLayout(self.grid_layout)
        
        # Спейсер перед итогом
        self.data_layout.addStretch()
        
        # Разделитель перед балансом
        line2 = QFrame()
        line2.setFrameShape(QFrame.HLine)
        line2.setStyleSheet("color: #bdc3c7; border: none; background-color: #bdc3c7; max-height: 1px;")
        self.data_layout.addWidget(line2)
        
        # ИТОГОВЫЙ БАЛАНС
        balance_layout = QHBoxLayout()
        balance_lbl = QLabel("Баланс:")
        balance_lbl.setStyleSheet("font-weight: bold; font-size: 16px; color: #2c3e50; border: none; background: transparent;")
        self.balance_val = QLabel("0 мл")
        self.balance_val.setStyleSheet(f"font-weight: bold; font-size: 16px; color: {COLOR_PRIMARY}; border: none; background: transparent;")
        balance_layout.addWidget(balance_lbl)
        balance_layout.addStretch()
        balance_layout.addWidget(self.balance_val)
        self.data_layout.addLayout(balance_layout)

        self.main_layout_v.addWidget(self.data_area)

        # 3. Нижний закругляющийся угол
        self.bottom_corner = QWidget()
        self.bottom_corner.setObjectName("balance_footer")
        self.bottom_corner.setFixedHeight(15)
        self.main_layout_v.addWidget(self.bottom_corner)

        # Применяем QSS стили
        self.main_container.setStyleSheet("""
            QWidget#balance_header {
                font-weight: bold; font-size: 15px; color: #2c3e50 !important; 
                background-color: #e9ecef !important;
                border-top: 1.5px solid #bdc3c7 !important;
                border-left: 1.5px solid #bdc3c7 !important;
                border-right: 1.5px solid #bdc3c7 !important;
                border-bottom: 0.5px solid #bdc3c7 !important;
                border-top-left-radius: 5px !important; border-top-right-radius: 5px !important;
            }
            QWidget#balance_data_area {
                background-color: white !important;
                border-left: 1.5px solid #bdc3c7 !important;
                border-right: 1.5px solid #bdc3c7 !important;
                border-top: none !important; border-bottom: none !important;
            }
            QWidget#balance_footer {
                background-color: white !important;
                border-left: 1.5px solid #bdc3c7 !important;
                border-right: 1.5px solid #bdc3c7 !important;
                border-bottom: 1.5px solid #bdc3c7 !important;
                border-bottom-left-radius: 5px !important; border-bottom-right-radius: 5px !important;
                border-top: none !important;
            }
        """)

        # Заменяем основной layout виджета на наш контейнер
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(self.main_container)

    def _add_grid_row(self, row, title, icon_name):
        """Вспомогательный метод для добавления строки в сетку."""
        # 1. Иконка
        icon_lbl = QLabel()
        icon_lbl.setStyleSheet("border: none; background: transparent;")
        icon_path = os.path.join(self.icons_dir, icon_name)
        icon_h = 16
        if icon_name == "balans_drenazh.png": icon_h = 14
        
        if os.path.exists(icon_path):
            icon_lbl.setPixmap(QPixmap(icon_path).scaledToHeight(icon_h, Qt.SmoothTransformation))
        else:
            icon_lbl.setFixedSize(icon_h, icon_h)
            
        # 2. Название
        text_lbl = QLabel(title)
        text_lbl.setStyleSheet(self.label_style)
        
        # 3. Поле ввода
        val_input = QLineEdit()
        val_input.setPlaceholderText("0")
        val_input.setFixedWidth(45)
        val_input.setValidator(QIntValidator(0, 99999))
        val_input.setStyleSheet("""
            QLineEdit {
                font-weight: 600; color: #495057; font-size: 12px; 
                border: 1px solid #ced4da; border-radius: 3px; 
                padding: 1px 2px; background: white;
            }
            QLineEdit:focus { border: 1px solid #9aa3ab; }
        """)
        
        # 4. Разделитель
        slash_lbl = QLabel("/")
        slash_lbl.setStyleSheet("color: #adb5bd; font-weight: bold;")
        
        # 5. Значение X (из сетки)
        x_val_lbl = QLabel("0")
        x_val_lbl.setFixedWidth(40)
        x_val_lbl.setStyleSheet(f"font-weight: 600; color: {COLOR_PRIMARY}; font-size: 12px;")
        
        # 6. Единицы
        unit_lbl = QLabel("мл")
        unit_lbl.setStyleSheet(f"font-size: 11px; color: {COLOR_PRIMARY};")
        
        # Добавляем в QGridLayout
        self.grid_layout.addWidget(icon_lbl, row, 0, Qt.AlignCenter)
        self.grid_layout.addWidget(text_lbl, row, 1, Qt.AlignLeft | Qt.AlignVCenter)
        self.grid_layout.addWidget(val_input, row, 2, Qt.AlignRight | Qt.AlignVCenter)
        self.grid_layout.addWidget(slash_lbl, row, 3, Qt.AlignCenter)
        self.grid_layout.addWidget(x_val_lbl, row, 4, Qt.AlignLeft | Qt.AlignVCenter)
        self.grid_layout.addWidget(unit_lbl, row, 5, Qt.AlignLeft | Qt.AlignVCenter)
        
        return val_input, x_val_lbl

    def update_balance(self, total_in, total_out, total_in_daily=None, total_out_daily=None):
        balance_cur = int(round(total_in - total_out))
        if total_in_daily is None: total_in_daily = total_in
        if total_out_daily is None: total_out_daily = total_out
        balance_day = int(round(total_in_daily - total_out_daily))

        prefix_cur = "+" if balance_cur > 0 else ""
        color = COLOR_PRIMARY if balance_cur >= 0 else COLOR_DANGER
        
        self.balance_val.setText(f"{prefix_cur}{balance_cur}/{balance_day} мл")
        self.balance_val.setStyleSheet(f"font-weight: bold; font-size: 16px; color: {color};")
        self.total_out_val.setText(f"{int(total_out)} мл")

    def update_quick_values(self, hour_data: dict):
        """Обновляет значения 'X' в полях ввода."""
        # Мы обновляем только лейблы "X" (накопленный итог), 
        # сами QLineEdit (поля ввода) этот метод не трогает.
        self.diurez_x.setText(str(int(hour_data.get('urine', 0))))
        self.drenazh_x.setText(str(int(hour_data.get('drain_output', 0))))
        self.zond_x.setText(str(int(hour_data.get('ng_output', 0))))
        self.rvota_x.setText(str(int(hour_data.get('stool', 0))))
        self.other_x.setText(str(int(hour_data.get('other_output', 0))))

    def set_loading_state(self):
        self.balance_val.setText("—/— мл")
        self.balance_val.setStyleSheet(f"font-weight: bold; font-size: 16px; color: {COLOR_PRIMARY};")
        self.total_out_val.setText("— мл")
        self.diurez_x.setText("—")
        self.drenazh_x.setText("—")
        self.zond_x.setText("—")
        self.rvota_x.setText("—")
        self.other_x.setText("—")

    def set_quick_input_enabled(self, enabled: bool):
        """Блокирует или разблокирует поля быстрого ввода (для архивных карт)."""
        bg_color = "white" if enabled else "#f0f0f0"
        style = f"""
            QLineEdit {{
                font-weight: 600; color: #495057; font-size: 12px; 
                border: 1px solid #ced4da; border-radius: 3px; 
                padding: 1px 2px; background: {bg_color};
            }}
            QLineEdit:focus {{ border: 1px solid #9aa3ab; }}
        """
        for field in [self.diurez_val, self.drenazh_val, self.zond_val, self.rvota_val, self.other_val]:
            field.setReadOnly(not enabled)
            field.setStyleSheet(style)
