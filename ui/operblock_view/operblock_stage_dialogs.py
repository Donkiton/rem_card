from __future__ import annotations
from rem_card.ui.styles.theme_runtime import set_widget_style

from datetime import datetime, timedelta
import re

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from rem_card.services.operblock_service import (
    normalize_operblock_transfer_department,
)
from rem_card.services.operblock_anesthesia_types import (
    normalize_operblock_anesthesia_type_label,
)
from rem_card.services.operblock_team import (
    normalize_operblock_team_text,
)
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.styles.theme import (
    BORDER_COLOR,
    BORDER_LIGHT,
    COLOR_PRIMARY_DARK,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)
from rem_card.ui.operblock_view.operblock_control_styles import (
    operblock_arrow_button_style,
    operblock_combo_box_style as _operblock_combo_box_style,
    operblock_vertical_scrollbar_style as _operblock_vertical_scrollbar_style,
)

from rem_card.ui.operblock_view.operblock_visual_primitives import (
    DANGER_BUTTON_STYLE,
    OPERBLOCK_DIALOG_CANCEL_BUTTON_STYLE,
    OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE,
    OperBlockClickableLabel,
)
from rem_card.ui.operblock_view.operblock_helpers import (
    _parse_datetime_value,
    _minute_floor_dt,
    _safe_int,
    _format_order_time,
)
from rem_card.ui.operblock_view.operblock_dialog_base import (
    OperBlockStyledDialog,
)
from rem_card.ui.operblock_view.operblock_medication_order_dialogs import (
    _gas_time_step_icon,
)
from rem_card.ui.operblock_view.operblock_medication_edit_dialogs import (
    OperationStageTimeEditDialog,
)

class OperBlockDialogTimeInput(QFrame):
    def __init__(
        self,
        initial_datetime: datetime | None = None,
        parent=None,
        *,
        min_datetime: datetime | None = None,
        max_datetime: datetime | None = None,
        object_prefix: str = "OperBlockDialogTime",
    ):
        super().__init__(parent)
        self._object_prefix = re.sub(r"\W+", "", str(object_prefix or "OperBlockDialogTime")) or "OperBlockDialogTime"
        self._start_datetime = _minute_floor_dt(initial_datetime) or datetime.now().replace(second=0, microsecond=0)
        self._time_min_datetime = _minute_floor_dt(min_datetime)
        self._time_max_datetime = _minute_floor_dt(max_datetime)
        if self._time_min_datetime and self._time_max_datetime and self._time_max_datetime < self._time_min_datetime:
            self._time_max_datetime = None
        self._time_text_updating = False
        self._init_ui()

    def _init_ui(self) -> None:
        frame_name = f"{self._object_prefix}InputFrame"
        input_name = f"{self._object_prefix}Input"
        stepper_name = f"{self._object_prefix}StepperColumn"
        button_name = f"{self._object_prefix}StepButton"
        self.setObjectName(frame_name)
        self.setFixedHeight(52)
        self.setMinimumWidth(170)
        self.setMaximumWidth(240)
        self.setProperty("focused", False)
        set_widget_style(self, f"""
            QFrame#{frame_name} {{
                background-color: #FFFFFF;
                border: 1px solid #D1D5DB;
                border-radius: 8px;
            }}
            QFrame#{frame_name}[focused="true"] {{
                border: 1px solid #6366F1;
            }}
            QLineEdit#{input_name} {{
                background-color: transparent;
                color: #111827;
                border: none;
                font-size: 18px;
                font-weight: 400;
                padding: 0 14px;
                selection-background-color: #C7D2FE;
            }}
            QFrame#{stepper_name} {{
                background-color: transparent;
                border: none;
            }}
            """
            + operblock_arrow_button_style(f"QPushButton#{button_name}"))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.time_input = QLineEdit()
        self.time_input.setObjectName(input_name)
        start_dt = self._coerce_time_datetime(self._start_datetime)
        self.time_input.setText(f"{start_dt.hour:02d}:{start_dt.minute:02d}")
        self.time_input.setPlaceholderText("09:10")
        self.time_input.setMaxLength(5)
        self.time_input.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self.time_input.textEdited.connect(self._on_time_text_edited)
        self.time_input.editingFinished.connect(self._commit_time_text)
        self.time_input.installEventFilter(self)
        layout.addWidget(self.time_input, 1)

        stepper = QFrame()
        stepper.setObjectName(stepper_name)
        stepper.setFixedWidth(42)
        stepper_layout = QVBoxLayout(stepper)
        stepper_layout.setContentsMargins(6, 4, 6, 4)
        stepper_layout.setSpacing(4)

        up_button = QPushButton()
        up_button.setObjectName(button_name)
        up_button.setFixedSize(30, 20)
        up_button.setIcon(_gas_time_step_icon(up=True))
        up_button.setIconSize(QSize(14, 14))
        up_button.setCursor(Qt.PointingHandCursor)
        up_button.clicked.connect(lambda _=False: self._step_time(1))
        down_button = QPushButton()
        down_button.setObjectName(button_name)
        down_button.setFixedSize(30, 20)
        down_button.setIcon(_gas_time_step_icon(up=False))
        down_button.setIconSize(QSize(14, 14))
        down_button.setCursor(Qt.PointingHandCursor)
        down_button.clicked.connect(lambda _=False: self._step_time(-1))
        stepper_layout.addWidget(up_button)
        stepper_layout.addWidget(down_button)
        layout.addWidget(stepper, 0)

    def eventFilter(self, obj, event):
        if obj is getattr(self, "time_input", None):
            if event.type() == QEvent.FocusIn:
                self._set_time_focus(True)
            elif event.type() == QEvent.FocusOut:
                self._set_time_focus(False)
        return super().eventFilter(obj, event)

    def _set_time_focus(self, focused: bool) -> None:
        self.setProperty("focused", bool(focused))
        self.style().unpolish(self)
        self.style().polish(self)

    def _on_time_text_edited(self, text: str) -> None:
        if self._time_text_updating:
            return
        digits = re.sub(r"\D", "", str(text or ""))
        if len(digits) < 4:
            return
        event_dt = self._time_datetime_from_text(digits[:4])
        if event_dt is None:
            return
        self._set_time_input_text(self._time_text_from_datetime(self._coerce_time_datetime(event_dt)), select_all=False)

    def _commit_time_text(self) -> str:
        raw_text = self.time_input.text()
        event_dt = self._time_datetime_from_text(raw_text)
        if event_dt is None:
            event_dt = self._fallback_time_datetime()
        normalized = self._time_text_from_datetime(self._coerce_time_datetime(event_dt))
        self._set_time_input_text(normalized, select_all=False)
        return normalized

    def _step_time(self, delta_minutes: int) -> None:
        current_dt = self._time_datetime_from_text(self.time_input.text())
        if current_dt is None:
            current_dt = self._fallback_time_datetime()
        stepped = self._coerce_time_datetime(current_dt + timedelta(minutes=int(delta_minutes)))
        self._set_time_input_text(self._time_text_from_datetime(stepped), select_all=True)

    def _set_time_input_text(self, text: str, *, select_all: bool) -> None:
        self._time_text_updating = True
        try:
            self.time_input.setText(text)
            if select_all:
                self.time_input.setFocus(Qt.OtherFocusReason)
                self.time_input.selectAll()
            else:
                self.time_input.setCursorPosition(len(text))
        finally:
            self._time_text_updating = False

    def _fallback_time_datetime(self) -> datetime:
        return self._coerce_time_datetime(self._start_datetime)

    def _time_datetime_from_text(self, value: str) -> datetime | None:
        minutes = OperationStageTimeEditDialog._time_minutes_from_text(value)
        if minutes is None:
            return None
        hour = minutes // 60
        minute = minutes % 60
        base_dt = self._start_datetime or self._time_min_datetime or datetime.now().replace(second=0, microsecond=0)
        same_day = datetime.combine(base_dt.date(), datetime.min.time()).replace(hour=hour, minute=minute)
        candidates = [same_day]
        if hour < 6:
            candidates.append(same_day + timedelta(days=1))
        previous_day_is_plausible = (
            base_dt.hour < 6
            or (self._time_min_datetime and self._time_min_datetime.date() < base_dt.date())
        )
        if hour >= 12 and previous_day_is_plausible:
            candidates.append(same_day - timedelta(days=1))

        def in_bounds(candidate: datetime) -> bool:
            if self._time_min_datetime and candidate < self._time_min_datetime:
                return False
            if self._time_max_datetime and candidate > self._time_max_datetime:
                return False
            return True

        bounded = [candidate for candidate in candidates if in_bounds(candidate)]
        source = bounded or candidates
        return min(source, key=lambda candidate: abs((candidate - base_dt).total_seconds()))

    def _coerce_time_datetime(self, value: datetime) -> datetime:
        event_dt = _minute_floor_dt(value) or datetime.now().replace(second=0, microsecond=0)
        if self._time_min_datetime and event_dt < self._time_min_datetime:
            return self._time_min_datetime
        if self._time_max_datetime and event_dt > self._time_max_datetime:
            return self._time_max_datetime
        return event_dt

    @staticmethod
    def _time_text_from_datetime(value: datetime) -> str:
        return f"{value.hour:02d}:{value.minute:02d}"

    def datetime_text(self) -> str:
        selected_text = self._commit_time_text()
        selected_dt = self._time_datetime_from_text(selected_text) or self._fallback_time_datetime()
        return self._coerce_time_datetime(selected_dt).isoformat(timespec="seconds")


