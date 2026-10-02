from __future__ import annotations
from rem_card.ui.styles.theme_runtime import set_widget_style

from datetime import datetime, timedelta
import re

from PySide6.QtCore import QEvent, QSize, Qt, QTime
from PySide6.QtGui import (
    QColor,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QTimeEdit,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from rem_card.services.operblock_route_settings import (
    operblock_route_label,
)
from rem_card.services.operblock_icon_defaults import (
    default_drug_icon_file,
    drug_icon_candidate_keys_from_payload,
    edit_icon_key,
    type_icon_key,
)
from rem_card.ui.shared.window_state import SavedFramelessDialogMixin
from rem_card.ui.styles.theme import (
    STYLE_PATIENT_FORM_CANCEL_BUTTON,
    STYLE_PATIENT_FORM_VALID_FIELD,
    TEXT_PRIMARY,
)
from rem_card.ui.operblock_view.operblock_control_styles import (
    operblock_arrow_button_style,
    operblock_med_action_button_style,
)

from rem_card.ui.operblock_view.operblock_visual_primitives import (
    OPERBLOCK_ORDER_ROUTE_DEFAULT,
    OPERBLOCK_ORDER_ROUTE_INTRAMUSCULAR,
    _apply_operblock_window_icon,
    _create_operblock_title_icon,
    OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE,
)
from rem_card.ui.operblock_view.operblock_helpers import (
    _parse_datetime_value,
    _minute_floor_dt,
    _build_order_text_for_display,
    _normalize_bolus_dose_text,
    _normalize_order_route_code,
    _split_infusion_rate_text,
    _format_infusion_rate,
    _compact_infusion_rate_display_text,
    _normalize_volume_ml_text,
    OXYGEN_ICON_FILE,
    _payload_or_text_is_oxygen,
    _normalize_oxygen_flow_text,
)
from rem_card.ui.operblock_view.operblock_dialog_base import (
    OperBlockStyledDialog,
)
from rem_card.ui.operblock_view.operblock_medication_order_dialogs import (
    GAS_MAC_HINT_TOOLTIP,
    _create_gas_dialog_image_icon,
    _create_gas_dialog_plain_icon,
    _gas_time_step_icon,
)

class MedicationEditDialogBase(SavedFramelessDialogMixin, QDialog):
    def __init__(
        self,
        *,
        title: str,
        drug_name: str,
        subtitle: str,
        value_label: str,
        value_text: str = "",
        placeholder: str = "",
        left_icon_file: str,
        left_icon_background: str,
        right_icon_file,
        geometry_key: str,
        parent=None,
        start_datetime: datetime | None = None,
        min_datetime: datetime | None = None,
        max_datetime: datetime | None = None,
        show_time: bool = True,
        show_route: bool = False,
        show_value: bool = True,
        time_label: str = "Время начала",
        route_code: str = OPERBLOCK_ORDER_ROUTE_DEFAULT,
        route_options: list[dict[str, str]] | None = None,
        action_text: str = "Сохранить",
        minimum_width: int | None = None,
        right_icon_fallback_file: str = "",
    ):
        super().__init__(parent)
        self._dialog_title = str(title or "Изменить назначение").strip() or "Изменить назначение"
        self._drug_name = re.sub(r"\s+", " ", str(drug_name or "Назначение").strip()) or "Назначение"
        self._subtitle = str(subtitle or "").strip()
        self._value_label = str(value_label or "Доза").strip()
        self._value_text = str(value_text or "").strip()
        self._placeholder = str(placeholder or "").strip()
        self._left_icon_file = str(left_icon_file or "").strip()
        self._left_icon_background = str(left_icon_background or "#EEF2FF").strip()
        self._right_icon_file = right_icon_file
        self._right_icon_fallback_file = str(right_icon_fallback_file or "").strip()
        self._start_datetime = _minute_floor_dt(start_datetime)
        self._time_min_datetime = _minute_floor_dt(min_datetime)
        self._time_max_datetime = _minute_floor_dt(max_datetime)
        if self._time_min_datetime and self._time_max_datetime and self._time_max_datetime < self._time_min_datetime:
            self._time_max_datetime = None
        self._show_time = bool(show_time)
        self._show_value = bool(show_value)
        self._time_label = str(time_label or "Время").strip() or "Время"
        self._route_code = _normalize_order_route_code(route_code)
        self._route_options = self._normalize_route_options(route_options, self._route_code)
        if self._route_code not in {option["code"] for option in self._route_options} and self._route_options:
            self._route_code = self._route_options[0]["code"]
        self._show_route = bool(show_route and len(self._route_options) > 1)
        self._action_text = str(action_text or "Сохранить")
        self._time_text_updating = False

        if minimum_width is not None:
            width = int(minimum_width)
        elif self._show_route and self._show_time:
            width = 720
        elif self._show_time and self._show_value:
            width = 650
        elif self._show_time:
            width = 540
        else:
            width = 560
        height = 342 if self._show_time and (self._show_value or self._show_route) else 292
        self.setWindowTitle(self._dialog_title)
        _apply_operblock_window_icon(self)
        self.setMinimumSize(width, height)
        self.resize(width, height)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setMouseTracking(True)
        self._init_saved_frameless_dialog(geometry_key, drag_area_height=44)
        self._init_ui()
        self._restore_saved_geometry()

    @staticmethod
    def _normalize_route_options(route_options: list[dict[str, str]] | None, current_code: str = "") -> list[dict[str, str]]:
        source = route_options
        if source is None:
            source = [
                {"code": OPERBLOCK_ORDER_ROUTE_DEFAULT, "label": "в/в"},
                {"code": OPERBLOCK_ORDER_ROUTE_INTRAMUSCULAR, "label": "в/м"},
            ]
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        label_indexes: dict[str, int] = {}
        current_code = _normalize_order_route_code(current_code)
        for option in source or []:
            code = _normalize_order_route_code((option or {}).get("code"))
            if not code or code in seen:
                continue
            raw_label = str((option or {}).get("label") or "").strip()
            label_routes = [{"code": code, "label": raw_label}] if raw_label else None
            label = str(operblock_route_label(code, short=True, routes=label_routes) or raw_label or code).strip()
            label_key = label.casefold()
            if label_key in label_indexes:
                index = label_indexes[label_key]
                existing_code = result[index]["code"]
                if code == current_code or (existing_code != current_code and code == OPERBLOCK_ORDER_ROUTE_DEFAULT):
                    result[index] = {"code": code, "label": label}
                    seen.add(code)
                continue
            result.append({"code": code, "label": label})
            label_indexes[label_key] = len(result) - 1
            seen.add(code)
        if not result:
            result.append({"code": OPERBLOCK_ORDER_ROUTE_DEFAULT, "label": operblock_route_label(OPERBLOCK_ORDER_ROUTE_DEFAULT, short=True)})
        return result

    def _init_ui(self) -> None:
        set_widget_style(self, """
            QDialog {
                background-color: transparent;
                font-family: "Segoe UI", "Inter", Arial, sans-serif;
            }
            QFrame#MedDialogFrame {
                background-color: #F8FAFC;
                border: 1px solid #CBD5E1;
                border-radius: 12px;
            }
            QFrame#MedDialogHeader {
                background-color: #F8FAFC;
                border-top-left-radius: 12px;
                border-top-right-radius: 12px;
                border-bottom: 1px solid #E5E7EB;
            }
            QLabel#MedDialogTitle {
                color: #111827;
                font-size: 16px;
                font-weight: 600;
                background: transparent;
            }
            QPushButton#MedDialogClose {
                background-color: transparent;
                color: #1F2937;
                border: none;
                border-radius: 6px;
                font-size: 18px;
                font-weight: 300;
                padding-bottom: 1px;
            }
            QPushButton#MedDialogClose:hover {
                background-color: #e74c3c;
                color: white;
            }
            QFrame#MedDialogBody {
                background-color: #F8FAFC;
                border: none;
            }
            QLabel#MedDrugName {
                color: #111827;
                font-size: 19px;
                font-weight: 700;
                background: transparent;
            }
            QLabel#MedDrugSubtitle {
                color: #6B7280;
                font-size: 12px;
                font-weight: 400;
                background: transparent;
            }
            QFrame#MedSeparator {
                background-color: #E5E7EB;
                border: none;
                max-height: 1px;
                min-height: 1px;
            }
            QFrame#MedFieldCard {
                background-color: #FFFFFF;
                border: 1px solid #E5E7EB;
                border-radius: 14px;
            }
            QLabel#MedFieldTitle {
                color: #6B7280;
                font-size: 12px;
                font-weight: 500;
                background: transparent;
            }
            QLineEdit#MedValueInput {
                background-color: #FFFFFF;
                color: #111827;
                border: 1px solid #D1D5DB;
                border-radius: 8px;
                font-size: 18px;
                font-weight: 400;
                padding: 0 14px;
                selection-background-color: #C7D2FE;
            }
            QLineEdit#MedValueInput:focus {
                border: 1px solid #6366F1;
            }
            QFrame#MedTimeInputFrame {
                background-color: #FFFFFF;
                border: 1px solid #D1D5DB;
                border-radius: 8px;
            }
            QFrame#MedTimeInputFrame[focused="true"] {
                border: 1px solid #6366F1;
            }
            QLineEdit#MedTimeInput {
                background-color: transparent;
                color: #111827;
                border: none;
                font-size: 18px;
                font-weight: 400;
                padding: 0 14px;
                selection-background-color: #C7D2FE;
            }
            QFrame#MedTimeStepperColumn {
                background-color: transparent;
                border: none;
            }
            """
            + operblock_arrow_button_style("QPushButton#MedTimeStepButton")
            + """
            QPushButton#MedRouteButton {
                background-color: #F1F5F9;
                color: #64748B;
                border: 1px solid #D1D5DB;
                border-radius: 8px;
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton#MedRouteButton:checked {
                background-color: #DFF4E8;
                border-color: #2F8A57;
                color: #17633A;
            }
            QPushButton#MedRouteButton:hover {
                border-color: #94A3B8;
            }
            QFrame#MedDialogFooter {
                background-color: #F8FAFC;
                border-bottom-left-radius: 12px;
                border-bottom-right-radius: 12px;
                border-top: 1px solid #E5E7EB;
            }
            """
            + operblock_med_action_button_style("QPushButton#MedCancelButton", "QPushButton#MedSaveButton"))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(0)

        self.bg_container = QFrame(self)
        self.bg_container.setObjectName("MedDialogFrame")
        shadow = QGraphicsDropShadowEffect(self.bg_container)
        shadow.setBlurRadius(26)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(15, 23, 42, 38))
        self.bg_container.setGraphicsEffect(shadow)
        outer.addWidget(self.bg_container)

        main = QVBoxLayout(self.bg_container)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        header = QFrame(self.bg_container)
        header.setObjectName("MedDialogHeader")
        header.setFixedHeight(34)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(8, 0, 0, 0)
        header_layout.setSpacing(8)
        header_icon = _create_operblock_title_icon(20)
        if header_icon is not None:
            header_layout.addWidget(header_icon, 0, Qt.AlignVCenter)
        title = QLabel(self._dialog_title)
        title.setObjectName("MedDialogTitle")
        title.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        header_layout.addWidget(title, 1)
        close_button = QPushButton("×")
        close_button.setObjectName("MedDialogClose")
        close_button.setFixedSize(34, 34)
        close_button.setCursor(Qt.PointingHandCursor)
        close_button.clicked.connect(self.reject)
        header_layout.addWidget(close_button, 0, Qt.AlignVCenter)
        main.addWidget(header)

        body = QFrame(self.bg_container)
        body.setObjectName("MedDialogBody")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(22, 12, 22, 10)
        body_layout.setSpacing(10)

        drug_row = QFrame(body)
        drug_layout = QHBoxLayout(drug_row)
        drug_layout.setContentsMargins(0, 0, 0, 0)
        drug_layout.setSpacing(16)
        drug_layout.addWidget(
            _create_gas_dialog_image_icon(
                self._left_icon_file,
                frame_size=52,
                icon_size=32,
                background=self._left_icon_background,
                parent=drug_row,
            ),
            0,
            Qt.AlignVCenter,
        )
        drug_text_col = QVBoxLayout()
        drug_text_col.setContentsMargins(0, 0, 0, 0)
        drug_text_col.setSpacing(3)
        drug_name = QLabel(self._drug_name)
        drug_name.setObjectName("MedDrugName")
        drug_subtitle = QLabel(self._subtitle)
        drug_subtitle.setObjectName("MedDrugSubtitle")
        drug_text_col.addStretch(1)
        drug_text_col.addWidget(drug_name)
        drug_text_col.addWidget(drug_subtitle)
        drug_text_col.addStretch(1)
        drug_layout.addLayout(drug_text_col, 1)
        if self._right_icon_file:
            drug_layout.addWidget(
                _create_gas_dialog_plain_icon(
                    self._right_icon_file,
                    icon_size=92,
                    parent=drug_row,
                    fallback_file=self._right_icon_fallback_file,
                ),
                0,
                Qt.AlignRight | Qt.AlignVCenter,
            )
        body_layout.addWidget(drug_row)

        separator = QFrame(body)
        separator.setObjectName("MedSeparator")
        body_layout.addWidget(separator)

        fields = QHBoxLayout()
        fields.setContentsMargins(0, 0, 0, 0)
        fields.setSpacing(14)
        if self._show_value:
            fields.addWidget(self._value_card(body), 1)
        if self._show_time:
            fields.addWidget(self._time_card(body), 1)
        if self._show_route:
            fields.addWidget(self._route_card(body), 0)
        body_layout.addLayout(fields)
        main.addWidget(body, 1)

        footer = QFrame(self.bg_container)
        footer.setObjectName("MedDialogFooter")
        footer.setFixedHeight(54)
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(22, 0, 22, 0)
        footer_layout.setSpacing(10)
        footer_layout.addStretch(1)
        cancel_button = QPushButton("Отменить")
        cancel_button.setObjectName("MedCancelButton")
        cancel_button.setFixedSize(118, 38)
        cancel_button.setCursor(Qt.PointingHandCursor)
        cancel_button.clicked.connect(self.reject)
        save_button = QPushButton(self._action_text)
        save_button.setObjectName("MedSaveButton")
        save_button.setFixedSize(138, 38)
        save_button.setCursor(Qt.PointingHandCursor)
        save_button.clicked.connect(self.accept)
        cancel_button.setAutoDefault(False)
        cancel_button.setDefault(False)
        save_button.setAutoDefault(True)
        save_button.setDefault(True)
        footer_layout.addWidget(cancel_button)
        footer_layout.addWidget(save_button)
        main.addWidget(footer)

    def _field_card(self, parent=None, *, minimum_width: int = 220) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame(parent)
        card.setObjectName("MedFieldCard")
        card.setMinimumHeight(100)
        card.setMinimumWidth(int(minimum_width))
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(6)
        return card, layout

    def _value_card(self, parent=None) -> QFrame:
        card, layout = self._field_card(parent)
        title = QLabel(self._value_label)
        title.setObjectName("MedFieldTitle")
        layout.addWidget(title)
        self.value_input = QLineEdit()
        self.value_input.setObjectName("MedValueInput")
        self.value_input.setFixedHeight(52)
        self.value_input.setText(self._value_text)
        self.value_input.setPlaceholderText(self._placeholder)
        self.value_input.selectAll()
        layout.addWidget(self.value_input)
        return card

    def _time_card(self, parent=None) -> QFrame:
        card, layout = self._field_card(parent)
        title = QLabel(self._time_label)
        title.setObjectName("MedFieldTitle")
        layout.addWidget(title)

        self.time_frame = QFrame()
        self.time_frame.setObjectName("MedTimeInputFrame")
        self.time_frame.setFixedHeight(52)
        self.time_frame.setProperty("focused", False)
        time_layout = QHBoxLayout(self.time_frame)
        time_layout.setContentsMargins(0, 0, 0, 0)
        time_layout.setSpacing(0)

        self.time_input = QLineEdit()
        self.time_input.setObjectName("MedTimeInput")
        start_dt = self._coerce_time_datetime(self._start_datetime or datetime.now().replace(second=0, microsecond=0))
        self.time_input.setText(f"{start_dt.hour:02d}:{start_dt.minute:02d}")
        self.time_input.setPlaceholderText("09:10")
        self.time_input.setMaxLength(5)
        self.time_input.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self.time_input.textEdited.connect(self._on_time_text_edited)
        self.time_input.editingFinished.connect(self._commit_time_text)
        self.time_input.installEventFilter(self)
        time_layout.addWidget(self.time_input, 1)

        stepper = QFrame()
        stepper.setObjectName("MedTimeStepperColumn")
        stepper.setFixedWidth(42)
        stepper_layout = QVBoxLayout(stepper)
        stepper_layout.setContentsMargins(6, 4, 6, 4)
        stepper_layout.setSpacing(4)

        up_button = QPushButton()
        up_button.setObjectName("MedTimeStepButton")
        up_button.setFixedSize(30, 20)
        up_button.setIcon(_gas_time_step_icon(up=True))
        up_button.setIconSize(QSize(14, 14))
        up_button.setCursor(Qt.PointingHandCursor)
        up_button.clicked.connect(lambda _=False: self._step_time(1))
        down_button = QPushButton()
        down_button.setObjectName("MedTimeStepButton")
        down_button.setFixedSize(30, 20)
        down_button.setIcon(_gas_time_step_icon(up=False))
        down_button.setIconSize(QSize(14, 14))
        down_button.setCursor(Qt.PointingHandCursor)
        down_button.clicked.connect(lambda _=False: self._step_time(-1))
        stepper_layout.addWidget(up_button)
        stepper_layout.addWidget(down_button)
        time_layout.addWidget(stepper, 0)

        layout.addWidget(self.time_frame)
        return card

    def _route_card(self, parent=None) -> QFrame:
        max_label_len = max((len(str(option.get("label") or "")) for option in self._route_options), default=0)
        button_width = max(70, min(132, 12 + max_label_len * 7))
        card_width = max(142, min(520, 24 + (button_width + 8) * len(self._route_options)))
        card, layout = self._field_card(parent, minimum_width=card_width)
        card.setMaximumWidth(card_width + 10)
        title = QLabel("Место")
        title.setObjectName("MedFieldTitle")
        layout.addWidget(title)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.route_button_group = QButtonGroup(self)
        self.route_button_group.setExclusive(True)
        checked_any = False
        for option in self._route_options:
            route_code = _normalize_order_route_code((option or {}).get("code"))
            raw_label = str((option or {}).get("label") or "").strip()
            label_routes = [{"code": route_code, "label": raw_label}] if raw_label else None
            label = str(operblock_route_label(route_code, short=True, routes=label_routes) or raw_label or route_code).strip()
            button = QPushButton(label)
            button.setObjectName("MedRouteButton")
            button.setCheckable(True)
            button.setFixedSize(button_width, 52)
            button.setCursor(Qt.PointingHandCursor)
            button.setProperty("route_code", route_code)
            self.route_button_group.addButton(button)
            row.addWidget(button)
            if route_code == self._route_code:
                button.setChecked(True)
                checked_any = True
        if not checked_any and self.route_button_group.buttons():
            self.route_button_group.buttons()[0].setChecked(True)
        layout.addLayout(row)
        return card

    def value_text(self) -> str:
        return self.value_input.text().strip()

    def route_code(self) -> str:
        group = getattr(self, "route_button_group", None)
        if group is None:
            return self._route_code or OPERBLOCK_ORDER_ROUTE_DEFAULT
        checked = group.checkedButton()
        if checked is None:
            return self._route_code or OPERBLOCK_ORDER_ROUTE_DEFAULT
        return _normalize_order_route_code(checked.property("route_code"))

    def start_time_text(self) -> str:
        if not getattr(self, "_show_time", True):
            return ""
        return self._commit_time_text()

    def accept(self) -> None:
        if getattr(self, "_show_time", True):
            self._commit_time_text()
        super().accept()

    def eventFilter(self, obj, event):
        if obj is getattr(self, "time_input", None):
            if event.type() == QEvent.FocusIn:
                self._set_time_focus(True)
            elif event.type() == QEvent.FocusOut:
                self._set_time_focus(False)
        return super().eventFilter(obj, event)

    def _set_time_focus(self, focused: bool) -> None:
        frame = getattr(self, "time_frame", None)
        if frame is None:
            return
        frame.setProperty("focused", bool(focused))
        frame.style().unpolish(frame)
        frame.style().polish(frame)

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
        fallback_dt = self._start_datetime or self._time_min_datetime or datetime.now().replace(second=0, microsecond=0)
        return self._coerce_time_datetime(fallback_dt)

    def _time_base_datetime(self) -> datetime:
        return self._time_min_datetime or self._start_datetime or datetime.now().replace(second=0, microsecond=0)

    def _time_datetime_from_text(self, value: str) -> datetime | None:
        minutes = self._time_minutes_from_text(value)
        if minutes is None:
            return None
        hour = minutes // 60
        minute = minutes % 60
        base_dt = self._time_base_datetime()
        same_day = datetime.combine(base_dt.date(), datetime.min.time()).replace(hour=hour, minute=minute)
        if same_day >= base_dt:
            return same_day
        crosses_midnight = hour < 6 or (base_dt.hour >= 12 and hour < base_dt.hour)
        return same_day + timedelta(days=1) if crosses_midnight else same_day

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

    @staticmethod
    def _time_minutes_from_text(value: str) -> int | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        colon_match = re.fullmatch(r"(\d{1,2})\s*:\s*(\d{1,2})", raw)
        if colon_match:
            hour = int(colon_match.group(1))
            minute = int(colon_match.group(2))
        else:
            digits = re.sub(r"\D", "", raw)
            if len(digits) == 4:
                hour = int(digits[:2])
                minute = int(digits[2:])
            elif len(digits) == 3:
                hour = int(digits[:1])
                minute = int(digits[1:])
            else:
                return None
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour * 60 + minute
        return None


