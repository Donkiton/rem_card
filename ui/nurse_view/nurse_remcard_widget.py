from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
                             QPushButton, QFrame, QScrollArea, QDateEdit)
from PySide6.QtCore import QDate, QTimer, Qt
from ..shared.vitals_widget import VitalsWidget
from ..shared.chart_widget import ChartWidget
from datetime import datetime

class NurseRemCardWidget(QWidget):
    def __init__(self, remcard_service, admission_id, parent=None):
        super().__init__(parent)
        self.service = remcard_service
        self.admission_id = admission_id
        self.current_date = datetime.now()
        self._last_status = None
        
        self.init_ui()
        self.refresh_data()
        
        # Поллинг статуса
        self.polling_timer = QTimer(self)
        self.polling_timer.timeout.connect(self.check_status)
        self.polling_timer.start(4000)

    def init_ui(self):
        main_layout = QVBoxLayout(self)
        
        # Header (Info & Navigation)
        header_layout = QHBoxLayout()
        self.btn_back = QPushButton("← Назад")
        header_layout.addWidget(self.btn_back)

        self.header = QLabel(f"Ремкарта пациента (ID: {self.admission_id})")
        self.header.setStyleSheet("font-size: 14px; font-weight: bold;")
        header_layout.addWidget(self.header)
        
        self.lbl_status = QLabel("-")
        self.lbl_status.setFixedWidth(210) # Увеличено на ~30%
        self.lbl_status.setAlignment(Qt.AlignCenter)
        self.lbl_status.setStyleSheet("font-weight: bold; color: white; background-color: #7f8c8d; border-radius: 4px; padding: 2px;")
        header_layout.addWidget(self.lbl_status)
        
        header_layout.addStretch()
        header_layout.addWidget(QLabel("Просмотр за дату:"))
        self.date_selector = QDateEdit(QDate.currentDate())
        self.date_selector.setCalendarPopup(True)
        self.date_selector.dateChanged.connect(self.change_view_date)
        header_layout.addWidget(self.date_selector)
        
        main_layout.addLayout(header_layout)
        
        # Content (Vitals | Chart)
        content_layout = QHBoxLayout()
        
        # Левая часть - ввод
        self.vitals_input = VitalsWidget(self.service, self.admission_id)
        self.vitals_input.save_btn.clicked.connect(self.refresh_data)
        
        content_layout.addWidget(self.vitals_input, 1)
        
        # Правая часть - график
        self.chart = ChartWidget()
        self.chart.status_service = self.service.status_service
        self.chart.admission_id = self.admission_id
        content_layout.addWidget(self.chart, 4)
        
        main_layout.addLayout(content_layout)

    def change_view_date(self, qdate):
        self.current_date = datetime(qdate.year(), qdate.month(), qdate.day(), 12, 0)
        self.refresh_data()

    def check_status(self):
        if not self.admission_id or not self.service.status_service: return
        current_ev = self.service.status_service.get_current_status(self.admission_id)
        if not current_ev: return
        
        if self._last_status and self._last_status != current_ev.status:
            self.refresh_data()
            
        self._last_status = current_ev.status
        self.update_status_ui(current_ev)

    def update_status_ui(self, ev):
        from ...data.dto.remcard_dto import PatientStatus
        status_map = {
            PatientStatus.ACTIVE: ("🟢 В отделении", "#2ecc71"),
            PatientStatus.OUT: ("🟡 Вне отд.", "#f39c12"),
            PatientStatus.OR: ("🔴 Операционная", "#e74c3c"),
            PatientStatus.TRANSFERRED: ("⚫ Переведен", "#968c8c"),
            PatientStatus.DEAD: ("⚫ Умер", "#968c8c")
        }
        text, color = status_map.get(ev.status, (ev.status.value, "#7f8c8d"))
        self.lbl_status.setText(f"{text} ({ev.start_time.strftime('%H:%M')})")
        self.lbl_status.setStyleSheet(f"font-weight: bold; color: white; background-color: {color}; border-radius: 4px; padding: 2px;")
        
        is_active = (ev.status == PatientStatus.ACTIVE)
        self.vitals_input.setEnabled(is_active)

    def refresh_data(self):
        start, _ = self.service.get_day_period(self.current_date)
        # Для медсестры тоже используем расширенные виталы, чтобы график соединялся через сутки
        vitals_extended = self.service.get_vitals_extended(self.admission_id, self.current_date)
        if hasattr(self, 'chart'):
            self.chart.admission_id = self.admission_id
            self.chart.update_data(vitals_extended, start)
        self.check_status()
