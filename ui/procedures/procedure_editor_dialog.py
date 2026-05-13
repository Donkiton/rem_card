from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QDateTime, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QGraphicsDropShadowEffect,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from rem_card.data.dto.procedures_dto import (
    ProcedureBundle,
    ProcedureDTO,
    ProcedureStatus,
    ProcedureType,
)
from rem_card.services.doctor_list_service import DoctorListStore
from rem_card.ui.procedures.cvc_consent_widget import CvcConsentWidget
from rem_card.ui.procedures.cvc_procedure_widget import CvcProcedureWidget
from rem_card.ui.procedures.cvc_removal_widget import CvcRemovalWidget
from rem_card.ui.procedures.lumbar_puncture_consent_widget import LumbarPunctureConsentWidget
from rem_card.ui.procedures.lumbar_puncture_outcome_widget import LumbarPunctureOutcomeWidget
from rem_card.ui.procedures.lumbar_puncture_widget import LumbarPunctureWidget
from rem_card.ui.procedures.procedure_datetime_edit import ProcedureDateTimeEdit
from rem_card.ui.procedures.procedure_styles import (
    PROCEDURE_DIALOG_STYLE,
    apply_procedure_combo_style,
    apply_procedure_datetime_style,
)
from rem_card.ui.procedures.procedure_pdf_worker import ProcedurePdfWorker
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.shared.custom_title_bar import CustomTitleBar
from rem_card.ui.shared.pdf_opener import open_pdf_file
from rem_card.ui.shared.window_state import SavedFramelessDialogMixin


