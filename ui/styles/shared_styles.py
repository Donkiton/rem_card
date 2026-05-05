from __future__ import annotations

from rem_card.ui.styles.theme_manager import get_theme_manager
from rem_card.ui.styles.theme_tokens import token


def _tokens() -> dict[str, str]:
    return get_theme_manager().current_tokens()


def _t(key: str, default: str = "") -> str:
    return token(_tokens(), key, default)


def _set_style(widget, qss: str) -> None:
    if widget is not None:
        widget.setStyleSheet(qss)


def apply_custom_dialog_style(dialog) -> None:
    _set_style(
        dialog,
        f"""
        QDialog {{
            background-color: transparent;
        }}
        QFrame#DialogMainFrame {{
            background-color: {_t("dialog.bg")};
            border: 1px solid {_t("dialog.border")};
            border-radius: {_t("radius.dialog")};
        }}
        QFrame#DialogTitleBar {{
            background-color: {_t("dialog.header_bg")};
            border-top-left-radius: {_t("radius.dialog")};
            border-top-right-radius: {_t("radius.dialog")};
            border-bottom-left-radius: 0px;
            border-bottom-right-radius: 0px;
            border-bottom: 1px solid {_t("dialog.border")};
        }}
        QLabel#DialogTitleText {{
            color: {_t("dialog.header_text")};
            font-weight: bold;
            font-size: 14px;
            padding-left: 5px;
            background-color: transparent;
        }}
        QPushButton#DialogCloseBtn {{
            background-color: transparent;
            color: {_t("dialog.header_text")};
            font-weight: bold;
            font-size: 14px;
            border: none;
            padding: 2px 10px;
            border-top-right-radius: {_t("radius.dialog")};
        }}
        QPushButton#DialogCloseBtn:hover {{
            background-color: {_t("titlebar.close_hover")};
            color: {_t("text.inverse")};
        }}
        QLabel#DialogMessageText {{
            color: {_t("text.primary")};
            font-size: 13px;
            font-weight: bold;
            background-color: transparent;
        }}
        QPushButton#DialogOkBtn {{
            background-color: {_t("button.accent.bg")};
            color: {_t("button.accent.text")};
            font-size: 13px;
            font-weight: bold;
            padding: 6px 20px;
            border: 1px solid {_t("dialog.border")};
            border-radius: {_t("radius.dialog")};
        }}
        QPushButton#DialogOkBtn:hover {{
            background-color: {_t("button.accent.hover")};
        }}
        """,
    )


def apply_message_icon_style(label, msg_type: str) -> None:
    color_by_type = {
        "warning": _t("state.warning"),
        "custom": _t("state.warning"),
        "critical": _t("state.danger"),
        "information": _t("state.info"),
        "question": _t("state.info"),
        "balance_question": _t("state.info"),
    }
    color = color_by_type.get(str(msg_type or "").strip(), _t("state.warning"))
    _set_style(label, f"font-size: 36px; color: {color}; background-color: transparent;")


def apply_main_frame_window_style(frame, *, maximized: bool) -> None:
    if maximized:
        border = "none"
        radius = "0px"
    else:
        border = f"1px solid {_t('border.default')}"
        radius = _t("radius.dialog")
    _set_style(
        frame,
        f"""
        QFrame#MainFrame {{
            background-color: {_t("surface.window")} !important;
            border: {border};
            border-radius: {radius};
        }}
        """,
    )


def apply_base_sector_styles(label, container) -> None:
    _set_style(
        label,
        (
            "font-weight: bold; "
            f"color: {_t('sector.header_text')}; "
            f"background: {_t('sector.header_bg')}; "
            f"border: 1px solid {_t('border.subtle')};"
        ),
    )
    _set_style(container, "background: transparent; border: none;")


def apply_patient_archive_list_style(list_widget) -> None:
    _set_style(
        list_widget,
        f"""
        QListWidget {{
            font-size: 16px;
            border: 1px solid {_t("border.default")};
            border-radius: {_t("radius.dialog")};
            background-color: {_t("surface.card")};
            color: {_t("text.primary")};
        }}
        QListWidget::item {{
            padding: 12px;
            border-bottom: 1px solid {_t("surface.subtle")};
        }}
        QListWidget::item:selected {{
            background-color: {_t("surface.selected")};
            color: {_t("text.inverse")};
        }}
        QListWidget::item:hover:!selected {{
            background-color: {_t("surface.hover")};
        }}
        """,
    )


def apply_patient_archive_frame_style(frame) -> None:
    _set_style(
        frame,
        f"""
        #MainFrame {{
            background-color: {_t("surface.window")};
            border-radius: 10px;
            border: 1px solid {_t("border.default")};
        }}
        """,
    )


def apply_patient_archive_load_button_style(button) -> None:
    _set_style(
        button,
        f"""
        QPushButton {{
            font-size: 15px;
            font-weight: bold;
            background-color: {_t("state.success")};
            color: {_t("text.inverse")};
            border-radius: 10px;
            border: none;
        }}
        QPushButton:hover {{
            background-color: {_t("state.success.hover")};
        }}
        QPushButton:pressed {{
            background-color: {_t("button.success.hover")};
        }}
        QPushButton:disabled {{
            background-color: {_t("border.default")};
            color: {_t("text.disabled")};
        }}
        """,
    )


def apply_infusion_title_style(label) -> None:
    _set_style(label, "font-weight: bold; font-size: 14px;")


def apply_infusion_concentration_style(label) -> None:
    _set_style(label, f"font-weight: bold; color: {_t('text.secondary')}; font-size: 11px;")


def apply_infusion_result_style(label) -> None:
    _set_style(
        label,
        f"""
        QLabel {{
            font-weight: bold;
            font-size: 24px;
            color: {_t("text.primary")};
            background-color: {_t("surface.subtle")};
            border-radius: {_t("radius.md")};
            padding: 15px;
            border: 1px solid {_t("border.default")};
        }}
        """,
    )


def apply_infusion_spin_style(spin, state: str = "normal") -> None:
    state = str(state or "normal")
    bg = _t("field.bg")
    text = _t("field.text")
    border = _t("field.border")
    if state == "readonly":
        bg = _t("field.disabled_bg")
        text = _t("text.secondary")
    elif state == "error":
        bg = _t("sector.error_bg")
        border = _t("field.error_border")
    _set_style(
        spin,
        f"""
        QDoubleSpinBox {{
            padding: 4px;
            border: 1px solid {border};
            border-radius: {_t("radius.sm")};
            font-size: 14px;
            background-color: {bg};
            color: {text};
        }}
        QDoubleSpinBox:focus {{
            border: 1.5px solid {_t("field.focus_border")};
        }}
        """,
    )


def apply_minigame_label_style(label, variant: str = "primary") -> None:
    variant = str(variant or "primary")
    if variant == "title":
        qss = f"font-size: 18px; font-weight: bold; color: {_t('text.primary')};"
    elif variant == "accent":
        qss = f"font-weight: bold; color: {_t('sector.title_text')};"
    elif variant == "secondary":
        qss = f"color: {_t('text.secondary')};"
    else:
        qss = f"font-weight: bold; color: {_t('text.primary')};"
    _set_style(label, qss)
