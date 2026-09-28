from __future__ import annotations
from rem_card.ui.styles.theme_runtime import set_widget_style

import os

from PySide6.QtCore import Qt, QTime
from PySide6.QtGui import (
    QIcon,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTimeEdit,
    QVBoxLayout,
)

from rem_card.ui.shared.operblock_icon_settings import request_operblock_icon_pixmap
from rem_card.ui.styles.theme import (
    BORDER_LIGHT,
    STYLE_PATIENT_FORM_CANCEL_BUTTON,
    STYLE_PATIENT_FORM_VALID_FIELD,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)
from rem_card.ui.operblock_view.operblock_control_styles import (
    operblock_arrow_icon,
)

from rem_card.ui.operblock_view.operblock_visual_primitives import (
    OPERBLOCK_ORDER_ROUTE_DEFAULT,
    OPERBLOCK_ORDER_ROUTE_INTRAMUSCULAR,
    OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE,
)
from rem_card.ui.operblock_view.operblock_helpers import (
    _line_edit,
    _parse_datetime_value,
    _minute_floor_dt,
    _normalize_order_route_code,
)
from rem_card.ui.operblock_view.operblock_dialog_base import (
    OperBlockStyledDialog,
)

class EditOrderDialog(OperBlockStyledDialog):
    def __init__(self, text: str, parent=None, *, base_datetime=None, route_code: str = OPERBLOCK_ORDER_ROUTE_DEFAULT):
        self._base_datetime = _minute_floor_dt(_parse_datetime_value(base_datetime)) if base_datetime else None
        self._route_code = _normalize_order_route_code(route_code)
        height = 290 if self._base_datetime else 235
        super().__init__(
            "Редактировать назначение",
            "edit_order_dialog_geometry",
            parent,
            minimum_size=(620, height),
            initial_size=(660, height + 25),
        )
        self._init_ui(str(text or ""))
        self._finalize_dialog_chrome()

    def _init_ui(self, text: str):
        layout = self.content_layout

        text_label = QLabel("Назначение")
        set_widget_style(text_label, f"font-size: 13px; font-weight: 700; color: {TEXT_PRIMARY};")
        layout.addWidget(text_label)

        self.text_input = _line_edit()
        self.text_input.setText(text)
        self.text_input.selectAll()
        layout.addWidget(self.text_input)

        if self._base_datetime is not None:
            time_row = QHBoxLayout()
            time_row.setContentsMargins(0, 0, 0, 0)
            time_row.setSpacing(10)
            time_label = QLabel("Время введения")
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

        route_label = QLabel("Место введения")
        set_widget_style(route_label, f"font-size: 13px; font-weight: 700; color: {TEXT_PRIMARY};")
        layout.addWidget(route_label)

        route_row = QHBoxLayout()
        route_row.setContentsMargins(0, 0, 0, 0)
        route_row.setSpacing(8)
        self.route_button_group = QButtonGroup(self)
        self.route_button_group.setExclusive(True)
        self.route_iv_button = self._route_button("в/в", OPERBLOCK_ORDER_ROUTE_DEFAULT)
        self.route_im_button = self._route_button("в/м", OPERBLOCK_ORDER_ROUTE_INTRAMUSCULAR)
        route_row.addWidget(self.route_iv_button, 0)
        route_row.addWidget(self.route_im_button, 0)
        route_row.addStretch(1)
        layout.addLayout(route_row)
        if self._route_code == OPERBLOCK_ORDER_ROUTE_INTRAMUSCULAR:
            self.route_im_button.setChecked(True)
        else:
            self.route_iv_button.setChecked(True)

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

    def _route_button(self, text: str, route_code: str) -> QPushButton:
        button = QPushButton(text)
        button.setCheckable(True)
        button.setMinimumSize(70, 32)
        button.setCursor(Qt.PointingHandCursor)
        button.setProperty("route_code", route_code)
        set_widget_style(button, f"""
            QPushButton {{
                background-color: #F1F5F9;
                border: 1px solid {BORDER_LIGHT};
                border-radius: 5px;
                color: {TEXT_SECONDARY};
                font-size: 13px;
                font-weight: 700;
            }}
            QPushButton:checked {{
                background-color: #DFF4E8;
                border-color: #2F8A57;
                color: #17633A;
            }}
            """)
        self.route_button_group.addButton(button)
        return button

    def text(self) -> str:
        return self.text_input.text().strip()

    def route_code(self) -> str:
        checked = self.route_button_group.checkedButton()
        if checked is None:
            return OPERBLOCK_ORDER_ROUTE_DEFAULT
        return _normalize_order_route_code(checked.property("route_code"))

    def datetime_text(self) -> str:
        if self._base_datetime is None:
            return ""
        selected = self.time_input.time()
        value = self._base_datetime.replace(
            hour=selected.hour(),
            minute=selected.minute(),
            second=0,
            microsecond=0,
        )
        return value.isoformat(timespec="seconds")


GAS_MAC_HINT_TEXT = (
    "MAC (минимальная альвеолярная концентрация) — стандартная мера силы "
    "ингаляционного анестетика. 1,0 MAC соответствует концентрации газа, "
    "при которой 50% пациентов не реагируют на хирургический разрез."
)
GAS_MAC_HINT_TOOLTIP = (
    "<div style='width: 340px; white-space: normal;'>"
    "<b>MAC (минимальная альвеолярная концентрация)</b><br>"
    "Стандартная мера силы ингаляционного анестетика.<br><br>"
    "1,0 MAC соответствует концентрации газа, при которой 50% пациентов "
    "не реагируют на хирургический разрез."
    "</div>"
)


def _create_gas_dialog_image_icon(
    icon_ref,
    *,
    frame_size: int,
    icon_size: int,
    background: str,
    parent=None,
    fallback_file: str = "",
) -> QFrame:
    frame = QFrame(parent)
    frame.setFixedSize(frame_size, frame_size)
    set_widget_style(frame, f"background-color: {background}; border: none; border-radius: {frame_size // 2}px;")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(0, 0, 0, 0)
    icon_label = QLabel(frame)
    icon_label.setAlignment(Qt.AlignCenter)
    fallback = str(fallback_file or "").strip()
    if not fallback and isinstance(icon_ref, str) and os.path.splitext(icon_ref)[1]:
        fallback = icon_ref
    pixmap = request_operblock_icon_pixmap(
        icon_label,
        icon_ref,
        fallback_file=fallback,
        target_size=(icon_size, icon_size),
    )
    if not pixmap.isNull():
        icon_label.setPixmap(pixmap)
    layout.addWidget(icon_label, 1)
    return frame


def _create_gas_dialog_plain_icon(icon_ref, *, icon_size: int, parent=None, fallback_file: str = "") -> QLabel:
    icon_label = QLabel(parent)
    icon_label.setFixedSize(icon_size, icon_size)
    icon_label.setAlignment(Qt.AlignCenter)
    fallback = str(fallback_file or "").strip()
    if not fallback and isinstance(icon_ref, str) and os.path.splitext(icon_ref)[1]:
        fallback = icon_ref
    pixmap = request_operblock_icon_pixmap(
        icon_label,
        icon_ref,
        fallback_file=fallback,
        target_size=(icon_size, icon_size),
    )
    if not pixmap.isNull():
        icon_label.setPixmap(pixmap)
    return icon_label


def _gas_time_step_icon(*, up: bool) -> QIcon:
    return operblock_arrow_icon(up=up)
