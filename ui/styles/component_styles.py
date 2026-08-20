from __future__ import annotations

from rem_card.ui.styles.theme_tokens import token


def build_button_style(tokens: dict[str, str], role: str = "neutral", *, small: bool = False) -> str:
    t = lambda key, default="": token(tokens, key, default)
    role = role if role in {"neutral", "accent", "success", "danger", "ghost"} else "neutral"
    pad = "4px 10px" if small else "6px 20px"
    font_size = "12px" if small else "13px"
    border = t(f"button.{role}.border", t("button.neutral.border"))
    if role == "danger":
        border = t("border.error")
    return f"""
        QPushButton {{
            background-color: {t(f"button.{role}.bg", t("button.neutral.bg"))};
            color: {t(f"button.{role}.text", t("button.neutral.text"))};
            font-size: {font_size};
            font-weight: bold;
            padding: {pad};
            border: 1px solid {border};
            border-radius: {t("radius.dialog")};
        }}
        QPushButton:hover {{
            background-color: {t(f"button.{role}.hover", t("button.neutral.hover"))};
        }}
        QPushButton:pressed {{
            background-color: {t("button.neutral.pressed")};
        }}
        QPushButton:disabled {{
            background-color: {t("surface.window")};
            color: {t("text.disabled")};
            border-color: {t("border.subtle")};
        }}
    """


def build_field_style(tokens: dict[str, str]) -> str:
    t = lambda key, default="": token(tokens, key, default)
    return f"""
        QLineEdit, QTextEdit, QPlainTextEdit, QDateEdit, QDateTimeEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
            background-color: {t("field.bg")};
            color: {t("field.text")};
            border: 1px solid {t("field.border")};
            border-radius: {t("radius.sm")};
            padding: 5px;
        }}
        QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QDateEdit:focus,
        QDateTimeEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
            border: 1px solid {t("field.focus_border")};
        }}
    """


def build_table_style(tokens: dict[str, str]) -> str:
    t = lambda key, default="": token(tokens, key, default)
    return f"""
        QTableView, QTableWidget {{
            background-color: {t("table.bg")};
            alternate-background-color: {t("table.row_alt_bg")};
            color: {t("text.primary")};
            border: 1px solid {t("border.default")};
            gridline-color: {t("table.grid")};
        }}
        QTableView::item:selected, QTableWidget::item:selected {{
            background-color: {t("table.row_selected_bg")};
            color: {t("text.inverse")};
        }}
    """
