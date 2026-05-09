# --- ЦЕНТРАЛЬНАЯ ТЕМА ПРИЛОЖЕНИЯ РЕМКАРТА ---
# Файл остается публичным входом для старых импортов. Значения ниже строятся из
# новой семантической темы, чтобы UI не зависел от случайных hex-цветов в виджетах.

from rem_card.ui.styles.chart_styles import analytics_chart_colors
from rem_card.ui.styles.theme_manager import get_theme_manager
from rem_card.ui.styles.tooltip_style import TOOLTIP_BG, TOOLTIP_BORDER, TOOLTIP_TEXT


def get_current_tokens(role: str | None = None) -> dict:
    return get_theme_manager().tokens_for_role(role)


_TOKENS = get_theme_manager().current_tokens()


def _t(key: str, default: str = "") -> str:
    value = _TOKENS.get(key, default)
    return str(value if value is not None else default)


# --- ОБЩИЕ ЦВЕТА ФОНА ---
BG_MAIN = _t("surface.window", "#f8f9fa")
BG_LIGHT = _t("surface.panel", "#e9ecef")
BG_CARD = _t("surface.card", "#ffffff")
BG_ALT_ROW = _t("table.row_alt_bg", "#fdfdfd")

# --- ЦВЕТА ТЕКСТА ---
TEXT_PRIMARY = _t("text.primary", "#2c3e50")
TEXT_SECONDARY = _t("text.secondary", "#495057")
TEXT_MUTED = _t("text.muted", "#adb5bd")
TEXT_ON_DARK = _t("text.inverse", "#ffffff")

# --- СОСТОЯНИЯ И АКЦЕНТЫ ---
COLOR_PRIMARY = _t("surface.selected", "#6c757d")
COLOR_PRIMARY_DARK = _t("sector.title_text", "#5c6770")
COLOR_SUCCESS = _t("state.success", "#28a745")
COLOR_SUCCESS_DARK = _t("state.success.hover", "#218838")
COLOR_DANGER = _t("state.danger", "#e74c3c")
COLOR_WARNING = _t("state.warning", "#f39c12")
COLOR_INFO = _t("state.info", "#7f8c8d")
COLOR_SECONDARY = _t("state.secondary", "#6c757d")

# --- ГРАНИЦЫ И РАМКИ ---
BORDER_COLOR = _t("border.default", "#bdc3c7")
BORDER_LIGHT = _t("border.subtle", "#dee2e6")
BORDER_WIDTH = _t("border.width", "1.5px")
BORDER_RADIUS_LG = _t("radius.lg", "15px")
BORDER_RADIUS_MD = _t("radius.md", "8px")
BORDER_RADIUS_SM = _t("radius.sm", "4px")

# --- СПЕЦИФИЧЕСКИЕ ЦВЕТА ИНТЕРФЕЙСА ---
COLOR_BED_FREE = _t("state.success", "#28a745")
COLOR_BED_BUSY = _t("state.danger", "#dc3545")
COLOR_CHART_GRID = _t("chart.grid", "#e0e0e0")

# --- ЦВЕТА ЛЕГЕНДЫ И ГРАФИКОВ (Витальные функции) ---
COLOR_VITAL_AD_LINE = _t("medical.vital.bp.line", "#e74c3c")
COLOR_VITAL_AD_BG = _t("medical.vital.bp.bg", "#ffdada")
COLOR_VITAL_PULSE = _t("medical.vital.pulse.line", "#0000ff")
COLOR_VITAL_PULSE_BG = _t("medical.vital.pulse.bg", "#dadaff")
COLOR_VITAL_RESP = _t("medical.vital.resp.line", "#e67e22")
COLOR_VITAL_RESP_BG = _t("medical.vital.resp.bg", "#fff0da")
COLOR_VITAL_SPO2 = _t("medical.vital.spo2.line", "#03a9f4")
COLOR_VITAL_SPO2_BG = _t("medical.vital.spo2.bg", "#e1f5fe")
COLOR_VITAL_TEMP = _t("medical.vital.temp.line", "#27ae60")
COLOR_VITAL_TEMP_BG = _t("medical.vital.temp.bg", "#dafada")
COLOR_VITAL_CVP = _t("medical.vital.cvp.line", "#ed5cf7")
COLOR_VITAL_CVP_BG = _t("medical.vital.cvp.bg", "#f8c0fc")

# --- ЦВЕТА ЯЧЕЕК НАЗНАЧЕНИЙ ---
COLOR_CELL_CANCELLED = "#f08989"
COLOR_CELL_DRAFT = "#7cf7ca"

