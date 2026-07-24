import os
from functools import partial
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

from rem_card.app.logger import logger
from rem_card.services.patient_bed_management import PatientBedManagementService
from rem_card.ui.patient_bed_management.bed_labels import is_recovery_bed
from rem_card.ui.patient_bed_management.bed_widget import BedWidget
from rem_card.ui.patient_bed_management.patient_form import PatientForm
from rem_card.ui.patient_bed_management.side_patient_card import SidePatientCard
from rem_card.ui.shared.async_call import AsyncCallThread
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.styles.theme import (
    STYLE_PATIENT_BED_HEADER,
    STYLE_PATIENT_BED_ROOT,
    STYLE_PATIENT_BED_SUBTITLE,
    STYLE_PATIENT_BED_TITLE,
)


try:
    import shiboken6  # type: ignore
except Exception:  # pragma: no cover - optional runtime guard
    shiboken6 = None


NUM_BEDS = int(os.environ.get("REMCARD_NUM_BEDS", "12"))
BED_GRID_COLUMNS = 3
BED_CARD_HEIGHT = 190
BED_GRID_SPACING = 15
HEADER_HEIGHT = 80


def _qt_is_valid(obj) -> bool:
    if obj is None:
        return False
    if shiboken6 is None:
        return True
    try:
        return bool(shiboken6.isValid(obj))
    except Exception:
        return False


def _current_role() -> str:
    return str(os.environ.get("REMCARD_UI_ROLE") or "unknown")