class StartAnesthesiaDialog(OperBlockStyledDialog):
    def __init__(
        self,
        anesthesia_types: list[dict],
        anesthesiologists: list[str] | None = None,
        anesthetists: list[str] | None = None,
        parent=None,
        *,
        initial_assistance_type: str = "",
        initial_anesthesiologist: str = "",
        initial_anesthetist: str = "",
        initial_start_datetime: datetime | None = None,
        min_start_datetime: datetime | None = None,
        max_start_datetime: datetime | None = None,
    ):
        self._start_datetime = _minute_floor_dt(initial_start_datetime) or datetime.now().replace(second=0, microsecond=0)
        self._time_min_datetime = _minute_floor_dt(min_start_datetime)
        self._time_max_datetime = _minute_floor_dt(max_start_datetime)
        if self._time_min_datetime and self._time_max_datetime and self._time_max_datetime < self._time_min_datetime:
            self._time_max_datetime = None
        super().__init__(
            "Начать пособие",
            "start_anesthesia_dialog_geometry",
            parent,
            minimum_size=(560, 330),
            initial_size=(660, 380),
        )
        self._init_ui(anesthesia_types, anesthesiologists or [], anesthetists or [])
        if initial_assistance_type:
            self.assistance_combo.setEditText(normalize_operblock_anesthesia_type_label(initial_assistance_type))
        if initial_anesthesiologist:
            self.anesthesiologist_combo.setEditText(normalize_operblock_team_text(initial_anesthesiologist))
        if initial_anesthetist:
            self.anesthetist_combo.setEditText(normalize_operblock_team_text(initial_anesthetist))
        self._finalize_dialog_chrome()

    def _init_ui(self, anesthesia_types: list[dict], anesthesiologists: list[str], anesthetists: list[str]):
        layout = self.content_layout

        def add_labeled_row(caption: str, widget: QWidget, *, expand: bool = True) -> None:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(10)
            label = QLabel(caption)
            label.setFixedWidth(120)
            label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            row.addWidget(label)
            row.addWidget(widget, 1 if expand else 0)
            if not expand:
                row.addStretch(1)
            layout.addLayout(row)

        self.assistance_combo = QComboBox()
        self.assistance_combo.setEditable(True)
        self.assistance_combo.setMinimumWidth(260)
        self.assistance_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.assistance_combo.setMinimumContentsLength(38)
        self.assistance_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        set_widget_style(self.assistance_combo, _operblock_combo_box_style())
        line_edit = self.assistance_combo.lineEdit()
        if line_edit is not None:
            line_edit.setPlaceholderText("Не выбрано")
        for item in anesthesia_types or []:
            label = normalize_operblock_anesthesia_type_label((item or {}).get("label"))
            if label:
                self.assistance_combo.addItem(label, label)
        self.assistance_combo.setCurrentIndex(-1)
        self.assistance_combo.setEditText("")
        add_labeled_row("Вид пособия:", self.assistance_combo)

        self.anesthesiologist_combo = self._staff_combo(anesthesiologists)
        self.anesthesiologist_combo.setMinimumWidth(260)
        self.anesthesiologist_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        add_labeled_row("Анестезиолог:", self.anesthesiologist_combo)

        self.anesthetist_combo = self._staff_combo(anesthetists)
        self.anesthetist_combo.setMinimumWidth(260)
        self.anesthetist_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        add_labeled_row("Анестезист:", self.anesthetist_combo)

        self.start_time_input = OperBlockDialogTimeInput(
            self._start_datetime,
            self,
            min_datetime=self._time_min_datetime,
            max_datetime=self._time_max_datetime,
            object_prefix="StartAnesthesiaTime",
        )
        self.time_frame = self.start_time_input
        self.time_input = self.start_time_input.time_input
        add_labeled_row("Время начала:", self.start_time_input, expand=False)

        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel_button = QPushButton("Отмена")
        cancel_button.setMinimumHeight(34)
        set_widget_style(cancel_button, OPERBLOCK_DIALOG_CANCEL_BUTTON_STYLE)
        cancel_button.clicked.connect(self.reject)
        self.start_button = QPushButton("Начать")
        self.start_button.setMinimumHeight(34)
        set_widget_style(self.start_button, OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE)
        self.start_button.clicked.connect(self.accept)
        self._configure_enter_accept_button(cancel_button, self.start_button)
        footer.addWidget(cancel_button)
        footer.addWidget(self.start_button)
        layout.addStretch(1)
        layout.addLayout(footer)

    @staticmethod
    def _apply_staff_combo_popup_scrollbar(combo: QComboBox) -> None:
        view = combo.view()
        if view is None:
            return
        view.setObjectName("OperBlockStaffComboPopup")
        scrollbar = view.verticalScrollBar()
        if scrollbar is None:
            return
        scrollbar.setObjectName("OperBlockStaffComboPopupScrollBar")
        scrollbar.setFixedWidth(14)
        scrollbar.setSingleStep(36)
        scrollbar.setPageStep(144)
        set_widget_style(scrollbar, _operblock_vertical_scrollbar_style(
                "OperBlockStaffComboPopupScrollBar",
                width_px=14,
                left_margin_px=2,
                right_margin_px=1,
            ))

    @staticmethod
    def _staff_combo(items: list[str]) -> QComboBox:
        combo = QComboBox()
        combo.setEditable(True)
        combo.setFixedHeight(36)
        combo.setMinimumWidth(430)
        combo.setMinimumContentsLength(38)
        combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        set_widget_style(combo, _operblock_combo_box_style())
        StartAnesthesiaDialog._apply_staff_combo_popup_scrollbar(combo)
        line_edit = combo.lineEdit()
        if line_edit is not None:
            line_edit.setPlaceholderText("Не выбрано")
        seen: set[str] = set()
        for item in items or []:
            label = normalize_operblock_team_text(item)
            key = label.casefold()
            if label and key not in seen:
                seen.add(key)
                combo.addItem(label, label)
        combo.setCurrentIndex(-1)
        combo.setEditText("")
        return combo

    def selected_assistance_type(self) -> str:
        return normalize_operblock_anesthesia_type_label(self.assistance_combo.currentText())

    def selected_anesthesiologist(self) -> str:
        return normalize_operblock_team_text(self.anesthesiologist_combo.currentText())

    def selected_anesthetist(self) -> str:
        return normalize_operblock_team_text(self.anesthetist_combo.currentText())

    def start_datetime_text(self) -> str:
        return self.start_time_input.datetime_text()

    def accept(self) -> None:
        if not self.selected_assistance_type():
            CustomMessageBox.warning(self, "Начать пособие", "Укажите вид пособия.")
            return
        self.start_time_input.datetime_text()
        super().accept()


