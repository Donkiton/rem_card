from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap, QTransform

from rem_card.app.paths import get_icon_dir
from rem_card.ui.styles.theme import BG_CARD, BORDER_COLOR, BORDER_LIGHT, CUSTOM_DIALOG_RADIUS, TEXT_PRIMARY


OPERBLOCK_SCROLLBAR_GROOVE_BG = "#e8edf3"
OPERBLOCK_SCROLLBAR_GROOVE_BORDER = "#d4dce7"
OPERBLOCK_SCROLLBAR_HANDLE_BG = "#8fa3ba"
OPERBLOCK_SCROLLBAR_HANDLE_BORDER = "#71869d"
OPERBLOCK_SCROLLBAR_HANDLE_HOVER_BG = "#7890aa"

OPERBLOCK_ARROW_BUTTON_BG = "#EAF3FF"
OPERBLOCK_ARROW_BUTTON_BORDER = "#BFD1E5"
OPERBLOCK_ARROW_BUTTON_HOVER_BG = "#DCEBFA"
OPERBLOCK_ARROW_BUTTON_HOVER_BORDER = "#9FB8D2"
OPERBLOCK_ARROW_BUTTON_PRESSED_BG = "#CFE3F7"


def operblock_vertical_scrollbar_style(
    object_name: str,
    *,
    width_px: int = 13,
    left_margin_px: int = 2,
    right_margin_px: int = 1,
) -> str:
    return f"""
        QScrollBar#{object_name} {{
            background: transparent;
            margin: 6px {right_margin_px}px 6px {left_margin_px}px;
            width: {width_px}px;
        }}
        QScrollBar#{object_name}::groove:vertical {{
            background: {OPERBLOCK_SCROLLBAR_GROOVE_BG};
            border: 1px solid {OPERBLOCK_SCROLLBAR_GROOVE_BORDER};
            border-radius: 4px;
            width: 7px;
        }}
        QScrollBar#{object_name}::handle:vertical {{
            background: {OPERBLOCK_SCROLLBAR_HANDLE_BG};
            border: 1px solid {OPERBLOCK_SCROLLBAR_HANDLE_BORDER};
            border-radius: 4px;
            min-height: 28px;
        }}
        QScrollBar#{object_name}::handle:vertical:hover {{
            background: {OPERBLOCK_SCROLLBAR_HANDLE_HOVER_BG};
        }}
        QScrollBar#{object_name}::add-line:vertical,
        QScrollBar#{object_name}::sub-line:vertical {{
            height: 0px;
            background: transparent;
            border: none;
        }}
        QScrollBar#{object_name}::add-page:vertical,
        QScrollBar#{object_name}::sub-page:vertical {{
            background: transparent;
        }}
    """


def operblock_horizontal_scrollbar_style(
    object_name: str,
    *,
    height_px: int = 13,
    left_margin_px: int = 6,
    right_margin_px: int = 6,
    top_margin_px: int = 2,
    bottom_margin_px: int = 1,
) -> str:
    return f"""
        QScrollBar#{object_name} {{
            background: transparent;
            margin: {top_margin_px}px {right_margin_px}px {bottom_margin_px}px {left_margin_px}px;
            height: {height_px}px;
        }}
        QScrollBar#{object_name}::groove:horizontal {{
            background: {OPERBLOCK_SCROLLBAR_GROOVE_BG};
            border: 1px solid {OPERBLOCK_SCROLLBAR_GROOVE_BORDER};
            border-radius: 4px;
            height: 7px;
        }}
        QScrollBar#{object_name}::handle:horizontal {{
            background: {OPERBLOCK_SCROLLBAR_HANDLE_BG};
            border: 1px solid {OPERBLOCK_SCROLLBAR_HANDLE_BORDER};
            border-radius: 4px;
            min-width: 34px;
        }}
        QScrollBar#{object_name}::handle:horizontal:hover {{
            background: {OPERBLOCK_SCROLLBAR_HANDLE_HOVER_BG};
        }}
        QScrollBar#{object_name}::add-line:horizontal,
        QScrollBar#{object_name}::sub-line:horizontal {{
            width: 0px;
            background: transparent;
            border: none;
        }}
        QScrollBar#{object_name}::add-page:horizontal,
        QScrollBar#{object_name}::sub-page:horizontal {{
            background: transparent;
        }}
    """