class ProcedureEditorDialog(SavedFramelessDialogMixin, QDialog):
    def __init__(
        self,
        remcard_service,
        admission_id: int,
        *,
        procedure_id: Optional[int] = None,
        procedure_type: str = ProcedureType.CVC.value,
        parent=None,
    ):
        super().__init__(parent)
        self.remcard_service = remcard_service
        self.admission_id = int(admission_id)
        self.procedure_id = procedure_id
        self.procedure_type = procedure_type
        self.bundle: Optional[ProcedureBundle] = None
        self._write_pending = False
        self._pdf_worker = None
        self._print_buttons: list[QPushButton] = []

        self.setWindowTitle("Процедура пациента")
        self.setMinimumSize(860, 620)
        self.resize(980, 720)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setMouseTracking(True)
        self._init_saved_frameless_dialog("procedures/procedure_editor_geometry", drag_area_height=72)
        self._load_bundle()
        self._build_ui()
        self._apply_bundle()
        self._restore_saved_geometry()

    def _load_bundle(self):
        if self.procedure_id:
            self.bundle = self.remcard_service.get_procedure_bundle(int(self.procedure_id))
            if not self.bundle:
                raise ValueError("Процедура не найдена.")
            self.procedure_type = self.bundle.procedure.procedure_type
            return
        if self.procedure_type == ProcedureType.CVC.value:
            self.bundle = self.remcard_service.create_empty_cvc_procedure(self.admission_id)
            return
        if self.procedure_type == ProcedureType.LUMBAR_PUNCTURE.value:
            self.bundle = self.remcard_service.create_empty_lumbar_puncture_procedure(self.admission_id)
            return
        raise ValueError("Этот тип процедуры пока не реализован.")

    def _build_ui(self):
        self.setStyleSheet(PROCEDURE_DIALOG_STYLE)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(0)

        self.main_frame = QFrame(self)
        self.main_frame.setObjectName("ProcedureDialogMainFrame")
        self.main_frame.setMouseTracking(True)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(30)
        shadow.setColor(QColor(0, 0, 0, 55))
        shadow.setOffset(0, 6)
        self.main_frame.setGraphicsEffect(shadow)
        root.addWidget(self.main_frame)

        content = QVBoxLayout(self.main_frame)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(8)

        self.title_bar = CustomTitleBar(self)
        self.title_bar.title_label.setText(self._dialog_title())
        self.title_bar.btn_minimize.hide()
        self.title_bar.btn_maximize.hide()
        content.addWidget(self.title_bar)

        body = QWidget()
        body.setObjectName("ProcedureDialogBody")
        body.setAttribute(Qt.WA_StyledBackground, True)
        body.setMouseTracking(True)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(12, 0, 12, 12)
        body_layout.setSpacing(8)
        content.addWidget(body, 1)

        self.header_label = QLabel()
        self.header_label.setObjectName("procedure_header")
        self.header_label.setStyleSheet(
            """
            QLabel#procedure_header {
                background: #ffffff;
                border: 1.5px solid #bdc3c7;
                border-radius: 5px;
                padding: 8px;
                font-weight: bold;
                color: #243b53;
            }
            """
        )
        body_layout.addWidget(self.header_label)

        self.tabs = QTabWidget()
        body_layout.addWidget(self.tabs, 1)

        self.general_tab = QWidget()
        self.general_tab.setObjectName("ProcedureTabPage")
        self.general_tab.setAttribute(Qt.WA_StyledBackground, True)
        self._build_general_tab()
        self.tabs.addTab(self.general_tab, "Основные данные")

        self._build_procedure_tabs()

        self.print_tab = QWidget()
        self.print_tab.setObjectName("ProcedureTabPage")
        self.print_tab.setAttribute(Qt.WA_StyledBackground, True)
        self._build_print_tab()
        self.tabs.addTab(self.print_tab, "Печать")

        footer = QHBoxLayout()
        self.status_label = QLabel("")
        self.save_btn = QPushButton("Сохранить")
        self.save_btn.setObjectName("DialogOkBtn")
        self.save_btn.clicked.connect(self._on_save_clicked)
        self.close_btn = QPushButton("Закрыть")
        self.close_btn.setObjectName("DialogCancelBtn")
        self.close_btn.clicked.connect(self.reject)
        footer.addWidget(self.status_label, 1)
        footer.addWidget(self.save_btn)
        footer.addWidget(self.close_btn)
        body_layout.addLayout(footer)
        apply_procedure_combo_style(self)
        apply_procedure_datetime_style(self)

    def _build_procedure_tabs(self):
        self.cvc_widget = None
        self.removal_widget = None
        self.lp_widget = None
        self.lp_outcome_widget = None

        if self.procedure_type == ProcedureType.CVC.value:
            self.cvc_widget = CvcProcedureWidget()
            self.tabs.addTab(self.cvc_widget, "Медицинская часть")

            self.consent_widget = CvcConsentWidget()
            self.tabs.addTab(self.consent_widget, "Согласие ЦВК")

            self.removal_widget = CvcRemovalWidget()
            self.tabs.addTab(self.removal_widget, "Удаление/переустановка")
            return

        if self.procedure_type == ProcedureType.LUMBAR_PUNCTURE.value:
            self.lp_widget = LumbarPunctureWidget()
            self.tabs.addTab(self.lp_widget, "Медицинская часть")

            self.consent_widget = LumbarPunctureConsentWidget()
            self.tabs.addTab(self.consent_widget, "Согласие на пункцию")

            self.lp_outcome_widget = LumbarPunctureOutcomeWidget()
            self.tabs.addTab(self.lp_outcome_widget, "Итог")
            return

        raise ValueError("Этот тип процедуры пока не реализован.")

    def _build_general_tab(self):
        layout = QVBoxLayout(self.general_tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        box = QGroupBox("Общие поля процедуры")
        form = QFormLayout(box)
        self.status_combo = QComboBox()
        self.status_combo.addItem("Черновик", ProcedureStatus.DRAFT.value)
        self.status_combo.addItem("Активна", ProcedureStatus.ACTIVE.value)

        now = datetime.now().replace(second=0, microsecond=0)
        self.start_edit = ProcedureDateTimeEdit()
        self.start_edit.setCalendarPopup(True)
        self.start_edit.setDisplayFormat("dd.MM.yyyy HH:mm")
        self.start_edit.setDateTime(QDateTime(now))
        self.finish_edit = ProcedureDateTimeEdit()
        self.finish_edit.setCalendarPopup(True)
        self.finish_edit.setDisplayFormat("dd.MM.yyyy HH:mm")
        self.finish_edit.setDateTime(QDateTime(now))
        self.duration_label = QLabel("0")
        self.start_edit.dateTimeChanged.connect(self._recalculate_duration)
        self.finish_edit.dateTimeChanged.connect(self._recalculate_duration)

        self.doctor_combo = QComboBox()
        self.doctor_combo.setEditable(True)
        self._fill_doctors()
        self.notes_edit = QTextEdit()
        self.notes_edit.setFixedHeight(90)
        self.notes_edit.setPlaceholderText("Примечание")

        form.addRow("Статус:", self.status_combo)
        form.addRow("Начало:", self.start_edit)
        form.addRow("Окончание:", self.finish_edit)
        form.addRow("Длительность, мин:", self.duration_label)
        form.addRow("Врач-исполнитель:", self.doctor_combo)
        form.addRow("Примечание:", self.notes_edit)
        layout.addWidget(box)
        layout.addStretch(1)

    def _build_print_tab(self):
        layout = QVBoxLayout(self.print_tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        if self.procedure_type == ProcedureType.LUMBAR_PUNCTURE.value:
            box_title = "Печатные формы люмбальной пункции"
            protocol_title = "Печать протокола люмбальной пункции"
            protocol_kind = "lp_protocol"
            consent_kind = "lp_consent"
            removal_kind = ""
        else:
            box_title = "Печатные формы ЦВК"
            protocol_title = "Печать протокола ЦВК"
            protocol_kind = "cvc_protocol"
            consent_kind = "cvc_consent"
            removal_kind = "cvc_removal"

        box = QGroupBox(box_title)
        row = QHBoxLayout(box)
        self.print_protocol_btn = QPushButton(protocol_title)
        self.print_protocol_btn.clicked.connect(lambda: self._print_document(protocol_kind))
        self.print_consent_btn = QPushButton("Печать согласия")
        self.print_consent_btn.clicked.connect(lambda: self._print_document(consent_kind))
        self._print_buttons = [self.print_protocol_btn, self.print_consent_btn]
        row.addWidget(self.print_protocol_btn)
        row.addWidget(self.print_consent_btn)
        if removal_kind:
            self.print_removal_btn = QPushButton("Печать удаления катетера")
            self.print_removal_btn.clicked.connect(lambda: self._print_document(removal_kind))
            self._print_buttons.append(self.print_removal_btn)
            row.addWidget(self.print_removal_btn)
        row.addStretch(1)
        layout.addWidget(box)

        hint = QFrame()
        hint_layout = QVBoxLayout(hint)
        hint_layout.addWidget(QLabel("Печать доступна после первого сохранения процедуры."))
        layout.addWidget(hint)
        layout.addStretch(1)

    def _fill_doctors(self):
        try:
            doctors = DoctorListStore().load_doctors()
        except Exception:
            doctors = []
        self.doctor_combo.addItem("", "")
        self.doctor_combo.addItems(doctors)

    def _dialog_title(self) -> str:
        if self.procedure_type == ProcedureType.LUMBAR_PUNCTURE.value:
            return "Люмбальная пункция"
        if self.procedure_type == ProcedureType.CVC.value:
            return "ЦВК"
        return "Процедура пациента"

    def _header_title(self) -> str:
        if self.procedure_type == ProcedureType.LUMBAR_PUNCTURE.value:
            return "Люмбальная пункция"
        if self.procedure_type == ProcedureType.CVC.value:
            return "Постановка ЦВК"
        return "Процедура пациента"

    def _apply_bundle(self):
        if not self.bundle:
            return
        procedure = self.bundle.procedure
        snapshot = self.bundle.patient_snapshot
        self._apply_datetime_bounds(snapshot)
        self.header_label.setText(
            f"{self._header_title()}: "
            f"{snapshot.get('full_name') or 'Неизвестно'} | "
            f"ИБ № {snapshot.get('history_number') or ''} | "
            f"возраст {snapshot.get('age') or ''} | "
            f"пол {snapshot.get('sex') or ''}"
        )
        self._set_combo_data(self.status_combo, procedure.status)
        if procedure.started_at:
            self.start_edit.setDateTime(QDateTime(procedure.started_at))
        if procedure.finished_at:
            self.finish_edit.setDateTime(QDateTime(procedure.finished_at))
        self.duration_label.setText(str(procedure.duration_minutes or 0))
        if procedure.doctor_name_snapshot:
            self.doctor_combo.setEditText(procedure.doctor_name_snapshot)
        self.notes_edit.setPlainText(procedure.notes)
        if self.procedure_type == ProcedureType.CVC.value:
            self.cvc_widget.load(self.bundle.cvc)
            self.consent_widget.load(self.bundle.consent)
            self.removal_widget.load(self.bundle.cvc)
        elif self.procedure_type == ProcedureType.LUMBAR_PUNCTURE.value:
            self.lp_widget.load(self.bundle.lumbar_puncture)
            self.consent_widget.load(self.bundle.consent)
            self.lp_outcome_widget.load(self.bundle.lumbar_puncture)
        self._update_print_enabled()

    def _collect_procedure(self) -> ProcedureDTO:
        if not self.bundle:
            raise ValueError("Данные процедуры не загружены.")
        base = self.bundle.procedure
        doctor_name = self.doctor_combo.currentText().strip()
        procedure = ProcedureDTO(
            id=base.id,
            patient_id=base.patient_id,
            admission_id=base.admission_id or self.admission_id,
            procedure_type=base.procedure_type,
            status=str(self.status_combo.currentData() or ProcedureStatus.DRAFT.value),
            created_at=base.created_at,
            updated_at=base.updated_at,
            started_at=self.start_edit.dateTime().toPython(),
            finished_at=self.finish_edit.dateTime().toPython(),
            duration_minutes=int(self.duration_label.text() or 0),
            doctor_id=base.doctor_id,
            doctor_name_snapshot=doctor_name,
            department_snapshot=base.department_snapshot,
            patient_snapshot_json=base.patient_snapshot_json,
            diagnosis_snapshot=base.diagnosis_snapshot,
            notes=self.notes_edit.toPlainText().strip(),
            created_by=base.created_by or "doctor",
            updated_by="doctor",
            revision=base.revision,
            is_deleted=base.is_deleted,
        )
        return procedure

    def _collect_bundle(self):
        procedure = self._collect_procedure()
        if self.procedure_type == ProcedureType.CVC.value:
            cvc = self.cvc_widget.collect(procedure_id=procedure.id or 0, doctor_name=procedure.doctor_name_snapshot)
            self.removal_widget.apply_to(cvc)
            consent = self.consent_widget.collect(
                procedure_id=procedure.id or 0,
                doctor_name=procedure.doctor_name_snapshot,
                diagnosis=procedure.diagnosis_snapshot,
            )
            return procedure, cvc, consent

        if self.procedure_type == ProcedureType.LUMBAR_PUNCTURE.value:
            lumbar_puncture = self.lp_widget.collect(
                procedure_id=procedure.id or 0,
                doctor_name=procedure.doctor_name_snapshot,
            )
            self.lp_outcome_widget.apply_to(lumbar_puncture)
            consent = self.consent_widget.collect(
                procedure_id=procedure.id or 0,
                doctor_name=procedure.doctor_name_snapshot,
                diagnosis=procedure.diagnosis_snapshot,
            )
            return procedure, lumbar_puncture, consent

        raise ValueError("Этот тип процедуры пока не реализован.")

    def _on_save_clicked(self):
        if self._write_pending:
            return
        try:
            procedure, cvc, consent = self._collect_bundle()
        except Exception as exc:
            CustomMessageBox.warning(self, "Процедуры", str(exc))
            return

        self._set_write_pending(True, "Сохранение...")
        service = self.remcard_service
        if procedure.procedure_type == ProcedureType.LUMBAR_PUNCTURE.value:
            description = f"procedure_lumbar_puncture_save_ui:{self.admission_id}"

            def operation():
                return service.save_lumbar_puncture_procedure(procedure, cvc, consent)

        else:
            description = f"procedure_cvc_save_ui:{self.admission_id}"

            def operation():
                return service.save_cvc_procedure(procedure, cvc, consent)

        service.enqueue_write(
            description=description,
            operation=operation,
            on_success=self._on_save_success,
            on_error=self._on_save_error,
        )

    def _on_save_success(self, procedure_id):
        self._set_write_pending(False, "Сохранено.")
        self.procedure_id = int(procedure_id)
        try:
            self.bundle = self.remcard_service.get_procedure_bundle(int(procedure_id))
            self._apply_bundle()
        except Exception:
            pass

    def _on_save_error(self, exc: Exception):
        self._set_write_pending(False, "Ошибка сохранения.")
        CustomMessageBox.warning(self, "Ошибка сохранения процедуры", str(exc))

    def _set_write_pending(self, pending: bool, text: str):
        self._write_pending = bool(pending)
        self.status_label.setText(text)
        self.save_btn.setEnabled(not pending)
        self.close_btn.setEnabled(not pending)

    def _print_document(self, document_kind: str):
        if not self.procedure_id:
            CustomMessageBox.warning(self, "Печать", "Сначала сохраните процедуру.")
            return
        if self._pdf_worker is not None and self._pdf_worker.isRunning():
            return
        try:
            pdf_path = Path(self.remcard_service.build_procedure_pdf_path(int(self.procedure_id), document_kind))
        except Exception as exc:
            CustomMessageBox.warning(self, "Печать", str(exc))
            return
        self.status_label.setText("Формирование PDF...")
        self._pdf_worker = ProcedurePdfWorker(
            self.remcard_service,
            int(self.procedure_id),
            document_kind,
            pdf_path,
            parent=self,
        )
        self._pdf_worker.completed.connect(self._on_pdf_ready)
        self._pdf_worker.failed.connect(self._on_pdf_error)
        self._pdf_worker.finished.connect(self._clear_pdf_worker)
        self._pdf_worker.start()

    def _on_pdf_ready(self, pdf_path: str):
        self.status_label.setText("PDF сформирован.")
        open_pdf_file(pdf_path, parent=self)

    def _on_pdf_error(self, message: str):
        self.status_label.setText("Ошибка PDF.")
        CustomMessageBox.warning(self, "Печать", message)

    def _clear_pdf_worker(self):
        self._pdf_worker = None

    def _update_print_enabled(self):
        enabled = bool(self.procedure_id)
        for button in self._print_buttons:
            button.setEnabled(enabled)

    def _recalculate_duration(self):
        start = self.start_edit.dateTime().toPython()
        finish = self.finish_edit.dateTime().toPython()
        minutes = int(max(0, round((finish - start).total_seconds() / 60.0)))
        self.duration_label.setText(str(minutes))

    def _apply_datetime_bounds(self, snapshot: dict):
        admission_dt = self._parse_snapshot_datetime(snapshot.get("admission_datetime"))
        if not admission_dt:
            return
        for edit in self.findChildren(ProcedureDateTimeEdit):
            edit.set_minimum_datetime_from_python(admission_dt)

    @staticmethod
    def _parse_snapshot_datetime(value) -> Optional[datetime]:
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value).replace(" ", "T"))
        except ValueError:
            return None

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: str):
        idx = combo.findData(value or "")
        if idx >= 0:
            combo.setCurrentIndex(idx)