class EndSurgeryDialog(OperBlockStyledDialog):
    def __init__(
        self,
        parent=None,
        *,
        initial_end_datetime: datetime | None = None,
        min_end_datetime: datetime | None = None,
        max_end_datetime: datetime | None = None,
    ):
        super().__init__(
            "Завершить операцию",
            "end_surgery_dialog_geometry",
            parent,
            minimum_size=(430, 190),
            initial_size=(520, 230),
        )
        self._init_ui(initial_end_datetime, min_end_datetime, max_end_datetime)
        self._finalize_dialog_chrome()

    def _init_ui(
        self,
        initial_end_datetime: datetime | None,
        min_end_datetime: datetime | None,
        max_end_datetime: datetime | None,
    ):
        layout = self.content_layout
        label_width = 120
        row_spacing = 10
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(row_spacing)
        label = QLabel("Время завершения:")
        label.setFixedWidth(label_width)
        label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.end_time_input = OperBlockDialogTimeInput(
            initial_end_datetime,
            self,
            min_datetime=min_end_datetime,
            max_datetime=max_end_datetime,
            object_prefix="EndSurgeryTime",
        )
        row.addWidget(label)
        row.addWidget(self.end_time_input, 0)
        row.addStretch(1)
        layout.addLayout(row)

        footer_frame = QFrame()
        footer_frame.setObjectName("EndSurgeryDialogFooter")
        set_widget_style(footer_frame, "QFrame#EndSurgeryDialogFooter { background: transparent; border: none; }")
        footer_frame.setFixedWidth(label_width + row_spacing + self.end_time_input.maximumWidth())
        footer_frame.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        footer = QHBoxLayout(footer_frame)
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(row_spacing)
        footer.addStretch(1)
        cancel_button = QPushButton("Отмена")
        cancel_button.setMinimumHeight(34)
        set_widget_style(cancel_button, OPERBLOCK_DIALOG_CANCEL_BUTTON_STYLE)
        cancel_button.clicked.connect(self.reject)
        self.finish_button = QPushButton("Завершить")
        self.finish_button.setMinimumHeight(34)
        set_widget_style(self.finish_button, DANGER_BUTTON_STYLE)
        self.finish_button.clicked.connect(self.accept)
        self._configure_enter_accept_button(cancel_button, self.finish_button)
        footer.addWidget(cancel_button)
        footer.addWidget(self.finish_button)
        layout.addStretch(1)
        layout.addWidget(footer_frame, 0, Qt.AlignLeft)

    def end_datetime_text(self) -> str:
        return self.end_time_input.datetime_text()