class BolusEditDialog(MedicationEditDialogBase):
    def __init__(
        self,
        drug_name: str,
        dose_text: str = "",
        parent=None,
        *,
        base_datetime: datetime | None = None,
        min_datetime: datetime | None = None,
        max_datetime: datetime | None = None,
        route_code: str = OPERBLOCK_ORDER_ROUTE_DEFAULT,
        route_options: list[dict[str, str]] | None = None,
        action_text: str = "Сохранить",
    ):
        clean_name = re.sub(r"\s+", " ", str(drug_name or "Препарат").strip()) or "Препарат"
        super().__init__(
            title=f"Изменить препарат: {clean_name}",
            drug_name=clean_name,
            subtitle="Болюсное введение",
            value_label="Доза. (г; мг; мкг)",
            value_text=_normalize_bolus_dose_text(dose_text),
            placeholder="200 мг",
            left_icon_file=type_icon_key("bolus"),
            left_icon_background="#EAFBF5",
            right_icon_file=edit_icon_key("bolus"),
            geometry_key="operblock/bolus_edit_dialog_geometry_v2",
            parent=parent,
            start_datetime=base_datetime,
            min_datetime=min_datetime,
            max_datetime=max_datetime,
            show_time=base_datetime is not None,
            show_route=True,
            route_code=route_code,
            route_options=route_options,
            action_text=action_text,
            minimum_width=724 if base_datetime is not None else 610,
        )

    def dose_text(self) -> str:
        return _normalize_bolus_dose_text(self.value_text())

    def text(self) -> str:
        return _build_order_text_for_display(self._drug_name, self.dose_text())

    def datetime_text(self) -> str:
        if not getattr(self, "_show_time", False):
            return ""
        selected_text = self.start_time_text()
        selected_dt = self._time_datetime_from_text(selected_text) or self._fallback_time_datetime()
        return self._coerce_time_datetime(selected_dt).isoformat(timespec="seconds")