class PatientBedManagementWidget(QWidget):
    def __init__(self, db_manager, data_service=None, parent=None):
        super().__init__(parent)
        self.db_manager = db_manager
        self.patient_bed_service = PatientBedManagementService(db_manager, data_service=data_service)
        self._move_pending = False
        self._is_closing = False
        self._opening_patient_form = False
        self._active_patient_form = None
        self._active_patient_form_context = None
        self._refresh_worker = None
        self._refresh_pending = False
        self._beds_snapshot_by_bed = {}
        self._pending_side_card_update = None

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
            bed_widget = BedWidget(bed_number, "FREE", 0, self)
            bed_widget.clicked.connect(self._on_bed_clicked)
            index = bed_number - 1
            self.grid_layout.addWidget(bed_widget, index // BED_GRID_COLUMNS, index % BED_GRID_COLUMNS)
            self.bed_widgets.append(bed_widget)

    def _on_bed_clicked(self, bed_number: int, current_admission_id: int):
        if self._is_closing:
            return
        if self._update_side_card_from_snapshot(bed_number):
            return
        patient, admission = None, None
        if current_admission_id:
            patient, admission = self.patient_bed_service.get_patient_with_current_admission(bed_number)
        self.side_card.update_info(bed_number, patient, admission)

    def _open_patient_card_by_number(self, bed_number: int):
        if self._is_closing or self._opening_patient_form or _qt_is_valid(self._active_patient_form):
            return
        self._opening_patient_form = True
        QTimer.singleShot(0, lambda bed=int(bed_number): self._open_patient_form_safe(bed))

    def _open_patient_form_safe(self, bed_number: int):
        if self._is_closing:
            self._opening_patient_form = False
            return
        logger.info(
            "patient_form_open_request role=%s bed=%s active_form=%s opening=%s",
            _current_role(),
            int(bed_number),
            int(_qt_is_valid(self._active_patient_form)),
            int(self._opening_patient_form),
        )
        patient, admission = self.patient_bed_service.get_patient_with_current_admission(bed_number)
        admission_id = getattr(admission, "id", None)
        logger.info(
            "patient_form_open_context role=%s bed=%s admission_id=%s has_patient=%s has_admission=%s",
            _current_role(),
            int(bed_number),
            admission_id,
            int(patient is not None),
            int(admission is not None),
        )
        try:
            dialog = PatientForm(self.patient_bed_service, bed_number, patient, admission, self)
            self._active_patient_form = dialog
            self._active_patient_form_context = {
                "bed_number": int(bed_number),
                "admission_id": admission_id,
            }
            logger.info(
                "patient_form_open role=%s bed=%s admission_id=%s",
                _current_role(),
                int(bed_number),
                admission_id,
            )
            dialog.setModal(True)
            dialog.finished.connect(
                partial(
                    self._finish_patient_form_dialog,
                    dialog,
                    bed_number=int(bed_number),
                    expected_admission_id=admission_id,
                )
            )
            dialog.open()
        except Exception as exc:
            logger.exception(
                "patient_form_open_failed role=%s bed=%s admission_id=%s error=%s",
                _current_role(),
                int(bed_number),
                admission_id,
                exc,
            )
            self._active_patient_form = None
            self._active_patient_form_context = None
            raise
        finally:
            self._opening_patient_form = False

    def _finish_patient_form_dialog(self, dialog, result: int, bed_number: int, expected_admission_id):
        if self._active_patient_form is not dialog:
            logger.info(
                "patient_form_finished_skip_stale role=%s bed=%s admission_id=%s result=%s",
                _current_role(),
                int(bed_number),
                expected_admission_id,
                int(result),
            )
            if _qt_is_valid(dialog):
                dialog.deleteLater()
            return
        self._on_patient_form_finished(result, int(bed_number), expected_admission_id)
        if _qt_is_valid(dialog):
            dialog.deleteLater()

    def _on_patient_form_finished(self, result: int, bed_number: int, expected_admission_id):
        if not _qt_is_valid(self):
            return
        logger.info(
            "patient_form_finished role=%s bed=%s admission_id=%s result=%s",
            _current_role(),
            int(bed_number),
            expected_admission_id,
            int(result),
        )
        self._active_patient_form = None
        self._active_patient_form_context = None
        self._opening_patient_form = False
        if self._is_closing or int(result) != int(PatientForm.Accepted):
            return
        QTimer.singleShot(
            0,
            lambda bed=int(bed_number), expected_id=expected_admission_id: self._refresh_after_patient_form(
                bed,
                expected_id,
            ),
        )

    def _refresh_after_patient_form(self, bed_number: int, expected_admission_id):
        if self._is_closing or not _qt_is_valid(self):
            return
        logger.info(
            "patient_form_refresh_start role=%s bed=%s admission_id=%s",
            _current_role(),
            int(bed_number),
            expected_admission_id,
        )
        self._pending_side_card_update = (int(bed_number), expected_admission_id)
        self.refresh_bed_statuses()

    def move_patient(self, source_bed: int, target_bed: int):
        if self._is_closing or self._move_pending:
            return
        source_bed_data = self.patient_bed_service.get_bed_by_number(source_bed)
        target_bed_data = self.patient_bed_service.get_bed_by_number(target_bed)
        recovery_move_error = self._recovery_move_error(source_bed, target_bed, target_bed_data)
        if recovery_move_error:
            CustomMessageBox.warning(
                self,
                "Перенос пациента",
                recovery_move_error,
            )
            return
        if not source_bed_data or source_bed_data["status"] == "FREE":
            return
        if (
            is_recovery_bed(source_bed)
            and target_bed_data
            and target_bed_data["status"] != "FREE"
        ):
            self._merge_recovery_patient(source_bed, target_bed)
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
            self._pending_side_card_update = (int(target_bed), None)
            self.refresh_bed_statuses()

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

    @staticmethod
    def _recovery_move_error(source_bed: int, target_bed: int, target_bed_data) -> str:
        source_is_recovery = is_recovery_bed(source_bed)
        target_is_recovery = is_recovery_bed(target_bed)
        if not source_is_recovery and target_is_recovery:
            return "Пациента с обычной койки нельзя перенести на койку пробуждения."
        return ""

    def _merge_recovery_patient(self, source_bed: int, target_bed: int):
        try:
            preview = self.patient_bed_service.get_recovery_merge_preview(source_bed, target_bed)
        except Exception as exc:
            CustomMessageBox.warning(self, "Слияние пациентов", str(exc))
            self.refresh_bed_statuses()
            return
        action = CustomMessageBox.warning_with_actions(
            self,
            "Слияние пациентов",
            "Койка назначения уже занята. Объединить карту с койки пробуждения "
            "с картой на занятой койке?\n\n"
            "Главной останется карта на обычной койке. Карта с койки пробуждения "
            "будет помечена как слитая, а койка пробуждения освободится.",
            [("Объединить", 1), ("Отмена", 0)],
        )
        if action != 1:
            return
        differences = []
        if not preview.get("history_number_matches"):
            differences.append(
                "номер истории: "
                f"«{preview.get('source_history_number') or 'не указан'}» / "
                f"«{preview.get('target_history_number') or 'не указан'}»"
            )
        if not preview.get("full_name_matches"):
            differences.append(
                "ФИО: "
                f"«{preview.get('source_full_name') or 'не указано'}» / "
                f"«{preview.get('target_full_name') or 'не указано'}»"
            )
        if not preview.get("birth_date_matches"):
            differences.append("дата рождения различается")
        if differences:
            action = CustomMessageBox.warning_with_actions(
                self,
                "Различаются данные пациентов",
                "Защитная проверка обнаружила различия:\n\n"
                + "\n".join(f"• {item}" for item in differences)
                + "\n\nОбъединяйте карты только после проверки пациента.",
                [("Объединить", 1), ("Отмена", 0)],
            )
            if action != 1:
                return

        def operation():
            return self.patient_bed_service.merge_recovery_admission(
                source_bed,
                target_bed,
                expected_source_bed_revision=preview.get("source_bed_revision"),
                expected_target_bed_revision=preview.get("target_bed_revision"),
                expected_source_admission_revision=preview.get("source_admission_revision"),
                expected_target_admission_revision=preview.get("target_admission_revision"),
                allow_identity_mismatch=bool(differences),
            )

        def on_success(_result):
            if self._is_closing:
                return
            self._finish_move_pending()
            self._pending_side_card_update = (int(target_bed), None)
            self.refresh_bed_statuses()

        def on_error(exc):
            if self._is_closing:
                return
            self._finish_move_pending()
            self.refresh_bed_statuses()
            CustomMessageBox.warning(self, "Слияние пациентов", str(exc))

        self._begin_move_pending()
        try:
            self.patient_bed_service.enqueue_write(
                f"patient_bed_merge_recovery:{source_bed}:{target_bed}",
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
        worker = self._refresh_worker
        if worker is not None and worker.isRunning():
            self._refresh_pending = True
            return
        self._refresh_pending = False
        logger.info("patient_beds_refresh_start role=%s", _current_role())
        worker = AsyncCallThread(self._load_bed_status_rows, parent=self)
        self._refresh_worker = worker
        worker.succeeded.connect(self._apply_bed_status_rows)
        worker.failed.connect(self._on_bed_status_refresh_failed)
        worker.finished.connect(lambda: self._on_bed_status_refresh_finished(worker))
        worker.start()

    def _load_bed_status_rows(self):
        return self.patient_bed_service.get_beds_snapshot()

    def _apply_bed_status_rows(self, rows):
        if self._is_closing:
            return
        rows = list(rows or [])
        by_bed = {int(row["bed_number"]): row for row in rows}
        self._beds_snapshot_by_bed = by_bed

        for bed_widget in self.bed_widgets:
            bed_data = by_bed.get(int(bed_widget.bed_number))
            if not bed_data:
                bed_widget.set_status("FREE", 0)
                bed_widget.set_patient_info("")
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

        pending_side_update = self._pending_side_card_update
        self._pending_side_card_update = None
        if pending_side_update:
            bed_number, expected_admission_id = pending_side_update
            pending_row = by_bed.get(int(bed_number))
            if pending_row is None:
                self.side_card.update_info(int(bed_number), None, None)
            else:
                self._update_side_card_from_snapshot(bed_number, expected_admission_id=expected_admission_id)
            current_id = self._row_admission_id(pending_row)
            logger.info(
                "patient_form_refresh_end role=%s bed=%s admission_id=%s current_admission_id=%s",
                _current_role(),
                int(bed_number),
                expected_admission_id,
                current_id,
            )
        elif self.bed_widgets:
            current_bed = getattr(self.side_card, "current_bed_number", None)
            target_bed = int(current_bed) if current_bed else int(self.bed_widgets[0].bed_number)
            if by_bed.get(target_bed) is None:
                self.side_card.update_info(target_bed, None, None)
            else:
                self._update_side_card_from_snapshot(target_bed)

        logger.info("patient_beds_refresh_end role=%s rows=%s", _current_role(), len(rows))

    def _on_bed_status_refresh_failed(self, exc):
        if self._is_closing:
            return
        logger.warning("patient_beds_refresh_failed role=%s error=%s", _current_role(), exc, exc_info=True)

    def _on_bed_status_refresh_finished(self, worker):
        if self._refresh_worker is worker:
            self._refresh_worker = None
        if self._is_closing:
            self._refresh_pending = False
            return
        if self._refresh_pending:
            QTimer.singleShot(0, self.refresh_bed_statuses)

    @staticmethod
    def _row_admission_id(row):
        if not row:
            return None
        try:
            value = row["current_admission_id"]
        except Exception:
            value = None
        return int(value) if value is not None else None

    def _update_side_card_from_snapshot(self, bed_number: int, *, expected_admission_id=None) -> bool:
        row = self._beds_snapshot_by_bed.get(int(bed_number))
        if row is None:
            return False
        current_admission_id = self._row_admission_id(row)
        if (
            expected_admission_id is not None
            and current_admission_id is not None
            and int(expected_admission_id) != int(current_admission_id)
        ):
            return False
        builder = getattr(self.patient_bed_service, "records_from_bed_snapshot_row", None)
        if callable(builder):
            patient, admission = builder(row)
        elif current_admission_id:
            patient, admission = self.patient_bed_service.get_patient_with_current_admission(int(bed_number))
        else:
            patient, admission = None, None
        self.side_card.update_info(int(bed_number), patient, admission)
        return True

    def shutdown(self):
        self._is_closing = True
        self._opening_patient_form = False
        dialog = self._active_patient_form
        self._active_patient_form = None
        self._active_patient_form_context = None
        worker = self._refresh_worker
        self._refresh_worker = None
        self._refresh_pending = False
        if worker is not None and worker.isRunning():
            worker.quit()
            worker.wait(1200)
        if _qt_is_valid(dialog):
            try:
                if hasattr(dialog, "force_close_for_shutdown"):
                    dialog.force_close_for_shutdown()
                else:
                    dialog.reject()
            except Exception as exc:
                logger.warning("patient_form_shutdown_reject_failed role=%s error=%s", _current_role(), exc)

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)