class EndAnesthesiaTransferDialog(OperBlockStyledDialog):
    def __init__(
        self,
        departments: list[str],
        parent=None,
        *,
        initial_department: str = "",
        initial_end_datetime: datetime | None = None,
        min_end_datetime: datetime | None = None,
        max_end_datetime: datetime | None = None,
    ):
        super().__init__(
            "Завершить пособие",
            "end_anesthesia_transfer_dialog_geometry",
            parent,
            minimum_size=(520, 260),
            initial_size=(620, 300),
        )
        self._init_ui(departments, initial_department, initial_end_datetime, min_end_datetime, max_end_datetime)
        self._finalize_dialog_chrome()

    def _init_ui(
        self,
        departments: list[str],
        initial_department: str,
        initial_end_datetime: datetime | None,
        min_end_datetime: datetime | None,
        max_end_datetime: datetime | None,
    ):
        layout = self.content_layout
        time_row = QHBoxLayout()
        time_row.setContentsMargins(0, 0, 0, 0)
        time_row.setSpacing(10)
        time_label = QLabel("Время завершения:")
        time_label.setFixedWidth(120)
        time_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        time_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.end_time_input = OperBlockDialogTimeInput(
            initial_end_datetime,
            self,
            min_datetime=min_end_datetime,
            max_datetime=max_end_datetime,
            object_prefix="EndAnesthesiaTime",
        )
        time_row.addWidget(time_label)
        time_row.addWidget(self.end_time_input, 0)
        time_row.addStretch(1)
        layout.addLayout(time_row)

        transfer_row = QHBoxLayout()
        transfer_row.setContentsMargins(0, 0, 0, 0)
        transfer_row.setSpacing(10)
        transfer_label = QLabel("Переводится в:")
        transfer_label.setFixedWidth(120)
        transfer_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        transfer_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.department_combo = QComboBox()
        self.department_combo.setEditable(True)
        self.department_combo.setFixedHeight(36)
        self.department_combo.setMinimumWidth(260)
        self.department_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.department_combo.setSizeAdjustPolicy(QComboBox.AdjustToContentsOnFirstShow)
        set_widget_style(self.department_combo, _operblock_combo_box_style())
        line_edit = self.department_combo.lineEdit()
        if line_edit is not None:
            line_edit.setPlaceholderText("Куда переводится пациент")

        seen: set[str] = set()
        for department in departments or []:
            label = normalize_operblock_transfer_department(department)
            key = label.casefold()
            if label and key not in seen:
                seen.add(key)
                self.department_combo.addItem(label, label)
        self.department_combo.setCurrentIndex(-1)
        self.department_combo.setEditText(normalize_operblock_transfer_department(initial_department))
        transfer_row.addWidget(transfer_label)
        transfer_row.addWidget(self.department_combo, 1)
        layout.addLayout(transfer_row)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(10)
        footer.addStretch(1)
        cancel_button = QPushButton("Отмена")
        cancel_button.setMinimumHeight(34)
        set_widget_style(cancel_button, OPERBLOCK_DIALOG_CANCEL_BUTTON_STYLE)
        cancel_button.clicked.connect(self.reject)
        self.finish_button = QPushButton("Завершить")
        self.finish_button.setMinimumHeight(34)
        set_widget_style(self.finish_button, DANGER_BUTTON_STYLE)
        self.finish_button.clicked.connect(self.accept)
        self._configure_enter_accept_button(cancel_button, self.finish_button)
        footer.addWidget(cancel_button)
        footer.addWidget(self.finish_button)
        layout.addStretch(1)
        layout.addLayout(footer)

    def selected_department(self) -> str:
        return normalize_operblock_transfer_department(self.department_combo.currentText())

    def end_datetime_text(self) -> str:
        return self.end_time_input.datetime_text()

    def accept(self) -> None:
        if not self.selected_department():
            CustomMessageBox.warning(self, "Завершить пособие", "Укажите, куда переводится пациент.")
            return
        super().accept()


