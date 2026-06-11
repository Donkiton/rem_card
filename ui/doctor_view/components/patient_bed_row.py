from PySide6.QtWidgets import (QSplitter, QSizePolicy, QVBoxLayout, QWidget, QFrame)
from PySide6.QtCore import Qt, Signal
from datetime import datetime
from ...patient_bed_management.bed_labels import is_recovery_bed
from ...rem_card_sectors.sector_4_sub import Sector4b, Sector4v
from ...shared.layout_components import SplitterManager

class PatientBedRow(QWidget):
    """Компонент W1 для списка коек. Должен быть визуальным клоном сектора W."""
    # Сигнал теперь принимает (patient, action_type)
    show_card_requested = Signal(object, str)
    create_card_requested = Signal(object)
    archive_requested = Signal(object)
    full_report_requested = Signal(object)
    daily_report_requested = Signal(object)
    transfer_requested = Signal(object)
    cancel_transfer_requested = Signal(object)

    def __init__(self, patient, parent=None):
        super().__init__(parent)
        self.patient = patient
        
        # Важно для QScrollArea: разрешаем расширяться по горизонтали, но фиксируем по вертикали
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        
        # Основной вертикальный лейаут БЕЗ отступов, чтобы сектора прилегали друг к другу
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
        )

    def init_ui(self):
        # Создаем оригинальные сектора
        self.sector_4b = Sector4b()
        self.sector_4b.setFixedHeight(56)
        
        self.sector_4v = Sector4v()
        self.sector_4v.setFixedHeight(42)
        
        # Удаляем лейблы базового класса, чтобы они не занимали место в лейауте ("футтеры")
        if hasattr(self.sector_4b, "label"):
            self.sector_4b.label.setParent(None)
        if hasattr(self.sector_4v, "label"):
            self.sector_4v.label.setParent(None)
            
        # Устанавливаем политику размеров, аналогичную оригиналу в секторе W
        self.sector_4b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.sector_4v.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        
        # Меняем заголовок 4б, чтобы включить номер койки
        bed_num = self.patient.bed_number if hasattr(self.patient, "bed_number") else "-"
        self.sector_4b.header_lbl.setText(f"Информация (Койка {bed_num})")
        
        # Настройка внутренних отступов для полного сходства с сектором W:
        self.sector_4b.main_layout_v.setContentsMargins(0, 0, 0, 0)
        self.sector_4b.content_layout.setContentsMargins(0, 0, 0, 0)
        
        self.sector_4v.main_layout_v.setContentsMargins(0, 0, 0, 0)
        self.sector_4v.content_layout.setContentsMargins(7, 3, 10, 3)
        
        # Подгружаем данные пациента
        now = datetime.now()
        self.sector_4b.update_patient_info(
            self.patient,
            now,
            is_recovery=is_recovery_bed(getattr(self.patient, "bed_number", None)),
        )
        
        # НАСТРОЙКА КНОПОК:
        self.sector_4v.btn_card_list.setEnabled(True)
        self.sector_4v.btn_yest_card.setEnabled(True)
        
        self.sector_4v.btn_show_card.setEnabled(False)
        self.sector_4v.btn_new_card.setEnabled(False)
        
        # Перехватываем сигналы кнопок
        self.sector_4v.show_card_requested.connect(lambda: self.show_card_requested.emit(self.patient, "show"))
        self.sector_4v.create_card_requested.connect(lambda: self.create_card_requested.emit(self.patient))
        self.sector_4v.archive_requested.connect(lambda: self.archive_requested.emit(self.patient))
        self.sector_4v.full_report_requested.connect(lambda: self.full_report_requested.emit(self.patient))
        self.sector_4v.daily_report_requested.connect(lambda: self.daily_report_requested.emit(self.patient))
        self.sector_4v.yest_card_requested.connect(lambda: self.show_card_requested.emit(self.patient, "yest"))
        self.sector_4v.recovery_transfer_requested.connect(lambda: self.transfer_requested.emit(self.patient))
        self.sector_4v.recovery_cancel_transfer_requested.connect(lambda: self.cancel_transfer_requested.emit(self.patient))
        
        # Добавляем виджеты в лейаут напрямую
        self.main_layout.addWidget(self.sector_4b)
        self.main_layout.addWidget(self.sector_4v)
