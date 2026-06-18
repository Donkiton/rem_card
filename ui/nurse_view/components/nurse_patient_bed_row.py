from PySide6.QtWidgets import (QSizePolicy, QVBoxLayout, QWidget)
from PySide6.QtCore import Qt, Signal
from datetime import datetime
from ...patient_bed_management.bed_labels import is_recovery_bed
from ...rem_card_sectors.sector_4_sub import Sector4b
from ..sectors.nurse_sector_4v import NurseSector4v

class NursePatientBedRow(QWidget):
    """Компонент W1 для списка коек медсестры."""
    show_card_requested = Signal(object, str)
    archive_requested = Signal(object)
    full_report_requested = Signal(object)
    daily_report_requested = Signal(object)
    transfer_requested = Signal(object)
    cancel_transfer_requested = Signal(object)

    def __init__(self, patient, parent=None):
        super().__init__(parent)
        self.patient = patient
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)
        self.init_ui()

    def update_patient(self, patient, now: datetime | None = None):
        self.patient = patient
        bed_num = self.patient.bed_number if hasattr(self.patient, "bed_number") else "-"
        self.sector_4b.header_lbl.setText(f"Информация (Койка {bed_num})")
        self.sector_4b.update_patient_info(
            self.patient,
            now or datetime.now(),
            is_recovery=is_recovery_bed(getattr(self.patient, "bed_number", None)),
            auto_update_recovery_time=True,
        )

    def init_ui(self):
        # Используем стандартный сектор 4б (инфо), так как он не меняется
        self.sector_4b = Sector4b()
        self.sector_4b.setFixedHeight(56)
        
        # Используем специфичный для медсестры сектор 4в (кнопки)
        self.sector_4v = NurseSector4v()
        self.sector_4v.setFixedHeight(42)
        
        if hasattr(self.sector_4b, "label"):
            self.sector_4b.label.setParent(None)
        if hasattr(self.sector_4v, "label"):
            self.sector_4v.label.setParent(None)
            
        self.sector_4b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.sector_4v.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        
        bed_num = self.patient.bed_number if hasattr(self.patient, "bed_number") else "-"
        self.sector_4b.header_lbl.setText(f"Информация (Койка {bed_num})")
        
        self.sector_4b.main_layout_v.setContentsMargins(0, 0, 0, 0)
        self.sector_4b.content_layout.setContentsMargins(0, 0, 0, 0)
        
        self.sector_4v.main_layout_v.setContentsMargins(0, 0, 0, 0)
        self.sector_4v.content_layout.setContentsMargins(7, 3, 10, 3)
        
        now = datetime.now()
        self.sector_4b.update_patient_info(
            self.patient,
            now,
            is_recovery=is_recovery_bed(getattr(self.patient, "bed_number", None)),
            auto_update_recovery_time=True,
        )
        
        # Настройка сигналов
        self.sector_4v.show_card_requested.connect(lambda: self.show_card_requested.emit(self.patient, "show"))
        self.sector_4v.yest_card_requested.connect(lambda: self.show_card_requested.emit(self.patient, "yest"))
        self.sector_4v.archive_requested.connect(lambda: self.archive_requested.emit(self.patient))
        self.sector_4v.full_report_requested.connect(lambda: self.full_report_requested.emit(self.patient))
        self.sector_4v.daily_report_requested.connect(lambda: self.daily_report_requested.emit(self.patient))
        self.sector_4v.recovery_transfer_requested.connect(lambda: self.transfer_requested.emit(self.patient))
        self.sector_4v.recovery_cancel_transfer_requested.connect(lambda: self.cancel_transfer_requested.emit(self.patient))
        
        self.main_layout.addWidget(self.sector_4b)
        self.main_layout.addWidget(self.sector_4v)
