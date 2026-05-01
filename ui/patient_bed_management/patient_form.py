from datetime import datetime

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QCursor, QMouseEvent
from PySide6.QtWidgets import (
    QDialog,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from rem_card.services.mkb import MKBService
from rem_card.services.patient_bed_management import AdmissionRecord, PatientBedManagementService, PatientRecord
from rem_card.ui.patient_bed_management.tabs.diagnosis_tab import DiagnosisTabWidget
from rem_card.ui.patient_bed_management.tabs.general_tab import GeneralTabWidget
from rem_card.ui.shared.custom_message_box import CustomMessageBox


class PatientForm(QDialog):
    def __init__(
        self,
        patient_bed_service: PatientBedManagementService,
        bed_number: int,
        patient: PatientRecord = None,
        admission: AdmissionRecord = None,
        parent=None,
    ):
        super().__init__(parent)
        self.patient_bed_service = patient_bed_service
        self.mkb_service = MKBService()
        self.bed_number = int(bed_number)
        self.patient = patient
        self.admission = admission
        self.is_new_admission = patient is None and admission is None

        self._cursor = QCursor()
        self._resizing = False
        self._dragging = False
        self._drag_pos = QPoint()
        self._resize_edges = Qt.Edge(0)

        self.bg_color = "#f5f2e9"
        self.border_color = "#d1d1bc"

        self.setWindowTitle(f"Карта пациента - Койка {self.bed_number}")
        self.setMinimumSize(800, 600)
        self.resize(980, 770)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setMouseTracking(True)

        self._init_ui()
        self._load_data()

    def _init_ui(self):
        self.bg_container = QWidget(self)
        self.bg_container.setObjectName("bg_container")
        self.bg_container.setMouseTracking(True)
        self.bg_container.setStyleSheet(
            f"""
            QWidget#bg_container {{
                background-color: {self.bg_color};
                border: 2px solid {self.border_color};
                border-radius: 5px;
            }}
            """
        )

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(30)
        shadow.setColor(QColor(0, 0, 0, 40))
        shadow.setOffset(0, 5)
        self.bg_container.setGraphicsEffect(shadow)

        self.layout_container = QVBoxLayout(self)
        self.layout_container.setContentsMargins(10, 10, 10, 10)
        self.layout_container.addWidget(self.bg_container)

        self.main_layout = QVBoxLayout(self.bg_container)
        self.main_layout.setContentsMargins(16, 8, 16, 16)
        self.main_layout.setSpacing(10)

        self._init_header()
        self._init_form()
        self._init_buttons()

    def _init_header(self):
        self.header_panel = QWidget()
        self.header_panel.setFixedHeight(40)
        self.header_panel.setStyleSheet("background: transparent;")
        header_layout = QHBoxLayout(self.header_panel)
        header_layout.setContentsMargins(10, 0, 0, 0)

        title_label = QLabel(f"КАРТОЧКА ПАЦИЕНТА - КОЙКА {self.bed_number}")
        title_label.setStyleSheet(
            "color: #4a4a3a; font-size: 13px; font-weight: 800; "
            "letter-spacing: 1px; background: transparent;"
        )
        header_layout.addWidget(title_label)
        header_layout.addStretch()

        close_button = QPushButton("x")
        close_button.setFixedSize(30, 30)
        close_button.setCursor(Qt.PointingHandCursor)
        close_button.setStyleSheet(
            """
            QPushButton { background: transparent; color: #7a7a6a; font-size: 22px; border: none; }
            QPushButton:hover { background: #ef4444; color: white; border-radius: 5px; }
            """
        )
        close_button.clicked.connect(self.reject)
        header_layout.addWidget(close_button)
        self.main_layout.addWidget(self.header_panel)

    def _init_form(self):
        self.general_tab = GeneralTabWidget(self)
        self.diagnosis_tab = DiagnosisTabWidget(self.mkb_service, self, show_operations=False)

        tab_styling = f"""
            QWidget {{ background-color: {self.bg_color}; }}
            QLabel {{ color: #4a4a3f; font-size: 13px; font-weight: 600; background: transparent; }}
            QLineEdit, QComboBox, QSpinBox, QDateTimeEdit {{
                padding: 8px;
                border: 1px solid #c9c9b4;
                border-radius: 5px;
                background: #fdfdfa;
                color: #2d2d24;
            }}
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDateTimeEdit:focus {{
                border: 1px solid #8a8a68;
                background: white;
            }}
        """
        self.general_tab.setStyleSheet(tab_styling)
        self.diagnosis_tab.setStyleSheet(tab_styling)
        label_column_width = 250
        self.general_tab.set_label_column_width(label_column_width)
        self.diagnosis_tab.set_label_column_width(label_column_width)

        self.form_scroll = QScrollArea()
        self.form_scroll.setWidgetResizable(True)
        self.form_scroll.setFrameShape(QScrollArea.NoFrame)
        self.form_scroll.setStyleSheet(
            f"""
            QScrollArea {{
                border: 1px solid {self.border_color};
                background: {self.bg_color};
                border-radius: 5px;
            }}
            """
        )

        self.form_page = QWidget()
        self.form_page.setStyleSheet(f"background-color: {self.bg_color};")
        form_page_layout = QVBoxLayout(self.form_page)
        form_page_layout.setContentsMargins(0, 0, 0, 0)
        form_page_layout.setSpacing(2)

        general_title = QLabel("ОБЩИЕ ДАННЫЕ")
        general_title.setStyleSheet(
            "color: #4a4a3a; font-size: 12px; font-weight: 800; "
            "letter-spacing: 1px; margin: 10px 24px 0 24px; background: transparent;"
        )
        diagnosis_title = QLabel("ДИАГНОЗ")
        diagnosis_title.setStyleSheet(
            "color: #4a4a3a; font-size: 12px; font-weight: 800; "
            "letter-spacing: 1px; margin: 0 24px 0 24px; background: transparent;"
        )

        form_page_layout.addWidget(general_title)
        form_page_layout.addWidget(self.general_tab)
        form_page_layout.addWidget(diagnosis_title)
        form_page_layout.addWidget(self.diagnosis_tab)
        form_page_layout.addStretch(1)

        self.form_scroll.setWidget(self.form_page)
        self.main_layout.addWidget(self.form_scroll, 1)

    def _init_buttons(self):
        buttons_layout = QHBoxLayout()
        buttons_layout.setContentsMargins(0, 10, 0, 0)

        cancel_button = QPushButton("ОТМЕНИТЬ")
        cancel_button.setCursor(Qt.PointingHandCursor)
        cancel_button.setFixedHeight(45)
        cancel_button.setStyleSheet(
            """
            QPushButton { background: #f5f3e9; border: 1px solid #dcdcc6; border-radius: 5px; color: #7e7e6d; font-weight: 700; font-size: 12px; }
            QPushButton:hover { background: #ebe8d5; color: #2d2d24; }
            """
        )
        cancel_button.clicked.connect(self.reject)

        save_button = QPushButton("СОХРАНИТЬ КАРТОЧКУ")
        save_button.setCursor(Qt.PointingHandCursor)
        save_button.setFixedHeight(45)
        save_button.setStyleSheet(
            """
            QPushButton { background: #5d5d3d; border: none; border-radius: 5px; color: white; font-weight: 800; font-size: 12px; }
            QPushButton:hover { background: #4a4a31; }
            """
        )
        save_button.clicked.connect(self._save_data)

        buttons_layout.addWidget(cancel_button, 1)
        buttons_layout.addWidget(save_button, 2)
        self.main_layout.addLayout(buttons_layout)

    def _load_data(self):
        if not self.is_new_admission:
            self.general_tab.set_data(self.patient, self.admission)
            if self.admission:
                self.diagnosis_tab.set_data(self.admission, [])

    def _validate_input(self) -> bool:
        gen = self.general_tab.get_data()
        diag = self.diagnosis_tab.get_data()
        if not gen["history_number"] or not gen["full_name"]:
            CustomMessageBox.warning(self, "Ошибка", "Заполните номер ИБ и ФИО пациента")
            return False
        if not diag["diagnosis_text"]:
            CustomMessageBox.warning(self, "Ошибка", "Необходимо указать диагноз")
            return False
        return True

    def _save_data(self):
        if not self._validate_input():
            return
        try:
            gen_data = self.general_tab.get_data()
            diag_data = self.diagnosis_tab.get_data()
            patient_data = {"full_name": gen_data["full_name"]}
            admission_data = {
                "bed_number": self.bed_number,
                "history_number": gen_data["history_number"],
                "admission_datetime": gen_data["admission_datetime"],
                "patient_age": gen_data["age_value"],
                "patient_months": gen_data["months"],
                "patient_age_unit": gen_data["age_unit"],
                "patient_gender": gen_data["gender"],
                "diagnosis_code": diag_data["diagnosis_code"],
                "diagnosis_text": diag_data["diagnosis_text"],
                "department_profile": gen_data["department_profile"],
                "source_department": gen_data["source_department"],
                "updated_at": datetime.now(),
            }

            if self.is_new_admission:
                self.patient_bed_service.create_patient_and_admission(patient_data, admission_data)
            else:
                self.patient_bed_service.update_patient_and_admission(
                    int(self.patient.id),
                    int(self.admission.id),
                    patient_data,
                    admission_data,
                )
            self.accept()
        except Exception as exc:
            CustomMessageBox.warning(self, "Ошибка", f"Не удалось сохранить данные:\n{exc}")

    def reject(self):
        self.mkb_service.close_connection()
        super().reject()

    def accept(self):
        self.mkb_service.close_connection()
        super().accept()

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._dragging = True
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._dragging = False
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if event.buttons() & Qt.LeftButton and self._dragging:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(event)