class InfusionRateDialog(MedicationEditDialogBase):
    def __init__(
        self,
        title: str,
        rate_text: str = "",
        parent=None,
        *,
        drug_name: str = "Дозатор",
        start_datetime: datetime | None = None,
        min_datetime: datetime | None = None,
        max_datetime: datetime | None = None,
        action_text: str = "Сохранить",
    ):
        clean_name = re.sub(r"\s+", " ", str(drug_name or "Дозатор").strip()) or "Дозатор"
        super().__init__(
            title=title,
            drug_name=clean_name,
            subtitle="Длительная инфузия",
            value_label="Скорость: мл/ч",
            value_text=_compact_infusion_rate_display_text(rate_text),
            placeholder="1 мл/ч",
            left_icon_file=type_icon_key("continuous_infusion"),
            left_icon_background="#FFF7ED",
            right_icon_file=edit_icon_key("continuous_infusion"),
            geometry_key="operblock/infusion_rate_dialog_geometry_v2",
            parent=parent,
            start_datetime=start_datetime,
            min_datetime=min_datetime,
            max_datetime=max_datetime,
            show_time=start_datetime is not None,
            show_route=False,
            action_text=action_text,
            minimum_width=650 if start_datetime is not None else 560,
        )

    def rate_text(self) -> str:
        value, unit = _split_infusion_rate_text(self.value_text())
        return _format_infusion_rate(value, unit) if value else self.value_text()