def operblock_arrow_button_style(selector: str) -> str:
    return f"""
        {selector} {{
            background-color: {OPERBLOCK_ARROW_BUTTON_BG};
            border: 1px solid {OPERBLOCK_ARROW_BUTTON_BORDER};
            border-radius: 5px;
            padding: 0;
        }}
        {selector}:hover {{
            background-color: {OPERBLOCK_ARROW_BUTTON_HOVER_BG};
            border-color: {OPERBLOCK_ARROW_BUTTON_HOVER_BORDER};
        }}
        {selector}:pressed {{
            background-color: {OPERBLOCK_ARROW_BUTTON_PRESSED_BG};
        }}
    """


def operblock_med_action_button_style(cancel_selector: str, save_selector: str) -> str:
    return f"""
        {cancel_selector} {{
            background-color: #FFFFFF;
            color: #111827;
            border: 1px solid #D1D5DB;
            border-radius: 8px;
            font-size: 13px;
            font-weight: 600;
        }}
        {cancel_selector}:hover {{
            background-color: #F3F4F6;
            border-color: #B8C0CC;
        }}
        {save_selector} {{
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #6366F1, stop:1 #4F46E5);
            color: #FFFFFF;
            border: 1px solid #4F46E5;
            border-radius: 8px;
            font-size: 13px;
            font-weight: 700;
        }}
        {save_selector}:hover {{
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #7377F7, stop:1 #5B52EA);
            border-color: #6366F1;
        }}
    """


def operblock_arrow_icon(*, up: bool = False) -> QIcon:
    pixmap = QPixmap(os.path.join(get_icon_dir(), "combo_arrow_down.svg"))
    if pixmap.isNull():
        return QIcon()
    if up:
        pixmap = pixmap.transformed(QTransform().rotate(180), Qt.SmoothTransformation)
    return QIcon(pixmap)


def operblock_combo_box_style() -> str:
    arrow_path = os.path.join(get_icon_dir(), "combo_arrow_down.svg")
    arrow_image = f"url({arrow_path.replace(os.sep, '/')})" if os.path.exists(arrow_path) else "none"
    return f"""
        QComboBox {{
            padding: 7px 38px 7px 8px;
            border: 1px solid {BORDER_LIGHT};
            border-radius: {CUSTOM_DIALOG_RADIUS};
            background: {BG_CARD};
            color: {TEXT_PRIMARY};
        }}
        QComboBox:focus {{
            border: 1px solid {BORDER_COLOR};
            background: {BG_CARD};
        }}
        QComboBox::drop-down {{
            subcontrol-origin: border;
            subcontrol-position: top right;
            width: 28px;
            margin: 3px 3px 3px 0;
            border: 1px solid {OPERBLOCK_ARROW_BUTTON_BORDER};
            border-radius: 5px;
            background: {OPERBLOCK_ARROW_BUTTON_BG};
        }}
        QComboBox::drop-down:hover {{
            border-color: {OPERBLOCK_ARROW_BUTTON_HOVER_BORDER};
            background: {OPERBLOCK_ARROW_BUTTON_HOVER_BG};
        }}
        QComboBox::down-arrow {{
            image: {arrow_image};
            width: 12px;
            height: 12px;
        }}
        QComboBox QAbstractItemView {{
            background: {BG_CARD};
            color: {TEXT_PRIMARY};
            border: 1px solid {BORDER_COLOR};
            selection-background-color: #e3edf7;
            selection-color: {TEXT_PRIMARY};
            outline: 0;
        }}
        QComboBox QAbstractItemView::item {{
            min-height: 24px;
            padding: 4px 8px;
        }}
    """
