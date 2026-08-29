from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QDateTimeEdit, QWidget

from rem_card.ui.styles.theme import (
    FORM_DROPDOWN_ARROW_IMAGE,
    FORM_SPIN_DOWN_ARROW_IMAGE,
    FORM_SPIN_UP_ARROW_IMAGE,
)
from rem_card.ui.styles.theme_tokens import token


def _value(tokens: dict[str, str], key: str, default: str) -> str:
    return token(tokens, key, default)


def build_oral_nutrition_style(tokens: dict[str, str]) -> str:
    def t(key: str, default: str) -> str:
        return _value(tokens, key, default)

    return f"""
QWidget#OralNutritionRoot {{
    background-color: {t("surface.window", "#f8f9fa")};
}}
QFrame#OralNutritionOuterFrame {{
    background: transparent;
    border: none;
}}
QLabel#OralNutritionOuterHeader {{
    background-color: {t("sector.header_bg", "#e9ecef")};
    color: {t("sector.header_text", "#2c3e50")};
    border-top: 1.5px solid {t("sector.border", "#bdc3c7")};
    border-left: 1.5px solid {t("sector.border", "#bdc3c7")};
    border-right: 1.5px solid {t("sector.border", "#bdc3c7")};
    border-bottom: 0.5px solid {t("sector.border", "#bdc3c7")};
    border-top-left-radius: {t("radius.dialog", "5px")};
    border-top-right-radius: {t("radius.dialog", "5px")};
    font-size: 14px;
    font-weight: 700;
}}
QWidget#OralNutritionOuterBody {{
    background-color: {t("surface.window", "#f8f9fa")};
    border-left: 1.5px solid {t("sector.border", "#bdc3c7")};
    border-right: 1.5px solid {t("sector.border", "#bdc3c7")};
    border-bottom: 1.5px solid {t("sector.border", "#bdc3c7")};
    border-top: none;
    border-bottom-left-radius: {t("radius.dialog", "5px")};
    border-bottom-right-radius: {t("radius.dialog", "5px")};
}}
QFrame#OralNutritionSummary,
QFrame#OralNutritionSectionCard {{
    background-color: {t("surface.card", "#ffffff")};
    border: 1.5px solid {t("sector.border", "#bdc3c7")};
    border-radius: {t("radius.md", "7px")};
}}
QFrame#OralNutritionSummary {{
    border-left: 4px solid {t("button.accent.bg", "#2f80c0")};
}}
QLabel#OralNutritionTitle {{
    color: {t("sector.title_text", "#2c3e50")};
    background: transparent;
    border: none;
    font-size: 15px;
    font-weight: 700;
}}
QLabel#OralNutritionMeta {{
    color: {t("text.secondary", "#566573")};
    background: transparent;
    border: none;
    font-size: 12px;
}}
QLabel#OralNutritionSectionTitle {{
    color: {t("sector.title_text", "#2c3e50")};
    background: transparent;
    border: none;
    font-size: 13px;
    font-weight: 700;
}}
QLabel#OralNutritionStatus {{
    color: {t("text.secondary", "#566573")};
    background-color: {t("surface.panel", "#eef2f6")};
    border: 1px solid {t("border.subtle", "#d9e2ec")};
    border-radius: {t("radius.sm", "5px")};
    padding: 5px 9px;
    font-size: 12px;
}}
QPushButton#OralPrimaryButton,
QPushButton#OralSecondaryButton,
QPushButton#OralDangerButton {{
    border-radius: {t("radius.md", "7px")};
    padding: 6px 12px;
    min-height: 20px;
    font-size: 12px;
    font-weight: 650;
}}
QPushButton#OralPrimaryButton {{
    background-color: {t("button.accent.bg", "#2f80c0")};
    color: {t("button.accent.text", "#ffffff")};
    border: 1px solid {t("border.focus", "#23689f")};
}}
QPushButton#OralPrimaryButton:hover {{
    background-color: {t("button.accent.hover", "#236fa9")};
}}
QPushButton#OralSecondaryButton {{
    background-color: {t("surface.subtle", "#f4f7fb")};
    color: {t("text.primary", "#172033")};
    border: 1px solid {t("border.default", "#b9c5d3")};
}}
QPushButton#OralSecondaryButton:hover {{
    background-color: {t("surface.hover", "#e8f1fb")};
    border-color: {t("border.focus", "#7aa6d8")};
}}
QPushButton#OralDangerButton {{
    background-color: {t("button.danger.bg", "#f7e5e3")};
    color: {t("button.danger.text", "#8b2f28")};
    border: 1px solid {t("border.error", "#d89a94")};
}}
QPushButton#OralDangerButton:hover {{
    background-color: {t("button.danger.hover", "#e9c5c1")};
}}
QPushButton#OralPrimaryButton:pressed,
QPushButton#OralSecondaryButton:pressed,
QPushButton#OralDangerButton:pressed {{
    background-color: {t("button.neutral.pressed", "#d5e2ef")};
}}
QPushButton#OralPrimaryButton:disabled,
QPushButton#OralSecondaryButton:disabled,
QPushButton#OralDangerButton:disabled {{
    background-color: {t("surface.subtle", "#f1f5f9")};
    color: {t("text.disabled", "#8a96a6")};
    border-color: {t("border.subtle", "#d9e2ec")};
}}
QTableWidget#OralIntakeTable,
QTableWidget#OralVersionTable,
QTableWidget#OralTotalsTable {{
    background-color: {t("table.bg", "#ffffff")};
    alternate-background-color: {t("table.row_alt_bg", "#f8fafc")};
    color: {t("text.primary", "#172033")};
    border: 1px solid {t("border.default", "#bdc3c7")};
    border-radius: {t("radius.sm", "5px")};
    gridline-color: {t("table.grid", "#d9e2ec")};
    selection-background-color: {t("table.cell_selected_bg", "#dbeafe")};
    selection-color: {t("table.cell_selected_text", "#172033")};
    outline: none;
}}
QTableWidget#OralIntakeTable::item,
QTableWidget#OralVersionTable::item,
QTableWidget#OralTotalsTable::item {{
    padding: 4px 6px;
    border: none;
}}
QTableWidget#OralIntakeTable::item:hover,
QTableWidget#OralVersionTable::item:hover,
QTableWidget#OralTotalsTable::item:hover {{
    background-color: {t("table.row_hover_bg", "#eef6ff")};
}}
QTableWidget#OralIntakeTable QHeaderView::section,
QTableWidget#OralVersionTable QHeaderView::section,
QTableWidget#OralTotalsTable QHeaderView::section {{
    background-color: {t("table.header_bg", "#e8eef5")};
    color: {t("table.header_text", "#172033")};
    padding: 6px;
    border: none;
    border-right: 1px solid {t("table.grid", "#d9e2ec")};
    border-bottom: 1px solid {t("table.grid", "#d9e2ec")};
    font-weight: 700;
}}
QTableWidget#OralIntakeTable QTableCornerButton::section,
QTableWidget#OralVersionTable QTableCornerButton::section,
QTableWidget#OralTotalsTable QTableCornerButton::section {{
    background-color: {t("table.header_bg", "#e8eef5")};
    border: none;
}}
"""