class InfusionVolumeDialog(MedicationEditDialogBase):
    def __init__(
        self,
        title: str,
        volume_text: str = "",
        parent=None,
        *,
        drug_name: str = "Капельница",
        start_datetime: datetime | None = None,
        min_datetime: datetime | None = None,
        max_datetime: datetime | None = None,
        show_time: bool = True,
        action_text: str = "Сохранить",
        field_label: str = "Объем: мл",
        placeholder: str = "200 мл",
    ):
        clean_name = re.sub(r"\s+", " ", str(drug_name or "Капельница").strip()) or "Капельница"
        super().__init__(
            title=title,
            drug_name=clean_name,
            subtitle="Капельная инфузия",
            value_label=field_label,
            value_text=_normalize_volume_ml_text(volume_text),
            placeholder=placeholder,
            left_icon_file=type_icon_key("timed_infusion"),
            left_icon_background="#EAF3FF",
            right_icon_file=edit_icon_key("timed_infusion"),
            geometry_key="operblock/infusion_volume_dialog_geometry_v2",
            parent=parent,
            start_datetime=start_datetime,
            min_datetime=min_datetime,
            max_datetime=max_datetime,
            show_time=show_time,
            show_route=False,
            action_text=action_text,
            minimum_width=650 if show_time else 560,
        )

    def volume_text(self) -> str:
        return _normalize_volume_ml_text(self.value_text())