# --- ЦВЕТА КНОПОК ---
COLOR_BTN_SAVE = _t("button.success.bg", BG_LIGHT)
COLOR_BTN_CLEAR = _t("button.danger.bg", "#fab1a0")
COLOR_BTN_BACK = _t("button.neutral.bg", "#6c757d")
COLOR_BTN_TPL_GEN = _t("button.neutral.bg", "#6c757d")
COLOR_BTN_TPL_APPLY = _t("button.success.bg", "#27ae60")
COLOR_BTN_DICT_ORANGE = _t("state.warning", "#e67e22")

# --- КНОПКИ ВЕРХНЕЙ ПАНЕЛИ И ПАНЕЛИ УПРАВЛЕНИЯ ---
COLOR_S8_BACK_BG = _t("button.neutral.bg", "#e9ecef")
COLOR_S8_BACK_TEXT = _t("button.neutral.text", "#000000")
COLOR_S8_PLACEHOLDER_BG = _t("surface.panel", "#e9ecef")
COLOR_S8_PLACEHOLDER_TEXT = _t("text.muted", "#95a5a6")
COLOR_S8_SETTINGS_BG = _t("button.neutral.bg", "#e9ecef")
COLOR_S8_SETTINGS_TEXT = _t("button.neutral.text", "#000000")
COLOR_S8_EXIT_BG = _t("button.neutral.bg", "#e9ecef")
COLOR_S8_EXIT_TEXT = _t("button.neutral.text", "#000000")
COLOR_S2_SAVE_BG = _t("button.neutral.bg", "#e9ecef")
COLOR_S2_SAVE_TEXT = _t("button.neutral.text", "#000000")
COLOR_S2_CLEAN_SHEET_BG = _t("button.neutral.bg", "#e9ecef")
COLOR_S2_CLEAN_SHEET_TEXT = _t("button.neutral.text", "#000000")
COLOR_S2_CLEAR_BG = _t("button.neutral.bg", "#e9ecef")
COLOR_S2_CLEAR_TEXT = _t("button.neutral.text", "#000000")
COLOR_S2_YESTERDAY_BG = _t("button.neutral.bg", "#e9ecef")
COLOR_S2_YESTERDAY_TEXT = _t("button.neutral.text", "#000000")
COLOR_S2_ROLLBACK_BG = _t("button.neutral.bg", "#e9ecef")
COLOR_S2_ROLLBACK_TEXT = _t("button.neutral.text", "#000000")

# --- КАСТОМНЫЕ ДИАЛОГОВЫЕ ОКНА ---
CUSTOM_DIALOG_RADIUS = _t("radius.dialog", "5px")
CUSTOM_DIALOG_BG = _t("dialog.bg", "#ffffff")
CUSTOM_DIALOG_TITLE_BG = _t("dialog.header_bg", "#e9ecef")
CUSTOM_DIALOG_TITLE_TEXT = _t("dialog.header_text", "#2c3e50")
CUSTOM_DIALOG_BORDER = _t("dialog.border", "#bdc3c7")

# --- ОБЩИЕ QSS-ФРАГМЕНТЫ И СТИЛИ ЭКРАНОВ ---
STYLE_TRANSPARENT_WIDGET = "background: transparent;"
STYLE_TRANSPARENT_LABEL = "border: none; background: transparent;"

STYLE_DIALOG_CLOSE_BUTTON = f"""
    QPushButton {{
        background: transparent;
        color: {TEXT_SECONDARY};
        font-size: 22px;
        border: none;
        border-radius: {CUSTOM_DIALOG_RADIUS};
    }}
    QPushButton:hover {{
        background: {COLOR_DANGER};
        color: {TEXT_ON_DARK};
    }}
"""

STYLE_SECTOR8_BUTTON = f"""
    QPushButton {{
        font-size: 13px;
        font-weight: bold;
        padding: 4px 12px;
        background-color: {BG_LIGHT};
        color: {TEXT_PRIMARY};
        border-radius: {CUSTOM_DIALOG_RADIUS};
        border: {BORDER_WIDTH} solid {BORDER_COLOR};
    }}
    QPushButton:hover {{
        background-color: #d8dde2;
    }}
    QPushButton:pressed {{
        background-color: {BORDER_COLOR};
    }}
    QPushButton:disabled {{
        background-color: {BG_MAIN};
        color: {TEXT_MUTED};
        border: 1px solid {BORDER_LIGHT};
    }}
"""

