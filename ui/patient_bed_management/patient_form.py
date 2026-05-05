from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
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
from rem_card.ui.shared.window_state import SavedFramelessDialogMixin
from rem_card.ui.styles.theme import (
    STYLE_DIALOG_CLOSE_BUTTON,
    STYLE_PATIENT_FORM_CANCEL_BUTTON,
    STYLE_PATIENT_FORM_CONTAINER,
    STYLE_PATIENT_FORM_PAGE,
    STYLE_PATIENT_FORM_SAVE_BUTTON,
    STYLE_PATIENT_FORM_SCROLL,
    STYLE_PATIENT_FORM_SECTION_TITLE,
    STYLE_PATIENT_FORM_TAB,
    STYLE_PATIENT_FORM_TITLE,
    STYLE_TRANSPARENT_WIDGET,
)


class PatientForm(SavedFramelessDialogMixin, QDialog):
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
        self._write_pending = False

        self.setWindowTitle(f"Карта пациента - Койка {self.bed_number}")
        self.setMinimumSize(800, 600)
        self.resize(980, 770)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setMouseTracking(True)
        self._init_saved_frameless_dialog("patient_bed_management/patient_form_geometry", drag_area_height=70)

        self._init_ui()
        self._restore_saved_geometry()
        self._load_data()

    def _init_ui(self):
        self.bg_container = QWidget(self)
        self.bg_container.setObjectName("bg_container")
        self.bg_container.setMouseTracking(True)
        self.bg_container.setStyleSheet(STYLE_PATIENT_FORM_CONTAINER)

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
        self.header_panel.setStyleSheet(STYLE_TRANSPARENT_WIDGET)
        header_layout = QHBoxLayout(self.header_panel)
        header_layout.setContentsMargins(10, 0, 0, 0)

        title_label = QLabel(f"КАРТОЧКА ПАЦИЕНТА - КОЙКА {self.bed_number}")
        title_label.setStyleSheet(STYLE_PATIENT_FORM_TITLE)
        header_layout.addWidget(title_label)
        header_layout.addStretch()

        close_button = QPushButton("x")
        close_button.setFixedSize(30, 30)
        close_button.setCursor(Qt.PointingHandCursor)
        close_button.setStyleSheet(STYLE_DIALOG_CLOSE_BUTTON)
        close_button.clicked.connect(self.reject)
        header_layout.addWidget(close_button)
        self.main_layout.addWidget(self.header_panel)

    def _init_form(self):
        self.general_tab = GeneralTabWidget(self)
        self.diagnosis_tab = DiagnosisTabWidget(self.mkb_service, self, show_operations=False)

        self.general_tab.setStyleSheet(STYLE_PATIENT_FORM_TAB)
        self.diagnosis_tab.setStyleSheet(STYLE_PATIENT_FORM_TAB)
        label_column_width = 250
        self.general_tab.set_label_column_width(label_column_width)
        self.diagnosis_tab.set_label_column_width(label_column_width)

        self.form_scroll = QScrollArea()
        self.form_scroll.setWidgetResizable(True)
        self.form_scroll.setFrameShape(QScrollArea.NoFrame)
        self.form_scroll.setStyleSheet(STYLE_PATIENT_FORM_SCROLL)

        self.form_page = QWidget()
        self.form_page.setStyleSheet(STYLE_PATIENT_FORM_PAGE)
        form_page_layout = QVBoxLayout(self.form_page)
        form_page_layout.setContentsMargins(0, 0, 0, 0)
        form_page_layout.setSpacing(2)

        general_title = QLabel("ОБЩИЕ ДАННЫЕ")
        general_title.setStyleSheet(STYLE_PATIENT_FORM_SECTION_TITLE)
        diagnosis_title = QLabel("ДИАГНОЗ")
        diagnosis_title.setStyleSheet(STYLE_PATIENT_FORM_SECTION_TITLE)

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

        self.cancel_button = QPushButton("ОТМЕНИТЬ")
        self.cancel_button.setCursor(Qt.PointingHandCursor)
        self.cancel_button.setFixedHeight(45)
        self.cancel_button.setStyleSheet(STYLE_PATIENT_FORM_CANCEL_BUTTON)
        self.cancel_button.clicked.connect(self.reject)

        self.save_button = QPushButton("СОХРАНИТЬ КАРТОЧКУ")
        self.save_button.setCursor(Qt.PointingHandCursor)
        self.save_button.setFixedHeight(45)
        self.save_button.setStyleSheet(STYLE_PATIENT_FORM_SAVE_BUTTON)
        self.save_button.clicked.connect(self._save_data)

        buttons_layout.addWidget(self.cancel_button, 1)
        buttons_layout.addWidget(self.save_button, 2)
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
        if self._write_pending:
            return
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
                description = f"patient_bed_create_admission:{self.bed_number}"

                def operation():
                    return self.patient_bed_service.create_patient_and_admission(patient_data, admission_data)
            else:
                patient_id = int(self.patient.id)
                admission_id = int(self.admission.id)
                expected_admission_revision = int(getattr(self.admission, "revision", 0) or 0)
                description = f"patient_bed_update_admission:{admission_id}"

                def operation():
                    return self.patient_bed_service.update_patient_and_admission(
                        patient_id,
                        admission_id,
                        patient_data,
                        admission_data,
                        expected_admission_revision=expected_admission_revision,
                    )

            self._begin_write_pending()
            self.patient_bed_service.enqueue_write(
                description,
                operation,
                on_success=lambda _result: self._on_write_success(),
                on_error=self._on_write_error,
            )
        except Exception as exc:
            self._finish_write_pending()
            CustomMessageBox.warning(self, "Ошибка", f"Не удалось сохранить данные:\n{exc}")

    def _begin_write_pending(self):
        self._write_pending = True
        self.form_page.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.save_button.setText("СОХРАНЕНИЕ...")

    def _finish_write_pending(self):
        self._write_pending = False
        self.form_page.setEnabled(True)
        self.cancel_button.setEnabled(True)
        self.save_button.setEnabled(True)
        self.save_button.setText("СОХРАНИТЬ КАРТОЧКУ")

    def _on_write_success(self):
        self._finish_write_pending()
        self.accept()

    def _on_write_error(self, exc):
        self._finish_write_pending()
        CustomMessageBox.warning(self, "Ошибка", f"Не удалось сохранить данные:\n{exc}")

    def reject(self):
        self.mkb_service.close_connection()
        super().reject()

    def accept(self):
        self.mkb_service.close_connection()
        super().accept()