class InfusionStopDialog(MedicationEditDialogBase):
    def __init__(
        self,
        drug_name: str,
        parent=None,
        *,
        start_datetime: datetime | None = None,
        min_datetime: datetime | None = None,
        max_datetime: datetime | None = None,
        infusion_kind: str = "rate",
        payload: dict | None = None,
    ):
        clean_name = re.sub(r"\s+", " ", str(drug_name or "Назначение").strip()) or "Назначение"
        kind = str(infusion_kind or "").strip().casefold()
        if kind == "gas":
            payload_data = payload if isinstance(payload, dict) else {}
            is_oxygen = _payload_or_text_is_oxygen(payload_data, clean_name)
            subtitle = "Кислород" if is_oxygen else "Ингаляционный анестетик"
            left_icon_file = OXYGEN_ICON_FILE if is_oxygen else type_icon_key("gas")
            left_icon_background = "#E0F2FE" if is_oxygen else "#EEF2FF"
            right_icon_file = drug_icon_candidate_keys_from_payload(payload_data, clean_name)
            if not right_icon_file:
                right_icon_file = [] if is_oxygen else [edit_icon_key("gas")]
            right_icon_fallback = OXYGEN_ICON_FILE if is_oxygen else default_drug_icon_file("gas")
        elif kind == "volume":
            subtitle = "Капельная инфузия"
            left_icon_file = type_icon_key("timed_infusion")
            left_icon_background = "#EAF3FF"
            right_icon_file = edit_icon_key("timed_infusion")
            right_icon_fallback = default_drug_icon_file("timed_infusion")
        else:
            subtitle = "Длительная инфузия"
            left_icon_file = type_icon_key("continuous_infusion")
            left_icon_background = "#FFF7ED"
            right_icon_file = edit_icon_key("continuous_infusion")
            right_icon_fallback = default_drug_icon_file("continuous_infusion")
        super().__init__(
            title=f"Остановить назначение: {clean_name}",
            drug_name=clean_name,
            subtitle=subtitle,
            value_label="",
            value_text="",
            placeholder="",
            left_icon_file=left_icon_file,
            left_icon_background=left_icon_background,
            right_icon_file=right_icon_file,
            right_icon_fallback_file=right_icon_fallback,
            geometry_key="operblock/infusion_stop_dialog_geometry_v1",
            parent=parent,
            start_datetime=start_datetime,
            min_datetime=min_datetime,
            max_datetime=max_datetime,
            show_time=True,
            show_route=False,
            show_value=False,
            time_label="Время окончания",
            action_text="Остановить",
            minimum_width=540,
        )

    def datetime_text(self) -> str:
        selected_text = self.start_time_text()
        selected_dt = self._time_datetime_from_text(selected_text) or self._fallback_time_datetime()
        return self._coerce_time_datetime(selected_dt).isoformat(timespec="seconds")


