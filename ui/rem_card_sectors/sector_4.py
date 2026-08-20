from rem_card.services.shift_service import ShiftService
from rem_card.ui.patient_bed_management.bed_labels import is_recovery_bed
from rem_card.ui.shared.base_sector import BaseSectorWidget
from PySide6.QtWidgets import (QHBoxLayout, QVBoxLayout, QLabel, QWidget)
from PySide6.QtCore import Qt
from rem_card.ui.styles.theme import COLOR_PRIMARY_DARK

class Sector4(BaseSectorWidget):
    def __init__(self, parent=None):
        super().__init__("4", parent)
        self.label.hide()
        self.setFrameStyle(BaseSectorWidget.NoFrame)
        self.setStyleSheet("background: transparent;")
        self._recovery_mode = False
        
        self.init_ui()

    def init_ui(self):
        # Общий контейнер (стиль как в 1б)
        self.main_container = QWidget()
        self.main_container.setObjectName("sector_4_main_container")
        self.main_layout_v = QVBoxLayout(self.main_container)
        self.main_layout_v.setContentsMargins(5, 5, 5, 5)
        self.main_layout_v.setSpacing(0)
        
        # 1. Шапка
        self.header_lbl = QLabel("Информация")
        self.header_lbl.setObjectName("sector_header")
        self.header_lbl.setAlignment(Qt.AlignCenter)
        self.header_lbl.setFixedHeight(28)
        self.main_layout_v.addWidget(self.header_lbl)

        # 2. Область контента
        self.content_area = QWidget()
        self.content_area.setObjectName("sector_content_area")
        self.content_layout = QVBoxLayout(self.content_area)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(0)
        self.main_layout_v.addWidget(self.content_area)

        # 3. Футер
        self.bottom_footer = QWidget()
        self.bottom_footer.setObjectName("sector_footer")
        self.bottom_footer.setFixedHeight(15)
        self.main_layout_v.addWidget(self.bottom_footer)

        # Стили
        self.main_container.setStyleSheet("""
            QWidget#sector_4_main_container {
                background-color: #f8f9fa !important;
            }
            QWidget#sector_header {
                font-weight: bold; 
                font-size: 14px; 
                color: #2c3e50 !important; 
                background-color: #e9ecef !important;
                border-top: 1.5px solid #bdc3c7 !important;
                border-left: 1.5px solid #bdc3c7 !important;
                border-right: 1.5px solid #bdc3c7 !important;
                border-bottom: 0.5px solid #bdc3c7 !important;
                border-top-left-radius: 5px !important;
                border-top-right-radius: 5px !important;
            }
            QWidget#sector_content_area {
                background-color: #f8f9fa !important;
                border-left: 1.5px solid #bdc3c7 !important;
                border-right: 1.5px solid #bdc3c7 !important;
                border-top: none !important;
                border-bottom: none !important;
            }
            QWidget#sector_footer {
                background-color: #f8f9fa !important;
                border-left: 1.5px solid #bdc3c7 !important;
                border-right: 1.5px solid #bdc3c7 !important;
                border-bottom: 1.5px solid #bdc3c7 !important;
                border-bottom-left-radius: 5px !important;
                border-bottom-right-radius: 5px !important;
                border-top: none !important;
            }
        """)

        # Создаем оригинальный контент
        self.info_widget = QWidget()
        self.info_layout = QHBoxLayout(self.info_widget)
        self.info_layout.setContentsMargins(10, 5, 10, 5)
        self.info_layout.setSpacing(20)
        
        self.lbl_history = QLabel("№ -")
        self.lbl_history.setStyleSheet(f"font-weight: bold; background: transparent; color: {COLOR_PRIMARY_DARK};")
        
        self.lbl_name = QLabel("-")
        self.lbl_name.setStyleSheet("font-weight: bold; font-size: 14px; background: transparent;")
        
        self.lbl_age = QLabel("Возраст: -")
        self.lbl_age.setStyleSheet("background: transparent;")
        
        self.lbl_days = QLabel("Сутки: -")
        self.lbl_days.setStyleSheet("background: transparent;")
        
        self.lbl_diagnosis = QLabel("Диагноз: -")
        self.lbl_diagnosis.setStyleSheet("background: transparent;")
        self.lbl_diagnosis.setWordWrap(True)
        
        self.info_layout.addWidget(self.lbl_history)
        self.info_layout.addWidget(self.lbl_name)
        self.info_layout.addWidget(self.lbl_age)
        self.info_layout.addWidget(self.lbl_days)
        self.info_layout.addWidget(self.lbl_diagnosis, 1)
        
        self.content_layout.addWidget(self.info_widget)
        super().set_content(self.main_container)

    def set_content(self, widget):
        for i in reversed(range(self.content_layout.count())): 
            item = self.content_layout.itemAt(i)
            if item.widget(): item.widget().setParent(None)
        if widget: self.content_layout.addWidget(widget)

    def set_recovery_mode(self, enabled: bool):
        self._recovery_mode = bool(enabled)

    def update_patient_info(self, patient, current_date, *, is_recovery: bool | None = None):
        if not patient:
            return
        recovery_mode = self._resolve_recovery_mode(patient, is_recovery)
        self._recovery_mode = recovery_mode
            
        self.lbl_history.setText(f"№ {patient.history_number}")
        self.lbl_name.setText(patient.get_display_name())
        
        age_str = patient.get_display_age(current_date) or "-"
        self.lbl_age.setText(f"Возраст: {age_str}")
        
        if patient.admission_datetime:
            if recovery_mode:
                value = self._format_department_time(patient.admission_datetime, current_date)
                self.lbl_days.setText(f"Время в отделении: {value}")
            else:
                value = self._format_icu_day(patient.admission_datetime, current_date)
                self.lbl_days.setText(f"Сутки: {value}")
        else:
            self.lbl_days.setText("Время в отделении: -" if recovery_mode else "Сутки: -")
            
        diag = patient.diagnosis_text if patient.diagnosis_text else "-"
        self.lbl_diagnosis.setText(f"Диагноз: {diag}")

    def _resolve_recovery_mode(self, patient, explicit_value) -> bool:
        if explicit_value is not None:
            return bool(explicit_value)
        bed_number = getattr(patient, "bed_number", None)
        try:
            return is_recovery_bed(bed_number)
        except Exception:
            return bool(getattr(self, "_recovery_mode", False))

    @staticmethod
    def _format_icu_day(admission_datetime, current_date) -> str:
        icu_day = ShiftService.calculate_icu_day(admission_datetime, current_date)
        return str(icu_day) if icu_day is not None else "-"

    @staticmethod
    def _format_department_time(admission_datetime, current_date) -> str:
        elapsed_seconds = max(0, int((current_date - admission_datetime).total_seconds()))
        total_minutes = (elapsed_seconds // 60 // 10) * 10
        hours, minutes = divmod(total_minutes, 60)
        return f"{hours}ч {minutes:02d}м"
