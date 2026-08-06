from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QAbstractItemView,
    QComboBox,
    QDateEdit,
    QDateTimeEdit,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QLineEdit,
    QListView,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableView,
    QTextEdit,
    QTimeEdit,
    QToolButton,
    QTreeView,
    QWidget,
)

from rem_card.ui.styles.theme_manager import get_theme_manager
from rem_card.ui.styles.theme_tokens import token
from rem_card.app.paths import get_icon_dir


_DANGER_WORDS = (
    "удал",
    "сброс",
    "очист",
    "ротац",
    "закрыть период",
    "сменить баз",
)
_PRIMARY_WORDS = (
    "сохран",
    "примен",
    "создать",
    "добав",
    "загруз",
    "выбрать",
    "готов",
    "продолж",
)
_SECONDARY_WORDS = (
    "отмена",
    "закрыть",
    "назад",
    "обнов",
    "измен",
    "обзор",
    "провер",
)
_BUTTON_STYLE_EXCLUSIONS = (
    "TitleControl",
    "TitleClose",
    "Settings",
    "AdminDictionary",
    "ThemeColor",
    "ThemeAccent",
    "ThemeDelete",
    "OperBlockFavoritePresetButton",
)


def _control_icon_url(icon_name: str) -> str:
    icon_path = os.path.join(get_icon_dir(), icon_name)
    if not os.path.exists(icon_path):
        return "none"
    return f"url({icon_path.replace(os.sep, '/')})"


def is_settings_context(widget: QWidget | None) -> bool:
    current = widget
    while current is not None:
        try:
            if bool(current.property("settingsContext")):
                return True
            current = current.parentWidget()
        except RuntimeError:
            return False
    return False


def apply_settings_surface(root: QWidget) -> None:
    """Apply one settings design language to a page or dialog tree."""

    tokens = get_theme_manager().current_tokens()
    root.setProperty("settingsContext", True)
    root.setProperty("settingsSurface", "dialog" if isinstance(root, QDialog) else "page")

    buttons = root.findChildren(QPushButton)
    for button in buttons:
        _polish_button(button)
    primary_buttons = [
        button
        for button in buttons
        if button.property("settingsSurfaceRole") == "primary"
    ]
    if len(primary_buttons) > 1:
        preferred_primary = max(primary_buttons, key=_primary_button_priority)
        for button in primary_buttons:
            if button is not preferred_primary:
                button.setProperty("settingsSurfaceRole", "secondary")
    for button in buttons:
        role = str(button.property("settingsSurfaceRole") or "")
        if role in {"primary", "secondary", "danger"}:
            button.setStyleSheet(_surface_button_style(tokens, role))

    for tool_button in root.findChildren(QToolButton):
        if tool_button.objectName().startswith(("Title", "qt_calendar")):
            continue
        tool_button.setProperty("settingsSurfaceToolButton", True)
        tool_button.setCursor(Qt.PointingHandCursor)
        tool_button.setMinimumSize(28, 28)

    for view in root.findChildren(QAbstractItemView):
        if view.objectName() == "AdminDictionaryTable":
            continue
        if isinstance(view, (QTableView, QListView, QTreeView)):
            view.setProperty("settingsSurfaceControl", True)
            if isinstance(view, QTableView):
                view.setAlternatingRowColors(True)
                view.setStyleSheet("")

    field_types = (
        QLineEdit,
        QTextEdit,
        QPlainTextEdit,
        QComboBox,
        QSpinBox,
        QDoubleSpinBox,
        QDateEdit,
        QDateTimeEdit,
        QTimeEdit,
        QAbstractSpinBox,
    )
    for field in root.findChildren(QWidget):
        if not isinstance(field, field_types):
            continue
        if field.objectName().startswith(("AdminDictionary", "Theme")):
            continue
        parent = field.parentWidget()
        if isinstance(field, QLineEdit) and isinstance(
            parent,
            (QComboBox, QAbstractSpinBox),
        ):
            continue
        field.setProperty("settingsSurfaceControl", True)
        if isinstance(
            field,
            (QLineEdit, QComboBox, QAbstractSpinBox, QDateEdit, QDateTimeEdit, QTimeEdit),
        ):
            field.setMinimumHeight(max(38, field.minimumHeight()))
            if field.maximumHeight() < 38:
                field.setMaximumHeight(38)
        direct_style = field.styleSheet().casefold()
        if any(
            marker in direct_style
            for marker in ("background-color: white", "#ffffff", "border: 1px solid gray")
        ):
            field.setStyleSheet("")

    original_style = root.property("settingsSurfaceOriginalStyle")
    if original_style is None:
        original_style = root.styleSheet()
        root.setProperty("settingsSurfaceOriginalStyle", original_style)
    root.setStyleSheet(
        f"{str(original_style or '')}\n"
        f"{build_settings_surface_style(tokens)}"
    )

    root.style().unpolish(root)
    root.style().polish(root)


