from rem_card.ui.shared.base_sector import BaseSectorWidget
from PySide6.QtWidgets import QVBoxLayout, QLabel, QWidget, QSizePolicy
from PySide6.QtCore import Qt
from rem_card.ui.styles.theme import (BG_MAIN, BG_LIGHT, TEXT_PRIMARY, BORDER_COLOR, 
                                     COLOR_VITAL_AD_BG, COLOR_VITAL_AD_LINE, 
                                     COLOR_VITAL_PULSE_BG, COLOR_VITAL_PULSE,
                                     COLOR_VITAL_RESP_BG, COLOR_VITAL_RESP,
                                     COLOR_VITAL_SPO2_BG, COLOR_VITAL_SPO2,
                                     COLOR_VITAL_TEMP_BG, COLOR_VITAL_TEMP,
                                     COLOR_VITAL_CVP_BG, COLOR_VITAL_CVP)

class Sector2g(BaseSectorWidget):
    def __init__(self, parent=None):
        super().__init__("2г", parent)
        self.label.hide()
        self.setFrameStyle(BaseSectorWidget.NoFrame)
        
        # Общий контейнер
        self.main_container = QWidget()
        self.main_container.setObjectName("sector_2g_main_container")
        self.main_layout_v = QVBoxLayout(self.main_container)
        # Сдвигаем рамку (слой 2) сверху и снизу на 3 пикселя
        self.main_layout_v.setContentsMargins(0, 3, 0, 5) 
        self.main_layout_v.setSpacing(0)
        
        # 1. Шапка "Показатели" (Закругление только СЛЕВА СВЕРХУ)
        self.header_lbl = QLabel("Показатели")
        self.header_lbl.setObjectName("vitals_header")
        self.header_lbl.setAlignment(Qt.AlignCenter)
        self.header_lbl.setFixedHeight(30)
        
        # 2. Область легенды (Вертикальная линия слева)
        self.legend_area = QWidget()
        self.legend_area.setObjectName("vitals_legend")
        self.legend_layout = QVBoxLayout(self.legend_area)
        self.legend_layout.setContentsMargins(0, 20, 0, 20)
        self.legend_layout.setSpacing(15)
        
        self.legend_items = {}
        
        items_data = [
            ("ad", "АД (мм.рт.ст)", COLOR_VITAL_AD_BG, COLOR_VITAL_AD_LINE),
            ("pulse", "ЧСС (уд/мин)", COLOR_VITAL_PULSE_BG, COLOR_VITAL_PULSE),
            ("temp", "Температура (°C)", COLOR_VITAL_TEMP_BG, COLOR_VITAL_TEMP),
            ("rr", "ЧДД (в мин)", COLOR_VITAL_RESP_BG, COLOR_VITAL_RESP),
            ("spo2", "SpO2 (%)", COLOR_VITAL_SPO2_BG, COLOR_VITAL_SPO2),
            ("cvp", "ЦВД (см.вод.ст)", COLOR_VITAL_CVP_BG, COLOR_VITAL_CVP)
        ]
        
        for key, text, bg, line in items_data:
            lbl = self.create_legend_label(text, bg, line)
            lbl.setFixedWidth(137)
            self.legend_layout.addWidget(lbl)
            self.legend_items[key] = lbl
            
        self.legend_layout.addStretch()
        
        # 3. Нижний закругляющийся угол (переход к 2в)
        self.bottom_corner = QWidget()
        self.bottom_corner.setObjectName("vitals_footer")
        self.bottom_corner.setFixedHeight(15)
        
        self.main_layout_v.addWidget(self.header_lbl)
        self.main_layout_v.addWidget(self.legend_area)
        self.main_layout_v.addWidget(self.bottom_corner)
        
        self.main_container.setStyleSheet(f"""
            QWidget#sector_2g_main_container {{
                background-color: {BG_MAIN} !important;
            }}
            QWidget#vitals_header {{
                font-weight: bold; 
                font-size: 14px; 
                color: {TEXT_PRIMARY} !important; 
                background-color: {BG_LIGHT} !important;
                border-top: 1.5px solid {BORDER_COLOR} !important;
                border-left: 1.5px solid {BORDER_COLOR} !important;
                border-right: none !important;
                border-bottom: 0.5px solid {BORDER_COLOR} !important;
                border-top-left-radius: 5px !important;
                margin-left: 0px !important;
            }}
            QWidget#vitals_legend {{
                background-color: {BG_MAIN} !important;
                border-left: 1.5px solid {BORDER_COLOR} !important;
                border-right: none !important;
                border-top: none !important;
                border-bottom: none !important;
                margin-left: 0px !important;
            }}
            QWidget#vitals_footer {{
                background-color: {BG_MAIN} !important;
                border-left: 1.5px solid {BORDER_COLOR} !important;
                border-right: none !important;
                border-top: none !important;
                border-bottom: 1.5px solid {BORDER_COLOR} !important;
                border-bottom-left-radius: 5px !important;
                border-bottom-right-radius: 0px !important;
                margin-left: 0px !important;
            }}
        """)
        
        self.set_content(self.main_container)

    def create_legend_label(self, text, bg_color, line_color):
        item = QLabel(text)
        item.setMinimumHeight(35)
        item.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        item.setContentsMargins(10, 0, 5, 0) # Уменьшен правый контентный отступ
        
        # Левый край ПРЯМОЙ (примыкает к линии), правый ЗАКРУГЛЕННЫЙ
        # Сдвиг на 0px для совпадения с рамкой контейнера
        style = f"""
            QLabel {{
                background-color: {bg_color} !important;
                color: #2c3e50 !important;
                font-weight: 600 !important;
                font-size: 12px !important;
                border-top-right-radius: 10px !important;
                border-bottom-right-radius: 10px !important;
                border-left: 6px solid {line_color} !important;
                margin-left: 0px !important;
                padding-right: 5px !important;
                border-top: none !important;
                border-right: none !important;
                border-bottom: none !important;
            }}
        """
        item.setStyleSheet(style)
        return item

    def update_legend(self, settings: dict):
        if not settings:
            settings = {'ad': 1, 'pulse': 1, 'temp': 1, 'spo2': 1, 'rr': 0, 'cvp': 0}
        
        for key, lbl in self.legend_items.items():
            is_visible = bool(settings.get(key, 0))
            lbl.setVisible(is_visible)
