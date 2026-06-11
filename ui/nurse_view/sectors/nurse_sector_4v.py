import os
from rem_card.ui.shared.base_sector import BaseSectorWidget
from PySide6.QtWidgets import (QHBoxLayout, QVBoxLayout, QLabel, QWidget, QPushButton, QFrame)
from PySide6.QtGui import QIcon
from PySide6.QtCore import Qt, QSize, Signal

class VitalBadge(QFrame):
    """Виджет плашки для вывода витального показателя."""
    def __init__(self, label_text, value_text, bg_color, border_color, parent=None):
        super().__init__(parent)
        self.setObjectName("vital_badge_frame")
        self.bg_color = bg_color
        self.border_color = border_color
        self.setFixedHeight(32)
        
        self.text_color = "#2c3e50"
        
        self.layout_inner = QHBoxLayout(self)
        self.layout_inner.setContentsMargins(5, 0, 5, 0)
        self.layout_inner.setSpacing(4)
        self.layout_inner.setAlignment(Qt.AlignCenter)
        
        self.lbl_tag = QLabel(label_text)
        self.lbl_val = QLabel(value_text)
        
        self.layout_inner.addWidget(self.lbl_tag)
        self.layout_inner.addWidget(self.lbl_val)
        
        self.update_style(13, 115)

    def set_value(self, text):
        self.lbl_val.setText(text)

    def update_style(self, font_size: int, width: int):
        self.setFixedWidth(width)
        self.setStyleSheet(f"""
            QFrame#vital_badge_frame {{
                background-color: {self.bg_color};
                border-radius: 7px;
                border: 1px solid {self.border_color};
            }}
            QLabel {{
                color: {self.text_color};
                background: transparent;
                font-weight: bold;
                font-size: {font_size}px;
                border: none;
            }}
        """)