STYLE_NEUTRAL_BUTTON = f"""
    QPushButton {{
        background-color: {BG_LIGHT};
        color: {TEXT_PRIMARY};
        font-size: 13px;
        font-weight: bold;
        padding: 6px 20px;
        border: 1px solid {BORDER_COLOR};
        border-radius: {CUSTOM_DIALOG_RADIUS};
    }}
    QPushButton:hover {{
        background-color: #d8dde2;
    }}
    QPushButton:disabled {{
        background-color: {BG_MAIN};
        color: {TEXT_MUTED};
    }}
"""

STYLE_SMALL_NEUTRAL_BUTTON = f"""
    QPushButton {{
        background-color: {BG_LIGHT};
        color: {TEXT_PRIMARY};
        font-size: 12px;
        font-weight: bold;
        padding: 4px 10px;
        border: 1px solid {BORDER_LIGHT};
        border-radius: {BORDER_RADIUS_SM};
        min-width: 34px;
    }}
    QPushButton:hover {{
        background-color: #d8dde2;
    }}
    QPushButton:disabled {{
        background-color: {BG_MAIN};
        color: {TEXT_MUTED};
    }}
"""

# --- АРХИВ ---
STYLE_ARCHIVE_FRAME = f"""
    QFrame#archiveMainFrame {{
        border: {BORDER_WIDTH} solid {BORDER_COLOR};
        border-radius: {CUSTOM_DIALOG_RADIUS};
        background-color: transparent;
    }}
"""
STYLE_ARCHIVE_TITLE = (
    f"{STYLE_TRANSPARENT_LABEL} color: {TEXT_PRIMARY}; "
    "font-weight: bold; font-size: 16px;"
)
STYLE_ARCHIVE_TABLE = f"""
    QTableWidget {{
        background-color: {BG_CARD};
        alternate-background-color: {BG_ALT_ROW};
        color: {TEXT_PRIMARY};
        border: 1px solid {BORDER_COLOR};
        gridline-color: {BORDER_LIGHT};
    }}
    QTableWidget::item:selected {{
        background-color: {COLOR_PRIMARY};
        color: {TEXT_ON_DARK};
    }}
"""
STYLE_ARCHIVE_PAGE_INFO = f"border: none; color: {TEXT_SECONDARY}; font-weight: 600;"