def build_oral_nutrition_dialog_style(tokens: dict[str, str]) -> str:
    def t(key: str, default: str) -> str:
        return _value(tokens, key, default)

    return f"""
QFrame#OralNutritionDialogBody {{
    background-color: {t("surface.window", "#f8f9fa")};
    border-bottom-left-radius: {t("radius.dialog", "5px")};
    border-bottom-right-radius: {t("radius.dialog", "5px")};
}}
QFrame#OralDialogSection {{
    background-color: {t("surface.card", "#ffffff")};
    border: 1px solid {t("border.default", "#bdc3c7")};
    border-radius: {t("radius.md", "7px")};
}}
QLabel#OralDialogSectionTitle {{
    color: {t("sector.title_text", "#2c3e50")};
    background: transparent;
    border: none;
    font-size: 13px;
    font-weight: 700;
}}
QLabel#OralDialogFieldLabel {{
    color: {t("text.primary", "#172033")};
    background: transparent;
    border: none;
    font-size: 12px;
    font-weight: 600;
}}
QLineEdit,
QTextEdit,
QComboBox,
QDateTimeEdit,
QSpinBox {{
    background-color: {t("field.bg", "#ffffff")};
    color: {t("field.text", "#172033")};
    border: 1px solid {t("field.border", "#b9c5d3")};
    border-radius: {t("radius.sm", "5px")};
    padding: 5px 8px;
    selection-background-color: {t("surface.selected", "#dbeafe")};
    selection-color: {t("text.primary", "#172033")};
}}
QComboBox,
QDateTimeEdit {{
    padding-right: 32px;
}}
QLineEdit:focus,
QTextEdit:focus,
QComboBox:focus,
QDateTimeEdit:focus,
QSpinBox:focus {{
    border: 2px solid {t("field.focus_border", "#3b82c4")};
}}
QLineEdit:disabled,
QTextEdit:disabled,
QComboBox:disabled,
QDateTimeEdit:disabled,
QSpinBox:disabled {{
    background-color: {t("field.disabled_bg", "#f1f5f9")};
    color: {t("text.disabled", "#7a8696")};
    border-color: {t("border.subtle", "#d9e2ec")};
}}
QComboBox::drop-down,
QDateTimeEdit::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 27px;
    border: none;
    border-left: 1px solid {t("field.border", "#b9c5d3")};
    background-color: {t("surface.subtle", "#f4f7fb")};
    border-top-right-radius: {t("radius.sm", "5px")};
    border-bottom-right-radius: {t("radius.sm", "5px")};
}}
QComboBox::drop-down:hover,
QDateTimeEdit::drop-down:hover {{
    background-color: {t("surface.hover", "#e8f1fb")};
}}
QComboBox::down-arrow,
QDateTimeEdit::down-arrow {{
    image: {FORM_DROPDOWN_ARROW_IMAGE};
    width: 12px;
    height: 12px;
}}
QSpinBox::up-button {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 24px;
    border-left: 1px solid {t("field.border", "#b9c5d3")};
    border-bottom: 1px solid {t("field.border", "#b9c5d3")};
    background-color: {t("surface.subtle", "#f4f7fb")};
    border-top-right-radius: {t("radius.sm", "5px")};
}}
QSpinBox::down-button {{
    subcontrol-origin: padding;
    subcontrol-position: bottom right;
    width: 24px;
    border-left: 1px solid {t("field.border", "#b9c5d3")};
    background-color: {t("surface.subtle", "#f4f7fb")};
    border-bottom-right-radius: {t("radius.sm", "5px")};
}}
QSpinBox::up-button:hover,
QSpinBox::down-button:hover {{
    background-color: {t("surface.hover", "#e8f1fb")};
}}
QSpinBox::up-arrow {{
    image: {FORM_SPIN_UP_ARROW_IMAGE};
    width: 10px;
    height: 10px;
}}
QSpinBox::down-arrow {{
    image: {FORM_SPIN_DOWN_ARROW_IMAGE};
    width: 10px;
    height: 10px;
}}
QCheckBox {{
    color: {t("text.primary", "#172033")};
    background: transparent;
    spacing: 7px;
    padding: 3px 2px;
}}
QTableWidget#OralScheduleTable {{
    background-color: {t("table.bg", "#ffffff")};
    alternate-background-color: {t("table.row_alt_bg", "#f8fafc")};
    color: {t("text.primary", "#172033")};
    border: 1px solid {t("border.default", "#bdc3c7")};
    border-radius: {t("radius.sm", "5px")};
    gridline-color: {t("table.grid", "#d9e2ec")};
    selection-background-color: {t("table.cell_selected_bg", "#dbeafe")};
    selection-color: {t("table.cell_selected_text", "#172033")};
    outline: none;
}}
QTableWidget#OralScheduleTable::item {{
    padding: 4px 6px;
}}
QTableWidget#OralScheduleTable QHeaderView::section {{
    background-color: {t("table.header_bg", "#e8eef5")};
    color: {t("table.header_text", "#172033")};
    padding: 6px;
    border: none;
    border-right: 1px solid {t("table.grid", "#d9e2ec")};
    border-bottom: 1px solid {t("table.grid", "#d9e2ec")};
    font-weight: 700;
}}
QComboBox QAbstractItemView {{
    background-color: {t("surface.card", "#ffffff")};
    color: {t("text.primary", "#172033")};
    border: 1px solid {t("field.border", "#b9c5d3")};
    selection-background-color: {t("table.cell_selected_bg", "#dbeafe")};
    selection-color: {t("table.cell_selected_text", "#172033")};
    outline: none;
}}
QPushButton#OralDialogPrimaryButton,
QPushButton#OralDialogSecondaryButton,
QPushButton#OralDialogDangerButton {{
    border-radius: {t("radius.md", "7px")};
    padding: 7px 16px;
    min-width: 105px;
    min-height: 20px;
    font-size: 12px;
    font-weight: 700;
}}
QPushButton#OralDialogPrimaryButton {{
    background-color: {t("button.accent.bg", "#2f80c0")};
    color: {t("button.accent.text", "#ffffff")};
    border: 1px solid {t("border.focus", "#23689f")};
}}
QPushButton#OralDialogPrimaryButton:hover {{
    background-color: {t("button.accent.hover", "#236fa9")};
}}
QPushButton#OralDialogSecondaryButton {{
    background-color: {t("surface.subtle", "#f4f7fb")};
    color: {t("text.primary", "#172033")};
    border: 1px solid {t("border.default", "#b9c5d3")};
}}
QPushButton#OralDialogSecondaryButton:hover {{
    background-color: {t("surface.hover", "#e8f1fb")};
    border-color: {t("border.focus", "#7aa6d8")};
}}
QPushButton#OralDialogDangerButton {{
    background-color: {t("button.danger.bg", "#f7e5e3")};
    color: {t("button.danger.text", "#8b2f28")};
    border: 1px solid {t("border.error", "#d89a94")};
}}
QPushButton#OralDialogDangerButton:hover {{
    background-color: {t("button.danger.hover", "#e9c5c1")};
}}
QPushButton#OralDialogPrimaryButton:disabled,
QPushButton#OralDialogSecondaryButton:disabled,
QPushButton#OralDialogDangerButton:disabled {{
    background-color: {t("surface.subtle", "#f1f5f9")};
    color: {t("text.disabled", "#8a96a6")};
    border-color: {t("border.subtle", "#d9e2ec")};
}}
QCalendarWidget QWidget {{
    background-color: {t("surface.card", "#ffffff")};
    color: {t("text.primary", "#172033")};
}}
QCalendarWidget QToolButton {{
    background-color: {t("surface.subtle", "#f4f7fb")};
    color: {t("text.primary", "#172033")};
    border: 1px solid {t("border.subtle", "#d9e2ec")};
    border-radius: {t("radius.sm", "5px")};
    padding: 4px 7px;
}}
QCalendarWidget QAbstractItemView {{
    background-color: {t("surface.card", "#ffffff")};
    color: {t("text.primary", "#172033")};
    selection-background-color: {t("table.cell_selected_bg", "#dbeafe")};
    selection-color: {t("table.cell_selected_text", "#172033")};
    outline: none;
}}
"""


