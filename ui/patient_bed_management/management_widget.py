import os
from math import ceil

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from rem_card.services.patient_bed_management import PatientBedManagementService
from rem_card.ui.patient_bed_management.bed_widget import BedWidget
from rem_card.ui.patient_bed_management.patient_form import PatientForm
from rem_card.ui.patient_bed_management.side_patient_card import SidePatientCard
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.styles.theme import (
    STYLE_PATIENT_BED_HEADER,
    STYLE_PATIENT_BED_ROOT,
    STYLE_PATIENT_BED_SUBTITLE,
    STYLE_PATIENT_BED_TITLE,
)


NUM_BEDS = int(os.environ.get("REMCARD_NUM_BEDS", "12"))
BED_GRID_COLUMNS = 3
BED_CARD_HEIGHT = 190
BED_GRID_SPACING = 15
HEADER_HEIGHT = 80


class PatientBedManagementWidget(QWidget):
    def __init__(self, db_manager, data_service=None, parent=None):
        super().__init__(parent)
        self.db_manager = db_manager
        self.patient_bed_service = PatientBedManagementService(db_manager, data_service=data_service)
        self._move_pending = False
        self._is_closing = False
        self._opening_patient_form = False

        self.bed_widgets = []
        self._init_ui()
        QTimer.singleShot(0, self.refresh_bed_statuses)

    def _init_ui(self):
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 5, 0, 0)
        root_layout.setSpacing(0)

        self.root_container = QWidget()
        self.root_container.setObjectName("patient_bed_root")
        self.root_container.setStyleSheet(STYLE_PATIENT_BED_ROOT)
        root_layout.addWidget(self.root_container)

        main_layout = QVBoxLayout(self.root_container)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        main_layout.addStretch(1)

        self.content_container = QWidget()
        content_layout = QHBoxLayout(self.content_container)
        content_layout.setContentsMargins(12, 12, 12, 12)
        content_layout.setSpacing(15)
        main_layout.addWidget(self.content_container, 0, Qt.AlignCenter)
        main_layout.addStretch(1)

        self.left_column = QWidget()
        self.left_column.setFixedWidth(780)
        left_layout = QVBoxLayout(self.left_column)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(15)
        content_layout.addWidget(self.left_column, 0, Qt.AlignTop)

        header_card = QFrame()
        header_card.setObjectName("patient_bed_header")
        header_card.setFixedHeight(80)
        header_card.setFixedWidth(250 * 3 + 15 * 2)
        header_card.setStyleSheet(STYLE_PATIENT_BED_HEADER)
        header_layout = QVBoxLayout(header_card)
        header_layout.setContentsMargins(15, 10, 15, 10)
        header_layout.setSpacing(2)

        title = QLabel("УПРАВЛЕНИЕ ПАЦИЕНТАМИ")
        title.setStyleSheet(STYLE_PATIENT_BED_TITLE)
        title.setAlignment(Qt.AlignCenter)
        subtitle = QLabel("ОАР №3 г. Амурск")
        subtitle.setStyleSheet(STYLE_PATIENT_BED_SUBTITLE)
        subtitle.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(title)
        header_layout.addWidget(subtitle)
        left_layout.addWidget(header_card, 0, Qt.AlignLeft)

        self.grid_container = QWidget()
        self.grid_layout = QGridLayout(self.grid_container)
        self.grid_layout.setSpacing(15)
        self.grid_layout.setContentsMargins(0, 0, 0, 0)
        self.grid_layout.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        left_layout.addWidget(self.grid_container)
        left_layout.addStretch()

        self.side_card = SidePatientCard()
        self.side_card.setFixedHeight(self._side_card_height())
        self.side_card.open_card_clicked.connect(self._open_patient_card_by_number)
        content_layout.addWidget(self.side_card, 0, Qt.AlignTop)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setColor(QColor(0, 0, 0, 20))
        shadow.setOffset(0, 4)
        self.root_container.setGraphicsEffect(shadow)

        self._init_bed_widgets()

    @staticmethod
    def _side_card_height() -> int:
        bed_rows = max(1, ceil(NUM_BEDS / BED_GRID_COLUMNS))
        grid_height = bed_rows * BED_CARD_HEIGHT + max(0, bed_rows - 1) * BED_GRID_SPACING
        return HEADER_HEIGHT + BED_GRID_SPACING + grid_height

    def _init_bed_widgets(self):
        for bed_number in range(1, NUM_BEDS + 1):
            bed_widget = BedWidget(bed_number, "FREE", None, self)
            bed_widget.clicked.connect(self._on_bed_clicked)
            index = bed_number - 1
            self.grid_layout.addWidget(bed_widget, index // BED_GRID_COLUMNS, index % BED_GRID_COLUMNS)
            self.bed_widgets.append(bed_widget)

    def _on_bed_clicked(self, bed_number: int, current_admission_id: int):
        if self._is_closing:
            return
        patient, admission = None, None
        if current_admission_id:
            patient, admission = self.patient_bed_service.get_patient_with_current_admission(bed_number)
        self.side_card.update_info(bed_number, patient, admission)

    def _open_patient_card_by_number(self, bed_number: int):
        if self._is_closing or self._opening_patient_form:
            return
        self._opening_patient_form = True
        QTimer.singleShot(0, lambda bed=int(bed_number): self._open_patient_form_safe(bed))

    def _open_patient_form_safe(self, bed_number: int):
        if self._is_closing:
            self._opening_patient_form = False
            return
        patient, admission = self.patient_bed_service.get_patient_with_current_admission(bed_number)
        admission_id = getattr(admission, "id", None)
        try:
            dialog = PatientForm(self.patient_bed_service, bed_number, patient, admission, self)
            if dialog.exec():
                if self._is_closing:
                    return
                new_patient, new_admission = self.patient_bed_service.get_patient_with_current_admission(bed_number)
                new_admission_id = getattr(new_admission, "id", None)
                if admission_id is not None and new_admission_id is not None and admission_id != new_admission_id:
                    return
                self.refresh_bed_statuses()
                self.side_card.update_info(bed_number, new_patient, new_admission)
        finally:
            self._opening_patient_form = False

    def move_patient(self, source_bed: int, target_bed: int):
        if self._is_closing or self._move_pending:
            return
        source_bed_data = self.patient_bed_service.get_bed_by_number(source_bed)
        target_bed_data = self.patient_bed_service.get_bed_by_number(target_bed)
        if not source_bed_data or source_bed_data["status"] == "FREE":
            return
        _source_patient, source_admission = self.patient_bed_service.get_patient_with_current_admission(source_bed)
        _target_patient, target_admission = (
            self.patient_bed_service.get_patient_with_current_admission(target_bed)
            if target_bed_data and target_bed_data["status"] != "FREE"
            else (None, None)
        )
        expected_source_bed_revision = int(source_bed_data["revision"] if "revision" in source_bed_data.keys() else 0)
        expected_target_bed_revision = int(target_bed_data["revision"] if target_bed_data and "revision" in target_bed_data.keys() else 0)
        expected_source_admission_revision = int(getattr(source_admission, "revision", 0) or 0) if source_admission else None
        expected_target_admission_revision = int(getattr(target_admission, "revision", 0) or 0) if target_admission else None

        message = f"Переместить пациента с койки {source_bed} на койку {target_bed}?"
        if target_bed_data and target_bed_data["status"] != "FREE":
            message = f"Койка {target_bed} занята. Поменять пациентов местами?"

        reply = CustomMessageBox.question(
            self,
            "Перенос пациента",
            message,
            CustomMessageBox.Yes | CustomMessageBox.No,
            CustomMessageBox.No,
        )
        if reply != CustomMessageBox.Yes:
            return

        def operation():
            return self.patient_bed_service.move_patient(
                source_bed,
                target_bed,
                expected_source_bed_revision=expected_source_bed_revision,
                expected_target_bed_revision=expected_target_bed_revision,
                expected_source_admission_revision=expected_source_admission_revision,
                expected_target_admission_revision=expected_target_admission_revision,
            )

        def on_success(_result):
            if self._is_closing:
                return
            self._finish_move_pending()
            if not _result:
                self.refresh_bed_statuses()
                CustomMessageBox.warning(self, "Ошибка", "Перенос не выполнен: исходная койка уже изменилась.")
                return
            self.refresh_bed_statuses()
            patient, admission = self.patient_bed_service.get_patient_with_current_admission(target_bed)
            self.side_card.update_info(target_bed, patient, admission)

        def on_error(exc):
            if self._is_closing:
                return
            self._finish_move_pending()
            self.refresh_bed_statuses()
            CustomMessageBox.warning(self, "Ошибка", str(exc))

        self._begin_move_pending()
        try:
            self.patient_bed_service.enqueue_write(
                f"patient_bed_move:{source_bed}:{target_bed}",
                operation,
                on_success=on_success,
                on_error=on_error,
            )
        except Exception as exc:
            on_error(exc)

    def _begin_move_pending(self):
        self._move_pending = True
        for bed_widget in self.bed_widgets:
            bed_widget.setEnabled(False)

    def _finish_move_pending(self):
        self._move_pending = False
        for bed_widget in self.bed_widgets:
            bed_widget.setEnabled(True)

    def refresh_bed_statuses(self):
        if self._is_closing:
            return
        rows = self.patient_bed_service.get_beds_snapshot()
        by_bed = {int(row["bed_number"]): row for row in rows}

        for bed_widget in self.bed_widgets:
            bed_data = by_bed.get(int(bed_widget.bed_number))
            if not bed_data:
                continue
            admission_id = bed_data["current_admission_id"] if bed_data["current_admission_id"] is not None else 0
            bed_widget.set_status(bed_data["status"], admission_id)
            if bed_data["current_admission_id"]:
                bed_widget.set_patient_info(
                    str(bed_data["full_name"] or ""),
                    str(bed_data["history_number"] or ""),
                    str(bed_data["diagnosis_text"] or ""),
                )
            else:
                bed_widget.set_patient_info("")

        if self.bed_widgets:
            selected = self.bed_widgets[0]
            self._on_bed_clicked(selected.bed_number, selected.current_admission_id)

    def shutdown(self):
        self._is_closing = True
        self._opening_patient_form = False

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)