# --- ОКНА АНАЛИТИКИ И ОТЧЕТОВ ---
STYLE_ANALYTICS_DIALOG_CONTAINER = f"""
    QWidget#bg_container, QWidget#container {{
        background-color: {BG_MAIN};
        border: {BORDER_WIDTH} solid {BORDER_COLOR};
        border-radius: {CUSTOM_DIALOG_RADIUS};
    }}
"""
STYLE_ANALYTICS_TITLE = (
    f"color: {TEXT_PRIMARY}; font-size: 14px; font-weight: 800; "
    "background: transparent;"
)
STYLE_ANALYTICS_PERIOD = f"color: {TEXT_SECONDARY}; font-size: 11px; font-weight: 600;"
STYLE_ANALYTICS_CONTROL_FRAME = f"""
    QFrame {{
        background: {BG_CARD};
        border: 1px solid {BORDER_LIGHT};
        border-radius: {BORDER_RADIUS_MD};
    }}
"""
STYLE_ANALYTICS_OPTION_BUTTON = f"""
    QPushButton {{
        color: {TEXT_PRIMARY};
        background: {BG_LIGHT};
        border: 1px solid {BORDER_COLOR};
        border-radius: {BORDER_RADIUS_SM};
        font-weight: 600;
        font-size: 12px;
        text-align: center;
        padding: 6px 10px;
    }}
    QPushButton:hover {{
        background: #d8dde2;
    }}
    QPushButton:disabled {{
        background: {BG_MAIN};
        color: {TEXT_MUTED};
        border-color: {BORDER_LIGHT};
    }}
"""
STYLE_ANALYTICS_SCROLL_AREA = f"""
    QScrollArea {{
        background: transparent;
        border: 1px solid {BORDER_LIGHT};
        border-radius: {BORDER_RADIUS_MD};
    }}
    QScrollBar:vertical {{
        border: none;
        background: {BG_CARD};
        width: 10px;
        margin: 0px;
    }}
    QScrollBar::handle:vertical {{
        background: {BORDER_COLOR};
        min-height: 20px;
        border-radius: 5px;
    }}
"""
STYLE_ANALYTICS_CHECKBOX_CONTAINER = f"background: {BG_CARD};"
STYLE_ANALYTICS_GROUP_LABEL = (
    f"color: {COLOR_PRIMARY_DARK}; font-weight: 800; font-size: 10px; "
    f"margin-top: 10px; border-bottom: 1px solid {BORDER_LIGHT};"
)
STYLE_ANALYTICS_CHECKBOX = (
    f"QCheckBox {{ color: {TEXT_PRIMARY}; font-weight: 500; "
    "font-size: 11px; background: transparent; }}"
)
STYLE_ANALYTICS_PREVIEW_BUTTON = f"""
    QPushButton {{
        background-color: {BG_CARD};
        color: {TEXT_PRIMARY};
        border: 1px solid {BORDER_COLOR};
        border-radius: {BORDER_RADIUS_MD};
        font-weight: 800;
        font-size: 11px;
    }}
    QPushButton:hover {{
        background-color: {BG_LIGHT};
    }}
    QPushButton:disabled {{
        background-color: {BG_MAIN};
        color: {TEXT_MUTED};
        border-color: {BORDER_LIGHT};
    }}
"""
STYLE_ANALYTICS_PRIMARY_BUTTON = f"""
    QPushButton {{
        background-color: {BG_LIGHT};
        color: {TEXT_PRIMARY};
        border: 1px solid {BORDER_COLOR};
        border-radius: {BORDER_RADIUS_MD};
        font-weight: 800;
        font-size: 12px;
    }}
    QPushButton:hover {{
        background-color: #d8dde2;
    }}
    QPushButton:disabled {{
        background-color: {BG_MAIN};
        color: {TEXT_MUTED};
        border-color: {BORDER_LIGHT};
    }}
"""
STYLE_ANALYTICS_TEXT_BROWSER = f"""
    QTextBrowser {{
        background-color: {BG_CARD};
        border: 1px solid {BORDER_LIGHT};
        border-radius: {BORDER_RADIUS_MD};
        padding: 10px;
        color: {TEXT_PRIMARY};
    }}
"""
STYLE_ANALYTICS_DATE_FRAME = f"""
    QFrame {{
        background: {BG_CARD};
        border: 1px solid {BORDER_LIGHT};
        border-radius: {BORDER_RADIUS_MD};
    }}
"""
STYLE_ANALYTICS_DATE_LABEL = f"border: none; font-weight: 600; color: {TEXT_SECONDARY};"
STYLE_ANALYTICS_DATE_EDIT = f"""
    QDateEdit {{
        padding: 5px;
        border: 1px solid {BORDER_LIGHT};
        border-radius: {BORDER_RADIUS_SM};
        background-color: {BG_CARD};
        color: {TEXT_PRIMARY};
    }}
    QDateEdit:focus {{
        border: 1px solid {BORDER_COLOR};
    }}
"""
ANALYTICS_CHART_COLORS = analytics_chart_colors(_TOKENS)

# --- УПРАВЛЕНИЕ ПАЦИЕНТАМИ ---
STYLE_PATIENT_BED_ROOT = f"""
    QWidget#patient_bed_root {{
        background-color: {BG_MAIN};
        border: {BORDER_WIDTH} solid {BORDER_COLOR};
        border-radius: {CUSTOM_DIALOG_RADIUS};
    }}
"""
STYLE_PATIENT_BED_HEADER = f"""
    QFrame#patient_bed_header {{
        background: {BG_CARD};
        border: {BORDER_WIDTH} solid {BORDER_COLOR};
        border-radius: {BORDER_RADIUS_MD};
    }}
"""
STYLE_PATIENT_BED_TITLE = (
    f"color: {TEXT_PRIMARY}; font-size: 24px; font-weight: 800; "
    "background: transparent; border: none;"
)
STYLE_PATIENT_BED_SUBTITLE = (
    f"color: {COLOR_PRIMARY_DARK}; font-size: 12px; font-weight: 600; "
    "background: transparent; border: none;"
)
STYLE_PATIENT_BED_LABEL = (
    f"color: {COLOR_PRIMARY_DARK}; font-size: 12px; font-weight: 800; "
    "background: transparent;"
)
STYLE_PATIENT_BED_HISTORY = (
    f"color: {TEXT_SECONDARY}; font-size: 13px; font-weight: 600; "
    "background: transparent;"
)
STYLE_PATIENT_BED_PATIENT = (
    f"color: {TEXT_PRIMARY}; font-size: 16px; font-weight: 700; "
    "background: transparent;"
)
STYLE_PATIENT_BED_STATUS_FREE = (
    f"color: {COLOR_SUCCESS}; font-size: 11px; font-weight: 700; "
    "background: transparent;"
)
STYLE_PATIENT_BED_STATUS_BUSY = (
    f"color: {COLOR_DANGER}; font-size: 11px; font-weight: 700; "
    "background: transparent;"
)
STYLE_PATIENT_BED_DROP_BG = "#f1f3f5"