class GasDoseDialog(SavedFramelessDialogMixin, QDialog):
    def __init__(
        self,
        gas_name: str,
        concentration_text: str = "",
        parent=None,
        *,
        start_datetime: datetime | None = None,
        min_datetime: datetime | None = None,
        max_datetime: datetime | None = None,
        show_time: bool = True,
        action_text: str = "Сохранить",
        payload: dict | None = None,
        is_oxygen: bool = False,
    ):
        super().__init__(parent)
        self._gas_name = re.sub(r"\s+", " ", str(gas_name or "Газ").strip()) or "Газ"
        self._is_oxygen = bool(is_oxygen or _payload_or_text_is_oxygen(payload if isinstance(payload, dict) else {}, self._gas_name))
        self._concentration_text = (
            _normalize_oxygen_flow_text(concentration_text)
            if self._is_oxygen
            else str(concentration_text or "").strip()
        )
        self._start_datetime = _minute_floor_dt(start_datetime)
        self._time_min_datetime = _minute_floor_dt(min_datetime)
        self._time_max_datetime = _minute_floor_dt(max_datetime)
        if self._time_min_datetime and self._time_max_datetime and self._time_max_datetime < self._time_min_datetime:
            self._time_max_datetime = None
        self._show_time = bool(show_time)
        self._action_text = str(action_text or "Сохранить")
        self._payload = dict(payload or {}) if isinstance(payload, dict) else {}
        self._time_text_updating = False
        self._dialog_title = f"Изменить кислород: {self._gas_name}" if self._is_oxygen else f"Изменить газ: {self._gas_name}"
        self.setWindowTitle(self._dialog_title)
        _apply_operblock_window_icon(self)
        self.setMinimumSize(610, 342 if self._show_time else 292)
        self.resize(610, 342 if self._show_time else 292)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setMouseTracking(True)
        self._init_saved_frameless_dialog("operblock/gas_dose_dialog_geometry_v3", drag_area_height=44)
        self._init_ui()
        self._restore_saved_geometry()

    def _init_ui(self):
        set_widget_style(self, """
            QDialog {
                background-color: transparent;
                font-family: "Segoe UI", "Inter", Arial, sans-serif;
            }
            QFrame#GasDialogFrame {
                background-color: #F8FAFC;
                border: 1px solid #CBD5E1;
                border-radius: 12px;
            }
            QFrame#GasDialogHeader {
                background-color: #F8FAFC;
                border-top-left-radius: 12px;
                border-top-right-radius: 12px;
                border-bottom: 1px solid #E5E7EB;
            }
            QLabel#GasDialogTitle {
                color: #111827;
                font-size: 16px;
                font-weight: 600;
                background: transparent;
            }
            QPushButton#GasDialogClose {
                background-color: transparent;
                color: #1F2937;
                border: none;
                border-radius: 6px;
                font-size: 18px;
                font-weight: 300;
                padding-bottom: 1px;
            }
            QPushButton#GasDialogClose:hover {
                background-color: #e74c3c;
                color: white;
            }
            QFrame#GasDialogBody {
                background-color: #F8FAFC;
                border: none;
            }
            QLabel#GasDrugName {
                color: #111827;
                font-size: 19px;
                font-weight: 700;
                background: transparent;
            }
            QLabel#GasDrugSubtitle {
                color: #6B7280;
                font-size: 12px;
                font-weight: 400;
                background: transparent;
            }
            QFrame#GasSeparator {
                background-color: #E5E7EB;
                border: none;
                max-height: 1px;
                min-height: 1px;
            }
            QFrame#GasFieldCard {
                background-color: #FFFFFF;
                border: 1px solid #E5E7EB;
                border-radius: 14px;
            }
            QLabel#GasFieldTitle {
                color: #6B7280;
                font-size: 12px;
                font-weight: 500;
                background: transparent;
            }
            QPushButton#GasInfoButton {
                color: #9CA3AF;
                font-size: 12px;
                font-weight: 700;
                border: 2px solid #9CA3AF;
                border-radius: 8px;
                background: transparent;
                padding: 0;
            }
            QPushButton#GasInfoButton:hover {
                color: #4F46E5;
                border-color: #6366F1;
                background-color: #EEF2FF;
            }
            QLineEdit#GasDoseInput {
                background-color: #FFFFFF;
                color: #111827;
                border: 1px solid #D1D5DB;
                border-radius: 8px;
                font-size: 18px;
                font-weight: 400;
                padding: 0 14px;
                selection-background-color: #C7D2FE;
            }
            QLineEdit#GasDoseInput:focus {
                border: 1px solid #6366F1;
            }
            QFrame#GasTimeInputFrame {
                background-color: #FFFFFF;
                border: 1px solid #D1D5DB;
                border-radius: 8px;
            }
            QFrame#GasTimeInputFrame[focused="true"] {
                border: 1px solid #6366F1;
            }
            QLineEdit#GasTimeInput {
                background-color: transparent;
                color: #111827;
                border: none;
                font-size: 18px;
                font-weight: 400;
                padding: 0 14px;
                selection-background-color: #C7D2FE;
            }
            QFrame#GasTimeStepperColumn {
                background-color: transparent;
                border: none;
            }
            """
            + operblock_arrow_button_style("QPushButton#GasTimeStepButton")
            + """
            QFrame#GasDialogFooter {
                background-color: #F8FAFC;
                border-bottom-left-radius: 12px;
                border-bottom-right-radius: 12px;
                border-top: 1px solid #E5E7EB;
            }
            QPushButton#GasCancelButton {
                background-color: #FFFFFF;
                color: #111827;
                border: 1px solid #D1D5DB;
                border-radius: 8px;
                font-size: 13px;
                font-weight: 600;
            }
            QPushButton#GasCancelButton:hover {
                background-color: #F3F4F6;
                border-color: #B8C0CC;
            }
            QPushButton#GasSaveButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #6366F1, stop:1 #4F46E5);
                color: #FFFFFF;
                border: 1px solid #4F46E5;
                border-radius: 8px;
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton#GasSaveButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #7377F7, stop:1 #5B52EA);
                border-color: #6366F1;
            }
            """)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(0)

        self.bg_container = QFrame(self)
        self.bg_container.setObjectName("GasDialogFrame")
        shadow = QGraphicsDropShadowEffect(self.bg_container)
        shadow.setBlurRadius(26)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(15, 23, 42, 38))
        self.bg_container.setGraphicsEffect(shadow)
        outer.addWidget(self.bg_container)

        main = QVBoxLayout(self.bg_container)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        header = QFrame(self.bg_container)
        header.setObjectName("GasDialogHeader")
        header.setFixedHeight(34)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(8, 0, 0, 0)
        header_layout.setSpacing(8)
        header_icon = _create_operblock_title_icon(20)
        if header_icon is not None:
            header_layout.addWidget(header_icon, 0, Qt.AlignVCenter)
        title = QLabel(self._dialog_title)
        title.setObjectName("GasDialogTitle")
        title.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        header_layout.addWidget(title, 1)
        close_button = QPushButton("×")
        close_button.setObjectName("GasDialogClose")
        close_button.setFixedSize(34, 34)
        close_button.setCursor(Qt.PointingHandCursor)
        close_button.clicked.connect(self.reject)
        header_layout.addWidget(close_button, 0, Qt.AlignVCenter)
        main.addWidget(header)

        body = QFrame(self.bg_container)
        body.setObjectName("GasDialogBody")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(22, 12, 22, 10)
        body_layout.setSpacing(10)

        drug_row = QFrame(body)
        drug_row.setObjectName("GasDrugCard")
        drug_layout = QHBoxLayout(drug_row)
        drug_layout.setContentsMargins(0, 0, 0, 0)
        drug_layout.setSpacing(16)
        drug_layout.addWidget(
            _create_gas_dialog_image_icon(
                OXYGEN_ICON_FILE if self._is_oxygen else type_icon_key("gas"),
                frame_size=52,
                icon_size=32,
                background="#E0F2FE" if self._is_oxygen else "#EEF2FF",
                parent=drug_row,
                fallback_file=OXYGEN_ICON_FILE if self._is_oxygen else "",
            ),
            0,
            Qt.AlignVCenter,
        )
        drug_text_col = QVBoxLayout()
        drug_text_col.setContentsMargins(0, 0, 0, 0)
        drug_text_col.setSpacing(3)
        drug_name = QLabel(self._gas_name)
        drug_name.setObjectName("GasDrugName")
        drug_subtitle = QLabel("Кислород" if self._is_oxygen else "Ингаляционный анестетик")
        drug_subtitle.setObjectName("GasDrugSubtitle")
        drug_text_col.addStretch(1)
        drug_text_col.addWidget(drug_name)
        drug_text_col.addWidget(drug_subtitle)
        drug_text_col.addStretch(1)
        drug_layout.addLayout(drug_text_col, 1)
        drug_layout.addWidget(
            _create_gas_dialog_plain_icon(
                drug_icon_candidate_keys_from_payload(self._payload, self._gas_name),
                icon_size=92,
                parent=drug_row,
                fallback_file=OXYGEN_ICON_FILE if self._is_oxygen else default_drug_icon_file("gas"),
            ),
            0,
            Qt.AlignRight | Qt.AlignVCenter,
        )
        body_layout.addWidget(drug_row)

        separator = QFrame(body)
        separator.setObjectName("GasSeparator")
        body_layout.addWidget(separator)

        fields = QHBoxLayout()
        fields.setContentsMargins(0, 0, 0, 0)
        fields.setSpacing(14)
        fields.addWidget(self._concentration_card(body), 1)
        if self._show_time:
            fields.addWidget(self._time_card(body), 1)
        body_layout.addLayout(fields)
        main.addWidget(body, 1)

        footer = QFrame(self.bg_container)
        footer.setObjectName("GasDialogFooter")
        footer.setFixedHeight(54)
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(22, 0, 22, 0)
        footer_layout.setSpacing(10)
        footer_layout.addStretch(1)
        cancel_button = QPushButton("Отменить")
        cancel_button.setObjectName("GasCancelButton")
        cancel_button.setFixedSize(118, 38)
        cancel_button.setCursor(Qt.PointingHandCursor)
        cancel_button.clicked.connect(self.reject)
        save_button = QPushButton(self._action_text)
        save_button.setObjectName("GasSaveButton")
        save_button.setFixedSize(138, 38)
        save_button.setCursor(Qt.PointingHandCursor)
        save_button.clicked.connect(self.accept)
        cancel_button.setAutoDefault(False)
        cancel_button.setDefault(False)
        save_button.setAutoDefault(True)
        save_button.setDefault(True)
        footer_layout.addWidget(cancel_button)
        footer_layout.addWidget(save_button)
        main.addWidget(footer)

    def _field_card(self, parent=None) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame(parent)
        card.setObjectName("GasFieldCard")
        card.setMinimumHeight(100)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(6)
        return card, layout

    def _concentration_card(self, parent=None) -> QFrame:
        card, layout = self._field_card(parent)
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)
        title = QLabel("Поток: л/мин" if self._is_oxygen else "Концентрация / MAC")
        title.setObjectName("GasFieldTitle")
        title_row.addWidget(title)
        if not self._is_oxygen:
            info = QPushButton("!")
            info.setObjectName("GasInfoButton")
            info.setToolTip(GAS_MAC_HINT_TOOLTIP)
            info.setFixedSize(18, 18)
            info.setCursor(Qt.PointingHandCursor)
            info.clicked.connect(lambda _=False, button=info: self._show_mac_hint(button))
            title_row.addWidget(info, 0, Qt.AlignVCenter)
        title_row.addStretch(1)
        layout.addLayout(title_row)

        self.volume_input = QLineEdit()
        self.volume_input.setObjectName("GasDoseInput")
        self.volume_input.setFixedHeight(52)
        self.volume_input.setText(self._concentration_text)
        self.volume_input.setPlaceholderText("10 л/мин" if self._is_oxygen else "0,8 MAC")
        self.volume_input.selectAll()
        layout.addWidget(self.volume_input)
        return card

    def _time_card(self, parent=None) -> QFrame:
        card, layout = self._field_card(parent)
        title = QLabel("Время начала")
        title.setObjectName("GasFieldTitle")
        layout.addWidget(title)

        self.time_frame = QFrame()
        self.time_frame.setObjectName("GasTimeInputFrame")
        self.time_frame.setFixedHeight(52)
        self.time_frame.setProperty("focused", False)
        time_layout = QHBoxLayout(self.time_frame)
        time_layout.setContentsMargins(0, 0, 0, 0)
        time_layout.setSpacing(0)

        self.time_input = QLineEdit()
        self.time_input.setObjectName("GasTimeInput")
        start_dt = self._start_datetime or datetime.now().replace(second=0, microsecond=0)
        start_dt = self._coerce_time_datetime(start_dt)
        self.time_input.setText(f"{start_dt.hour:02d}:{start_dt.minute:02d}")
        self.time_input.setPlaceholderText("09:10")
        self.time_input.setMaxLength(5)
        self.time_input.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self.time_input.textEdited.connect(self._on_time_text_edited)
        self.time_input.editingFinished.connect(self._commit_time_text)
        self.time_input.installEventFilter(self)
        time_layout.addWidget(self.time_input, 1)

        stepper = QFrame()
        stepper.setObjectName("GasTimeStepperColumn")
        stepper.setFixedWidth(42)
        stepper_layout = QVBoxLayout(stepper)
        stepper_layout.setContentsMargins(6, 4, 6, 4)
        stepper_layout.setSpacing(4)

        up_button = QPushButton()
        up_button.setObjectName("GasTimeStepButton")
        up_button.setFixedSize(30, 20)
        up_button.setIcon(_gas_time_step_icon(up=True))
        up_button.setIconSize(QSize(14, 14))
        up_button.setCursor(Qt.PointingHandCursor)
        up_button.clicked.connect(lambda _=False: self._step_time(1))
        down_button = QPushButton()
        down_button.setObjectName("GasTimeStepButton")
        down_button.setFixedSize(30, 20)
        down_button.setIcon(_gas_time_step_icon(up=False))
        down_button.setIconSize(QSize(14, 14))
        down_button.setCursor(Qt.PointingHandCursor)
        down_button.clicked.connect(lambda _=False: self._step_time(-1))
        stepper_layout.addWidget(up_button)
        stepper_layout.addWidget(down_button)
        time_layout.addWidget(stepper, 0)

        layout.addWidget(self.time_frame)
        return card

    def volume_text(self) -> str:
        if getattr(self, "_is_oxygen", False):
            return _normalize_oxygen_flow_text(self.volume_input.text())
        return self.volume_input.text().strip()

    def start_time_text(self) -> str:
        if not getattr(self, "_show_time", True):
            return ""
        return self._commit_time_text()

    def accept(self) -> None:
        if getattr(self, "_show_time", True):
            self._commit_time_text()
        if getattr(self, "_is_oxygen", False):
            normalized_flow = _normalize_oxygen_flow_text(self.volume_input.text())
            if normalized_flow:
                self.volume_input.setText(normalized_flow)
        super().accept()

    def eventFilter(self, obj, event):
        if obj is getattr(self, "time_input", None):
            if event.type() == QEvent.FocusIn:
                self._set_time_focus(True)
            elif event.type() == QEvent.FocusOut:
                self._set_time_focus(False)
        return super().eventFilter(obj, event)

    def _set_time_focus(self, focused: bool) -> None:
        frame = getattr(self, "time_frame", None)
        if frame is None:
            return
        frame.setProperty("focused", bool(focused))
        frame.style().unpolish(frame)
        frame.style().polish(frame)

    def _show_mac_hint(self, button: QWidget) -> None:
        QToolTip.showText(
            button.mapToGlobal(button.rect().bottomLeft()),
            GAS_MAC_HINT_TOOLTIP,
            button,
            button.rect(),
            12000,
        )

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
        fallback_dt = (
            self._start_datetime
            or self._time_min_datetime
            or datetime.now().replace(second=0, microsecond=0)
        )
        return self._coerce_time_datetime(fallback_dt)

    def _time_base_datetime(self) -> datetime:
        return (
            self._time_min_datetime
            or self._start_datetime
            or datetime.now().replace(second=0, microsecond=0)
        )

    def _time_datetime_from_text(self, value: str) -> datetime | None:
        minutes = self._time_minutes_from_text(value)
        if minutes is None:
            return None
        hour = minutes // 60
        minute = minutes % 60
        base_dt = self._time_base_datetime()
        same_day = datetime.combine(base_dt.date(), datetime.min.time()).replace(hour=hour, minute=minute)
        if same_day >= base_dt:
            return same_day
        crosses_midnight = hour < 6 or (base_dt.hour >= 12 and hour < base_dt.hour)
        return same_day + timedelta(days=1) if crosses_midnight else same_day

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

    @staticmethod
    def _time_minutes_from_text(value: str) -> int | None:
        raw = str(value or "").strip()
        if not raw:
            return None

        colon_match = re.fullmatch(r"(\d{1,2})\s*:\s*(\d{1,2})", raw)
        if colon_match:
            hour = int(colon_match.group(1))
            minute = int(colon_match.group(2))
        else:
            digits = re.sub(r"\D", "", raw)
            if len(digits) == 4:
                hour = int(digits[:2])
                minute = int(digits[2:])
            elif len(digits) == 3:
                hour = int(digits[:1])
                minute = int(digits[1:])
            else:
                return None

        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour * 60 + minute
        return None