def prepare_settings_file_dialog(dialog: QFileDialog) -> QFileDialog:
    """Use the shared settings surface for file and image selection."""

    dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
    apply_settings_surface(dialog)
    return dialog


def _polish_button(button: QPushButton) -> None:
    object_name = button.objectName()
    text = " ".join(button.text().casefold().split())
    if bool(button.property("settingsSurfaceSkip")):
        button.setProperty("settingsSurfaceRole", None)
        return
    if text in {"–", "-", "▢", "□", "✕", "×", "x"}:
        return
    if object_name.startswith(_BUTTON_STYLE_EXCLUSIONS):
        button.setProperty("settingsSurfaceRole", None)
        return

    if any(word in text for word in _DANGER_WORDS):
        role = "danger"
    elif any(word in text for word in _PRIMARY_WORDS) or text in {"ok", "ок", "да"}:
        role = "primary"
    elif any(word in text for word in _SECONDARY_WORDS) or text in {"нет", "no"}:
        role = "secondary"
    else:
        role = "secondary"

    button.setProperty("settingsSurfaceRole", role)
    if object_name in {"", "DialogOkBtn"}:
        button.setObjectName("SettingsSurfaceButton")
    button.setStyleSheet("")


def _primary_button_priority(button: QPushButton) -> int:
    text = " ".join(button.text().casefold().split())
    if text in {"ok", "ок", "сохранить", "применить", "готово"}:
        return 120
    if any(word in text for word in ("сохран", "примен", "готов")):
        return 100
    if any(word in text for word in ("выбрать", "продолж")):
        return 80
    return 50


def _surface_button_style(tokens: dict[str, str], role: str) -> str:
    def t(key: str, default: str = "") -> str:
        return token(tokens, key, default)

    if role == "primary":
        background = t("button.accent.bg")
        foreground = t("button.accent.text")
        border = t("button.neutral.border")
        hover = t("button.accent.hover")
    elif role == "danger":
        background = t("button.danger.bg")
        foreground = t("button.danger.text")
        border = t("border.error")
        hover = t("button.danger.hover")
    else:
        background = t("surface.card")
        foreground = t("text.primary")
        border = t("border.default")
        hover = t("surface.hover")

    return f"""
QPushButton {{
    background-color: {background};
    color: {foreground};
    border: 1px solid {border};
    border-radius: {t("radius.md")};
    padding: 7px 13px;
    min-height: 34px;
    font-size: 12px;
    font-weight: 650;
}}
QPushButton:hover {{
    background-color: {hover};
}}
QPushButton:disabled {{
    background-color: {t("surface.panel")};
    color: {t("text.disabled")};
    border-color: {t("border.subtle")};
}}
"""