class StartSurgeryDialog(OperBlockStyledDialog):
    def __init__(
        self,
        surgeons: list[str] | None = None,
        operating_nurses: list[str] | None = None,
        parent=None,
        *,
        initial_operation_name: str = "",
        initial_surgeons: list[str] | None = None,
        initial_operating_nurse: str = "",
        initial_start_datetime: datetime | None = None,
        min_start_datetime: datetime | None = None,
        max_start_datetime: datetime | None = None,
    ):
        self._start_datetime = _minute_floor_dt(initial_start_datetime) or datetime.now().replace(second=0, microsecond=0)
        self._time_min_datetime = _minute_floor_dt(min_start_datetime)
        self._time_max_datetime = _minute_floor_dt(max_start_datetime)
        if self._time_min_datetime and self._time_max_datetime and self._time_max_datetime < self._time_min_datetime:
            self._time_max_datetime = None
        super().__init__(
            "Начать операцию",
            "start_surgery_dialog_geometry",
            parent,
            minimum_size=(560, 390),
            initial_size=(660, 460),
        )
        self._surgeon_options = list(surgeons or [])
        self._surgeon_combos: list[QComboBox] = []
        self._syncing_surgeon_fields = False
        self._initial_operation_name = normalize_operblock_team_text(initial_operation_name)
        self._initial_surgeons = [
            normalize_operblock_team_text(item)
            for item in (initial_surgeons or [])
            if normalize_operblock_team_text(item)
        ]
        self._initial_operating_nurse = normalize_operblock_team_text(initial_operating_nurse)
        self._init_ui(surgeons or [], operating_nurses or [])
        self._finalize_dialog_chrome()

    def _init_ui(self, surgeons: list[str], operating_nurses: list[str]):
        layout = self.content_layout
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        self.operation_name_edit = QLineEdit()
        self.operation_name_edit.setMinimumWidth(430)
        self.operation_name_edit.setPlaceholderText("Название операции")
        if self._initial_operation_name:
            self.operation_name_edit.setText(self._initial_operation_name)
        form.addRow("Название операции:", self.operation_name_edit)

        self.start_time_input = OperBlockDialogTimeInput(
            self._start_datetime,
            self,
            min_datetime=self._time_min_datetime,
            max_datetime=self._time_max_datetime,
            object_prefix="StartSurgeryTime",
        )
        self.time_input = self.start_time_input.time_input
        form.addRow("Время начала:", self.start_time_input)

        self.surgeons_scroll = QScrollArea()
        self.surgeons_scroll.setObjectName("OperBlockSurgeryTeamScroll")
        self.surgeons_scroll.setWidgetResizable(True)
        self.surgeons_scroll.setFrameShape(QFrame.NoFrame)
        self.surgeons_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.surgeons_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.surgeons_scroll.setMinimumHeight(190)
        self.surgeons_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        surgeons_scrollbar = self.surgeons_scroll.verticalScrollBar()
        surgeons_scrollbar.setObjectName("OperBlockSurgeryTeamScrollBar")
        surgeons_scrollbar.setFixedWidth(14)
        surgeons_scrollbar.setSingleStep(36)
        surgeons_scrollbar.setPageStep(108)
        set_widget_style(surgeons_scrollbar, _operblock_vertical_scrollbar_style(
                "OperBlockSurgeryTeamScrollBar",
                width_px=14,
                left_margin_px=3,
                right_margin_px=2,
            ))

        self.surgeons_widget = QWidget()
        self.surgeons_widget.setObjectName("OperBlockSurgeryTeamContent")
        set_widget_style(self.surgeons_widget, "QWidget#OperBlockSurgeryTeamContent { background: transparent; border: none; }")
        self.surgeons_layout = QFormLayout(self.surgeons_widget)
        self.surgeons_layout.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        self.surgeons_layout.setContentsMargins(0, 0, 0, 0)
        self.surgeons_layout.setHorizontalSpacing(10)
        self.surgeons_layout.setVerticalSpacing(6)
        if self._initial_surgeons:
            for surgeon in self._initial_surgeons:
                self._add_surgeon_combo(surgeon)
        else:
            self._add_surgeon_combo()
        self.operating_nurse_combo = StartAnesthesiaDialog._staff_combo(operating_nurses)
        if self._initial_operating_nurse:
            self.operating_nurse_combo.setEditText(self._initial_operating_nurse)
        self._install_surgery_team_combo_event_filter(self.operating_nurse_combo)
        self.surgeons_layout.addRow("Опер. сестра:", self.operating_nurse_combo)
        self.surgeons_scroll.setWidget(self.surgeons_widget)
        set_widget_style(self.surgeons_scroll, """
            QScrollArea#OperBlockSurgeryTeamScroll {
                background: transparent;
                border: none;
            }
            QScrollArea#OperBlockSurgeryTeamScroll > QWidget > QWidget {
                background: transparent;
            }
            """)
        form.addRow(self.surgeons_scroll)
        layout.addLayout(form, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel_button = QPushButton("Отмена")
        cancel_button.setMinimumHeight(34)
        set_widget_style(cancel_button, OPERBLOCK_DIALOG_CANCEL_BUTTON_STYLE)
        cancel_button.clicked.connect(self.reject)
        self.start_button = QPushButton("Начать")
        self.start_button.setMinimumHeight(34)
        set_widget_style(self.start_button, OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE)
        self.start_button.clicked.connect(self.accept)
        self._configure_enter_accept_button(cancel_button, self.start_button)
        footer.addWidget(cancel_button)
        footer.addWidget(self.start_button)
        layout.addLayout(footer)

    def _add_surgeon_combo(self, text: str = "") -> QComboBox:
        combo = StartAnesthesiaDialog._staff_combo(self._surgeon_options)
        if text:
            combo.setEditText(text)
        combo.currentTextChanged.connect(lambda *_args: self._sync_surgeon_fields())
        self._install_surgery_team_combo_event_filter(combo)
        insert_row = self._operating_nurse_row()
        self._surgeon_combos.append(combo)
        self.surgeons_layout.insertRow(insert_row, "Хирург:" if len(self._surgeon_combos) == 1 else "", combo)
        self._refresh_surgeon_row_labels()
        self._scroll_surgeons_to_bottom_later()
        return combo

    def _install_surgery_team_combo_event_filter(self, combo: QComboBox) -> None:
        combo.installEventFilter(self)
        line_edit = combo.lineEdit()
        if line_edit is not None:
            line_edit.installEventFilter(self)

    def _operating_nurse_row(self) -> int:
        combo = getattr(self, "operating_nurse_combo", None)
        if combo is None:
            return self.surgeons_layout.rowCount()
        row, _role = self.surgeons_layout.getWidgetPosition(combo)
        if row < 0:
            return self.surgeons_layout.rowCount()
        return row

    def _refresh_surgeon_row_labels(self) -> None:
        for index, combo in enumerate(self._surgeon_combos):
            label = self.surgeons_layout.labelForField(combo)
            if isinstance(label, QLabel):
                label.setText("Хирург:" if index == 0 else "")

    def _scroll_surgeons_to_bottom_later(self) -> None:
        scroll = getattr(self, "surgeons_scroll", None)
        if scroll is None:
            return

        def scroll_to_bottom():
            try:
                bar = scroll.verticalScrollBar()
                bar.setValue(bar.maximum())
            except RuntimeError:
                return

        QTimer.singleShot(0, scroll_to_bottom)

    def _scroll_surgeon_combo_wheel(self, event) -> bool:
        scroll = getattr(self, "surgeons_scroll", None)
        if scroll is None:
            return False
        bar = scroll.verticalScrollBar()
        if bar.maximum() <= bar.minimum():
            return False
        pixel_delta = event.pixelDelta().y() if hasattr(event, "pixelDelta") else 0
        angle_delta = event.angleDelta().y() if hasattr(event, "angleDelta") else 0
        delta = pixel_delta or angle_delta
        if not delta:
            return False
        steps = max(1, int(round(abs(angle_delta) / 120))) if angle_delta else 1
        direction = -1 if delta > 0 else 1
        bar.setValue(max(bar.minimum(), min(bar.maximum(), bar.value() + direction * bar.singleStep() * steps)))
        event.accept()
        return True

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Wheel and self._is_surgery_team_wheel_widget(watched):
            if self._scroll_surgeon_combo_wheel(event):
                return True
        return super().eventFilter(watched, event)

    def _is_surgery_team_wheel_widget(self, watched) -> bool:
        team_combos = list(getattr(self, "_surgeon_combos", []))
        operating_nurse_combo = getattr(self, "operating_nurse_combo", None)
        if operating_nurse_combo is not None:
            team_combos.append(operating_nurse_combo)
        for combo in team_combos:
            if watched is combo:
                return True
            line_edit = combo.lineEdit()
            if line_edit is not None and watched is line_edit:
                return True
        return False

    def _sync_surgeon_fields(self) -> None:
        if self._syncing_surgeon_fields:
            return
        self._syncing_surgeon_fields = True
        try:
            if not self._surgeon_combos:
                self._add_surgeon_combo()

            if self._surgeon_combos and normalize_operblock_team_text(self._surgeon_combos[-1].currentText()):
                self._add_surgeon_combo()

            for combo in list(self._surgeon_combos[:-1]):
                if normalize_operblock_team_text(combo.currentText()):
                    continue
                self._surgeon_combos.remove(combo)
                self.surgeons_layout.removeRow(combo)

            if not self._surgeon_combos:
                self._add_surgeon_combo()
            self._refresh_surgeon_row_labels()
        finally:
            self._syncing_surgeon_fields = False

    def operation_name(self) -> str:
        return normalize_operblock_team_text(self.operation_name_edit.text())

    def selected_surgeons(self) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for combo in self._surgeon_combos:
            name = normalize_operblock_team_text(combo.currentText())
            key = name.casefold()
            if name and key not in seen:
                seen.add(key)
                result.append(name)
        return result

    def selected_surgeon(self) -> str:
        surgeons = self.selected_surgeons()
        return surgeons[0] if surgeons else ""

    def selected_operating_nurse(self) -> str:
        return normalize_operblock_team_text(self.operating_nurse_combo.currentText())

    def start_datetime_text(self) -> str:
        return self.start_time_input.datetime_text()

    def accept(self) -> None:
        if not self.operation_name():
            CustomMessageBox.warning(self, "Начать операцию", "Укажите название операции.")
            self.operation_name_edit.setFocus(Qt.OtherFocusReason)
            return
        self.start_datetime_text()
        super().accept()


class OperationStagesDialog(OperBlockStyledDialog):
    saveRequested = Signal(object)
    timeEditRequested = Signal(object)

    AUTO_STAGE_KINDS = {"anesthesia_start", "surgery_start"}
    CUSTOM_STAGE_KIND = "custom"

    def __init__(self, stages: list[dict], parent=None):
        super().__init__(
            "Этапы",
            "operation_stages_dialog_geometry",
            parent,
            minimum_size=(620, 380),
            initial_size=(760, 540),
        )
        self._rows: list[dict] = []
        self._row_widgets: dict[str, dict] = {}
        self._init_ui(stages)
        self._finalize_dialog_chrome()

    def _init_ui(self, stages: list[dict]):
        layout = self.content_layout
        layout.setSpacing(10)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("OperBlockOperationStagesScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scrollbar = self.scroll.verticalScrollBar()
        scrollbar.setObjectName("OperBlockOperationStagesScrollBar")
        scrollbar.setFixedWidth(14)
        scrollbar.setSingleStep(38)
        scrollbar.setPageStep(152)
        set_widget_style(scrollbar, _operblock_vertical_scrollbar_style(
                "OperBlockOperationStagesScrollBar",
                width_px=14,
                left_margin_px=3,
                right_margin_px=2,
            ))

        self.rows_widget = QWidget()
        self.rows_widget.setObjectName("OperBlockOperationStagesContent")
        set_widget_style(self.rows_widget, """
            QWidget#OperBlockOperationStagesContent {
                background: transparent;
                border: none;
            }
            """)
        self.rows_layout = QVBoxLayout(self.rows_widget)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(7)
        self.scroll.setWidget(self.rows_widget)
        set_widget_style(self.scroll, """
            QScrollArea#OperBlockOperationStagesScroll {
                background: transparent;
                border: none;
            }
            QScrollArea#OperBlockOperationStagesScroll > QWidget > QWidget {
                background: transparent;
            }
            """)
        layout.addWidget(self.scroll, 1)

        note_label = QLabel(
            "Начало пособия и начало операции добавляются автоматически. "
            "Конец операции и конец пособия также будут установлены автоматически."
        )
        note_label.setWordWrap(True)
        set_widget_style(note_label, f"font-size: 12px; color: {TEXT_SECONDARY}; background: transparent; border: none;")
        layout.addWidget(note_label, 0)

        footer = QHBoxLayout()
        footer.addStretch(1)
        close_button = QPushButton("Закрыть")
        close_button.setMinimumHeight(34)
        set_widget_style(close_button, OPERBLOCK_DIALOG_CANCEL_BUTTON_STYLE)
        close_button.clicked.connect(self.accept)
        footer.addWidget(close_button)
        layout.addLayout(footer)

        self.set_stages(stages)

    @staticmethod
    def _row_key(row: dict) -> str:
        event_id = _safe_int((row or {}).get("event_id") or (row or {}).get("source_id"))
        if event_id:
            return f"event:{event_id}"
        return "new"

    @staticmethod
    def _clean_label(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip())

    @classmethod
    def _stage_label(cls, row: dict) -> str:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        return cls._clean_label(
            row.get("label")
            or row.get("display_label")
            or row.get("raw_text")
            or (payload or {}).get("label")
        )

    @staticmethod
    def _stage_sort_key(row: dict) -> tuple[datetime, int]:
        return (
            _minute_floor_dt(_parse_datetime_value((row or {}).get("event_time"))) or datetime.max,
            _safe_int((row or {}).get("event_id") or (row or {}).get("source_id")) or 0,
        )

    @staticmethod
    def _default_new_stage_time() -> str:
        return datetime.now().replace(second=0, microsecond=0).isoformat(timespec="seconds")

    @classmethod
    def _normalized_stage_rows(cls, stages: list[dict]) -> list[dict]:
        rows = []
        seen_auto: set[str] = set()
        for item in stages or []:
            row = dict(item or {})
            kind = str(row.get("kind") or row.get("stage_kind") or "").strip()
            row["kind"] = kind
            row["label"] = cls._stage_label(row)
            row["event_id"] = _safe_int(row.get("event_id") or row.get("source_id"))
            row["revision"] = int(row.get("revision") or 0)
            if kind in cls.AUTO_STAGE_KINDS:
                if kind in seen_auto:
                    continue
                row["readonly"] = True
                seen_auto.add(kind)
                rows.append(row)
            elif kind == cls.CUSTOM_STAGE_KIND:
                row["readonly"] = False
                rows.append(row)
        rows.sort(key=cls._stage_sort_key)
        return rows

    def set_stages(self, stages: list[dict]) -> None:
        self._rows = self._normalized_stage_rows(stages)
        self._ensure_blank_row()
        self._render_rows()

    def _ensure_blank_row(self) -> None:
        self._rows = [row for row in self._rows if self._row_key(row) != "new"]
        self._rows.append(
            {
                "kind": self.CUSTOM_STAGE_KIND,
                "label": "",
                "event_id": None,
                "event_time": self._default_new_stage_time(),
                "revision": 0,
                "readonly": False,
                "new": True,
            }
        )

    def _clear_rows_layout(self) -> None:
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._row_widgets = {}

    def _render_rows(self) -> None:
        self._clear_rows_layout()
        for index, row in enumerate(self._rows, start=1):
            frame = self._create_row_widget(index, row)
            self.rows_layout.addWidget(frame)
        self.rows_layout.addStretch(1)

    def _create_row_widget(self, index: int, row: dict) -> QWidget:
        row_key = self._row_key(row)
        frame = QFrame()
        frame.setObjectName("OperBlockOperationStageRow")
        set_widget_style(frame, f"""
            QFrame#OperBlockOperationStageRow {{
                background: #ffffff;
                border: 1px solid {BORDER_LIGHT};
                border-radius: 6px;
            }}
            """)
        frame.setMinimumHeight(46)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(8)

        number_label = QLabel(str(index))
        number_label.setFixedWidth(28)
        number_label.setAlignment(Qt.AlignCenter)
        set_widget_style(number_label, f"font-size: 13px; font-weight: 800; color: {COLOR_PRIMARY_DARK};")
        layout.addWidget(number_label)

        can_edit_time = not bool(row.get("readonly")) and bool(row.get("event_time"))
        time_text = _format_order_time(row.get("event_time")) if row.get("event_time") else ""
        if can_edit_time:
            time_label = OperBlockClickableLabel(time_text, click_callback=lambda key=row_key: self._request_time_edit(key))
            time_label.setToolTip("Изменить время этапа")
        else:
            time_label = QLabel(time_text)
        time_label.setFixedWidth(46)
        time_label.setAlignment(Qt.AlignCenter)
        time_style = (
            f"font-size: 12px; color: {COLOR_PRIMARY_DARK}; font-weight: 700; text-decoration: underline;"
            if can_edit_time
            else f"font-size: 12px; color: {TEXT_SECONDARY};"
        )
        set_widget_style(time_label, time_style)
        layout.addWidget(time_label)

        edit = QLineEdit()
        edit.setText(str(row.get("label") or ""))
        edit.setPlaceholderText("Название этапа")
        edit.setMinimumHeight(32)
        edit.setReadOnly(bool(row.get("readonly")))
        edit.setProperty("row_key", row_key)
        set_widget_style(edit, f"""
            QLineEdit {{
                background: {'#f8fafc' if row.get('readonly') else '#ffffff'};
                border: 1px solid {BORDER_COLOR};
                border-radius: 5px;
                padding: 5px 8px;
                color: {TEXT_PRIMARY};
            }}
            QLineEdit:read-only {{
                color: {TEXT_SECONDARY};
            }}
            """)
        layout.addWidget(edit, 1)
        edit.returnPressed.connect(lambda key=row_key: self._request_save(key))

        save_button = QPushButton("Сохранить")
        save_button.setFixedWidth(104)
        save_button.setMinimumHeight(30)
        save_button.setCursor(Qt.PointingHandCursor)
        set_widget_style(save_button, OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE + "QPushButton { padding: 4px 8px; }")
        save_button.setProperty("row_key", row_key)
        save_button.clicked.connect(lambda _=False, key=row_key: self._request_save(key))
        layout.addWidget(save_button, 0)

        self._row_widgets[row_key] = {
            "frame": frame,
            "edit": edit,
            "button": save_button,
            "time_label": time_label,
            "row": row,
        }
        edit.textChanged.connect(lambda _text="", key=row_key: self._sync_row_button(key))
        self._sync_row_button(row_key, initial=True)
        return frame

    def _sync_row_button(self, row_key: str, *, initial: bool = False) -> None:
        widgets = self._row_widgets.get(row_key) or {}
        row = widgets.get("row") or {}
        button = widgets.get("button")
        edit = widgets.get("edit")
        if button is None or edit is None:
            return
        if row.get("readonly"):
            button.setText("Авто")
            button.setEnabled(False)
            return
        current = self._clean_label(edit.text())
        original = self._clean_label(row.get("label") or "")
        if row.get("new"):
            button.setText("Сохранить")
            button.setEnabled(bool(current))
            return
        if initial:
            button.setText("Сохранено")
            button.setEnabled(False)
            return
        button.setText("Сохранить")
        button.setEnabled(bool(current) and current != original)

    def _request_save(self, row_key: str) -> None:
        widgets = self._row_widgets.get(row_key) or {}
        row = dict(widgets.get("row") or {})
        edit = widgets.get("edit")
        button = widgets.get("button")
        if edit is None or button is None:
            return
        if row.get("readonly"):
            return
        label = self._clean_label(edit.text())
        if not label:
            CustomMessageBox.warning(self, "Этапы", "Укажите название этапа.")
            edit.setFocus(Qt.OtherFocusReason)
            return
        button.setText("Сохранение...")
        button.setEnabled(False)
        edit.setEnabled(False)
        self.saveRequested.emit(
            {
                "row_key": row_key,
                "event_id": _safe_int(row.get("event_id")),
                "expected_revision": int(row.get("revision") or 0),
                "label": label,
                "event_time": row.get("event_time"),
                "is_new": bool(row.get("new")),
            }
        )

    def _request_time_edit(self, row_key: str) -> None:
        widgets = self._row_widgets.get(row_key) or {}
        row = dict(widgets.get("row") or {})
        if row.get("readonly"):
            return
        event_id = _safe_int(row.get("event_id"))
        is_new = bool(row.get("new"))
        if not is_new and not event_id:
            return
        edit = widgets.get("edit")
        label = self._clean_label(edit.text()) if edit is not None else self._stage_label(row)
        if not label and not is_new:
            CustomMessageBox.warning(self, "Этапы", "Укажите название этапа.")
            if edit is not None:
                edit.setFocus(Qt.OtherFocusReason)
            return
        self.timeEditRequested.emit(
            {
                "row_key": row_key,
                "event_id": event_id,
                "expected_revision": int(row.get("revision") or 0),
                "label": label,
                "event_time": row.get("event_time"),
                "is_new": is_new,
            }
        )

    def apply_save_error(self, row_key: str) -> None:
        widgets = self._row_widgets.get(str(row_key or "")) or {}
        edit = widgets.get("edit")
        if edit is not None:
            edit.setEnabled(True)
        time_label = widgets.get("time_label")
        if time_label is not None:
            time_label.setEnabled(True)
        self._sync_row_button(str(row_key or ""))

    def apply_pending_stage_time(self, row_key: str, event_time: str) -> None:
        key = str(row_key or "")
        parsed = _minute_floor_dt(_parse_datetime_value(event_time))
        if parsed is None:
            return
        normalized = parsed.isoformat(timespec="seconds")
        for row in self._rows:
            if self._row_key(row) == key:
                row["event_time"] = normalized
                break
        widgets = self._row_widgets.get(key) or {}
        row = widgets.get("row")
        if isinstance(row, dict):
            row["event_time"] = normalized
        time_label = widgets.get("time_label")
        if time_label is not None:
            time_label.setText(_format_order_time(normalized))
            time_label.setEnabled(True)
        self._sync_row_button(key)

    def apply_saved_stage(self, row_key: str, stage: dict) -> None:
        event_id = _safe_int((stage or {}).get("source_id") or (stage or {}).get("event_id"))
        if not event_id:
            self.apply_save_error(row_key)
            return
        updated = {
            "kind": self.CUSTOM_STAGE_KIND,
            "label": self._stage_label(stage),
            "event_id": event_id,
            "event_time": (stage or {}).get("event_time"),
            "revision": int((stage or {}).get("revision") or 0),
            "readonly": False,
            "new": False,
            "payload": dict((stage or {}).get("payload") or {}),
        }
        if self._apply_saved_stage_in_place(row_key, updated):
            return
        replaced = False
        rows = []
        for row in self._rows:
            if self._row_key(row) == str(row_key or "") or _safe_int(row.get("event_id")) == event_id:
                if not replaced:
                    rows.append(updated)
                    replaced = True
                continue
            if self._row_key(row) != "new":
                rows.append(row)
        if not replaced:
            rows.append(updated)
        self.set_stages(rows)
        target_key = self._row_key(updated)
        widgets = self._row_widgets.get(target_key) or {}
        button = widgets.get("button")
        if button is not None:
            button.setText("Сохранено")
            button.setEnabled(False)

    def _apply_saved_stage_in_place(self, row_key: str, updated: dict) -> bool:
        if self._row_key(updated) == "new":
            return False
        old_key = str(row_key or "")
        target_key = self._row_key(updated)
        target_event_id = _safe_int(updated.get("event_id"))
        if old_key == "new" or not target_event_id:
            return False

        rows_without_blank = [dict(row or {}) for row in self._rows if self._row_key(row) != "new"]
        candidate_rows: list[dict] = []
        replaced = False
        for row in rows_without_blank:
            current_key = self._row_key(row)
            if current_key == old_key or _safe_int(row.get("event_id")) == target_event_id:
                if not replaced:
                    candidate_rows.append(dict(updated))
                    replaced = True
                continue
            candidate_rows.append(row)
        if not replaced:
            return False

        current_order = [self._row_key(row) for row in rows_without_blank]
        next_order = [self._row_key(row) for row in self._normalized_stage_rows(candidate_rows)]
        if current_order != next_order:
            return False

        widgets = self._row_widgets.get(old_key) or self._row_widgets.get(target_key) or {}
        if not widgets:
            return False
        for index, row in enumerate(self._rows):
            if self._row_key(row) == old_key or _safe_int(row.get("event_id")) == target_event_id:
                self._rows[index] = dict(updated)
                break

        if old_key != target_key:
            self._row_widgets[target_key] = widgets
            self._row_widgets.pop(old_key, None)
        widgets["row"] = dict(updated)
        edit = widgets.get("edit")
        if edit is not None:
            was_blocked = edit.blockSignals(True)
            try:
                edit.setProperty("row_key", target_key)
                edit.setText(str(updated.get("label") or ""))
                edit.setEnabled(True)
            finally:
                edit.blockSignals(was_blocked)
        time_label = widgets.get("time_label")
        if time_label is not None:
            time_label.setProperty("row_key", target_key)
            time_label.setText(_format_order_time(updated.get("event_time")) if updated.get("event_time") else "")
            time_label.setEnabled(True)
        button = widgets.get("button")
        if button is not None:
            button.setProperty("row_key", target_key)
            button.setText("Сохранено")
            button.setEnabled(False)
        return True