class TimeEditDialog(OperBlockStyledDialog):
    def __init__(self, title: str, base_datetime, parent=None, *, field_label: str = "Время"):
        self._base_datetime = _minute_floor_dt(_parse_datetime_value(base_datetime)) or datetime.now().replace(
            second=0,
            microsecond=0,
        )
        self._field_label = str(field_label or "Время")
        super().__init__(
            title,
            "time_edit_dialog_geometry",
            parent,
            minimum_size=(360, 140),
            initial_size=(400, 155),
        )
        self._init_ui()
        self._finalize_dialog_chrome()

    def _init_ui(self):
        layout = self.content_layout
        time_row = QHBoxLayout()
        time_row.setContentsMargins(0, 0, 0, 0)
        time_row.setSpacing(10)
        time_label = QLabel(self._field_label)
        set_widget_style(time_label, f"font-size: 13px; font-weight: 700; color: {TEXT_PRIMARY};")
        self.time_input = QTimeEdit()
        self.time_input.setDisplayFormat("HH:mm")
        self.time_input.setFixedHeight(34)
        self.time_input.setTime(QTime(self._base_datetime.hour, self._base_datetime.minute))
        set_widget_style(self.time_input, STYLE_PATIENT_FORM_VALID_FIELD)
        time_row.addWidget(time_label, 0)
        time_row.addWidget(self.time_input, 0)
        time_row.addStretch(1)
        layout.addLayout(time_row)

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel_button = QPushButton("Отменить")
        cancel_button.setMinimumHeight(34)
        set_widget_style(cancel_button, STYLE_PATIENT_FORM_CANCEL_BUTTON)
        cancel_button.clicked.connect(self.reject)
        save_button = QPushButton("Сохранить")
        save_button.setMinimumHeight(34)
        set_widget_style(save_button, OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE)
        save_button.clicked.connect(self.accept)
        self._configure_enter_accept_button(cancel_button, save_button)
        actions.addWidget(cancel_button)
        actions.addWidget(save_button)
        layout.addLayout(actions)

    def start_time_text(self) -> str:
        selected = self.time_input.time()
        return f"{selected.hour():02d}:{selected.minute():02d}"

    def datetime_text(self) -> str:
        selected = self.time_input.time()
        value = self._base_datetime.replace(
            hour=selected.hour(),
            minute=selected.minute(),
            second=0,
            microsecond=0,
        )
        return value.isoformat(timespec="seconds")


