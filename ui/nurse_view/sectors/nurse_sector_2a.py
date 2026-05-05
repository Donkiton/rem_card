from rem_card.ui.shared.base_sector import BaseSectorWidget
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget
from PySide6.QtCore import Qt, QTimer
from datetime import datetime, timedelta

class NurseSector2a(BaseSectorWidget):
    """Шапка карты для медсестры (без кнопок управления)."""
    def __init__(self, parent=None):
        super().__init__("2а", parent)
        self.label.hide()
        self.setObjectName("nurse_sector_2a_frame")

        self.setStyleSheet("""
            QFrame#nurse_sector_2a_frame {
                background: #e9ecef;
                border-top: 1.5px solid #bdc3c7;
                border-left: 1.5px solid #bdc3c7;
                border-right: 1.5px solid #bdc3c7;
                border-bottom: 0.5px solid #bdc3c7;
                border-top-left-radius: 5px;
                border-top-right-radius: 5px;
                border-bottom-left-radius: 0px;
                border-bottom-right-radius: 0px;
            }
            QWidget#nurse_sector_2a_info_widget {
                background: transparent;
                border-bottom: 0.5px solid #bdc3c7;
            }
        """)
        
        self.info_widget = QWidget()
        self.info_widget.setObjectName("nurse_sector_2a_info_widget")
        self.info_layout = QHBoxLayout(self.info_widget)
        self.info_layout.setContentsMargins(10, 0, 10, 0)
        
        self.lbl_title = QLabel("Реанимационная карта (Медсестра)")
        self.lbl_title.setStyleSheet("font-weight: bold; font-size: 16px; color: #5c6770; background: transparent;")
        
        self.lbl_period = QLabel("-")
        self.lbl_period.setStyleSheet("font-size: 14px; margin-left: 20px; background: transparent; color: #495057;")
        
        self.lbl_current_time = QLabel()
        self.lbl_current_time.setStyleSheet("font-weight: bold; font-size: 14px; color: #d35400; margin-left: 15px; background: transparent;")
        
        self.info_layout.addWidget(self.lbl_title)
        self.info_layout.addWidget(self.lbl_period)
        self.info_layout.addWidget(self.lbl_current_time)
        self.info_layout.addStretch()
        
        self.set_content(self.info_widget)
        
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_current_time)
        self.timer.start(1000)
        self.update_current_time()

    def update_period(self, start_dt):
        end_dt = start_dt + timedelta(hours=24)
        fmt_date = "%d.%m.%Y"
        fmt_time = "%H:%M"
        text = f"{start_dt.strftime(fmt_date)} ({start_dt.strftime(fmt_time)} - {end_dt.strftime(fmt_date)} {end_dt.strftime(fmt_time)})"
        self.lbl_period.setText(text)

    def update_current_time(self):
        self.lbl_current_time.setText(f"Текущее время: {datetime.now().strftime('%H:%M')}")
