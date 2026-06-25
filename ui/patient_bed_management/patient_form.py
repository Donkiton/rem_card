from datetime import datetime
import hashlib
import os
import weakref

from PySide6.QtCore import QSize, QTimer, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from rem_card.app.logger import logger
from rem_card.app.patient_age import storage_age_from_birth_date
from rem_card.services.mkb import MKBService
from rem_card.services.patient_bed_management import AdmissionRecord, PatientBedManagementService, PatientRecord
from rem_card.ui.patient_bed_management.bed_labels import format_patient_bed_label
from rem_card.ui.patient_bed_management.form_widgets import IconBadge, line_icon
from rem_card.ui.patient_bed_management.tabs.diagnosis_tab import DiagnosisTabWidget
from rem_card.ui.patient_bed_management.tabs.general_tab import GeneralTabWidget
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.shared.window_state import SavedFramelessDialogMixin
from rem_card.ui.styles.theme import (
    STYLE_PATIENT_FORM_BED_BADGE,
    STYLE_PATIENT_FORM_CARD,
    STYLE_PATIENT_FORM_CANCEL_BUTTON,
    STYLE_PATIENT_FORM_CONTAINER,
    STYLE_PATIENT_FORM_PAGE,
    STYLE_PATIENT_FORM_SAVE_BUTTON,
    STYLE_PATIENT_FORM_TAB,
    STYLE_PATIENT_FORM_TITLE,
    STYLE_TRANSPARENT_WIDGET,
)


try:
    import shiboken6  # type: ignore
except Exception:  # pragma: no cover - optional runtime guard
    shiboken6 = None


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


def _short_hash(value) -> str:
    text = str(value or "").strip().casefold()
    if not text:
        return "-"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]


def _days_from_today(value) -> int | None:
    if not value:
        return None
    try:
        return (value.date() - datetime.now().date()).days
    except Exception:
        return None


def _format_log_fields(fields: dict) -> str:
    parts = []
    for key, value in fields.items():
        if value is None:
            value = "none"
        elif value is True:
            value = 1
        elif value is False:
            value = 0
        parts.append(f"{key}={value}")
    return " ".join(parts)