class OperationStageTimeEditDialog(MedicationEditDialogBase):
    def __init__(
        self,
        base_datetime,
        parent=None,
        *,
        stage_label: str = "",
        field_label: str = "Время этапа",
        min_datetime: datetime | None = None,
        max_datetime: datetime | None = None,
    ):
        base_dt = _minute_floor_dt(_parse_datetime_value(base_datetime)) or datetime.now().replace(second=0, microsecond=0)
        clean_label = re.sub(r"\s+", " ", str(stage_label or "").strip()) or "Этап операции"
        super().__init__(
            title="Время этапа операции",
            drug_name=clean_label,
            subtitle="Этап операции",
            value_label="",
            value_text="",
            placeholder="",
            left_icon_file=type_icon_key("operation_stage"),
            left_icon_background="#EAFBF5",
            right_icon_file=edit_icon_key("operation_stage"),
            right_icon_fallback_file="etap2.png",
            geometry_key="operblock/operation_stage_time_edit_dialog_geometry_v2",
            parent=parent,
            start_datetime=base_dt,
            min_datetime=min_datetime,
            max_datetime=max_datetime,
            show_time=True,
            show_value=False,
            show_route=False,
            time_label=field_label,
            action_text="Сохранить",
            minimum_width=540,
        )
        self.setMinimumSize(max(self.minimumWidth(), 540), max(self.minimumHeight(), 342))
        if self.height() < 342:
            self.resize(max(self.width(), 540), 342)

    def _time_datetime_from_text(self, value: str) -> datetime | None:
        minutes = self._time_minutes_from_text(value)
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

    def datetime_text(self) -> str:
        selected_text = self.start_time_text()
        selected_dt = self._time_datetime_from_text(selected_text) or self._fallback_time_datetime()
        return self._coerce_time_datetime(selected_dt).isoformat(timespec="seconds")
