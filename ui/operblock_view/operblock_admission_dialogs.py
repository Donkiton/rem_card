from __future__ import annotations
from rem_card.ui.styles.theme_runtime import set_widget_style

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import re
from typing import Any

from PySide6.QtCore import QDate, QEvent, QEventLoop, QRegularExpression, QSize, Qt
from PySide6.QtGui import (
    QIntValidator,
    QRegularExpressionValidator,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from rem_card.app.logger import logger
from rem_card.app.patient_age import parse_date_value
from rem_card.services.mkb import MKBService
from rem_card.services.operblock_service import (
    OPERBLOCK_BLOOD_GROUP_OPTIONS,
    OPERBLOCK_BLOOD_RH_OPTIONS,
    is_complete_operblock_mkb_code,
    normalize_operblock_blood_group,
    normalize_operblock_blood_rh,
    normalize_operblock_history_number,
    normalize_operblock_mkb_code,
)
from rem_card.services.operblock_anesthesia_types import (
    load_operblock_anesthesia_types,
    normalize_operblock_anesthesia_type_label,
)
from rem_card.services.operblock_team import (
    load_operblock_anesthesiologists,
    load_operblock_anesthetists,
    load_operblock_operating_nurses,
    load_operblock_surgeons,
    normalize_operblock_team_text,
)
from rem_card.services.patient_departments import PROFILE_DEPARTMENTS, normalize_profile_department
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.shared.window_state import SavedFramelessDialogMixin
from rem_card.ui.styles.shared_styles import apply_custom_dialog_style
from rem_card.ui.styles.theme import (
    BG_CARD,
    BG_MAIN,
    BORDER_LIGHT,
    STYLE_PATIENT_FORM_CANCEL_BUTTON,
    STYLE_PATIENT_FORM_INVALID_FIELD,
    STYLE_PATIENT_FORM_MANUAL_FIELD,
    STYLE_PATIENT_FORM_PAGE,
    STYLE_PATIENT_FORM_READONLY_FIELD,
    STYLE_PATIENT_FORM_SECTION_TITLE,
    STYLE_PATIENT_FORM_TAB,
    STYLE_PATIENT_FORM_VALID_FIELD,
    STYLE_SECTOR8_BUTTON,
    TEXT_SECONDARY,
)
from rem_card.ui.operblock_view.operblock_control_styles import (
    operblock_arrow_button_style,
    operblock_arrow_icon,
    operblock_combo_box_style as _operblock_combo_box_style,
    operblock_vertical_scrollbar_style as _operblock_vertical_scrollbar_style,
)

from rem_card.ui.operblock_view.operblock_visual_primitives import (
    _apply_operblock_window_icon,
    _create_operblock_title_icon,
    OPERBLOCK_DIALOG_CANCEL_BUTTON_STYLE,
    OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE,
    _label,
)
from rem_card.ui.operblock_view.operblock_helpers import (
    _line_edit,
    _parse_datetime_value,
    _minute_floor_dt,
    _operblock_time_minutes_from_text,
    _operblock_format_time_edit_text,
    _operblock_time_text_from_minutes,
    normalize_operblock_birth_date_text,
    _operblock_table_display_name,
)
from rem_card.ui.operblock_view.operblock_dialog_base import (
    OperBlockStyledDialog,
)
from rem_card.ui.operblock_view.operblock_stage_dialogs import (
    StartAnesthesiaDialog,
)
from rem_card.ui.operblock_view.operblock_vitals_adapter import (
    OPERBLOCK_STARTED_AT_LOCK_TOOLTIP,
)

class OperBlockAdmissionTimeInput(QFrame):
    def __init__(self, initial_datetime: datetime | None = None, parent=None):
        super().__init__(parent)
        initial = _minute_floor_dt(initial_datetime) or datetime.now().replace(second=0, microsecond=0)
        self._base_datetime = initial
        self._min_datetime: datetime | None = None
        self._max_datetime = datetime.now().replace(second=0, microsecond=0)
        self._max_datetime_fixed = False
        self._time_text_updating = False
        self._locked = False
        self._lock_reason = ""
        self._init_ui()
        self.set_datetime(initial)

    def _init_ui(self) -> None:
        self.setObjectName("OperBlockAdmissionTimeInput")
        set_widget_style(self, f"""
            QFrame#OperBlockAdmissionTimeInput {{
                background: transparent;
                border: none;
            }}
            QFrame#OperBlockAdmissionTimeFrame {{
                background-color: #FFFFFF;
                border: 1px solid #D1D5DB;
                border-radius: 8px;
            }}
            QFrame#OperBlockAdmissionTimeFrame[focused="true"] {{
                border: 1px solid #6366F1;
            }}
            QFrame#OperBlockAdmissionTimeFrame[locked="true"] {{
                background-color: #F8FAFC;
                border: 1px solid #CBD5E1;
            }}
            QLineEdit#OperBlockAdmissionTimeLineEdit {{
                background: transparent;
                border: none;
                color: #111827;
                font-size: 17px;
                font-weight: 700;
                padding: 0 12px;
                selection-background-color: #C7D2FE;
            }}
            QLineEdit#OperBlockAdmissionTimeLineEdit[locked="true"] {{
                color: #64748B;
            }}
            QFrame#OperBlockAdmissionTimeStepper {{
                background: transparent;
                border: none;
            }}
            QPushButton#OperBlockAdmissionTimeInfoButton {{
                background-color: #FFF7ED;
                border: 1px solid #FDBA74;
                border-radius: 10px;
                color: #C2410C;
                font-size: 13px;
                font-weight: 900;
                padding: 0;
            }}
            QLabel#OperBlockAdmissionTimeNote {{
                color: {TEXT_SECONDARY};
                font-size: 11px;
                line-height: 14px;
                background: transparent;
                border: none;
            }}
            QLabel#OperBlockAdmissionTimeNote[locked="true"] {{
                color: #B45309;
            }}
            """
            + operblock_arrow_button_style("QPushButton#OperBlockAdmissionTimeStepButton"))
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self.time_frame = QFrame()
        self.time_frame.setObjectName("OperBlockAdmissionTimeFrame")
        self.time_frame.setFixedHeight(40)
        self.time_frame.setMinimumWidth(170)
        self.time_frame.setMaximumWidth(240)
        self.time_frame.setProperty("focused", False)
        self.time_frame.setProperty("locked", False)
        frame_layout = QHBoxLayout(self.time_frame)
        frame_layout.setContentsMargins(0, 0, 0, 0)
        frame_layout.setSpacing(0)

        self.time_input = QLineEdit()
        self.time_input.setObjectName("OperBlockAdmissionTimeLineEdit")
        self.time_input.setPlaceholderText("06:40")
        self.time_input.setMaxLength(5)
        self.time_input.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self.time_input.textEdited.connect(self._on_time_text_edited)
        self.time_input.editingFinished.connect(self._commit_time_text)
        self.time_input.installEventFilter(self)
        frame_layout.addWidget(self.time_input, 1)

        stepper = QFrame()
        stepper.setObjectName("OperBlockAdmissionTimeStepper")
        stepper.setFixedWidth(42)
        stepper_layout = QVBoxLayout(stepper)
        stepper_layout.setContentsMargins(6, 4, 6, 4)
        stepper_layout.setSpacing(4)

        self.up_button = QPushButton()
        self.up_button.setObjectName("OperBlockAdmissionTimeStepButton")
        self.up_button.setFixedSize(30, 14)
        self.up_button.setIcon(operblock_arrow_icon(up=True))
        self.up_button.setIconSize(QSize(12, 12))
        self.up_button.setCursor(Qt.PointingHandCursor)
        self.up_button.clicked.connect(lambda _=False: self._step_time(1))

        self.down_button = QPushButton()
        self.down_button.setObjectName("OperBlockAdmissionTimeStepButton")
        self.down_button.setFixedSize(30, 14)
        self.down_button.setIcon(operblock_arrow_icon(up=False))
        self.down_button.setIconSize(QSize(12, 12))
        self.down_button.setCursor(Qt.PointingHandCursor)
        self.down_button.clicked.connect(lambda _=False: self._step_time(-1))

        stepper_layout.addWidget(self.up_button)
        stepper_layout.addWidget(self.down_button)
        frame_layout.addWidget(stepper, 0)
        row.addWidget(self.time_frame, 0)

        self.info_button = QPushButton("!")
        self.info_button.setObjectName("OperBlockAdmissionTimeInfoButton")
        self.info_button.setFixedSize(20, 20)
        self.info_button.setCursor(Qt.PointingHandCursor)
        self.info_button.setToolTip(OPERBLOCK_STARTED_AT_LOCK_TOOLTIP)
        self.info_button.clicked.connect(self._show_lock_tooltip)
        self.info_button.hide()
        row.addWidget(self.info_button, 0, Qt.AlignVCenter)
        root.addLayout(row, 0)

        self.note_label = QLabel(
            "Время можно изменить до внесения данных в карту. После начала пособия, операции, назначений "
            "или дополнительных витальных показателей сначала отмените эти изменения."
        )
        self.note_label.setObjectName("OperBlockAdmissionTimeNote")
        self.note_label.setWordWrap(True)
        self.note_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.note_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        root.addWidget(self.note_label, 1)

    def eventFilter(self, obj, event):
        if obj is getattr(self, "time_input", None):
            if event.type() == QEvent.FocusIn:
                self._set_time_focus(True)
            elif event.type() == QEvent.FocusOut:
                self._set_time_focus(False)
        return super().eventFilter(obj, event)

    def _set_time_focus(self, focused: bool) -> None:
        self.time_frame.setProperty("focused", bool(focused))
        self._refresh_widget_style(self.time_frame)

    @staticmethod
    def _refresh_widget_style(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _on_time_text_edited(self, text: str) -> None:
        if self._time_text_updating:
            return
        formatted = _operblock_format_time_edit_text(text)
        self._set_time_input_text(formatted, select_all=False)

    def _commit_time_text(self) -> str:
        minutes = _operblock_time_minutes_from_text(self.time_input.text())
        if minutes is None:
            minutes = self._base_datetime.hour * 60 + self._base_datetime.minute
        selected_dt = self._coerce_datetime(self._datetime_from_minutes(minutes))
        self._base_datetime = selected_dt
        normalized = _operblock_time_text_from_minutes(selected_dt.hour * 60 + selected_dt.minute)
        self._set_time_input_text(normalized, select_all=False)
        return normalized

    def _datetime_from_minutes(self, minutes: int) -> datetime:
        hour = int(minutes) // 60
        minute = int(minutes) % 60
        base = self._base_datetime or datetime.now().replace(second=0, microsecond=0)
        candidates = [
            datetime.combine(base.date() + timedelta(days=offset), datetime.min.time()).replace(hour=hour, minute=minute)
            for offset in (-1, 0, 1)
        ]
        bounded = [
            item
            for item in candidates
            if (self._min_datetime is None or item >= self._min_datetime)
            and (self._max_datetime is None or item <= self._max_datetime)
        ]
        source = bounded or candidates
        return min(source, key=lambda item: abs((item - base).total_seconds()))

    def _coerce_datetime(self, value: datetime) -> datetime:
        selected = _minute_floor_dt(value) or datetime.now().replace(second=0, microsecond=0)
        current_minute = datetime.now().replace(second=0, microsecond=0)
        if not self._max_datetime_fixed and (
            self._max_datetime is None or current_minute > self._max_datetime
        ):
            self._max_datetime = current_minute
        if self._max_datetime is not None and selected > self._max_datetime:
            selected = self._max_datetime
        if self._min_datetime is not None and selected < self._min_datetime:
            selected = self._min_datetime
        return selected

    def _step_time(self, delta_minutes: int) -> None:
        if self._locked:
            return
        minutes = _operblock_time_minutes_from_text(self.time_input.text())
        current_dt = self._datetime_from_minutes(minutes) if minutes is not None else self._base_datetime
        selected_dt = self._coerce_datetime(current_dt + timedelta(minutes=int(delta_minutes)))
        self._base_datetime = selected_dt
        self._set_time_input_text(selected_dt.strftime("%H:%M"), select_all=True)

    def _set_time_input_text(self, text: str, *, select_all: bool) -> None:
        self._time_text_updating = True
        try:
            self.time_input.setText(str(text or "")[:5])
            if select_all:
                self.time_input.setFocus(Qt.OtherFocusReason)
                self.time_input.selectAll()
            else:
                self.time_input.setCursorPosition(len(self.time_input.text()))
        finally:
            self._time_text_updating = False

    def set_datetime(self, value: datetime | str | None) -> None:
        parsed = _minute_floor_dt(_parse_datetime_value(value)) if not isinstance(value, datetime) else _minute_floor_dt(value)
        selected = self._coerce_datetime(parsed or datetime.now().replace(second=0, microsecond=0))
        self._base_datetime = selected
        self._set_time_input_text(selected.strftime("%H:%M"), select_all=False)

    def set_bounds(
        self,
        minimum: datetime | str | None = None,
        maximum: datetime | str | None = None,
    ) -> None:
        self._min_datetime = _minute_floor_dt(_parse_datetime_value(minimum))
        parsed_maximum = _minute_floor_dt(_parse_datetime_value(maximum))
        self._max_datetime_fixed = parsed_maximum is not None
        self._max_datetime = parsed_maximum or datetime.now().replace(second=0, microsecond=0)
        if (
            self._min_datetime is not None
            and self._max_datetime is not None
            and self._max_datetime < self._min_datetime
        ):
            self._max_datetime = self._min_datetime
        self.set_datetime(self._base_datetime)

    def set_locked(self, locked: bool, reason: str = "") -> None:
        self._locked = bool(locked)
        self._lock_reason = str(reason or "").strip()
        tooltip = (
            f"{OPERBLOCK_STARTED_AT_LOCK_TOOLTIP}\nПричина: {self._lock_reason}"
            if self._lock_reason
            else OPERBLOCK_STARTED_AT_LOCK_TOOLTIP
        )
        self.time_input.setReadOnly(self._locked)
        self.time_input.setProperty("locked", self._locked)
        self.time_frame.setProperty("locked", self._locked)
        self.up_button.setEnabled(not self._locked)
        self.down_button.setEnabled(not self._locked)
        self.info_button.setVisible(self._locked)
        self.info_button.setToolTip(tooltip)
        self.note_label.setProperty("locked", self._locked)
        if self._locked:
            self.note_label.setText(
                "Время поступления заблокировано. Отмените внесённые изменения в карте, чтобы снова изменить это время."
            )
        elif self._min_datetime is not None:
            maximum_text = (
                f" до {self._max_datetime.strftime('%H:%M')}"
                if self._max_datetime is not None
                else ""
            )
            self.note_label.setText(
                "Время поступления можно указать с "
                f"{self._min_datetime.strftime('%H:%M')} (время отправки из РАО)"
                f"{maximum_text}."
            )
        else:
            self.note_label.setText(
                "Время можно изменить до внесения данных в карту. После начала пособия, операции, назначений "
                "или дополнительных витальных показателей сначала отмените эти изменения."
            )
        self._refresh_widget_style(self.time_input)
        self._refresh_widget_style(self.time_frame)
        self._refresh_widget_style(self.note_label)

    def _show_lock_tooltip(self) -> None:
        QToolTip.showText(
            self.info_button.mapToGlobal(self.info_button.rect().bottomRight()),
            self.info_button.toolTip() or OPERBLOCK_STARTED_AT_LOCK_TOOLTIP,
            self.info_button,
            self.info_button.rect(),
            9000,
        )

    def datetime_value(self) -> datetime:
        self._commit_time_text()
        return self._base_datetime

    def datetime_text(self) -> str:
        return self.datetime_value().isoformat(timespec="seconds")


class OperBlockQueueDialog(OperBlockStyledDialog):
    def __init__(self, loader, parent=None):
        self._loader = loader
        self._rows: list[dict[str, Any]] = []
        self.selected_handoff_id: int | None = None
        super().__init__(
            "Очередь пациентов из РАО",
            "rao_queue_dialog_geometry",
            parent,
            minimum_size=(720, 420),
            initial_size=(920, 560),
        )
        self._init_ui()
        self._finalize_dialog_chrome()

    def _init_ui(self) -> None:
        intro = QLabel(
            "Список запрашивается только при открытии этого окна и по кнопке «Обновить»."
        )
        intro.setWordWrap(True)
        set_widget_style(intro, f"color: {TEXT_SECONDARY}; font-size: 13px;")
        self.content_layout.addWidget(intro)

        self.table = QTableWidget(0, 7)
        self.table.setObjectName("OperBlockRaoQueueTable")
        self.table.setHorizontalHeaderLabels(
            ["Отправлен", "Ожидается", "ФИО", "История", "Койка", "Диагноз", "Профиль"]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.Stretch)
        header.setSectionResizeMode(6, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._update_selection)
        self.table.itemDoubleClicked.connect(lambda *_args: self._choose_selected())
        self.content_layout.addWidget(self.table, 1)

        self.status_label = QLabel("")
        set_widget_style(self.status_label, f"color: {TEXT_SECONDARY}; font-size: 12px;")
        self.content_layout.addWidget(self.status_label)

        actions = QHBoxLayout()
        self.refresh_button = QPushButton("Обновить")
        self.refresh_button.setMinimumHeight(36)
        self.refresh_button.setCursor(Qt.PointingHandCursor)
        set_widget_style(self.refresh_button, STYLE_SECTOR8_BUTTON)
        self.refresh_button.clicked.connect(self.refresh_rows)
        actions.addWidget(self.refresh_button)
        actions.addStretch(1)
        close_button = QPushButton("Закрыть")
        close_button.setMinimumHeight(36)
        set_widget_style(close_button, OPERBLOCK_DIALOG_CANCEL_BUTTON_STYLE)
        close_button.clicked.connect(self.reject)
        actions.addWidget(close_button)
        self.choose_button = QPushButton("Занять стол")
        self.choose_button.setMinimumHeight(36)
        self.choose_button.setEnabled(False)
        set_widget_style(self.choose_button, OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE)
        self.choose_button.clicked.connect(self._choose_selected)
        actions.addWidget(self.choose_button)
        self.content_layout.addLayout(actions)

    @staticmethod
    def _display_time(value: Any) -> str:
        parsed = _parse_datetime_value(value)
        return parsed.strftime("%d.%m.%Y %H:%M") if parsed is not None else str(value or "")

    def refresh_rows(self) -> None:
        self.refresh_button.setEnabled(False)
        self.choose_button.setEnabled(False)
        self.status_label.setText("Загрузка…")
        QApplication.processEvents(QEventLoop.ExcludeUserInputEvents)
        try:
            self._rows = [dict(row or {}) for row in (self._loader() or [])]
        except Exception as exc:
            self._rows = []
            self.table.setRowCount(0)
            self.status_label.setText("Не удалось обновить очередь.")
            CustomMessageBox.warning(self, "Очередь", str(exc))
        else:
            self._fill_table()
        finally:
            self.refresh_button.setEnabled(True)

    def _fill_table(self) -> None:
        self.table.setRowCount(len(self._rows))
        for row_index, row in enumerate(self._rows):
            patient = dict(row.get("patient_snapshot") or {})
            values = (
                self._display_time(row.get("dispatched_at")),
                self._display_time(row.get("expected_arrival_at")),
                str(patient.get("full_name") or ""),
                str(patient.get("history_number") or ""),
                str(row.get("current_bed_number") or row.get("bed_number_at_dispatch") or ""),
                str(patient.get("diagnosis_text") or patient.get("diagnosis_code") or ""),
                str(patient.get("department_profile") or ""),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.UserRole, int(row["id"]))
                self.table.setItem(row_index, column, item)
        if self._rows:
            self.status_label.setText(f"Ожидают операционную: {len(self._rows)}")
            self.table.selectRow(0)
        else:
            self.status_label.setText("Пациентов, ожидающих операционную, нет.")
        self._update_selection()

    def _selected_id(self) -> int | None:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        value = item.data(Qt.UserRole) if item is not None else None
        return int(value) if value is not None else None

    def _update_selection(self) -> None:
        self.choose_button.setEnabled(self._selected_id() is not None)

    def _choose_selected(self) -> None:
        handoff_id = self._selected_id()
        if handoff_id is None:
            return
        self.selected_handoff_id = handoff_id
        self.accept()


class OccupyTableDialog(SavedFramelessDialogMixin, QDialog):
    EMPTY_BIRTH_DATE = QDate(1900, 1, 1)

    def __init__(
        self,
        table_code: str,
        table_name: str,
        parent=None,
        *,
        mode: str = "create",
        initial_data: dict | None = None,
        operation_case_id: int | None = None,
    ):
        super().__init__(parent)
        self.table_code = table_code
        self.table_name = table_name or _operblock_table_display_name(table_code)
        self.operation_case_id = int(operation_case_id) if operation_case_id else None
        self.handoff_id: int | None = None
        self.source_rao_admission_id: int | None = None
        self.is_edit_mode = str(mode or "").strip().lower() == "edit"
        self.mkb_service = MKBService()
        self._surgeon_rows: list[tuple[QWidget, QComboBox]] = []
        try:
            self._anesthesia_type_options = load_operblock_anesthesia_types()
        except Exception as exc:
            logger.error("operblock anesthesia types load failed: %s", exc, exc_info=True)
            self._anesthesia_type_options = []
        try:
            self._surgeon_options = load_operblock_surgeons()
        except Exception as exc:
            logger.error("operblock surgeons load failed: %s", exc, exc_info=True)
            self._surgeon_options = []
        try:
            self._operating_nurse_options = load_operblock_operating_nurses()
        except Exception as exc:
            logger.error("operblock operating nurses load failed: %s", exc, exc_info=True)
            self._operating_nurse_options = []
        try:
            self._anesthesiologist_options = load_operblock_anesthesiologists()
        except Exception as exc:
            logger.error("operblock anesthesiologists load failed: %s", exc, exc_info=True)
            self._anesthesiologist_options = []
        try:
            self._anesthetist_options = load_operblock_anesthetists()
        except Exception as exc:
            logger.error("operblock anesthetists load failed: %s", exc, exc_info=True)
            self._anesthetist_options = []
        self._save_button_text = (
            "СОХРАНИТЬ ИЗМЕНЕНИЯ" if self.is_edit_mode else "СОЗДАТЬ КАРТОЧКУ ПАЦИЕНТА"
        )
        self.setWindowTitle("Редактировать пациента" if self.is_edit_mode else "Занять стол")
        _apply_operblock_window_icon(self)
        self.setMinimumSize(780, 620)
        self.resize(900, 760)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setMouseTracking(True)
        self._init_saved_frameless_dialog("operblock/occupy_table_dialog_geometry", drag_area_height=70)
        self._init_ui()
        if initial_data:
            self.set_data(initial_data)
        self._restore_saved_geometry()

    def _section(self, title: str) -> tuple[QFrame, QFormLayout]:
        frame = QFrame()
        frame.setObjectName("OperBlockPatientFormSection")
        set_widget_style(frame, f"""
            QFrame#OperBlockPatientFormSection {{
                background-color: {BG_CARD};
                border: 1px solid {BORDER_LIGHT};
                border-radius: 8px;
            }}
            QFrame#OperBlockPatientFormSection QLabel {{
                background: transparent;
                border: none;
            }}
            """)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(18, 14, 18, 16)
        layout.setSpacing(10)
        title_label = QLabel(title)
        set_widget_style(title_label, STYLE_PATIENT_FORM_SECTION_TITLE)
        layout.addWidget(title_label)
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        layout.addLayout(form)
        return frame, form

    @staticmethod
    def _composite_form_label(text: str, object_name: str, *, top_offset: int = 27) -> QLabel:
        label = QLabel(text)
        label.setObjectName(object_name)
        label.setAlignment(Qt.AlignRight | Qt.AlignTop)
        label.setContentsMargins(0, int(top_offset), 0, 0)
        label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        return label

    def _init_ui(self):
        apply_custom_dialog_style(self)

        self.bg_container = QFrame(self)
        self.bg_container.setObjectName("DialogMainFrame")
        self.bg_container.setMouseTracking(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.bg_container)

        main = QVBoxLayout(self.bg_container)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        header = QFrame(self.bg_container)
        header.setObjectName("DialogTitleBar")
        header.setFixedHeight(30)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(5, 0, 0, 0)
        header_layout.setSpacing(0)
        icon_label = _create_operblock_title_icon(20)
        if icon_label is not None:
            header_layout.addWidget(icon_label)
            header_layout.addSpacing(8)
        header_text = (
            f"РЕДАКТИРОВАТЬ ПАЦИЕНТА — {self.table_name.upper()}"
            if self.is_edit_mode
            else f"ЗАНЯТЬ СТОЛ — {self.table_name.upper()}"
        )
        title = QLabel(header_text)
        title.setObjectName("DialogTitleText")
        header_layout.addWidget(title)
        header_layout.addStretch(1)
        self.close_button = QPushButton("✕")
        self.close_button.setObjectName("DialogCloseBtn")
        self.close_button.setFixedSize(30, 30)
        self.close_button.setCursor(Qt.PointingHandCursor)
        self.close_button.clicked.connect(self.reject)
        header_layout.addWidget(self.close_button)
        main.addWidget(header)

        content = QFrame(self.bg_container)
        content.setObjectName("OperBlockOccupyFormContent")
        set_widget_style(content, f"""
            {STYLE_PATIENT_FORM_TAB}
            QFrame#OperBlockOccupyFormContent {{
                background-color: {BG_MAIN};
            }}
            """)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(20, 16, 20, 20)
        content_layout.setSpacing(10)

        self.form_scroll = QScrollArea()
        self.form_scroll.setObjectName("OperBlockOccupyFormScroll")
        self.form_scroll.setWidgetResizable(True)
        self.form_scroll.setFrameShape(QScrollArea.NoFrame)
        self.form_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.form_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        set_widget_style(self.form_scroll, f"""
            QScrollArea#OperBlockOccupyFormScroll {{
                border: none;
                background: {BG_MAIN};
            }}
            QScrollArea#OperBlockOccupyFormScroll > QWidget {{
                background: {BG_MAIN};
            }}
            QScrollArea#OperBlockOccupyFormScroll > QWidget > QWidget {{
                background: {BG_MAIN};
            }}
            """)
        self.form_scroll.viewport().setObjectName("OperBlockOccupyFormViewport")
        set_widget_style(self.form_scroll.viewport(), f"background: {BG_MAIN};")
        form_scrollbar = self.form_scroll.verticalScrollBar()
        form_scrollbar.setObjectName("OperBlockPatientFormScrollBar")
        form_scrollbar.setFixedWidth(14)
        form_scrollbar.setSingleStep(36)
        form_scrollbar.setPageStep(180)
        set_widget_style(form_scrollbar, _operblock_vertical_scrollbar_style(
                "OperBlockPatientFormScrollBar",
                width_px=14,
                left_margin_px=2,
                right_margin_px=1,
            ))

        self.form_page = QWidget()
        self.form_page.setObjectName("OperBlockOccupyFormPage")
        set_widget_style(self.form_page, f"""
            {STYLE_PATIENT_FORM_PAGE}
            QWidget#OperBlockOccupyFormPage {{
                background-color: {BG_MAIN};
            }}
            """)
        page_layout = QVBoxLayout(self.form_page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(10)

        general, general_form = self._section("1. ОБЩИЕ ДАННЫЕ ПАЦИЕНТА")
        self.history_input = _line_edit()
        self.history_input.setPlaceholderText("12345, АБ123 или 123/456")
        self.full_name_input = _line_edit()
        self.full_name_input.setPlaceholderText("Фамилия Имя Отчество")
        self.gender_combo = QComboBox()
        self.gender_combo.setFixedHeight(34)
        self.gender_combo.addItems(["Мужской", "Женский"])
        set_widget_style(self.gender_combo, _operblock_combo_box_style())
        self._install_occupy_combo_wheel_redirect(self.gender_combo)
        self.birth_date_input = _line_edit()
        self.birth_date_input.setObjectName("OperBlockOccupyBirthDateInput")
        self.birth_date_input.setFixedHeight(34)
        self.birth_date_input.setPlaceholderText("дд.мм.гггг")
        self.birth_date_input.setMaxLength(10)
        self.birth_date_input.setValidator(QRegularExpressionValidator(QRegularExpression(r"^[0-9.,/\\]*$")))
        self.birth_date_input.textEdited.connect(self._on_birth_date_text_edited)
        self.birth_date_input.editingFinished.connect(self._normalize_birth_date_field)
        general_form.addRow("Номер истории болезни *:", self.history_input)
        general_form.addRow("ФИО пациента *:", self.full_name_input)
        general_form.addRow("Пол *:", self.gender_combo)
        general_form.addRow("Дата рождения *:", self.birth_date_input)
        page_layout.addWidget(general)

        diagnosis, diagnosis_form = self._section("2. ДИАГНОЗ")
        self.diagnosis_code_input = _line_edit()
        self.diagnosis_code_input.setPlaceholderText("Код МКБ-10")
        self.diagnosis_code_input.setMaxLength(6)
        self.diagnosis_code_input.textEdited.connect(self._on_mkb_code_text_edited)
        self.diagnosis_code_input.editingFinished.connect(self._validate_mkb_code)
        self.diagnosis_name = _label("", size=12, color=TEXT_SECONDARY)
        self.diagnosis_text_input = _line_edit()
        self._set_manual_diagnosis_enabled(False, clear=True, placeholder="Сначала введите код МКБ-10")
        code_line = QHBoxLayout()
        code_line.setContentsMargins(0, 0, 0, 0)
        code_line.setSpacing(12)
        code_line.addWidget(self.diagnosis_code_input, 0)
        code_line.addWidget(self.diagnosis_name, 1)
        diagnosis_form.addRow("Код диагноза МКБ-10 *:", code_line)
        diagnosis_form.addRow("Диагноз *:", self.diagnosis_text_input)
        self.department_profile_combo = self._profile_department_combo()
        self._install_occupy_combo_wheel_redirect(self.department_profile_combo)
        diagnosis_form.addRow("Профильное отделение:", self.department_profile_combo)
        page_layout.addWidget(diagnosis)

        operation, operation_form = self._section("3. ОПЕРАЦИОННАЯ ИНФОРМАЦИЯ")
        self.operation_name_input = _line_edit()
        self.operation_name_input.setPlaceholderText("Название операции")
        self.operation_name_input.setMinimumWidth(430)
        self.operation_name_input.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.admission_time_input = OperBlockAdmissionTimeInput(datetime.now(), self)
        self.anesthesia_assistance_type_combo = QComboBox()
        self.anesthesia_assistance_type_combo.setEditable(True)
        self.anesthesia_assistance_type_combo.setFixedHeight(34)
        self.anesthesia_assistance_type_combo.setMinimumWidth(430)
        self.anesthesia_assistance_type_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.anesthesia_assistance_type_combo.setMinimumContentsLength(38)
        self.anesthesia_assistance_type_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        set_widget_style(self.anesthesia_assistance_type_combo, _operblock_combo_box_style())
        assistance_line_edit = self.anesthesia_assistance_type_combo.lineEdit()
        if assistance_line_edit is not None:
            assistance_line_edit.setPlaceholderText("Вид пособия")
        seen_assistance_types: set[str] = set()
        for item in self._anesthesia_type_options or []:
            label = normalize_operblock_anesthesia_type_label((item or {}).get("label"))
            key = label.casefold()
            if label and key not in seen_assistance_types:
                seen_assistance_types.add(key)
                self.anesthesia_assistance_type_combo.addItem(label, label)
        self.anesthesia_assistance_type_combo.setCurrentIndex(-1)
        self.anesthesia_assistance_type_combo.setEditText("")
        self._install_occupy_combo_wheel_redirect(self.anesthesia_assistance_type_combo)
        self.height_input = _line_edit()
        self.height_input.setPlaceholderText("см")
        self.height_input.setValidator(QIntValidator(1, 260, self.height_input))
        self.weight_input = _line_edit()
        self.weight_input.setPlaceholderText("кг")
        height_weight_widget = QWidget()
        height_weight_widget.setObjectName("OperBlockOccupyHeightWeightFields")
        set_widget_style(height_weight_widget, """
            QWidget#OperBlockOccupyHeightWeightFields {
                background: transparent;
                border: none;
            }
            QWidget#OperBlockOccupyHeightWeightFields QLabel {
                background: transparent;
                border: none;
            }
            """)
        height_weight_layout = QGridLayout(height_weight_widget)
        height_weight_layout.setContentsMargins(0, 0, 0, 0)
        height_weight_layout.setHorizontalSpacing(12)
        height_weight_layout.setVerticalSpacing(4)
        height_label = QLabel("Рост (см)")
        weight_label = QLabel("Вес (кг)")
        for label in (height_label, weight_label):
            set_widget_style(label, f"font-size: 12px; font-weight: 700; color: {TEXT_SECONDARY};")
        height_weight_layout.addWidget(height_label, 0, 0)
        height_weight_layout.addWidget(weight_label, 0, 1)
        height_weight_layout.addWidget(self.height_input, 1, 0)
        height_weight_layout.addWidget(self.weight_input, 1, 1)
        height_weight_layout.setColumnStretch(0, 1)
        height_weight_layout.setColumnStretch(1, 1)
        self.allergies_input = _line_edit()
        self.allergies_input.setPlaceholderText("Нет данных")
        self.blood_group_combo = self._fixed_option_combo(OPERBLOCK_BLOOD_GROUP_OPTIONS)
        self.blood_rh_combo = self._fixed_option_combo(OPERBLOCK_BLOOD_RH_OPTIONS)
        self._install_occupy_combo_wheel_redirect(self.blood_group_combo)
        self._install_occupy_combo_wheel_redirect(self.blood_rh_combo)
        blood_widget = QWidget()
        blood_widget.setObjectName("OperBlockOccupyBloodFields")
        set_widget_style(blood_widget, """
            QWidget#OperBlockOccupyBloodFields {
                background: transparent;
                border: none;
            }
            QWidget#OperBlockOccupyBloodFields QLabel {
                background: transparent;
                border: none;
            }
            """)
        blood_layout = QGridLayout(blood_widget)
        blood_layout.setContentsMargins(0, 0, 0, 0)
        blood_layout.setHorizontalSpacing(12)
        blood_layout.setVerticalSpacing(4)
        blood_group_label = QLabel("Группа крови")
        blood_rh_label = QLabel("Резус")
        for label in (blood_group_label, blood_rh_label):
            set_widget_style(label, f"font-size: 12px; font-weight: 700; color: {TEXT_SECONDARY};")
        blood_layout.addWidget(blood_group_label, 0, 0)
        blood_layout.addWidget(blood_rh_label, 0, 1)
        blood_layout.addWidget(self.blood_group_combo, 1, 0)
        blood_layout.addWidget(self.blood_rh_combo, 1, 1)
        blood_layout.setColumnStretch(0, 1)
        blood_layout.setColumnStretch(1, 1)
        surgery_team_widget = QWidget()
        surgery_team_widget.setObjectName("OperBlockOccupySurgeryFields")
        set_widget_style(surgery_team_widget, """
            QWidget#OperBlockOccupySurgeryFields {
                background: transparent;
                border: none;
            }
            QWidget#OperBlockOccupySurgeryFields QLabel {
                background: transparent;
                border: none;
            }
            """)
        surgery_team_layout = QGridLayout(surgery_team_widget)
        surgery_team_layout.setContentsMargins(0, 0, 0, 0)
        surgery_team_layout.setHorizontalSpacing(12)
        surgery_team_layout.setVerticalSpacing(6)
        self.surgery_team_layout = surgery_team_layout
        surgeon_label = QLabel("Хирург")
        operating_nurse_label = QLabel("Опер. сестра")
        for label in (surgeon_label, operating_nurse_label):
            set_widget_style(label, f"font-size: 12px; font-weight: 700; color: {TEXT_SECONDARY};")
        surgery_team_layout.addWidget(surgeon_label, 0, 0)
        surgery_team_layout.addWidget(operating_nurse_label, 0, 1)

        self.operating_nurse_combo = StartAnesthesiaDialog._staff_combo(self._operating_nurse_options)
        self._configure_occupy_team_combo(self.operating_nurse_combo)
        self._install_occupy_combo_wheel_redirect(self.operating_nurse_combo)
        self.add_surgeon_button = QPushButton("+ Добавить хирурга")
        self.add_surgeon_button.setCursor(Qt.PointingHandCursor)
        self.add_surgeon_button.setFixedHeight(32)
        set_widget_style(self.add_surgeon_button, STYLE_SECTOR8_BUTTON)
        self.add_surgeon_button.clicked.connect(lambda: self._add_surgeon_row())
        surgery_team_layout.setColumnStretch(0, 1)
        surgery_team_layout.setColumnStretch(1, 1)
        surgery_team_layout.setColumnStretch(2, 0)
        self._add_surgeon_row()
        self.anesthesiologist_combo = StartAnesthesiaDialog._staff_combo(self._anesthesiologist_options)
        self.anesthetist_combo = StartAnesthesiaDialog._staff_combo(self._anesthetist_options)
        for combo in (self.anesthesiologist_combo, self.anesthetist_combo):
            self._configure_occupy_team_combo(combo)
            self._install_occupy_combo_wheel_redirect(combo)
        anesthesia_team_widget = QWidget()
        anesthesia_team_widget.setObjectName("OperBlockOccupyAnesthesiaFields")
        set_widget_style(anesthesia_team_widget, """
            QWidget#OperBlockOccupyAnesthesiaFields {
                background: transparent;
                border: none;
            }
            QWidget#OperBlockOccupyAnesthesiaFields QLabel {
                background: transparent;
                border: none;
            }
            """)
        anesthesia_team_layout = QGridLayout(anesthesia_team_widget)
        anesthesia_team_layout.setContentsMargins(0, 0, 0, 0)
        anesthesia_team_layout.setHorizontalSpacing(12)
        anesthesia_team_layout.setVerticalSpacing(4)
        anesthesiologist_label = QLabel("Анестезиолог")
        anesthetist_label = QLabel("Анестезистка")
        for label in (anesthesiologist_label, anesthetist_label):
            set_widget_style(label, f"font-size: 12px; font-weight: 700; color: {TEXT_SECONDARY};")
        anesthesia_team_layout.addWidget(anesthesiologist_label, 0, 0)
        anesthesia_team_layout.addWidget(anesthetist_label, 0, 1)
        anesthesia_team_layout.addWidget(self.anesthesiologist_combo, 1, 0)
        anesthesia_team_layout.addWidget(self.anesthetist_combo, 1, 1)
        anesthesia_team_layout.setColumnStretch(0, 1)
        anesthesia_team_layout.setColumnStretch(1, 1)
        operation_form.addRow("Время поступления в оперблок:", self.admission_time_input)
        operation_form.addRow("Название операции:", self.operation_name_input)
        operation_form.addRow("Вид анест. пособия:", self.anesthesia_assistance_type_combo)
        self.height_weight_row_label = self._composite_form_label(
            "Рост / вес:",
            "OperBlockOccupyHeightWeightRowLabel",
        )
        operation_form.addRow(self.height_weight_row_label, height_weight_widget)
        operation_form.addRow("Аллергии:", self.allergies_input)
        self.blood_row_label = self._composite_form_label(
            "Кровь:",
            "OperBlockOccupyBloodRowLabel",
        )
        operation_form.addRow(self.blood_row_label, blood_widget)
        self.surgery_team_row_label = self._composite_form_label(
            "Хирургия:",
            "OperBlockOccupySurgeryRowLabel",
            top_offset=30,
        )
        operation_form.addRow(self.surgery_team_row_label, surgery_team_widget)
        self.anesthesia_team_row_label = self._composite_form_label(
            "Анестезия:",
            "OperBlockOccupyAnesthesiaRowLabel",
        )
        operation_form.addRow(self.anesthesia_team_row_label, anesthesia_team_widget)
        page_layout.addWidget(operation)

        vitals, vitals_form = self._section("4. ИСХОДНЫЕ ВИТАЛЬНЫЕ ПОКАЗАТЕЛИ")
        self.sys_input = _line_edit()
        self.sys_input.setPlaceholderText("Систолическое")
        self.sys_input.setValidator(QIntValidator(0, 300, self.sys_input))
        self.sys_input.installEventFilter(self)
        self.dia_input = _line_edit()
        self.dia_input.setPlaceholderText("Диастолическое")
        self.dia_input.setValidator(QIntValidator(0, 300, self.dia_input))
        self.pulse_input = _line_edit()
        self.pulse_input.setPlaceholderText("ЧСС")
        self.pulse_input.setValidator(QIntValidator(0, 300, self.pulse_input))
        self.spo2_input = _line_edit()
        self.spo2_input.setPlaceholderText("%")
        self.spo2_input.setValidator(QIntValidator(0, 100, self.spo2_input))
        ad_row = QHBoxLayout()
        ad_row.setContentsMargins(0, 0, 0, 0)
        ad_row.setSpacing(8)
        slash = QLabel("/")
        slash.setAlignment(Qt.AlignCenter)
        set_widget_style(slash, f"font-size: 18px; font-weight: 800; color: {TEXT_SECONDARY}; background: transparent;")
        ad_row.addWidget(self.sys_input, 1)
        ad_row.addWidget(slash, 0)
        ad_row.addWidget(self.dia_input, 1)
        vitals_form.addRow("АД:", ad_row)
        vitals_form.addRow("ЧСС:", self.pulse_input)
        vitals_form.addRow("SpO₂:", self.spo2_input)
        page_layout.addWidget(vitals)
        page_layout.addStretch(1)

        self.form_scroll.setWidget(self.form_page)
        content_layout.addWidget(self.form_scroll, 1)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 10, 0, 0)
        self.cancel_button = QPushButton("ОТМЕНИТЬ")
        self.cancel_button.setCursor(Qt.PointingHandCursor)
        self.cancel_button.setFixedHeight(45)
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.setDefault(False)
        set_widget_style(self.cancel_button, STYLE_PATIENT_FORM_CANCEL_BUTTON)
        self.cancel_button.clicked.connect(self.reject)
        self.save_button = QPushButton(self._save_button_text)
        self.save_button.setCursor(Qt.PointingHandCursor)
        self.save_button.setFixedHeight(45)
        self.save_button.setAutoDefault(True)
        self.save_button.setDefault(True)
        set_widget_style(self.save_button, OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE)
        buttons.addWidget(self.cancel_button, 1)
        buttons.addWidget(self.save_button, 2)
        content_layout.addLayout(buttons)
        main.addWidget(content, 1)

    @staticmethod
    def _configure_occupy_team_combo(combo: QComboBox) -> None:
        combo.setMinimumWidth(0)
        combo.setMinimumContentsLength(18)
        combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def _install_occupy_combo_wheel_redirect(self, combo: QComboBox) -> None:
        combo.installEventFilter(self)
        line_edit = combo.lineEdit()
        if line_edit is not None:
            line_edit.installEventFilter(self)

    def _add_surgeon_row(self, text: str = "") -> QComboBox:
        combo = StartAnesthesiaDialog._staff_combo(self._surgeon_options)
        self._configure_occupy_team_combo(combo)
        self._install_occupy_combo_wheel_redirect(combo)
        if text:
            combo.setEditText(text)
        remove_button = QPushButton("Удалить")
        remove_button.setObjectName("OperBlockOccupyRemoveSurgeonButton")
        remove_button.setFixedHeight(32)
        remove_button.setFixedWidth(remove_button.sizeHint().width() + 20)
        remove_button.setCursor(Qt.PointingHandCursor)
        set_widget_style(remove_button, STYLE_PATIENT_FORM_CANCEL_BUTTON)
        row = QWidget()
        row.setObjectName("OperBlockOccupySurgeonRow")
        set_widget_style(row, "QWidget#OperBlockOccupySurgeonRow { background: transparent; border: none; }")
        row_layout = QVBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(0)
        row_layout.addWidget(combo)
        row._remove_button = remove_button
        remove_button.clicked.connect(lambda _=False, widget=row: self._remove_surgeon_row(widget))
        self._surgeon_rows.append((row, combo))
        self._refresh_surgery_team_layout()
        return combo

    def _remove_surgeon_row(self, row: QWidget) -> None:
        for index, (widget, combo) in enumerate(list(self._surgeon_rows)):
            if widget is not row:
                continue
            self._surgeon_rows.pop(index)
            button = getattr(widget, "_remove_button", None)
            self.surgery_team_layout.removeWidget(widget)
            if button is not None:
                self.surgery_team_layout.removeWidget(button)
                button.deleteLater()
            widget.deleteLater()
            break
        if not self._surgeon_rows:
            self._add_surgeon_row()
            return
        self._refresh_surgery_team_layout()

    def _refresh_surgeon_remove_buttons(self) -> None:
        single = len(self._surgeon_rows) <= 1
        for widget, _combo in self._surgeon_rows:
            button = getattr(widget, "_remove_button", None)
            if button is not None:
                button.setVisible(not single)

    def _refresh_surgery_team_layout(self) -> None:
        for index, (widget, _combo) in enumerate(self._surgeon_rows):
            row_index = index + 1
            button = getattr(widget, "_remove_button", None)
            self.surgery_team_layout.addWidget(widget, row_index, 0)
            if button is not None:
                self.surgery_team_layout.addWidget(button, row_index, 2, Qt.AlignTop)
        self.surgery_team_layout.addWidget(self.operating_nurse_combo, 1, 1, Qt.AlignTop)
        self.surgery_team_layout.addWidget(self.add_surgeon_button, len(self._surgeon_rows) + 1, 0, 1, 2)
        self._refresh_surgeon_remove_buttons()

    @staticmethod
    def _fixed_option_combo(options: tuple[str, ...]) -> QComboBox:
        combo = QComboBox()
        combo.setEditable(False)
        combo.setFixedHeight(34)
        set_widget_style(combo, _operblock_combo_box_style())
        combo.addItem("Не указано", "")
        for option in options:
            combo.addItem(option, option)
        combo.setCurrentIndex(0)
        return combo

    @staticmethod
    def _profile_department_combo() -> QComboBox:
        combo = QComboBox()
        combo.setEditable(True)
        combo.setFixedHeight(34)
        combo.setMinimumWidth(430)
        combo.setMinimumContentsLength(38)
        combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        set_widget_style(combo, _operblock_combo_box_style())
        line_edit = combo.lineEdit()
        if line_edit is not None:
            line_edit.setPlaceholderText("Не указано")
        for department in PROFILE_DEPARTMENTS:
            combo.addItem(department)
        combo.setCurrentIndex(-1)
        return combo

    @staticmethod
    def _set_combo_text(combo: QComboBox, value: str) -> None:
        text = normalize_operblock_team_text(value)
        if combo.isEditable():
            combo.setEditText(text)
            return
        for index in range(combo.count()):
            if combo.itemText(index).casefold() == text.casefold():
                combo.setCurrentIndex(index)
                return

    @staticmethod
    def _set_fixed_combo_text(combo: QComboBox, value: str, normalizer) -> None:
        try:
            text = normalizer(value)
        except ValueError:
            text = ""
        for index in range(combo.count()):
            if str(combo.itemData(index) or "") == text:
                combo.setCurrentIndex(index)
                return
        combo.setCurrentIndex(0)

    def _occupy_combo_wheel_widgets(self) -> list[QComboBox]:
        combos: list[QComboBox] = []
        for attr in (
            "gender_combo",
            "department_profile_combo",
            "anesthesia_assistance_type_combo",
            "blood_group_combo",
            "blood_rh_combo",
            "operating_nurse_combo",
            "anesthesiologist_combo",
            "anesthetist_combo",
        ):
            combo = getattr(self, attr, None)
            if isinstance(combo, QComboBox):
                combos.append(combo)
        combos.extend(combo for _widget, combo in getattr(self, "_surgeon_rows", []))
        unique: list[QComboBox] = []
        seen: set[int] = set()
        for combo in combos:
            key = id(combo)
            if key in seen:
                continue
            seen.add(key)
            unique.append(combo)
        return unique

    def _is_occupy_combo_wheel_widget(self, watched) -> bool:
        for combo in self._occupy_combo_wheel_widgets():
            if watched is combo:
                return True
            line_edit = combo.lineEdit()
            if line_edit is not None and watched is line_edit:
                return True
        return False

    def _scroll_occupy_form_wheel(self, event) -> bool:
        scroll = getattr(self, "form_scroll", None)
        if scroll is None:
            event.accept()
            return True
        pixel_delta = event.pixelDelta().y() if hasattr(event, "pixelDelta") else 0
        angle_delta = event.angleDelta().y() if hasattr(event, "angleDelta") else 0
        delta = pixel_delta or angle_delta
        if delta:
            bar = scroll.verticalScrollBar()
            if bar.maximum() > bar.minimum():
                if pixel_delta:
                    shift = -pixel_delta
                else:
                    steps = max(1, int(round(abs(angle_delta) / 120)))
                    shift = (-1 if angle_delta > 0 else 1) * bar.singleStep() * steps
                next_value = max(bar.minimum(), min(bar.maximum(), bar.value() + shift))
                bar.setValue(next_value)
        event.accept()
        return True

    def _replace_surgeons(self, surgeons: list[str]) -> None:
        for widget, _combo in list(self._surgeon_rows):
            button = getattr(widget, "_remove_button", None)
            self.surgery_team_layout.removeWidget(widget)
            if button is not None:
                self.surgery_team_layout.removeWidget(button)
                button.deleteLater()
            widget.deleteLater()
        self._surgeon_rows.clear()
        for surgeon in surgeons or [""]:
            self._add_surgeon_row(surgeon)

    @staticmethod
    def _expanded_birth_year(year_text: str) -> int | None:
        text = "".join(ch for ch in str(year_text or "") if ch.isdigit())
        if not text:
            return None
        if len(text) <= 2:
            year_num = int(text)
            pivot = datetime.now().year % 100
            return (1900 if year_num > pivot else 2000) + year_num
        if len(text) == 4:
            return int(text)
        return None

    @classmethod
    def _date_from_birth_parts(cls, day_text: str, month_text: str, year_text: str) -> date | None:
        year = cls._expanded_birth_year(year_text)
        if year is None:
            return None
        try:
            return date(year, int(day_text), int(month_text))
        except ValueError:
            return None

    @classmethod
    def _parse_birth_date_text(cls, text: str) -> date | None:
        raw_text = str(text or "").strip()
        if not raw_text:
            return None

        direct = parse_date_value(raw_text)
        if direct is not None:
            return direct

        normalized = normalize_operblock_birth_date_text(raw_text, final=True)
        parsed = parse_date_value(normalized)
        if parsed is not None:
            return parsed

        groups = re.findall(r"\d+", raw_text)
        candidates: list[tuple[str, str, str]] = []
        if len(groups) == 3:
            candidates.append((groups[0], groups[1], groups[2]))

        digits = "".join(ch for ch in raw_text if ch.isdigit())
        if len(digits) == 8:
            candidates.append((digits[:2], digits[2:4], digits[4:]))
        elif len(digits) == 7:
            candidates.extend(
                (
                    (digits[:2], digits[2:3], digits[3:]),
                    (digits[:1], digits[1:3], digits[3:]),
                )
            )
        elif len(digits) == 6:
            candidates.append((digits[:2], digits[2:4], digits[4:]))
        elif len(digits) == 5:
            candidates.extend(
                (
                    (digits[:2], digits[2:3], digits[3:]),
                    (digits[:1], digits[1:3], digits[3:]),
                )
            )

        for day_text, month_text, year_text in candidates:
            candidate = cls._date_from_birth_parts(day_text, month_text, year_text)
            if candidate is not None:
                return candidate
        return None

    def _on_birth_date_text_edited(self, text: str):
        normalized = normalize_operblock_birth_date_text(text, final=False)
        if normalized == text:
            return
        old_cursor_pos = self.birth_date_input.cursorPosition()
        if old_cursor_pos >= len(text):
            cursor_pos = len(normalized)
        else:
            cursor_pos = min(old_cursor_pos + max(0, len(normalized) - len(text)), len(normalized))
        self.birth_date_input.blockSignals(True)
        self.birth_date_input.setText(normalized)
        self.birth_date_input.setCursorPosition(cursor_pos)
        self.birth_date_input.blockSignals(False)

    def _normalize_birth_date_field(self):
        text = self.birth_date_input.text().strip()
        if not text:
            return
        birth_date = self._parse_birth_date_text(text)
        if birth_date is None:
            normalized = normalize_operblock_birth_date_text(text, final=True)
            if normalized != text:
                self.birth_date_input.setText(normalized)
            return
        self.birth_date_input.setText(birth_date.strftime("%d.%m.%Y"))

    def set_data(self, data: dict) -> None:
        payload = data or {}
        self.handoff_id = (
            int(payload.get("handoff_id"))
            if payload.get("handoff_id") not in (None, "")
            else None
        )
        self.source_rao_admission_id = (
            int(payload.get("source_rao_admission_id"))
            if payload.get("source_rao_admission_id") not in (None, "")
            else None
        )
        self.history_input.setText(str(payload.get("history_number") or ""))
        self.full_name_input.setText(str(payload.get("full_name") or ""))
        self._set_combo_text(self.gender_combo, str(payload.get("gender") or ""))
        birth_date = parse_date_value(payload.get("birth_date"))
        if birth_date is not None:
            self.birth_date_input.setText(birth_date.strftime("%d.%m.%Y"))
        else:
            self.birth_date_input.clear()
        code = normalize_operblock_mkb_code(str(payload.get("diagnosis_code") or ""))
        self.diagnosis_code_input.setText(code)
        diagnosis_text = str(payload.get("diagnosis_text") or "").strip()
        if code:
            self._validate_mkb_code()
        if diagnosis_text and not self.diagnosis_text_input.text().strip():
            self._set_manual_diagnosis_enabled(True, text=diagnosis_text)
        elif diagnosis_text and not self.diagnosis_text_input.isReadOnly():
            self.diagnosis_text_input.setText(diagnosis_text)
        self.operation_name_input.setText(str(payload.get("operation_name") or ""))
        started_at = _parse_datetime_value(payload.get("started_at"))
        self.admission_time_input.set_bounds(
            payload.get("started_at_min"),
            payload.get("started_at_max"),
        )
        if started_at is not None:
            self.admission_time_input.set_datetime(started_at)
        can_edit_started_at = bool(payload.get("can_edit_started_at", True))
        self.admission_time_input.set_locked(
            not can_edit_started_at,
            str(payload.get("started_at_edit_lock_reason") or OPERBLOCK_STARTED_AT_LOCK_TOOLTIP),
        )
        self.anesthesia_assistance_type_combo.setEditText(
            normalize_operblock_anesthesia_type_label(payload.get("anesthesia_assistance_type"))
        )
        self.height_input.setText("" if payload.get("height_cm") in (None, "") else str(payload.get("height_cm")))
        weight = payload.get("weight_kg")
        self.weight_input.setText("" if weight in (None, "") else str(weight).replace(".", ","))
        self.allergies_input.setText(str(payload.get("allergies") or ""))
        self._set_fixed_combo_text(
            self.blood_group_combo,
            str(payload.get("blood_group") or ""),
            normalize_operblock_blood_group,
        )
        self._set_fixed_combo_text(
            self.blood_rh_combo,
            str(payload.get("blood_rh") or ""),
            normalize_operblock_blood_rh,
        )
        self._replace_surgeons([normalize_operblock_team_text(item) for item in payload.get("surgeons") or []])
        self._set_combo_text(self.operating_nurse_combo, str(payload.get("operating_nurse") or ""))
        self._set_combo_text(self.anesthesiologist_combo, str(payload.get("anesthesiologist") or ""))
        self._set_combo_text(self.anesthetist_combo, str(payload.get("anesthetist") or ""))
        department_profile = normalize_profile_department(
            payload.get("department_profile"),
            clear_legacy_operblock=True,
        )
        self.department_profile_combo.setEditText(department_profile)
        for edit, key in (
            (self.sys_input, "preop_sys"),
            (self.dia_input, "preop_dia"),
            (self.pulse_input, "preop_pulse"),
            (self.spo2_input, "preop_spo2"),
        ):
            value = payload.get(key)
            edit.setText("" if value in (None, "") else str(value))
    def _on_mkb_code_text_edited(self, text: str):
        normalized = normalize_operblock_mkb_code(text)
        self.diagnosis_code_input.blockSignals(True)
        self.diagnosis_code_input.setText(normalized)
        self.diagnosis_code_input.setCursorPosition(len(normalized))
        self.diagnosis_code_input.blockSignals(False)
        set_widget_style(self.diagnosis_code_input, "")
        self.diagnosis_name.setText("")
        if not is_complete_operblock_mkb_code(normalized):
            self._set_manual_diagnosis_enabled(False, clear=True, placeholder="Сначала введите полный код МКБ-10")

    def _set_manual_diagnosis_enabled(
        self,
        enabled: bool,
        *,
        clear: bool = False,
        text: str | None = None,
        placeholder: str = "Введите диагноз вручную",
    ):
        self.diagnosis_text_input.setEnabled(True)
        self.diagnosis_text_input.setReadOnly(not bool(enabled))
        set_widget_style(self.diagnosis_text_input, STYLE_PATIENT_FORM_MANUAL_FIELD if enabled else STYLE_PATIENT_FORM_READONLY_FIELD)
        self.diagnosis_text_input.setPlaceholderText(placeholder)
        if text is not None:
            self.diagnosis_text_input.setText(text)
        elif clear:
            self.diagnosis_text_input.clear()

    def _validate_mkb_code(self):
        code = normalize_operblock_mkb_code(self.diagnosis_code_input.text())
        self.diagnosis_code_input.blockSignals(True)
        self.diagnosis_code_input.setText(code)
        self.diagnosis_code_input.blockSignals(False)
        if not code:
            self.diagnosis_name.setText("")
            set_widget_style(self.diagnosis_code_input, "")
            self._set_manual_diagnosis_enabled(False, clear=True, placeholder="Сначала введите код МКБ-10")
            return False
        if not is_complete_operblock_mkb_code(code):
            self.diagnosis_name.setText("Формат кода: X33, S82.0 или S82.01")
            set_widget_style(self.diagnosis_code_input, STYLE_PATIENT_FORM_INVALID_FIELD)
            self._set_manual_diagnosis_enabled(False, clear=True, placeholder="Сначала введите полный код МКБ-10")
            return False
        name = self.mkb_service.get_diagnosis_by_code(code)
        if name:
            self.diagnosis_name.setText(name)
            set_widget_style(self.diagnosis_code_input, STYLE_PATIENT_FORM_VALID_FIELD)
            self._set_manual_diagnosis_enabled(False, text=name, placeholder="Диагноз из МКБ-10")
            return True
        else:
            self.diagnosis_name.setText("Код не найден")
            set_widget_style(self.diagnosis_code_input, STYLE_PATIENT_FORM_INVALID_FIELD)
            self._set_manual_diagnosis_enabled(True, clear=self.diagnosis_text_input.isReadOnly())
            return True

    def set_saving(self, saving: bool):
        self.form_page.setEnabled(not saving)
        self.cancel_button.setEnabled(not saving)
        self.close_button.setEnabled(not saving)
        self.save_button.setEnabled(not saving)
        self.save_button.setText("СОХРАНЕНИЕ..." if saving else self._save_button_text)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Wheel and self._is_occupy_combo_wheel_widget(watched):
            return self._scroll_occupy_form_wheel(event)
        if watched is getattr(self, "sys_input", None) and event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Slash:
                self.dia_input.setFocus(Qt.TabFocusReason)
                self.dia_input.selectAll()
                event.accept()
                return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if self.save_button.isEnabled():
                self.save_button.click()
            event.accept()
            return
        super().keyPressEvent(event)

    @staticmethod
    def _optional_int(text: str, label: str, minimum: int, maximum: int) -> int | None:
        value = str(text or "").strip()
        if not value:
            return None
        if not re.fullmatch(r"\d+", value):
            raise ValueError(f"{label}: укажите целое число.")
        number = int(value)
        if number < minimum or number > maximum:
            raise ValueError(f"{label}: допустимый диапазон {minimum}-{maximum}.")
        return number

    @staticmethod
    def _optional_weight(text: str) -> float | None:
        value = str(text or "").strip().replace(",", ".")
        if not value:
            return None
        try:
            number = float(Decimal(value))
        except (InvalidOperation, ValueError):
            raise ValueError("Вес: укажите число.") from None
        if number < 0.5 or number > 500:
            raise ValueError("Вес: допустимый диапазон 0.5-500.")
        return number

    def selected_surgeons(self) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for _widget, combo in self._surgeon_rows:
            name = normalize_operblock_team_text(combo.currentText())
            key = name.casefold()
            if name and key not in seen:
                seen.add(key)
                result.append(name)
        return result

    def _birth_date_value(self) -> date:
        text = self.birth_date_input.text().strip()
        if not text:
            raise ValueError("Укажите дату рождения.")
        birth_date = self._parse_birth_date_text(text)
        if birth_date is None:
            raise ValueError("Укажите корректную дату рождения.")
        min_birth_date = date(int(self.EMPTY_BIRTH_DATE.year()), int(self.EMPTY_BIRTH_DATE.month()), int(self.EMPTY_BIRTH_DATE.day()))
        if birth_date < min_birth_date:
            raise ValueError("Дата рождения не может быть раньше 01.01.1900.")
        if birth_date > date.today():
            raise ValueError("Дата рождения не может быть позже текущей даты.")
        self.birth_date_input.setText(birth_date.strftime("%d.%m.%Y"))
        return birth_date

    def get_data(self) -> dict:
        history_number = normalize_operblock_history_number(self.history_input.text())
        full_name = self.full_name_input.text().strip()
        if not full_name:
            raise ValueError("ФИО пациента не заполнено.")
        birth_date = self._birth_date_value()
        diagnosis_code = normalize_operblock_mkb_code(self.diagnosis_code_input.text())
        if not diagnosis_code:
            raise ValueError("Введите код МКБ-10.")
        if not is_complete_operblock_mkb_code(diagnosis_code):
            raise ValueError("Код МКБ-10 должен быть в формате X33, S82.0 или S82.01.")
        self._validate_mkb_code()
        diagnosis_text = self.diagnosis_text_input.text().strip()
        if not diagnosis_text:
            raise ValueError("Диагноз не заполнен. Если код МКБ-10 не найден, заполните ручной ввод.")
        preop_sys = self._optional_int(self.sys_input.text(), "АД систолическое", 0, 300)
        preop_dia = self._optional_int(self.dia_input.text(), "АД диастолическое", 0, 300)
        if (preop_sys is None) ^ (preop_dia is None):
            raise ValueError("АД: заполните систолическое и диастолическое значения.")
        if preop_sys is not None and preop_dia is not None and preop_dia > preop_sys:
            raise ValueError("АД диастолическое не может быть выше систолического.")
        preop_pulse = self._optional_int(self.pulse_input.text(), "ЧСС", 0, 300)
        preop_spo2 = self._optional_int(self.spo2_input.text(), "SpO₂", 0, 100)
        if any(value is not None for value in (preop_sys, preop_dia, preop_pulse, preop_spo2)) and any(
            value is None for value in (preop_sys, preop_dia, preop_pulse, preop_spo2)
        ):
            raise ValueError("Исходные витальные показатели заполните полностью: АД, ЧСС и SpO₂.")
        return {
            "table_code": self.table_code,
            "operation_case_id": self.operation_case_id,
            "history_number": history_number,
            "full_name": full_name,
            "gender": self.gender_combo.currentText(),
            "birth_date": birth_date,
            "started_at": self.admission_time_input.datetime_text(),
            "diagnosis_code": diagnosis_code or None,
            "diagnosis_text": diagnosis_text,
            "department_profile": normalize_profile_department(self.department_profile_combo.currentText()),
            "operation_name": normalize_operblock_team_text(self.operation_name_input.text()),
            "anesthesia_assistance_type": normalize_operblock_anesthesia_type_label(
                self.anesthesia_assistance_type_combo.currentText()
            ),
            "height_cm": self._optional_int(self.height_input.text(), "Рост", 1, 260),
            "weight_kg": self._optional_weight(self.weight_input.text()),
            "allergies": normalize_operblock_team_text(self.allergies_input.text()),
            "blood_group": normalize_operblock_blood_group(self.blood_group_combo.currentData()),
            "blood_rh": normalize_operblock_blood_rh(self.blood_rh_combo.currentData()),
            "surgeons": self.selected_surgeons(),
            "operating_nurse": normalize_operblock_team_text(self.operating_nurse_combo.currentText()),
            "anesthesiologist": normalize_operblock_team_text(self.anesthesiologist_combo.currentText()),
            "anesthetist": normalize_operblock_team_text(self.anesthetist_combo.currentText()),
            "preop_sys": preop_sys,
            "preop_dia": preop_dia,
            "preop_pulse": preop_pulse,
            "preop_spo2": preop_spo2,
            "handoff_id": self.handoff_id,
            "source_rao_admission_id": self.source_rao_admission_id,
        }