def _invoke_form_later(form_ref, method_name: str, *args):
    form = form_ref()
    if not _qt_is_valid(form):
        return
    if getattr(form, "_closing", False):
        return
    method = getattr(form, method_name, None)
    if method is not None:
        method(*args)


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
        self._closing = False
        self._mkb_closed = False
        self._write_description = ""
        self._form_log_id = f"{os.getpid()}:{id(self):x}"
        self._reject_reason = "unknown"
        self.bed_label = format_patient_bed_label(self.bed_number)
        self.dialog_title_text = "Новая карточка пациента" if self.is_new_admission else "Карточка пациента"

        self.setWindowTitle(f"{self.dialog_title_text} - {self.bed_label}")
        self.setMinimumSize(1106, 506)
        self.resize(1106, 506)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setMouseTracking(True)
        self._init_saved_frameless_dialog(
            "patient_bed_management/patient_form_geometry",
            resize_margin=10,
            drag_area_height=60,
        )

        self._init_ui()
        self._restore_saved_geometry()
        self._load_data()
        logger.info(
            "patient_form_ready role=%s form_id=%s bed=%s admission_id=%s is_new=%s state=%s",
            _current_role(),
            self._form_log_id,
            self.bed_number,
            getattr(self.admission, "id", None),
            int(self.is_new_admission),
            self._format_form_state(),
        )

    def _init_ui(self):
        self.bg_container = QWidget(self)
        self.bg_container.setObjectName("bg_container")
        self.bg_container.setMouseTracking(True)
        self.bg_container.setStyleSheet(STYLE_PATIENT_FORM_CONTAINER)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(16)
        shadow.setColor(QColor(15, 23, 42, 45))
        shadow.setOffset(0, 4)
        self.bg_container.setGraphicsEffect(shadow)

        self.layout_container = QVBoxLayout(self)
        self.layout_container.setContentsMargins(3, 3, 3, 10)
        self.layout_container.addWidget(self.bg_container)

        self.main_layout = QVBoxLayout(self.bg_container)
        self.main_layout.setContentsMargins(16, 0, 25, 12)
        self.main_layout.setSpacing(0)

        self._init_header()
        self._init_form()
        self._init_buttons()

    def _init_header(self):
        self.header_panel = QWidget()
        self.header_panel.setFixedHeight(60)
        self.header_panel.setStyleSheet(STYLE_TRANSPARENT_WIDGET)
        header_layout = QHBoxLayout(self.header_panel)
        header_layout.setContentsMargins(3, 0, 0, 0)
        header_layout.setSpacing(13)

        header_layout.addWidget(IconBadge("folder", "#1d73f8", "#eaf3ff", side=33, icon_size=20))

        title_label = QLabel(self.dialog_title_text)
        title_label.setStyleSheet(STYLE_PATIENT_FORM_TITLE)
        header_layout.addWidget(title_label)

        bed_badge = QLabel(self.bed_label)
        bed_badge.setStyleSheet(STYLE_PATIENT_FORM_BED_BADGE)
        bed_badge.setFixedHeight(25)
        header_layout.addWidget(bed_badge)
        header_layout.addStretch()

        close_button = QPushButton("×")
        self.close_button = close_button
        close_button.setFixedSize(36, 36)
        close_button.setCursor(Qt.PointingHandCursor)
        close_button.setStyleSheet(
            """
            QPushButton {
                background: transparent;
                color: #17233f;
                border: none;
                border-radius: 5px;
                font-size: 31px;
                font-weight: 300;
                padding: 0px 0px 3px 0px;
                min-width: 0px;
            }
            QPushButton:hover {
                background: #f1f5f9;
                color: #0f172a;
            }
            """
        )
        close_button.clicked.connect(lambda checked=False: self._request_reject("close_button"))
        header_layout.addWidget(close_button)
        self.main_layout.addWidget(self.header_panel)

    def _init_form(self):
        self.general_tab = GeneralTabWidget(self)
        self.diagnosis_tab = DiagnosisTabWidget(self.mkb_service, self, show_operations=False)

        self.general_tab.setStyleSheet(STYLE_PATIENT_FORM_TAB)
        self.diagnosis_tab.setStyleSheet(STYLE_PATIENT_FORM_TAB)
        label_column_width = 208
        self.general_tab.set_label_column_width(label_column_width)
        self.diagnosis_tab.set_label_column_width(label_column_width)

        self.form_page = QWidget()
        self.form_page.setStyleSheet(STYLE_PATIENT_FORM_PAGE)
        self.form_page.setMinimumHeight(356)
        self.form_page.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        form_page_layout = QHBoxLayout(self.form_page)
        form_page_layout.setContentsMargins(0, 0, 0, 0)
        form_page_layout.setSpacing(13)

        form_page_layout.addWidget(
            self._build_section_card("ОБЩИЕ ДАННЫЕ", "user", "#1e6ff2", "#eef6ff", self.general_tab),
            1,
        )
        form_page_layout.addWidget(
            self._build_section_card("ДИАГНОЗ", "diagnosis", "#168f86", "#ecfdfb", self.diagnosis_tab),
            1,
        )

        self.main_layout.addWidget(self.form_page, 1)

    def _build_section_card(self, title: str, icon_kind: str, accent: str, icon_bg: str, content: QWidget) -> QFrame:
        card = QFrame()
        card.setObjectName("patientFormCard")
        card.setMinimumHeight(356)
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        card.setStyleSheet(STYLE_PATIENT_FORM_CARD)

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 16, 18, 13)
        card_layout.setSpacing(9)

        title_row = QWidget()
        title_row.setStyleSheet(STYLE_TRANSPARENT_WIDGET)
        title_layout = QHBoxLayout(title_row)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(9)
        title_layout.addWidget(IconBadge(icon_kind, accent, icon_bg, side=21, icon_size=16))

        title_label = QLabel(title)
        title_label.setStyleSheet(
            f"color: {accent}; font-size: 13px; font-weight: 900; background: transparent; border: none;"
        )
        title_layout.addWidget(title_label)
        title_layout.addStretch()

        card_layout.addWidget(title_row)
        card_layout.addWidget(content, 1)
        return card

    def _init_buttons(self):
        buttons_layout = QHBoxLayout()
        buttons_layout.setContentsMargins(0, 16, 0, 0)
        buttons_layout.setSpacing(13)

        self.cancel_button = QPushButton("Отменить")
        self.cancel_button.setCursor(Qt.PointingHandCursor)
        self.cancel_button.setFixedHeight(38)
        self.cancel_button.setIcon(line_icon("x", "#475569", 17))
        self.cancel_button.setIconSize(QSize(17, 17))
        self.cancel_button.setStyleSheet(STYLE_PATIENT_FORM_CANCEL_BUTTON)
        self.cancel_button.clicked.connect(lambda checked=False: self._request_reject("cancel_button"))

        self.save_button = QPushButton("Сохранить карточку")
        self.save_button.setCursor(Qt.PointingHandCursor)
        self.save_button.setFixedHeight(38)
        self.save_button.setIcon(line_icon("save", "#ffffff", 17))
        self.save_button.setIconSize(QSize(17, 17))
        self.save_button.setStyleSheet(STYLE_PATIENT_FORM_SAVE_BUTTON)
        self.save_button.clicked.connect(self._save_data)

        buttons_layout.addWidget(self.cancel_button, 1)
        buttons_layout.addWidget(self.save_button, 1)
        self.main_layout.addLayout(buttons_layout)

    def _load_data(self):
        if not self.is_new_admission:
            self.general_tab.set_data(self.patient, self.admission)
            if self.admission:
                self.diagnosis_tab.set_data(self.admission, [])

    def _request_reject(self, reason: str):
        self._reject_reason = str(reason or "unknown")
        self.reject()

    def _collect_form_state(self, gen_data=None, diag_data=None) -> dict:
        try:
            gen = gen_data if gen_data is not None else self.general_tab.get_data()
        except Exception as exc:
            logger.warning(
                "patient_form_state_general_failed role=%s form_id=%s bed=%s admission_id=%s error=%s",
                _current_role(),
                getattr(self, "_form_log_id", "-"),
                getattr(self, "bed_number", None),
                getattr(getattr(self, "admission", None), "id", None),
                exc,
            )
            gen = {}

        try:
            diag = diag_data if diag_data is not None else self.diagnosis_tab.get_data()
        except Exception as exc:
            logger.warning(
                "patient_form_state_diagnosis_failed role=%s form_id=%s bed=%s admission_id=%s error=%s",
                _current_role(),
                getattr(self, "_form_log_id", "-"),
                getattr(self, "bed_number", None),
                getattr(getattr(self, "admission", None), "id", None),
                exc,
            )
            diag = {}

        history_number = str(gen.get("history_number") or "")
        full_name = str(gen.get("full_name") or "")
        birth_text = str(gen.get("birth_date_text") or "")
        diagnosis_code = str(diag.get("diagnosis_code") or "")
        diagnosis_text = str(diag.get("diagnosis_text") or "")
        department_profile = str(gen.get("department_profile") or "")
        source_department = str(gen.get("source_department") or "")
        admission_datetime = gen.get("admission_datetime")

        return {
            "history_len": len(history_number),
            "history_hash": _short_hash(history_number),
            "full_name_len": len(full_name),
            "full_name_hash": _short_hash(full_name),
            "birth_text_len": len(birth_text),
            "birth_valid": bool(gen.get("birth_date")),
            "gender_set": bool(gen.get("gender")),
            "admission_offset_days": _days_from_today(admission_datetime),
            "diagnosis_code_len": len(diagnosis_code),
            "diagnosis_text_len": len(diagnosis_text),
            "department_profile_len": len(department_profile),
            "source_department_len": len(source_department),
        }

    def _format_form_state(self, gen_data=None, diag_data=None) -> str:
        return _format_log_fields(self._collect_form_state(gen_data, diag_data))

    def _log_validation_failed(self, reason: str, gen_data: dict, diag_data: dict):
        logger.info(
            "patient_form_validation_failed role=%s form_id=%s bed=%s admission_id=%s reason=%s state=%s",
            _current_role(),
            self._form_log_id,
            self.bed_number,
            getattr(self.admission, "id", None),
            reason,
            self._format_form_state(gen_data, diag_data),
        )

    def _validate_input(self) -> bool:
        gen = self.general_tab.get_data()
        diag = self.diagnosis_tab.get_data()
        if not gen["history_number"] or not gen["full_name"]:
            self._log_validation_failed("missing_history_or_full_name", gen, diag)
            CustomMessageBox.warning(self, "Ошибка", "Заполните номер ИБ и ФИО пациента")
            return False
        if not gen.get("birth_date"):
            self._log_validation_failed("missing_or_invalid_birth_date", gen, diag)
            message = "Укажите корректную дату рождения" if gen.get("birth_date_text") else "Укажите дату рождения пациента"
            CustomMessageBox.warning(self, "Ошибка", message)
            return False
        admission_datetime = gen.get("admission_datetime")
        if admission_datetime and gen["birth_date"] > admission_datetime.date():
            self._log_validation_failed("birth_date_after_admission", gen, diag)
            CustomMessageBox.warning(self, "Ошибка", "Дата рождения не может быть позже даты поступления")
            return False
        if not diag["diagnosis_text"]:
            self._log_validation_failed("missing_diagnosis_text", gen, diag)
            CustomMessageBox.warning(self, "Ошибка", "Необходимо указать диагноз")
            return False
        return True

    def _save_data(self):
        logger.info(
            "patient_form_save_clicked role=%s form_id=%s bed=%s admission_id=%s pending=%s state=%s",
            _current_role(),
            self._form_log_id,
            self.bed_number,
            getattr(self.admission, "id", None),
            int(self._write_pending),
            self._format_form_state(),
        )
        if self._write_pending:
            logger.info(
                "patient_form_save_ignored_pending role=%s form_id=%s bed=%s admission_id=%s op=%s",
                _current_role(),
                self._form_log_id,
                self.bed_number,
                getattr(self.admission, "id", None),
                self._write_description,
            )
            return
        if not self._validate_input():
            return
        try:
            gen_data = self.general_tab.get_data()
            if not self._confirm_non_today_admission_date(gen_data["admission_datetime"]):
                logger.info(
                    "patient_form_save_cancelled_by_admission_date role=%s form_id=%s bed=%s admission_id=%s state=%s",
                    _current_role(),
                    self._form_log_id,
                    self.bed_number,
                    getattr(self.admission, "id", None),
                    self._format_form_state(),
                )
                return
            diag_data = self.diagnosis_tab.get_data()
            age_data = storage_age_from_birth_date(gen_data["birth_date"], gen_data["admission_datetime"])
            patient_data = {
                "full_name": gen_data["full_name"],
                "birth_date": gen_data["birth_date"],
            }
            admission_data = {
                "bed_number": self.bed_number,
                "history_number": gen_data["history_number"],
                "admission_datetime": gen_data["admission_datetime"],
                "patient_age": age_data["patient_age"],
                "patient_months": age_data["patient_months"],
                "patient_age_unit": age_data["patient_age_unit"],
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
                    result = self.patient_bed_service.create_patient_and_admission(patient_data, admission_data)
                    logger.info(
                        "patient_form_service_result role=%s form_id=%s bed=%s op=%s result=%s",
                        _current_role(),
                        self._form_log_id,
                        self.bed_number,
                        description,
                        result,
                    )
                    return result
            else:
                patient_id = int(self.patient.id)
                admission_id = int(self.admission.id)
                expected_admission_revision = int(getattr(self.admission, "revision", 0) or 0)
                description = f"patient_bed_update_admission:{admission_id}"

                def operation():
                    result = self.patient_bed_service.update_patient_and_admission(
                        patient_id,
                        admission_id,
                        patient_data,
                        admission_data,
                        expected_admission_revision=expected_admission_revision,
                    )
                    logger.info(
                        "patient_form_service_result role=%s form_id=%s bed=%s admission_id=%s op=%s result=%s expected_revision=%s",
                        _current_role(),
                        self._form_log_id,
                        self.bed_number,
                        admission_id,
                        description,
                        result,
                        expected_admission_revision,
                    )
                    return result

            self._begin_write_pending()
            self._write_description = description
            logger.info(
                "patient_form_write_start role=%s form_id=%s bed=%s admission_id=%s op=%s state=%s",
                _current_role(),
                self._form_log_id,
                self.bed_number,
                getattr(self.admission, "id", None),
                description,
                self._format_form_state(gen_data, diag_data),
            )
            self.patient_bed_service.enqueue_write(
                description,
                operation,
                on_success=self._make_write_success_callback(description),
                on_error=self._make_write_error_callback(description),
            )
        except Exception as exc:
            self._finish_write_pending()
            logger.exception(
                "patient_form_save_exception role=%s form_id=%s bed=%s admission_id=%s op=%s state=%s error=%s",
                _current_role(),
                self._form_log_id,
                self.bed_number,
                getattr(self.admission, "id", None),
                self._write_description,
                self._format_form_state(),
                exc,
            )
            CustomMessageBox.warning(self, "Ошибка", f"Не удалось сохранить данные:\n{exc}")

    def _confirm_non_today_admission_date(self, admission_datetime: datetime) -> bool:
        if not admission_datetime:
            return True
        admission_date = admission_datetime.date()
        today = datetime.now().date()
        if admission_date == today:
            return True

        action_text = "создание" if self.is_new_admission else "редактирование"
        continue_text = "Продолжить создание" if self.is_new_admission else "Продолжить редактирование"
        logger.info(
            "patient_form_admission_date_confirmation_show role=%s form_id=%s bed=%s admission_id=%s offset_days=%s",
            _current_role(),
            getattr(self, "_form_log_id", "-"),
            getattr(self, "bed_number", None),
            getattr(getattr(self, "admission", None), "id", None),
            _days_from_today(admission_datetime),
        )
        message = (
            f"Дата поступления {admission_date.strftime('%d.%m.%Y')} отличается от сегодняшней "
            f"даты {today.strftime('%d.%m.%Y')}.\n\n"
            f"Продолжить {action_text} карточки с указанной датой или изменить дату?"
        )
        result = CustomMessageBox.warning_with_actions(
            self,
            "Проверка даты поступления",
            message,
            [
                (continue_text, CustomMessageBox.Yes),
                ("Изменить дату", CustomMessageBox.No),
            ],
        )
        confirmed = result == CustomMessageBox.Yes
        logger.info(
            "patient_form_admission_date_confirmation_result role=%s form_id=%s bed=%s admission_id=%s confirmed=%s",
            _current_role(),
            getattr(self, "_form_log_id", "-"),
            getattr(self, "bed_number", None),
            getattr(getattr(self, "admission", None), "id", None),
            int(confirmed),
        )
        return confirmed

    def _make_write_success_callback(self, description: str):
        form_ref = weakref.ref(self)

        def _callback(_result):
            form = form_ref()
            if not _qt_is_valid(form) or getattr(form, "_closing", False):
                logger.info(
                    "patient_form_write_success_skip role=%s op=%s result=%s reason=invalid_or_closing",
                    _current_role(),
                    description,
                    _result,
                )
                return
            logger.info(
                "patient_form_write_success_callback role=%s form_id=%s bed=%s admission_id=%s op=%s result=%s",
                _current_role(),
                form._form_log_id,
                form.bed_number,
                getattr(form.admission, "id", None),
                description,
                _result,
            )
            QTimer.singleShot(0, lambda ref=form_ref, result=_result: _invoke_form_later(ref, "_on_write_success", description, result))

        return _callback

    def _make_write_error_callback(self, description: str):
        form_ref = weakref.ref(self)

        def _callback(exc):
            form = form_ref()
            if not _qt_is_valid(form) or getattr(form, "_closing", False):
                logger.warning(
                    "patient_form_write_error_skip role=%s op=%s error=%s",
                    _current_role(),
                    description,
                    exc,
                )
                return
            exc_info = (type(exc), exc, exc.__traceback__) if getattr(exc, "__traceback__", None) else None
            logger.warning(
                "patient_form_write_error_callback role=%s form_id=%s bed=%s admission_id=%s op=%s error=%s state=%s",
                _current_role(),
                form._form_log_id,
                form.bed_number,
                getattr(form.admission, "id", None),
                description,
                exc,
                form._format_form_state(),
                exc_info=exc_info,
            )
            QTimer.singleShot(
                0,
                lambda ref=form_ref, err=exc: _invoke_form_later(ref, "_on_write_error", err, description),
            )

        return _callback

    def _begin_write_pending(self):
        self._write_pending = True
        self._set_write_controls_enabled(False)
        if _qt_is_valid(self.save_button):
            self.save_button.setText("Сохранение...")

    def _finish_write_pending(self):
        self._write_pending = False
        self._set_write_controls_enabled(True)
        if _qt_is_valid(self.save_button):
            self.save_button.setText("Сохранить карточку")

    def _set_write_controls_enabled(self, enabled: bool):
        for widget in (
            getattr(self, "form_page", None),
            getattr(self, "cancel_button", None),
            getattr(self, "save_button", None),
            getattr(self, "close_button", None),
        ):
            if _qt_is_valid(widget):
                widget.setEnabled(enabled)

    def _on_write_success(self, description: str = "", result=None):
        if self._closing or not _qt_is_valid(self):
            return
        logger.info(
            "patient_form_write_success role=%s form_id=%s bed=%s admission_id=%s op=%s result=%s",
            _current_role(),
            self._form_log_id,
            self.bed_number,
            getattr(self.admission, "id", None),
            description or self._write_description,
            result,
        )
        self._finish_write_pending()
        self.accept()

    def _on_write_error(self, exc, description: str = ""):
        if self._closing or not _qt_is_valid(self):
            return
        exc_info = (type(exc), exc, exc.__traceback__) if getattr(exc, "__traceback__", None) else None
        logger.warning(
            "patient_form_write_error role=%s form_id=%s bed=%s admission_id=%s op=%s error=%s state=%s",
            _current_role(),
            self._form_log_id,
            self.bed_number,
            getattr(self.admission, "id", None),
            description or self._write_description,
            exc,
            self._format_form_state(),
            exc_info=exc_info,
        )
        self._finish_write_pending()
        CustomMessageBox.warning(self, "Ошибка", f"Не удалось сохранить данные:\n{exc}")

    def _close_mkb_service_once(self):
        if self._mkb_closed:
            return
        self._mkb_closed = True
        try:
            self.mkb_service.close_connection()
        except Exception as exc:
            logger.warning(
                "patient_form_mkb_close_failed role=%s bed=%s admission_id=%s error=%s",
                _current_role(),
                self.bed_number,
                getattr(self.admission, "id", None),
                exc,
            )

    def reject(self):
        if self._write_pending and not self._closing:
            logger.info(
                "patient_form_reject_ignored_pending_write role=%s form_id=%s bed=%s admission_id=%s reason=%s op=%s",
                _current_role(),
                self._form_log_id,
                self.bed_number,
                getattr(self.admission, "id", None),
                self._reject_reason,
                self._write_description,
            )
            return
        if self._closing:
            return
        self._closing = True
        logger.info(
            "patient_form_reject role=%s form_id=%s bed=%s admission_id=%s reason=%s state=%s",
            _current_role(),
            self._form_log_id,
            self.bed_number,
            getattr(self.admission, "id", None),
            self._reject_reason,
            self._format_form_state(),
        )
        self._close_mkb_service_once()
        super().reject()

    def force_close_for_shutdown(self):
        if self._closing:
            return
        self._closing = True
        logger.info(
            "patient_form_force_close role=%s form_id=%s bed=%s admission_id=%s state=%s",
            _current_role(),
            self._form_log_id,
            self.bed_number,
            getattr(self.admission, "id", None),
            self._format_form_state(),
        )
        self._close_mkb_service_once()
        super().reject()

    def accept(self):
        if self._closing:
            return
        self._closing = True
        logger.info(
            "patient_form_accept role=%s form_id=%s bed=%s admission_id=%s state=%s",
            _current_role(),
            self._form_log_id,
            self.bed_number,
            getattr(self.admission, "id", None),
            self._format_form_state(),
        )
        self._close_mkb_service_once()
        super().accept()

    def closeEvent(self, event):
        logger.info(
            "patient_form_close_event role=%s form_id=%s bed=%s admission_id=%s closing=%s pending=%s",
            _current_role(),
            getattr(self, "_form_log_id", "-"),
            getattr(self, "bed_number", None),
            getattr(getattr(self, "admission", None), "id", None),
            int(getattr(self, "_closing", False)),
            int(getattr(self, "_write_pending", False)),
        )
        self._close_mkb_service_once()
        super().closeEvent(event)