class NurseSector4v(BaseSectorWidget):
    """Нижняя часть объединенного блока для медсестры (4в) - БЕЗ кнопок создания."""
    show_card_requested = Signal()
    archive_requested = Signal()
    yest_card_requested = Signal()
    full_report_requested = Signal()
    daily_report_requested = Signal()
    recovery_transfer_requested = Signal()
    recovery_cancel_transfer_requested = Signal()

    def __init__(self, parent=None):
        super().__init__("4в", parent)
        self.label.hide()
        self.setFrameStyle(BaseSectorWidget.NoFrame)
        self.setStyleSheet("background: transparent;")
        
        self.icon_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "icon")
        self.icon_dir = os.path.normpath(self.icon_dir)
        
        self.init_ui()

    def init_ui(self):
        self.main_container = QWidget()
        self.main_container.setObjectName("sector_4v_main_container")
        self.main_layout_v = QVBoxLayout(self.main_container)
        self.main_layout_v.setContentsMargins(0, 0, 0, 5)
        self.main_layout_v.setSpacing(0)
        
        self.content_area = QWidget()
        self.content_area.setObjectName("sector_content_area")
        self.content_layout_wrapper = QVBoxLayout(self.content_area)
        self.content_layout_wrapper.setContentsMargins(0, 0, 0, 0)
        self.content_layout_wrapper.setSpacing(0)

        # Внутренний виджет для скролла
        self.inner_content = QWidget()
        self.inner_content.setStyleSheet("background: transparent;")
        self.content_layout = QHBoxLayout(self.inner_content)
        self.content_layout.setContentsMargins(7, 0, 10, 0)
        self.content_layout.setSpacing(10)
        
        button_style = """
            QPushButton {
                font-size: 13px; 
                font-weight: bold; 
                padding: 4px 12px; 
                background-color: #ecf0f1; 
                color: #2c3e50; 
                border-radius: 5px; 
                border: 1.5px solid #bdc3c7;
            }
            QPushButton:hover {
                background-color: #dcdde1;
            }
            QPushButton:pressed {
                background-color: #bdc3c7;
            }
            QPushButton:disabled {
                background-color: #f1f2f6;
                color: #a4b0be;
                border: 1px solid #dcdde1;
            }
        """

        # Кнопка "Вчерашняя карта"
        self.btn_yest_card = QPushButton(" Вчерашняя карта")
        yest_icon_path = os.path.join(self.icon_dir, "yestcard.png")
        self.btn_yest_card.setIcon(QIcon(yest_icon_path))
        self.btn_yest_card.setIconSize(QSize(20, 20))
        self.btn_yest_card.setMinimumHeight(32)
        self.btn_yest_card.setStyleSheet(button_style)
        self.btn_yest_card.clicked.connect(self.yest_card_requested.emit)

        # Кнопка "Показать карту"
        self.btn_show_card = QPushButton(" Показать карту")
        show_icon_path = os.path.join(self.icon_dir, "loadcard.png")
        self.btn_show_card.setIcon(QIcon(show_icon_path))
        self.btn_show_card.setIconSize(QSize(20, 20))
        self.btn_show_card.setMinimumHeight(32)
        self.btn_show_card.setStyleSheet(button_style)
        self.btn_show_card.clicked.connect(self.show_card_requested.emit)

        # Кнопка "Список карт"
        self.btn_card_list = QPushButton(" Список карт")
        icon_path = os.path.join(self.icon_dir, "medical-chart.png")
        self.btn_card_list.setIcon(QIcon(icon_path))
        self.btn_card_list.setIconSize(QSize(20, 20))
        self.btn_card_list.setMinimumHeight(32)
        self.btn_card_list.setStyleSheet(button_style)
        self.btn_card_list.clicked.connect(self.archive_requested.emit)
        
        # Кнопка "Отчет за сутки"
        self.btn_daily_print = QPushButton(" Отчет за сутки")
        icon_daily_path = os.path.join(self.icon_dir, "sutki_print.png")
        self.btn_daily_print.setIcon(QIcon(icon_daily_path))
        self.btn_daily_print.setIconSize(QSize(20, 20))
        self.btn_daily_print.setMinimumHeight(32)
        self.btn_daily_print.setStyleSheet(button_style)
        self.btn_daily_print.clicked.connect(self.daily_report_requested.emit)

        # Кнопка "Общий отчет"
        self.btn_all_print = QPushButton(" Общий отчет")
        icon_all_path = os.path.join(self.icon_dir, "allprint.png")
        self.btn_all_print.setIcon(QIcon(icon_all_path))
        self.btn_all_print.setIconSize(QSize(20, 20))
        self.btn_all_print.setMinimumHeight(32)
        self.btn_all_print.setStyleSheet(button_style)
        self.btn_all_print.clicked.connect(self.full_report_requested.emit)

        self.btn_recovery_transfer = QPushButton(" Перевод")
        self.btn_recovery_transfer.setMinimumHeight(32)
        self.btn_recovery_transfer.setStyleSheet(button_style)
        self.btn_recovery_transfer.clicked.connect(self.recovery_transfer_requested.emit)
        self.btn_recovery_transfer.setVisible(False)

        self.btn_recovery_cancel_transfer = QPushButton(" Отменить перевод")
        cancel_icon_path = os.path.join(self.icon_dir, "icon-cancelled.png")
        if os.path.exists(cancel_icon_path):
            self.btn_recovery_cancel_transfer.setIcon(QIcon(cancel_icon_path))
            self.btn_recovery_cancel_transfer.setIconSize(QSize(20, 20))
        self.btn_recovery_cancel_transfer.setMinimumHeight(32)
        self.btn_recovery_cancel_transfer.setStyleSheet(button_style)
        self.btn_recovery_cancel_transfer.clicked.connect(self.recovery_cancel_transfer_requested.emit)
        self.btn_recovery_cancel_transfer.setVisible(False)
        
        self.content_layout.addWidget(self.btn_show_card)
        self.content_layout.addWidget(self.btn_yest_card)
        self.content_layout.addWidget(self.btn_card_list)
        self.content_layout.addWidget(self.btn_daily_print)
        self.content_layout.addWidget(self.btn_all_print)
        self.content_layout.addWidget(self.btn_recovery_transfer)
        self.content_layout.addWidget(self.btn_recovery_cancel_transfer)
        self.content_layout.addStretch()

        from rem_card.ui.styles.theme import (COLOR_VITAL_AD_LINE, COLOR_VITAL_PULSE, 
                                            COLOR_VITAL_SPO2, COLOR_VITAL_TEMP,
                                            COLOR_VITAL_RESP_BG, COLOR_VITAL_RESP,
                                            COLOR_VITAL_CVP_BG, COLOR_VITAL_CVP)
        
        self.badge_ad = VitalBadge("АД:", "-/-", "#ffdada", COLOR_VITAL_AD_LINE)
        self.badge_pulse = VitalBadge("ЧСС:", "-/мин.", "#dadaff", COLOR_VITAL_PULSE)
        self.badge_temp = VitalBadge("T:", "-\u00B0C", "#dafada", COLOR_VITAL_TEMP)
        self.badge_rr = VitalBadge("ЧДД:", "-", COLOR_VITAL_RESP_BG, COLOR_VITAL_RESP)
        self.badge_spo2 = VitalBadge("SpO\u2082:", "-%", "#e1f5fe", COLOR_VITAL_SPO2)
        self.badge_cvp = VitalBadge("ЦВД:", "-", COLOR_VITAL_CVP_BG, COLOR_VITAL_CVP)

        self.badges_container = QWidget()
        self.badges_layout = QHBoxLayout(self.badges_container)
        self.badges_layout.setContentsMargins(0, 0, 0, 0)
        self.badges_layout.setSpacing(5)
        
        self.badges = {
            'temp': self.badge_temp,
            'ad': self.badge_ad,
            'pulse': self.badge_pulse,
            'rr': self.badge_rr,
            'spo2': self.badge_spo2,
            'cvp': self.badge_cvp
        }
        
        for b in self.badges.values():
            self.badges_layout.addWidget(b)
            
        self.content_layout.addWidget(self.badges_container)
        
        from PySide6.QtWidgets import QScrollArea
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.NoFrame)
        self.scroll_area.setStyleSheet("background: transparent; border: none;")
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Hide the horizontal bar during startup layout recalculation in W1 rows.
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        
        # Чтобы scroll area не диктовала окну огромную ширину
        self.scroll_area.setMinimumWidth(100)
        
        self.scroll_area.setWidget(self.inner_content)
        self.content_layout_wrapper.addWidget(self.scroll_area)
        
        self.main_layout_v.addWidget(self.content_area)

        self.main_container.setStyleSheet("""
            QWidget#sector_4v_main_container {
                background-color: #f8f9fa !important;
            }
            QWidget#sector_content_area {
                background-color: #f8f9fa !important;
                border-left: 1.5px solid #bdc3c7 !important;
                border-right: 1.5px solid #bdc3c7 !important;
                border-bottom: 1.5px solid #bdc3c7 !important;
                border-bottom-left-radius: 5px !important;
                border-bottom-right-radius: 5px !important;
                border-top: none !important;
            }
        """)

        super().set_content(self.main_container)

    def set_content(self, widget):
        for i in reversed(range(self.content_layout.count())): 
            item = self.content_layout.itemAt(i)
            if item.widget(): item.widget().setParent(None)
        if widget: self.content_layout.addWidget(widget)

    def set_buttons_state(self, card_exists: bool, yest_card_exists: bool = True):
        self.btn_show_card.setEnabled(card_exists)
        self.btn_yest_card.setEnabled(yest_card_exists)

    def set_recovery_mode(self, enabled: bool, *, can_transfer: bool = True, can_cancel_transfer: bool = False):
        standard_buttons = (
            self.btn_show_card,
            self.btn_yest_card,
            self.btn_card_list,
            self.btn_daily_print,
            self.btn_all_print,
        )
        for button in standard_buttons:
            button.setVisible(not enabled)
        self.btn_recovery_transfer.setVisible(enabled)
        self.btn_recovery_transfer.setEnabled(bool(enabled and can_transfer))
        self.btn_recovery_cancel_transfer.setVisible(bool(enabled and can_cancel_transfer))
        self.btn_recovery_cancel_transfer.setEnabled(bool(enabled and can_cancel_transfer))

    def update_latest_vitals(self, latest_values, settings=None):
        if settings is None:
            settings = {'ad': 1, 'pulse': 1, 'temp': 1, 'spo2': 1, 'rr': 0, 'cvp': 0}
            
        if latest_values is None:
            latest_values = {}

        visible_count = sum(1 for k in self.badges.keys() if settings.get(k, 0))
        visible_count = max(1, visible_count)
        
        spacing = 5 * (visible_count - 1)
        w = max(65, int((480 - spacing) / visible_count))
        f_size = 13 if visible_count <= 4 else (12 if visible_count == 5 else 11)
        
        visual_profile = (
            tuple(1 if settings.get(k, 0) else 0 for k in self.badges.keys()),
            f_size,
            w,
            1 if visible_count > 4 else 0,
        )
        if getattr(self, "_last_visual_profile", None) != visual_profile:
            for k, b in self.badges.items():
                b.setVisible(bool(settings.get(k, 0)))
                b.update_style(f_size, w)
                if visible_count > 4:
                    b.layout_inner.setContentsMargins(2, 0, 2, 0)
                    b.layout_inner.setSpacing(2)
                else:
                    b.layout_inner.setContentsMargins(5, 0, 5, 0)
                    b.layout_inner.setSpacing(4)
            self._last_visual_profile = visual_profile

        # АД
        v_sys = latest_values.get('sys')
        v_dia = latest_values.get('dia')
        sys_str = f"{int(v_sys)}" if v_sys is not None else "-"
        dia_str = f"{int(v_dia)}" if v_dia is not None else "-"
        self.badge_ad.set_value(f"{sys_str}/{dia_str}")

        # Пульс
        v_pulse = latest_values.get('pulse')
        self.badge_pulse.set_value(f"{int(v_pulse)}" if v_pulse is not None else "-")

        # SpO2
        v_spo2 = latest_values.get('spo2')
        self.badge_spo2.set_value(f"{int(v_spo2)}%" if v_spo2 is not None else "-%")

        # Температура
        v_temp = latest_values.get('temp')
        self.badge_temp.set_value(f"{float(v_temp):.1f}°C" if v_temp is not None else "-°C")

        # ЧДД
        v_rr = latest_values.get('rr')
        self.badge_rr.set_value(f"{int(v_rr)}" if v_rr is not None else "-")

        # ЦВД
        v_cvp = latest_values.get('cvp')
        cvp_str = "-"
        if v_cvp is not None:
            cvp_str = "Н/Н" if v_cvp == -1 else f"{int(v_cvp)}"
        self.badge_cvp.set_value(cvp_str)