def build_settings_surface_style(tokens: dict[str, str]) -> str:
    def t(key: str, default: str = "") -> str:
        return token(tokens, key, default)

    combo_arrow = _control_icon_url("combo_arrow_down.svg")
    spin_up_arrow = _control_icon_url("spin_arrow_up.svg")
    spin_down_arrow = _control_icon_url("spin_arrow_down.svg")

    return f"""
QWidget[settingsContext="true"] {{
    background-color: {t("surface.window")};
    color: {t("text.primary")};
}}

QWidget[settingsContext="true"] QFrame#DialogMainFrame {{
    background-color: {t("dialog.bg")};
    border: 1px solid {t("dialog.border")};
    border-radius: 12px;
}}

QWidget[settingsContext="true"][settingsEmbedded="true"] QFrame#DialogMainFrame {{
    background-color: {t("surface.window")};
    border: none;
    border-radius: 0px;
}}

QWidget[settingsContext="true"][settingsEmbedded="true"] QFrame#AdminSettingsEmbeddedContent {{
    background-color: {t("surface.window")};
    border: none;
}}

QWidget[settingsContext="true"] QFrame#DialogTitleBar {{
    background-color: {t("dialog.header_bg")};
    border: none;
    border-bottom: 1px solid {t("border.subtle")};
}}

QWidget[settingsContext="true"] QFrame[settingsSurfacePanel="true"] {{
    background-color: {t("surface.card")};
    border: 1px solid {t("border.subtle")};
    border-radius: 10px;
}}

QWidget[settingsContext="true"] QFrame#BackgroundSettingsPanel,
QWidget[settingsContext="true"] QFrame#RemCardIconDetail,
QWidget[settingsContext="true"] QFrame#OperBlockIconDetail,
QWidget[settingsContext="true"] QFrame#DisplaySettingsOptionCard {{
    background-color: {t("surface.card")};
    border: 1px solid {t("border.subtle")};
    border-radius: 10px;
}}

QWidget[settingsContext="true"] QLabel#BackgroundPreview,
QWidget[settingsContext="true"] QLabel#RemCardIconPreview,
QWidget[settingsContext="true"] QLabel#OperBlockIconPreview {{
    background-color: {t("surface.panel")};
    color: {t("text.secondary")};
    border: 1px solid {t("border.subtle")};
    border-radius: 9px;
}}

QWidget[settingsContext="true"] QLabel#BackgroundSettingsTitle,
QWidget[settingsContext="true"] QLabel#RemCardIconTitle,
QWidget[settingsContext="true"] QLabel#OperBlockIconTitle,
QWidget[settingsContext="true"] QLabel#DisplaySettingsSectionTitle,
QWidget[settingsContext="true"] QLabel#DisplaySettingsSideTitle {{
    color: {t("text.primary")};
    background-color: transparent;
    font-weight: 700;
}}

QWidget[settingsContext="true"] QFrame#SettingsSurfaceDivider {{
    background-color: {t("border.subtle")};
    border: none;
}}

QWidget[settingsContext="true"] QLabel[settingsSurfaceLabel="true"] {{
    color: {t("text.primary")};
    background-color: transparent;
    font-size: 13px;
    font-weight: 600;
}}

QWidget[settingsContext="true"] QLabel[settingsSurfaceDescription="true"] {{
    color: {t("text.secondary")};
    background-color: transparent;
    font-size: 11px;
}}

QWidget[settingsContext="true"] QLabel[settingsSurfaceDisabled="true"] {{
    color: {t("text.disabled")};
}}

QWidget[settingsContext="true"] QLabel[settingsSurfaceMuted="true"] {{
    color: {t("text.secondary")};
    background-color: transparent;
    font-style: italic;
}}

QWidget[settingsContext="true"] QPushButton[settingsSurfaceRole="primary"],
QWidget[settingsContext="true"] QPushButton[settingsSurfaceRole="secondary"],
QWidget[settingsContext="true"] QPushButton[settingsSurfaceRole="danger"] {{
    min-height: 34px;
    border-radius: {t("radius.md")};
    padding: 7px 13px;
    font-size: 12px;
    font-weight: 650;
}}

QWidget[settingsContext="true"] QPushButton[settingsSurfaceRole="primary"] {{
    background-color: {t("button.accent.bg")};
    color: {t("button.accent.text")};
    border: 1px solid {t("button.neutral.border")};
}}

QWidget[settingsContext="true"] QPushButton[settingsSurfaceRole="primary"]:hover {{
    background-color: {t("button.accent.hover")};
}}

QWidget[settingsContext="true"] QPushButton[settingsSurfaceRole="secondary"] {{
    background-color: {t("surface.card")};
    color: {t("text.primary")};
    border: 1px solid {t("border.default")};
}}

QWidget[settingsContext="true"] QPushButton[settingsSurfaceRole="secondary"]:hover {{
    background-color: {t("surface.hover")};
}}

QWidget[settingsContext="true"] QPushButton[settingsSurfaceRole="danger"] {{
    background-color: {t("button.danger.bg")};
    color: {t("button.danger.text")};
    border: 1px solid {t("border.error")};
}}

QWidget[settingsContext="true"] QPushButton[settingsSurfaceRole="danger"]:hover {{
    background-color: {t("button.danger.hover")};
}}

QWidget[settingsContext="true"] QPushButton:disabled {{
    background-color: {t("surface.panel")};
    color: {t("text.disabled")};
    border-color: {t("border.subtle")};
}}

QWidget[settingsContext="true"] QToolButton[settingsSurfaceToolButton="true"] {{
    background-color: {t("surface.card")};
    color: {t("text.primary")};
    border: 1px solid {t("border.default")};
    border-radius: 7px;
    padding: 4px;
}}

QWidget[settingsContext="true"] QToolButton[settingsSurfaceToolButton="true"]:hover {{
    background-color: {t("surface.hover")};
    border-color: {t("border.focus")};
}}

QWidget[settingsContext="true"] QLineEdit[settingsSurfaceControl="true"],
QWidget[settingsContext="true"] QTextEdit[settingsSurfaceControl="true"],
QWidget[settingsContext="true"] QPlainTextEdit[settingsSurfaceControl="true"],
QWidget[settingsContext="true"] QComboBox[settingsSurfaceControl="true"],
QWidget[settingsContext="true"] QSpinBox[settingsSurfaceControl="true"],
QWidget[settingsContext="true"] QDoubleSpinBox[settingsSurfaceControl="true"],
QWidget[settingsContext="true"] QDateEdit[settingsSurfaceControl="true"],
QWidget[settingsContext="true"] QDateTimeEdit[settingsSurfaceControl="true"],
QWidget[settingsContext="true"] QTimeEdit[settingsSurfaceControl="true"] {{
    background-color: {t("field.bg")};
    color: {t("field.text")};
    border: 1px solid {t("field.border")};
    border-radius: 9px;
    padding: 7px 10px;
    min-height: 20px;
    selection-background-color: {t("surface.selected")};
}}

QWidget[settingsContext="true"] QLineEdit[settingsSurfaceControl="true"]:focus,
QWidget[settingsContext="true"] QTextEdit[settingsSurfaceControl="true"]:focus,
QWidget[settingsContext="true"] QComboBox[settingsSurfaceControl="true"]:focus {{
    border: 2px solid {t("field.focus_border")};
}}

QWidget[settingsContext="true"] QComboBox[settingsSurfaceControl="true"] {{
    padding-right: 42px;
}}

QWidget[settingsContext="true"] QComboBox[settingsSurfaceControl="true"]::drop-down,
QWidget[settingsContext="true"] QDateEdit[settingsSurfaceControl="true"]::drop-down,
QWidget[settingsContext="true"] QDateTimeEdit[settingsSurfaceControl="true"]::drop-down {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 30px;
    margin: 3px 3px 3px 0px;
    background-color: {t("surface.panel")};
    border: 1px solid {t("border.subtle")};
    border-radius: 7px;
}}

QWidget[settingsContext="true"] QComboBox[settingsSurfaceControl="true"]::drop-down:hover,
QWidget[settingsContext="true"] QDateEdit[settingsSurfaceControl="true"]::drop-down:hover,
QWidget[settingsContext="true"] QDateTimeEdit[settingsSurfaceControl="true"]::drop-down:hover {{
    background-color: {t("surface.hover")};
    border-color: {t("border.focus")};
}}

QWidget[settingsContext="true"] QComboBox[settingsSurfaceControl="true"]::down-arrow,
QWidget[settingsContext="true"] QDateEdit[settingsSurfaceControl="true"]::down-arrow,
QWidget[settingsContext="true"] QDateTimeEdit[settingsSurfaceControl="true"]::down-arrow {{
    image: {combo_arrow};
    width: 12px;
    height: 12px;
}}

QWidget[settingsContext="true"] QComboBox[settingsSurfaceControl="true"] QAbstractItemView {{
    background-color: {t("surface.card")};
    color: {t("text.primary")};
    border: 1px solid {t("border.default")};
    border-radius: 8px;
    outline: 0px;
    padding: 4px;
    selection-background-color: {t("surface.selected")};
    selection-color: {t("text.primary")};
}}

QWidget[settingsContext="true"] QComboBox[settingsSurfaceControl="true"] QAbstractItemView::item {{
    min-height: 28px;
    padding: 4px 8px;
    border-radius: 5px;
}}

QWidget[settingsContext="true"] QSpinBox[settingsSurfaceControl="true"],
QWidget[settingsContext="true"] QDoubleSpinBox[settingsSurfaceControl="true"],
QWidget[settingsContext="true"] QTimeEdit[settingsSurfaceControl="true"] {{
    padding-right: 38px;
}}

QWidget[settingsContext="true"] QSpinBox[settingsSurfaceControl="true"]::up-button,
QWidget[settingsContext="true"] QDoubleSpinBox[settingsSurfaceControl="true"]::up-button,
QWidget[settingsContext="true"] QTimeEdit[settingsSurfaceControl="true"]::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 26px;
    margin: 3px 3px 0px 0px;
    background-color: {t("surface.panel")};
    border: 1px solid {t("border.subtle")};
    border-bottom: none;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
}}

QWidget[settingsContext="true"] QSpinBox[settingsSurfaceControl="true"]::down-button,
QWidget[settingsContext="true"] QDoubleSpinBox[settingsSurfaceControl="true"]::down-button,
QWidget[settingsContext="true"] QTimeEdit[settingsSurfaceControl="true"]::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 26px;
    margin: 0px 3px 3px 0px;
    background-color: {t("surface.panel")};
    border: 1px solid {t("border.subtle")};
    border-top: none;
    border-bottom-left-radius: 7px;
    border-bottom-right-radius: 7px;
}}

QWidget[settingsContext="true"] QSpinBox[settingsSurfaceControl="true"]::up-button:hover,
QWidget[settingsContext="true"] QSpinBox[settingsSurfaceControl="true"]::down-button:hover,
QWidget[settingsContext="true"] QDoubleSpinBox[settingsSurfaceControl="true"]::up-button:hover,
QWidget[settingsContext="true"] QDoubleSpinBox[settingsSurfaceControl="true"]::down-button:hover,
QWidget[settingsContext="true"] QTimeEdit[settingsSurfaceControl="true"]::up-button:hover,
QWidget[settingsContext="true"] QTimeEdit[settingsSurfaceControl="true"]::down-button:hover {{
    background-color: {t("surface.hover")};
    border-color: {t("border.focus")};
}}

QWidget[settingsContext="true"] QSpinBox[settingsSurfaceControl="true"]::up-arrow,
QWidget[settingsContext="true"] QDoubleSpinBox[settingsSurfaceControl="true"]::up-arrow,
QWidget[settingsContext="true"] QTimeEdit[settingsSurfaceControl="true"]::up-arrow {{
    image: {spin_up_arrow};
    width: 10px;
    height: 10px;
}}

QWidget[settingsContext="true"] QSpinBox[settingsSurfaceControl="true"]::down-arrow,
QWidget[settingsContext="true"] QDoubleSpinBox[settingsSurfaceControl="true"]::down-arrow,
QWidget[settingsContext="true"] QTimeEdit[settingsSurfaceControl="true"]::down-arrow {{
    image: {spin_down_arrow};
    width: 10px;
    height: 10px;
}}

QWidget[settingsContext="true"] QTableView[settingsSurfaceControl="true"],
QWidget[settingsContext="true"] QListView[settingsSurfaceControl="true"],
QWidget[settingsContext="true"] QTreeView[settingsSurfaceControl="true"] {{
    background-color: {t("table.bg")};
    alternate-background-color: {t("table.row_alt_bg")};
    color: {t("text.primary")};
    border: 1px solid {t("border.subtle")};
    border-radius: 10px;
    gridline-color: {t("table.grid")};
    selection-background-color: {t("table.row_selected_bg")};
    selection-color: {t("text.inverse")};
}}

QWidget[settingsContext="true"] QTableView[settingsSurfaceControl="true"]::item,
QWidget[settingsContext="true"] QListView[settingsSurfaceControl="true"]::item,
QWidget[settingsContext="true"] QTreeView[settingsSurfaceControl="true"]::item {{
    padding: 6px;
}}

QWidget[settingsContext="true"] QHeaderView::section {{
    background-color: {t("table.header_bg")};
    color: {t("table.header_text")};
    border: none;
    border-right: 1px solid {t("table.grid")};
    border-bottom: 1px solid {t("table.grid")};
    padding: 8px 7px;
    font-weight: 700;
}}

QWidget[settingsContext="true"] QGroupBox {{
    background-color: {t("surface.card")};
    border: 1px solid {t("border.subtle")};
    border-radius: 10px;
    margin-top: 12px;
    padding-top: 12px;
}}

QWidget[settingsContext="true"] QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
    color: {t("text.secondary")};
    font-weight: 650;
}}

QWidget[settingsContext="true"] QTabWidget::pane {{
    background-color: {t("surface.card")};
    border: 1px solid {t("border.subtle")};
    border-radius: 8px;
}}

QWidget[settingsContext="true"] QScrollBar:vertical {{
    background: transparent;
    border: none;
    width: 12px;
    margin: 2px;
}}

QWidget[settingsContext="true"] QScrollBar::handle:vertical {{
    background: {t("border.default")};
    border: 2px solid transparent;
    border-radius: 5px;
    min-height: 32px;
}}

QWidget[settingsContext="true"] QScrollBar::handle:vertical:hover {{
    background: {t("border.focus")};
}}

QWidget[settingsContext="true"] QScrollBar:horizontal {{
    background: transparent;
    border: none;
    height: 12px;
    margin: 2px;
}}

QWidget[settingsContext="true"] QScrollBar::handle:horizontal {{
    background: {t("border.default")};
    border: 2px solid transparent;
    border-radius: 5px;
    min-width: 32px;
}}

QWidget[settingsContext="true"] QScrollBar::handle:horizontal:hover {{
    background: {t("border.focus")};
}}

QWidget[settingsContext="true"] QScrollBar::add-line,
QWidget[settingsContext="true"] QScrollBar::sub-line {{
    background: transparent;
    border: none;
    width: 0px;
    height: 0px;
}}

QWidget[settingsContext="true"] QScrollBar::add-page,
QWidget[settingsContext="true"] QScrollBar::sub-page {{
    background: transparent;
    border: none;
}}
"""