STYLE_SIDE_PATIENT_CARD = f"""
    SidePatientCard {{
        background-color: {BG_CARD};
        border: {BORDER_WIDTH} solid {BORDER_COLOR};
        border-radius: {BORDER_RADIUS_MD};
    }}
    QLabel {{
        border: none;
        background: transparent;
    }}
"""
STYLE_SIDE_PATIENT_PHOTO = f"""
    QLabel {{
        border: 3px solid {BORDER_COLOR};
        background-color: transparent;
    }}
"""
STYLE_SIDE_PATIENT_NAME = f"color: {TEXT_PRIMARY}; font-weight: 800; font-size: 27px;"
STYLE_SIDE_PATIENT_DIAGNOSIS = f"color: {TEXT_PRIMARY}; font-weight: 500; font-size: 18px;"
STYLE_SIDE_PATIENT_STATUS_FREE = f"color: {COLOR_SUCCESS}; font-weight: 800; font-size: 24px;"
STYLE_SIDE_PATIENT_ACTION_BUTTON = f"""
    QPushButton {{
        background-color: {BG_LIGHT};
        color: {TEXT_PRIMARY};
        border: 1px solid {BORDER_COLOR};
        border-radius: {BORDER_RADIUS_MD};
        font-weight: 800;
        font-size: 18px;
    }}
    QPushButton:hover {{
        background-color: #d8dde2;
    }}
    QPushButton:disabled {{
        background-color: {BG_MAIN};
        color: {TEXT_MUTED};
        border-color: {BORDER_LIGHT};
    }}
"""

