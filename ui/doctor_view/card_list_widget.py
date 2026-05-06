import os
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QListWidget, QListWidgetItem, QDialog, QPushButton, QFrame
from PySide6.QtGui import QIcon
from PySide6.QtCore import Qt, Signal, QSize, QTimer
from datetime import datetime
from ..shared.async_call import AsyncCallThread
from ..shared.custom_title_bar import CustomTitleBar
from rem_card.ui.styles.shared_styles import apply_patient_archive_list_style

class CardListWidget(QWidget):
    card_selected = Signal(object) # передает datetime
    
    def __init__(self, remcard_service, parent=None):
        super().__init__(parent)
        self.remcard_service = remcard_service
        self.current_patient_id = None
        self._load_worker = None
        self._load_pending_patient = None
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        self.title_lbl = QLabel("СПИСОК КАРТ ПАЦИЕНТА")
        self.title_lbl.setProperty("heading", "true")
        layout.addWidget(self.title_lbl)
        
        self.list_widget = QListWidget()
        apply_patient_archive_list_style(self.list_widget)
        self.list_widget.itemClicked.connect(self.on_item_clicked)
        layout.addWidget(self.list_widget)

    def load_cards(self, patient):
        self.current_patient_id = patient.id
        self.title_lbl.setText(f"<b>СПИСОК КАРТ ПАЦИЕНТА:</b> {patient.get_display_name()}")
        self.list_widget.clear()
        self.list_widget.addItem(QListWidgetItem("Загрузка..."))
        if self._load_worker and self._load_worker.isRunning():
            self._load_pending_patient = patient
            return

        self._load_pending_patient = None
        worker = AsyncCallThread(self.remcard_service.get_all_card_dates, patient.id, parent=self)
        self._load_worker = worker
        worker.succeeded.connect(lambda dates, p=patient: self._apply_loaded_cards(p, dates))
        worker.failed.connect(self._on_load_failed)
        worker.finished.connect(self._on_load_finished)
        worker.start()

    def _apply_loaded_cards(self, patient, dates):
        if self.current_patient_id != patient.id:
            return

        self.list_widget.clear()
        if not dates:
            item = QListWidgetItem("Нет сохраненных карт")
            item.setFlags(Qt.NoItemFlags)
            self.list_widget.addItem(item)
            return

        for i, dt in enumerate(dates, 1):
            date_str = dt.strftime("%d.%m.%Y")
            item = QListWidgetItem(f"Карта №{i} — {date_str}")
            item.setData(Qt.UserRole, dt)
            self.list_widget.addItem(item)

    def _on_load_failed(self, exc: Exception):
        self.list_widget.clear()
        item = QListWidgetItem("Не удалось загрузить список карт")
        item.setFlags(Qt.NoItemFlags)
        self.list_widget.addItem(item)

    def _on_load_finished(self):
        self._load_worker = None
        if self._load_pending_patient is not None:
            pending_patient = self._load_pending_patient
            self._load_pending_patient = None
            QTimer.singleShot(0, lambda p=pending_patient: self.load_cards(p))

    def on_item_clicked(self, item):
        dt = item.data(Qt.UserRole)
        if dt:
            self.card_selected.emit(dt)

class PatientArchiveDialog(QDialog):
    """Диалог выбора карты из архива с подтверждением."""
    def __init__(self, remcard_service, patient, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.remcard_service = remcard_service
        self.patient = patient
        self.selected_date = None
        
        # Вычисление пути к иконкам относительно текущего файла
        self.icon_dir = os.path.join(os.path.dirname(__file__), "..", "..", "icon")
        self.icon_dir = os.path.normpath(self.icon_dir)
        
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle("Архив карт текущего пациента")
        self.setMinimumSize(500, 600)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        main_frame = QFrame(self)
        main_frame.setObjectName("MainFrame")
        main_frame.setStyleSheet("""
            #MainFrame {
                background-color: #f8f9fa;
                border-radius: 10px;
                border: 1px solid #bdc3c7;
            }
        """)
        
        main_layout = QVBoxLayout(main_frame)
        main_layout.setContentsMargins(0, 0, 0, 20)
        main_layout.setSpacing(15)
        
        title_bar = CustomTitleBar(self)
        title_bar.title_label.setText("Архив карт текущего пациента")
        main_layout.addWidget(title_bar)
        
        content_layout = QVBoxLayout()
        content_layout.setContentsMargins(20, 0, 20, 0)
        content_layout.setSpacing(15)
        
        # Список карт
        self.card_list_widget = CardListWidget(self.remcard_service, self)
        self.card_list_widget.load_cards(self.patient)
        
        # Подключаем клик по элементу списка для активации кнопки загрузки
        self.card_list_widget.list_widget.itemClicked.connect(self.on_item_selected)
        
        content_layout.addWidget(self.card_list_widget)
        
        # Кнопка загрузки выбранной карты
        self.btn_load = QPushButton(" Загрузить выбранную карту")
        icon_path = os.path.join(self.icon_dir, "archivepacient.png")
        if os.path.exists(icon_path):
            self.btn_load.setIcon(QIcon(icon_path))
            self.btn_load.setIconSize(QSize(24, 24))
            
        self.btn_load.setFixedHeight(55)
        self.btn_load.setEnabled(False) # Изначально неактивна
        
        # Стиль кнопки во всю ширину
        self.btn_load.setStyleSheet("""
            QPushButton {
                font-size: 15px; 
                font-weight: bold; 
                background-color: #27ae60; 
                color: white; 
                border-radius: 10px; 
                border: none;
            }
            QPushButton:hover {
                background-color: #2ecc71;
            }
            QPushButton:pressed {
                background-color: #1e8449;
            }
            QPushButton:disabled {
                background-color: #bdc3c7;
                color: #ecf0f1;
            }
        """)
        
        self.btn_load.clicked.connect(self.accept)
        content_layout.addWidget(self.btn_load)
        
        main_layout.addLayout(content_layout)
        layout.addWidget(main_frame)

    def on_item_selected(self, item):
        dt = item.data(Qt.UserRole)
        if dt:
            self.selected_date = dt
            self.btn_load.setEnabled(True)

    def get_selected_date(self):
        return self.selected_date
