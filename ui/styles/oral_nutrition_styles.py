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
    border-radius: 5px;
    padding: 6px 12px;
    min-height: 20px;
}}
QPushButton#OralPrimaryButton {{
    background-color: #2f80c0;
    color: #ffffff;
    border: 1px solid #23689f;
    padding: 6px 18px;
    font-weight: bold;
    min-width: 110px;
}}
QPushButton#OralPrimaryButton:hover {{
    background-color: #236fa9;
}}
QPushButton#OralSecondaryButton {{
    background-color: #f4f7fb;
    color: #172033;
    border: 1px solid #b9c5d3;
}}
QPushButton#OralSecondaryButton:hover {{
    background-color: #e8f1fb;
    border-color: #7aa6d8;
}}
QPushButton#OralDangerButton {{
    background-color: #f7e5e3;
    color: #8b2f28;
    border: 1px solid #d89a94;
    font-weight: bold;
}}
QPushButton#OralDangerButton:hover {{
    background-color: #e9c5c1;
}}
QPushButton#OralPrimaryButton:pressed {{
    background-color: #23689f;
}}
QPushButton#OralSecondaryButton:pressed {{
    background-color: #d5e2ef;
}}
QPushButton#OralDangerButton:pressed {{
    background-color: #ddb5b0;
}}
QPushButton#OralPrimaryButton:disabled {{
    background-color: #9dbbd3;
    color: #edf4fa;
    border-color: #9dbbd3;
}}
QPushButton#OralSecondaryButton:disabled,
QPushButton#OralDangerButton:disabled {{
    background-color: #f1f5f9;
    color: #8a96a6;
    border-color: #d9e2ec;
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
    background-color: #d9e2ec;
    color: #243b53;
    padding: 6px;
    border: none;
    border-right: 1px solid {t("table.grid", "#d9e2ec")};
    border-bottom: 1px solid {t("table.grid", "#d9e2ec")};
    font-weight: 700;
}}
QTableWidget#OralIntakeTable QHeaderView::section:hover,
QTableWidget#OralVersionTable QHeaderView::section:hover,
QTableWidget#OralTotalsTable QHeaderView::section:hover {{
    background-color: #cbd7e5;
}}
QTableWidget#OralIntakeTable QTableCornerButton::section,
QTableWidget#OralVersionTable QTableCornerButton::section,
QTableWidget#OralTotalsTable QTableCornerButton::section {{
    background-color: #d9e2ec;
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
    background-color: #d9e2ec;
    color: #243b53;
    padding: 6px;
    border: none;
    border-right: 1px solid {t("table.grid", "#d9e2ec")};
    border-bottom: 1px solid {t("table.grid", "#d9e2ec")};
    font-weight: 700;
}}
QTableWidget#OralScheduleTable QHeaderView::section:hover {{
    background-color: #cbd7e5;
}}
QTableWidget#OralScheduleTable QTableCornerButton::section {{
    background-color: #d9e2ec;
    border: none;
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
    border-radius: 5px;
    padding: 6px 18px;
    min-width: 110px;
    font-weight: bold;
}}
QPushButton#OralDialogPrimaryButton {{
    background-color: #2f80c0;
    color: #ffffff;
    border: 1px solid #23689f;
}}
QPushButton#OralDialogPrimaryButton:hover {{
    background-color: #236fa9;
}}
QPushButton#OralDialogSecondaryButton {{
    background-color: #f4f7fb;
    color: #172033;
    border: 1px solid #b9c5d3;
}}
QPushButton#OralDialogSecondaryButton:hover {{
    background-color: #e5ebf2;
}}
QPushButton#OralDialogDangerButton {{
    background-color: #f7e5e3;
    color: #8b2f28;
    border: 1px solid #d89a94;
}}
QPushButton#OralDialogDangerButton:hover {{
    background-color: #e9c5c1;
}}
QPushButton#OralDialogPrimaryButton:pressed {{
    background-color: #23689f;
}}
QPushButton#OralDialogSecondaryButton:pressed {{
    background-color: #d5e2ef;
}}
QPushButton#OralDialogDangerButton:pressed {{
    background-color: #ddb5b0;
}}
QPushButton#OralDialogPrimaryButton:disabled {{
    background-color: #9dbbd3;
    color: #edf4fa;
    border-color: #9dbbd3;
}}
QPushButton#OralDialogSecondaryButton:disabled,
QPushButton#OralDialogDangerButton:disabled {{
    background-color: #f1f5f9;
    color: #8a96a6;
    border-color: #d9e2ec;
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
    # PySide returns Python wrappers for Qt-owned popup views/calendars. If
    # these wrappers only live as temporaries, cyclic GC can finalize them on
    # whichever Python worker happens to trigger a collection. Destroying a
    # popup (and its basic timer) outside the GUI thread corrupts Qt's native
    # heap on Windows. Keep them alive for the complete dialog lifetime.
    retained_popups = []

    combos = [root] if isinstance(root, QComboBox) else []
    combos.extend(root.findChildren(QComboBox))
    for combo in combos:
        view = combo.view()
        view.setStyleSheet(popup_style)
        retained_popups.append(view)

    date_edits = [root] if isinstance(root, QDateTimeEdit) else []
    date_edits.extend(root.findChildren(QDateTimeEdit))
    for editor in date_edits:
        if editor.calendarPopup():
            calendar = editor.calendarWidget()
            calendar.setStyleSheet(build_oral_nutrition_dialog_style(tokens))
            retained_popups.append(calendar)

    root._oral_popup_widgets = retained_popups