STYLE_PATIENT_FORM_CONTAINER = STYLE_ANALYTICS_DIALOG_CONTAINER
STYLE_PATIENT_FORM_TITLE = STYLE_ANALYTICS_TITLE
STYLE_PATIENT_FORM_TAB = f"""
    QWidget {{ background-color: {BG_MAIN}; }}
    QLabel {{ color: {TEXT_SECONDARY}; font-size: 13px; font-weight: 600; background: transparent; }}
    QLineEdit, QComboBox, QSpinBox, QDateTimeEdit {{
        padding: 8px;
        border: 1px solid {BORDER_LIGHT};
        border-radius: {CUSTOM_DIALOG_RADIUS};
        background: {BG_CARD};
        color: {TEXT_PRIMARY};
    }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDateTimeEdit:focus {{
        border: 1px solid {BORDER_COLOR};
        background: {BG_CARD};
    }}
"""
STYLE_PATIENT_FORM_SCROLL = f"""
    QScrollArea {{
        border: 1px solid {BORDER_LIGHT};
        background: {BG_MAIN};
        border-radius: {CUSTOM_DIALOG_RADIUS};
    }}
"""
STYLE_PATIENT_FORM_PAGE = f"background-color: {BG_MAIN};"
STYLE_PATIENT_FORM_SECTION_TITLE = (
    f"color: {TEXT_PRIMARY}; font-size: 12px; font-weight: 800; "
    "margin: 10px 24px 0 24px; background: transparent;"
)
STYLE_PATIENT_FORM_CANCEL_BUTTON = f"""
    QPushButton {{
        background: {BG_LIGHT};
        border: 1px solid {BORDER_COLOR};
        border-radius: {CUSTOM_DIALOG_RADIUS};
        color: {TEXT_SECONDARY};
        font-weight: 700;
        font-size: 12px;
    }}
    QPushButton:hover {{
        background: #d8dde2;
        color: {TEXT_PRIMARY};
    }}
    QPushButton:disabled {{
        background: {BG_MAIN};
        color: {TEXT_MUTED};
    }}
"""
STYLE_PATIENT_FORM_SAVE_BUTTON = f"""
    QPushButton {{
        background: {BG_LIGHT};
        border: 1px solid {BORDER_COLOR};
        border-radius: {CUSTOM_DIALOG_RADIUS};
        color: {TEXT_PRIMARY};
        font-weight: 800;
        font-size: 12px;
    }}
    QPushButton:hover {{
        background: #d8dde2;
    }}
    QPushButton:disabled {{
        background: {BG_MAIN};
        color: {TEXT_MUTED};
        border-color: {BORDER_LIGHT};
    }}
"""
STYLE_FORM_DATETIME_EDIT = f"""
    QDateTimeEdit {{
        background-color: {BG_CARD};
        color: {TEXT_PRIMARY};
        border: 1px solid {BORDER_LIGHT};
    }}
    QDateTimeEdit::up-button {{ width: 0px; border: none; }}
    QDateTimeEdit::down-button {{ width: 0px; border: none; }}
    QDateTimeEdit::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: top right;
        width: 20px;
        border-left-width: 1px;
        border-left-color: {BORDER_LIGHT};
        border-left-style: solid;
        border-top-right-radius: 3px;
        border-bottom-right-radius: 3px;
    }}
    QDateTimeEdit::down-arrow {{
        image: none;
    }}
    QCalendarWidget QWidget {{
        background-color: {BG_CARD};
        color: {TEXT_PRIMARY};
        border-radius: 0px;
    }}
    QCalendarWidget QAbstractItemView:enabled {{
        background-color: {BG_CARD};
        color: {TEXT_PRIMARY};
        selection-background-color: {COLOR_PRIMARY};
        selection-color: {TEXT_ON_DARK};
        border-radius: 0px;
    }}
    QCalendarWidget QToolButton {{
        color: {TEXT_PRIMARY};
        background-color: transparent;
        border: none;
        border-radius: 0px;
    }}
    QCalendarWidget QToolButton:hover {{
        color: {COLOR_PRIMARY_DARK};
    }}
    QCalendarWidget QToolButton#qt_calendar_monthbutton {{ margin-left: -6px; }}
    QCalendarWidget QWidget#qt_calendar_navigationbar {{
        background-color: {BG_LIGHT};
        border-bottom: 1px solid {BORDER_LIGHT};
        border-radius: 0px;
    }}
"""
STYLE_PATIENT_DIAGNOSIS_TEXT_LABEL = (
    f"color: {TEXT_SECONDARY}; font-weight: 500; "
    "background: transparent; font-family: 'IBM Plex Sans';"
)
STYLE_PATIENT_FORM_ROW_LABEL = (
    f"background: transparent; color: {TEXT_SECONDARY}; "
    "font-size: 13px; font-weight: 600;"
)
STYLE_PATIENT_FORM_READONLY_FIELD = f"background-color: {BG_LIGHT}; color: {TEXT_MUTED};"
STYLE_PATIENT_FORM_VALID_FIELD = f"background-color: #e1ffdc; border: 1px solid {COLOR_SUCCESS};"
STYLE_PATIENT_FORM_INVALID_FIELD = f"background-color: #ffdcde; border: 1px solid {COLOR_DANGER};"
STYLE_PATIENT_FORM_MANUAL_FIELD = (
    f"background-color: {BG_CARD}; color: {TEXT_PRIMARY}; "
    f"border: 1px solid {BORDER_LIGHT};"
)
STYLE_PATIENT_OPERATIONS_GROUP = f"""
    QGroupBox {{
        border: 1px solid {BORDER_LIGHT};
        border-radius: {BORDER_RADIUS_MD};
        margin-top: 18px;
        padding-top: 15px;
        font-weight: 800;
        color: {TEXT_PRIMARY};
        background-color: {BG_LIGHT};
        border-top-left-radius: 0px;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        padding: 8px 20px;
        background: {BG_LIGHT};
        border: 1px solid {BORDER_LIGHT};
        border-bottom: none;
        border-top-left-radius: {BORDER_RADIUS_MD};
        border-top-right-radius: {BORDER_RADIUS_MD};
        left: 0px;
        top: 0px;
        font-size: 12px;
        color: {TEXT_PRIMARY};
    }}
"""
STYLE_PATIENT_OPERATION_LABEL = f"background: transparent; font-weight: 600; color: {TEXT_SECONDARY};"
STYLE_PATIENT_OPERATION_FIELD = (
    f"background-color: {BG_CARD}; color: {TEXT_PRIMARY}; "
    f"border: 1px solid {BORDER_LIGHT};"
)

def get_patient_bed_card_style(status: str, *, hovered: bool = False, drop_target: bool = False) -> str:
    border_color = BORDER_COLOR
    border_width = "2px" if hovered or drop_target else BORDER_WIDTH
    border_style = "dashed" if drop_target else "solid"
    background = STYLE_PATIENT_BED_DROP_BG if drop_target else BG_CARD
    return f"""
        BedWidget {{
            background-color: {background};
            border: {border_width} {border_style} {border_color};
            border-radius: {BORDER_RADIUS_MD};
        }}
    """