def apply_oral_popup_styles(root: QWidget, tokens: dict[str, str]) -> None:
    def t(key: str, default: str) -> str:
        return _value(tokens, key, default)

    popup_style = f"""
QAbstractItemView {{
    background-color: {t("surface.card", "#ffffff")};
    alternate-background-color: {t("table.row_alt_bg", "#f8fafc")};
    color: {t("text.primary", "#172033")};
    border: 1px solid {t("field.border", "#b9c5d3")};
    selection-background-color: {t("table.cell_selected_bg", "#dbeafe")};
    selection-color: {t("table.cell_selected_text", "#172033")};
    outline: none;
}}
QAbstractItemView::item {{
    min-height: 24px;
    padding: 4px 8px;
}}
QAbstractItemView::item:hover {{
    background-color: {t("surface.hover", "#eef6ff")};
}}
"""
    combos = [root] if isinstance(root, QComboBox) else []
    combos.extend(root.findChildren(QComboBox))
    for combo in combos:
        combo.view().setStyleSheet(popup_style)

    date_edits = [root] if isinstance(root, QDateTimeEdit) else []
    date_edits.extend(root.findChildren(QDateTimeEdit))
    for editor in date_edits:
        if editor.calendarPopup():
            editor.calendarWidget().setStyleSheet(
                build_oral_nutrition_dialog_style(tokens)
            )
