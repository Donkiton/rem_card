from __future__ import annotations

from rem_card.ui.styles.component_styles import build_button_style
from rem_card.ui.styles.theme_tokens import token


def build_dialog_close_button_style(tokens: dict[str, str]) -> str:
    t = lambda key, default="": token(tokens, key, default)
    return f"""
        QPushButton {{
            background: transparent;
            color: {t("text.secondary")};
            font-size: 22px;
            border: none;
            border-radius: {t("radius.dialog")};
        }}
        QPushButton:hover {{
            background: {t("state.danger")};
            color: {t("text.inverse")};
        }}
    """


def build_custom_dialog_style(tokens: dict[str, str]) -> str:
    t = lambda key, default="": token(tokens, key, default)
    return f"""
        QDialog {{
            background-color: transparent;
        }}
        QFrame#DialogMainFrame {{
            background-color: {t("dialog.bg")};
            border: 1px solid {t("dialog.border")};
            border-radius: {t("radius.dialog")};
        }}
        QFrame#DialogTitleBar {{
            background-color: {t("dialog.header_bg")};
            border-top-left-radius: {t("radius.dialog")};
            border-top-right-radius: {t("radius.dialog")};
            border-bottom-left-radius: 0px;
            border-bottom-right-radius: 0px;
            border-bottom: 1px solid {t("dialog.border")};
        }}
        QLabel#DialogTitleText {{
            color: {t("dialog.header_text")};
            font-weight: bold;
            font-size: 14px;
            padding-left: 5px;
            background-color: transparent;
        }}
        QPushButton#DialogCloseBtn {{
            background-color: transparent;
            color: {t("dialog.header_text")};
            font-weight: bold;
            font-size: 14px;
            border: none;
            padding: 2px 10px;
            border-top-right-radius: {t("radius.dialog")};
        }}
        QPushButton#DialogCloseBtn:hover {{
            background-color: {t("state.danger")};
            color: {t("text.inverse")};
        }}
        QLabel#DialogMessageText {{
            color: {t("text.primary")};
            font-size: 13px;
            font-weight: bold;
            background-color: transparent;
        }}
        {build_button_style(tokens, "accent")}
    """