def get_side_patient_label_style(font_size: int, *, is_title: bool = False, is_bold: bool = False) -> str:
    color = COLOR_PRIMARY_DARK if is_title else TEXT_PRIMARY
    weight = "800" if is_bold or is_title else "500"
    return f"color: {color}; font-weight: {weight}; font-size: {int(font_size)}px;"

# --- ГЛОБАЛЬНЫЙ QSS СТИЛЬ ---
GLOBAL_STYLE = f"""
QWidget {{
    background-color: transparent;
    color: {TEXT_PRIMARY};
    font-family: "Segoe UI", Roboto, Arial, sans-serif;
    font-size: 14px;
}}

QMainWindow, QDialog {{
    background-color: {BG_MAIN};
}}

/* Подсказки - форсированный стиль для читаемости */
QToolTip {{
    background-color: {TOOLTIP_BG} !important;
    color: {TOOLTIP_TEXT} !important;
    border: 1px solid {TOOLTIP_BORDER} !important;
    padding: 5px !important;
    font-size: 13px !important;
}}

/* Поля ввода */
QLineEdit, QDateEdit, QDateTimeEdit, QComboBox {{
    background-color: {BG_CARD};
    border: 1px solid {BORDER_LIGHT};
    border-radius: {BORDER_RADIUS_SM};
    padding: 5px;
    color: {TEXT_PRIMARY};
}}

QLineEdit:focus, QDateEdit:focus, QDateTimeEdit:focus, QComboBox:focus {{
    border: 2px solid {BORDER_COLOR};
}}

/* Кнопки */
QPushButton {{
    background-color: {BG_LIGHT};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_COLOR};
    border-radius: {BORDER_RADIUS_MD};
    padding: 8px 16px;
    font-weight: 600;
}}

QPushButton:hover {{
    background-color: #d8dde2;
}}

QPushButton:pressed {{
    background-color: {BORDER_COLOR};
}}

QPushButton:disabled {{
    background-color: {BG_MAIN};
    color: {TEXT_MUTED};
    border-color: {BORDER_LIGHT};
}}

QDialog QPushButton, QMessageBox QPushButton {{
    background-color: {BG_LIGHT};
    color: {TEXT_PRIMARY};
    padding: 6px 12px;
    min-width: 90px;
    border: 1px solid {BORDER_COLOR};
    border-radius: {BORDER_RADIUS_SM};
    font-weight: bold;
}}

/* Фикс для QMessageBox и стандартных диалогов, чтобы кнопки были в едином стиле */
QMessageBox QPushButton, QDialogButtonBox QPushButton {{
    color: {TEXT_PRIMARY} !important;
    background-color: {BG_LIGHT} !important;
    border: 1px solid {BORDER_COLOR} !important;
    border-radius: {BORDER_RADIUS_MD} !important;
    padding: 8px 16px !important;
    font-weight: 600 !important;
    min-width: 90px !important;
}}

QDialog QPushButton:hover, QMessageBox QPushButton:hover {{
    background-color: #d8dde2 !important;
}}

/* Стиль самого бокса сообщения */
QMessageBox {{
    background-color: white !important;
}}

QMessageBox QLabel {{
    color: #2c3e50 !important;
    background-color: transparent !important;
    font-size: 15px !important;
    font-weight: bold !important;
}}

/* Таблицы */
QTableView, QTableWidget {{
    background-color: {BG_CARD};
    alternate-background-color: {BG_ALT_ROW};
    border: 1px solid {BORDER_COLOR};
    gridline-color: {BORDER_LIGHT};
}}

QHeaderView::section {{
    background-color: {BG_LIGHT};
    color: {TEXT_PRIMARY};
    padding: 6px;
    border: none;
    border-right: 1px solid {BORDER_COLOR};
    border-bottom: 1px solid {BORDER_COLOR};
    font-weight: bold;
}}

/* Кастомный стиль кнопок предупреждений (бежевый, рамка, темный текст) */
QPushButton#DialogOkBtn {{
    background-color: {BG_LIGHT} !important;
    color: {TEXT_PRIMARY} !important;
    font-size: 13px !important;
    font-weight: bold !important;
    padding: 6px 20px !important;
    border: 1px solid {CUSTOM_DIALOG_BORDER} !important;
    border-radius: {CUSTOM_DIALOG_RADIUS} !important;
}}
QPushButton#DialogOkBtn:hover {{
    background-color: #d8dde2 !important;
}}
QPushButton#DialogOkBtn:disabled {{
    background-color: #f8f9fa !important;
    color: #adb5bd !important;
}}

/* СТИЛИ ГЛАВНОГО ЗАГОЛОВКА ПРИЛОЖЕНИЯ */
QFrame#MainTitleBar {{
    background-color: {CUSTOM_DIALOG_TITLE_BG} !important;
    border-bottom: 1px solid {CUSTOM_DIALOG_BORDER} !important;
    border-top-left-radius: {CUSTOM_DIALOG_RADIUS} !important;
    border-top-right-radius: {CUSTOM_DIALOG_RADIUS} !important;
}}
QLabel#MainTitleText {{
    color: {CUSTOM_DIALOG_TITLE_TEXT} !important;
    font-weight: bold !important;
    font-size: 14px !important;
    background-color: transparent !important;
}}
QPushButton#TitleControlBtn {{
    background-color: transparent !important;
    color: {CUSTOM_DIALOG_TITLE_TEXT} !important;
    font-weight: bold !important;
    font-size: 16px !important;
    border: none !important;
    border-radius: 5px !important;
    padding: 0px !important;
    min-width: 40px !important;
    margin: 2px 1px !important;
}}
QPushButton#TitleControlBtn:hover {{
    background-color: rgba(0, 0, 0, 0.1) !important;
}}
QPushButton#TitleCloseBtn {{
    background-color: transparent !important;
    color: {CUSTOM_DIALOG_TITLE_TEXT} !important;
    font-weight: bold !important;
    font-size: 16px !important;
    border: none !important;
    border-radius: 5px !important;
    padding: 0px !important;
    min-width: 40px !important;
    margin: 2px 1px !important;
}}
QPushButton#TitleCloseBtn:hover {{
    background-color: #e74c3c !important;
    color: white !important;
}}
"""

