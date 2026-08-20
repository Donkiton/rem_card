from __future__ import annotations

from rem_card.ui.styles.theme_tokens import token
from rem_card.ui.styles.tooltip_style import TOOLTIP_BG, TOOLTIP_BORDER, TOOLTIP_TEXT


def build_global_style(tokens: dict[str, str]) -> str:
    t = lambda key, default="": token(tokens, key, default)
    return f"""
QWidget {{
    background-color: transparent;
    color: {t("text.primary")};
    font-family: "Segoe UI", Roboto, Arial, sans-serif;
    font-size: 14px;
}}

QMainWindow, QDialog {{
    background-color: {t("surface.window")};
}}

QFrame#MainFrame,
QWidget#bg_container,
QWidget#container,
QWidget#patient_bed_root {{
    background-color: {t("surface.window")} !important;
}}

QToolTip {{
    background-color: {TOOLTIP_BG} !important;
    color: {TOOLTIP_TEXT} !important;
    border: 1px solid {TOOLTIP_BORDER} !important;
    padding: 5px !important;
    font-size: 13px !important;
}}

QLineEdit, QTextEdit, QPlainTextEdit, QDateEdit, QDateTimeEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background-color: {t("field.bg")};
    border: 1px solid {t("field.border")};
    border-radius: {t("radius.sm")};
    padding: 5px;
    color: {t("field.text")};
    selection-background-color: {t("surface.selected")};
    selection-color: {t("text.inverse")};
}}

QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QDateEdit:focus, QDateTimeEdit:focus,
QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 2px solid {t("field.focus_border")};
}}

QLineEdit:disabled, QTextEdit:disabled, QPlainTextEdit:disabled, QDateEdit:disabled,
QDateTimeEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
    background-color: {t("field.disabled_bg")};
    color: {t("text.disabled")};
}}

QPushButton {{
    background-color: {t("button.neutral.bg")};
    color: {t("button.neutral.text")};
    border: 1px solid {t("button.neutral.border")};
    border-radius: {t("radius.md")};
    padding: 8px 16px;
    font-weight: 600;
}}

QPushButton:hover {{
    background-color: {t("button.neutral.hover")};
}}

QPushButton:pressed {{
    background-color: {t("button.neutral.pressed")};
}}

QPushButton:disabled {{
    background-color: {t("surface.window")};
    color: {t("text.disabled")};
    border-color: {t("border.subtle")};
}}

QPushButton#DialogOkBtn, QPushButton#ThemeAccentButton {{
    background-color: {t("button.accent.bg")} !important;
    color: {t("button.accent.text")} !important;
    border: 1px solid {t("button.neutral.border")} !important;
    border-radius: {t("radius.dialog")} !important;
    font-weight: bold !important;
}}

QPushButton#DialogOkBtn:hover, QPushButton#ThemeAccentButton:hover {{
    background-color: {t("button.accent.hover")} !important;
}}

QPushButton#ThemeDangerButton {{
    background-color: {t("button.danger.bg")} !important;
    color: {t("button.danger.text")} !important;
    border: 1px solid {t("border.error")} !important;
}}

QPushButton#ThemeDangerButton:hover {{
    background-color: {t("button.danger.hover")} !important;
}}

QTableView, QTableWidget {{
    background-color: {t("table.bg")};
    alternate-background-color: {t("table.row_alt_bg")};
    border: 1px solid {t("border.default")};
    gridline-color: {t("table.grid")};
    color: {t("text.primary")};
}}

QTableView::item:hover, QTableWidget::item:hover {{
    background-color: {t("table.row_hover_bg")};
}}

QTableView::item:selected, QTableWidget::item:selected {{
    background-color: {t("table.row_selected_bg")};
    color: {t("text.inverse")};
}}

QHeaderView::section {{
    background-color: {t("table.header_bg")};
    color: {t("table.header_text")};
    padding: 6px;
    border: none;
    border-right: 1px solid {t("border.default")};
    border-bottom: 1px solid {t("border.default")};
    font-weight: bold;
}}

QTabWidget::pane {{
    border: 1px solid {t("border.default")};
    background: {t("surface.card")};
}}

QTabBar::tab {{
    background: {t("surface.panel")};
    color: {t("text.secondary")};
    border: 1px solid {t("border.subtle")};
    padding: 6px 12px;
}}

QTabBar::tab:selected {{
    background: {t("surface.card")};
    color: {t("sector.title_text")};
    border-color: {t("border.focus")};
}}

QGroupBox {{
    border: 1px solid {t("border.subtle")};
    border-radius: {t("radius.md")};
    margin-top: 12px;
    padding-top: 12px;
    color: {t("text.primary")};
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 5px;
    color: {t("text.secondary")};
}}

QScrollBar:vertical {{
    border: none;
    background: {t("surface.panel")};
    width: 10px;
    margin: 0px;
}}

QScrollBar::handle:vertical {{
    background: {t("border.default")};
    min-height: 20px;
    border-radius: 5px;
}}

QScrollBar:horizontal {{
    border: none;
    background: {t("surface.panel")};
    height: 10px;
    margin: 0px;
}}

QScrollBar::handle:horizontal {{
    background: {t("border.default")};
    min-width: 20px;
    border-radius: 5px;
}}

QMessageBox {{
    background-color: {t("dialog.bg")} !important;
}}

QMessageBox QLabel {{
    color: {t("text.primary")} !important;
    background-color: transparent !important;
    font-size: 15px !important;
    font-weight: bold !important;
}}

QFrame#MainTitleBar {{
    background-color: {t("titlebar.bg")} !important;
    border-bottom: 1px solid {t("dialog.border")} !important;
    border-top-left-radius: {t("radius.dialog")} !important;
    border-top-right-radius: {t("radius.dialog")} !important;
}}

QLabel#MainTitleText {{
    color: {t("titlebar.text")} !important;
    font-weight: 800 !important;
    font-size: 14px !important;
    background-color: transparent !important;
}}

QPushButton#TitleControlBtn, QPushButton#TitleCloseBtn {{
    background-color: transparent !important;
    color: {t("titlebar.text")} !important;
    font-weight: bold !important;
    font-size: 16px !important;
    border: none !important;
    border-radius: {t("radius.dialog")} !important;
    padding: 0px !important;
    min-width: 40px !important;
    margin: 2px 1px !important;
}}

QPushButton#TitleControlBtn:hover {{
    background-color: {t("titlebar.button_hover")} !important;
}}

QPushButton#TitleCloseBtn:hover {{
    background-color: {t("titlebar.close_hover")} !important;
    color: {t("text.inverse")} !important;
}}
"""