STYLE_CUSTOM_DIALOG = f"""
    QDialog {{
        background-color: transparent;
    }}
    QFrame#DialogMainFrame {{
        background-color: {CUSTOM_DIALOG_BG};
        border: 1px solid {CUSTOM_DIALOG_BORDER};
        border-radius: {CUSTOM_DIALOG_RADIUS};
    }}
    QFrame#DialogTitleBar {{
        background-color: {CUSTOM_DIALOG_TITLE_BG};
        border-top-left-radius: {CUSTOM_DIALOG_RADIUS};
        border-top-right-radius: {CUSTOM_DIALOG_RADIUS};
        border-bottom-left-radius: 0px;
        border-bottom-right-radius: 0px;
        border-bottom: 1px solid {CUSTOM_DIALOG_BORDER};
    }}
    QLabel#DialogTitleText {{
        color: {CUSTOM_DIALOG_TITLE_TEXT};
        font-weight: bold;
        font-size: 14px;
        padding-left: 5px;
        background-color: transparent;
    }}
    QPushButton#DialogCloseBtn {{
        background-color: transparent;
        color: {CUSTOM_DIALOG_TITLE_TEXT};
        font-weight: bold;
        font-size: 14px;
        border: none;
        padding: 2px 10px;
        border-top-right-radius: {CUSTOM_DIALOG_RADIUS};
    }}
    QPushButton#DialogCloseBtn:hover {{
        background-color: #e74c3c;
        color: white;
    }}
    QLabel#DialogMessageText {{
        color: {TEXT_PRIMARY};
        font-size: 13px;
        font-weight: bold;
        background-color: transparent;
    }}
    QPushButton#DialogOkBtn {{
        background-color: {BG_LIGHT};
        color: {TEXT_PRIMARY};
        font-size: 13px;
        font-weight: bold;
        padding: 6px 20px;
        border: 1px solid {CUSTOM_DIALOG_BORDER};
        border-radius: {CUSTOM_DIALOG_RADIUS};
    }}
    QPushButton#DialogOkBtn:hover {{
        background-color: #d8dde2;
    }}
"""

def get_bed_style(is_occupied: bool) -> str:
    color = COLOR_BED_BUSY if is_occupied else COLOR_BED_FREE
    return f"""
        QFrame {{
            background-color: {BG_CARD};
            border-radius: 12px;
            border: 2px solid {color};
        }}
        QLabel {{
            color: {TEXT_PRIMARY};
            background: transparent;
        }}
    """
