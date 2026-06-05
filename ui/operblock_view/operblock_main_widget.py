from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import html
import json
import os
import re
import time
from typing import TYPE_CHECKING
import weakref

from PySide6.QtCore import QEvent, QMimeData, QRectF, QSize, Qt, QTime, QTimer
from PySide6.QtGui import QColor, QDrag, QFont, QFontMetrics, QIcon, QImage, QImageReader, QPainterPath, QPixmap, QTransform
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QGraphicsPathItem,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QScrollBar,
    QSizePolicy,
    QStackedWidget,
    QHeaderView,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from rem_card.app import operblock_startup_metrics
from rem_card.app.logger import logger
from rem_card.app.patient_age import parse_date_value
from rem_card.app.paths import get_icon_dir, get_patient_assets_dir
from rem_card.services.mkb import MKBService
from rem_card.services.operblock_service import (
    OPERBLOCK_TABLES,
    OperBlockConflictError,
    OperBlockService,
    is_complete_operblock_mkb_code,
    normalize_operblock_history_number,
    normalize_operblock_mkb_code,
)
from rem_card.services.concurrency import DataConflictError
from rem_card.services.operblock_timeline import format_operblock_medication_display_label
from rem_card.services.operblock_quick_orders import (
    build_operblock_quick_order_text,
    load_operblock_quick_orders,
    normalize_operblock_quick_order_kind,
    normalize_operblock_quick_order_group,
    save_operblock_quick_orders,
)
from rem_card.services.operblock_medication_presets import (
    OPERBLOCK_MEDICATION_PRESET_KINDS,
    build_operblock_preset_bolus_text,
    build_operblock_preset_payload,
    load_operblock_diluent_options,
    load_operblock_medication_presets,
    normalize_operblock_medication_preset_kind,
    operblock_medication_preset_display_name,
    save_operblock_medication_presets,
)
from rem_card.services.operblock_icon_defaults import (
    default_drug_icon_file,
    drug_icon_candidate_keys_from_payload,
    edit_icon_key,
    type_icon_key,
)
from rem_card.services.operblock_anesthesia_types import (
    load_operblock_anesthesia_types,
    normalize_operblock_anesthesia_type_label,
    save_operblock_anesthesia_types,
)
from rem_card.services.operblock_team import (
    OPERBLOCK_TEAM_DEFAULT_POSITIONS,
    load_operblock_anesthesiologists,
    load_operblock_anesthetists,
    load_operblock_operating_nurses,
    load_operblock_surgeons,
    load_operblock_team,
    normalize_operblock_team_text,
    save_operblock_team,
)
from rem_card.ui.nurse_view.sectors.nurse_sector_4v import VitalBadge
from rem_card.ui.rem_card_sectors.sector_1a import Sector1a
from rem_card.ui.rem_card_sectors.sector_1b import Sector1b
from rem_card.ui.rem_card_sectors.sector_2g import Sector2g
from rem_card.ui.rem_card_sectors.sector_2v import Sector2v
from rem_card.ui.rem_card_sectors.sector_8 import Sector8
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.shared.display_settings_storage import DisplaySettingsStorage, role_display_settings_from_payload
from rem_card.ui.shared.operblock_icon_settings import load_operblock_icon_pixmap
from rem_card.ui.shared.vitals_widget import VitalsWidget
from rem_card.ui.shared.window_state import SavedFramelessDialogMixin
from rem_card.ui.styles.shared_styles import apply_custom_dialog_style
from rem_card.ui.styles.sector_styles import build_remcard_tab_button_style
from rem_card.ui.styles.sector_styles import (
    build_remcard_current_time_label_style,
    build_remcard_period_label_style,
)
from rem_card.ui.styles.theme import (
    BG_CARD,
    BG_LIGHT,
    BG_MAIN,
    BORDER_COLOR,
    BORDER_LIGHT,
    COLOR_DANGER,
    COLOR_PRIMARY_DARK,
    COLOR_SUCCESS,
    COLOR_VITAL_AD_LINE,
    COLOR_VITAL_PULSE,
    COLOR_VITAL_SPO2,
    CUSTOM_DIALOG_RADIUS,
    STYLE_PATIENT_FORM_CANCEL_BUTTON,
    STYLE_PATIENT_FORM_INVALID_FIELD,
    STYLE_PATIENT_FORM_MANUAL_FIELD,
    STYLE_PATIENT_FORM_PAGE,
    STYLE_PATIENT_FORM_READONLY_FIELD,
    STYLE_PATIENT_FORM_SAVE_BUTTON,
    STYLE_PATIENT_FORM_SCROLL,
    STYLE_PATIENT_FORM_SECTION_TITLE,
    STYLE_PATIENT_FORM_TAB,
    STYLE_PATIENT_FORM_VALID_FIELD,
    STYLE_SECTOR8_BUTTON,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)
from rem_card.ui.styles.theme_manager import get_theme_manager


if TYPE_CHECKING:
    from rem_card.ui.operblock_view.operblock_chart_widget import OperBlockChartWidget


OPERBLOCK_VITAL_SETTINGS = {"ad": 1, "pulse": 1, "temp": 0, "spo2": 1, "rr": 0, "cvp": 0}
OPERBLOCK_INITIAL_CHART_HOURS = 3
OPERBLOCK_CHART_EXPAND_THRESHOLD_MINUTES = 20
OPERBLOCK_MAX_CHART_HOURS = 72
OPERBLOCK_VITAL_TIME_STEP_MINUTES = 5
OPERBLOCK_CHART_GRID_STEP_MINUTES = 15
OPERBLOCK_VITAL_TIME_QUICK_ACTIONS = (
    ("Сейчас", None),
    ("-5 минут", -5),
    ("-10 минут", -10),
    ("+5 минут", 5),
)
OPERBLOCK_QUICK_ORDERS_WIDTH = 230
OPERBLOCK_QUICK_ORDER_DRAG_MIME = "application/x-operblock-medication-preset"
OPERBLOCK_ADD_ORDER_BUTTON_TEXT = "Добавить"
OPERBLOCK_INFUSION_CHANGE_BUTTON_TEXT = "Изменить"
OPERBLOCK_INFUSION_STOP_BUTTON_TEXT = "Стоп"
OPERBLOCK_INFUSION_RATE_UNIT = "мл/час"
OPERBLOCK_DEFAULT_INFUSION_RATES = ("1 мл/час", "2 мл/час", "5 мл/час", "10 мл/час")
OPERBLOCK_INFUSION_HISTORY_COLUMN = 4
OPERBLOCK_PRESET_KIND_TITLES = {
    "bolus": "Болюс",
    "gas": "Газ",
    "continuous_infusion": "Дозатор",
    "timed_infusion": "Капельница",
}
OPERBLOCK_PRESET_KIND_GROUP_TITLES = {
    "bolus": "Болюсы",
    "gas": "Газы",
    "continuous_infusion": "Дозатор",
    "timed_infusion": "Капельницы",
}
OPERBLOCK_PRESET_GROUP_OPTIONS = (
    "Болюсы",
    "Газы",
    "Дозатор",
    "Капельницы",
    "Вазопрессоры",
    "Седация",
    "Миорелаксанты",
    "Анальгезия",
    "Антибиотики / капельницы",
    "Растворы / прочее",
)
OPERBLOCK_PRESET_KIND_BADGES = {
    "bolus": "БОЛ",
    "gas": "ГАЗ",
    "continuous_infusion": "ДОЗ",
    "timed_infusion": "КАП",
}
OPERBLOCK_ORDERS_BG = "#F6F8FA"
OPERBLOCK_ORDERS_CARD_BG = "#FFFFFF"
OPERBLOCK_ORDERS_BORDER = BORDER_COLOR
OPERBLOCK_ORDERS_TEXT = "#0F172A"
OPERBLOCK_ORDERS_MUTED = "#64748B"
OPERBLOCK_ORDERS_ACCENT = "#2563EB"
OPERBLOCK_EVENT_COLORS = {
    "Болюс": ("#EEF3FF", "#2F6FAE"),
    "Газ": ("#E0F2FE", "#0369A1"),
    "Дозатор": ("#ECF7F0", "#2F8A57"),
    "Капельница": ("#F2F6F8", "#506070"),
    "Изм. скорость": ("#FFF3E0", "#B26A00"),
    "Изм. доза": ("#E0F2FE", "#0369A1"),
    "Стоп": ("#FDECEC", "#C62828"),
}
OPERBLOCK_TEMPLATE_FILTERS = (
    ("bolus", "Болюсы"),
    ("continuous_infusion", "Дозатор"),
    ("timed_infusion", "Капельницы"),
    ("gas", "Газ"),
    ("favorite", "Избранное"),
)
OPERBLOCK_ORDERS_FILTERS = (
    ("all", "Все"),
    ("bolus", "Болюсы"),
    ("gas", "Газ"),
    ("continuous_infusion", "Дозатор"),
    ("timed_infusion", "Капельницы"),
    ("active", "Активные"),
)
OPERBLOCK_ORDER_ROUTE_DEFAULT = "iv"
OPERBLOCK_ORDER_ROUTE_INTRAMUSCULAR = "im"
OPERBLOCK_ORDER_ROUTE_TAG_RE = re.compile(r"\[OB_ROUTE:(?P<route>iv|im)\]", flags=re.IGNORECASE)
OPERBLOCK_ORDER_ROUTE_LONG_LABELS = {
    OPERBLOCK_ORDER_ROUTE_DEFAULT: "в/в",
    OPERBLOCK_ORDER_ROUTE_INTRAMUSCULAR: "в/мышечно",
}
OPERBLOCK_ORDER_ROUTE_SHORT_LABELS = {
    OPERBLOCK_ORDER_ROUTE_DEFAULT: "в/в",
    OPERBLOCK_ORDER_ROUTE_INTRAMUSCULAR: "в/м",
}
OPERBLOCK_ROUTE_ONLY_REFRESH_SUPPRESS_SECONDS = 10.0
OPERBLOCK_LOCAL_WRITE_REFRESH_SUPPRESS_SECONDS = 10.0
OPERBLOCK_ACTIVE_INFUSION_COLUMNS = 3
OPERBLOCK_ACTIVE_INFUSION_CARD_MIN_HEIGHT = 128
OPERBLOCK_ACTIVE_INFUSION_GRID_SPACING = 10
OPERBLOCK_ACTIVE_INFUSION_EMPTY_HEIGHT = OPERBLOCK_ACTIVE_INFUSION_CARD_MIN_HEIGHT


TOOLTIP_WHITE_STYLE = """
    QToolTip {
        color: #000000;
        background-color: #ffffff;
        border: 1px solid #8a8f94;
        padding: 4px 6px;
    }
"""


def _operblock_vertical_scrollbar_style(
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
            background: #e8edf3;
            border: 1px solid #d4dce7;
            border-radius: 4px;
            width: 7px;
        }}
        QScrollBar#{object_name}::handle:vertical {{
            background: #8fa3ba;
            border: 1px solid #71869d;
            border-radius: 4px;
            min-height: 28px;
        }}
        QScrollBar#{object_name}::handle:vertical:hover {{
            background: #7890aa;
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


def _operblock_combo_box_style() -> str:
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
            border: 1px solid #9fb2c7;
            border-radius: 5px;
            background: #e6eef7;
        }}
        QComboBox::drop-down:hover {{
            border-color: #7f9bb8;
            background: #d8e5f2;
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


def _operblock_app_icon_path() -> str:
    for icon_name in ("remcardicon.png", "remcardicon.ico"):
        icon_path = os.path.join(get_icon_dir(), icon_name)
        if os.path.exists(icon_path):
            return icon_path
    return ""


def _apply_operblock_window_icon(window: QWidget) -> None:
    icon_path = _operblock_app_icon_path()
    if icon_path:
        window.setWindowIcon(QIcon(icon_path))


def _create_operblock_title_icon(size: int = 22) -> QLabel | None:
    icon_path = _operblock_app_icon_path()
    if not icon_path:
        return None
    pixmap = QPixmap(icon_path)
    if pixmap.isNull():
        return None
    icon_label = QLabel()
    icon_label.setObjectName("operblock_dialog_icon")
    icon_label.setFixedSize(size, size)
    icon_label.setPixmap(pixmap.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    return icon_label


DANGER_BUTTON_STYLE = f"""
    QPushButton {{
        background-color: #fff0f0;
        color: {COLOR_DANGER};
        font-size: 13px;
        font-weight: bold;
        padding: 4px 12px;
        border-radius: {CUSTOM_DIALOG_RADIUS};
        border: 1.5px solid {COLOR_DANGER};
    }}
    QPushButton:hover {{
        background-color: #ffe2e2;
    }}
    QPushButton:disabled {{
        background-color: {BG_MAIN};
        color: {TEXT_MUTED};
        border: 1px solid {BORDER_LIGHT};
    }}
"""


OPERBLOCK_DIALOG_CANCEL_BUTTON_STYLE = (
    STYLE_PATIENT_FORM_CANCEL_BUTTON
    + """
    QPushButton {
        padding: 4px 12px;
        min-width: 72px;
    }
"""
)
OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE = (
    STYLE_PATIENT_FORM_SAVE_BUTTON
    + """
    QPushButton {
        padding: 4px 12px;
        min-width: 72px;
    }
"""
)


def _operblock_primary_action_button_style(
    *,
    radius: int = 8,
    padding: str = "8px 15px",
    font_size: int = 13,
    font_weight: int = 700,
) -> str:
    return f"""
        QPushButton {{
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #6366F1, stop:1 #4F46E5);
            color: #FFFFFF;
            border: 1px solid #4F46E5;
            border-radius: {int(radius)}px;
            padding: {padding};
            font-size: {int(font_size)}px;
            font-weight: {int(font_weight)};
        }}
        QPushButton:hover {{
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #7478FF, stop:1 #5B54F0);
            border-color: #6366F1;
        }}
        QPushButton:pressed {{
            background: #4338CA;
            border-color: #4338CA;
        }}
        QPushButton:disabled {{
            background: #CBD5E1;
            border-color: {OPERBLOCK_ORDERS_BORDER};
            color: #FFFFFF;
        }}
    """


OPERBLOCK_MEDICATION_SETTINGS_ROW_STYLE = f"""
    QFrame#medicationPresetSettingsRow {{
        background-color: {BG_LIGHT};
        border: 1px solid {BORDER_LIGHT};
        border-radius: {CUSTOM_DIALOG_RADIUS};
    }}
    QFrame#medicationPresetSettingsRow QLineEdit:read-only {{
        background: {BG_MAIN};
        color: {TEXT_SECONDARY};
    }}
"""


PATIENT_CARD_STYLE = f"""
    QFrame#operblockTableCard {{
        background-color: {BG_CARD};
        border: 1.5px solid {BORDER_COLOR};
        border-radius: 8px;
    }}
    QLabel {{
        background: transparent;
        border: none;
    }}
"""


SECTOR_HEADER_STYLE = f"""
    QLabel {{
        font-weight: bold;
        font-size: 14px;
        color: {TEXT_PRIMARY};
        background-color: {BG_LIGHT};
        border-top: 1.5px solid {BORDER_COLOR};
        border-left: 1.5px solid {BORDER_COLOR};
        border-right: 1.5px solid {BORDER_COLOR};
        border-bottom: 0.5px solid {BORDER_COLOR};
        border-top-left-radius: {CUSTOM_DIALOG_RADIUS};
        border-top-right-radius: {CUSTOM_DIALOG_RADIUS};
    }}
"""


SECTOR_BODY_STYLE = f"""
    QFrame {{
        background-color: {BG_MAIN};
        border-left: 1.5px solid {BORDER_COLOR};
        border-right: 1.5px solid {BORDER_COLOR};
        border-bottom: 1.5px solid {BORDER_COLOR};
        border-bottom-left-radius: {CUSTOM_DIALOG_RADIUS};
        border-bottom-right-radius: {CUSTOM_DIALOG_RADIUS};
        border-top: none;
    }}
    QLabel {{
        background: transparent;
        border: none;
        color: {TEXT_PRIMARY};
    }}
"""


def _label(text: str, *, size: int = 12, weight: int = 400, color: str = TEXT_PRIMARY) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(f"font-size: {size}px; font-weight: {weight}; color: {color}; background: transparent; border: none;")
    return label


def _line_edit() -> QLineEdit:
    edit = QLineEdit()
    edit.setFixedHeight(34)
    return edit


def _format_dt(value) -> str:
    text = str(value or "").replace("T", " ")
    if "." in text:
        text = text.split(".", 1)[0]
    return text[:16] if text else "-"


def _parse_datetime_value(value) -> datetime | None:
    text = str(value or "").strip().replace("T", " ")
    if not text:
        return None
    if "." in text:
        text = text.split(".", 1)[0]
    candidates = (text[:19], text[:16], text)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        for candidate in candidates:
            try:
                return datetime.strptime(candidate, fmt)
            except Exception:
                continue
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _minute_floor_dt(value: datetime | None) -> datetime | None:
    return value.replace(second=0, microsecond=0) if isinstance(value, datetime) else None


def _format_protocol_started_at(value) -> str:
    parsed = _parse_datetime_value(value)
    return parsed.strftime("%d.%m.%Y %H:%M") if parsed else "-"


def _format_main_remcard_status_text(started_at, *, active: bool = True) -> tuple[str, str]:
    parsed = _parse_datetime_value(started_at)
    time_str = parsed.strftime("%H:%M") if parsed else "--:--"
    if active:
        return f"🔴 Операционная ({time_str})", "#e74c3c"
    return f"⚫ Случай закрыт ({time_str})", "#968c8c"


def normalize_operblock_birth_date_text(value: str, *, final: bool = True) -> str:
    text = str(value or "").strip().replace(",", ".").replace("/", ".").replace("\\", ".")
    if not text:
        return ""

    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return ""
    day = digits[:2]
    rest = digits[2:]
    if len(digits) <= 2:
        return day
    if not rest:
        return day

    month_len = 1
    if len(rest) >= 2:
        two_digit_month = int(rest[:2])
        month_len = 2 if 1 <= two_digit_month <= 12 else 1
    month = rest[:month_len]
    year = rest[month_len:]

    if final and len(month) == 1:
        month = month.zfill(2)
    if final and len(year) in (1, 2):
        year_num = int(year)
        pivot = datetime.now().year % 100
        century = 1900 if year_num > pivot else 2000
        year = f"{century + year_num:04d}"

    result = f"{day}.{month}"
    if year:
        result = f"{result}.{year[:4]}"
    return result[:10]


def _safe_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None


def _stable_ui_hash(payload) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_IMPLICIT_DOSE_UNIT_PATTERN = (
    r"(?:мкгр|мкг|мг|мл|гр|г|ед|ме|ME|IU|mcg|mkg|mg|ml|ug|g|ed)"
)
_IMPLICIT_DOSE_COMPONENT_PATTERN = (
    rf"\d+(?:[.,]\d+)?\s*{_IMPLICIT_DOSE_UNIT_PATTERN}(?![A-Za-zА-Яа-яЁёµ/])"
)
_IMPLICIT_TRAILING_DOSE_RE = re.compile(
    rf"(?P<dose>{_IMPLICIT_DOSE_COMPONENT_PATTERN}"
    rf"(?:\s*(?:[,;+]|и|\s+)\s*{_IMPLICIT_DOSE_COMPONENT_PATTERN})*"
    rf"(?:\s*\([^)]*\))*\s*)$",
    flags=re.IGNORECASE,
)


def _split_order_drug_and_dose(text: str) -> tuple[str, str]:
    clean = re.sub(r"\s+", " ", str(text or "").strip())
    if " - " in clean:
        drug, dose = clean.split(" - ", 1)
        return drug.strip() or "Без названия", dose.strip()

    match = _IMPLICIT_TRAILING_DOSE_RE.search(clean)
    if match:
        drug = clean[: match.start()].strip()
        if drug and not re.search(r"\d+\s*[-–—]\s*$", drug):
            drug = re.sub(r"\s*[:;,–—-]\s*$", "", drug).strip()
            if drug:
                return drug, match.group("dose").strip()
    return clean or "Без названия", ""


def _build_order_text_for_display(drug_name: str, dose_text: str) -> str:
    drug = str(drug_name or "").strip()
    dose = str(dose_text or "").strip()
    return f"{drug} {dose}".strip() if dose else drug


_BOLUS_MASS_DOSE_RE = re.compile(
    r"^(?P<value>\d+(?:[.,]\d+)?)(?:\s*(?P<unit>мкгр|мкг|mcg|mkg|ug|мг|mg|гр|г|g))?$",
    flags=re.IGNORECASE,
)


def _normalize_bolus_dose_text(value: str) -> str:
    clean = re.sub(r"\s+", " ", str(value or "").strip())
    if not clean:
        return ""

    single_match = _BOLUS_MASS_DOSE_RE.fullmatch(clean)
    if single_match:
        try:
            amount = Decimal(single_match.group("value").replace(",", "."))
        except (InvalidOperation, AttributeError):
            return clean
        _unit_key, unit_label = _normalize_dose_unit(single_match.group("unit") or "мг")
        return f"{_format_decimal_ru(amount)} {unit_label}"

    def normalize_component(match: re.Match) -> str:
        try:
            amount = Decimal(match.group("value").replace(",", "."))
        except (InvalidOperation, AttributeError):
            return match.group(0)
        _unit_key, unit_label = _normalize_dose_unit(match.group("unit"))
        return f"{_format_decimal_ru(amount)} {unit_label}"

    normalized = _DOSE_COMPONENT_RE.sub(normalize_component, clean)
    return normalized if _order_dose_components(normalized) else clean


def _normalize_order_route_code(value) -> str:
    code = str(value or "").strip().casefold()
    if code in {"im", "вм", "в/м", "intramuscular", "внутримышечно", "в/мышечно"}:
        return OPERBLOCK_ORDER_ROUTE_INTRAMUSCULAR
    return OPERBLOCK_ORDER_ROUTE_DEFAULT


def _order_route_code_from_comment(comment: str) -> str:
    match = OPERBLOCK_ORDER_ROUTE_TAG_RE.search(str(comment or ""))
    return _normalize_order_route_code(match.group("route")) if match else OPERBLOCK_ORDER_ROUTE_DEFAULT


def _strip_order_route_tag(comment: str) -> str:
    return re.sub(r"\s+", " ", OPERBLOCK_ORDER_ROUTE_TAG_RE.sub("", str(comment or ""))).strip()


def _order_comment_with_route(comment: str, route_code: str) -> str:
    clean_comment = _strip_order_route_tag(comment)
    if _normalize_order_route_code(route_code) == OPERBLOCK_ORDER_ROUTE_INTRAMUSCULAR:
        return f"{clean_comment} [OB_ROUTE:im]".strip()
    return clean_comment


def _order_route_code(row: dict) -> str:
    route = str((row or {}).get("route") or "").strip()
    if route:
        return _normalize_order_route_code(route)
    payload = (row or {}).get("payload")
    if isinstance(payload, dict) and payload.get("route"):
        return _normalize_order_route_code(payload.get("route"))
    return _order_route_code_from_comment(str((row or {}).get("comment") or ""))


def _order_route_suffix(row: dict, *, short: bool = False) -> str:
    route_code = _order_route_code(row)
    if route_code == OPERBLOCK_ORDER_ROUTE_DEFAULT:
        return ""
    labels = OPERBLOCK_ORDER_ROUTE_SHORT_LABELS if short else OPERBLOCK_ORDER_ROUTE_LONG_LABELS
    label = labels.get(route_code, "")
    return f"({label})" if label else ""


def _order_dose_text_with_route(dose_text: str, row: dict, *, short: bool = False) -> str:
    clean = re.sub(r"\s+", " ", str(dose_text or "").strip())
    suffix = _order_route_suffix(row, short=short)
    return f"{clean} {suffix}".strip() if clean and suffix else clean


def _clean_operblock_marker_drug_name(drug_name: str) -> str:
    clean = re.sub(r"\s+", " ", str(drug_name or "").strip())
    clean = re.sub(r"\s+\d+(?:[.,]\d+)?\s*%$", "", clean).strip()
    return clean or "Без названия"


def _preferred_operblock_marker_dose(dose_text: str) -> str:
    components = _order_dose_components(dose_text)
    priorities = (
        lambda item: bool(item.get("parenthesized")) and item.get("unit_key") in {"мг", "мкг"},
        lambda item: item.get("unit_key") in {"мг", "мкг"},
        lambda item: item.get("unit_key") == "мл",
    )
    for predicate in priorities:
        for component in components:
            if predicate(component):
                return f"{_format_decimal_ru(component['value'])} {component['unit_label']}"
    return str(dose_text or "").strip()


def _format_operblock_order_marker_text(text: str) -> str:
    return format_operblock_medication_display_label(text)


def _format_order_day(value) -> str:
    parsed = _parse_datetime_value(value)
    return parsed.strftime("%d.%m.%Y") if parsed else "Без даты"


def _format_order_time(value) -> str:
    parsed = _parse_datetime_value(value)
    return parsed.strftime("%H:%M") if parsed else "--:--"


def _split_infusion_rate_text(rate_text: str) -> tuple[str, str]:
    text = re.sub(r"\s+", " ", str(rate_text or "").strip())
    match = re.match(r"^(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>.*)$", text)
    if not match:
        return "", ""
    value = match.group("value").strip()
    try:
        value = _format_decimal_ru(Decimal(value.replace(",", ".")))
    except (InvalidOperation, ValueError):
        pass
    return value, OPERBLOCK_INFUSION_RATE_UNIT


def _format_infusion_rate(value, unit) -> str:
    value_text = str(value or "").strip()
    if not value_text:
        return ""
    try:
        value_text = _format_decimal_ru(Decimal(value_text.replace(",", ".")))
    except (InvalidOperation, ValueError):
        pass
    unit_text = OPERBLOCK_INFUSION_RATE_UNIT
    return f"{value_text} {unit_text}".strip()


def _compact_infusion_rate_display_text(rate_text: str) -> str:
    value, _unit = _split_infusion_rate_text(rate_text)
    if value:
        return f"{value} мл/ч"
    text = re.sub(r"\s+", " ", str(rate_text or "").strip())
    return text.replace("мл/час", "мл/ч")


def _compact_infusion_rate_texts(rate_texts: list[str]) -> str:
    cleaned = [re.sub(r"\s+", " ", str(text or "").strip()) for text in rate_texts]
    cleaned = [text for text in cleaned if text]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]

    parsed: list[tuple[str, str]] = []

    def normalize_unit(unit_text: str) -> str:
        raw_unit = re.sub(r"\s+", "", str(unit_text or "").strip().casefold())
        if raw_unit in {"мл", "ml"}:
            return "мл"
        if raw_unit in {"мл/час", "мл/ч", "ml/h", "ml/hr", "ml/hour"}:
            return OPERBLOCK_INFUSION_RATE_UNIT
        if raw_unit in {"mac", "мак"}:
            return "MAC"
        return re.sub(r"\s+", " ", str(unit_text or "").strip())

    for text in cleaned:
        match = re.match(r"^(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>.*)$", text)
        if not match:
            return " - ".join(cleaned)
        value = match.group("value").strip()
        try:
            value = _format_decimal_ru(Decimal(value.replace(",", ".")))
        except (InvalidOperation, ValueError):
            pass
        unit = normalize_unit(match.group("unit"))
        if not value or not unit:
            return " - ".join(cleaned)
        parsed.append((value, unit))

    units = {unit for _value, unit in parsed}
    if len(units) != 1:
        return " - ".join(cleaned)

    values: list[str] = []
    for value, _unit in parsed:
        if not values or values[-1] != value:
            values.append(value)
    return f"{'-'.join(values)} {parsed[0][1]}"


def _decimal_from_ru_number(value) -> Decimal | None:
    text = str(value or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _format_infusion_volume_ml(value: Decimal | None) -> str:
    if value is None:
        return ""
    rounded = max(Decimal("0"), value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if rounded == rounded.to_integral_value():
        text = str(int(rounded))
    else:
        text = format(rounded.normalize(), "f").rstrip("0").rstrip(".").replace(".", ",")
    return f"{text} мл"


def _round_timed_infusion_volume_ml(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    rounded = max(Decimal("0"), value).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return rounded if rounded > 0 else None


def _normalize_volume_ml_text(value) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    text = re.sub(r"\s*мл\s*$", "", text, flags=re.IGNORECASE).strip()
    volume = _decimal_from_ru_number(text)
    if volume is None or volume <= 0:
        return ""
    return _format_infusion_volume_ml(volume).replace(" мл", "")


def _volume_decimal_ml(value) -> Decimal | None:
    return _decimal_from_ru_number(_normalize_volume_ml_text(value))


def _volume_text_without_unit(value: Decimal | None) -> str:
    return _format_infusion_volume_ml(value).replace(" мл", "") if value is not None else ""


def _solution_display_label(value) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return ""
    lowered = text.casefold()
    if lowered.startswith("s. "):
        return text
    if lowered.startswith("sol. "):
        return f"S. {text[5:].strip()}".strip()
    return f"S. {text}"


def _source_has_solvent(source: dict | None) -> bool:
    source = source or {}
    return bool(str(source.get("solvent_id") or "").strip() or str(source.get("solvent_label") or "").strip())


def _source_solvent_volume_ml(source: dict | None) -> Decimal | None:
    if not _source_has_solvent(source):
        return None
    return _volume_decimal_ml((source or {}).get("solvent_volume_ml"))


def _infusion_declared_volume_ml(interval: dict) -> Decimal | None:
    if _is_gas_infusion(interval):
        return None
    payload = interval.get("payload") if isinstance(interval.get("payload"), dict) else {}

    def declared(value: Decimal | None) -> Decimal | None:
        if _infusion_has_rate(interval):
            return value
        return _round_timed_infusion_volume_ml(value)

    declared_total = _volume_decimal_ml(
        (payload or {}).get("declared_total_volume_ml") or (payload or {}).get("total_volume_ml")
    )
    if declared_total is not None:
        return declared(declared_total)

    event_volume = _volume_decimal_ml(interval.get("volume_ml"))
    payload_volume = _volume_decimal_ml((payload or {}).get("volume_ml"))
    base_volume = event_volume if event_volume is not None else payload_volume
    calculated_volume = _volume_decimal_ml((payload or {}).get("calculated_volume_ml"))
    solvent_volume = _source_solvent_volume_ml(payload)
    if solvent_volume is not None:
        if calculated_volume is not None:
            if base_volume is None or abs(base_volume - calculated_volume) <= Decimal("0.01"):
                return declared(calculated_volume + solvent_volume)
            return declared(base_volume)
        return declared(base_volume if base_volume is not None else solvent_volume)
    if base_volume is not None:
        return declared(base_volume)
    return declared(_volume_decimal_ml((payload or {}).get("solvent_volume_ml")))


def _format_infusion_declared_volume(interval: dict) -> str:
    return _format_infusion_volume_ml(_infusion_declared_volume_ml(interval))


def _is_gas_infusion(interval: dict) -> bool:
    payload = interval.get("payload") if isinstance(interval.get("payload"), dict) else {}
    kind = str((payload or {}).get("kind") or "").strip().casefold()
    return kind == "gas"


def _infusion_display_drug_name(interval: dict, fallback: str = "Дозатор") -> str:
    data = interval or {}
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    clean_fallback = str(fallback or "").strip() or ("Газ" if _is_gas_infusion(data) else "Дозатор")
    raw_name = (
        str((payload or {}).get("display_name") or "").strip()
        or str((payload or {}).get("label") or "").strip()
        or str(data.get("drug_label") or "").strip()
        or str(data.get("display_label") or "").strip()
        or clean_fallback
    )
    name = re.sub(r"\s+", " ", raw_name).strip() or clean_fallback
    name = re.sub(
        r"\s+\d+(?:[,.]\d+)?\s*(?:мл/час|мл/ч|ml/h|ml/hr|мл|ml|MAC|мак)\s*$",
        "",
        name,
        flags=re.IGNORECASE,
    ).strip() or clean_fallback
    concentration = re.sub(
        r"\s+",
        " ",
        str((payload or {}).get("concentration") or data.get("concentration_text") or "").strip(),
    )
    has_percentage_in_name = bool(re.search(r"\d+(?:[.,]\d+)?\s*%", name))
    if concentration and not has_percentage_in_name and concentration.casefold() not in name.casefold():
        name = f"{name} {concentration}".strip()
    return name


def _normalize_gas_identity_text(value: str) -> str:
    clean = re.sub(r"\s+", " ", str(value or "").strip())
    clean = re.sub(r"^(?:s|sol)\.\s+", "", clean, flags=re.IGNORECASE).strip()
    clean = re.sub(r"\s+\d+(?:[.,]\d+)?\s*%$", "", clean).strip()
    return clean.casefold().replace("ё", "е")


def _gas_display_name_for_payload(drug_name: str, payload: dict | None) -> str:
    gas_payload = dict(payload or {}) if isinstance(payload, dict) else {}
    gas_payload["kind"] = "gas"
    return _infusion_display_drug_name({"drug_label": drug_name, "payload": gas_payload}, "Газ")


def _gas_identity_matches(active_interval: dict, drug_name: str, payload: dict | None) -> bool:
    active_payload = active_interval.get("payload") if isinstance(active_interval.get("payload"), dict) else {}
    requested_payload = payload if isinstance(payload, dict) else {}
    for key in ("preset_id", "source_drug_id"):
        active_value = str((active_payload or {}).get(key) or "").strip()
        requested_value = str((requested_payload or {}).get(key) or "").strip()
        if active_value and requested_value and active_value == requested_value:
            return True
    active_name = _normalize_gas_identity_text(_infusion_display_drug_name(active_interval, "Газ"))
    requested_name = _normalize_gas_identity_text(_gas_display_name_for_payload(drug_name, requested_payload))
    return bool(active_name and requested_name and active_name == requested_name)


def _normalize_gas_dose_text(value: str) -> str:
    clean = re.sub(r"\s+", " ", str(value or "").strip())
    if not clean:
        return ""
    value_only = re.sub(r"(?i)(?:mac|мак)", " ", clean)
    value_only = re.sub(r"\s+", " ", value_only).strip()
    match = re.fullmatch(
        r"(?P<first>\d+(?:[.,]\d+)?)(?:\s*[-–—]\s*(?P<second>\d+(?:[.,]\d+)?))?",
        value_only,
    )
    if match:
        values = []
        for group_name in ("first", "second"):
            raw_value = match.group(group_name)
            if not raw_value:
                continue
            try:
                values.append(_format_decimal_ru(Decimal(raw_value.replace(",", "."))))
            except (InvalidOperation, ValueError):
                values.append(raw_value.replace(".", ","))
        return f"{'-'.join(values)} MAC"
    clean = re.sub(r"(?i)(?:mac|мак)", " MAC ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    clean = re.sub(r"(?i)(?:\s+MAC)+$", " MAC", clean)
    if "mac" not in clean.casefold():
        clean = f"{clean} MAC"
    return clean


def _gas_dose_text(interval: dict) -> str:
    payload = interval.get("payload") if isinstance(interval.get("payload"), dict) else {}
    return _normalize_gas_dose_text(str((payload or {}).get("display_dose_text") or (payload or {}).get("dose_text") or ""))


def _gas_dose_events(interval: dict) -> list[dict]:
    events: list[dict] = []
    for item in list((interval or {}).get("dose_history") or []):
        event_dt = _minute_floor_dt(_parse_datetime_value((item or {}).get("event_time")))
        dose_text = _normalize_gas_dose_text(str((item or {}).get("dose_text") or ""))
        if event_dt is None or not dose_text:
            continue
        events.append(
            {
                "event_id": (item or {}).get("event_id"),
                "event_time": event_dt,
                "dose_text": dose_text,
                "revision": (item or {}).get("revision"),
            }
        )
    if not events:
        start_dt = _minute_floor_dt(_parse_datetime_value((interval or {}).get("start_time")))
        dose_text = _gas_dose_text(interval or {})
        if start_dt is not None and dose_text:
            events.append({"event_time": start_dt, "dose_text": dose_text})
    events.sort(key=lambda item: item["event_time"])
    deduped: list[dict] = []
    for event in events:
        if deduped and deduped[-1]["event_time"] == event["event_time"]:
            deduped[-1] = event
        else:
            deduped.append(event)
    return deduped


def _is_volume_only_infusion(interval: dict) -> bool:
    if _is_gas_infusion(interval):
        return False
    return bool(_infusion_declared_volume_ml(interval) is not None and not _infusion_has_rate(interval))


def _counted_infusion_volume_ml(interval: dict, *, now: datetime | None = None) -> Decimal | None:
    if _infusion_has_rate(interval):
        return _infusion_volume_ml(interval, now=now)
    if _infusion_declared_volume_ml(interval) is None:
        return None
    if str(interval.get("status") or "") == "active" and not interval.get("end_time"):
        return None
    return _infusion_declared_volume_ml(interval)


def _infusion_rate_events(interval: dict) -> list[dict]:
    events = []
    for item in list(interval.get("rate_history") or []):
        event_dt = _minute_floor_dt(_parse_datetime_value((item or {}).get("event_time")))
        rate_value = _decimal_from_ru_number((item or {}).get("rate_value"))
        if event_dt is None or rate_value is None:
            continue
        events.append({"event_time": event_dt, "rate_value": rate_value})
    if not events:
        start_dt = _minute_floor_dt(_parse_datetime_value(interval.get("start_time")))
        rate_value = _decimal_from_ru_number(interval.get("current_rate_value"))
        if start_dt is not None and rate_value is not None:
            events.append({"event_time": start_dt, "rate_value": rate_value})
    events.sort(key=lambda item: item["event_time"])
    deduped: list[dict] = []
    for event in events:
        if deduped and deduped[-1]["event_time"] == event["event_time"]:
            deduped[-1] = event
        else:
            deduped.append(event)
    return deduped


def _infusion_volume_ml(interval: dict, *, now: datetime | None = None) -> Decimal | None:
    start_dt = _minute_floor_dt(_parse_datetime_value(interval.get("start_time")))
    if start_dt is None:
        return None
    current_time = _minute_floor_dt(now or datetime.now())
    end_dt = _minute_floor_dt(_parse_datetime_value(interval.get("end_time")))
    if end_dt is None and str(interval.get("status") or "") != "active":
        end_dt = current_time
    effective_end = end_dt or current_time
    if effective_end <= start_dt:
        return Decimal("0")

    events = [event for event in _infusion_rate_events(interval) if event["event_time"] <= effective_end]
    if not events:
        return None
    if events[0]["event_time"] > start_dt:
        events.insert(0, {"event_time": start_dt, "rate_value": events[0]["rate_value"]})

    total = Decimal("0")
    for index, event in enumerate(events):
        segment_start = max(start_dt, event["event_time"])
        next_start = events[index + 1]["event_time"] if index + 1 < len(events) else effective_end
        segment_end = min(effective_end, next_start)
        if segment_end <= segment_start:
            continue
        minutes = Decimal(int((segment_end - segment_start).total_seconds() // 60))
        total += event["rate_value"] * minutes / Decimal("60")
    return total


def _format_infusion_executed_volume(interval: dict, *, now: datetime | None = None) -> str:
    volume = _infusion_volume_ml(interval, now=now)
    return _format_infusion_volume_ml(volume)


def _infusion_has_rate(interval: dict) -> bool:
    if _is_gas_infusion(interval or {}):
        return False
    if _format_infusion_rate(interval.get("current_rate_value"), interval.get("current_rate_unit")):
        return True
    return bool(_infusion_rate_events(interval))


def _normalize_infusion_rate_option(rate_text: str) -> str:
    value, unit = _split_infusion_rate_text(rate_text)
    return _format_infusion_rate(value, unit) if value else ""


def _normalize_infusion_rate_options(rates) -> list[str]:
    result: list[str] = []
    for rate in rates or []:
        text = _normalize_infusion_rate_option(str(rate or ""))
        if text and text not in result:
            result.append(text)
    return result or list(OPERBLOCK_DEFAULT_INFUSION_RATES)


def _dose_option_with_unit(value, unit) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return ""
    if _order_dose_components(text):
        return text
    _unit_key, unit_label = _normalize_dose_unit(str(unit or ""))
    return f"{text} {unit_label}".strip() if unit_label else text


def _timed_infusion_dose_options(preset: dict) -> list[str]:
    result: list[str] = []

    def add_option(value) -> None:
        text = re.sub(r"\s+", " ", str(value or "").strip())
        if text and text not in result:
            result.append(text)

    for dose in preset.get("doses") or []:
        add_option(dose)

    if not result:
        add_option(_dose_option_with_unit(preset.get("default_dose"), preset.get("unit")))

    if not result:
        for value in (preset.get("solvent_volume_ml"), preset.get("volume_ml")):
            volume = _normalize_volume_ml_text(value)
            if volume:
                add_option(f"{volume} мл")
    return result


def _quick_order_title_and_concentration(title: str, concentration_text: str = "") -> tuple[str, str]:
    clean_title = re.sub(r"\s+", " ", str(title or "").strip())
    clean_concentration = re.sub(r"\s+", " ", str(concentration_text or "").strip())
    return clean_title or str(title or "").strip(), clean_concentration


def _quick_order_solvent_text(source: dict) -> str:
    solvent = _solution_display_label((source or {}).get("solvent_label") or (source or {}).get("solvent_id"))
    volume = _normalize_volume_ml_text((source or {}).get("solvent_volume_ml"))
    if solvent and volume:
        return f"{solvent} - {volume} мл"
    return solvent or (f"{volume} мл" if volume else "")


def _split_semicolon_list(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(";") if item.strip()]


def _join_semicolon_list(values) -> str:
    return "; ".join(str(value or "").strip() for value in values or [] if str(value or "").strip())


def _format_infusion_duration(start_time) -> str:
    start_dt = _parse_datetime_value(start_time)
    if start_dt is None:
        return ""
    delta = max(timedelta(0), datetime.now().replace(second=0, microsecond=0) - _minute_floor_dt(start_dt))
    minutes = int(delta.total_seconds() // 60)
    hours, mins = divmod(minutes, 60)
    if hours:
        return f"{hours}ч {mins:02d}мин"
    return f"{mins}мин"


def _format_infusion_interval_duration(start_time, end_time=None) -> str:
    start_dt = _minute_floor_dt(_parse_datetime_value(start_time))
    if start_dt is None:
        return ""
    end_dt = _minute_floor_dt(_parse_datetime_value(end_time)) or datetime.now().replace(second=0, microsecond=0)
    delta = max(timedelta(0), end_dt - start_dt)
    minutes = int(delta.total_seconds() // 60)
    hours, mins = divmod(minutes, 60)
    if hours:
        return f"{hours}ч {mins:02d}мин"
    return f"{mins}мин"


def _order_sort_dt(row: dict) -> datetime:
    return _parse_datetime_value(row.get("datetime")) or datetime.min


_DOSE_COMPONENT_RE = re.compile(r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>[A-Za-zА-Яа-яЁёµ%./]+)")


def _normalize_dose_unit(unit: str) -> tuple[str, str]:
    raw = str(unit or "").strip().lower().replace("ё", "е").replace("µ", "мк")
    raw = raw.replace(".", "")
    aliases = {
        "ml": "мл",
        "мл": "мл",
        "миллилитр": "мл",
        "миллилитра": "мл",
        "миллилитры": "мл",
        "миллилитров": "мл",
        "mg": "мг",
        "мг": "мг",
        "миллиграмм": "мг",
        "миллиграмма": "мг",
        "миллиграммы": "мг",
        "миллиграммов": "мг",
        "миллиграмы": "мг",
        "mcg": "мкг",
        "mkg": "мкг",
        "мкг": "мкг",
        "мкгр": "мкг",
        "микрограмм": "мкг",
        "микрограмма": "мкг",
        "микрограммы": "мкг",
        "микрограммов": "мкг",
        "ug": "мкг",
        "g": "г",
        "гр": "г",
        "г": "г",
        "грам": "г",
        "грамм": "г",
        "грама": "г",
        "грамма": "г",
        "грамы": "г",
        "граммы": "г",
        "граммов": "г",
        "ed": "ед",
        "ед": "ед",
        "me": "МЕ",
        "ме": "МЕ",
        "mac": "MAC",
        "мак": "MAC",
        "%": "%",
    }
    label = aliases.get(raw, raw or unit)
    return label.casefold(), label


def _parse_dose_components(text: str, *, parenthesized: bool) -> list[dict]:
    components: list[dict] = []
    for match in _DOSE_COMPONENT_RE.finditer(str(text or "")):
        try:
            value = Decimal(match.group("value").replace(",", "."))
        except (InvalidOperation, AttributeError):
            continue
        unit_key, unit_label = _normalize_dose_unit(match.group("unit"))
        if not unit_key:
            continue
        components.append(
            {
                "value": value,
                "unit_key": unit_key,
                "unit_label": unit_label,
                "parenthesized": parenthesized,
            }
        )
    return components


def _order_dose_components(dose_text: str) -> list[dict]:
    clean = str(dose_text or "")
    parenthetical_parts = re.findall(r"\(([^)]*)\)", clean)
    main_text = re.sub(r"\([^)]*\)", " ", clean)
    components = _parse_dose_components(main_text, parenthesized=False)
    for part in parenthetical_parts:
        components.extend(_parse_dose_components(part, parenthesized=True))
    return components


def _format_decimal_ru(value: Decimal) -> str:
    if value == value.to_integral_value():
        return str(int(value))
    text = format(value.normalize(), "f").rstrip("0").rstrip(".")
    return text.replace(".", ",")


def _mass_to_micrograms(value: Decimal, unit_key: str) -> Decimal | None:
    if unit_key == "мкг":
        return value
    if unit_key == "мг":
        return value * Decimal("1000")
    if unit_key == "г":
        return value * Decimal("1000000")
    return None


def _parse_concentration_mass_per_ml(concentration_text: str) -> Decimal | None:
    text = re.sub(r"\s+", " ", str(concentration_text or "").strip())
    match = re.search(
        r"(?P<mass>\d+(?:[.,]\d+)?)\s*(?P<mass_unit>мкгр|мкг|mcg|mkg|ug|мг|mg|гр|г|g)"
        r"\s*/\s*(?:(?P<volume>\d+(?:[.,]\d+)?)\s*)?(?P<volume_unit>мл|ml)\b",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    mass_value = _decimal_from_ru_number(match.group("mass"))
    volume_value = _decimal_from_ru_number(match.group("volume") or "1")
    if mass_value is None or volume_value is None or mass_value <= 0 or volume_value <= 0:
        return None
    mass_unit_key, _mass_unit_label = _normalize_dose_unit(match.group("mass_unit"))
    mass_micrograms = _mass_to_micrograms(mass_value, mass_unit_key)
    if mass_micrograms is None:
        return None
    return mass_micrograms / volume_value


def _quick_order_explicit_volume_ml(dose_text: str) -> Decimal | None:
    for component in _order_dose_components(dose_text):
        if component.get("unit_key") == "мл" and component.get("value") > 0:
            return component["value"]
    return None


def _quick_order_mass_dose_component(dose_text: str) -> dict | None:
    return next(
        (
            item
            for item in _order_dose_components(dose_text)
            if not bool(item.get("parenthesized")) and item.get("unit_key") in {"мг", "мкг", "г"}
        ),
        None,
    )


def _quick_order_mass_dose_volume_ml(dose_text: str, concentration_text: str) -> Decimal | None:
    concentration_per_ml = _parse_concentration_mass_per_ml(concentration_text)
    if concentration_per_ml is None or concentration_per_ml <= 0:
        return None
    dose_component = _quick_order_mass_dose_component(dose_text)
    if not dose_component:
        return None
    dose_micrograms = _mass_to_micrograms(dose_component["value"], str(dose_component.get("unit_key") or ""))
    if dose_micrograms is None or dose_micrograms <= 0:
        return None
    return dose_micrograms / concentration_per_ml


def _quick_order_dose_volume_ml(dose_text: str, concentration_text: str) -> Decimal | None:
    explicit_volume = _quick_order_explicit_volume_ml(dose_text)
    if explicit_volume is not None:
        return explicit_volume
    return _quick_order_mass_dose_volume_ml(dose_text, concentration_text)


def _quick_order_dose_volume_ml_text(dose_text: str, concentration_text: str) -> str:
    volume = _quick_order_dose_volume_ml(dose_text, concentration_text)
    return _format_infusion_volume_ml(volume).replace(" мл", "") if volume is not None else ""


def _timed_infusion_total_volume_ml(
    preset: dict,
    dose_text: str,
    concentration_text: str,
) -> Decimal | None:
    dose_volume = _quick_order_dose_volume_ml(dose_text, concentration_text)
    solvent_volume = _source_solvent_volume_ml(preset)
    if solvent_volume is not None:
        return _round_timed_infusion_volume_ml((dose_volume or Decimal("0")) + solvent_volume)
    if dose_volume is not None:
        return _round_timed_infusion_volume_ml(dose_volume)
    return _round_timed_infusion_volume_ml(
        _volume_decimal_ml((preset or {}).get("volume_ml") or (preset or {}).get("solvent_volume_ml"))
    )


def _quick_order_dose_display_text(dose_text: str, concentration_text: str) -> str:
    clean_dose = re.sub(r"\s+", " ", str(dose_text or "").strip())
    if not clean_dose or re.search(r"\([^)]*\b(?:мл|ml)\b[^)]*\)", clean_dose, flags=re.IGNORECASE):
        return clean_dose
    volume_text = _format_infusion_volume_ml(_quick_order_mass_dose_volume_ml(clean_dose, concentration_text))
    return f"{clean_dose} ({volume_text})" if volume_text else clean_dose


def _summarize_dose_texts(dose_texts: list[str], *, include_unparsed: bool = True) -> str:
    totals: dict[tuple[bool, str], dict] = {}
    order: list[tuple[bool, str]] = []
    unparsed: list[str] = []
    for dose_text in dose_texts:
        clean_text = re.sub(r"\s+", " ", str(dose_text or "").strip())
        if not clean_text:
            continue
        components = _order_dose_components(clean_text)
        if not components:
            unparsed.append(clean_text)
            continue
        for component in components:
            key = (bool(component["parenthesized"]), str(component["unit_key"]))
            if key not in totals:
                totals[key] = {"value": Decimal("0"), "unit_label": component["unit_label"], "count": 0}
                order.append(key)
            totals[key]["value"] += component["value"]
            totals[key]["count"] += 1

    main_parts: list[str] = []
    parenthetical_parts: list[str] = []
    for key in order:
        total = totals[key]
        if key[1] == "mac":
            value = total["value"] / max(1, int(total.get("count") or 0))
            part = f"среднее {_format_decimal_ru(value)} {total['unit_label']}"
        else:
            part = f"{_format_decimal_ru(total['value'])} {total['unit_label']}"
        if key[0]:
            parenthetical_parts.append(part)
        else:
            main_parts.append(part)

    summary = ", ".join(main_parts) if main_parts else ""
    if parenthetical_parts:
        parens = ", ".join(parenthetical_parts)
        summary = f"{summary} ({parens})" if summary else f"({parens})"

    unique_unparsed = list(dict.fromkeys(unparsed))
    if include_unparsed and unique_unparsed:
        unparsed_text = ", ".join(unique_unparsed)
        summary = f"{summary}, {unparsed_text}" if summary else unparsed_text
    return summary


def _summarize_order_total(rows: list[dict]) -> str:
    summary = _summarize_dose_texts(
        [str(row.get("dose_text") or "") for row in rows],
        include_unparsed=False,
    )
    if not summary:
        return f"внесено: {len(rows)}"
    return f"Итого: {summary}"


class OperBlockSector8Panel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.icon_dir = get_icon_dir()
        self._protocol_mode = False
        self._launcher_back = False
        self._display_visible: dict[str, bool] = {}
        self._display_order: list[str] = []
        self._init_ui()

    def _init_ui(self):
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(10, 0, 10, 0)
        self.layout.setSpacing(10)

        self.title_label = QLabel("Оперблок")
        self.title_label.setStyleSheet(
            f"font-size: 14px; font-weight: 800; color: {COLOR_PRIMARY_DARK}; "
            "background: transparent; border: none;"
        )

        self.btn_archive = self._button(" Архив", "binder.png")
        self.btn_refresh = self._button(" Обновить", "refresh.png")
        self.btn_settings = self._button(" Настройки", "settings.png")
        self.btn_back = self._button(" Назад", "back.png")
        self.btn_exit = self._button(" Выход", "exit.png")
        self.btn_settings.setVisible(False)
        self._button_widgets = {
            "archive": self.btn_archive,
            "refresh": self.btn_refresh,
            "back": self.btn_back,
            "exit": self.btn_exit,
        }
        self.apply_display_settings()

    def _button(self, text: str, icon_name: str) -> QPushButton:
        button = QPushButton(text, self)
        icon_path = os.path.join(self.icon_dir, icon_name)
        if os.path.exists(icon_path):
            button.setIcon(QIcon(icon_path))
            button.setIconSize(button.iconSize())
        button.setMinimumHeight(32)
        button.setCursor(Qt.PointingHandCursor)
        button.setStyleSheet(STYLE_SECTOR8_BUTTON)
        return button

    def _clear_layout(self):
        while self.layout.count():
            self.layout.takeAt(0)

    def apply_display_settings(self):
        try:
            payload = DisplaySettingsStorage().load()
            settings = role_display_settings_from_payload(payload, "operblock")
            section = settings["sector8_buttons"]
            order = list(section["order"])
            visible = dict(section["visible"])
        except Exception:
            order = list(getattr(self, "_button_widgets", {}).keys())
            visible = {button_id: True for button_id in order}

        self._display_order = [button_id for button_id in order if button_id in self._button_widgets]
        for button_id in self._button_widgets:
            if button_id not in self._display_order:
                self._display_order.append(button_id)
        self._display_visible = {
            button_id: bool(visible.get(button_id, True))
            for button_id in self._button_widgets
        }

        self._clear_layout()
        self.layout.addWidget(self.title_label)
        self.layout.addStretch(1)
        for button in self._button_widgets.values():
            button.setVisible(False)
        for button_id in self._display_order:
            button = self._button_widgets.get(button_id)
            if button is None:
                continue
            if self._display_visible.get(button_id, True):
                self.layout.addWidget(button)
                button.setVisible(True)
        self.btn_settings.setVisible(False)
        self._apply_back_visibility()
        self.updateGeometry()

    def _apply_back_visibility(self):
        visible_by_settings = bool(self._display_visible.get("back", True))
        should_show = visible_by_settings and (self._protocol_mode or self._launcher_back)
        self.btn_back.setVisible(should_show)

    def set_protocol_mode(self, enabled: bool, *, launcher_back: bool = False):
        self._protocol_mode = bool(enabled)
        self._launcher_back = bool(launcher_back)
        self._apply_back_visibility()


class ElidedTooltipLabel(QLabel):
    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full_text = ""
        self.setWordWrap(False)
        self.setMinimumWidth(80)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.set_full_text(text)

    def set_full_text(self, text: str):
        self._full_text = str(text or "")
        self.setToolTip(self._full_text)
        self._apply_elide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self):
        width = max(0, self.contentsRect().width())
        text = self._full_text
        if width > 0:
            text = self.fontMetrics().elidedText(self._full_text, Qt.ElideRight, width)
        if self.text() != text:
            super().setText(text)


class OperBlockClickableLabel(QLabel):
    def __init__(self, text: str = "", click_callback=None, parent=None):
        super().__init__(text, parent)
        self._click_callback = click_callback
        if callable(click_callback):
            self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and callable(self._click_callback):
            self._click_callback()
            event.accept()
            return
        super().mousePressEvent(event)


class OperBlockStyledDialog(SavedFramelessDialogMixin, QDialog):
    def __init__(
        self,
        title: str,
        settings_key: str,
        parent=None,
        *,
        minimum_size: tuple[int, int] | None = None,
        initial_size: tuple[int, int] | None = None,
        drag_area_height: int = 58,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        _apply_operblock_window_icon(self)
        if minimum_size:
            self.setMinimumSize(*minimum_size)
        if initial_size:
            self.resize(*initial_size)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setMouseTracking(True)
        self._init_saved_frameless_dialog(f"operblock/{settings_key}", drag_area_height=drag_area_height)
        self._init_dialog_chrome(title)

    def _init_dialog_chrome(self, title: str):
        apply_custom_dialog_style(self)

        self.layout_container = QVBoxLayout(self)
        self.layout_container.setContentsMargins(0, 0, 0, 0)

        self.bg_container = QFrame(self)
        self.bg_container.setObjectName("DialogMainFrame")
        self.bg_container.setMouseTracking(True)
        self.layout_container.addWidget(self.bg_container)

        self.main_layout = QVBoxLayout(self.bg_container)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        header_panel = QFrame(self.bg_container)
        header_panel.setObjectName("DialogTitleBar")
        header_panel.setFixedHeight(30)
        header_layout = QHBoxLayout(header_panel)
        header_layout.setContentsMargins(5, 0, 0, 0)
        header_layout.setSpacing(0)

        icon_label = _create_operblock_title_icon(20)
        if icon_label is not None:
            header_layout.addWidget(icon_label)
            header_layout.addSpacing(8)

        title_label = QLabel(str(title or ""))
        title_label.setObjectName("DialogTitleText")
        header_layout.addWidget(title_label)
        header_layout.addStretch(1)

        self.close_button = QPushButton("✕")
        self.close_button.setObjectName("DialogCloseBtn")
        self.close_button.setFixedSize(30, 30)
        self.close_button.setCursor(Qt.PointingHandCursor)
        self.close_button.clicked.connect(self.reject)
        header_layout.addWidget(self.close_button)
        self.main_layout.addWidget(header_panel)

        self.content_widget = QFrame(self.bg_container)
        self.content_widget.setStyleSheet(STYLE_PATIENT_FORM_TAB)
        self.content_layout = QVBoxLayout(self.content_widget)
        self.content_layout.setContentsMargins(20, 16, 20, 20)
        self.content_layout.setSpacing(10)
        self.main_layout.addWidget(self.content_widget, 1)

    def _finalize_dialog_chrome(self):
        self._restore_saved_geometry()

    def _configure_enter_accept_button(self, cancel_button: QPushButton, save_button: QPushButton) -> None:
        cancel_button.setAutoDefault(False)
        cancel_button.setDefault(False)
        save_button.setAutoDefault(True)
        save_button.setDefault(True)


class OperBlockSettingsDialog(OperBlockStyledDialog):
    def __init__(self, parent=None):
        super().__init__(
            "Настройки оперблока",
            "settings_menu_geometry",
            parent,
            minimum_size=(420, 220),
            initial_size=(520, 280),
        )
        self._init_ui()
        self._finalize_dialog_chrome()

    def _init_ui(self):
        layout = self.content_layout
        layout.setSpacing(12)

        self.medications_button = self._menu_button("Настройки препаратов")
        self.anesthesia_types_button = self._menu_button("Виды пособия")
        self.team_button = self._menu_button("Опер. бригада")
        layout.addWidget(self.medications_button)
        layout.addWidget(self.anesthesia_types_button)
        layout.addWidget(self.team_button)
        layout.addStretch(1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        close_button = QPushButton("Закрыть")
        close_button.setMinimumHeight(34)
        close_button.setStyleSheet(OPERBLOCK_DIALOG_CANCEL_BUTTON_STYLE)
        close_button.clicked.connect(self.reject)
        footer.addWidget(close_button)
        layout.addLayout(footer)

    @staticmethod
    def _menu_button(text: str) -> QPushButton:
        button = QPushButton(text)
        button.setMinimumHeight(42)
        button.setCursor(Qt.PointingHandCursor)
        button.setStyleSheet(STYLE_SECTOR8_BUTTON)
        return button


class OperBlockAnesthesiaTypesDialog(OperBlockStyledDialog):
    def __init__(self, items: list[dict], parent=None):
        self._working_items = [dict(item or {}) for item in (items or [])]
        super().__init__(
            "Виды пособия",
            "anesthesia_types_settings_geometry",
            parent,
            minimum_size=(520, 360),
            initial_size=(640, 460),
        )
        self._init_ui()
        self._render_table()
        self._finalize_dialog_chrome()

    def _init_ui(self):
        layout = self.content_layout
        layout.setSpacing(10)

        self.table = QTableWidget()
        self.table.setObjectName("OperBlockAnesthesiaTypesTable")
        self.table.setColumnCount(1)
        self.table.setHorizontalHeaderLabels(["Вид пособия"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setStyleSheet(
            """
            QTableWidget#OperBlockAnesthesiaTypesTable {
                background: #f3f6fa;
                alternate-background-color: #e9eef5;
                gridline-color: #cbd5e1;
                selection-background-color: #dbeafe;
                selection-color: #172033;
            }
            QTableWidget#OperBlockAnesthesiaTypesTable::item {
                padding: 5px 7px;
            }
            QHeaderView::section {
                background-color: #d9e2ec;
                color: #243b53;
                border: 1px solid #b8c4d3;
                padding: 5px 7px;
                font-weight: bold;
            }
            QHeaderView::section:hover {
                background-color: #cbd7e5;
            }
            """
        )
        self.table.itemSelectionChanged.connect(self._sync_input_from_selection)
        self.table.itemDoubleClicked.connect(lambda _item: self.type_input.setFocus())
        layout.addWidget(self.table, 1)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        self.type_input = QLineEdit()
        self.type_input.setPlaceholderText("Название вида пособия")
        form.addRow("Вид пособия:", self.type_input)
        layout.addLayout(form)

        edit_actions = QHBoxLayout()
        self.add_button = QPushButton("Добавить")
        self.update_button = QPushButton("Сохранить изменение")
        self.move_up_button = QPushButton("Выше")
        self.move_down_button = QPushButton("Ниже")
        self.delete_button = QPushButton("Удалить")
        for button in (self.add_button, self.update_button, self.move_up_button, self.move_down_button):
            button.setMinimumHeight(34)
            button.setStyleSheet(STYLE_SECTOR8_BUTTON)
        self.delete_button.setMinimumHeight(34)
        self.delete_button.setStyleSheet(DANGER_BUTTON_STYLE)
        self.add_button.clicked.connect(self._add_item)
        self.update_button.clicked.connect(self._update_selected_item)
        self.move_up_button.clicked.connect(lambda: self._move_selected_item(-1))
        self.move_down_button.clicked.connect(lambda: self._move_selected_item(1))
        self.delete_button.clicked.connect(self._delete_selected_item)
        edit_actions.addWidget(self.add_button)
        edit_actions.addWidget(self.update_button)
        edit_actions.addWidget(self.move_up_button)
        edit_actions.addWidget(self.move_down_button)
        edit_actions.addWidget(self.delete_button)
        layout.addLayout(edit_actions)

        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel_button = QPushButton("Отмена")
        cancel_button.setMinimumHeight(34)
        cancel_button.setStyleSheet(OPERBLOCK_DIALOG_CANCEL_BUTTON_STYLE)
        cancel_button.clicked.connect(self.reject)
        self.save_button = QPushButton("Сохранить")
        self.save_button.setMinimumHeight(34)
        self.save_button.setStyleSheet(OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE)
        self.save_button.clicked.connect(self.accept)
        self._configure_enter_accept_button(cancel_button, self.save_button)
        footer.addWidget(cancel_button)
        footer.addWidget(self.save_button)
        layout.addLayout(footer)

    def _render_table(self, select_id: str | None = None):
        selected_id = select_id or self._selected_item_id()
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(0)
            for row, item in enumerate(self._working_items):
                self.table.insertRow(row)
                table_item = QTableWidgetItem(str((item or {}).get("label") or ""))
                table_item.setData(Qt.UserRole, str((item or {}).get("id") or ""))
                self.table.setItem(row, 0, table_item)
            if self.table.rowCount():
                target_row = 0
                if selected_id:
                    for row in range(self.table.rowCount()):
                        item = self.table.item(row, 0)
                        if item and str(item.data(Qt.UserRole) or "") == selected_id:
                            target_row = row
                            break
                self.table.selectRow(target_row)
        finally:
            self.table.blockSignals(False)
        self._sync_input_from_selection()

    def _selected_row(self) -> int:
        indexes = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not indexes:
            return -1
        return int(indexes[0].row())

    def _selected_item_id(self) -> str:
        row = self._selected_row()
        if row < 0:
            return ""
        item = self.table.item(row, 0)
        return str(item.data(Qt.UserRole) or "") if item is not None else ""

    def _sync_input_from_selection(self):
        row = self._selected_row()
        if 0 <= row < len(self._working_items):
            self.type_input.setText(str(self._working_items[row].get("label") or ""))
        else:
            self.type_input.clear()
        self.update_button.setEnabled(row >= 0)
        self.delete_button.setEnabled(row >= 0)
        self.move_up_button.setEnabled(row > 0)
        self.move_down_button.setEnabled(0 <= row < len(self._working_items) - 1)

    def _input_label(self) -> str:
        return normalize_operblock_anesthesia_type_label(self.type_input.text())

    def _label_exists(self, label: str, *, ignore_row: int = -1) -> bool:
        key = label.casefold()
        for row, item in enumerate(self._working_items):
            if row == ignore_row:
                continue
            if str((item or {}).get("label") or "").casefold() == key:
                return True
        return False

    def _add_item(self):
        label = self._input_label()
        if not label:
            CustomMessageBox.warning(self, "Виды пособия", "Укажите вид пособия.")
            return
        if self._label_exists(label):
            CustomMessageBox.warning(self, "Виды пособия", "Такой вид пособия уже есть.")
            return
        item_id = f"manual_{int(time.time() * 1000)}"
        self._working_items.append({"id": item_id, "label": label, "sort_order": len(self._working_items) * 10 + 10})
        self._render_table(item_id)
        self.type_input.clear()

    def _update_selected_item(self):
        row = self._selected_row()
        if not (0 <= row < len(self._working_items)):
            return
        label = self._input_label()
        if not label:
            CustomMessageBox.warning(self, "Виды пособия", "Укажите вид пособия.")
            return
        if self._label_exists(label, ignore_row=row):
            CustomMessageBox.warning(self, "Виды пособия", "Такой вид пособия уже есть.")
            return
        self._working_items[row]["label"] = label
        self._render_table(str(self._working_items[row].get("id") or ""))

    def _delete_selected_item(self):
        row = self._selected_row()
        if not (0 <= row < len(self._working_items)):
            return
        self._working_items.pop(row)
        self._render_table()

    def _move_selected_item(self, direction: int):
        row = self._selected_row()
        target_row = row + int(direction)
        if not (0 <= row < len(self._working_items)) or not (0 <= target_row < len(self._working_items)):
            return
        self._working_items[row], self._working_items[target_row] = (
            self._working_items[target_row],
            self._working_items[row],
        )
        self._render_table(str(self._working_items[target_row].get("id") or ""))

    def items(self) -> list[dict]:
        result: list[dict] = []
        for index, item in enumerate(self._working_items, start=1):
            label = normalize_operblock_anesthesia_type_label((item or {}).get("label"))
            if not label:
                continue
            result.append(
                {
                    "id": str((item or {}).get("id") or ""),
                    "label": label,
                    "sort_order": index * 10,
                }
            )
        return result


class OperBlockTeamDialog(OperBlockStyledDialog):
    def __init__(self, items: list[dict], parent=None):
        self._working_items = [dict(item or {}) for item in (items or [])]
        super().__init__(
            "Опер. бригада",
            "team_settings_geometry",
            parent,
            minimum_size=(640, 420),
            initial_size=(760, 520),
        )
        self._init_ui()
        self._render_table()
        self._finalize_dialog_chrome()

    def _init_ui(self):
        layout = self.content_layout
        layout.setSpacing(10)

        self.table = QTableWidget()
        self.table.setObjectName("OperBlockTeamTable")
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["ФИО", "Должность"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setStyleSheet(
            """
            QTableWidget#OperBlockTeamTable {
                background: #f3f6fa;
                alternate-background-color: #e9eef5;
                gridline-color: #cbd5e1;
                selection-background-color: #dbeafe;
                selection-color: #172033;
            }
            QTableWidget#OperBlockTeamTable::item {
                padding: 5px 7px;
            }
            QHeaderView::section {
                background-color: #d9e2ec;
                color: #243b53;
                border: 1px solid #b8c4d3;
                padding: 5px 7px;
                font-weight: bold;
            }
            QHeaderView::section:hover {
                background-color: #cbd7e5;
            }
            """
        )
        self.table.itemSelectionChanged.connect(self._sync_inputs_from_selection)
        layout.addWidget(self.table, 1)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("ФИО")
        self.name_input.setMinimumWidth(360)
        self.position_combo = QComboBox()
        self.position_combo.setEditable(True)
        self.position_combo.setMinimumWidth(260)
        for position in OPERBLOCK_TEAM_DEFAULT_POSITIONS:
            self.position_combo.addItem(position, position)
        self.position_combo.setCurrentIndex(-1)
        self.position_combo.setEditText("")
        form.addRow("ФИО:", self.name_input)
        form.addRow("Должность:", self.position_combo)
        layout.addLayout(form)

        edit_actions = QHBoxLayout()
        self.add_button = QPushButton("Добавить")
        self.update_button = QPushButton("Сохранить изменение")
        self.delete_button = QPushButton("Удалить")
        for button in (self.add_button, self.update_button):
            button.setMinimumHeight(34)
            button.setStyleSheet(STYLE_SECTOR8_BUTTON)
        self.delete_button.setMinimumHeight(34)
        self.delete_button.setStyleSheet(DANGER_BUTTON_STYLE)
        self.add_button.clicked.connect(self._add_item)
        self.update_button.clicked.connect(self._update_selected_item)
        self.delete_button.clicked.connect(self._delete_selected_item)
        edit_actions.addWidget(self.add_button)
        edit_actions.addWidget(self.update_button)
        edit_actions.addWidget(self.delete_button)
        layout.addLayout(edit_actions)

        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel_button = QPushButton("Отмена")
        cancel_button.setMinimumHeight(34)
        cancel_button.setStyleSheet(OPERBLOCK_DIALOG_CANCEL_BUTTON_STYLE)
        cancel_button.clicked.connect(self.reject)
        self.save_button = QPushButton("Сохранить")
        self.save_button.setMinimumHeight(34)
        self.save_button.setStyleSheet(OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE)
        self.save_button.clicked.connect(self.accept)
        self._configure_enter_accept_button(cancel_button, self.save_button)
        footer.addWidget(cancel_button)
        footer.addWidget(self.save_button)
        layout.addLayout(footer)

    def _render_table(self, select_id: str | None = None):
        selected_id = select_id or self._selected_item_id()
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(0)
            for row, item in enumerate(self._working_items):
                self.table.insertRow(row)
                name_item = QTableWidgetItem(str((item or {}).get("name") or ""))
                name_item.setData(Qt.UserRole, str((item or {}).get("id") or ""))
                position_item = QTableWidgetItem(str((item or {}).get("position") or ""))
                self.table.setItem(row, 0, name_item)
                self.table.setItem(row, 1, position_item)
            if self.table.rowCount():
                target_row = 0
                if selected_id:
                    for row in range(self.table.rowCount()):
                        item = self.table.item(row, 0)
                        if item and str(item.data(Qt.UserRole) or "") == selected_id:
                            target_row = row
                            break
                self.table.selectRow(target_row)
        finally:
            self.table.blockSignals(False)
        self._sync_inputs_from_selection()

    def _selected_row(self) -> int:
        indexes = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not indexes:
            return -1
        return int(indexes[0].row())

    def _selected_item_id(self) -> str:
        row = self._selected_row()
        if row < 0:
            return ""
        item = self.table.item(row, 0)
        return str(item.data(Qt.UserRole) or "") if item is not None else ""

    def _sync_inputs_from_selection(self):
        row = self._selected_row()
        if 0 <= row < len(self._working_items):
            item = self._working_items[row]
            self.name_input.setText(str(item.get("name") or ""))
            self.position_combo.setEditText(str(item.get("position") or ""))
        else:
            self.name_input.clear()
            self.position_combo.setCurrentIndex(-1)
            self.position_combo.setEditText("")
        self.update_button.setEnabled(row >= 0)
        self.delete_button.setEnabled(row >= 0)

    def _input_name(self) -> str:
        return normalize_operblock_team_text(self.name_input.text())

    def _input_position(self) -> str:
        return normalize_operblock_team_text(self.position_combo.currentText())

    def _member_exists(self, name: str, position: str, *, ignore_row: int = -1) -> bool:
        key = (name.casefold(), position.casefold())
        for row, item in enumerate(self._working_items):
            if row == ignore_row:
                continue
            existing_key = (
                str((item or {}).get("name") or "").casefold(),
                str((item or {}).get("position") or "").casefold(),
            )
            if existing_key == key:
                return True
        return False

    def _validate_inputs(self, *, ignore_row: int = -1) -> tuple[str, str] | None:
        name = self._input_name()
        position = self._input_position()
        if not name:
            CustomMessageBox.warning(self, "Опер. бригада", "Укажите ФИО.")
            return None
        if not position:
            CustomMessageBox.warning(self, "Опер. бригада", "Укажите должность.")
            return None
        if self._member_exists(name, position, ignore_row=ignore_row):
            CustomMessageBox.warning(self, "Опер. бригада", "Такой сотрудник с этой должностью уже есть.")
            return None
        return name, position

    def _add_item(self):
        values = self._validate_inputs()
        if values is None:
            return
        name, position = values
        item_id = f"member_{int(time.time() * 1000)}"
        self._working_items.append(
            {
                "id": item_id,
                "name": name,
                "position": position,
                "sort_order": len(self._working_items) * 10 + 10,
            }
        )
        self._render_table(item_id)
        self.name_input.clear()
        self.position_combo.setCurrentIndex(-1)
        self.position_combo.setEditText("")

    def _update_selected_item(self):
        row = self._selected_row()
        if not (0 <= row < len(self._working_items)):
            return
        values = self._validate_inputs(ignore_row=row)
        if values is None:
            return
        name, position = values
        self._working_items[row]["name"] = name
        self._working_items[row]["position"] = position
        self._render_table(str(self._working_items[row].get("id") or ""))

    def _delete_selected_item(self):
        row = self._selected_row()
        if not (0 <= row < len(self._working_items)):
            return
        self._working_items.pop(row)
        self._render_table()

    def items(self) -> list[dict]:
        result: list[dict] = []
        for index, item in enumerate(self._working_items, start=1):
            name = normalize_operblock_team_text((item or {}).get("name"))
            position = normalize_operblock_team_text((item or {}).get("position"))
            if not name or not position:
                continue
            result.append(
                {
                    "id": str((item or {}).get("id") or ""),
                    "name": name,
                    "position": position,
                    "sort_order": index * 10,
                }
            )
        return result


class StartAnesthesiaDialog(OperBlockStyledDialog):
    def __init__(
        self,
        anesthesia_types: list[dict],
        anesthesiologists: list[str] | None = None,
        anesthetists: list[str] | None = None,
        parent=None,
    ):
        super().__init__(
            "Начать пособие",
            "start_anesthesia_dialog_geometry",
            parent,
            minimum_size=(560, 260),
            initial_size=(660, 310),
        )
        self._init_ui(anesthesia_types, anesthesiologists or [], anesthetists or [])
        self._finalize_dialog_chrome()

    def _init_ui(self, anesthesia_types: list[dict], anesthesiologists: list[str], anesthetists: list[str]):
        layout = self.content_layout
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        self.assistance_combo = QComboBox()
        self.assistance_combo.setEditable(True)
        self.assistance_combo.setMinimumWidth(430)
        self.assistance_combo.setMinimumContentsLength(38)
        self.assistance_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.assistance_combo.setStyleSheet(_operblock_combo_box_style())
        line_edit = self.assistance_combo.lineEdit()
        if line_edit is not None:
            line_edit.setPlaceholderText("Не выбрано")
        for item in anesthesia_types or []:
            label = normalize_operblock_anesthesia_type_label((item or {}).get("label"))
            if label:
                self.assistance_combo.addItem(label, label)
        self.assistance_combo.setCurrentIndex(-1)
        self.assistance_combo.setEditText("")
        form.addRow("Вид пособия:", self.assistance_combo)

        self.anesthesiologist_combo = self._staff_combo(anesthesiologists)
        form.addRow("Анестезиолог:", self.anesthesiologist_combo)

        self.anesthetist_combo = self._staff_combo(anesthetists)
        form.addRow("Анестезист:", self.anesthetist_combo)
        layout.addLayout(form)

        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel_button = QPushButton("Отмена")
        cancel_button.setMinimumHeight(34)
        cancel_button.setStyleSheet(OPERBLOCK_DIALOG_CANCEL_BUTTON_STYLE)
        cancel_button.clicked.connect(self.reject)
        self.start_button = QPushButton("Начать")
        self.start_button.setMinimumHeight(34)
        self.start_button.setStyleSheet(OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE)
        self.start_button.clicked.connect(self.accept)
        self._configure_enter_accept_button(cancel_button, self.start_button)
        footer.addWidget(cancel_button)
        footer.addWidget(self.start_button)
        layout.addStretch(1)
        layout.addLayout(footer)

    @staticmethod
    def _staff_combo(items: list[str]) -> QComboBox:
        combo = QComboBox()
        combo.setEditable(True)
        combo.setFixedHeight(36)
        combo.setMinimumWidth(430)
        combo.setMinimumContentsLength(38)
        combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        combo.setStyleSheet(_operblock_combo_box_style())
        line_edit = combo.lineEdit()
        if line_edit is not None:
            line_edit.setPlaceholderText("Не выбрано")
        seen: set[str] = set()
        for item in items or []:
            label = normalize_operblock_team_text(item)
            key = label.casefold()
            if label and key not in seen:
                seen.add(key)
                combo.addItem(label, label)
        combo.setCurrentIndex(-1)
        combo.setEditText("")
        return combo

    def selected_assistance_type(self) -> str:
        return normalize_operblock_anesthesia_type_label(self.assistance_combo.currentText())

    def selected_anesthesiologist(self) -> str:
        return normalize_operblock_team_text(self.anesthesiologist_combo.currentText())

    def selected_anesthetist(self) -> str:
        return normalize_operblock_team_text(self.anesthetist_combo.currentText())

    def accept(self) -> None:
        if not self.selected_assistance_type():
            CustomMessageBox.warning(self, "Начать пособие", "Укажите вид пособия.")
            return
        super().accept()


class StartSurgeryDialog(OperBlockStyledDialog):
    def __init__(
        self,
        surgeons: list[str] | None = None,
        operating_nurses: list[str] | None = None,
        parent=None,
    ):
        super().__init__(
            "Начать операцию",
            "start_surgery_dialog_geometry",
            parent,
            minimum_size=(560, 360),
            initial_size=(660, 430),
        )
        self._surgeon_options = list(surgeons or [])
        self._surgeon_combos: list[QComboBox] = []
        self._syncing_surgeon_fields = False
        self._init_ui(surgeons or [], operating_nurses or [])
        self._finalize_dialog_chrome()

    def _init_ui(self, surgeons: list[str], operating_nurses: list[str]):
        layout = self.content_layout
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        self.operation_name_edit = QLineEdit()
        self.operation_name_edit.setMinimumWidth(430)
        self.operation_name_edit.setPlaceholderText("Название операции")
        form.addRow("Название операции:", self.operation_name_edit)

        self.surgeons_scroll = QScrollArea()
        self.surgeons_scroll.setObjectName("OperBlockSurgeryTeamScroll")
        self.surgeons_scroll.setWidgetResizable(True)
        self.surgeons_scroll.setFrameShape(QFrame.NoFrame)
        self.surgeons_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.surgeons_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.surgeons_scroll.setMinimumHeight(190)
        self.surgeons_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        surgeons_scrollbar = self.surgeons_scroll.verticalScrollBar()
        surgeons_scrollbar.setObjectName("OperBlockSurgeryTeamScrollBar")
        surgeons_scrollbar.setFixedWidth(14)
        surgeons_scrollbar.setSingleStep(36)
        surgeons_scrollbar.setPageStep(108)
        surgeons_scrollbar.setStyleSheet(
            _operblock_vertical_scrollbar_style(
                "OperBlockSurgeryTeamScrollBar",
                width_px=14,
                left_margin_px=3,
                right_margin_px=2,
            )
        )

        self.surgeons_widget = QWidget()
        self.surgeons_widget.setObjectName("OperBlockSurgeryTeamContent")
        self.surgeons_widget.setStyleSheet(
            "QWidget#OperBlockSurgeryTeamContent { background: transparent; border: none; }"
        )
        self.surgeons_layout = QFormLayout(self.surgeons_widget)
        self.surgeons_layout.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        self.surgeons_layout.setContentsMargins(0, 0, 0, 0)
        self.surgeons_layout.setHorizontalSpacing(10)
        self.surgeons_layout.setVerticalSpacing(6)
        self._add_surgeon_combo()
        self.operating_nurse_combo = StartAnesthesiaDialog._staff_combo(operating_nurses)
        self._install_surgery_team_combo_event_filter(self.operating_nurse_combo)
        self.surgeons_layout.addRow("Операционная медсестра:", self.operating_nurse_combo)
        self.surgeons_scroll.setWidget(self.surgeons_widget)
        self.surgeons_scroll.setStyleSheet(
            """
            QScrollArea#OperBlockSurgeryTeamScroll {
                background: transparent;
                border: none;
            }
            QScrollArea#OperBlockSurgeryTeamScroll > QWidget > QWidget {
                background: transparent;
            }
            """
        )
        form.addRow(self.surgeons_scroll)
        layout.addLayout(form, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel_button = QPushButton("Отмена")
        cancel_button.setMinimumHeight(34)
        cancel_button.setStyleSheet(OPERBLOCK_DIALOG_CANCEL_BUTTON_STYLE)
        cancel_button.clicked.connect(self.reject)
        self.start_button = QPushButton("Начать")
        self.start_button.setMinimumHeight(34)
        self.start_button.setStyleSheet(OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE)
        self.start_button.clicked.connect(self.accept)
        self._configure_enter_accept_button(cancel_button, self.start_button)
        footer.addWidget(cancel_button)
        footer.addWidget(self.start_button)
        layout.addLayout(footer)

    def _add_surgeon_combo(self, text: str = "") -> QComboBox:
        combo = StartAnesthesiaDialog._staff_combo(self._surgeon_options)
        if text:
            combo.setEditText(text)
        combo.currentTextChanged.connect(lambda *_args: self._sync_surgeon_fields())
        self._install_surgery_team_combo_event_filter(combo)
        insert_row = self._operating_nurse_row()
        self._surgeon_combos.append(combo)
        self.surgeons_layout.insertRow(insert_row, "Хирург:" if len(self._surgeon_combos) == 1 else "", combo)
        self._refresh_surgeon_row_labels()
        self._scroll_surgeons_to_bottom_later()
        return combo

    def _install_surgery_team_combo_event_filter(self, combo: QComboBox) -> None:
        combo.installEventFilter(self)
        line_edit = combo.lineEdit()
        if line_edit is not None:
            line_edit.installEventFilter(self)

    def _operating_nurse_row(self) -> int:
        combo = getattr(self, "operating_nurse_combo", None)
        if combo is None:
            return self.surgeons_layout.rowCount()
        row, _role = self.surgeons_layout.getWidgetPosition(combo)
        if row < 0:
            return self.surgeons_layout.rowCount()
        return row

    def _refresh_surgeon_row_labels(self) -> None:
        for index, combo in enumerate(self._surgeon_combos):
            label = self.surgeons_layout.labelForField(combo)
            if isinstance(label, QLabel):
                label.setText("Хирург:" if index == 0 else "")

    def _scroll_surgeons_to_bottom_later(self) -> None:
        scroll = getattr(self, "surgeons_scroll", None)
        if scroll is None:
            return

        def scroll_to_bottom():
            try:
                bar = scroll.verticalScrollBar()
                bar.setValue(bar.maximum())
            except RuntimeError:
                return

        QTimer.singleShot(0, scroll_to_bottom)

    def _scroll_surgeon_combo_wheel(self, event) -> bool:
        scroll = getattr(self, "surgeons_scroll", None)
        if scroll is None:
            return False
        bar = scroll.verticalScrollBar()
        if bar.maximum() <= bar.minimum():
            return False
        pixel_delta = event.pixelDelta().y() if hasattr(event, "pixelDelta") else 0
        angle_delta = event.angleDelta().y() if hasattr(event, "angleDelta") else 0
        delta = pixel_delta or angle_delta
        if not delta:
            return False
        steps = max(1, int(round(abs(angle_delta) / 120))) if angle_delta else 1
        direction = -1 if delta > 0 else 1
        bar.setValue(max(bar.minimum(), min(bar.maximum(), bar.value() + direction * bar.singleStep() * steps)))
        event.accept()
        return True

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Wheel and self._is_surgery_team_wheel_widget(watched):
            if self._scroll_surgeon_combo_wheel(event):
                return True
        return super().eventFilter(watched, event)

    def _is_surgery_team_wheel_widget(self, watched) -> bool:
        team_combos = list(getattr(self, "_surgeon_combos", []))
        operating_nurse_combo = getattr(self, "operating_nurse_combo", None)
        if operating_nurse_combo is not None:
            team_combos.append(operating_nurse_combo)
        for combo in team_combos:
            if watched is combo:
                return True
            line_edit = combo.lineEdit()
            if line_edit is not None and watched is line_edit:
                return True
        return False

    def _sync_surgeon_fields(self) -> None:
        if self._syncing_surgeon_fields:
            return
        self._syncing_surgeon_fields = True
        try:
            if not self._surgeon_combos:
                self._add_surgeon_combo()

            if self._surgeon_combos and normalize_operblock_team_text(self._surgeon_combos[-1].currentText()):
                self._add_surgeon_combo()

            for combo in list(self._surgeon_combos[:-1]):
                if normalize_operblock_team_text(combo.currentText()):
                    continue
                self._surgeon_combos.remove(combo)
                self.surgeons_layout.removeRow(combo)

            if not self._surgeon_combos:
                self._add_surgeon_combo()
            self._refresh_surgeon_row_labels()
        finally:
            self._syncing_surgeon_fields = False

    def operation_name(self) -> str:
        return normalize_operblock_team_text(self.operation_name_edit.text())

    def selected_surgeons(self) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for combo in self._surgeon_combos:
            name = normalize_operblock_team_text(combo.currentText())
            key = name.casefold()
            if name and key not in seen:
                seen.add(key)
                result.append(name)
        return result

    def selected_surgeon(self) -> str:
        surgeons = self.selected_surgeons()
        return surgeons[0] if surgeons else ""

    def selected_operating_nurse(self) -> str:
        return normalize_operblock_team_text(self.operating_nurse_combo.currentText())

    def accept(self) -> None:
        if not self.operation_name():
            CustomMessageBox.warning(self, "Начать операцию", "Укажите название операции.")
            self.operation_name_edit.setFocus(Qt.OtherFocusReason)
            return
        super().accept()


class EditOperBlockStaffDialog(OperBlockStyledDialog):
    def __init__(
        self,
        *,
        surgeon_options: list[str] | None = None,
        operating_nurse_options: list[str] | None = None,
        anesthesiologist_options: list[str] | None = None,
        anesthetist_options: list[str] | None = None,
        current_surgeons: list[str] | None = None,
        current_operating_nurse: str = "",
        current_anesthesiologist: str = "",
        current_anesthetist: str = "",
        surgery_enabled: bool = True,
        anesthesia_enabled: bool = True,
        parent=None,
    ):
        super().__init__(
            "Изменить состав",
            "edit_staff_dialog_geometry",
            parent,
            minimum_size=(560, 380),
            initial_size=(660, 460),
        )
        self._surgeon_options = list(surgeon_options or [])
        self._surgeon_combos: list[QComboBox] = []
        self._syncing_surgeon_fields = False
        self._surgery_enabled = bool(surgery_enabled)
        self._anesthesia_enabled = bool(anesthesia_enabled)
        self._init_ui(
            operating_nurse_options or [],
            anesthesiologist_options or [],
            anesthetist_options or [],
            current_surgeons or [],
            current_operating_nurse,
            current_anesthesiologist,
            current_anesthetist,
        )
        self._finalize_dialog_chrome()

    def _init_ui(
        self,
        operating_nurse_options: list[str],
        anesthesiologist_options: list[str],
        anesthetist_options: list[str],
        current_surgeons: list[str],
        current_operating_nurse: str,
        current_anesthesiologist: str,
        current_anesthetist: str,
    ) -> None:
        layout = self.content_layout

        self.team_scroll = QScrollArea()
        self.team_scroll.setObjectName("OperBlockEditStaffScroll")
        self.team_scroll.setWidgetResizable(True)
        self.team_scroll.setFrameShape(QFrame.NoFrame)
        self.team_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.team_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.team_scroll.setMinimumHeight(250)
        self.team_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        scrollbar = self.team_scroll.verticalScrollBar()
        scrollbar.setObjectName("OperBlockEditStaffScrollBar")
        scrollbar.setFixedWidth(14)
        scrollbar.setSingleStep(36)
        scrollbar.setPageStep(108)
        scrollbar.setStyleSheet(
            _operblock_vertical_scrollbar_style(
                "OperBlockEditStaffScrollBar",
                width_px=14,
                left_margin_px=3,
                right_margin_px=2,
            )
        )

        self.team_widget = QWidget()
        self.team_widget.setObjectName("OperBlockEditStaffContent")
        self.team_widget.setStyleSheet("QWidget#OperBlockEditStaffContent { background: transparent; border: none; }")
        self.team_layout = QFormLayout(self.team_widget)
        self.team_layout.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        self.team_layout.setContentsMargins(0, 0, 0, 0)
        self.team_layout.setHorizontalSpacing(10)
        self.team_layout.setVerticalSpacing(8)

        self._syncing_surgeon_fields = True
        try:
            for surgeon in current_surgeons:
                self._add_surgeon_combo(surgeon)
            if not self._surgeon_combos:
                self._add_surgeon_combo()
            if self._surgeon_combos and normalize_operblock_team_text(self._surgeon_combos[-1].currentText()):
                self._add_surgeon_combo()
        finally:
            self._syncing_surgeon_fields = False

        self.operating_nurse_combo = StartAnesthesiaDialog._staff_combo(operating_nurse_options)
        self.operating_nurse_combo.setEditText(normalize_operblock_team_text(current_operating_nurse))
        self._install_team_combo_event_filter(self.operating_nurse_combo)
        self.team_layout.addRow("Операционная медсестра:", self.operating_nurse_combo)

        self.anesthesiologist_combo = StartAnesthesiaDialog._staff_combo(anesthesiologist_options)
        self.anesthesiologist_combo.setEditText(normalize_operblock_team_text(current_anesthesiologist))
        self._install_team_combo_event_filter(self.anesthesiologist_combo)
        self.team_layout.addRow("Анестезиолог:", self.anesthesiologist_combo)

        self.anesthetist_combo = StartAnesthesiaDialog._staff_combo(anesthetist_options)
        self.anesthetist_combo.setEditText(normalize_operblock_team_text(current_anesthetist))
        self._install_team_combo_event_filter(self.anesthetist_combo)
        self.team_layout.addRow("Анестезист:", self.anesthetist_combo)

        for combo in self._surgeon_combos + [self.operating_nurse_combo]:
            combo.setEnabled(self._surgery_enabled)
        for combo in (self.anesthesiologist_combo, self.anesthetist_combo):
            combo.setEnabled(self._anesthesia_enabled)

        self.team_scroll.setWidget(self.team_widget)
        self.team_scroll.setStyleSheet(
            """
            QScrollArea#OperBlockEditStaffScroll {
                background: transparent;
                border: none;
            }
            QScrollArea#OperBlockEditStaffScroll > QWidget > QWidget {
                background: transparent;
            }
            """
        )
        layout.addWidget(self.team_scroll, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel_button = QPushButton("Отмена")
        cancel_button.setMinimumHeight(34)
        cancel_button.setStyleSheet(OPERBLOCK_DIALOG_CANCEL_BUTTON_STYLE)
        cancel_button.clicked.connect(self.reject)
        self.save_button = QPushButton("Сохранить")
        self.save_button.setMinimumHeight(34)
        self.save_button.setStyleSheet(OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE)
        self.save_button.clicked.connect(self.accept)
        self._configure_enter_accept_button(cancel_button, self.save_button)
        footer.addWidget(cancel_button)
        footer.addWidget(self.save_button)
        layout.addLayout(footer)
        self._refresh_surgeon_row_labels()

    def _add_surgeon_combo(self, text: str = "") -> QComboBox:
        combo = StartAnesthesiaDialog._staff_combo(self._surgeon_options)
        combo.setEnabled(self._surgery_enabled)
        clean_text = normalize_operblock_team_text(text)
        if clean_text:
            combo.setEditText(clean_text)
        combo.currentTextChanged.connect(lambda *_args: self._sync_surgeon_fields())
        self._install_team_combo_event_filter(combo)
        insert_row = self._first_static_staff_row()
        self._surgeon_combos.append(combo)
        self.team_layout.insertRow(insert_row, "Хирург:" if len(self._surgeon_combos) == 1 else "", combo)
        self._refresh_surgeon_row_labels()
        self._scroll_team_to_bottom_later()
        return combo

    def _install_team_combo_event_filter(self, combo: QComboBox) -> None:
        combo.installEventFilter(self)
        line_edit = combo.lineEdit()
        if line_edit is not None:
            line_edit.installEventFilter(self)

    def _first_static_staff_row(self) -> int:
        for attr in ("operating_nurse_combo", "anesthesiologist_combo", "anesthetist_combo"):
            combo = getattr(self, attr, None)
            if combo is None:
                continue
            row, _role = self.team_layout.getWidgetPosition(combo)
            if row >= 0:
                return row
        return self.team_layout.rowCount()

    def _refresh_surgeon_row_labels(self) -> None:
        for index, combo in enumerate(self._surgeon_combos):
            label = self.team_layout.labelForField(combo)
            if isinstance(label, QLabel):
                label.setText("Хирург:" if index == 0 else "")

    def _scroll_team_to_bottom_later(self) -> None:
        scroll = getattr(self, "team_scroll", None)
        if scroll is None:
            return

        def scroll_to_bottom():
            try:
                bar = scroll.verticalScrollBar()
                bar.setValue(bar.maximum())
            except RuntimeError:
                return

        QTimer.singleShot(0, scroll_to_bottom)

    def _scroll_team_wheel(self, event) -> bool:
        scroll = getattr(self, "team_scroll", None)
        if scroll is None:
            return False
        bar = scroll.verticalScrollBar()
        if bar.maximum() <= bar.minimum():
            return False
        pixel_delta = event.pixelDelta().y() if hasattr(event, "pixelDelta") else 0
        angle_delta = event.angleDelta().y() if hasattr(event, "angleDelta") else 0
        delta = pixel_delta or angle_delta
        if not delta:
            return False
        steps = max(1, int(round(abs(angle_delta) / 120))) if angle_delta else 1
        direction = -1 if delta > 0 else 1
        bar.setValue(max(bar.minimum(), min(bar.maximum(), bar.value() + direction * bar.singleStep() * steps)))
        event.accept()
        return True

    def _is_team_wheel_widget(self, watched) -> bool:
        combos = list(getattr(self, "_surgeon_combos", [])) + [
            self.operating_nurse_combo,
            self.anesthesiologist_combo,
            self.anesthetist_combo,
        ]
        for combo in combos:
            if watched is combo:
                return True
            line_edit = combo.lineEdit()
            if line_edit is not None and watched is line_edit:
                return True
        return False

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Wheel and self._is_team_wheel_widget(watched):
            if self._scroll_team_wheel(event):
                return True
        return super().eventFilter(watched, event)

    def _sync_surgeon_fields(self) -> None:
        if self._syncing_surgeon_fields or not self._surgery_enabled:
            return
        self._syncing_surgeon_fields = True
        try:
            if not self._surgeon_combos:
                self._add_surgeon_combo()

            if self._surgeon_combos and normalize_operblock_team_text(self._surgeon_combos[-1].currentText()):
                self._add_surgeon_combo()

            for combo in list(self._surgeon_combos[:-1]):
                if normalize_operblock_team_text(combo.currentText()):
                    continue
                self._surgeon_combos.remove(combo)
                self.team_layout.removeRow(combo)

            if not self._surgeon_combos:
                self._add_surgeon_combo()
            self._refresh_surgeon_row_labels()
        finally:
            self._syncing_surgeon_fields = False

    def selected_surgeons(self) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for combo in self._surgeon_combos:
            name = normalize_operblock_team_text(combo.currentText())
            key = name.casefold()
            if name and key not in seen:
                seen.add(key)
                result.append(name)
        return result

    def selected_operating_nurse(self) -> str:
        return normalize_operblock_team_text(self.operating_nurse_combo.currentText())

    def selected_anesthesiologist(self) -> str:
        return normalize_operblock_team_text(self.anesthesiologist_combo.currentText())

    def selected_anesthetist(self) -> str:
        return normalize_operblock_team_text(self.anesthetist_combo.currentText())


class QuickOrdersSettingsDialog(OperBlockStyledDialog):
    def __init__(self, templates: list[dict], parent=None):
        self._rows: list[dict] = []
        self._templates: list[dict] = []
        super().__init__(
            "Быстрые назначения оперблока",
            "quick_orders_settings_geometry",
            parent,
            minimum_size=(900, 420),
            initial_size=(980, 520),
        )
        self._init_ui()
        for template in templates or []:
            self._add_row(template)
        if not self._rows:
            self._add_row()
        self._finalize_dialog_chrome()

    def _init_ui(self):
        layout = self.content_layout

        hint = QLabel(
            "Укажите препарат, группу 1-4 для колонки назначений и до четырех быстрых дозировок. "
            "Пустые дозировки в секторе не показываются."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")
        layout.addWidget(hint)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.setStyleSheet(STYLE_PATIENT_FORM_SCROLL)
        self.rows_host = QWidget()
        self.rows_host.setStyleSheet(STYLE_PATIENT_FORM_PAGE)
        self.rows_layout = QVBoxLayout(self.rows_host)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(6)
        self.rows_layout.addStretch(1)
        self.scroll.setWidget(self.rows_host)
        layout.addWidget(self.scroll, 1)

        actions = QHBoxLayout()
        self.add_row_button = QPushButton("Добавить препарат")
        self.add_row_button.setMinimumHeight(34)
        self.add_row_button.setStyleSheet(STYLE_SECTOR8_BUTTON)
        self.add_row_button.clicked.connect(lambda: self._add_row())
        self.cancel_button = QPushButton("Отменить")
        self.cancel_button.setMinimumHeight(34)
        self.cancel_button.setStyleSheet(OPERBLOCK_DIALOG_CANCEL_BUTTON_STYLE)
        self.cancel_button.clicked.connect(self.reject)
        self.save_button = QPushButton("Сохранить")
        self.save_button.setMinimumHeight(34)
        self.save_button.setStyleSheet(OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE)
        self.save_button.clicked.connect(self.accept)
        actions.addWidget(self.add_row_button)
        actions.addStretch(1)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.save_button)
        layout.addLayout(actions)

    def _add_row(self, template: dict | None = None):
        template = template or {}
        row = QFrame()
        row.setObjectName("quickOrderSettingsRow")
        row.setStyleSheet(
            f"""
            QFrame#quickOrderSettingsRow {{
                background-color: {BG_LIGHT};
                border: 1px solid {BORDER_LIGHT};
                border-radius: {CUSTOM_DIALOG_RADIUS};
            }}
            QLineEdit, QComboBox {{
                padding: 8px;
                border: 1px solid {BORDER_LIGHT};
                border-radius: {CUSTOM_DIALOG_RADIUS};
                background: {BG_CARD};
                color: {TEXT_PRIMARY};
            }}
            QLineEdit:focus, QComboBox:focus {{
                border: 1px solid {BORDER_COLOR};
                background: {BG_CARD};
            }}
            """
        )
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(8, 6, 8, 6)
        row_layout.setSpacing(6)

        drug_edit = QLineEdit()
        drug_edit.setPlaceholderText("Препарат")
        drug_edit.setText(str(template.get("drug_name") or ""))
        drug_edit.setMinimumHeight(32)
        row_layout.addWidget(drug_edit, 2)

        group_combo = QComboBox()
        group_combo.setMinimumHeight(32)
        group_combo.setFixedWidth(100)
        for group_number in range(1, 5):
            group_combo.addItem(f"Группа {group_number}", group_number)
        group_combo.setCurrentIndex(normalize_operblock_quick_order_group(template.get("group")) - 1)
        row_layout.addWidget(group_combo, 0)

        dose_edits: list[QLineEdit] = []
        doses = list(template.get("doses") or [])
        for index in range(4):
            edit = QLineEdit()
            edit.setPlaceholderText(f"Дозировка {index + 1}")
            edit.setText(str(doses[index]) if index < len(doses) else "")
            edit.setMinimumHeight(32)
            edit.setFixedWidth(105)
            dose_edits.append(edit)
            row_layout.addWidget(edit, 0)

        remove_button = QPushButton("Удалить")
        remove_button.setMinimumHeight(32)
        remove_button.setStyleSheet(DANGER_BUTTON_STYLE)
        remove_button.clicked.connect(lambda _=False, widget=row: self._remove_row(widget))
        row_layout.addWidget(remove_button, 0)

        extra = {
            key: value
            for key, value in dict(template).items()
            if key not in {"drug_name", "drug", "label", "group", "doses"}
        }
        record = {"widget": row, "drug": drug_edit, "group": group_combo, "doses": dose_edits, "extra": extra}
        self._rows.append(record)
        self.rows_layout.insertWidget(max(0, self.rows_layout.count() - 1), row)

    def _remove_row(self, widget: QWidget):
        self._rows = [row for row in self._rows if row.get("widget") is not widget]
        widget.setParent(None)
        widget.deleteLater()
        if not self._rows:
            self._add_row()

    def templates(self) -> list[dict]:
        return list(self._templates)

    def accept(self):
        templates: list[dict] = []
        seen: set[str] = set()
        for row in self._rows:
            drug_name = row["drug"].text().strip()
            doses = [edit.text().strip() for edit in row["doses"] if edit.text().strip()]
            if not drug_name and not doses:
                continue
            if not drug_name:
                CustomMessageBox.warning(self, "Ошибка", "Укажите название препарата.")
                return
            key = drug_name.casefold()
            if key in seen:
                CustomMessageBox.warning(self, "Ошибка", f"Препарат '{drug_name}' указан дважды.")
                return
            seen.add(key)
            item = dict(row.get("extra") or {})
            item.update(
                {
                    "drug_name": drug_name,
                    "label": str(item.get("label") or drug_name).strip() or drug_name,
                    "group": normalize_operblock_quick_order_group(row["group"].currentData()),
                    "kind": normalize_operblock_quick_order_kind(item.get("kind")),
                    "doses": doses[:4],
                }
            )
            templates.append(item)
        self._templates = templates
        super().accept()


def _operblock_table_editor_style() -> str:
    return f"""
        QLineEdit {{
            padding: 1px 4px;
            border: 1px solid {BORDER_COLOR};
            border-radius: 3px;
            background: {BG_CARD};
            color: {TEXT_PRIMARY};
            selection-background-color: #DCEBFF;
            selection-color: {TEXT_PRIMARY};
        }}
        QLineEdit:focus {{
            border: 1px solid {OPERBLOCK_ORDERS_ACCENT};
            background: {BG_CARD};
            color: {TEXT_PRIMARY};
        }}
    """


class _OperBlockTableTextDelegate(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        editor = super().createEditor(parent, option, index)
        if isinstance(editor, QLineEdit):
            editor.setStyleSheet(_operblock_table_editor_style())
            editor.setTextMargins(2, 0, 2, 0)
            editor.setMinimumHeight(20)
        return editor

    def updateEditorGeometry(self, editor, option, index):
        if isinstance(editor, QLineEdit):
            editor.setGeometry(option.rect.adjusted(1, 2, -1, -2))
            return
        super().updateEditorGeometry(editor, option, index)


class _OperBlockComboBoxDelegate(QStyledItemDelegate):
    def __init__(self, options_provider, parent=None):
        super().__init__(parent)
        self._options_provider = options_provider

    def createEditor(self, parent, _option, _index):
        combo = QComboBox(parent)
        combo.setEditable(False)
        combo.setStyleSheet(_operblock_combo_box_style())
        combo.addItems(self._option_labels())
        QTimer.singleShot(0, combo.showPopup)
        return combo

    def setEditorData(self, editor, index):
        if not isinstance(editor, QComboBox):
            return super().setEditorData(editor, index)
        current_text = str(index.data(Qt.ItemDataRole.EditRole) or index.data(Qt.ItemDataRole.DisplayRole) or "")
        match_index = -1
        for option_index in range(editor.count()):
            if editor.itemText(option_index).casefold() == current_text.casefold():
                match_index = option_index
                break
        if match_index < 0 and current_text:
            editor.addItem(current_text)
            match_index = editor.count() - 1
        editor.setCurrentIndex(max(0, match_index))

    def setModelData(self, editor, model, index):
        if not isinstance(editor, QComboBox):
            return super().setModelData(editor, model, index)
        model.setData(index, editor.currentText(), Qt.ItemDataRole.EditRole)

    def updateEditorGeometry(self, editor, option, _index):
        editor.setGeometry(option.rect)

    def _option_labels(self) -> list[str]:
        try:
            raw_options = self._options_provider()
        except Exception:
            raw_options = []
        labels: list[str] = []
        for option in raw_options or []:
            if isinstance(option, tuple):
                label = str(option[0] or "").strip()
            else:
                label = str(option or "").strip()
            if label and label not in labels:
                labels.append(label)
        return labels


class _QuickOrderPresetCard(QFrame):
    def __init__(self, preset_id: str, owner, parent=None):
        super().__init__(parent)
        self._preset_id = str(preset_id or "")
        self._owner = owner
        self._drag_start_global_pos = None
        self.setAcceptDrops(True)
        self.setCursor(Qt.OpenHandCursor)

    @staticmethod
    def _event_global_pos(event):
        try:
            return event.globalPosition().toPoint()
        except Exception:
            try:
                return event.globalPos()
            except Exception:
                return None

    @staticmethod
    def _is_drag_source_widget(widget) -> bool:
        while widget is not None:
            if isinstance(widget, QPushButton):
                return False
            widget = widget.parentWidget() if hasattr(widget, "parentWidget") else None
        return True

    def bind_drag_sources(self) -> None:
        for child in self.findChildren(QWidget):
            if child is self or isinstance(child, QPushButton):
                continue
            child.installEventFilter(self)

    def eventFilter(self, source, event):
        if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            self._remember_drag_start(event, source)
        elif event.type() == QEvent.MouseMove:
            if self._maybe_start_drag(event, source):
                return True
        elif event.type() in (QEvent.MouseButtonRelease, QEvent.Leave):
            self._drag_start_global_pos = None
        return super().eventFilter(source, event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._remember_drag_start(event, self)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._maybe_start_drag(event, self):
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_start_global_pos = None
        super().mouseReleaseEvent(event)

    def _remember_drag_start(self, event, source) -> None:
        if not self._preset_id or not self._is_drag_source_widget(source):
            self._drag_start_global_pos = None
            return
        self._drag_start_global_pos = self._event_global_pos(event)

    def _maybe_start_drag(self, event, source) -> bool:
        if not self._preset_id or not self._is_drag_source_widget(source) or self._drag_start_global_pos is None:
            return False
        current_pos = self._event_global_pos(event)
        if current_pos is None:
            return False
        distance = (current_pos - self._drag_start_global_pos).manhattanLength()
        if distance < QApplication.startDragDistance():
            return False
        self._drag_start_global_pos = None
        self._start_drag()
        return True

    def _drag_preset_id(self, event) -> str:
        mime = event.mimeData()
        if not mime or not mime.hasFormat(OPERBLOCK_QUICK_ORDER_DRAG_MIME):
            return ""
        try:
            return bytes(mime.data(OPERBLOCK_QUICK_ORDER_DRAG_MIME)).decode("utf-8")
        except Exception:
            return ""

    def _start_drag(self) -> None:
        pixmap = self.grab()
        if not self._owner._begin_quick_order_drag(self._preset_id):
            return
        mime = QMimeData()
        mime.setData(OPERBLOCK_QUICK_ORDER_DRAG_MIME, self._preset_id.encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        if not pixmap.isNull():
            drag.setPixmap(pixmap)
            drag.setHotSpot(pixmap.rect().center())
        self.setCursor(Qt.ClosedHandCursor)
        try:
            action = drag.exec(Qt.MoveAction)
        finally:
            self.setCursor(Qt.OpenHandCursor)
        if action != Qt.MoveAction or not self._owner._quick_order_drag_committed:
            self._owner._cancel_quick_order_drag()

    def dragEnterEvent(self, event):
        source_id = self._drag_preset_id(event)
        if source_id and source_id != self._preset_id:
            self._owner._preview_quick_order_drag(source_id, self._preset_id, after=False)
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event):
        source_id = self._drag_preset_id(event)
        if source_id and source_id != self._preset_id:
            try:
                local_y = float(event.position().y())
            except Exception:
                local_y = float(event.pos().y())
            self._owner._preview_quick_order_drag(source_id, self._preset_id, after=local_y > self.height() / 2)
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event):
        source_id = self._drag_preset_id(event)
        if source_id and source_id != self._preset_id:
            try:
                local_y = float(event.position().y())
            except Exception:
                local_y = float(event.pos().y())
            self._owner._preview_quick_order_drag(source_id, self._preset_id, after=local_y > self.height() / 2)
            if self._owner._commit_quick_order_drag():
                event.acceptProposedAction()
                return
        event.ignore()


class _QuickOrderPresetListWidget(QWidget):
    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self._owner = owner
        self.setAcceptDrops(True)

    @staticmethod
    def _drag_preset_id(event) -> str:
        mime = event.mimeData()
        if not mime or not mime.hasFormat(OPERBLOCK_QUICK_ORDER_DRAG_MIME):
            return ""
        try:
            return bytes(mime.data(OPERBLOCK_QUICK_ORDER_DRAG_MIME)).decode("utf-8")
        except Exception:
            return ""

    @staticmethod
    def _event_y(event) -> float:
        try:
            return float(event.position().y())
        except Exception:
            return float(event.pos().y())

    def dragEnterEvent(self, event):
        source_id = self._drag_preset_id(event)
        if source_id:
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event):
        source_id = self._drag_preset_id(event)
        if source_id and self._owner._preview_quick_order_drag_at_y(source_id, self._event_y(event)):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event):
        source_id = self._drag_preset_id(event)
        if source_id:
            self._owner._preview_quick_order_drag_at_y(source_id, self._event_y(event))
            if self._owner._commit_quick_order_drag():
                event.acceptProposedAction()
                return
        event.ignore()


class OperBlockMedicationPresetsDialog(OperBlockStyledDialog):
    COL_ENABLED = 0
    COL_LABEL = 1
    COL_DISPLAY = 2
    COL_KIND = 3
    COL_GROUP = 4
    COL_DOSES = 5
    COL_RATES = 6
    COL_CONCENTRATION = 7
    COL_SOLVENT = 8
    COL_VOLUME = 9
    COL_DURATION = 10
    COL_FAVORITE = 11
    TABLE_HEADERS = (
        "Вкл",
        "Истинное название",
        "Отображаемое",
        "Тип",
        "Группа",
        "Дозы",
        "Скорости",
        "Конц.",
        "Растворитель",
        "мл",
        "мин",
        "Избр.",
    )

    def __init__(self, presets: list[dict], parent=None):
        self._table_header_settings_key = "operblock/medication_presets_settings_table_header_v2"
        self._restoring_table_header = False
        self._fitting_table_header = False
        self._working_templates: list[dict | None] = [dict(preset or {}) for preset in (presets or [])]
        self._visible_template_indexes: list[int] = []
        self._templates: list[dict] = []
        self._table_rendering = True
        self._favorite_icon_cache: dict[bool, QIcon] = {}
        self._diluent_options = self._load_diluent_options()
        self._group_options = self._build_group_options([item for item in self._working_templates if item is not None])
        minimum_width = self._bounded_dialog_width(parent, preferred=640, min_width=420)
        initial_width = self._bounded_dialog_width(parent, preferred=1180, min_width=minimum_width)
        super().__init__(
            "Настроить препараты",
            "medication_presets_settings_geometry",
            parent,
            minimum_size=(minimum_width, 520),
            initial_size=(initial_width, 680),
        )
        self._save_table_header_timer = QTimer(self)
        self._save_table_header_timer.setSingleShot(True)
        self._save_table_header_timer.timeout.connect(self._save_table_header_state)
        self._init_ui()
        self._table_rendering = False
        self._render_table()
        self._finalize_dialog_chrome()
        QTimer.singleShot(0, self._fit_table_columns_to_viewport)

    @staticmethod
    def _bounded_dialog_width(parent, *, preferred: int, min_width: int) -> int:
        width = int(preferred)
        parent_width = 0
        try:
            parent_width = int(parent.width()) if parent is not None else 0
        except Exception:
            parent_width = 0
        if parent_width > 0:
            available_width = max(360, parent_width - 32)
            width = min(width, available_width)
        return max(int(min_width), width)

    def _load_diluent_options(self) -> list[dict]:
        try:
            return load_operblock_diluent_options()
        except Exception:
            logger.exception("Не удалось загрузить растворители для настроек препаратов оперблока")
            return []

    @staticmethod
    def _build_group_options(presets: list[dict]) -> list[str]:
        groups: list[str] = []

        def add_group(value) -> None:
            text = OperBlockMedicationPresetsDialog._group_display_text(value)
            if text and text not in groups:
                groups.append(text)

        for group in OPERBLOCK_PRESET_GROUP_OPTIONS:
            add_group(group)
        for preset in presets or []:
            add_group((preset or {}).get("group"))
        return groups

    @staticmethod
    def _group_display_text(value) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip())
        folded = text.casefold()
        if folded in {"газ", "газы"}:
            return "Газы"
        if folded in {"инфузии / перфузор", "инфузии", "перфузор", "перфузоры"}:
            return "Дозатор"
        return text

    @staticmethod
    def _is_seed_preset(preset: dict) -> bool:
        payload = preset.get("payload") if isinstance(preset, dict) else {}
        return bool(
            preset.get("source_drug_id")
            or str(preset.get("preset_id") or "").startswith("drug:")
            or (isinstance(payload, dict) and payload.get("source") == "drugs.seed.json")
            or (isinstance(payload, dict) and payload.get("opblock_seed"))
        )

    def _init_ui(self):
        layout = self.content_layout
        self.content_widget.setStyleSheet(f"{STYLE_PATIENT_FORM_TAB}\n{_operblock_combo_box_style()}")

        hint = QLabel(
            "Таблица редактирует только шаблоны оперблока. Основной справочник doctor/nurse не меняется."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")
        layout.addWidget(hint)

        filters = QHBoxLayout()
        filters.setContentsMargins(0, 0, 0, 0)
        filters.setSpacing(8)
        self.filter_input = _line_edit()
        self.filter_input.setPlaceholderText("Фильтр")
        self.filter_input.textChanged.connect(self._apply_filter)
        self.kind_filter = QComboBox()
        self.kind_filter.setFixedHeight(34)
        self.kind_filter.addItem("Все типы", "")
        for kind, title in OPERBLOCK_PRESET_KIND_TITLES.items():
            self.kind_filter.addItem(title, kind)
        self.kind_filter.currentIndexChanged.connect(self._apply_filter)
        self.enabled_filter = QComboBox()
        self.enabled_filter.setFixedHeight(34)
        self.enabled_filter.addItem("Все", "")
        self.enabled_filter.addItem("Включенные", "enabled")
        self.enabled_filter.addItem("Скрытые", "disabled")
        self.enabled_filter.setCurrentIndex(1)
        self.enabled_filter.currentIndexChanged.connect(self._apply_filter)
        import_button = QPushButton("Импорт из справочника")
        import_button.setFixedHeight(34)
        import_button.setStyleSheet(STYLE_SECTOR8_BUTTON)
        import_button.clicked.connect(self._show_disabled_presets)
        filters.addWidget(self.filter_input, 2)
        filters.addWidget(self.kind_filter, 1)
        filters.addWidget(self.enabled_filter, 1)
        filters.addWidget(import_button, 0)
        layout.addLayout(filters)

        self.table = QTableWidget()
        self.table.setObjectName("medicationPresetSettingsTable")
        self.table.setColumnCount(len(self.TABLE_HEADERS))
        self.table.setHorizontalHeaderLabels(self.TABLE_HEADERS)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.table.setEditTriggers(
            QTableWidget.DoubleClicked | QTableWidget.EditKeyPressed | QTableWidget.AnyKeyPressed
        )
        self.table.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.table.setMinimumWidth(0)
        self.table.setSizeAdjustPolicy(QAbstractScrollArea.AdjustIgnored)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setItemDelegate(_OperBlockTableTextDelegate(self.table))
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionsMovable(True)
        if hasattr(header, "setFirstSectionMovable"):
            header.setFirstSectionMovable(True)
        header.setMinimumSectionSize(24)
        for column in range(len(self.TABLE_HEADERS)):
            header.setSectionResizeMode(column, QHeaderView.Interactive)
        self._apply_default_table_column_widths()
        self._restore_table_header_state()
        self._apply_table_column_visibility()
        header.sectionResized.connect(self._on_table_header_changed)
        header.sectionMoved.connect(self._on_table_header_changed)
        self.table.setStyleSheet(
            f"""
            QTableWidget#medicationPresetSettingsTable {{
                background-color: {BG_CARD};
                alternate-background-color: #F8FAFC;
                gridline-color: {BORDER_LIGHT};
                border: 1px solid {BORDER_LIGHT};
                border-radius: {CUSTOM_DIALOG_RADIUS};
                color: {TEXT_PRIMARY};
            }}
            QTableWidget#medicationPresetSettingsTable::item {{
                padding: 3px 5px;
                border: none;
            }}
            QTableWidget#medicationPresetSettingsTable::item:selected {{
                background-color: #EAF2FF;
                color: {TEXT_PRIMARY};
            }}
            QHeaderView::section {{
                background-color: {BG_LIGHT};
                color: {TEXT_PRIMARY};
                border: none;
                border-right: 1px solid {BORDER_LIGHT};
                border-bottom: 1px solid {BORDER_LIGHT};
                padding: 5px 6px;
                font-weight: 700;
            }}
            """
        )
        self.table.setItemDelegateForColumn(
            self.COL_KIND,
            _OperBlockComboBoxDelegate(self._kind_combo_options, self.table),
        )
        self.table.setItemDelegateForColumn(
            self.COL_GROUP,
            _OperBlockComboBoxDelegate(self._group_combo_options, self.table),
        )
        self.table.setItemDelegateForColumn(
            self.COL_SOLVENT,
            _OperBlockComboBoxDelegate(self._solvent_combo_options, self.table),
        )
        self.table.itemChanged.connect(self._on_table_item_changed)
        self.table.cellClicked.connect(self._open_combo_cell_editor)
        layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.add_row_button = QPushButton("Добавить препарат")
        self.add_row_button.setMinimumHeight(34)
        self.add_row_button.setStyleSheet(STYLE_SECTOR8_BUTTON)
        self.add_row_button.clicked.connect(lambda: self._add_row({"enabled": True, "kind": "bolus"}))
        self.toggle_visible_button = QPushButton("Вкл/Выкл все")
        self.toggle_visible_button.setMinimumHeight(34)
        self.toggle_visible_button.setStyleSheet(STYLE_SECTOR8_BUTTON)
        self.toggle_visible_button.clicked.connect(self._toggle_visible_enabled)
        self.delete_selected_button = QPushButton("Удалить выбранные")
        self.delete_selected_button.setMinimumHeight(34)
        self.delete_selected_button.setStyleSheet(DANGER_BUTTON_STYLE)
        self.delete_selected_button.clicked.connect(self._delete_selected_rows)
        self.cancel_button = QPushButton("Отменить")
        self.cancel_button.setMinimumHeight(34)
        self.cancel_button.setStyleSheet(OPERBLOCK_DIALOG_CANCEL_BUTTON_STYLE)
        self.cancel_button.clicked.connect(self.reject)
        self.save_button = QPushButton("Сохранить")
        self.save_button.setMinimumHeight(34)
        self.save_button.setStyleSheet(OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE)
        self.save_button.clicked.connect(self.accept)
        actions.addWidget(self.add_row_button)
        actions.addWidget(self.toggle_visible_button)
        actions.addWidget(self.delete_selected_button)
        actions.addStretch(1)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.save_button)
        layout.addLayout(actions)

    def _apply_default_table_column_widths(self):
        defaults = {
            self.COL_ENABLED: 48,
            self.COL_LABEL: 210,
            self.COL_DISPLAY: 180,
            self.COL_KIND: 138,
            self.COL_GROUP: 1,
            self.COL_DOSES: 170,
            self.COL_RATES: 170,
            self.COL_CONCENTRATION: 82,
            self.COL_SOLVENT: 130,
            self.COL_VOLUME: 62,
            self.COL_DURATION: 62,
            self.COL_FAVORITE: 58,
        }
        header = self.table.horizontalHeader()
        for column in range(len(self.TABLE_HEADERS)):
            header.resizeSection(column, int(defaults.get(column, 110)))

    def _apply_table_column_visibility(self) -> None:
        if not hasattr(self, "table"):
            return
        self.table.setColumnHidden(self.COL_GROUP, True)
        self.table.horizontalHeader().resizeSection(self.COL_FAVORITE, 58)

    def _restore_table_header_state(self):
        value = self._settings().value(self._table_header_settings_key)
        if value is None:
            return
        header = self.table.horizontalHeader()
        self._restoring_table_header = True
        try:
            header.restoreState(value)
        except Exception:
            return
        finally:
            self._restoring_table_header = False
        header.setStretchLastSection(False)
        header.setSectionsMovable(True)
        if hasattr(header, "setFirstSectionMovable"):
            header.setFirstSectionMovable(True)
        for column in range(len(self.TABLE_HEADERS)):
            header.setSectionResizeMode(column, QHeaderView.Interactive)
        self._apply_table_column_visibility()
        self._fit_table_columns_to_viewport()

    def _on_table_header_changed(self, *_args):
        if getattr(self, "_restoring_table_header", False) or getattr(self, "_fitting_table_header", False):
            return
        self._fit_table_columns_to_viewport()
        timer = getattr(self, "_save_table_header_timer", None)
        if timer is not None:
            timer.start(500)
        else:
            self._save_table_header_state()

    def _table_header_available_width(self) -> int:
        viewport = self.table.viewport()
        width = viewport.width() if viewport is not None else 0
        if width <= 0:
            width = self.table.contentsRect().width()
        return max(0, int(width) - 2)

    def _fit_table_columns_to_viewport(self):
        if not hasattr(self, "table") or getattr(self, "_fitting_table_header", False):
            return
        header = self.table.horizontalHeader()
        column_count = self.table.columnCount()
        if column_count <= 0:
            return
        visible_columns = [column for column in range(column_count) if not self.table.isColumnHidden(column)]
        if not visible_columns:
            return
        available_width = self._table_header_available_width()
        if available_width <= 0:
            return
        base_min_width = max(24, int(header.minimumSectionSize() or 24))
        min_width = base_min_width
        if available_width < min_width * len(visible_columns):
            min_width = max(1, available_width // len(visible_columns))
        widths = {
            column: max(min_width, int(header.sectionSize(column) or min_width))
            for column in visible_columns
        }
        total_width = sum(widths.values())
        if total_width <= 0 or abs(total_width - available_width) <= 1:
            return
        scale = available_width / total_width
        fitted = {column: max(min_width, int(round(width * scale))) for column, width in widths.items()}
        delta = available_width - sum(fitted.values())
        guard = 0
        while delta != 0 and guard < len(visible_columns) * 4:
            guard += 1
            if delta > 0:
                index = max(visible_columns, key=lambda column: fitted[column])
                fitted[index] += 1
                delta -= 1
                continue
            candidates = [column for column, width in fitted.items() if width > min_width]
            if not candidates:
                break
            index = max(candidates, key=lambda column: fitted[column])
            fitted[index] -= 1
            delta += 1
        self._fitting_table_header = True
        try:
            for column, width in fitted.items():
                if header.sectionSize(column) != width:
                    header.resizeSection(column, width)
        finally:
            self._fitting_table_header = False

    def _save_table_header_state(self):
        if not hasattr(self, "table"):
            return
        settings = self._settings()
        settings.setValue(self._table_header_settings_key, self.table.horizontalHeader().saveState())
        settings.sync()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._fit_table_columns_to_viewport)

    def done(self, result: int) -> None:
        self._fit_table_columns_to_viewport()
        self._save_table_header_state()
        super().done(result)

    def _add_row(self, preset: dict | None = None, *, source_index: int | None = None):
        item = dict(preset or {})
        item.setdefault("enabled", True)
        item.setdefault("kind", "bolus")
        if source_index is not None and 0 <= int(source_index) < len(self._working_templates):
            self._working_templates[int(source_index)] = item
            target_index = int(source_index)
        else:
            self._working_templates.append(item)
            target_index = len(self._working_templates) - 1
        self._render_table()
        if target_index in self._visible_template_indexes:
            row = self._visible_template_indexes.index(target_index)
            self.table.selectRow(row)
            self.table.scrollToItem(self.table.item(row, self.COL_LABEL))

    def _delete_selected_rows(self):
        selected_indexes = {
            self._visible_template_indexes[index.row()]
            for index in self.table.selectionModel().selectedRows()
            if 0 <= index.row() < len(self._visible_template_indexes)
        }
        if not selected_indexes:
            CustomMessageBox.warning(self, "Удаление", "Выберите препараты для удаления.")
            return
        for template_index in selected_indexes:
            self._working_templates[template_index] = None
        if not any(item is not None for item in self._working_templates):
            self._working_templates.append({"enabled": True, "kind": "bolus"})
        self._render_table()

    def _kind_combo_options(self) -> list[str]:
        return list(OPERBLOCK_PRESET_KIND_TITLES.values())

    def _group_combo_options(self) -> list[str]:
        return list(self._group_options)

    def _solvent_combo_options(self) -> list[str]:
        labels = ["Без растворителя"]
        for option in self._diluent_options:
            option_id = str((option or {}).get("id") or "").strip()
            option_label = str((option or {}).get("label") or (option or {}).get("display") or option_id).strip()
            if option_label and option_label not in labels:
                labels.append(option_label)
        return labels

    @staticmethod
    def _grayscale_pixmap(pixmap: QPixmap) -> QPixmap:
        image = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)
        result = QImage(image.size(), QImage.Format.Format_ARGB32)
        result.fill(Qt.GlobalColor.transparent)
        for y in range(image.height()):
            for x in range(image.width()):
                color = image.pixelColor(x, y)
                gray = int(color.red() * 0.299 + color.green() * 0.587 + color.blue() * 0.114)
                result.setPixelColor(x, y, QColor(gray, gray, gray, color.alpha()))
        return QPixmap.fromImage(result)

    def _favorite_icon(self, active: bool) -> QIcon:
        key = bool(active)
        cached = self._favorite_icon_cache.get(key)
        if cached is not None:
            return cached
        icon_path = os.path.join(get_icon_dir(), "remcardicon.png")
        pixmap = QPixmap(icon_path)
        if pixmap.isNull():
            icon = QIcon()
        else:
            pixmap = pixmap.scaled(18, 18, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            icon = QIcon(pixmap if key else self._grayscale_pixmap(pixmap))
        self._favorite_icon_cache[key] = icon
        return icon

    def _favorite_button(self, template_index: int, preset: dict) -> QPushButton:
        enabled = bool((preset or {}).get("enabled"))
        favorite = bool((preset or {}).get("favorite")) and enabled
        button = QPushButton()
        button.setObjectName("OperBlockFavoritePresetButton")
        button.setFixedSize(26, 24)
        button.setIcon(self._favorite_icon(favorite))
        button.setIconSize(QSize(18, 18))
        button.setEnabled(enabled)
        button.setCursor(Qt.PointingHandCursor if enabled else Qt.ArrowCursor)
        button.setToolTip(
            "Добавить в избранное" if enabled and not favorite
            else "Убрать из избранного" if enabled
            else "Сначала включите препарат"
        )
        button.setStyleSheet(
            f"""
            QPushButton#OperBlockFavoritePresetButton {{
                background: transparent;
                border: none;
                padding: 2px;
            }}
            QPushButton#OperBlockFavoritePresetButton:hover {{
                background: #EEF3FF;
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 5px;
            }}
            QPushButton#OperBlockFavoritePresetButton:disabled {{
                background: transparent;
                border: none;
            }}
            """
        )
        button.clicked.connect(lambda _=False, index=int(template_index): self._toggle_favorite(index))
        return button

    def _toggle_favorite(self, template_index: int) -> None:
        if not (0 <= int(template_index) < len(self._working_templates)):
            return
        template = self._working_templates[int(template_index)]
        if template is None or not bool(template.get("enabled")):
            return
        template["favorite"] = not bool(template.get("favorite"))
        self._render_table()

    def _open_combo_cell_editor(self, row: int, column: int):
        if column not in {self.COL_KIND, self.COL_GROUP, self.COL_SOLVENT}:
            return
        item = self.table.item(row, column)
        if item is not None and item.flags() & Qt.ItemFlag.ItemIsEditable:
            self.table.editItem(item)

    def _show_disabled_presets(self):
        self.enabled_filter.setCurrentIndex(2)
        self.filter_input.clear()
        self._apply_filter()

    def _toggle_visible_enabled(self):
        visible_indexes = list(self._visible_template_indexes)
        if not visible_indexes:
            return
        target_checked = any(
            not bool(self._working_templates[index].get("enabled"))
            for index in visible_indexes
            if self._working_templates[index] is not None
        )
        for index in visible_indexes:
            template = self._working_templates[index]
            if template is not None:
                template["enabled"] = target_checked
        self._render_table()

    def _apply_filter(self, *_args):
        if getattr(self, "_table_rendering", False):
            return
        self._render_table()

    def _render_table(self):
        query = self.filter_input.text().strip().casefold() if hasattr(self, "filter_input") else ""
        kind_filter = self.kind_filter.currentData() if hasattr(self, "kind_filter") else ""
        enabled_filter = self.enabled_filter.currentData() if hasattr(self, "enabled_filter") else "enabled"
        visible_indexes = [
            index
            for index, preset in enumerate(self._working_templates)
            if preset is not None and self._preset_matches_filter(preset, query, kind_filter, enabled_filter)
        ]
        self._visible_template_indexes = visible_indexes
        previous_block = self.table.blockSignals(True)
        previous_updates = self.table.updatesEnabled()
        self._table_rendering = True
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(len(visible_indexes))
            for row, template_index in enumerate(visible_indexes):
                preset = self._working_templates[template_index] or {}
                self._populate_table_row(row, template_index, preset)
        finally:
            self.table.setUpdatesEnabled(previous_updates)
            self.table.blockSignals(previous_block)
            self._table_rendering = False

    def _populate_table_row(self, row: int, template_index: int, preset: dict):
        self.table.setItem(row, self.COL_ENABLED, self._enabled_item(template_index, bool(preset.get("enabled"))))
        self.table.setItem(
            row,
            self.COL_LABEL,
            self._text_item(
                str(preset.get("label") or preset.get("drug_name") or ""),
                template_index,
                editable=not self._is_seed_preset(preset),
            ),
        )
        self.table.setItem(
            row,
            self.COL_DISPLAY,
            self._text_item(str(preset.get("display_name") or preset.get("label") or ""), template_index),
        )
        kind = self._kind_from_value(preset.get("kind"), allow_invalid=True)
        kind_title = OPERBLOCK_PRESET_KIND_TITLES.get(kind, kind)
        self.table.setItem(
            row,
            self.COL_KIND,
            self._text_item(kind_title, template_index),
        )
        default_group = OPERBLOCK_PRESET_KIND_GROUP_TITLES.get(kind, "Болюсы")
        self.table.setItem(
            row,
            self.COL_GROUP,
            self._text_item(self._group_display_text(preset.get("group") or default_group), template_index),
        )
        self.table.setItem(row, self.COL_DOSES, self._text_item(_join_semicolon_list(preset.get("doses") or []), template_index))
        self.table.setItem(row, self.COL_RATES, self._text_item(_join_semicolon_list(preset.get("rates") or []), template_index))
        self.table.setItem(row, self.COL_CONCENTRATION, self._text_item(str(preset.get("concentration") or ""), template_index))
        self.table.setItem(row, self.COL_SOLVENT, self._text_item(self._solvent_display_text(preset), template_index))
        self.table.setItem(row, self.COL_VOLUME, self._text_item(str(preset.get("solvent_volume_ml") or ""), template_index))
        self.table.setItem(row, self.COL_DURATION, self._text_item(str(preset.get("duration_min") or ""), template_index))
        self.table.setItem(row, self.COL_FAVORITE, self._favorite_item(template_index))
        self.table.setCellWidget(row, self.COL_FAVORITE, self._favorite_button(template_index, preset))

    @staticmethod
    def _enabled_item(template_index: int, enabled: bool) -> QTableWidgetItem:
        item = QTableWidgetItem("")
        item.setData(Qt.ItemDataRole.UserRole, template_index)
        item.setFlags(
            Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsUserCheckable
        )
        item.setCheckState(Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        return item

    @staticmethod
    def _text_item(text: str, template_index: int, *, editable: bool = True) -> QTableWidgetItem:
        item = QTableWidgetItem(str(text or ""))
        item.setData(Qt.ItemDataRole.UserRole, template_index)
        flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
        if editable:
            flags |= Qt.ItemFlag.ItemIsEditable
        item.setFlags(flags)
        return item

    @staticmethod
    def _favorite_item(template_index: int) -> QTableWidgetItem:
        item = QTableWidgetItem("")
        item.setData(Qt.ItemDataRole.UserRole, template_index)
        item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        return item

    def _on_table_item_changed(self, item: QTableWidgetItem):
        if getattr(self, "_table_rendering", False) or item is None:
            return
        row = item.row()
        if row < 0 or row >= len(self._visible_template_indexes):
            return
        template_index = self._visible_template_indexes[row]
        template = self._working_templates[template_index]
        if template is None:
            return
        column = item.column()
        if column == self.COL_ENABLED:
            enabled = item.checkState() == Qt.CheckState.Checked
            template["enabled"] = enabled
            if not enabled:
                template["favorite"] = False
            QTimer.singleShot(0, self._render_table)
            return
        if column == self.COL_LABEL and not self._is_seed_preset(template):
            template["label"] = item.text().strip()
        elif column == self.COL_DISPLAY:
            template["display_name"] = item.text().strip()
        elif column == self.COL_KIND:
            template["kind"] = self._kind_from_value(item.text(), allow_invalid=True)
            QTimer.singleShot(0, self._render_table)
        elif column == self.COL_GROUP:
            template["group"] = item.text().strip()
        elif column == self.COL_DOSES:
            template["doses"] = _split_semicolon_list(item.text())
        elif column == self.COL_RATES:
            template["rates"] = _split_semicolon_list(item.text())
        elif column == self.COL_CONCENTRATION:
            template["concentration"] = item.text().strip() or None
        elif column == self.COL_SOLVENT:
            solvent_id, solvent_label = self._resolve_solvent_text(item.text(), template)
            template["solvent_id"] = solvent_id
            template["solvent_label"] = solvent_label
        elif column == self.COL_VOLUME:
            template["solvent_volume_ml"] = item.text().strip() or None
        elif column == self.COL_DURATION:
            template["duration_min"] = _safe_int(item.text())
        elif column == self.COL_FAVORITE:
            return

    @staticmethod
    def _kind_from_value(value, *, allow_invalid: bool = False) -> str:
        text = str(value or "").strip()
        folded = text.casefold()
        for kind, title in OPERBLOCK_PRESET_KIND_TITLES.items():
            if folded in {kind.casefold(), title.casefold()}:
                return kind
        legacy = {
            "болюс": "bolus",
            "болюсы": "bolus",
            "gas": "gas",
            "газ": "gas",
            "газы": "gas",
            "ингаляция": "gas",
            "ингаляции": "gas",
            "inhalation": "gas",
            "infusion": "continuous_infusion",
            "continuous": "continuous_infusion",
            "дозатор": "continuous_infusion",
            "перфузор": "continuous_infusion",
            "перфузоры": "continuous_infusion",
            "инфузия": "continuous_infusion",
            "инфузии": "continuous_infusion",
            "инфузии / перфузор": "continuous_infusion",
            "timed": "timed_infusion",
            "капельница": "timed_infusion",
            "капельницы": "timed_infusion",
            "solvent": "timed_infusion",
            "раствор": "timed_infusion",
            "растворы": "timed_infusion",
            "event": "bolus",
            "событие": "bolus",
            "события": "bolus",
        }
        if folded in legacy:
            return legacy[folded]
        if allow_invalid and text:
            return text
        return normalize_operblock_medication_preset_kind(text)

    def _solvent_display_text(self, preset: dict) -> str:
        solvent_id = str(preset.get("solvent_id") or "").strip()
        solvent_label = str(preset.get("solvent_label") or "").strip()
        if solvent_id:
            for option in self._diluent_options:
                option_id = str((option or {}).get("id") or "").strip()
                option_label = str((option or {}).get("label") or (option or {}).get("display") or option_id).strip()
                if option_id == solvent_id:
                    return option_label or solvent_label or solvent_id
        return solvent_label

    def _resolve_solvent_text(self, text: str, current: dict) -> tuple[str | None, str | None]:
        clean = str(text or "").strip()
        if not clean or clean.casefold() == "без растворителя":
            return None, None
        for option in self._diluent_options:
            option_id = str((option or {}).get("id") or "").strip()
            option_label = str((option or {}).get("label") or (option or {}).get("display") or option_id).strip()
            if clean.casefold() in {option_id.casefold(), option_label.casefold()}:
                return option_id or None, option_label or None
        current_id = str(current.get("solvent_id") or "").strip()
        current_label = str(current.get("solvent_label") or "").strip()
        if current_id and current_label and clean.casefold() == current_label.casefold():
            return current_id, current_label
        return None, clean

    def _preset_matches_filter(self, preset: dict, query: str, kind_filter: str, enabled_filter: str) -> bool:
        kind = self._kind_from_value((preset or {}).get("kind"))
        enabled = bool((preset or {}).get("enabled"))
        if kind_filter and kind != kind_filter:
            return False
        if enabled_filter == "enabled" and not enabled:
            return False
        if enabled_filter == "disabled" and enabled:
            return False
        if query:
            haystack = " ".join(
                (
                    str((preset or {}).get("label") or ""),
                    str((preset or {}).get("display_name") or ""),
                    str((preset or {}).get("group") or ""),
                    self._solvent_display_text(preset or {}),
                    str((preset or {}).get("solvent_id") or ""),
                )
            ).casefold()
            if query not in haystack:
                return False
        return True

    @staticmethod
    def _manual_preset_id(kind: str, label: str) -> str:
        slug = re.sub(r"[^0-9A-Za-zА-Яа-яЁё]+", "_", str(label or "").casefold()).strip("_") or "preset"
        return f"manual:{kind}:{slug}"

    def templates(self) -> list[dict]:
        return list(self._templates)

    def _template_from_preset(self, preset: dict) -> dict | None:
        label = str(preset.get("label") or preset.get("drug_name") or "").strip()
        if not label:
            CustomMessageBox.warning(self, "Ошибка", "Укажите название препарата.")
            return None
        kind = self._kind_from_value(preset.get("kind"), allow_invalid=True)
        if kind not in OPERBLOCK_MEDICATION_PRESET_KINDS:
            CustomMessageBox.warning(self, "Ошибка", f"Недопустимый тип препарата: {kind}.")
            return None
        item = dict(preset or {})
        preset_id = str(item.get("preset_id") or "").strip() or self._manual_preset_id(kind, label)
        solvent_id, solvent_label = self._resolve_solvent_text(self._solvent_display_text(item), item)
        item.update(
            {
                "preset_id": preset_id,
                "label": label,
                "display_name": str(item.get("display_name") or "").strip() or label,
                "kind": kind,
                "group": self._group_display_text(item.get("group"))
                or OPERBLOCK_PRESET_KIND_GROUP_TITLES.get(kind, "Болюсы"),
                "doses": _split_semicolon_list(_join_semicolon_list(item.get("doses") or [])),
                "rates": _split_semicolon_list(_join_semicolon_list(item.get("rates") or [])),
                "concentration": str(item.get("concentration") or "").strip() or None,
                "solvent_id": solvent_id,
                "solvent_label": solvent_label,
                "solvent_volume_ml": str(item.get("solvent_volume_ml") or "").strip() or None,
                "duration_min": _safe_int(item.get("duration_min")),
                "enabled": bool(item.get("enabled")),
                "favorite": bool(item.get("favorite")) and bool(item.get("enabled")),
            }
        )
        return item

    def accept(self):
        current_item = self.table.currentItem()
        if current_item is not None:
            self.table.closePersistentEditor(current_item)
        templates: list[dict] = []
        seen_ids: set[str] = set()
        labels: dict[str, str] = {}
        duplicate_labels: set[str] = set()
        for preset in self._working_templates:
            if preset is None:
                continue
            item = self._template_from_preset(preset)
            if item is None:
                return
            preset_id = str(item.get("preset_id") or "").strip()
            if preset_id in seen_ids:
                CustomMessageBox.warning(self, "Ошибка", f"preset_id '{preset_id}' указан дважды.")
                return
            seen_ids.add(preset_id)
            label = str(item.get("label") or "").strip()
            label_key = label.casefold()
            if label_key in labels:
                duplicate_labels.add(label)
            labels[label_key] = label
            templates.append(item)
        if duplicate_labels:
            CustomMessageBox.warning(
                self,
                "Дубликаты названий",
                "Есть повторяющиеся названия препаратов. Они будут сохранены, но лучше уточнить отображаемое название.",
            )
        self._templates = templates
        super().accept()


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
        text_label.setStyleSheet(f"font-size: 13px; font-weight: 700; color: {TEXT_PRIMARY};")
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
            time_label.setStyleSheet(f"font-size: 13px; font-weight: 700; color: {TEXT_PRIMARY};")
            self.time_input = QTimeEdit()
            self.time_input.setDisplayFormat("HH:mm")
            self.time_input.setFixedHeight(34)
            self.time_input.setTime(QTime(self._base_datetime.hour, self._base_datetime.minute))
            self.time_input.setStyleSheet(STYLE_PATIENT_FORM_VALID_FIELD)
            time_row.addWidget(time_label, 0)
            time_row.addWidget(self.time_input, 0)
            time_row.addStretch(1)
            layout.addLayout(time_row)

        route_label = QLabel("Место введения")
        route_label.setStyleSheet(f"font-size: 13px; font-weight: 700; color: {TEXT_PRIMARY};")
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
        cancel_button.setStyleSheet(STYLE_PATIENT_FORM_CANCEL_BUTTON)
        cancel_button.clicked.connect(self.reject)
        save_button = QPushButton("Сохранить")
        save_button.setMinimumHeight(34)
        save_button.setStyleSheet(OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE)
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
        button.setStyleSheet(
            f"""
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
            """
        )
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
    frame.setStyleSheet(
        f"background-color: {background}; border: none; border-radius: {frame_size // 2}px;"
    )
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(0, 0, 0, 0)
    icon_label = QLabel(frame)
    icon_label.setAlignment(Qt.AlignCenter)
    fallback = str(fallback_file or "").strip()
    if not fallback and isinstance(icon_ref, str) and os.path.splitext(icon_ref)[1]:
        fallback = icon_ref
    pixmap = load_operblock_icon_pixmap(icon_ref, fallback_file=fallback)
    if not pixmap.isNull():
        icon_label.setPixmap(pixmap.scaled(icon_size, icon_size, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    layout.addWidget(icon_label, 1)
    return frame


def _create_gas_dialog_plain_icon(icon_ref, *, icon_size: int, parent=None, fallback_file: str = "") -> QLabel:
    icon_label = QLabel(parent)
    icon_label.setFixedSize(icon_size, icon_size)
    icon_label.setAlignment(Qt.AlignCenter)
    fallback = str(fallback_file or "").strip()
    if not fallback and isinstance(icon_ref, str) and os.path.splitext(icon_ref)[1]:
        fallback = icon_ref
    pixmap = load_operblock_icon_pixmap(icon_ref, fallback_file=fallback)
    if not pixmap.isNull():
        icon_label.setPixmap(pixmap.scaled(icon_size, icon_size, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    return icon_label


def _gas_time_step_icon(*, up: bool) -> QIcon:
    pixmap = QPixmap(os.path.join(get_icon_dir(), "combo_arrow_down.svg"))
    if pixmap.isNull():
        return QIcon()
    if up:
        pixmap = pixmap.transformed(QTransform().rotate(180), Qt.SmoothTransformation)
    return QIcon(pixmap)


class MedicationEditDialogBase(SavedFramelessDialogMixin, QDialog):
    def __init__(
        self,
        *,
        title: str,
        drug_name: str,
        subtitle: str,
        value_label: str,
        value_text: str = "",
        placeholder: str = "",
        left_icon_file: str,
        left_icon_background: str,
        right_icon_file,
        geometry_key: str,
        parent=None,
        start_datetime: datetime | None = None,
        min_datetime: datetime | None = None,
        max_datetime: datetime | None = None,
        show_time: bool = True,
        show_route: bool = False,
        show_value: bool = True,
        time_label: str = "Время начала",
        route_code: str = OPERBLOCK_ORDER_ROUTE_DEFAULT,
        action_text: str = "Сохранить",
        minimum_width: int | None = None,
        right_icon_fallback_file: str = "",
    ):
        super().__init__(parent)
        self._dialog_title = str(title or "Изменить назначение").strip() or "Изменить назначение"
        self._drug_name = re.sub(r"\s+", " ", str(drug_name or "Назначение").strip()) or "Назначение"
        self._subtitle = str(subtitle or "").strip()
        self._value_label = str(value_label or "Доза").strip()
        self._value_text = str(value_text or "").strip()
        self._placeholder = str(placeholder or "").strip()
        self._left_icon_file = str(left_icon_file or "").strip()
        self._left_icon_background = str(left_icon_background or "#EEF2FF").strip()
        self._right_icon_file = right_icon_file
        self._right_icon_fallback_file = str(right_icon_fallback_file or "").strip()
        self._start_datetime = _minute_floor_dt(start_datetime)
        self._time_min_datetime = _minute_floor_dt(min_datetime)
        self._time_max_datetime = _minute_floor_dt(max_datetime)
        if self._time_min_datetime and self._time_max_datetime and self._time_max_datetime < self._time_min_datetime:
            self._time_max_datetime = None
        self._show_time = bool(show_time)
        self._show_route = bool(show_route)
        self._show_value = bool(show_value)
        self._time_label = str(time_label or "Время").strip() or "Время"
        self._route_code = _normalize_order_route_code(route_code)
        self._action_text = str(action_text or "Сохранить")
        self._time_text_updating = False

        if minimum_width is not None:
            width = int(minimum_width)
        elif self._show_route and self._show_time:
            width = 720
        elif self._show_time and self._show_value:
            width = 650
        elif self._show_time:
            width = 540
        else:
            width = 560
        height = 342 if self._show_time and (self._show_value or self._show_route) else 292
        self.setWindowTitle(self._dialog_title)
        _apply_operblock_window_icon(self)
        self.setMinimumSize(width, height)
        self.resize(width, height)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setMouseTracking(True)
        self._init_saved_frameless_dialog(geometry_key, drag_area_height=44)
        self._init_ui()
        self._restore_saved_geometry()

    def _init_ui(self) -> None:
        self.setStyleSheet(
            """
            QDialog {
                background-color: transparent;
                font-family: "Segoe UI", "Inter", Arial, sans-serif;
            }
            QFrame#MedDialogFrame {
                background-color: #F8FAFC;
                border: 1px solid #CBD5E1;
                border-radius: 12px;
            }
            QFrame#MedDialogHeader {
                background-color: #F8FAFC;
                border-top-left-radius: 12px;
                border-top-right-radius: 12px;
                border-bottom: 1px solid #E5E7EB;
            }
            QLabel#MedDialogTitle {
                color: #111827;
                font-size: 16px;
                font-weight: 600;
                background: transparent;
            }
            QPushButton#MedDialogClose {
                background-color: transparent;
                color: #1F2937;
                border: none;
                border-radius: 6px;
                font-size: 18px;
                font-weight: 300;
                padding-bottom: 1px;
            }
            QPushButton#MedDialogClose:hover {
                background-color: #e74c3c;
                color: white;
            }
            QFrame#MedDialogBody {
                background-color: #F8FAFC;
                border: none;
            }
            QLabel#MedDrugName {
                color: #111827;
                font-size: 19px;
                font-weight: 700;
                background: transparent;
            }
            QLabel#MedDrugSubtitle {
                color: #6B7280;
                font-size: 12px;
                font-weight: 400;
                background: transparent;
            }
            QFrame#MedSeparator {
                background-color: #E5E7EB;
                border: none;
                max-height: 1px;
                min-height: 1px;
            }
            QFrame#MedFieldCard {
                background-color: #FFFFFF;
                border: 1px solid #E5E7EB;
                border-radius: 14px;
            }
            QLabel#MedFieldTitle {
                color: #6B7280;
                font-size: 12px;
                font-weight: 500;
                background: transparent;
            }
            QLineEdit#MedValueInput {
                background-color: #FFFFFF;
                color: #111827;
                border: 1px solid #D1D5DB;
                border-radius: 8px;
                font-size: 18px;
                font-weight: 400;
                padding: 0 14px;
                selection-background-color: #C7D2FE;
            }
            QLineEdit#MedValueInput:focus {
                border: 1px solid #6366F1;
            }
            QFrame#MedTimeInputFrame {
                background-color: #FFFFFF;
                border: 1px solid #D1D5DB;
                border-radius: 8px;
            }
            QFrame#MedTimeInputFrame[focused="true"] {
                border: 1px solid #6366F1;
            }
            QLineEdit#MedTimeInput {
                background-color: transparent;
                color: #111827;
                border: none;
                font-size: 18px;
                font-weight: 400;
                padding: 0 14px;
                selection-background-color: #C7D2FE;
            }
            QFrame#MedTimeStepperColumn {
                background-color: transparent;
                border: none;
            }
            QPushButton#MedTimeStepButton {
                background-color: #EAF3FF;
                border: 1px solid #BFD1E5;
                border-radius: 5px;
                padding: 0;
            }
            QPushButton#MedTimeStepButton:hover {
                background-color: #DCEBFA;
                border-color: #9FB8D2;
            }
            QPushButton#MedTimeStepButton:pressed {
                background-color: #CFE3F7;
            }
            QPushButton#MedRouteButton {
                background-color: #F1F5F9;
                color: #64748B;
                border: 1px solid #D1D5DB;
                border-radius: 8px;
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton#MedRouteButton:checked {
                background-color: #DFF4E8;
                border-color: #2F8A57;
                color: #17633A;
            }
            QPushButton#MedRouteButton:hover {
                border-color: #94A3B8;
            }
            QFrame#MedDialogFooter {
                background-color: #F8FAFC;
                border-bottom-left-radius: 12px;
                border-bottom-right-radius: 12px;
                border-top: 1px solid #E5E7EB;
            }
            QPushButton#MedCancelButton {
                background-color: #FFFFFF;
                color: #111827;
                border: 1px solid #D1D5DB;
                border-radius: 8px;
                font-size: 13px;
                font-weight: 600;
            }
            QPushButton#MedCancelButton:hover {
                background-color: #F3F4F6;
                border-color: #B8C0CC;
            }
            QPushButton#MedSaveButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #6366F1, stop:1 #4F46E5);
                color: #FFFFFF;
                border: 1px solid #4F46E5;
                border-radius: 8px;
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton#MedSaveButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #7377F7, stop:1 #5B52EA);
                border-color: #6366F1;
            }
            """
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(0)

        self.bg_container = QFrame(self)
        self.bg_container.setObjectName("MedDialogFrame")
        shadow = QGraphicsDropShadowEffect(self.bg_container)
        shadow.setBlurRadius(26)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(15, 23, 42, 38))
        self.bg_container.setGraphicsEffect(shadow)
        outer.addWidget(self.bg_container)

        main = QVBoxLayout(self.bg_container)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        header = QFrame(self.bg_container)
        header.setObjectName("MedDialogHeader")
        header.setFixedHeight(34)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(8, 0, 0, 0)
        header_layout.setSpacing(8)
        header_icon = _create_operblock_title_icon(20)
        if header_icon is not None:
            header_layout.addWidget(header_icon, 0, Qt.AlignVCenter)
        title = QLabel(self._dialog_title)
        title.setObjectName("MedDialogTitle")
        title.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        header_layout.addWidget(title, 1)
        close_button = QPushButton("×")
        close_button.setObjectName("MedDialogClose")
        close_button.setFixedSize(34, 34)
        close_button.setCursor(Qt.PointingHandCursor)
        close_button.clicked.connect(self.reject)
        header_layout.addWidget(close_button, 0, Qt.AlignVCenter)
        main.addWidget(header)

        body = QFrame(self.bg_container)
        body.setObjectName("MedDialogBody")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(22, 12, 22, 10)
        body_layout.setSpacing(10)

        drug_row = QFrame(body)
        drug_layout = QHBoxLayout(drug_row)
        drug_layout.setContentsMargins(0, 0, 0, 0)
        drug_layout.setSpacing(16)
        drug_layout.addWidget(
            _create_gas_dialog_image_icon(
                self._left_icon_file,
                frame_size=52,
                icon_size=32,
                background=self._left_icon_background,
                parent=drug_row,
            ),
            0,
            Qt.AlignVCenter,
        )
        drug_text_col = QVBoxLayout()
        drug_text_col.setContentsMargins(0, 0, 0, 0)
        drug_text_col.setSpacing(3)
        drug_name = QLabel(self._drug_name)
        drug_name.setObjectName("MedDrugName")
        drug_subtitle = QLabel(self._subtitle)
        drug_subtitle.setObjectName("MedDrugSubtitle")
        drug_text_col.addStretch(1)
        drug_text_col.addWidget(drug_name)
        drug_text_col.addWidget(drug_subtitle)
        drug_text_col.addStretch(1)
        drug_layout.addLayout(drug_text_col, 1)
        if self._right_icon_file:
            drug_layout.addWidget(
                _create_gas_dialog_plain_icon(
                    self._right_icon_file,
                    icon_size=92,
                    parent=drug_row,
                    fallback_file=self._right_icon_fallback_file,
                ),
                0,
                Qt.AlignRight | Qt.AlignVCenter,
            )
        body_layout.addWidget(drug_row)

        separator = QFrame(body)
        separator.setObjectName("MedSeparator")
        body_layout.addWidget(separator)

        fields = QHBoxLayout()
        fields.setContentsMargins(0, 0, 0, 0)
        fields.setSpacing(14)
        if self._show_value:
            fields.addWidget(self._value_card(body), 1)
        if self._show_time:
            fields.addWidget(self._time_card(body), 1)
        if self._show_route:
            fields.addWidget(self._route_card(body), 0)
        body_layout.addLayout(fields)
        main.addWidget(body, 1)

        footer = QFrame(self.bg_container)
        footer.setObjectName("MedDialogFooter")
        footer.setFixedHeight(54)
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(22, 0, 22, 0)
        footer_layout.setSpacing(10)
        footer_layout.addStretch(1)
        cancel_button = QPushButton("Отменить")
        cancel_button.setObjectName("MedCancelButton")
        cancel_button.setFixedSize(118, 38)
        cancel_button.setCursor(Qt.PointingHandCursor)
        cancel_button.clicked.connect(self.reject)
        save_button = QPushButton(self._action_text)
        save_button.setObjectName("MedSaveButton")
        save_button.setFixedSize(138, 38)
        save_button.setCursor(Qt.PointingHandCursor)
        save_button.clicked.connect(self.accept)
        cancel_button.setAutoDefault(False)
        cancel_button.setDefault(False)
        save_button.setAutoDefault(True)
        save_button.setDefault(True)
        footer_layout.addWidget(cancel_button)
        footer_layout.addWidget(save_button)
        main.addWidget(footer)

    def _field_card(self, parent=None, *, minimum_width: int = 220) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame(parent)
        card.setObjectName("MedFieldCard")
        card.setMinimumHeight(100)
        card.setMinimumWidth(int(minimum_width))
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(6)
        return card, layout

    def _value_card(self, parent=None) -> QFrame:
        card, layout = self._field_card(parent)
        title = QLabel(self._value_label)
        title.setObjectName("MedFieldTitle")
        layout.addWidget(title)
        self.value_input = QLineEdit()
        self.value_input.setObjectName("MedValueInput")
        self.value_input.setFixedHeight(52)
        self.value_input.setText(self._value_text)
        self.value_input.setPlaceholderText(self._placeholder)
        self.value_input.selectAll()
        layout.addWidget(self.value_input)
        return card

    def _time_card(self, parent=None) -> QFrame:
        card, layout = self._field_card(parent)
        title = QLabel(self._time_label)
        title.setObjectName("MedFieldTitle")
        layout.addWidget(title)

        self.time_frame = QFrame()
        self.time_frame.setObjectName("MedTimeInputFrame")
        self.time_frame.setFixedHeight(52)
        self.time_frame.setProperty("focused", False)
        time_layout = QHBoxLayout(self.time_frame)
        time_layout.setContentsMargins(0, 0, 0, 0)
        time_layout.setSpacing(0)

        self.time_input = QLineEdit()
        self.time_input.setObjectName("MedTimeInput")
        start_dt = self._coerce_time_datetime(self._start_datetime or datetime.now().replace(second=0, microsecond=0))
        self.time_input.setText(f"{start_dt.hour:02d}:{start_dt.minute:02d}")
        self.time_input.setPlaceholderText("09:10")
        self.time_input.setMaxLength(5)
        self.time_input.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self.time_input.textEdited.connect(self._on_time_text_edited)
        self.time_input.editingFinished.connect(self._commit_time_text)
        self.time_input.installEventFilter(self)
        time_layout.addWidget(self.time_input, 1)

        stepper = QFrame()
        stepper.setObjectName("MedTimeStepperColumn")
        stepper.setFixedWidth(42)
        stepper_layout = QVBoxLayout(stepper)
        stepper_layout.setContentsMargins(6, 4, 6, 4)
        stepper_layout.setSpacing(4)

        up_button = QPushButton()
        up_button.setObjectName("MedTimeStepButton")
        up_button.setFixedSize(30, 20)
        up_button.setIcon(_gas_time_step_icon(up=True))
        up_button.setIconSize(QSize(14, 14))
        up_button.setCursor(Qt.PointingHandCursor)
        up_button.clicked.connect(lambda _=False: self._step_time(1))
        down_button = QPushButton()
        down_button.setObjectName("MedTimeStepButton")
        down_button.setFixedSize(30, 20)
        down_button.setIcon(_gas_time_step_icon(up=False))
        down_button.setIconSize(QSize(14, 14))
        down_button.setCursor(Qt.PointingHandCursor)
        down_button.clicked.connect(lambda _=False: self._step_time(-1))
        stepper_layout.addWidget(up_button)
        stepper_layout.addWidget(down_button)
        time_layout.addWidget(stepper, 0)

        layout.addWidget(self.time_frame)
        return card

    def _route_card(self, parent=None) -> QFrame:
        card, layout = self._field_card(parent, minimum_width=142)
        card.setMaximumWidth(164)
        title = QLabel("Место")
        title.setObjectName("MedFieldTitle")
        layout.addWidget(title)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.route_button_group = QButtonGroup(self)
        self.route_button_group.setExclusive(True)
        for label, route_code in (("в/в", OPERBLOCK_ORDER_ROUTE_DEFAULT), ("в/м", OPERBLOCK_ORDER_ROUTE_INTRAMUSCULAR)):
            button = QPushButton(label)
            button.setObjectName("MedRouteButton")
            button.setCheckable(True)
            button.setFixedSize(56, 52)
            button.setCursor(Qt.PointingHandCursor)
            button.setProperty("route_code", route_code)
            self.route_button_group.addButton(button)
            row.addWidget(button)
            if route_code == self._route_code:
                button.setChecked(True)
        layout.addLayout(row)
        return card

    def value_text(self) -> str:
        return self.value_input.text().strip()

    def route_code(self) -> str:
        group = getattr(self, "route_button_group", None)
        if group is None:
            return OPERBLOCK_ORDER_ROUTE_DEFAULT
        checked = group.checkedButton()
        if checked is None:
            return OPERBLOCK_ORDER_ROUTE_DEFAULT
        return _normalize_order_route_code(checked.property("route_code"))

    def start_time_text(self) -> str:
        if not getattr(self, "_show_time", True):
            return ""
        return self._commit_time_text()

    def accept(self) -> None:
        if getattr(self, "_show_time", True):
            self._commit_time_text()
        super().accept()

    def eventFilter(self, obj, event):
        if obj is getattr(self, "time_input", None):
            if event.type() == QEvent.FocusIn:
                self._set_time_focus(True)
            elif event.type() == QEvent.FocusOut:
                self._set_time_focus(False)
        return super().eventFilter(obj, event)

    def _set_time_focus(self, focused: bool) -> None:
        frame = getattr(self, "time_frame", None)
        if frame is None:
            return
        frame.setProperty("focused", bool(focused))
        frame.style().unpolish(frame)
        frame.style().polish(frame)

    def _on_time_text_edited(self, text: str) -> None:
        if self._time_text_updating:
            return
        digits = re.sub(r"\D", "", str(text or ""))
        if len(digits) < 4:
            return
        event_dt = self._time_datetime_from_text(digits[:4])
        if event_dt is None:
            return
        self._set_time_input_text(self._time_text_from_datetime(self._coerce_time_datetime(event_dt)), select_all=False)

    def _commit_time_text(self) -> str:
        raw_text = self.time_input.text()
        event_dt = self._time_datetime_from_text(raw_text)
        if event_dt is None:
            event_dt = self._fallback_time_datetime()
        normalized = self._time_text_from_datetime(self._coerce_time_datetime(event_dt))
        self._set_time_input_text(normalized, select_all=False)
        return normalized

    def _step_time(self, delta_minutes: int) -> None:
        current_dt = self._time_datetime_from_text(self.time_input.text())
        if current_dt is None:
            current_dt = self._fallback_time_datetime()
        stepped = self._coerce_time_datetime(current_dt + timedelta(minutes=int(delta_minutes)))
        self._set_time_input_text(self._time_text_from_datetime(stepped), select_all=True)

    def _set_time_input_text(self, text: str, *, select_all: bool) -> None:
        self._time_text_updating = True
        try:
            self.time_input.setText(text)
            if select_all:
                self.time_input.setFocus(Qt.OtherFocusReason)
                self.time_input.selectAll()
            else:
                self.time_input.setCursorPosition(len(text))
        finally:
            self._time_text_updating = False

    def _fallback_time_datetime(self) -> datetime:
        fallback_dt = self._start_datetime or self._time_min_datetime or datetime.now().replace(second=0, microsecond=0)
        return self._coerce_time_datetime(fallback_dt)

    def _time_base_datetime(self) -> datetime:
        return self._time_min_datetime or self._start_datetime or datetime.now().replace(second=0, microsecond=0)

    def _time_datetime_from_text(self, value: str) -> datetime | None:
        minutes = self._time_minutes_from_text(value)
        if minutes is None:
            return None
        hour = minutes // 60
        minute = minutes % 60
        base_dt = self._time_base_datetime()
        same_day = datetime.combine(base_dt.date(), datetime.min.time()).replace(hour=hour, minute=minute)
        if same_day >= base_dt:
            return same_day
        crosses_midnight = hour < 6 or (base_dt.hour >= 12 and hour < base_dt.hour)
        return same_day + timedelta(days=1) if crosses_midnight else same_day

    def _coerce_time_datetime(self, value: datetime) -> datetime:
        event_dt = _minute_floor_dt(value) or datetime.now().replace(second=0, microsecond=0)
        if self._time_min_datetime and event_dt < self._time_min_datetime:
            return self._time_min_datetime
        if self._time_max_datetime and event_dt > self._time_max_datetime:
            return self._time_max_datetime
        return event_dt

    @staticmethod
    def _time_text_from_datetime(value: datetime) -> str:
        return f"{value.hour:02d}:{value.minute:02d}"

    @staticmethod
    def _time_minutes_from_text(value: str) -> int | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        colon_match = re.fullmatch(r"(\d{1,2})\s*:\s*(\d{1,2})", raw)
        if colon_match:
            hour = int(colon_match.group(1))
            minute = int(colon_match.group(2))
        else:
            digits = re.sub(r"\D", "", raw)
            if len(digits) == 4:
                hour = int(digits[:2])
                minute = int(digits[2:])
            elif len(digits) == 3:
                hour = int(digits[:1])
                minute = int(digits[1:])
            else:
                return None
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour * 60 + minute
        return None


class BolusEditDialog(MedicationEditDialogBase):
    def __init__(
        self,
        drug_name: str,
        dose_text: str = "",
        parent=None,
        *,
        base_datetime: datetime | None = None,
        min_datetime: datetime | None = None,
        max_datetime: datetime | None = None,
        route_code: str = OPERBLOCK_ORDER_ROUTE_DEFAULT,
        action_text: str = "Сохранить",
    ):
        clean_name = re.sub(r"\s+", " ", str(drug_name or "Препарат").strip()) or "Препарат"
        super().__init__(
            title=f"Изменить препарат: {clean_name}",
            drug_name=clean_name,
            subtitle="Болюсное введение",
            value_label="Доза. (г; мг; мкг)",
            value_text=_normalize_bolus_dose_text(dose_text),
            placeholder="200 мг",
            left_icon_file=type_icon_key("bolus"),
            left_icon_background="#EAFBF5",
            right_icon_file=edit_icon_key("bolus"),
            geometry_key="operblock/bolus_edit_dialog_geometry_v2",
            parent=parent,
            start_datetime=base_datetime,
            min_datetime=min_datetime,
            max_datetime=max_datetime,
            show_time=base_datetime is not None,
            show_route=True,
            route_code=route_code,
            action_text=action_text,
            minimum_width=724 if base_datetime is not None else 610,
        )

    def dose_text(self) -> str:
        return _normalize_bolus_dose_text(self.value_text())

    def text(self) -> str:
        return _build_order_text_for_display(self._drug_name, self.dose_text())

    def datetime_text(self) -> str:
        if not getattr(self, "_show_time", False):
            return ""
        selected_text = self.start_time_text()
        selected_dt = self._time_datetime_from_text(selected_text) or self._fallback_time_datetime()
        return self._coerce_time_datetime(selected_dt).isoformat(timespec="seconds")


class InfusionRateDialog(MedicationEditDialogBase):
    def __init__(
        self,
        title: str,
        rate_text: str = "",
        parent=None,
        *,
        drug_name: str = "Дозатор",
        start_datetime: datetime | None = None,
        min_datetime: datetime | None = None,
        max_datetime: datetime | None = None,
        action_text: str = "Сохранить",
    ):
        clean_name = re.sub(r"\s+", " ", str(drug_name or "Дозатор").strip()) or "Дозатор"
        super().__init__(
            title=title,
            drug_name=clean_name,
            subtitle="Длительная инфузия",
            value_label="Скорость: мл/ч",
            value_text=_compact_infusion_rate_display_text(rate_text),
            placeholder="1 мл/ч",
            left_icon_file=type_icon_key("continuous_infusion"),
            left_icon_background="#FFF7ED",
            right_icon_file=edit_icon_key("continuous_infusion"),
            geometry_key="operblock/infusion_rate_dialog_geometry_v2",
            parent=parent,
            start_datetime=start_datetime,
            min_datetime=min_datetime,
            max_datetime=max_datetime,
            show_time=start_datetime is not None,
            show_route=False,
            action_text=action_text,
            minimum_width=650 if start_datetime is not None else 560,
        )

    def rate_text(self) -> str:
        value, unit = _split_infusion_rate_text(self.value_text())
        return _format_infusion_rate(value, unit) if value else self.value_text()


class InfusionVolumeDialog(MedicationEditDialogBase):
    def __init__(
        self,
        title: str,
        volume_text: str = "",
        parent=None,
        *,
        drug_name: str = "Капельница",
        start_datetime: datetime | None = None,
        min_datetime: datetime | None = None,
        max_datetime: datetime | None = None,
        show_time: bool = True,
        action_text: str = "Сохранить",
        field_label: str = "Объем: мл",
        placeholder: str = "200 мл",
    ):
        clean_name = re.sub(r"\s+", " ", str(drug_name or "Капельница").strip()) or "Капельница"
        super().__init__(
            title=title,
            drug_name=clean_name,
            subtitle="Капельная инфузия",
            value_label=field_label,
            value_text=_normalize_volume_ml_text(volume_text),
            placeholder=placeholder,
            left_icon_file=type_icon_key("timed_infusion"),
            left_icon_background="#EAF3FF",
            right_icon_file=edit_icon_key("timed_infusion"),
            geometry_key="operblock/infusion_volume_dialog_geometry_v2",
            parent=parent,
            start_datetime=start_datetime,
            min_datetime=min_datetime,
            max_datetime=max_datetime,
            show_time=show_time,
            show_route=False,
            action_text=action_text,
            minimum_width=650 if show_time else 560,
        )

    def volume_text(self) -> str:
        return _normalize_volume_ml_text(self.value_text())


class InfusionStopDialog(MedicationEditDialogBase):
    def __init__(
        self,
        drug_name: str,
        parent=None,
        *,
        start_datetime: datetime | None = None,
        min_datetime: datetime | None = None,
        max_datetime: datetime | None = None,
        infusion_kind: str = "rate",
        payload: dict | None = None,
    ):
        clean_name = re.sub(r"\s+", " ", str(drug_name or "Назначение").strip()) or "Назначение"
        kind = str(infusion_kind or "").strip().casefold()
        if kind == "gas":
            subtitle = "Ингаляционный анестетик"
            left_icon_file = type_icon_key("gas")
            left_icon_background = "#EEF2FF"
            payload_data = payload if isinstance(payload, dict) else {}
            right_icon_file = drug_icon_candidate_keys_from_payload(payload_data, clean_name)
            if not right_icon_file:
                right_icon_file = [edit_icon_key("gas")]
            right_icon_fallback = default_drug_icon_file("gas")
        elif kind == "volume":
            subtitle = "Капельная инфузия"
            left_icon_file = type_icon_key("timed_infusion")
            left_icon_background = "#EAF3FF"
            right_icon_file = edit_icon_key("timed_infusion")
            right_icon_fallback = default_drug_icon_file("timed_infusion")
        else:
            subtitle = "Длительная инфузия"
            left_icon_file = type_icon_key("continuous_infusion")
            left_icon_background = "#FFF7ED"
            right_icon_file = edit_icon_key("continuous_infusion")
            right_icon_fallback = default_drug_icon_file("continuous_infusion")
        super().__init__(
            title=f"Остановить назначение: {clean_name}",
            drug_name=clean_name,
            subtitle=subtitle,
            value_label="",
            value_text="",
            placeholder="",
            left_icon_file=left_icon_file,
            left_icon_background=left_icon_background,
            right_icon_file=right_icon_file,
            right_icon_fallback_file=right_icon_fallback,
            geometry_key="operblock/infusion_stop_dialog_geometry_v1",
            parent=parent,
            start_datetime=start_datetime,
            min_datetime=min_datetime,
            max_datetime=max_datetime,
            show_time=True,
            show_route=False,
            show_value=False,
            time_label="Время окончания",
            action_text="Остановить",
            minimum_width=540,
        )

    def datetime_text(self) -> str:
        selected_text = self.start_time_text()
        selected_dt = self._time_datetime_from_text(selected_text) or self._fallback_time_datetime()
        return self._coerce_time_datetime(selected_dt).isoformat(timespec="seconds")


class GasDoseDialog(SavedFramelessDialogMixin, QDialog):
    def __init__(
        self,
        gas_name: str,
        concentration_text: str = "",
        parent=None,
        *,
        start_datetime: datetime | None = None,
        min_datetime: datetime | None = None,
        max_datetime: datetime | None = None,
        show_time: bool = True,
        action_text: str = "Сохранить",
        payload: dict | None = None,
    ):
        super().__init__(parent)
        self._gas_name = re.sub(r"\s+", " ", str(gas_name or "Газ").strip()) or "Газ"
        self._concentration_text = str(concentration_text or "").strip()
        self._start_datetime = _minute_floor_dt(start_datetime)
        self._time_min_datetime = _minute_floor_dt(min_datetime)
        self._time_max_datetime = _minute_floor_dt(max_datetime)
        if self._time_min_datetime and self._time_max_datetime and self._time_max_datetime < self._time_min_datetime:
            self._time_max_datetime = None
        self._show_time = bool(show_time)
        self._action_text = str(action_text or "Сохранить")
        self._payload = dict(payload or {}) if isinstance(payload, dict) else {}
        self._time_text_updating = False
        self.setWindowTitle(f"Изменить газ: {self._gas_name}")
        _apply_operblock_window_icon(self)
        self.setMinimumSize(610, 342 if self._show_time else 292)
        self.resize(610, 342 if self._show_time else 292)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setMouseTracking(True)
        self._init_saved_frameless_dialog("operblock/gas_dose_dialog_geometry_v3", drag_area_height=44)
        self._init_ui()
        self._restore_saved_geometry()

    def _init_ui(self):
        self.setStyleSheet(
            """
            QDialog {
                background-color: transparent;
                font-family: "Segoe UI", "Inter", Arial, sans-serif;
            }
            QFrame#GasDialogFrame {
                background-color: #F8FAFC;
                border: 1px solid #CBD5E1;
                border-radius: 12px;
            }
            QFrame#GasDialogHeader {
                background-color: #F8FAFC;
                border-top-left-radius: 12px;
                border-top-right-radius: 12px;
                border-bottom: 1px solid #E5E7EB;
            }
            QLabel#GasDialogTitle {
                color: #111827;
                font-size: 16px;
                font-weight: 600;
                background: transparent;
            }
            QPushButton#GasDialogClose {
                background-color: transparent;
                color: #1F2937;
                border: none;
                border-radius: 6px;
                font-size: 18px;
                font-weight: 300;
                padding-bottom: 1px;
            }
            QPushButton#GasDialogClose:hover {
                background-color: #e74c3c;
                color: white;
            }
            QFrame#GasDialogBody {
                background-color: #F8FAFC;
                border: none;
            }
            QLabel#GasDrugName {
                color: #111827;
                font-size: 19px;
                font-weight: 700;
                background: transparent;
            }
            QLabel#GasDrugSubtitle {
                color: #6B7280;
                font-size: 12px;
                font-weight: 400;
                background: transparent;
            }
            QFrame#GasSeparator {
                background-color: #E5E7EB;
                border: none;
                max-height: 1px;
                min-height: 1px;
            }
            QFrame#GasFieldCard {
                background-color: #FFFFFF;
                border: 1px solid #E5E7EB;
                border-radius: 14px;
            }
            QLabel#GasFieldTitle {
                color: #6B7280;
                font-size: 12px;
                font-weight: 500;
                background: transparent;
            }
            QPushButton#GasInfoButton {
                color: #9CA3AF;
                font-size: 12px;
                font-weight: 700;
                border: 2px solid #9CA3AF;
                border-radius: 8px;
                background: transparent;
                padding: 0;
            }
            QPushButton#GasInfoButton:hover {
                color: #4F46E5;
                border-color: #6366F1;
                background-color: #EEF2FF;
            }
            QLineEdit#GasDoseInput {
                background-color: #FFFFFF;
                color: #111827;
                border: 1px solid #D1D5DB;
                border-radius: 8px;
                font-size: 18px;
                font-weight: 400;
                padding: 0 14px;
                selection-background-color: #C7D2FE;
            }
            QLineEdit#GasDoseInput:focus {
                border: 1px solid #6366F1;
            }
            QFrame#GasTimeInputFrame {
                background-color: #FFFFFF;
                border: 1px solid #D1D5DB;
                border-radius: 8px;
            }
            QFrame#GasTimeInputFrame[focused="true"] {
                border: 1px solid #6366F1;
            }
            QLineEdit#GasTimeInput {
                background-color: transparent;
                color: #111827;
                border: none;
                font-size: 18px;
                font-weight: 400;
                padding: 0 14px;
                selection-background-color: #C7D2FE;
            }
            QFrame#GasTimeStepperColumn {
                background-color: transparent;
                border: none;
            }
            QPushButton#GasTimeStepButton {
                background-color: #EAF3FF;
                border: 1px solid #BFD1E5;
                border-radius: 5px;
                padding: 0;
            }
            QPushButton#GasTimeStepButton:hover {
                background-color: #DCEBFA;
                border-color: #9FB8D2;
            }
            QPushButton#GasTimeStepButton:pressed {
                background-color: #CFE3F7;
            }
            QFrame#GasDialogFooter {
                background-color: #F8FAFC;
                border-bottom-left-radius: 12px;
                border-bottom-right-radius: 12px;
                border-top: 1px solid #E5E7EB;
            }
            QPushButton#GasCancelButton {
                background-color: #FFFFFF;
                color: #111827;
                border: 1px solid #D1D5DB;
                border-radius: 8px;
                font-size: 13px;
                font-weight: 600;
            }
            QPushButton#GasCancelButton:hover {
                background-color: #F3F4F6;
                border-color: #B8C0CC;
            }
            QPushButton#GasSaveButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #6366F1, stop:1 #4F46E5);
                color: #FFFFFF;
                border: 1px solid #4F46E5;
                border-radius: 8px;
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton#GasSaveButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #7377F7, stop:1 #5B52EA);
                border-color: #6366F1;
            }
            """
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(0)

        self.bg_container = QFrame(self)
        self.bg_container.setObjectName("GasDialogFrame")
        shadow = QGraphicsDropShadowEffect(self.bg_container)
        shadow.setBlurRadius(26)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(15, 23, 42, 38))
        self.bg_container.setGraphicsEffect(shadow)
        outer.addWidget(self.bg_container)

        main = QVBoxLayout(self.bg_container)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        header = QFrame(self.bg_container)
        header.setObjectName("GasDialogHeader")
        header.setFixedHeight(34)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(8, 0, 0, 0)
        header_layout.setSpacing(8)
        header_icon = _create_operblock_title_icon(20)
        if header_icon is not None:
            header_layout.addWidget(header_icon, 0, Qt.AlignVCenter)
        title = QLabel(f"Изменить газ: {self._gas_name}")
        title.setObjectName("GasDialogTitle")
        title.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        header_layout.addWidget(title, 1)
        close_button = QPushButton("×")
        close_button.setObjectName("GasDialogClose")
        close_button.setFixedSize(34, 34)
        close_button.setCursor(Qt.PointingHandCursor)
        close_button.clicked.connect(self.reject)
        header_layout.addWidget(close_button, 0, Qt.AlignVCenter)
        main.addWidget(header)

        body = QFrame(self.bg_container)
        body.setObjectName("GasDialogBody")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(22, 12, 22, 10)
        body_layout.setSpacing(10)

        drug_row = QFrame(body)
        drug_row.setObjectName("GasDrugCard")
        drug_layout = QHBoxLayout(drug_row)
        drug_layout.setContentsMargins(0, 0, 0, 0)
        drug_layout.setSpacing(16)
        drug_layout.addWidget(
            _create_gas_dialog_image_icon(
                type_icon_key("gas"),
                frame_size=52,
                icon_size=32,
                background="#EEF2FF",
                parent=drug_row,
            ),
            0,
            Qt.AlignVCenter,
        )
        drug_text_col = QVBoxLayout()
        drug_text_col.setContentsMargins(0, 0, 0, 0)
        drug_text_col.setSpacing(3)
        drug_name = QLabel(self._gas_name)
        drug_name.setObjectName("GasDrugName")
        drug_subtitle = QLabel("Ингаляционный анестетик")
        drug_subtitle.setObjectName("GasDrugSubtitle")
        drug_text_col.addStretch(1)
        drug_text_col.addWidget(drug_name)
        drug_text_col.addWidget(drug_subtitle)
        drug_text_col.addStretch(1)
        drug_layout.addLayout(drug_text_col, 1)
        drug_layout.addWidget(
            _create_gas_dialog_plain_icon(
                drug_icon_candidate_keys_from_payload(self._payload, self._gas_name),
                icon_size=92,
                parent=drug_row,
                fallback_file=default_drug_icon_file("gas"),
            ),
            0,
            Qt.AlignRight | Qt.AlignVCenter,
        )
        body_layout.addWidget(drug_row)

        separator = QFrame(body)
        separator.setObjectName("GasSeparator")
        body_layout.addWidget(separator)

        fields = QHBoxLayout()
        fields.setContentsMargins(0, 0, 0, 0)
        fields.setSpacing(14)
        fields.addWidget(self._concentration_card(body), 1)
        if self._show_time:
            fields.addWidget(self._time_card(body), 1)
        body_layout.addLayout(fields)
        main.addWidget(body, 1)

        footer = QFrame(self.bg_container)
        footer.setObjectName("GasDialogFooter")
        footer.setFixedHeight(54)
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(22, 0, 22, 0)
        footer_layout.setSpacing(10)
        footer_layout.addStretch(1)
        cancel_button = QPushButton("Отменить")
        cancel_button.setObjectName("GasCancelButton")
        cancel_button.setFixedSize(118, 38)
        cancel_button.setCursor(Qt.PointingHandCursor)
        cancel_button.clicked.connect(self.reject)
        save_button = QPushButton(self._action_text)
        save_button.setObjectName("GasSaveButton")
        save_button.setFixedSize(138, 38)
        save_button.setCursor(Qt.PointingHandCursor)
        save_button.clicked.connect(self.accept)
        cancel_button.setAutoDefault(False)
        cancel_button.setDefault(False)
        save_button.setAutoDefault(True)
        save_button.setDefault(True)
        footer_layout.addWidget(cancel_button)
        footer_layout.addWidget(save_button)
        main.addWidget(footer)

    def _field_card(self, parent=None) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame(parent)
        card.setObjectName("GasFieldCard")
        card.setMinimumHeight(100)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(6)
        return card, layout

    def _concentration_card(self, parent=None) -> QFrame:
        card, layout = self._field_card(parent)
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)
        title = QLabel("Концентрация / MAC")
        title.setObjectName("GasFieldTitle")
        title_row.addWidget(title)
        info = QPushButton("!")
        info.setObjectName("GasInfoButton")
        info.setToolTip(GAS_MAC_HINT_TOOLTIP)
        info.setFixedSize(18, 18)
        info.setCursor(Qt.PointingHandCursor)
        info.clicked.connect(lambda _=False, button=info: self._show_mac_hint(button))
        title_row.addWidget(info, 0, Qt.AlignVCenter)
        title_row.addStretch(1)
        layout.addLayout(title_row)

        self.volume_input = QLineEdit()
        self.volume_input.setObjectName("GasDoseInput")
        self.volume_input.setFixedHeight(52)
        self.volume_input.setText(self._concentration_text)
        self.volume_input.setPlaceholderText("0,8 MAC")
        self.volume_input.selectAll()
        layout.addWidget(self.volume_input)
        return card

    def _time_card(self, parent=None) -> QFrame:
        card, layout = self._field_card(parent)
        title = QLabel("Время начала")
        title.setObjectName("GasFieldTitle")
        layout.addWidget(title)

        self.time_frame = QFrame()
        self.time_frame.setObjectName("GasTimeInputFrame")
        self.time_frame.setFixedHeight(52)
        self.time_frame.setProperty("focused", False)
        time_layout = QHBoxLayout(self.time_frame)
        time_layout.setContentsMargins(0, 0, 0, 0)
        time_layout.setSpacing(0)

        self.time_input = QLineEdit()
        self.time_input.setObjectName("GasTimeInput")
        start_dt = self._start_datetime or datetime.now().replace(second=0, microsecond=0)
        start_dt = self._coerce_time_datetime(start_dt)
        self.time_input.setText(f"{start_dt.hour:02d}:{start_dt.minute:02d}")
        self.time_input.setPlaceholderText("09:10")
        self.time_input.setMaxLength(5)
        self.time_input.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self.time_input.textEdited.connect(self._on_time_text_edited)
        self.time_input.editingFinished.connect(self._commit_time_text)
        self.time_input.installEventFilter(self)
        time_layout.addWidget(self.time_input, 1)

        stepper = QFrame()
        stepper.setObjectName("GasTimeStepperColumn")
        stepper.setFixedWidth(42)
        stepper_layout = QVBoxLayout(stepper)
        stepper_layout.setContentsMargins(6, 4, 6, 4)
        stepper_layout.setSpacing(4)

        up_button = QPushButton()
        up_button.setObjectName("GasTimeStepButton")
        up_button.setFixedSize(30, 20)
        up_button.setIcon(_gas_time_step_icon(up=True))
        up_button.setIconSize(QSize(14, 14))
        up_button.setCursor(Qt.PointingHandCursor)
        up_button.clicked.connect(lambda _=False: self._step_time(1))
        down_button = QPushButton()
        down_button.setObjectName("GasTimeStepButton")
        down_button.setFixedSize(30, 20)
        down_button.setIcon(_gas_time_step_icon(up=False))
        down_button.setIconSize(QSize(14, 14))
        down_button.setCursor(Qt.PointingHandCursor)
        down_button.clicked.connect(lambda _=False: self._step_time(-1))
        stepper_layout.addWidget(up_button)
        stepper_layout.addWidget(down_button)
        time_layout.addWidget(stepper, 0)

        layout.addWidget(self.time_frame)
        return card

    def volume_text(self) -> str:
        return self.volume_input.text().strip()

    def start_time_text(self) -> str:
        if not getattr(self, "_show_time", True):
            return ""
        return self._commit_time_text()

    def accept(self) -> None:
        if getattr(self, "_show_time", True):
            self._commit_time_text()
        super().accept()

    def eventFilter(self, obj, event):
        if obj is getattr(self, "time_input", None):
            if event.type() == QEvent.FocusIn:
                self._set_time_focus(True)
            elif event.type() == QEvent.FocusOut:
                self._set_time_focus(False)
        return super().eventFilter(obj, event)

    def _set_time_focus(self, focused: bool) -> None:
        frame = getattr(self, "time_frame", None)
        if frame is None:
            return
        frame.setProperty("focused", bool(focused))
        frame.style().unpolish(frame)
        frame.style().polish(frame)

    def _show_mac_hint(self, button: QWidget) -> None:
        QToolTip.showText(
            button.mapToGlobal(button.rect().bottomLeft()),
            GAS_MAC_HINT_TOOLTIP,
            button,
            button.rect(),
            12000,
        )

    def _on_time_text_edited(self, text: str) -> None:
        if self._time_text_updating:
            return
        digits = re.sub(r"\D", "", str(text or ""))
        if len(digits) < 4:
            return
        event_dt = self._time_datetime_from_text(digits[:4])
        if event_dt is None:
            return
        self._set_time_input_text(self._time_text_from_datetime(self._coerce_time_datetime(event_dt)), select_all=False)

    def _commit_time_text(self) -> str:
        raw_text = self.time_input.text()
        event_dt = self._time_datetime_from_text(raw_text)
        if event_dt is None:
            event_dt = self._fallback_time_datetime()
        normalized = self._time_text_from_datetime(self._coerce_time_datetime(event_dt))
        self._set_time_input_text(normalized, select_all=False)
        return normalized

    def _step_time(self, delta_minutes: int) -> None:
        current_dt = self._time_datetime_from_text(self.time_input.text())
        if current_dt is None:
            current_dt = self._fallback_time_datetime()
        stepped = self._coerce_time_datetime(current_dt + timedelta(minutes=int(delta_minutes)))
        self._set_time_input_text(self._time_text_from_datetime(stepped), select_all=True)

    def _set_time_input_text(self, text: str, *, select_all: bool) -> None:
        self._time_text_updating = True
        try:
            self.time_input.setText(text)
            if select_all:
                self.time_input.setFocus(Qt.OtherFocusReason)
                self.time_input.selectAll()
            else:
                self.time_input.setCursorPosition(len(text))
        finally:
            self._time_text_updating = False

    def _fallback_time_datetime(self) -> datetime:
        fallback_dt = (
            self._start_datetime
            or self._time_min_datetime
            or datetime.now().replace(second=0, microsecond=0)
        )
        return self._coerce_time_datetime(fallback_dt)

    def _time_base_datetime(self) -> datetime:
        return (
            self._time_min_datetime
            or self._start_datetime
            or datetime.now().replace(second=0, microsecond=0)
        )

    def _time_datetime_from_text(self, value: str) -> datetime | None:
        minutes = self._time_minutes_from_text(value)
        if minutes is None:
            return None
        hour = minutes // 60
        minute = minutes % 60
        base_dt = self._time_base_datetime()
        same_day = datetime.combine(base_dt.date(), datetime.min.time()).replace(hour=hour, minute=minute)
        if same_day >= base_dt:
            return same_day
        crosses_midnight = hour < 6 or (base_dt.hour >= 12 and hour < base_dt.hour)
        return same_day + timedelta(days=1) if crosses_midnight else same_day

    def _coerce_time_datetime(self, value: datetime) -> datetime:
        event_dt = _minute_floor_dt(value) or datetime.now().replace(second=0, microsecond=0)
        if self._time_min_datetime and event_dt < self._time_min_datetime:
            return self._time_min_datetime
        if self._time_max_datetime and event_dt > self._time_max_datetime:
            return self._time_max_datetime
        return event_dt

    @staticmethod
    def _time_text_from_datetime(value: datetime) -> str:
        return f"{value.hour:02d}:{value.minute:02d}"

    @staticmethod
    def _time_minutes_from_text(value: str) -> int | None:
        raw = str(value or "").strip()
        if not raw:
            return None

        colon_match = re.fullmatch(r"(\d{1,2})\s*:\s*(\d{1,2})", raw)
        if colon_match:
            hour = int(colon_match.group(1))
            minute = int(colon_match.group(2))
        else:
            digits = re.sub(r"\D", "", raw)
            if len(digits) == 4:
                hour = int(digits[:2])
                minute = int(digits[2:])
            elif len(digits) == 3:
                hour = int(digits[:1])
                minute = int(digits[1:])
            else:
                return None

        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour * 60 + minute
        return None


class TimeEditDialog(OperBlockStyledDialog):
    def __init__(self, title: str, base_datetime, parent=None, *, field_label: str = "Время"):
        self._base_datetime = _minute_floor_dt(_parse_datetime_value(base_datetime)) or datetime.now().replace(
            second=0,
            microsecond=0,
        )
        self._field_label = str(field_label or "Время")
        super().__init__(
            title,
            "time_edit_dialog_geometry",
            parent,
            minimum_size=(360, 140),
            initial_size=(400, 155),
        )
        self._init_ui()
        self._finalize_dialog_chrome()

    def _init_ui(self):
        layout = self.content_layout
        time_row = QHBoxLayout()
        time_row.setContentsMargins(0, 0, 0, 0)
        time_row.setSpacing(10)
        time_label = QLabel(self._field_label)
        time_label.setStyleSheet(f"font-size: 13px; font-weight: 700; color: {TEXT_PRIMARY};")
        self.time_input = QTimeEdit()
        self.time_input.setDisplayFormat("HH:mm")
        self.time_input.setFixedHeight(34)
        self.time_input.setTime(QTime(self._base_datetime.hour, self._base_datetime.minute))
        self.time_input.setStyleSheet(STYLE_PATIENT_FORM_VALID_FIELD)
        time_row.addWidget(time_label, 0)
        time_row.addWidget(self.time_input, 0)
        time_row.addStretch(1)
        layout.addLayout(time_row)

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel_button = QPushButton("Отменить")
        cancel_button.setMinimumHeight(34)
        cancel_button.setStyleSheet(STYLE_PATIENT_FORM_CANCEL_BUTTON)
        cancel_button.clicked.connect(self.reject)
        save_button = QPushButton("Сохранить")
        save_button.setMinimumHeight(34)
        save_button.setStyleSheet(OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE)
        save_button.clicked.connect(self.accept)
        self._configure_enter_accept_button(cancel_button, save_button)
        actions.addWidget(cancel_button)
        actions.addWidget(save_button)
        layout.addLayout(actions)

    def start_time_text(self) -> str:
        selected = self.time_input.time()
        return f"{selected.hour():02d}:{selected.minute():02d}"

    def datetime_text(self) -> str:
        selected = self.time_input.time()
        value = self._base_datetime.replace(
            hour=selected.hour(),
            minute=selected.minute(),
            second=0,
            microsecond=0,
        )
        return value.isoformat(timespec="seconds")


class OperBlockVitalsServiceAdapter:
    status_service = None

    def __init__(self, remcard_service, operblock_service: OperBlockService):
        self._remcard_service = remcard_service
        self._operblock_service = operblock_service
        self._operation_case_id: int | None = None
        self._admission_id: int | None = None
        self._started_at: datetime | None = None
        self._ended_at: datetime | None = None

    def set_operation_context(
        self,
        *,
        operation_case_id: int | None,
        admission_id: int | None,
        started_at: datetime | None,
        ended_at: datetime | None,
    ):
        self._operation_case_id = int(operation_case_id) if operation_case_id else None
        self._admission_id = int(admission_id) if admission_id else None
        self._started_at = _minute_floor_dt(started_at)
        self._ended_at = ended_at

    def normalize_time(self, value: str, fallback_time: str | None = None) -> str:
        return self._remcard_service.normalize_time(value, fallback_time)

    def is_time_input_valid(self, value: str) -> bool:
        return self._remcard_service.is_time_input_valid(value)

    def resolve_datetime(self, time: str, shift_date: datetime) -> datetime:
        normalized = self.normalize_time(time)
        hour, minute = map(int, normalized.split(":"))
        start = _minute_floor_dt(self._started_at or shift_date or datetime.now())
        same_day = datetime.combine(start.date(), datetime.min.time()).replace(hour=hour, minute=minute)
        if same_day >= start:
            return same_day
        crosses_midnight = hour < 6 or (start.hour >= 12 and hour < start.hour)
        if crosses_midnight:
            return same_day + timedelta(days=1)
        return same_day

    def get_day_period(self, date: datetime):
        start = self._started_at or date
        end = self._ended_at or (start + timedelta(hours=OPERBLOCK_INITIAL_CHART_HOURS))
        return start, max(end, start)

    def get_effective_bounds(self, admission_id: int, date: datetime):
        return self.get_day_period(date)

    def get_patient(self, admission_id: int):
        return self._remcard_service.get_patient(admission_id)

    def get_vitals(self, admission_id: int, date: datetime):
        if self._operation_case_id:
            return self._operblock_service.list_operation_vitals(self._operation_case_id)
        return self._remcard_service.get_vitals(admission_id, date)

    def get_vitals_extended(self, admission_id: int, date: datetime):
        return self.get_vitals(admission_id, date)

    def suggest_vital_time(self, shift_date: datetime, **_kwargs) -> str:
        vitals = self.get_vitals(self._admission_id or 0, shift_date)
        if vitals:
            return vitals[-1].timestamp.strftime("%H:%M")
        start = self._started_at or shift_date or datetime.now()
        return start.strftime("%H:%M")

    def next_full_hour(self, time: str, shift_date: datetime) -> str:
        current_dt = self.resolve_datetime(time, shift_date)
        return (current_dt + timedelta(minutes=OPERBLOCK_VITAL_TIME_STEP_MINUTES)).strftime("%H:%M")

    def now_time(self, current_dt: datetime, shift_date: datetime) -> str:
        _ = shift_date
        return current_dt.strftime("%H:%M")

    def current_shift_time(self, shift_date: datetime) -> str:
        return self.now_time(datetime.now(), shift_date)

    def apply_offset(self, time: str, shift_date: datetime, delta_minutes: int) -> str:
        current_dt = self.resolve_datetime(time, shift_date)
        target_dt = current_dt + timedelta(minutes=int(delta_minutes))
        start = self._started_at or shift_date
        if target_dt < start:
            target_dt = start
        return target_dt.strftime("%H:%M")

    def display_hint(self, time: str, shift_date: datetime) -> dict:
        resolved = self.resolve_datetime(time, shift_date)
        start = self._started_at or shift_date
        day_offset = max(0, (resolved.date() - start.date()).days)
        return {
            "label": resolved.strftime("%H:%M"),
            "day_offset": day_offset,
            "text": f"операция +{day_offset} день" if day_offset else "операция",
        }

    def add_vital(self, dto, shift_date: datetime | None = None, force: bool = False, expected_revision=None):
        _ = shift_date, force
        timestamp = getattr(dto, "timestamp", None)
        if self._started_at and isinstance(timestamp, datetime):
            start_minute = self._started_at.replace(second=0, microsecond=0)
            vital_minute = timestamp.replace(second=0, microsecond=0)
            if vital_minute < start_minute:
                raise ValueError(
                    f"Пациент поступил в операционную в {start_minute.strftime('%H:%M')}. "
                    "Ввод данных ранее этого времени невозможен."
                )
        if self._ended_at and isinstance(timestamp, datetime):
            end_minute = self._ended_at.replace(second=0, microsecond=0)
            vital_minute = timestamp.replace(second=0, microsecond=0)
            if vital_minute > end_minute:
                raise ValueError(
                    f"Операция завершена в {end_minute.strftime('%H:%M')}. "
                    "Ввод данных позже этого времени невозможен."
                )
        return self._operblock_service.add_vital_record(dto, expected_revision=expected_revision)

    def delete_last_vital(self, admission_id: int, date: datetime, expected_revision=None):
        vitals = self.get_vitals(admission_id, date)
        if not vitals:
            return None
        latest = vitals[-1]
        _ = latest
        return self._operblock_service.delete_last_vital_record(
            admission_id,
            expected_revision=expected_revision,
        )

    def enqueue_write(self, *args, **kwargs):
        return self._remcard_service.enqueue_write(*args, **kwargs)


class OccupyTableDialog(SavedFramelessDialogMixin, QDialog):
    def __init__(self, table_code: str, table_name: str, parent=None):
        super().__init__(parent)
        self.table_code = table_code
        self.table_name = table_name
        self.mkb_service = MKBService()
        self.setWindowTitle("Занять стол")
        _apply_operblock_window_icon(self)
        self.setMinimumSize(760, 520)
        self.resize(860, 600)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setMouseTracking(True)
        self._init_saved_frameless_dialog("operblock/occupy_table_dialog_geometry", drag_area_height=70)
        self._init_ui()
        self._restore_saved_geometry()

    def _init_ui(self):
        apply_custom_dialog_style(self)

        self.bg_container = QFrame(self)
        self.bg_container.setObjectName("DialogMainFrame")
        self.bg_container.setMouseTracking(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.bg_container)

        main = QVBoxLayout(self.bg_container)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        header = QFrame(self.bg_container)
        header.setObjectName("DialogTitleBar")
        header.setFixedHeight(30)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(5, 0, 0, 0)
        header_layout.setSpacing(0)
        icon_label = _create_operblock_title_icon(20)
        if icon_label is not None:
            header_layout.addWidget(icon_label)
            header_layout.addSpacing(8)
        title = QLabel(f"ЗАНЯТЬ СТОЛ - {self.table_name.upper()}")
        title.setObjectName("DialogTitleText")
        header_layout.addWidget(title)
        header_layout.addStretch(1)
        self.close_button = QPushButton("✕")
        self.close_button.setObjectName("DialogCloseBtn")
        self.close_button.setFixedSize(30, 30)
        self.close_button.setCursor(Qt.PointingHandCursor)
        self.close_button.clicked.connect(self.reject)
        header_layout.addWidget(self.close_button)
        main.addWidget(header)

        content = QFrame(self.bg_container)
        content.setStyleSheet(STYLE_PATIENT_FORM_TAB)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(20, 16, 20, 20)
        content_layout.setSpacing(10)

        self.form_scroll = QScrollArea()
        self.form_scroll.setWidgetResizable(True)
        self.form_scroll.setFrameShape(QScrollArea.NoFrame)
        self.form_scroll.setStyleSheet(STYLE_PATIENT_FORM_SCROLL)

        self.form_page = QWidget()
        self.form_page.setStyleSheet(STYLE_PATIENT_FORM_PAGE)
        page_layout = QVBoxLayout(self.form_page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(2)

        general_title = QLabel("ОБЩИЕ ДАННЫЕ")
        general_title.setStyleSheet(STYLE_PATIENT_FORM_SECTION_TITLE)
        page_layout.addWidget(general_title)

        general = QWidget()
        general.setStyleSheet(STYLE_PATIENT_FORM_TAB)
        general_form = QFormLayout(general)
        general_form.setContentsMargins(24, 12, 24, 12)
        general_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        general_form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        general_form.setHorizontalSpacing(14)
        general_form.setVerticalSpacing(10)

        self.history_input = _line_edit()
        self.history_input.setPlaceholderText("12345, АБ123 или 123/456")
        self.full_name_input = _line_edit()
        self.full_name_input.setPlaceholderText("Фамилия Имя Отчество")
        self.gender_combo = QComboBox()
        self.gender_combo.setFixedHeight(34)
        self.gender_combo.addItems(["Мужской", "Женский"])
        self.birth_date_input = _line_edit()
        self.birth_date_input.setPlaceholderText("дд.мм.гггг")
        self.birth_date_input.setMaxLength(10)
        self.birth_date_input.textEdited.connect(self._on_birth_date_text_edited)
        self.birth_date_input.editingFinished.connect(self._normalize_birth_date_field)
        general_form.addRow("Номер истории болезни:", self.history_input)
        general_form.addRow("ФИО пациента:", self.full_name_input)
        general_form.addRow("Пол:", self.gender_combo)
        general_form.addRow("Дата рождения:", self.birth_date_input)
        page_layout.addWidget(general)

        diagnosis_title = QLabel("ДИАГНОЗ")
        diagnosis_title.setStyleSheet(STYLE_PATIENT_FORM_SECTION_TITLE)
        page_layout.addWidget(diagnosis_title)

        diagnosis = QWidget()
        diagnosis.setStyleSheet(STYLE_PATIENT_FORM_TAB)
        diagnosis_form = QFormLayout(diagnosis)
        diagnosis_form.setContentsMargins(24, 12, 24, 12)
        diagnosis_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        diagnosis_form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        diagnosis_form.setHorizontalSpacing(14)
        diagnosis_form.setVerticalSpacing(10)

        self.diagnosis_code_input = _line_edit()
        self.diagnosis_code_input.setPlaceholderText("Код МКБ-10")
        self.diagnosis_code_input.setMaxLength(6)
        self.diagnosis_code_input.textEdited.connect(self._on_mkb_code_text_edited)
        self.diagnosis_code_input.editingFinished.connect(self._validate_mkb_code)
        self.diagnosis_name = _label("", size=12, color=TEXT_SECONDARY)
        self.diagnosis_text_input = _line_edit()
        self._set_manual_diagnosis_enabled(False, clear=True, placeholder="Сначала введите код МКБ-10")
        code_line = QHBoxLayout()
        code_line.setContentsMargins(0, 0, 0, 0)
        code_line.setSpacing(12)
        code_line.addWidget(self.diagnosis_code_input, 0)
        code_line.addWidget(self.diagnosis_name, 1)
        diagnosis_form.addRow("Код диагноза МКБ-10:", code_line)
        diagnosis_form.addRow("Ручной ввод диагноза:", self.diagnosis_text_input)
        page_layout.addWidget(diagnosis)
        page_layout.addStretch(1)

        self.form_scroll.setWidget(self.form_page)
        content_layout.addWidget(self.form_scroll, 1)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 10, 0, 0)
        self.cancel_button = QPushButton("ОТМЕНИТЬ")
        self.cancel_button.setCursor(Qt.PointingHandCursor)
        self.cancel_button.setFixedHeight(45)
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.setDefault(False)
        self.cancel_button.setStyleSheet(STYLE_PATIENT_FORM_CANCEL_BUTTON)
        self.cancel_button.clicked.connect(self.reject)
        self.save_button = QPushButton("СОХРАНИТЬ КАРТОЧКУ")
        self.save_button.setCursor(Qt.PointingHandCursor)
        self.save_button.setFixedHeight(45)
        self.save_button.setAutoDefault(True)
        self.save_button.setDefault(True)
        self.save_button.setStyleSheet(OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE)
        buttons.addWidget(self.cancel_button, 1)
        buttons.addWidget(self.save_button, 2)
        content_layout.addLayout(buttons)
        main.addWidget(content, 1)

    def _on_birth_date_text_edited(self, text: str):
        normalized = normalize_operblock_birth_date_text(text, final=False)
        if normalized == text:
            return
        self.birth_date_input.blockSignals(True)
        self.birth_date_input.setText(normalized)
        self.birth_date_input.setCursorPosition(len(normalized))
        self.birth_date_input.blockSignals(False)

    def _normalize_birth_date_field(self):
        normalized = normalize_operblock_birth_date_text(self.birth_date_input.text())
        birth_date = parse_date_value(normalized)
        if birth_date is None:
            if normalized != self.birth_date_input.text():
                self.birth_date_input.blockSignals(True)
                self.birth_date_input.setText(normalized)
                self.birth_date_input.blockSignals(False)
            return
        self.birth_date_input.blockSignals(True)
        self.birth_date_input.setText(birth_date.strftime("%d.%m.%Y"))
        self.birth_date_input.blockSignals(False)

    def _on_mkb_code_text_edited(self, text: str):
        normalized = normalize_operblock_mkb_code(text)
        self.diagnosis_code_input.blockSignals(True)
        self.diagnosis_code_input.setText(normalized)
        self.diagnosis_code_input.setCursorPosition(len(normalized))
        self.diagnosis_code_input.blockSignals(False)
        self.diagnosis_code_input.setStyleSheet("")
        self.diagnosis_name.setText("")
        if not is_complete_operblock_mkb_code(normalized):
            self._set_manual_diagnosis_enabled(False, clear=True, placeholder="Сначала введите полный код МКБ-10")

    def _set_manual_diagnosis_enabled(
        self,
        enabled: bool,
        *,
        clear: bool = False,
        text: str | None = None,
        placeholder: str = "Введите диагноз вручную",
    ):
        self.diagnosis_text_input.setEnabled(bool(enabled))
        self.diagnosis_text_input.setStyleSheet(
            STYLE_PATIENT_FORM_MANUAL_FIELD if enabled else STYLE_PATIENT_FORM_READONLY_FIELD
        )
        self.diagnosis_text_input.setPlaceholderText(placeholder)
        if text is not None:
            self.diagnosis_text_input.setText(text)
        elif clear:
            self.diagnosis_text_input.clear()

    def _validate_mkb_code(self):
        code = normalize_operblock_mkb_code(self.diagnosis_code_input.text())
        self.diagnosis_code_input.blockSignals(True)
        self.diagnosis_code_input.setText(code)
        self.diagnosis_code_input.blockSignals(False)
        if not code:
            self.diagnosis_name.setText("")
            self.diagnosis_code_input.setStyleSheet("")
            self._set_manual_diagnosis_enabled(False, clear=True, placeholder="Сначала введите код МКБ-10")
            return False
        if not is_complete_operblock_mkb_code(code):
            self.diagnosis_name.setText("Формат кода: X33, S82.0 или S82.01")
            self.diagnosis_code_input.setStyleSheet(STYLE_PATIENT_FORM_INVALID_FIELD)
            self._set_manual_diagnosis_enabled(False, clear=True, placeholder="Сначала введите полный код МКБ-10")
            return False
        name = self.mkb_service.get_diagnosis_by_code(code)
        if name:
            self.diagnosis_name.setText(name)
            self.diagnosis_code_input.setStyleSheet(STYLE_PATIENT_FORM_VALID_FIELD)
            self._set_manual_diagnosis_enabled(False, text=name, placeholder="Диагноз из МКБ-10")
            return True
        else:
            self.diagnosis_name.setText("Код не найден")
            self.diagnosis_code_input.setStyleSheet(STYLE_PATIENT_FORM_INVALID_FIELD)
            self._set_manual_diagnosis_enabled(True, clear=not self.diagnosis_text_input.isEnabled())
            return True

    def set_saving(self, saving: bool):
        self.form_page.setEnabled(not saving)
        self.cancel_button.setEnabled(not saving)
        self.close_button.setEnabled(not saving)
        self.save_button.setEnabled(not saving)
        self.save_button.setText("СОХРАНЕНИЕ..." if saving else "СОХРАНИТЬ КАРТОЧКУ")

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if self.save_button.isEnabled():
                self.save_button.click()
            event.accept()
            return
        super().keyPressEvent(event)

    def get_data(self) -> dict:
        history_number = normalize_operblock_history_number(self.history_input.text())
        full_name = self.full_name_input.text().strip()
        if not full_name:
            raise ValueError("ФИО пациента не заполнено.")
        self._normalize_birth_date_field()
        birth_date = parse_date_value(self.birth_date_input.text().strip())
        if birth_date is None:
            raise ValueError("Укажите корректную дату рождения.")
        diagnosis_code = normalize_operblock_mkb_code(self.diagnosis_code_input.text())
        if not diagnosis_code:
            raise ValueError("Введите код МКБ-10.")
        if not is_complete_operblock_mkb_code(diagnosis_code):
            raise ValueError("Код МКБ-10 должен быть в формате X33, S82.0 или S82.01.")
        self._validate_mkb_code()
        diagnosis_text = self.diagnosis_text_input.text().strip()
        if not diagnosis_text:
            raise ValueError("Диагноз не заполнен. Если код МКБ-10 не найден, заполните ручной ввод.")
        return {
            "table_code": self.table_code,
            "history_number": history_number,
            "full_name": full_name,
            "gender": self.gender_combo.currentText(),
            "birth_date": birth_date,
            "diagnosis_code": diagnosis_code or None,
            "diagnosis_text": diagnosis_text,
        }


class OperBlockMainWidget(QWidget):
    def __init__(self, patient_service, remcard_service, operblock_service: OperBlockService | None = None, parent=None):
        super().__init__(parent)
        self.patient_service = patient_service
        self.remcard_service = remcard_service
        self.data_service = getattr(remcard_service, "data_service", None)
        if operblock_service is None:
            raise RuntimeError("OperBlockService не передан в OperBlockMainWidget.")
        self.operblock_service = operblock_service
        self.operblock_vitals_service = OperBlockVitalsServiceAdapter(remcard_service, operblock_service)
        self._is_closing = False
        self._board_hash = ""
        self._protocol_hash = ""
        self._current_operation_case_id: int | None = None
        self._current_admission_id: int | None = None
        self._current_operation_start: datetime | None = None
        self._current_operation_end: datetime | None = None
        self._current_case_active = False
        self._current_stage_state: dict = {}
        self._current_anesthesia_start: datetime | None = None
        self._current_anesthesia_end: datetime | None = None
        self._current_surgery_start: datetime | None = None
        self._current_surgery_end: datetime | None = None
        self._current_anesthesia_active = False
        self._current_surgery_active = False
        self._current_anesthesia_assistance_type = ""
        self._current_operation_name = ""
        self._current_protocol_date = datetime.now()
        self._vitals_context_key: tuple[int, int, str] | None = None
        self._refresh_generation = 0
        self._write_pending = False
        self._table_cards: dict[str, QFrame] = {}
        self._board_card_hashes: dict[str, str] = {}
        self._board_card_states: dict[str, dict] = {}
        self._board_photo_thumbnail_cache: dict[tuple, QPixmap] = {}
        self._quick_order_templates: list[dict] = []
        self._medication_presets: list[dict] = []
        self._preset_search_text = ""
        self._preset_kind_filter = "bolus"
        self._orders_filter_kind = "all"
        self._orders_hide_deleted = True
        self._quick_order_buttons: list[QPushButton] = []
        self._pending_quick_orders_scroll_state: dict | None = None
        self._order_action_buttons: list[QPushButton] = []
        self._infusion_action_buttons: list[QPushButton] = []
        self._orders_force_top_on_next_apply = False
        self._current_orders_rows: list[dict] = []
        self._current_timeline_snapshot: dict | None = None
        self._route_only_write_suppressions: dict[tuple[int, int], float] = {}
        self._local_write_refresh_suppressions: dict[str, dict] = {}
        self._collapsed_order_group_keys: set[str] = set()
        self._orders_render_signature = ""
        self._orders_source_signature = ""
        self._rendered_medication_group_widgets: dict[str, QWidget] = {}
        self._rendered_medication_group_signatures: dict[str, str] = {}
        self._rendered_medication_group_order: list[str] = []
        self._rendered_order_detail_labels: dict[int, ElidedTooltipLabel] = {}
        self._rendered_medication_group_total_labels: dict[str, ElidedTooltipLabel] = {}
        self._active_infusions_render_signature = ""
        self._rendered_active_infusion_widgets: dict[str, QWidget] = {}
        self._rendered_active_infusion_signatures: dict[str, str] = {}
        self._rendered_active_infusion_order: list[str] = []
        self._active_infusions_empty_widget: QWidget | None = None
        self._orders_empty_widget: QWidget | None = None
        self._last_infusion_elapsed_refresh_minute = ""
        self._archive_cases: list[dict] = []
        self._role_launcher_mode = False
        self.protocol_page: QWidget | None = None
        self.archive_page: QWidget | None = None
        self.content_stack: QStackedWidget | None = None
        self.vitals_chart: OperBlockChartWidget | None = None
        self.vitals_input: VitalsWidget | None = None
        self._creating_lazy_protocol_page = False
        self._creating_lazy_archive_page = False
        self._board_refresh_seq = 0
        self._board_refresh_count_before_ready = 0
        self._current_board_apply_metrics: dict | None = None
        init_ui_started = operblock_startup_metrics.timer_start()
        try:
            self._init_ui()
        finally:
            operblock_startup_metrics.record_since("operblock_init_ui_ms", init_ui_started, source="operblock_widget")
        self._protocol_clock_timer = QTimer(self)
        self._protocol_clock_timer.timeout.connect(self._update_protocol_current_time_label)
        self._protocol_clock_timer.start(1000)
        self._update_protocol_current_time_label()
        self._connect_updates()

    def _init_ui(self):
        self.setStyleSheet(f"QWidget {{ background-color: {BG_MAIN}; color: {TEXT_PRIMARY}; }}")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 2, 0, 0)
        root.setSpacing(0)

        self.sector_8 = Sector8()
        self.sector_8.setFixedHeight(38)
        self.sector_8_panel = OperBlockSector8Panel()
        self.sector_8_panel.btn_archive.clicked.connect(self._show_operblock_archive)
        self.sector_8_panel.btn_refresh.clicked.connect(lambda: self.auto_refresh(force=True))
        self.sector_8_panel.btn_settings.clicked.connect(self._open_operblock_settings)
        self.sector_8_panel.btn_back.clicked.connect(self.on_back_clicked)
        self.sector_8_panel.btn_exit.clicked.connect(lambda: self.window().close())
        self.sector_8.set_content(self.sector_8_panel)
        root.addWidget(self.sector_8)

        self.stack = QStackedWidget()
        self.board_page = self._build_board_page()
        self.stack.addWidget(self.board_page)
        root.addWidget(self.stack, 1)
        self._set_protocol_chrome(False)

    def _ensure_protocol_page_created(self) -> bool:
        if self.protocol_page is not None:
            return True
        metric_started = operblock_startup_metrics.timer_start()
        self._creating_lazy_protocol_page = True
        try:
            self.protocol_page = self._build_protocol_page()
            self.stack.addWidget(self.protocol_page)
            self._update_protocol_current_time_label()
            self._apply_protocol_controls_state()
            return True
        finally:
            self._creating_lazy_protocol_page = False
            operblock_startup_metrics.record_since(
                "protocol_page_lazy_created_ms",
                metric_started,
                source="operblock_widget",
            )

    def _ensure_archive_page_created(self) -> bool:
        if self.archive_page is not None:
            return True
        metric_started = operblock_startup_metrics.timer_start()
        self._creating_lazy_archive_page = True
        try:
            self.archive_page = self._build_archive_page()
            self.stack.addWidget(self.archive_page)
            return True
        finally:
            self._creating_lazy_archive_page = False
            operblock_startup_metrics.record_since(
                "archive_page_lazy_created_ms",
                metric_started,
                source="operblock_widget",
            )

    def _build_board_page(self) -> QWidget:
        metric_started = operblock_startup_metrics.timer_start()
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(3, 5, 3, 3)
        layout.setSpacing(6)

        self.cards_layout = QHBoxLayout()
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(8)
        for table in OPERBLOCK_TABLES:
            table_code = str(table["code"])
            card = self._make_empty_table_card(table_code, table["display_name"])
            self._table_cards[table_code] = card
            table_payload = self._empty_board_table_payload(table)
            self._board_card_hashes[table_code] = self._board_table_content_hash(table_payload)
            self._board_card_states[table_code] = {
                "kind": "empty",
                "content_hash": self._board_card_hashes[table_code],
                "has_photo": True,
            }
            self.cards_layout.addWidget(card, 1)
        layout.addLayout(self.cards_layout, 1)
        operblock_startup_metrics.record_since("build_board_page_ms", metric_started, source="operblock_widget")
        return page

    def _build_archive_page(self) -> QWidget:
        metric_started = operblock_startup_metrics.timer_start()
        page = QWidget()
        page.setStyleSheet(f"QWidget {{ background-color: {BG_MAIN}; color: {TEXT_PRIMARY}; }}")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("Архив пациентов операционной")
        title.setStyleSheet(f"font-size: 18px; font-weight: 800; color: {COLOR_PRIMARY_DARK};")
        self.archive_search_input = QLineEdit()
        self.archive_search_input.setPlaceholderText("ФИО, ИБ, диагноз")
        self.archive_search_input.setMinimumHeight(34)
        self.archive_search_input.textChanged.connect(self._apply_operblock_archive_cases)
        self.archive_refresh_button = QPushButton("Обновить")
        self.archive_refresh_button.setMinimumHeight(34)
        self.archive_refresh_button.setStyleSheet(STYLE_SECTOR8_BUTTON)
        self.archive_refresh_button.clicked.connect(lambda: self.refresh_operblock_archive(force=True))
        header.addWidget(title, 0)
        header.addWidget(self.archive_search_input, 1)
        header.addWidget(self.archive_refresh_button, 0)
        layout.addLayout(header)

        self.archive_table = QTableWidget()
        self.archive_table.setColumnCount(7)
        self.archive_table.setHorizontalHeaderLabels(["Стол", "ФИО", "ИБ №", "Диагноз", "Поступил", "Освобождён", "Статус"])
        self.archive_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.archive_table.setSelectionMode(QTableWidget.SingleSelection)
        self.archive_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.archive_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.archive_table.itemSelectionChanged.connect(self._update_operblock_archive_buttons)
        self.archive_table.itemDoubleClicked.connect(lambda _item: self._open_selected_archive_case())
        layout.addWidget(self.archive_table, 1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.archive_open_button = QPushButton("Открыть карту")
        self.archive_restore_button = QPushButton("Вернуть на стол")
        self.archive_delete_button = QPushButton("Удалить текущего")
        self.archive_delete_all_button = QPushButton("Удалить всех")
        for button in (self.archive_open_button, self.archive_restore_button, self.archive_delete_button, self.archive_delete_all_button):
            button.setMinimumHeight(36)
            button.setStyleSheet(STYLE_SECTOR8_BUTTON)
            button.setEnabled(False)
        self.archive_delete_button.setStyleSheet(DANGER_BUTTON_STYLE)
        self.archive_delete_all_button.setStyleSheet(DANGER_BUTTON_STYLE)
        self.archive_open_button.clicked.connect(self._open_selected_archive_case)
        self.archive_restore_button.clicked.connect(self._restore_selected_archive_case)
        self.archive_delete_button.clicked.connect(self._delete_selected_archive_case)
        self.archive_delete_all_button.clicked.connect(self._delete_all_archive_cases)
        actions.addWidget(self.archive_open_button)
        actions.addWidget(self.archive_restore_button)
        actions.addWidget(self.archive_delete_button)
        actions.addWidget(self.archive_delete_all_button)
        layout.addLayout(actions)
        operblock_startup_metrics.record_since("build_archive_page_ms", metric_started, source="operblock_widget")
        if getattr(self, "_creating_lazy_archive_page", False):
            operblock_startup_metrics.record_since(
                "archive_page_lazy_build_ms",
                metric_started,
                source="operblock_widget",
            )
        return page

    def _build_protocol_page(self) -> QWidget:
        metric_started = operblock_startup_metrics.timer_start()
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(3, 5, 3, 3)
        layout.setSpacing(0)

        layout.addWidget(self._build_protocol_left_column(), 0)

        right_column = QWidget()
        right_column.setStyleSheet("background: transparent;")
        right_layout = QVBoxLayout(right_column)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self.header_frame = self._build_patient_header_panel()
        right_layout.addWidget(self.header_frame, 0)

        self.protocol_title_frame = self._build_protocol_title_frame()
        right_layout.addWidget(self.protocol_title_frame, 0)

        self.tab_bar = self._build_protocol_tab_bar()
        right_layout.addWidget(self.tab_bar, 0)

        self.content_stack = QStackedWidget()
        self.content_stack.addWidget(self._build_vitals_tab())
        self.content_stack.addWidget(self._build_orders_tab())
        right_layout.addWidget(self.content_stack, 1)
        self._apply_protocol_tab_display_settings()

        layout.addWidget(right_column, 1)
        operblock_startup_metrics.record_since("build_protocol_page_ms", metric_started, source="operblock_widget")
        if getattr(self, "_creating_lazy_protocol_page", False):
            operblock_startup_metrics.record_since(
                "protocol_page_lazy_build_ms",
                metric_started,
                source="operblock_widget",
            )
        return page

    def _build_protocol_left_column(self) -> QWidget:
        column = QWidget()
        column.setFixedWidth(250)
        column.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_protocol_action_sector(), 1)
        layout.addWidget(self._build_vitals_input_sector(), 0)
        return column

    def _build_protocol_action_sector(self) -> QWidget:
        self.protocol_actions_sector = Sector1a()
        self.protocol_actions_sector.setObjectName("sector_1a_operblock")
        self.protocol_actions_sector.setFixedWidth(250)

        actions_panel = QWidget()
        actions_panel.setStyleSheet("background: transparent; border: none;")
        actions_layout = QVBoxLayout(actions_panel)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(5)

        self.start_anesthesia_button = self._stage_action_button(" Начать пособие")
        self.end_anesthesia_button = self._stage_action_button(" Завершить пособие", danger=True)
        self.start_surgery_button = self._stage_action_button(" Начать операцию")
        self.close_case_button = self._stage_action_button(" Завершить операцию", danger=True)
        self.release_table_button = self._stage_action_button(" Освободить стол", danger=True)
        self.report_button = QPushButton(" Отчет за операцию")
        report_icon = os.path.join(get_icon_dir(), "allprint.png")
        if os.path.exists(report_icon):
            self.report_button.setIcon(QIcon(report_icon))
        self.report_button.setMinimumHeight(32)
        self.report_button.setCursor(Qt.PointingHandCursor)
        self.report_button.setStyleSheet(STYLE_SECTOR8_BUTTON)

        self.start_anesthesia_button.clicked.connect(self._start_anesthesia)
        self.end_anesthesia_button.clicked.connect(self._end_anesthesia)
        self.start_surgery_button.clicked.connect(self._start_surgery)
        self.close_case_button.clicked.connect(self._end_surgery)
        self.release_table_button.clicked.connect(self._confirm_release_current_case)
        self.report_button.clicked.connect(self._show_report_placeholder)

        for button in (
            self.start_anesthesia_button,
            self.end_anesthesia_button,
            self.start_surgery_button,
            self.close_case_button,
            self.release_table_button,
            self.report_button,
        ):
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            actions_layout.addWidget(button)

        self.protocol_actions_sector.set_content(actions_panel)
        if self.protocol_actions_sector.layout() is not None:
            self.protocol_actions_sector.layout().setContentsMargins(3, 0, 5, 4)
        self.protocol_actions_sector.setMinimumHeight(270)
        header = self.protocol_actions_sector.findChild(QLabel, "sector_header")
        if header is not None:
            header.setText("Управление")
        if getattr(self.protocol_actions_sector, "content_layout", None) is not None:
            self.protocol_actions_sector.content_layout.setAlignment(Qt.AlignTop)
        return self.protocol_actions_sector

    def _build_vitals_input_sector(self) -> QWidget:
        self.vitals_input_sector = Sector1b()
        self.vitals_input_sector.setObjectName("sector_1b_operblock")
        self.vitals_input_sector.setFixedWidth(250)
        vitals_widget_started = operblock_startup_metrics.timer_start()
        self.vitals_input = VitalsWidget(
            self.operblock_vitals_service,
            None,
            datetime.now(),
            forced_settings=OPERBLOCK_VITAL_SETTINGS,
            allow_inactive_status_input=True,
            force_vital_status=True,
            allow_future_input=True,
            time_quick_actions=OPERBLOCK_VITAL_TIME_QUICK_ACTIONS,
        )
        operblock_startup_metrics.record_since(
            "vitals_widget_create_ms",
            vitals_widget_started,
            source="operblock_widget",
        )
        self.vitals_input.data_changed.connect(self._on_standard_vitals_changed)
        try:
            self.vitals_input.undo_btn.clicked.disconnect()
        except Exception:
            pass
        self.vitals_input.undo_btn.clicked.connect(self._undo_last_action)
        self.vitals_input_sector.set_content(self.vitals_input)
        self.vitals_input_sector.content_layout.setAlignment(Qt.AlignTop)
        return self.vitals_input_sector

    def _build_patient_header_panel(self) -> QWidget:
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.protocol_info_header_label = QLabel("Информация")
        self.protocol_info_header_label.setFixedHeight(28)
        self.protocol_info_header_label.setAlignment(Qt.AlignCenter)
        self.protocol_info_header_label.setStyleSheet(SECTOR_HEADER_STYLE)
        layout.addWidget(self.protocol_info_header_label)

        top_body = QFrame()
        top_body.setStyleSheet(SECTOR_BODY_STYLE)
        top_layout = QHBoxLayout(top_body)
        top_layout.setContentsMargins(10, 2, 10, 4)
        top_layout.setSpacing(12)
        top_layout.setAlignment(Qt.AlignVCenter)

        self.protocol_status_label = QLabel("-")
        self.protocol_status_label.setFixedWidth(185)
        self.protocol_status_label.setAlignment(Qt.AlignCenter)
        self.protocol_status_label.setStyleSheet(
            """
            font-weight: bold; font-size: 13px; color: white;
            background-color: #7f8c8d; border-radius: 4px; padding: 2px 5px;
            """
        )
        self.protocol_history_label = _label("№ -", size=14, weight=700, color=COLOR_PRIMARY_DARK)
        self.protocol_patient_label = _label("-", size=16, weight=700)
        self.protocol_age_label = _label("Возраст: -", size=14)
        self.protocol_diagnosis_label = ElidedTooltipLabel("Диагноз: -")
        self.protocol_diagnosis_label.setStyleSheet(
            f"font-size: 14px; font-weight: 400; color: {TEXT_PRIMARY}; background: transparent; border: none;"
            f"{TOOLTIP_WHITE_STYLE}"
        )
        top_layout.addWidget(self.protocol_status_label, 0, Qt.AlignVCenter)
        top_layout.addWidget(self.protocol_history_label, 0, Qt.AlignVCenter)
        top_layout.addWidget(self.protocol_patient_label, 0, Qt.AlignVCenter)
        top_layout.addWidget(self.protocol_age_label, 0, Qt.AlignVCenter)
        top_layout.addWidget(self.protocol_diagnosis_label, 1, Qt.AlignVCenter)

        self.badge_ad = VitalBadge("АД:", "-/-", "#ffdada", COLOR_VITAL_AD_LINE)
        self.badge_pulse = VitalBadge("ЧСС:", "-", "#dadaff", COLOR_VITAL_PULSE)
        self.badge_spo2 = VitalBadge("SpO₂:", "-%", "#e1f5fe", COLOR_VITAL_SPO2)
        for badge in (self.badge_ad, self.badge_pulse, self.badge_spo2):
            badge.update_style(13, 115)
            badge.setFixedHeight(28)
            badge.layout_inner.setContentsMargins(5, 0, 5, 0)
            top_layout.addWidget(badge, 0, Qt.AlignVCenter)
        top_body.setFixedHeight(34)
        layout.addWidget(top_body)
        return wrapper

    def _build_protocol_title_frame(self) -> QWidget:
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 5, 0, 0)
        layout.setSpacing(0)

        body = QFrame()
        body.setObjectName("operblockProtocolTitle")
        body.setStyleSheet(
            f"""
            QFrame#operblockProtocolTitle {{
                background-color: {BG_LIGHT};
                border: 1.5px solid {BORDER_COLOR};
                border-bottom: 0.5px solid {BORDER_COLOR};
                border-top-left-radius: {CUSTOM_DIALOG_RADIUS};
                border-top-right-radius: {CUSTOM_DIALOG_RADIUS};
            }}
            QLabel {{
                background: transparent;
                border: none;
            }}
            """
        )
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(12, 0, 12, 0)
        body_layout.setSpacing(14)
        self.protocol_title_label = ElidedTooltipLabel(self._protocol_title_text())
        self.protocol_title_label.setStyleSheet(f"font-weight: bold; font-size: 16px; color: {COLOR_PRIMARY_DARK};")
        tokens = get_theme_manager().current_tokens()
        self.protocol_started_label = QLabel("-", body)
        self.protocol_started_label.setStyleSheet(build_remcard_period_label_style(tokens))
        self.protocol_current_time_label = QLabel("", body)
        self.protocol_current_time_label.setStyleSheet(build_remcard_current_time_label_style(tokens))
        body_layout.addWidget(self.protocol_title_label, 1)
        body_layout.addWidget(self.protocol_started_label, 0)
        body_layout.addWidget(self.protocol_current_time_label, 0)
        body_layout.addStretch(1)
        body.setFixedHeight(36)
        layout.addWidget(body)
        return wrapper

    def _protocol_title_text(self, now: datetime | None = None) -> str:
        assistance_type = normalize_operblock_anesthesia_type_label(
            getattr(self, "_current_anesthesia_assistance_type", "")
        )
        operation_name = normalize_operblock_team_text(getattr(self, "_current_operation_name", ""))

        parts: list[str] = []
        if assistance_type:
            parts.append(assistance_type)
        if operation_name:
            parts.append(operation_name)
        if not parts:
            return "Протокол анестезии"
        return f"Протокол анестезии: {', '.join(parts)}"

    def _update_protocol_title_label(self):
        label = getattr(self, "protocol_title_label", None)
        if label is not None:
            text = self._protocol_title_text()
            if hasattr(label, "set_full_text"):
                label.set_full_text(text)
            else:
                label.setText(text)

    def _build_protocol_tab_bar(self) -> QWidget:
        frame = QFrame()
        frame.setStyleSheet(
            f"""
            QFrame {{
                background: {BG_MAIN};
                border-left: 1.5px solid {BORDER_COLOR};
                border-right: 1.5px solid {BORDER_COLOR};
                border-bottom: 1.5px solid {BORDER_COLOR};
                border-top: none;
                border-bottom-left-radius: {CUSTOM_DIALOG_RADIUS};
                border-bottom-right-radius: {CUSTOM_DIALOG_RADIUS};
            }}
            """
        )
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(5, 0, 5, 0)
        layout.setSpacing(10)
        self.protocol_tabs_layout = layout
        self.tab_group = QButtonGroup(self)
        self.tab_group.setExclusive(True)
        self.vitals_tab_button = self._tab_button("Витальные функции", checked=True)
        self.orders_tab_button = self._tab_button("Назначения")
        self._protocol_tab_widgets = {
            "vitals": self.vitals_tab_button,
            "orders": self.orders_tab_button,
        }
        self._protocol_tab_indexes = {
            "vitals": 0,
            "orders": 1,
        }
        self._protocol_tab_order = ["vitals", "orders"]
        self._protocol_tab_visible = {"vitals": True, "orders": True}
        self.tab_group.addButton(self.vitals_tab_button, 0)
        self.tab_group.addButton(self.orders_tab_button, 1)
        self.tab_group.idClicked.connect(self._set_protocol_tab)
        layout.addWidget(self.vitals_tab_button)
        layout.addWidget(self.orders_tab_button)
        layout.addStretch(1)
        frame.setFixedHeight(37)
        return frame

    def _tab_button(self, text: str, *, checked: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setCheckable(True)
        button.setChecked(checked)
        button.setFixedHeight(32)
        button.setStyleSheet(build_remcard_tab_button_style(get_theme_manager().current_tokens()))
        return button

    def _clear_protocol_tabs_layout(self):
        layout = getattr(self, "protocol_tabs_layout", None)
        if layout is None:
            return
        while layout.count():
            layout.takeAt(0)

    def _apply_protocol_tab_display_settings(self):
        layout = getattr(self, "protocol_tabs_layout", None)
        widgets = dict(getattr(self, "_protocol_tab_widgets", {}) or {})
        if layout is None or not widgets:
            return
        try:
            payload = DisplaySettingsStorage().load()
            settings = role_display_settings_from_payload(payload, "operblock")
            section = settings["remcard_tabs"]
            order = [str(item_id) for item_id in section.get("order", [])]
            visible = {
                str(item_id): bool(value)
                for item_id, value in (section.get("visible") or {}).items()
            }
        except Exception:
            order = ["vitals", "orders"]
            visible = {"vitals": True, "orders": True}

        ordered_tabs = [tab_id for tab_id in order if tab_id in widgets]
        for tab_id in ("vitals", "orders"):
            if tab_id not in ordered_tabs:
                ordered_tabs.append(tab_id)
        visible_tabs = [tab_id for tab_id in ordered_tabs if visible.get(tab_id, True)]
        if not visible_tabs:
            visible_tabs = [ordered_tabs[0] if ordered_tabs else "vitals"]

        self._protocol_tab_order = ordered_tabs
        self._protocol_tab_visible = {tab_id: tab_id in visible_tabs for tab_id in widgets}
        self._clear_protocol_tabs_layout()
        for button in widgets.values():
            button.setVisible(False)
        for tab_id in ordered_tabs:
            button = widgets.get(tab_id)
            if button is None or not self._protocol_tab_visible.get(tab_id, False):
                continue
            layout.addWidget(button)
            button.setVisible(True)
        layout.addStretch(1)
        self._ensure_visible_protocol_tab()

    def _first_visible_protocol_tab_id(self) -> str:
        order = list(getattr(self, "_protocol_tab_order", ["vitals", "orders"]) or ["vitals", "orders"])
        visible = dict(getattr(self, "_protocol_tab_visible", {}) or {})
        for tab_id in order:
            if visible.get(tab_id, True):
                return tab_id
        return "vitals"

    def _set_protocol_tab_by_id(self, tab_id: str):
        if self.content_stack is None:
            return
        target_id = "orders" if tab_id == "orders" else "vitals"
        if target_id == "orders" and not self._orders_tab_enabled():
            target_id = "vitals"
        target_index = int((getattr(self, "_protocol_tab_indexes", {}) or {}).get(target_id, 0))
        self.content_stack.setCurrentIndex(target_index)
        for current_id, button in (getattr(self, "_protocol_tab_widgets", {}) or {}).items():
            button.setChecked(current_id == target_id)
        if target_id == "vitals":
            QTimer.singleShot(0, self._update_vitals_chart)

    def _ensure_visible_protocol_tab(self):
        stack = getattr(self, "content_stack", None)
        if stack is None:
            return
        current_id = "orders" if stack.currentIndex() == 1 else "vitals"
        visible = dict(getattr(self, "_protocol_tab_visible", {}) or {})
        if current_id == "orders" and not self._orders_tab_enabled():
            self._set_protocol_tab_by_id("vitals")
            return
        if not visible.get(current_id, True):
            self._set_protocol_tab_by_id(self._first_visible_protocol_tab_id())

    def _stage_action_button(self, text: str, *, danger: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setMinimumHeight(32)
        button.setCursor(Qt.PointingHandCursor)
        button.setStyleSheet(
            DANGER_BUTTON_STYLE
            if danger
            else STYLE_SECTOR8_BUTTON
        )
        return button

    def _set_protocol_tab(self, index: int):
        target = max(0, min(1, int(index)))
        target_id = "orders" if target == 1 else "vitals"
        if not getattr(self, "_protocol_tab_visible", {}).get(target_id, True):
            target_id = self._first_visible_protocol_tab_id()
        self._set_protocol_tab_by_id(target_id)

    def _orders_tab_enabled(self) -> bool:
        return bool(getattr(self, "_current_case_active", False) and getattr(self, "_current_anesthesia_active", False))

    def _build_vitals_tab(self) -> QWidget:
        metric_started = operblock_startup_metrics.timer_start()
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.vitals_legend_sector = Sector2g()
        self.vitals_legend_sector.update_legend(OPERBLOCK_VITAL_SETTINGS)
        self.vitals_legend_sector.setFixedWidth(140)
        self.vitals_staff_legend_panel = self._build_operblock_staff_legend_panel()
        legend_layout = getattr(self.vitals_legend_sector, "legend_layout", None)
        if legend_layout is not None:
            legend_layout.insertWidget(max(0, legend_layout.count() - 1), self.vitals_staff_legend_panel)
        layout.addWidget(self.vitals_legend_sector, 0)

        self.vitals_chart_sector = Sector2v()
        chart_import_started = operblock_startup_metrics.timer_start()
        from rem_card.ui.operblock_view.operblock_chart_widget import OperBlockChartWidget

        chart_import_elapsed = (time.perf_counter() - chart_import_started) * 1000.0 if chart_import_started else 0.0
        operblock_startup_metrics.record_duration(
            "operblock_chart_lazy_import_ms",
            chart_import_elapsed,
            source="operblock_widget",
        )
        operblock_startup_metrics.record_duration(
            "operblock_chart_module_import_ms",
            chart_import_elapsed,
            source="operblock_widget",
        )
        chart_started = operblock_startup_metrics.timer_start()
        self.vitals_chart = OperBlockChartWidget()
        chart_elapsed = (time.perf_counter() - chart_started) * 1000.0 if chart_started else 0.0
        operblock_startup_metrics.record_duration(
            "operblock_chart_create_ms",
            chart_elapsed,
            source="operblock_widget",
        )
        if getattr(self, "_creating_lazy_protocol_page", False):
            operblock_startup_metrics.record_duration(
                "chart_lazy_created_ms",
                chart_elapsed,
                source="operblock_widget",
            )
        self.vitals_chart.set_visible_hours(OPERBLOCK_INITIAL_CHART_HOURS)
        self.vitals_chart.set_time_grid_step_minutes(OPERBLOCK_CHART_GRID_STEP_MINUTES)
        self.vitals_chart.admission_id = None
        self.vitals_chart_sector.set_content(self.vitals_chart)
        layout.addWidget(self.vitals_chart_sector, 1)
        operblock_startup_metrics.record_since("build_vitals_tab_ms", metric_started, source="operblock_widget")
        return page

    def _build_operblock_staff_legend_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("OperBlockStaffLegendPanel")
        panel.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 28, 0, 0)
        layout.setSpacing(8)
        self.vitals_staff_legend_layout = layout
        panel.setStyleSheet(
            f"""
            QWidget#OperBlockStaffLegendPanel {{
                background: transparent;
                border: none;
            }}
            QFrame#OperBlockStaffSeparator {{
                background: {BORDER_COLOR};
                border: none;
                min-height: 1px;
                max-height: 1px;
                margin: 0 10px 2px 10px;
            }}
            QLabel#OperBlockStaffRoleLabel {{
                background-color: #E8F3FA;
                color: #2c3e50;
                font-size: 12px;
                font-weight: 600;
                border-left: 6px solid #4E9AC7;
                border-top-right-radius: 10px;
                border-bottom-right-radius: 10px;
                border-top: none;
                border-right: none;
                border-bottom: none;
                margin-left: 0px;
                padding: 7px 5px 7px 10px;
            }}
            QLabel#OperBlockStaffNameLabel {{
                background: transparent;
                color: #2c3e50;
                font-size: 12px;
                font-weight: 500;
                border: none;
                padding: 0 4px 0 18px;
                margin-left: 0px;
            }}
            QWidget#OperBlockStaffNames {{
                background: transparent;
                border: none;
            }}
            """
        )
        panel.hide()
        return panel

    @staticmethod
    def _normal_staff_name_list(value, *, split_commas: bool = False) -> list[str]:
        if value is None:
            raw_items = []
        elif isinstance(value, str):
            raw_items = [value]
        elif isinstance(value, dict):
            raw_items = []
        else:
            try:
                raw_items = list(value)
            except TypeError:
                raw_items = [value]
        result: list[str] = []
        seen: set[str] = set()
        for item in raw_items:
            parts = re.split(r"\s*,\s*", item) if split_commas and isinstance(item, str) else [item]
            for part in parts:
                name = normalize_operblock_team_text(part)
                key = name.casefold()
                if name and key not in seen:
                    seen.add(key)
                    result.append(name)
        return result

    def _staff_names_from_stage_state(self, *keys: str, split_commas: bool = False) -> list[str]:
        state = getattr(self, "_current_stage_state", {}) or {}
        for key in keys:
            names = self._normal_staff_name_list(state.get(key), split_commas=split_commas)
            if names:
                return names
        return []

    @staticmethod
    def _staff_legend_label(text: str, object_name: str, click_callback=None) -> QLabel:
        if callable(click_callback):
            label = OperBlockClickableLabel(str(text or ""), click_callback)
            label.setToolTip("Изменить состав")
        else:
            label = QLabel(str(text or ""))
        label.setObjectName(object_name)
        label.setWordWrap(True)
        label.setMinimumHeight(22 if object_name == "OperBlockStaffNameLabel" else 35)
        label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        return label

    def _add_staff_legend_section(self, title: str, names: list[str], *, force: bool = False) -> bool:
        layout = getattr(self, "vitals_staff_legend_layout", None)
        if layout is None or (not names and not force):
            return False
        edit_callback = self._open_operblock_staff_editor
        layout.addWidget(self._staff_legend_label(title, "OperBlockStaffRoleLabel", edit_callback))
        if names:
            names_widget = QWidget()
            names_widget.setObjectName("OperBlockStaffNames")
            names_layout = QVBoxLayout(names_widget)
            names_layout.setContentsMargins(0, 0, 0, 2)
            names_layout.setSpacing(2)
            for name in names:
                names_layout.addWidget(self._staff_legend_label(name, "OperBlockStaffNameLabel", edit_callback))
            layout.addWidget(names_widget)
        return True

    def _update_operblock_staff_legend(self) -> None:
        panel = getattr(self, "vitals_staff_legend_panel", None)
        layout = getattr(self, "vitals_staff_legend_layout", None)
        if panel is None or layout is None:
            return
        self._clear_layout(layout)
        separator = QFrame()
        separator.setObjectName("OperBlockStaffSeparator")
        layout.addWidget(separator)

        state = getattr(self, "_current_stage_state", {}) or {}
        surgery_active = bool(state.get("surgery_active"))
        anesthesia_active = bool(state.get("anesthesia_active"))
        surgeons = self._staff_names_from_stage_state(
            "current_surgeons",
            "last_surgeons",
            "first_surgeons",
            split_commas=True,
        )
        if not surgeons:
            surgeons = self._staff_names_from_stage_state(
                "current_surgeon",
                "last_surgeon",
                "first_surgeon",
                split_commas=True,
            )
        operating_nurses = self._staff_names_from_stage_state(
            "current_operating_nurse",
            "last_operating_nurse",
            "first_operating_nurse",
        )
        anesthesiologists = self._staff_names_from_stage_state(
            "current_anesthesiologist",
            "last_anesthesiologist",
            "first_anesthesiologist",
        )
        anesthetists = self._staff_names_from_stage_state(
            "current_anesthetist",
            "last_anesthetist",
            "first_anesthetist",
        )

        has_content = False
        has_content = self._add_staff_legend_section("Хирурги:", surgeons, force=surgery_active) or has_content
        has_content = self._add_staff_legend_section("Опер. сестра:", operating_nurses, force=surgery_active) or has_content
        has_content = self._add_staff_legend_section("Анестезиолог:", anesthesiologists, force=anesthesia_active) or has_content
        has_content = self._add_staff_legend_section("Анестезист:", anesthetists, force=anesthesia_active) or has_content
        panel.setVisible(has_content)

    def _open_operblock_staff_editor(self) -> None:
        if self._write_pending:
            return
        if not self._current_operation_case_id:
            return
        state = getattr(self, "_current_stage_state", {}) or {}
        surgery_enabled = bool(state.get("surgery_active"))
        anesthesia_enabled = bool(state.get("anesthesia_active"))
        if not surgery_enabled and not anesthesia_enabled:
            CustomMessageBox.warning(
                self,
                "Изменить состав",
                "Изменять состав можно только во время операции или анестезиологического пособия.",
            )
            self.refresh_protocol(force=True)
            return

        try:
            surgeon_options = load_operblock_surgeons()
            operating_nurse_options = load_operblock_operating_nurses()
            anesthesiologist_options = load_operblock_anesthesiologists()
            anesthetist_options = load_operblock_anesthetists()
        except Exception as exc:
            CustomMessageBox.warning(self, "Изменить состав", f"Не удалось загрузить сотрудников: {exc}")
            return

        current_surgeons = self._normal_staff_name_list(state.get("current_surgeons"), split_commas=True)
        if not current_surgeons:
            current_surgeons = self._normal_staff_name_list(state.get("current_surgeon"), split_commas=True)
        dialog = EditOperBlockStaffDialog(
            surgeon_options=surgeon_options,
            operating_nurse_options=operating_nurse_options,
            anesthesiologist_options=anesthesiologist_options,
            anesthetist_options=anesthetist_options,
            current_surgeons=current_surgeons,
            current_operating_nurse=normalize_operblock_team_text(state.get("current_operating_nurse")),
            current_anesthesiologist=normalize_operblock_team_text(state.get("current_anesthesiologist")),
            current_anesthetist=normalize_operblock_team_text(state.get("current_anesthetist")),
            surgery_enabled=surgery_enabled,
            anesthesia_enabled=anesthesia_enabled,
            parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return

        case_id = int(self._current_operation_case_id)
        payload: dict[str, object] = {}
        if surgery_enabled:
            payload["surgeons"] = dialog.selected_surgeons()
            payload["operating_nurse"] = dialog.selected_operating_nurse()
        if anesthesia_enabled:
            payload["anesthesiologist"] = dialog.selected_anesthesiologist()
            payload["anesthetist"] = dialog.selected_anesthetist()
        if not payload:
            return

        self._run_stage_action(
            f"operblock_update_staff:{case_id}",
            lambda: self.operblock_service.update_operation_staff(case_id, **payload),
        )

    def _orders_panel(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("operblockOrdersPanel")
        frame.setStyleSheet(
            f"""
            QFrame#operblockOrdersPanel {{
                background-color: {OPERBLOCK_ORDERS_CARD_BG};
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 6px;
            }}
            QLabel {{
                background: transparent;
                border: none;
                color: {OPERBLOCK_ORDERS_TEXT};
            }}
            """
        )
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)
        if title:
            label = QLabel(title)
            label.setStyleSheet(
                f"font-size: 15px; font-weight: 500; color: {OPERBLOCK_ORDERS_TEXT}; "
                "background: transparent; border: none;"
            )
            layout.addWidget(label)
        return frame, layout

    def _build_orders_tab(self) -> QWidget:
        metric_started = operblock_startup_metrics.timer_start()
        page = QWidget()
        page.setStyleSheet(f"QWidget {{ background-color: {OPERBLOCK_ORDERS_BG}; color: {OPERBLOCK_ORDERS_TEXT}; }}")
        outer_layout = QHBoxLayout(page)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        content_column = QWidget()
        content_column.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(content_column)
        layout.setContentsMargins(0, 3, 0, 5)
        layout.setSpacing(3)

        input_panel, input_body = self._orders_panel("")
        input_panel.setObjectName("operblockNewOrderPanel")
        input_panel.setStyleSheet(
            f"""
            QFrame#operblockNewOrderPanel {{
                background-color: {OPERBLOCK_ORDERS_BG};
                border: 1.5px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 5px;
            }}
            QLabel {{
                background: transparent;
                border: none;
                color: {OPERBLOCK_ORDERS_TEXT};
            }}
            """
        )
        input_body.setContentsMargins(14, 12, 14, 12)
        input_layout = QHBoxLayout()
        input_layout.setContentsMargins(0, 0, 0, 0)
        input_layout.setSpacing(10)
        input_title = QLabel("Новое назначение")
        input_title.setMinimumWidth(132)
        input_title.setStyleSheet(
            f"font-size: 14px; font-weight: 500; color: {OPERBLOCK_ORDERS_TEXT}; background: transparent; border: none;"
        )
        self.order_input = _line_edit()
        self.order_input.setPlaceholderText("Введите препарат или назначение")
        self.order_input.setMinimumHeight(40)
        self.order_input.setStyleSheet(
            f"""
            QLineEdit {{
                background-color: {OPERBLOCK_ORDERS_CARD_BG};
                color: {OPERBLOCK_ORDERS_TEXT};
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 8px;
                padding: 0 10px;
            }}
            QLineEdit:focus {{
                border: 1px solid {OPERBLOCK_ORDERS_ACCENT};
            }}
            """
        )
        self._add_line_edit_icon(self.order_input, "search")
        self.order_dose_input = _line_edit()
        self.order_dose_input.setPlaceholderText("Доза (мг, мл, %)")
        self.order_dose_input.setFixedWidth(190)
        self.order_dose_input.setMinimumHeight(40)
        self.order_dose_input.setStyleSheet(self.order_input.styleSheet())
        self.order_rate_input = _line_edit()
        self.order_rate_input.setPlaceholderText("Скорость (мл/час)")
        self.order_rate_input.setFixedWidth(145)
        self.order_rate_input.setMinimumHeight(40)
        self.order_rate_input.setStyleSheet(self.order_input.styleSheet())
        self.order_type_combo = QComboBox()
        self.order_type_combo.setFixedWidth(140)
        self.order_type_combo.setMinimumHeight(40)
        self.order_type_combo.addItems(("Болюс", "Газ", "Дозатор", "Капельница"))
        self.order_type_combo.setStyleSheet(
            _operblock_combo_box_style()
            + f"""
            QComboBox {{
                border-radius: 8px;
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                padding: 7px 30px 7px 10px;
            }}
            """
        )
        self.order_type_combo.currentTextChanged.connect(self._update_manual_order_type_fields)
        self.save_order_button = QPushButton("Добавить")
        self.save_order_button.setIcon(self._operblock_ui_icon("plus"))
        self.save_order_button.setIconSize(QSize(16, 16))
        self.save_order_button.setMinimumHeight(40)
        self.save_order_button.setMinimumWidth(122)
        self.save_order_button.setCursor(Qt.PointingHandCursor)
        self.save_order_button.setStyleSheet(_operblock_primary_action_button_style(radius=8, padding="8px 15px"))
        self.save_order_button.clicked.connect(self._save_order)
        input_layout.addWidget(input_title, 0)
        input_layout.addWidget(self.order_input, 1)
        input_layout.addWidget(self.order_dose_input, 0)
        input_layout.addWidget(self.order_rate_input, 0)
        input_layout.addWidget(self.order_type_combo, 0)
        input_layout.addWidget(self.save_order_button)
        input_body.addLayout(input_layout)
        self._update_manual_order_type_fields()
        layout.addWidget(input_panel, 0)

        active_panel, active_body = self._orders_panel("")
        self.active_infusions_panel = active_panel
        active_panel.setObjectName("operblockActiveInfusionsPanel")
        active_panel.setStyleSheet(
            f"""
            QFrame#operblockActiveInfusionsPanel {{
                background-color: {OPERBLOCK_ORDERS_BG};
                border: 1.5px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 5px;
            }}
            QLabel {{
                background: transparent;
                border: none;
                color: {OPERBLOCK_ORDERS_TEXT};
            }}
            """
        )
        active_body.setContentsMargins(14, 12, 14, 14)
        active_header = QHBoxLayout()
        active_header.setContentsMargins(0, 0, 0, 2)
        active_header.setSpacing(8)
        active_title = QLabel("Активные дозаторы, капельницы и газы")
        active_title.setStyleSheet(
            f"font-size: 15px; font-weight: 500; color: {OPERBLOCK_ORDERS_TEXT}; background: transparent; border: none;"
        )
        self.active_infusions_count_label = self._count_badge("0")
        active_header.addWidget(active_title, 0)
        active_header.addWidget(self.active_infusions_count_label, 0)
        active_header.addStretch(1)
        active_body.addLayout(active_header)

        self.active_infusions_scroll = QScrollArea()
        self.active_infusions_scroll.setObjectName("operblockActiveInfusionsScroll")
        self.active_infusions_scroll.setWidgetResizable(True)
        self.active_infusions_scroll.setFrameShape(QScrollArea.NoFrame)
        self.active_infusions_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.active_infusions_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.active_infusions_scroll.setMinimumHeight(OPERBLOCK_ACTIVE_INFUSION_EMPTY_HEIGHT)
        self.active_infusions_scroll.setMaximumHeight(OPERBLOCK_ACTIVE_INFUSION_CARD_MIN_HEIGHT)
        self.active_infusions_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.active_infusions_scroll.setStyleSheet(
            "QScrollArea#operblockActiveInfusionsScroll { background: transparent; border: none; }"
        )
        active_scroll_bar = self.active_infusions_scroll.verticalScrollBar()
        active_scroll_bar.setObjectName("OperBlockActiveInfusionsScrollBar")
        active_scroll_bar.setFixedWidth(14)
        active_scroll_bar.setStyleSheet(
            _operblock_vertical_scrollbar_style(
                "OperBlockActiveInfusionsScrollBar",
                width_px=14,
                left_margin_px=3,
                right_margin_px=2,
            )
        )
        self.active_infusions_content = QWidget()
        self.active_infusions_content.setMinimumHeight(OPERBLOCK_ACTIVE_INFUSION_EMPTY_HEIGHT)
        self.active_infusions_content.setStyleSheet("background: transparent;")
        self.active_infusions_list = QGridLayout(self.active_infusions_content)
        self.active_infusions_list.setContentsMargins(0, 0, 0, 0)
        self.active_infusions_list.setHorizontalSpacing(OPERBLOCK_ACTIVE_INFUSION_GRID_SPACING)
        self.active_infusions_list.setVerticalSpacing(OPERBLOCK_ACTIVE_INFUSION_GRID_SPACING)
        for column in range(OPERBLOCK_ACTIVE_INFUSION_COLUMNS):
            self.active_infusions_list.setColumnStretch(column, 1)
        self.active_infusions_scroll.setWidget(self.active_infusions_content)
        active_body.addWidget(self.active_infusions_scroll, 0)
        layout.addWidget(active_panel, 0)

        timeline_panel, timeline_body = self._orders_panel("")
        self.orders_timeline_panel = timeline_panel
        timeline_panel.setObjectName("operblockOrdersTimelinePanel")
        timeline_panel.setStyleSheet(
            f"""
            QFrame#operblockOrdersTimelinePanel {{
                background-color: {OPERBLOCK_ORDERS_BG};
                border: 1.5px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 5px;
            }}
            QLabel {{
                background: transparent;
                border: none;
                color: {OPERBLOCK_ORDERS_TEXT};
            }}
            """
        )
        timeline_body.setContentsMargins(14, 12, 14, 10)
        timeline_header = QHBoxLayout()
        timeline_header.setContentsMargins(0, 0, 0, 2)
        timeline_header.setSpacing(8)
        timeline_title = QLabel("Назначения")
        timeline_title.setStyleSheet(
            f"font-size: 15px; font-weight: 500; color: {OPERBLOCK_ORDERS_TEXT}; background: transparent; border: none;"
        )
        self.orders_count_label = self._count_badge("0")
        self.orders_filter_button = QPushButton("Фильтры")
        self.orders_filter_button.setIcon(self._operblock_ui_icon("filter"))
        self.orders_filter_button.setIconSize(QSize(15, 15))
        self.orders_filter_button.setFixedHeight(32)
        self.orders_filter_button.setCursor(Qt.PointingHandCursor)
        self.orders_filter_button.setStyleSheet(self._secondary_order_button_style())
        self.orders_filter_button.clicked.connect(self._show_orders_filter_menu)
        self.orders_sort_combo = QComboBox()
        self.orders_sort_combo.setFixedHeight(32)
        self.orders_sort_combo.setMinimumWidth(210)
        self.orders_sort_combo.addItems(("По времени (новые сверху)", "По времени (старые сверху)", "По препарату", "Только активные"))
        self.orders_sort_combo.setStyleSheet(_operblock_combo_box_style())
        timeline_header.addWidget(timeline_title, 0)
        timeline_header.addWidget(self.orders_count_label, 0)
        timeline_header.addStretch(1)
        timeline_header.addWidget(self.orders_filter_button, 0)
        timeline_header.addWidget(self.orders_sort_combo, 0)
        timeline_body.addLayout(timeline_header)
        self.orders_scroll = QScrollArea()
        self.orders_scroll.setWidgetResizable(True)
        self.orders_scroll.setFrameShape(QScrollArea.NoFrame)
        self.orders_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.orders_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.orders_scroll.setStyleSheet(
            """
            QScrollArea {{ background: transparent; border: none; }}
            """
        )
        orders_scroll_bar = self.orders_scroll.verticalScrollBar()
        orders_scroll_bar.setObjectName("OperBlockOrdersScrollBar")
        orders_scroll_bar.setFixedWidth(14)
        orders_scroll_bar.setStyleSheet(
            _operblock_vertical_scrollbar_style(
                "OperBlockOrdersScrollBar",
                width_px=14,
                left_margin_px=3,
                right_margin_px=2,
            )
        )
        orders_content = QWidget()
        orders_content.setStyleSheet("background: transparent;")
        self.orders_list = QVBoxLayout()
        self.orders_list.setContentsMargins(0, 0, 0, 0)
        self.orders_list.setSpacing(4)
        orders_content.setLayout(self.orders_list)
        self.orders_scroll.setWidget(orders_content)
        timeline_body.addWidget(self.orders_scroll, 1)
        layout.addWidget(timeline_panel, 1)

        outer_layout.addWidget(content_column, 1)

        quick_wrapper = QWidget()
        quick_wrapper.setStyleSheet("background: transparent;")
        quick_wrapper_layout = QVBoxLayout(quick_wrapper)
        quick_wrapper_layout.setContentsMargins(5, 3, 0, 5)
        quick_wrapper_layout.setSpacing(0)

        quick_panel, quick_body = self._orders_panel("Быстрые назначения")
        self.quick_orders_panel = quick_panel
        quick_panel.setObjectName("operblockQuickOrdersPanel")
        quick_panel.setStyleSheet(
            f"""
            QFrame#operblockQuickOrdersPanel {{
                background-color: {OPERBLOCK_ORDERS_BG};
                border: 1.5px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 5px;
            }}
            QLabel {{
                background: transparent;
                border: none;
                color: {OPERBLOCK_ORDERS_TEXT};
            }}
            """
        )
        quick_panel.setFixedWidth(286)
        quick_body.setSpacing(8)

        self.quick_orders_controls_panel = QFrame()
        self.quick_orders_controls_panel.setObjectName("operblockQuickOrdersControlsPanel")
        self.quick_orders_controls_panel.setStyleSheet(
            f"""
            QFrame#operblockQuickOrdersControlsPanel {{
                background-color: {OPERBLOCK_ORDERS_CARD_BG};
                border: 1.5px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 5px;
            }}
            """
        )
        quick_controls_layout = QVBoxLayout(self.quick_orders_controls_panel)
        quick_controls_layout.setContentsMargins(8, 8, 8, 8)
        quick_controls_layout.setSpacing(8)

        self.preset_search_input = QLineEdit()
        self.preset_search_input.setPlaceholderText("Найти препарат...")
        self.preset_search_input.setFixedHeight(32)
        self.preset_search_input.setStyleSheet(
            f"""
            QLineEdit {{
                background-color: {OPERBLOCK_ORDERS_CARD_BG};
                color: {OPERBLOCK_ORDERS_TEXT};
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 8px;
                padding: 0 9px;
            }}
            QLineEdit:focus {{ border: 1px solid {OPERBLOCK_ORDERS_ACCENT}; }}
            """
        )
        self.preset_search_input.textChanged.connect(self._on_preset_search_changed)
        quick_controls_layout.addWidget(self.preset_search_input)

        filter_row = QGridLayout()
        filter_row.setContentsMargins(0, 0, 0, 0)
        filter_row.setHorizontalSpacing(4)
        filter_row.setVerticalSpacing(4)
        filter_row.setColumnStretch(0, 1)
        filter_row.setColumnStretch(1, 1)
        self.preset_filter_group = QButtonGroup(self)
        self.preset_filter_group.setExclusive(True)
        for index, (filter_key, label) in enumerate(OPERBLOCK_TEMPLATE_FILTERS):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setChecked(filter_key == self._preset_kind_filter)
            button.setFixedHeight(28)
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(
                f"""
                QPushButton {{
                    background-color: #F8FAFC;
                    color: {OPERBLOCK_ORDERS_MUTED};
                    border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                    border-radius: 6px;
                    padding: 4px 6px;
                    font-size: 11px;
                    font-weight: 500;
                }}
                QPushButton:checked {{
                    background-color: #EEF3FF;
                    color: {OPERBLOCK_ORDERS_ACCENT};
                    border-color: {OPERBLOCK_ORDERS_BORDER};
                }}
                """
            )
            self.preset_filter_group.addButton(button, index)
            if filter_key == "favorite":
                filter_row.addWidget(button, index // 2, 0, 1, 2)
            else:
                filter_row.addWidget(button, index // 2, index % 2)
        self.preset_filter_group.idClicked.connect(self._on_preset_filter_changed)
        quick_controls_layout.addLayout(filter_row)

        self.preset_settings_button = QPushButton("Настроить препараты")
        self.preset_settings_button.setFixedHeight(32)
        self.preset_settings_button.setStyleSheet(
            f"""
            QPushButton {{
                background-color: #F8FAFC;
                color: {OPERBLOCK_ORDERS_TEXT};
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 7px;
                padding: 4px 8px;
                font-weight: 500;
            }}
            QPushButton:hover {{ background-color: #EEF3FF; }}
            """
        )
        self.preset_settings_button.clicked.connect(self._open_quick_orders_settings)
        self.preset_settings_button.setVisible(False)
        quick_controls_layout.addWidget(self.preset_settings_button)
        quick_body.addWidget(self.quick_orders_controls_panel, 0)

        quick_scroll = QScrollArea()
        quick_scroll.setObjectName("operblockQuickOrdersScroll")
        quick_scroll.setWidgetResizable(True)
        quick_scroll.setFrameShape(QScrollArea.NoFrame)
        quick_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        quick_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        quick_scroll.setStyleSheet("QScrollArea#operblockQuickOrdersScroll { background: transparent; border: none; }")
        self.quick_orders_scroll = quick_scroll
        quick_scroll_bar = quick_scroll.verticalScrollBar()
        quick_scroll_bar.setObjectName("OperBlockQuickOrdersScrollBar")
        quick_scroll_bar.setFixedWidth(14)
        quick_scroll_bar.setStyleSheet(
            _operblock_vertical_scrollbar_style(
                "OperBlockQuickOrdersScrollBar",
                width_px=14,
                left_margin_px=3,
                right_margin_px=2,
            )
        )
        quick_content = _QuickOrderPresetListWidget(self)
        quick_content.setStyleSheet("background: transparent;")
        self.quick_orders_list = QVBoxLayout(quick_content)
        self.quick_orders_list.setContentsMargins(0, 0, 0, 0)
        self.quick_orders_list.setSpacing(8)
        quick_scroll.setWidget(quick_content)
        quick_body.addWidget(quick_scroll, 1)
        quick_wrapper_layout.addWidget(quick_panel, 1)
        outer_layout.addWidget(quick_wrapper, 0)

        self._refresh_quick_orders()
        operblock_startup_metrics.record_since("build_orders_tab_ms", metric_started, source="operblock_widget")
        if getattr(self, "_creating_lazy_protocol_page", False):
            operblock_startup_metrics.record_since(
                "orders_tab_lazy_created_ms",
                metric_started,
                source="operblock_widget",
            )
        return page

    def _operblock_ui_icon(self, name: str) -> QIcon:
        path = os.path.join(get_icon_dir(), f"operblock_{name}.svg")
        return QIcon(path) if os.path.exists(path) else QIcon()

    def _add_line_edit_icon(self, line_edit: QLineEdit, icon_name: str) -> None:
        icon = self._operblock_ui_icon(icon_name)
        if icon.isNull():
            return
        try:
            position = QLineEdit.ActionPosition.LeadingPosition
        except AttributeError:
            position = QLineEdit.LeadingPosition
        line_edit.addAction(icon, position)

    @staticmethod
    def _count_badge(text: str) -> QLabel:
        badge = QLabel(str(text or "0"))
        badge.setAlignment(Qt.AlignCenter)
        badge.setMinimumWidth(24)
        badge.setFixedHeight(22)
        badge.setStyleSheet(
            f"font-size: 12px; font-weight: 500; color: #1D4ED8; background-color: #DBEAFE; "
            f"border: 1px solid {OPERBLOCK_ORDERS_BORDER}; border-radius: 11px; padding: 0 7px;"
        )
        return badge

    @staticmethod
    def _secondary_order_button_style() -> str:
        return f"""
            QPushButton {{
                background-color: #FFFFFF;
                color: {OPERBLOCK_ORDERS_TEXT};
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 7px;
                padding: 5px 10px;
                font-size: 12px;
                font-weight: 500;
            }}
            QPushButton:hover {{
                background-color: #F8FAFC;
                border-color: {OPERBLOCK_ORDERS_BORDER};
            }}
            QPushButton:disabled {{
                color: {OPERBLOCK_ORDERS_MUTED};
                background-color: #F1F5F9;
            }}
        """

    def _show_orders_filter_menu(self):
        menu = self._actions_menu()
        current_filter = str(getattr(self, "_orders_filter_kind", "all") or "all")
        for filter_key, title in OPERBLOCK_ORDERS_FILTERS:
            action = menu.addAction(title)
            action.setCheckable(True)
            action.setChecked(filter_key == current_filter)
            action.triggered.connect(lambda _checked=False, key=filter_key: self._set_orders_filter(key))
        menu.addSeparator()
        hide_deleted_action = menu.addAction("Удалённые скрыть")
        hide_deleted_action.setCheckable(True)
        hide_deleted_action.setChecked(bool(getattr(self, "_orders_hide_deleted", True)))
        hide_deleted_action.triggered.connect(self._toggle_orders_hide_deleted)
        menu.exec(self.orders_filter_button.mapToGlobal(self.orders_filter_button.rect().bottomLeft()))

    def _set_orders_filter(self, filter_key: str):
        normalized = str(filter_key or "all").strip()
        allowed = {key for key, _title in OPERBLOCK_ORDERS_FILTERS}
        self._orders_filter_kind = normalized if normalized in allowed else "all"
        self._update_orders_filter_button_text()
        self._apply_orders({"orders": getattr(self, "_current_orders_rows", [])})

    def _toggle_orders_hide_deleted(self, checked: bool):
        self._orders_hide_deleted = bool(checked)
        self._apply_orders({"orders": getattr(self, "_current_orders_rows", [])})

    def _update_orders_filter_button_text(self):
        filter_key = str(getattr(self, "_orders_filter_kind", "all") or "all")
        title_by_key = dict(OPERBLOCK_ORDERS_FILTERS)
        title = title_by_key.get(filter_key, "Все")
        if filter_key == "all":
            self.orders_filter_button.setText("Фильтры")
        else:
            self.orders_filter_button.setText(f"Фильтры: {title}")

    def _placeholder_frame(self, title: str, rows: list[str]) -> QWidget:
        widget, body_layout = self._sector(title)
        body_layout.setContentsMargins(10, 10, 10, 10)
        for row in rows:
            body_layout.addWidget(_label(row, size=12, color=TEXT_SECONDARY))
        body_layout.addStretch(1)
        return widget

    def _sector(self, title: str) -> tuple[QWidget, QVBoxLayout]:
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        header = QLabel(title)
        header.setFixedHeight(28)
        header.setAlignment(Qt.AlignCenter)
        header.setStyleSheet(SECTOR_HEADER_STYLE)
        body = QFrame()
        body.setStyleSheet(SECTOR_BODY_STYLE)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        layout.addWidget(header)
        layout.addWidget(body, 1)
        return wrapper, body_layout

    def _connect_updates(self):
        if self.data_service and hasattr(self.data_service, "changes_detected"):
            self.data_service.changes_detected.connect(self._on_changes_detected)

    def start_auto_refresh(self, *, wake_monitor: bool = True):
        if self._is_closing:
            return
        metric_started = operblock_startup_metrics.timer_start()
        operblock_startup_metrics.record_value(
            "start_auto_refresh_nested_phase",
            "wrapper:refresh_board",
            source="operblock_widget",
        )
        try:
            if self.data_service and hasattr(self.data_service, "set_change_monitor_enabled"):
                self.data_service.set_change_monitor_enabled(False)
            self.refresh_board(force=True, refresh_reason="start_auto_refresh")
            if self.data_service and wake_monitor:
                self.data_service.request_immediate_refresh(force_emit=False, source="operblock_start")
        finally:
            operblock_startup_metrics.record_since(
                "start_auto_refresh_ms",
                metric_started,
                source="operblock_widget",
                nested_role="wrapper",
                child_phase="first_refresh_board_ms",
            )

    def auto_refresh(self, force: bool = False):
        if self._is_closing:
            return
        current_widget = self.stack.currentWidget()
        if self.protocol_page is not None and current_widget == self.protocol_page and self._current_operation_case_id:
            self.refresh_protocol(force=force)
        elif self.archive_page is not None and current_widget == self.archive_page:
            self.refresh_operblock_archive(force=force)
        else:
            self.refresh_board(force=force, refresh_reason="auto_refresh")

    def apply_operblock_icon_settings(self):
        self._orders_render_signature = ""
        self._active_infusions_render_signature = ""
        self._rendered_medication_group_signatures.clear()
        self._rendered_active_infusion_signatures.clear()
        if self.protocol_page is not None and self.stack.currentWidget() == self.protocol_page and self._current_operation_case_id:
            self.refresh_protocol(force=True)
        elif getattr(self, "quick_orders_list", None) is not None:
            self._refresh_quick_orders()

    def refresh_board(self, *, force: bool = False, refresh_reason: str = "refresh_board"):
        if self._is_closing:
            return
        enter_started = operblock_startup_metrics.timer_start()
        self._board_refresh_seq += 1
        refresh_seq = self._board_refresh_seq
        self._board_refresh_count_before_ready += 1
        is_first_refresh = not getattr(self, "_operblock_startup_first_refresh_recorded", False)
        refresh_fields = {
            "refresh_seq": refresh_seq,
            "refresh_reason": str(refresh_reason or "refresh_board"),
            "force": bool(force),
            "is_first": bool(is_first_refresh),
            "is_first_refresh": bool(is_first_refresh),
        }
        operblock_startup_metrics.record_value("refresh_seq", refresh_seq, source="operblock_widget", **refresh_fields)
        operblock_startup_metrics.record_value(
            "refresh_count_before_ready",
            self._board_refresh_count_before_ready,
            source="operblock_widget",
            **refresh_fields,
        )
        operblock_startup_metrics.record_value(
            "refresh_reason",
            refresh_fields["refresh_reason"],
            source="operblock_widget",
            **refresh_fields,
        )
        operblock_startup_metrics.record_value("refresh_force", bool(force), source="operblock_widget", **refresh_fields)
        operblock_startup_metrics.record_value(
            "refresh_is_first_refresh",
            bool(is_first_refresh),
            source="operblock_widget",
            **refresh_fields,
        )
        operblock_startup_metrics.record_value(
            "refresh_is_first",
            bool(is_first_refresh),
            source="operblock_widget",
            **refresh_fields,
        )
        operblock_startup_metrics.record_since("refresh_board_enter_ms", enter_started, source="operblock_widget", **refresh_fields)
        first_refresh_started = None
        if not getattr(self, "_operblock_startup_first_refresh_recorded", False):
            first_refresh_started = operblock_startup_metrics.timer_start()
            self._operblock_startup_first_refresh_recorded = True
        try:
            try:
                snapshot_started = operblock_startup_metrics.timer_start()
                snapshot = self.operblock_service.build_operblock_board_snapshot()
            except Exception as exc:
                logger.error("operblock board refresh failed: %s", exc, exc_info=True)
                CustomMessageBox.warning(self, "Ошибка чтения БД", str(exc))
                return
            finally:
                operblock_startup_metrics.record_since(
                    "refresh_board_snapshot_ms",
                    snapshot_started if "snapshot_started" in locals() else 0.0,
                    source="operblock_widget",
                    nested_role="child",
                    parent_phase="first_refresh_board_ms",
                    **refresh_fields,
                )
            if not force and snapshot.get("content_hash") == self._board_hash:
                return
            self._board_hash = snapshot.get("content_hash") or ""
            apply_started = operblock_startup_metrics.timer_start()
            self._apply_board_snapshot(snapshot, refresh_context=refresh_fields)
            operblock_startup_metrics.record_since(
                "refresh_board_apply_total_ms",
                apply_started,
                source="operblock_widget",
                nested_role="child",
                parent_phase="first_refresh_board_ms",
                **refresh_fields,
            )
        finally:
            operblock_startup_metrics.record_since(
                "first_refresh_board_ms",
                first_refresh_started,
                source="operblock_widget",
                nested_role="child",
                parent_phase="start_auto_refresh_ms",
                **refresh_fields,
            )

    def refresh_operblock_archive(self, *, force: bool = False):
        if self._is_closing:
            return
        try:
            cases = self.operblock_service.list_archived_operation_cases()
        except Exception as exc:
            logger.error("operblock archive refresh failed: %s", exc, exc_info=True)
            CustomMessageBox.warning(self, "Архив оперблока", f"Не удалось обновить архив:\n{exc}")
            return
        source_hash = _stable_ui_hash(cases)
        if not force and source_hash == getattr(self, "_archive_cases_hash", ""):
            return
        self._archive_cases_hash = source_hash
        self._archive_cases = [dict(item or {}) for item in cases]
        self._apply_operblock_archive_cases()

    def refresh_protocol(self, *, force: bool = False):
        if self._is_closing or not self._current_operation_case_id:
            return
        generation = self._refresh_generation = self._refresh_generation + 1
        try:
            snapshot = self.operblock_service.build_operblock_protocol_snapshot(self._current_operation_case_id)
        except OperBlockConflictError as exc:
            CustomMessageBox.warning(self, "Оперблок", str(exc))
            self._show_board()
            return
        except Exception as exc:
            logger.error("operblock protocol refresh failed: %s", exc, exc_info=True)
            CustomMessageBox.warning(self, "Ошибка", f"Не удалось обновить протокол:\n{exc}")
            return
        if self._is_closing or generation != self._refresh_generation:
            return
        if not force and snapshot.get("content_hash") == self._protocol_hash:
            return
        self._protocol_hash = snapshot.get("content_hash") or ""
        self._apply_protocol_snapshot(snapshot)

    @staticmethod
    def _empty_board_table_payload(table: dict) -> dict:
        return {
            "code": str(table.get("code") or ""),
            "display_name": table.get("display_name") or "",
            "sort_order": table.get("sort_order"),
            "occupied": False,
            "patient": None,
        }

    @staticmethod
    def _board_table_content_hash(table: dict) -> str:
        return _stable_ui_hash(
            {
                "code": str(table.get("code") or ""),
                "display_name": table.get("display_name") or "",
                "sort_order": table.get("sort_order"),
                "occupied": bool(table.get("occupied")),
                "patient": table.get("patient") or None,
            }
        )

    def _board_card_layout_index(self, widget: QWidget | None) -> int:
        if widget is None:
            return -1
        layout = getattr(self, "cards_layout", None)
        if layout is None:
            return -1
        for index in range(layout.count()):
            item = layout.itemAt(index)
            if item is not None and item.widget() is widget:
                return index
        return -1

    def _remove_board_card_widget(self, widget: QWidget | None) -> bool:
        if widget is None:
            return False
        if getattr(self, "cards_layout", None) is not None:
            self.cards_layout.removeWidget(widget)
        widget.setParent(None)
        return True

    def _apply_board_snapshot(self, snapshot: dict, *, refresh_context: dict | None = None):
        metric_started = operblock_startup_metrics.timer_start()
        refresh_fields = dict(refresh_context or {})
        tables = list(snapshot.get("tables", []) or [])
        occupied_count = sum(1 for table in tables if table.get("occupied"))
        empty_count = max(0, len(tables) - occupied_count)
        apply_fields = {
            **refresh_fields,
            "nested_role": "child",
            "parent_phase": "apply_board_snapshot_ms",
        }
        apply_metrics = {
            "photo_count": 0,
            "missing_photo_count": 0,
            "current_card_fields": {},
            "photo_cache_hit_count": 0,
            "photo_cache_miss_count": 0,
        }
        previous_apply_metrics = self._current_board_apply_metrics
        self._current_board_apply_metrics = apply_metrics
        try:
            operblock_startup_metrics.record_value("board_apply_table_count", len(tables), source="operblock_widget", **apply_fields)
            operblock_startup_metrics.record_value("board_apply_occupied_count", occupied_count, source="operblock_widget", **apply_fields)
            operblock_startup_metrics.record_value("board_apply_empty_count", empty_count, source="operblock_widget", **apply_fields)
            snapshot_codes = {str(table.get("code") or "") for table in tables}
            clear_started = operblock_startup_metrics.timer_start()
            remove_started = operblock_startup_metrics.timer_start()
            removed_count = 0
            for table_code, widget in list(self._table_cards.items()):
                if table_code in snapshot_codes:
                    continue
                if self._remove_board_card_widget(widget):
                    removed_count += 1
                self._table_cards.pop(table_code, None)
                self._board_card_hashes.pop(table_code, None)
                self._board_card_states.pop(table_code, None)
            operblock_startup_metrics.record_since(
                "board_apply_clear_remove_widgets_ms",
                remove_started,
                source="operblock_widget",
                removed_count=removed_count,
                **apply_fields,
            )
            operblock_startup_metrics.record_duration(
                "board_apply_clear_delete_later_ms",
                0.0,
                source="operblock_widget",
                reason="delete_later_not_used",
                **apply_fields,
            )
            operblock_startup_metrics.record_since("board_apply_clear_total_ms", clear_started, source="operblock_widget", **apply_fields)

            loop_started = operblock_startup_metrics.timer_start()
            recreated_count = 0
            reused_count = 0
            skipped_unchanged_count = 0
            replaced_count = 0
            updated_count = 0
            relayout_count = 0
            relayout_elapsed_recorded = False
            for target_index, table in enumerate(tables):
                table_code = str(table.get("code") or "")
                table_hash = self._board_table_content_hash(table)
                card_kind = "occupied" if table.get("occupied") else "empty"
                previous_card = self._table_cards.get(table_code)
                previous_hash = self._board_card_hashes.get(table_code, "")
                previous_state = self._board_card_states.get(table_code) or {}
                card_fields = {
                    **refresh_fields,
                    "table_code": table_code,
                    "card_kind": card_kind,
                    "nested_role": "child",
                    "parent_phase": "board_apply_card_loop_total_ms",
                }
                card_inner_fields = {
                    **refresh_fields,
                    "table_code": table_code,
                    "card_kind": card_kind,
                    "nested_role": "child",
                    "parent_phase": (
                        "board_apply_make_occupied_card_ms" if table.get("occupied") else "board_apply_make_empty_card_ms"
                    ),
                }
                if previous_card is not None and previous_hash == table_hash:
                    reused_count += 1
                    skipped_unchanged_count += 1
                    if previous_state.get("has_photo"):
                        apply_metrics["photo_cache_hit_count"] = int(apply_metrics.get("photo_cache_hit_count") or 0) + 1
                    current_index = self._board_card_layout_index(previous_card)
                    if current_index != target_index:
                        move_started = operblock_startup_metrics.timer_start()
                        if current_index >= 0:
                            self.cards_layout.removeWidget(previous_card)
                        self.cards_layout.insertWidget(target_index, previous_card, 1)
                        operblock_startup_metrics.record_since(
                            "board_apply_order_relayout_ms",
                            move_started,
                            source="operblock_widget",
                            **card_fields,
                        )
                        relayout_elapsed_recorded = True
                        relayout_count += 1
                    continue

                apply_metrics["current_card_fields"] = card_inner_fields
                if previous_card is not None:
                    self._remove_board_card_widget(previous_card)
                    replaced_count += 1
                    updated_count += 1
                if table.get("occupied"):
                    card_started = operblock_startup_metrics.timer_start()
                    card = self._make_occupied_table_card(table)
                    operblock_startup_metrics.record_since(
                        "board_apply_make_occupied_card_ms",
                        card_started,
                        source="operblock_widget",
                        **card_fields,
                    )
                else:
                    card_started = operblock_startup_metrics.timer_start()
                    card = self._make_empty_table_card(table["code"], table["display_name"])
                    operblock_startup_metrics.record_since(
                        "board_apply_make_empty_card_ms",
                        card_started,
                        source="operblock_widget",
                        **card_fields,
                    )
                self._table_cards[table_code] = card
                self._board_card_hashes[table_code] = table_hash
                self._board_card_states[table_code] = {
                    "kind": card_kind,
                    "content_hash": table_hash,
                    "has_photo": True,
                }
                layout_add_started = operblock_startup_metrics.timer_start()
                self.cards_layout.insertWidget(target_index, card, 1)
                operblock_startup_metrics.record_since("board_apply_layout_add_ms", layout_add_started, source="operblock_widget", **card_fields)
                recreated_count += 1
            apply_metrics["current_card_fields"] = {}
            if not relayout_elapsed_recorded:
                operblock_startup_metrics.record_duration(
                    "board_apply_order_relayout_ms",
                    0.0,
                    source="operblock_widget",
                    **apply_fields,
                )
            operblock_startup_metrics.record_since("board_apply_card_loop_total_ms", loop_started, source="operblock_widget", **apply_fields)

            after_loop_started = operblock_startup_metrics.timer_start()
            operblock_startup_metrics.record_value(
                "board_apply_card_recreated_count",
                recreated_count,
                source="operblock_widget",
                **apply_fields,
            )
            operblock_startup_metrics.record_value(
                "board_apply_card_reused_count",
                reused_count,
                source="operblock_widget",
                **apply_fields,
            )
            operblock_startup_metrics.record_value(
                "board_apply_card_skipped_unchanged_count",
                skipped_unchanged_count,
                source="operblock_widget",
                **apply_fields,
            )
            operblock_startup_metrics.record_value(
                "board_apply_card_replaced_count",
                replaced_count,
                source="operblock_widget",
                **apply_fields,
            )
            operblock_startup_metrics.record_value(
                "board_apply_card_updated_count",
                updated_count,
                source="operblock_widget",
                **apply_fields,
            )
            operblock_startup_metrics.record_value(
                "board_apply_order_relayout_count",
                relayout_count,
                source="operblock_widget",
                **apply_fields,
            )
            operblock_startup_metrics.record_value(
                "board_apply_photo_count",
                int(apply_metrics.get("photo_count") or 0),
                source="operblock_widget",
                **apply_fields,
            )
            operblock_startup_metrics.record_value(
                "board_apply_missing_photo_count",
                int(apply_metrics.get("missing_photo_count") or 0),
                source="operblock_widget",
                **apply_fields,
            )
            operblock_startup_metrics.record_value(
                "board_apply_photo_cache_hit_count",
                int(apply_metrics.get("photo_cache_hit_count") or 0),
                source="operblock_widget",
                **apply_fields,
            )
            operblock_startup_metrics.record_value(
                "board_apply_photo_cache_miss_count",
                int(apply_metrics.get("photo_cache_miss_count") or 0),
                source="operblock_widget",
                **apply_fields,
            )
            operblock_startup_metrics.record_since("board_apply_after_loop_ms", after_loop_started, source="operblock_widget", **apply_fields)
        finally:
            self._current_board_apply_metrics = previous_apply_metrics
            operblock_startup_metrics.record_since(
                "apply_board_snapshot_ms",
                metric_started,
                source="operblock_widget",
                nested_role="child",
                parent_phase="first_refresh_board_ms",
                **refresh_fields,
            )

    def _show_operblock_archive(self):
        if self._is_closing or self._write_pending:
            return
        first_open = self.archive_page is None
        first_open_started = operblock_startup_metrics.timer_start() if first_open else None
        if not self._ensure_archive_page_created():
            return
        self._set_protocol_chrome(True)
        self.stack.setCurrentWidget(self.archive_page)
        self._current_operation_case_id = None
        self._current_admission_id = None
        self._current_operation_start = None
        self._current_operation_end = None
        self._current_case_active = False
        self._current_stage_state = {}
        self._current_anesthesia_active = False
        self._current_surgery_active = False
        self._current_anesthesia_assistance_type = ""
        self._current_operation_name = ""
        self._update_protocol_title_label()
        self._update_operblock_staff_legend()
        self.refresh_operblock_archive(force=True)
        if first_open:
            operblock_startup_metrics.record_since(
                "first_open_archive_ms",
                first_open_started,
                source="operblock_widget",
            )

    def _filtered_archive_cases(self) -> list[dict]:
        query = str(getattr(self, "archive_search_input", None).text() if hasattr(self, "archive_search_input") else "").strip().casefold()
        cases = list(getattr(self, "_archive_cases", []) or [])
        if not query:
            return cases
        result = []
        for case in cases:
            haystack = " ".join(
                [
                    str(case.get("full_name") or ""),
                    str(case.get("history_number") or ""),
                    str(case.get("diagnosis_code") or ""),
                    str(case.get("diagnosis_text") or ""),
                    str(case.get("table_display_name") or ""),
                ]
            ).casefold()
            if query in haystack:
                result.append(case)
        return result

    def _apply_operblock_archive_cases(self):
        table = getattr(self, "archive_table", None)
        if table is None:
            return
        cases = self._filtered_archive_cases()
        table.setRowCount(0)
        table.setRowCount(len(cases))
        for row, case in enumerate(cases):
            diagnosis_text = str(case.get("diagnosis_text") or "—")
            diagnosis_code = str(case.get("diagnosis_code") or "").strip()
            if diagnosis_code:
                diagnosis_text = f"{diagnosis_code}: {diagnosis_text}"
            values = [
                case.get("table_display_name") or "—",
                case.get("full_name") or "Неизвестно",
                case.get("history_number") or "",
                diagnosis_text,
                _format_dt(case.get("started_at")),
                _format_dt(case.get("ended_at")),
                "В архиве",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setData(Qt.UserRole, dict(case))
                table.setItem(row, column, item)
        self._update_operblock_archive_buttons()

    def _archive_case_from_row(self, row: int) -> dict | None:
        table = getattr(self, "archive_table", None)
        if table is None or row < 0:
            return None
        item = table.item(row, 0)
        if item is None:
            return None
        data = item.data(Qt.UserRole)
        return dict(data or {}) if isinstance(data, dict) else None

    def _selected_archive_case(self) -> dict | None:
        table = getattr(self, "archive_table", None)
        if table is None:
            return None
        return self._archive_case_from_row(table.currentRow())

    def _update_operblock_archive_buttons(self):
        selected = self._selected_archive_case()
        enabled = bool(selected) and not self._write_pending
        has_cases = bool(getattr(self, "_archive_cases", None)) and not self._write_pending
        if hasattr(self, "archive_open_button"):
            self.archive_open_button.setEnabled(enabled)
        if hasattr(self, "archive_restore_button"):
            self.archive_restore_button.setEnabled(enabled)
        if hasattr(self, "archive_delete_button"):
            self.archive_delete_button.setEnabled(enabled)
        if hasattr(self, "archive_delete_all_button"):
            self.archive_delete_all_button.setEnabled(has_cases)

    def _open_selected_archive_case(self):
        selected = self._selected_archive_case()
        case_id = _safe_int((selected or {}).get("operation_case_id"))
        if case_id:
            self._open_protocol(case_id)

    def _restore_selected_archive_case(self):
        selected = self._selected_archive_case()
        case_id = _safe_int((selected or {}).get("operation_case_id"))
        if not case_id or self._write_pending:
            return
        reply = CustomMessageBox.question(
            self,
            "Возврат из архива",
            "Вернуть пациента на операционный стол?",
            CustomMessageBox.Yes | CustomMessageBox.No,
            CustomMessageBox.No,
        )
        if reply != CustomMessageBox.Yes:
            return
        self._write_pending = True
        self._update_operblock_archive_buttons()

        def operation():
            return self.operblock_service.restore_archived_operation_case(case_id)

        self._enqueue_write(
            f"operblock_restore_archive_case:{case_id}",
            operation,
            on_success=lambda result: self._on_restore_archive_case_success(result),
            on_error=lambda exc: self._on_restore_archive_case_error(exc),
        )

    def _on_restore_archive_case_success(self, result):
        self._write_pending = False
        self.refresh_board(force=True)
        self.refresh_operblock_archive(force=True)
        case_id = _safe_int((result or {}).get("operation_case_id"))
        if case_id:
            self._open_protocol(case_id)

    def _on_restore_archive_case_error(self, exc: Exception):
        self._write_pending = False
        CustomMessageBox.warning(self, "Возврат из архива", str(exc))
        self.refresh_operblock_archive(force=True)

    def _delete_selected_archive_case(self):
        selected = self._selected_archive_case()
        case_id = _safe_int((selected or {}).get("operation_case_id"))
        if not case_id or self._write_pending:
            return
        patient_name = str((selected or {}).get("full_name") or "выбранного пациента")
        reply = CustomMessageBox.question(
            self,
            "Удаление из архива",
            f"Действительно удалить из архива пациента {patient_name}?",
            CustomMessageBox.Yes | CustomMessageBox.No,
            CustomMessageBox.No,
        )
        if reply != CustomMessageBox.Yes:
            return
        self._write_pending = True
        self._update_operblock_archive_buttons()

        def operation():
            return self.operblock_service.delete_archived_operation_case(case_id)

        self._enqueue_write(
            f"operblock_delete_archive_case:{case_id}",
            operation,
            on_success=lambda _result: self._on_delete_archive_case_success(),
            on_error=lambda exc: self._on_delete_archive_case_error(exc),
        )

    def _delete_all_archive_cases(self):
        if self._write_pending:
            return
        count = len(getattr(self, "_archive_cases", []) or [])
        if count <= 0:
            return
        reply = CustomMessageBox.question(
            self,
            "Удаление архива",
            f"Действительно удалить всех пациентов из архива операционной? Количество: {count}.",
            CustomMessageBox.Yes | CustomMessageBox.No,
            CustomMessageBox.No,
        )
        if reply != CustomMessageBox.Yes:
            return
        self._write_pending = True
        self._update_operblock_archive_buttons()

        self._enqueue_write(
            "operblock_delete_all_archive_cases",
            lambda: self.operblock_service.delete_all_archived_operation_cases(),
            on_success=lambda _result: self._on_delete_archive_case_success(),
            on_error=lambda exc: self._on_delete_archive_case_error(exc),
        )

    def _on_delete_archive_case_success(self):
        self._write_pending = False
        self._archive_cases_hash = ""
        self.refresh_board(force=True)
        self.refresh_operblock_archive(force=True)

    def _on_delete_archive_case_error(self, exc: Exception):
        self._write_pending = False
        CustomMessageBox.warning(self, "Удаление из архива", str(exc))
        self.refresh_operblock_archive(force=True)

    def _make_empty_table_card(self, table_code: str, display_name: str) -> QFrame:
        apply_metrics = self._current_board_apply_metrics
        metric_fields = dict((apply_metrics or {}).get("current_card_fields") or {})
        frame = self._base_card()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        header = self._card_header(display_name)
        layout.addWidget(header)

        body_started = operblock_startup_metrics.timer_start() if apply_metrics is not None else 0.0
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(34, 34, 34, 34)
        body_layout.setSpacing(18)
        body_layout.addStretch(1)

        photo = QLabel()
        photo.setFixedSize(240, 240)
        photo.setAlignment(Qt.AlignCenter)
        self._set_patient_photo(photo, None)
        body_layout.addWidget(photo, 0, Qt.AlignCenter)

        free = QLabel("МЕСТО СВОБОДНО")
        free.setAlignment(Qt.AlignCenter)
        free.setStyleSheet(f"color: {COLOR_SUCCESS}; font-size: 24px; font-weight: 800; background: transparent;")
        body_layout.addWidget(free)

        button = QPushButton("ЗАНЯТЬ СТОЛ")
        button.setFixedHeight(62)
        button.setCursor(Qt.PointingHandCursor)
        button.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {BG_LIGHT};
                color: {TEXT_PRIMARY};
                border: 1px solid {BORDER_COLOR};
                border-radius: 8px;
                font-weight: 800;
                font-size: 18px;
            }}
            QPushButton:hover {{
                background-color: #d8dde2;
            }}
            """
        )
        button.clicked.connect(lambda _=False, code=table_code, name=display_name: self._open_occupy_dialog(code, name))
        body_layout.addWidget(button)
        body_layout.addStretch(1)
        layout.addWidget(body, 1)
        operblock_startup_metrics.record_since(
            "board_apply_card_body_ms",
            body_started,
            source="operblock_widget",
            **metric_fields,
        )
        return frame

    def _make_occupied_table_card(self, table: dict) -> QFrame:
        apply_metrics = self._current_board_apply_metrics
        metric_fields = dict((apply_metrics or {}).get("current_card_fields") or {})
        patient = table.get("patient") or {}
        frame = self._base_card()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._card_header(table.get("display_name") or ""))

        body_started = operblock_startup_metrics.timer_start() if apply_metrics is not None else 0.0
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(30, 28, 30, 28)
        body_layout.setSpacing(18)

        preview = QHBoxLayout()
        preview.setSpacing(26)
        photo = QLabel()
        photo.setFixedSize(260, 260)
        photo.setAlignment(Qt.AlignCenter)
        self._set_patient_photo(photo, patient.get("gender"))
        preview.addWidget(photo, 0, Qt.AlignTop)

        info = QVBoxLayout()
        info.setSpacing(12)
        status = QLabel("В ОПЕРАЦИОННОЙ")
        status.setStyleSheet(
            "color: white; font-weight: 800; font-size: 14px; "
            "background-color: #e74c3c; border-radius: 4px; padding: 4px 10px;"
        )
        status.setFixedWidth(180)
        status.setAlignment(Qt.AlignCenter)
        info.addWidget(status, 0, Qt.AlignLeft)
        info.addWidget(_label(f"ИБ № {patient.get('history_number') or '—'}", size=18, weight=800, color=COLOR_PRIMARY_DARK))
        info.addWidget(_label(patient.get("full_name") or "Неизвестно", size=27, weight=800))
        info.addWidget(_label(f"Возраст: {patient.get('age') or '—'}", size=20))
        info.addWidget(_label(f"Пол: {patient.get('gender') or '—'}", size=18))
        diagnosis_line = patient.get("diagnosis_text") or "—"
        code = patient.get("diagnosis_code")
        if code:
            diagnosis_line = f"{code}: {diagnosis_line}"
        diagnosis = _label(f"Диагноз: {diagnosis_line}", size=18)
        diagnosis.setMinimumHeight(54)
        info.addWidget(diagnosis)
        info.addStretch(1)
        preview.addLayout(info, 1)
        body_layout.addLayout(preview, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        open_btn = QPushButton("ОТКРЫТЬ КАРТОЧКУ")
        close_btn = QPushButton("ОСВОБОДИТЬ СТОЛ")
        for button in (open_btn, close_btn):
            button.setFixedHeight(56)
            button.setCursor(Qt.PointingHandCursor)
        open_btn.setStyleSheet(STYLE_SECTOR8_BUTTON)
        close_btn.setStyleSheet(DANGER_BUTTON_STYLE)
        open_btn.clicked.connect(
            lambda _=False, case_id=patient.get("operation_case_id"): self._open_protocol(int(case_id))
        )
        close_btn.clicked.connect(
            lambda _=False, case_id=patient.get("operation_case_id"): self._confirm_release_case(int(case_id))
        )
        buttons.addWidget(open_btn, 1)
        buttons.addWidget(close_btn, 2)
        body_layout.addLayout(buttons)
        layout.addWidget(body, 1)
        operblock_startup_metrics.record_since(
            "board_apply_card_body_ms",
            body_started,
            source="operblock_widget",
            **metric_fields,
        )
        return frame

    def _card_header(self, display_name: str) -> QLabel:
        apply_metrics = self._current_board_apply_metrics
        metric_fields = dict((apply_metrics or {}).get("current_card_fields") or {})
        metric_started = operblock_startup_metrics.timer_start() if apply_metrics is not None else 0.0
        label = QLabel(display_name)
        label.setFixedHeight(54)
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet(
            f"""
            QLabel {{
                background-color: {BG_LIGHT};
                color: {TEXT_PRIMARY};
                font-size: 24px;
                font-weight: 800;
                border-bottom: 1px solid {BORDER_COLOR};
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
            }}
            """
        )
        operblock_startup_metrics.record_since(
            "board_apply_card_header_ms",
            metric_started,
            source="operblock_widget",
            **metric_fields,
        )
        return label

    def _base_card(self) -> QFrame:
        apply_metrics = self._current_board_apply_metrics
        metric_fields = dict((apply_metrics or {}).get("current_card_fields") or {})
        metric_started = operblock_startup_metrics.timer_start() if apply_metrics is not None else 0.0
        frame = QFrame()
        frame.setObjectName("operblockTableCard")
        frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        stylesheet_started = operblock_startup_metrics.timer_start() if apply_metrics is not None else 0.0
        frame.setStyleSheet(PATIENT_CARD_STYLE)
        operblock_startup_metrics.record_since(
            "board_apply_card_stylesheet_ms",
            stylesheet_started,
            source="operblock_widget",
            **metric_fields,
        )
        shadow_started = operblock_startup_metrics.timer_start() if apply_metrics is not None else 0.0
        shadow = QGraphicsDropShadowEffect(frame)
        shadow.setBlurRadius(18)
        shadow.setColor(QColor(0, 0, 0, 22))
        shadow.setOffset(3, 4)
        frame.setGraphicsEffect(shadow)
        operblock_startup_metrics.record_since(
            "board_apply_card_shadow_effect_ms",
            shadow_started,
            source="operblock_widget",
            **metric_fields,
        )
        operblock_startup_metrics.record_since(
            "board_apply_card_widget_create_ms",
            metric_started,
            source="operblock_widget",
            **metric_fields,
        )
        return frame

    def _open_occupy_dialog(self, table_code: str, table_name: str):
        if self._is_closing or self._write_pending:
            return
        dialog = OccupyTableDialog(table_code, table_name, self)
        dialog_ref = weakref.ref(dialog)

        def save():
            form = dialog_ref()
            if form is None:
                return
            try:
                payload = form.get_data()
            except Exception as exc:
                CustomMessageBox.warning(form, "Ошибка", str(exc))
                return
            form.set_saving(True)
            self._write_pending = True

            def operation():
                return self.operblock_service.create_operation_case(payload)

            self._enqueue_write(
                f"operblock_create_operation_case:{table_code}",
                operation,
                on_success=lambda result: self._on_occupy_success(dialog_ref, result),
                on_error=lambda exc: self._on_occupy_error(dialog_ref, exc),
            )

        dialog.save_button.clicked.connect(save)
        dialog.exec()

    def _on_occupy_success(self, dialog_ref, result):
        self._write_pending = False
        dialog = dialog_ref()
        if dialog is not None:
            dialog.set_saving(False)
            dialog.accept()
        self.refresh_board(force=True)
        case_id = int((result or {}).get("operation_case_id") or 0)
        if case_id:
            QTimer.singleShot(0, lambda: self._open_protocol(case_id))

    def _on_occupy_error(self, dialog_ref, exc: Exception):
        self._write_pending = False
        dialog = dialog_ref()
        if dialog is not None:
            dialog.set_saving(False)
        CustomMessageBox.warning(self, "Ошибка сохранения", str(exc))
        self.refresh_board(force=True)

    def _open_protocol(self, operation_case_id: int):
        if self._is_closing:
            return
        first_open = self.protocol_page is None
        first_open_started = operblock_startup_metrics.timer_start() if first_open else None
        if not self._ensure_protocol_page_created():
            return
        self._current_operation_case_id = int(operation_case_id)
        self._current_operation_start = None
        self._current_operation_end = None
        self._current_case_active = False
        self._current_stage_state = {}
        self._current_anesthesia_start = None
        self._current_anesthesia_end = None
        self._current_surgery_start = None
        self._current_surgery_end = None
        self._current_anesthesia_active = False
        self._current_surgery_active = False
        self._current_anesthesia_assistance_type = ""
        self._current_operation_name = ""
        self._update_protocol_title_label()
        self._update_operblock_staff_legend()
        self._current_protocol_date = datetime.now()
        self._vitals_context_key = None
        self._current_orders_rows = []
        self._current_timeline_snapshot = None
        if getattr(self, "vitals_chart", None) and hasattr(self.vitals_chart, "set_timeline_snapshot"):
            self.vitals_chart.set_timeline_snapshot(None, None, force=True)
        self._protocol_hash = ""
        self.operblock_vitals_service.set_operation_context(
            operation_case_id=self._current_operation_case_id,
            admission_id=None,
            started_at=None,
            ended_at=None,
        )
        self._set_protocol_chrome(True)
        self.stack.setCurrentWidget(self.protocol_page)
        self.refresh_protocol(force=True)
        if first_open:
            operblock_startup_metrics.record_since(
                "first_open_protocol_ms",
                first_open_started,
                source="operblock_widget",
            )

    def _apply_protocol_snapshot(self, snapshot: dict):
        header = snapshot.get("header") or {}
        self._current_admission_id = int(header.get("admission_id") or 0)
        table_name = str(header.get("table_display_name") or "").strip()
        self.protocol_info_header_label.setText(f"Информация - {table_name}" if table_name else "Информация")
        started_at = header.get("started_at")
        ended_at = header.get("ended_at")
        self.protocol_started_label.setText(_format_protocol_started_at(started_at))
        self._current_operation_start = _minute_floor_dt(_parse_datetime_value(started_at)) or _minute_floor_dt(datetime.now())
        self._current_operation_end = _parse_datetime_value(ended_at)
        self._current_protocol_date = self._current_operation_start
        self.protocol_patient_label.setText(header.get("full_name") or "Неизвестно")
        self.protocol_history_label.setText(f"№ {header.get('history_number') or '—'}")
        self.protocol_age_label.setText(f"Возраст: {header.get('age') or '—'}")
        diagnosis_text = header.get("diagnosis_text") or "—"
        diagnosis_code = header.get("diagnosis_code")
        diagnosis_line = f"{diagnosis_code}: {diagnosis_text}" if diagnosis_code else diagnosis_text
        self.protocol_diagnosis_label.set_full_text(f"Диагноз: {diagnosis_line}")
        self._update_protocol_status_label(started_at, active=header.get("status") == "active")
        self._current_case_active = header.get("status") == "active"
        self._apply_stage_state(header.get("stage_state") or {})
        latest = header.get("latest") or {}
        self._update_latest_badges(latest)
        self.operblock_vitals_service.set_operation_context(
            operation_case_id=self._current_operation_case_id,
            admission_id=self._current_admission_id,
            started_at=self._current_operation_start,
            ended_at=self._current_operation_end,
        )
        orders_snapshot = snapshot.get("orders") or {}
        self._current_orders_rows = [dict(row or {}) for row in orders_snapshot.get("orders") or []]
        self._current_timeline_snapshot = dict(snapshot.get("timeline") or {})
        self._apply_active_infusions()
        self._set_vitals_context(self._current_admission_id)
        self._update_vitals_chart()
        self._apply_orders(orders_snapshot)
        self._apply_protocol_controls_state()

    def _update_protocol_status_label(self, started_at, *, active: bool):
        text, color = _format_main_remcard_status_text(started_at, active=active)
        self.protocol_status_label.setText(text)
        self.protocol_status_label.setStyleSheet(
            f"background-color: {color}; color: white; font-weight: bold; border-radius: 4px; padding: 2px;"
        )

    def _update_latest_badges(self, latest: dict):
        ad = str(latest.get("ad") or "-/-")
        pulse = latest.get("pulse")
        spo2 = latest.get("spo2")
        self.badge_ad.set_value(ad)
        self.badge_pulse.set_value(str(pulse if pulse is not None else "-"))
        self.badge_spo2.set_value(f"{int(spo2)}%" if spo2 is not None else "-%")

    def _apply_stage_state(self, stage_state: dict):
        self._current_stage_state = dict(stage_state or {})
        self._current_anesthesia_active = bool(self._current_stage_state.get("anesthesia_active"))
        self._current_surgery_active = bool(self._current_stage_state.get("surgery_active"))
        self._current_anesthesia_start = _minute_floor_dt(
            _parse_datetime_value(self._current_stage_state.get("current_anesthesia_start"))
            or _parse_datetime_value(self._current_stage_state.get("last_anesthesia_start"))
        )
        self._current_anesthesia_end = _minute_floor_dt(_parse_datetime_value(self._current_stage_state.get("last_anesthesia_end")))
        self._current_surgery_start = _minute_floor_dt(
            _parse_datetime_value(self._current_stage_state.get("current_surgery_start"))
            or _parse_datetime_value(self._current_stage_state.get("last_surgery_start"))
        )
        self._current_surgery_end = _minute_floor_dt(_parse_datetime_value(self._current_stage_state.get("last_surgery_end")))
        self._current_anesthesia_assistance_type = normalize_operblock_anesthesia_type_label(
            self._current_stage_state.get("current_anesthesia_assistance_type")
            or self._current_stage_state.get("last_anesthesia_assistance_type")
            or self._current_stage_state.get("first_anesthesia_assistance_type")
        )
        self._current_operation_name = normalize_operblock_team_text(
            self._current_stage_state.get("current_operation_name")
            or self._current_stage_state.get("last_operation_name")
            or self._current_stage_state.get("first_operation_name")
        )
        self._update_protocol_title_label()
        self._update_operblock_staff_legend()
        self._apply_protocol_controls_state()

    def _apply_protocol_controls_state(self):
        case_active = bool(getattr(self, "_current_case_active", False))
        aid_active = bool(getattr(self, "_current_anesthesia_active", False))
        surgery_active = bool(getattr(self, "_current_surgery_active", False))
        write_enabled = case_active and not self._write_pending
        if hasattr(self, "start_anesthesia_button"):
            self.start_anesthesia_button.setEnabled(write_enabled and not aid_active)
            self.end_anesthesia_button.setEnabled(write_enabled and aid_active)
            self.start_surgery_button.setEnabled(write_enabled and aid_active and not surgery_active)
            self.close_case_button.setEnabled(write_enabled and aid_active and surgery_active)
            self.release_table_button.setEnabled(write_enabled and not aid_active)
        if getattr(self, "vitals_input", None) is not None:
            try:
                self.vitals_input.set_forced_read_only(not case_active)
            except Exception:
                pass
            if hasattr(self.vitals_input, "save_btn"):
                self.vitals_input.save_btn.setEnabled(write_enabled)
            if hasattr(self.vitals_input, "undo_btn"):
                self.vitals_input.undo_btn.setEnabled(write_enabled)

        orders_tab_available = case_active and aid_active
        orders_controls_enabled = write_enabled and aid_active
        if hasattr(self, "orders_tab_button"):
            orders_visible = bool(getattr(self, "_protocol_tab_visible", {}).get("orders", True))
            self.orders_tab_button.setEnabled(orders_visible and orders_tab_available)
            if (
                self.content_stack is not None
                and (not orders_visible or not orders_tab_available)
                and self.content_stack.currentIndex() == 1
            ):
                self._set_protocol_tab_by_id("vitals")
            self._ensure_visible_protocol_tab()
        self._set_orders_entry_controls_enabled(orders_controls_enabled)

    def _set_orders_entry_controls_enabled(self, enabled: bool):
        for widget_name in ("order_input", "order_dose_input", "order_type_combo", "save_order_button"):
            widget = getattr(self, widget_name, None)
            if widget is not None:
                widget.setEnabled(bool(enabled))
        self._set_quick_order_buttons_enabled(enabled)
        self._set_order_action_buttons_enabled(enabled)
        self._set_infusion_action_buttons_enabled(enabled)

    def _set_vitals_context(self, admission_id: int):
        if not admission_id or not getattr(self, "vitals_input", None):
            return
        context_key = (
            int(admission_id),
            int(self._current_operation_case_id or 0),
            self._current_protocol_date.isoformat(timespec="minutes"),
        )
        if self._vitals_context_key == context_key:
            return
        self._vitals_context_key = context_key
        self.vitals_input.set_context(int(admission_id), self._current_protocol_date)

    def _update_vitals_chart(self):
        chart = getattr(self, "vitals_chart", None)
        if not self._current_admission_id or not self._current_operation_case_id or chart is None:
            return
        try:
            start_dt = self._current_operation_start or self._current_protocol_date
            vitals = self.operblock_service.list_operation_vitals(self._current_operation_case_id)
            anesthesia_started_at = self._first_anesthesia_start_for_chart()
            timeline_transform = type(chart).build_operation_timeline_transform(anesthesia_started_at, vitals)
            display_start_dt = timeline_transform.display_origin_at or start_dt
            visible_hours = self._calculate_operblock_chart_hours(
                display_start_dt,
                vitals,
                self._current_operation_end,
                timeline_transform=timeline_transform,
                timeline_snapshot=getattr(self, "_current_timeline_snapshot", None),
            )
            chart.admission_id = self._current_admission_id
            if hasattr(chart, "set_operation_timeline_model"):
                chart.set_operation_timeline_model(timeline_transform)
            chart.set_visible_hours(visible_hours)
            chart.update_data(vitals, display_start_dt, active_intervals=None)
            if hasattr(chart, "set_timeline_snapshot"):
                chart.set_timeline_snapshot(
                    getattr(self, "_current_timeline_snapshot", None),
                    display_start_dt,
                    force=True,
                )
            elif hasattr(chart, "set_operation_orders"):
                chart.set_operation_orders(getattr(self, "_current_orders_rows", []), display_start_dt, force=True)
            if getattr(self, "vitals_legend_sector", None):
                self.vitals_legend_sector.update_legend(OPERBLOCK_VITAL_SETTINGS)
        except Exception as exc:
            logger.error("operblock vitals chart refresh failed: %s", exc, exc_info=True)

    def _first_anesthesia_start_for_chart(self) -> datetime | None:
        state = getattr(self, "_current_stage_state", {}) or {}
        candidates = [
            state.get("first_anesthesia_start"),
            state.get("current_anesthesia_start"),
            state.get("last_anesthesia_start"),
        ]
        for value in candidates:
            parsed = _minute_floor_dt(_parse_datetime_value(value))
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    def _timeline_extent_datetimes(timeline_snapshot: dict | None) -> list[datetime]:
        snapshot = timeline_snapshot or {}
        result: list[datetime] = []

        def add(value) -> None:
            parsed = _minute_floor_dt(_parse_datetime_value(value))
            if isinstance(parsed, datetime):
                result.append(parsed)

        for event in snapshot.get("operation_events") or []:
            add((event or {}).get("event_time"))
        for event in snapshot.get("bolus_events") or []:
            add((event or {}).get("event_time"))

        now_dt = datetime.now().replace(second=0, microsecond=0)
        for interval in snapshot.get("infusion_intervals") or []:
            interval = interval or {}
            add(interval.get("start_time"))
            add(interval.get("end_time"))
            for history in interval.get("rate_history") or []:
                add((history or {}).get("event_time"))
            if str(interval.get("status") or "") == "active":
                add(now_dt)
        return result

    @staticmethod
    def _calculate_operblock_chart_hours(
        start_dt: datetime | None,
        vitals,
        ended_at: datetime | None = None,
        *,
        timeline_transform: object | None = None,
        timeline_snapshot: dict | None = None,
    ) -> int:
        if start_dt is None:
            return OPERBLOCK_INITIAL_CHART_HOURS
        if timeline_transform and timeline_transform.display_origin_at:
            elapsed_hours = 0.0
            for vital in vitals or []:
                timestamp = getattr(vital, "timestamp", None)
                x = timeline_transform.display_hours_for_vital(timestamp, fallback_start=start_dt)
                if x is not None:
                    elapsed_hours = max(elapsed_hours, float(x))
            if ended_at:
                x = timeline_transform.display_hours_for(ended_at, fallback_start=start_dt)
                if x is not None:
                    elapsed_hours = max(elapsed_hours, float(x))
            for event_dt in OperBlockMainWidget._timeline_extent_datetimes(timeline_snapshot):
                x = timeline_transform.display_hours_for(event_dt, fallback_start=start_dt)
                if x is not None:
                    elapsed_hours = max(elapsed_hours, float(x))
        else:
            max_dt = ended_at if ended_at and ended_at > start_dt else start_dt
            for vital in vitals or []:
                timestamp = getattr(vital, "timestamp", None)
                if isinstance(timestamp, datetime) and timestamp > max_dt:
                    max_dt = timestamp
            for event_dt in OperBlockMainWidget._timeline_extent_datetimes(timeline_snapshot):
                if event_dt > max_dt:
                    max_dt = event_dt
            elapsed_hours = max(0.0, (max_dt - start_dt).total_seconds() / 3600.0)
        visible_hours = OPERBLOCK_INITIAL_CHART_HOURS
        threshold_hours = OPERBLOCK_CHART_EXPAND_THRESHOLD_MINUTES / 60.0
        while elapsed_hours >= visible_hours - threshold_hours and visible_hours < OPERBLOCK_MAX_CHART_HOURS:
            visible_hours += 1
        return visible_hours

    def _on_standard_vitals_changed(self):
        self.refresh_protocol(force=True)
        self.refresh_board(force=True)

    def _load_quick_orders_data(self):
        metric_started = operblock_startup_metrics.timer_start()
        try:
            try:
                self._quick_order_templates = load_operblock_quick_orders()
                self._medication_presets = load_operblock_medication_presets(include_disabled=True)
            except Exception as exc:
                logger.error("operblock medication presets load failed: %s", exc, exc_info=True)
                self._quick_order_templates = []
                self._medication_presets = []
        finally:
            elapsed_ms = (time.perf_counter() - metric_started) * 1000.0 if metric_started else 0.0
            operblock_startup_metrics.record_duration(
                "quick_orders_load_ms",
                elapsed_ms,
                source="operblock_widget",
            )
            if getattr(self, "_creating_lazy_protocol_page", False):
                operblock_startup_metrics.record_duration(
                    "quick_orders_lazy_load_ms",
                    elapsed_ms,
                    source="operblock_widget",
                )

    def _refresh_quick_orders(self):
        self._load_quick_orders_data()
        self._render_quick_orders()

    def _on_preset_search_changed(self, text: str):
        self._preset_search_text = str(text or "").strip().casefold()
        self._render_quick_orders()

    def _on_preset_filter_changed(self, button_id: int):
        try:
            self._preset_kind_filter = OPERBLOCK_TEMPLATE_FILTERS[int(button_id)][0]
        except Exception:
            self._preset_kind_filter = "bolus"
        self._render_quick_orders()

    def _render_quick_orders(self):
        layout = getattr(self, "quick_orders_list", None)
        if layout is None:
            return
        self._quick_order_drag_source_id = None
        self._quick_order_drag_placeholder = None
        self._quick_order_drag_order = []
        self._quick_order_card_widgets = {}
        self._quick_order_visible_preset_ids = []
        self._clear_layout(layout)
        self._quick_order_buttons = []
        presets = self._filtered_medication_presets()
        if not presets:
            layout.addWidget(_label("Быстрые назначения не настроены", size=12, color=OPERBLOCK_ORDERS_MUTED))
            layout.addStretch(1)
            return

        add_icon = os.path.join(get_icon_dir(), "add_nazn.png")
        for preset in presets:
            preset_id = self._quick_order_preset_id(preset)
            row = self._make_medication_preset_row(preset, add_icon)
            self._quick_order_card_widgets[preset_id] = row
            self._quick_order_visible_preset_ids.append(preset_id)
            layout.addWidget(row)
        layout.addStretch(1)

    def _filtered_medication_presets(self) -> list[dict]:
        query = str(getattr(self, "_preset_search_text", "") or "").casefold()
        filter_kind = str(getattr(self, "_preset_kind_filter", "bolus") or "bolus")
        result: list[dict] = []
        for preset in getattr(self, "_medication_presets", []) or []:
            if not preset.get("enabled"):
                continue
            kind = normalize_operblock_medication_preset_kind(preset.get("kind"))
            if filter_kind == "favorite":
                if not any(bool(preset.get(key)) for key in ("favorite", "is_favorite", "pinned")):
                    continue
            elif filter_kind == "timed_infusion":
                if kind not in {"timed_infusion", "solvent"}:
                    continue
            elif kind != filter_kind:
                continue
            if query:
                haystack = " ".join(
                    [
                        str(preset.get("label") or ""),
                        str(preset.get("display_name") or ""),
                        str(preset.get("latin") or ""),
                        str(preset.get("group") or ""),
                        " ".join(str(alias) for alias in preset.get("aliases") or []),
                    ]
                ).casefold()
                if query not in haystack:
                    continue
            result.append(dict(preset))
        result.sort(key=self._quick_order_preset_sort_key)
        return result

    @staticmethod
    def _quick_order_preset_id(preset: dict) -> str:
        preset_id = str((preset or {}).get("preset_id") or "").strip()
        if preset_id:
            return preset_id
        label = str((preset or {}).get("label") or (preset or {}).get("display_name") or "").strip()
        kind = normalize_operblock_medication_preset_kind((preset or {}).get("kind"))
        return f"manual:{kind}:{label.casefold()}"

    @staticmethod
    def _quick_order_preset_sort_key(preset: dict) -> tuple:
        sort_order = _safe_int((preset or {}).get("sort_order"))
        return (
            sort_order if sort_order is not None else 99_999,
            str((preset or {}).get("display_name") or (preset or {}).get("label") or "").casefold(),
            OperBlockMainWidget._quick_order_preset_id(preset),
        )

    def _quick_order_layout_index(self, widget: QWidget | None) -> int:
        layout = getattr(self, "quick_orders_list", None)
        if layout is None or widget is None:
            return -1
        for index in range(layout.count()):
            item = layout.itemAt(index)
            if item is not None and item.widget() is widget:
                return index
        return -1

    def _make_quick_order_drag_placeholder(self, source_widget: QWidget) -> QFrame:
        placeholder = QFrame()
        placeholder.setObjectName("QuickOrderDragPlaceholder")
        height = max(46, int(source_widget.height() or source_widget.sizeHint().height() or 46))
        placeholder.setMinimumHeight(height)
        placeholder.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        placeholder.setStyleSheet(
            f"""
            QFrame#QuickOrderDragPlaceholder {{
                background-color: #EAF3FF;
                border: 2px dashed {OPERBLOCK_ORDERS_ACCENT};
                border-radius: 7px;
            }}
            """
        )
        return placeholder

    def _begin_quick_order_drag(self, source_preset_id: str) -> bool:
        source_id = str(source_preset_id or "").strip()
        layout = getattr(self, "quick_orders_list", None)
        source_widget = (getattr(self, "_quick_order_card_widgets", {}) or {}).get(source_id)
        if not source_id or layout is None or source_widget is None:
            return False
        self._cancel_quick_order_drag()
        source_index = self._quick_order_layout_index(source_widget)
        if source_index < 0:
            return False
        placeholder = self._make_quick_order_drag_placeholder(source_widget)
        self._quick_order_drag_source_id = source_id
        self._quick_order_drag_placeholder = placeholder
        self._quick_order_drag_order = list(getattr(self, "_quick_order_visible_preset_ids", []) or [])
        self._quick_order_drag_committed = False
        source_widget.hide()
        layout.insertWidget(source_index, placeholder)
        return True

    def _cancel_quick_order_drag(self) -> None:
        if not getattr(self, "_quick_order_drag_source_id", None):
            return
        self._restore_quick_order_drag_layout()
        self._quick_order_drag_source_id = None
        self._quick_order_drag_placeholder = None
        self._quick_order_drag_order = []
        self._quick_order_drag_committed = False

    def _restore_quick_order_drag_layout(self) -> None:
        layout = getattr(self, "quick_orders_list", None)
        if layout is None:
            return
        placeholder = getattr(self, "_quick_order_drag_placeholder", None)
        if placeholder is not None:
            layout.removeWidget(placeholder)
            placeholder.setParent(None)
            placeholder.deleteLater()
        widgets = getattr(self, "_quick_order_card_widgets", {}) or {}
        for widget in widgets.values():
            layout.removeWidget(widget)
        for index, preset_id in enumerate(getattr(self, "_quick_order_visible_preset_ids", []) or []):
            widget = widgets.get(preset_id)
            if widget is None:
                continue
            widget.show()
            layout.insertWidget(index, widget)

    def _finish_quick_order_drag_layout(self, order: list[str]) -> None:
        layout = getattr(self, "quick_orders_list", None)
        if layout is None:
            return
        placeholder = getattr(self, "_quick_order_drag_placeholder", None)
        if placeholder is not None:
            layout.removeWidget(placeholder)
            placeholder.setParent(None)
            placeholder.deleteLater()
        widgets = getattr(self, "_quick_order_card_widgets", {}) or {}
        for widget in widgets.values():
            layout.removeWidget(widget)
        final_order = [preset_id for preset_id in order if preset_id in widgets]
        for preset_id in getattr(self, "_quick_order_visible_preset_ids", []) or []:
            if preset_id not in final_order and preset_id in widgets:
                final_order.append(preset_id)
        for index, preset_id in enumerate(final_order):
            widget = widgets.get(preset_id)
            if widget is None:
                continue
            widget.show()
            layout.insertWidget(index, widget)
        self._quick_order_visible_preset_ids = final_order
        self._quick_order_drag_source_id = None
        self._quick_order_drag_placeholder = None
        self._quick_order_drag_order = []

    def _apply_quick_order_drag_order(self, order: list[str]) -> bool:
        source_id = str(getattr(self, "_quick_order_drag_source_id", "") or "")
        placeholder = getattr(self, "_quick_order_drag_placeholder", None)
        layout = getattr(self, "quick_orders_list", None)
        if not source_id or placeholder is None or layout is None:
            return False
        normalized_order = [str(item) for item in order if str(item or "").strip()]
        if source_id not in normalized_order:
            return False
        if normalized_order == list(getattr(self, "_quick_order_drag_order", []) or []):
            return True
        widgets = getattr(self, "_quick_order_card_widgets", {}) or {}
        for widget in widgets.values():
            layout.removeWidget(widget)
        layout.removeWidget(placeholder)
        for index, preset_id in enumerate(normalized_order):
            widget = placeholder if preset_id == source_id else widgets.get(preset_id)
            if widget is None:
                continue
            if widget is not placeholder:
                widget.show()
            layout.insertWidget(index, widget)
        source_widget = widgets.get(source_id)
        if source_widget is not None:
            source_widget.hide()
        self._quick_order_drag_order = normalized_order
        return True

    def _preview_quick_order_drag(self, source_preset_id: str, target_preset_id: str, *, after: bool = False) -> bool:
        source_id = str(source_preset_id or "").strip()
        target_id = str(target_preset_id or "").strip()
        if not source_id or not target_id or source_id == target_id:
            return False
        current = [item for item in (getattr(self, "_quick_order_drag_order", None) or getattr(self, "_quick_order_visible_preset_ids", []) or []) if item != source_id]
        if target_id not in current:
            return False
        insert_index = current.index(target_id) + (1 if after else 0)
        next_order = list(current)
        next_order.insert(insert_index, source_id)
        return self._apply_quick_order_drag_order(next_order)

    def _preview_quick_order_drag_at_y(self, source_preset_id: str, y: float) -> bool:
        source_id = str(source_preset_id or "").strip()
        if not source_id:
            return False
        order_without_source = [
            item
            for item in (getattr(self, "_quick_order_drag_order", None) or getattr(self, "_quick_order_visible_preset_ids", []) or [])
            if item != source_id
        ]
        widgets = getattr(self, "_quick_order_card_widgets", {}) or {}
        insert_index = len(order_without_source)
        for index, preset_id in enumerate(order_without_source):
            widget = widgets.get(preset_id)
            if widget is None or not widget.isVisible():
                continue
            if float(y) < float(widget.y() + widget.height() / 2):
                insert_index = index
                break
        next_order = list(order_without_source)
        next_order.insert(insert_index, source_id)
        return self._apply_quick_order_drag_order(next_order)

    def _save_quick_order_preset_order(self, visible_order: list[str]) -> list[dict]:
        visible_ids = [str(item) for item in visible_order if str(item or "").strip()]
        visible_set = set(visible_ids)
        presets = [dict(preset or {}) for preset in getattr(self, "_medication_presets", []) or []]
        global_ids = [self._quick_order_preset_id(preset) for preset in sorted(presets, key=self._quick_order_preset_sort_key)]
        ordered_ids: list[str] = []
        visible_iter = iter(visible_ids)
        for preset_id in global_ids:
            if preset_id in visible_set:
                try:
                    ordered_ids.append(next(visible_iter))
                except StopIteration:
                    continue
            else:
                ordered_ids.append(preset_id)
        for preset_id in visible_ids:
            if preset_id not in ordered_ids:
                ordered_ids.append(preset_id)
        sort_order_by_id = {preset_id: (index + 1) * 10 for index, preset_id in enumerate(ordered_ids)}
        for preset in presets:
            preset_id = self._quick_order_preset_id(preset)
            if preset_id in sort_order_by_id:
                preset["sort_order"] = sort_order_by_id[preset_id]
        return save_operblock_medication_presets(presets)

    def _commit_quick_order_drag(self) -> bool:
        source_id = str(getattr(self, "_quick_order_drag_source_id", "") or "")
        order = list(getattr(self, "_quick_order_drag_order", []) or [])
        if not source_id or source_id not in order:
            return False
        if order == list(getattr(self, "_quick_order_visible_preset_ids", []) or []):
            self._cancel_quick_order_drag()
            self._quick_order_drag_committed = True
            return True
        scroll_state = self._capture_quick_orders_scroll_state()
        try:
            self._medication_presets = self._save_quick_order_preset_order(order)
        except Exception as exc:
            logger.error("operblock quick order reorder failed: %s", exc, exc_info=True)
            CustomMessageBox.warning(self, "Быстрые назначения", f"Не удалось сохранить порядок:\n{exc}")
            self._cancel_quick_order_drag()
            self._refresh_quick_orders()
            return False
        self._quick_order_drag_committed = True
        self._finish_quick_order_drag_layout(order)
        self._restore_quick_orders_scroll_state_later(scroll_state)
        return True

    def _make_medication_preset_row(self, preset: dict, add_icon: str = "") -> QWidget:
        display_name = operblock_medication_preset_display_name(preset)
        label = str(preset.get("label") or display_name).strip()
        kind = normalize_operblock_medication_preset_kind(preset.get("kind"))
        doses = [str(dose or "").strip() for dose in preset.get("doses") or [] if str(dose or "").strip()]
        rates = _normalize_infusion_rate_options(preset.get("rates")) if kind == "continuous_infusion" else []
        concentration_text = str(preset.get("concentration") or "").strip()
        title_text, concentration_text = _quick_order_title_and_concentration(display_name or label, concentration_text)
        solvent_text = _quick_order_solvent_text(preset)
        title_line = f"{title_text} + {solvent_text}" if solvent_text else title_text

        frame = _QuickOrderPresetCard(self._quick_order_preset_id(preset), self)
        frame.setObjectName("medicationPresetRow")
        frame.setStyleSheet(
            f"""
            QFrame#medicationPresetRow {{
                background-color: {OPERBLOCK_ORDERS_CARD_BG};
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 6px;
            }}
            QPushButton {{
                background-color: #F8FAFC;
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 4px;
                color: {OPERBLOCK_ORDERS_TEXT};
                padding: 4px 6px;
                font-size: 11px;
                font-weight: 500;
            }}
            QPushButton:hover {{
                background-color: #EEF3FF;
                color: {OPERBLOCK_ORDERS_ACCENT};
                border-color: {OPERBLOCK_ORDERS_BORDER};
            }}
            QPushButton:disabled {{
                color: {OPERBLOCK_ORDERS_MUTED};
                background-color: #F1F5F9;
            }}
            """
        )
        row_layout = QVBoxLayout(frame)
        row_layout.setContentsMargins(8, 8, 8, 8)
        row_layout.setSpacing(6)

        name_label = ElidedTooltipLabel(title_line)
        name_label.setStyleSheet(
            f"font-size: 13px; font-weight: 500; color: {OPERBLOCK_ORDERS_TEXT}; background: transparent; border: none;"
            f"{TOOLTIP_WHITE_STYLE}"
        )
        row_layout.addWidget(name_label)
        if concentration_text:
            detail_label = ElidedTooltipLabel(concentration_text)
            detail_label.setStyleSheet(
                f"font-size: 11px; color: {OPERBLOCK_ORDERS_MUTED}; background: transparent; border: none;"
                f"{TOOLTIP_WHITE_STYLE}"
            )
            row_layout.addWidget(detail_label)

        def add_button(grid: QGridLayout, text: str, row: int, column: int, callback) -> None:
            button = QPushButton(text)
            button.setFixedHeight(28)
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(callback)
            self._quick_order_buttons.append(button)
            grid.addWidget(button, row, column)

        if kind == "bolus":
            if doses:
                visible_doses = doses[:4]
                dose_columns = 2 if len(visible_doses) > 1 else 1
                dose_grid = QGridLayout()
                dose_grid.setContentsMargins(0, 0, 0, 0)
                dose_grid.setHorizontalSpacing(6)
                dose_grid.setVerticalSpacing(6)
                for column in range(dose_columns):
                    dose_grid.setColumnStretch(column, 1)
                for index, dose in enumerate(visible_doses):
                    add_button(
                        dose_grid,
                        _quick_order_dose_display_text(dose, concentration_text),
                        index // dose_columns,
                        index % dose_columns,
                        lambda _=False, current=preset, value=dose: self._add_preset_bolus(current, value),
                    )
                row_layout.addLayout(dose_grid)
            else:
                row_layout.addWidget(_label("Дозировки не указаны", size=12, color=OPERBLOCK_ORDERS_MUTED))
        elif kind == "gas":
            if doses:
                visible_doses = doses[:4]
                dose_columns = 2 if len(visible_doses) > 1 else 1
                dose_grid = QGridLayout()
                dose_grid.setContentsMargins(0, 0, 0, 0)
                dose_grid.setHorizontalSpacing(6)
                dose_grid.setVerticalSpacing(6)
                for column in range(dose_columns):
                    dose_grid.setColumnStretch(column, 1)
                for index, dose in enumerate(visible_doses):
                    add_button(
                        dose_grid,
                        _normalize_gas_dose_text(dose),
                        index // dose_columns,
                        index % dose_columns,
                        lambda _=False, current=preset, value=dose: self._add_preset_gas(current, value),
                    )
                row_layout.addLayout(dose_grid)
            else:
                row_layout.addWidget(_label("Дозы MAC не указаны", size=12, color=OPERBLOCK_ORDERS_MUTED))
        elif kind == "continuous_infusion":
            if rates:
                visible_rates = rates[:4]
                rate_columns = 2 if len(visible_rates) > 1 else 1
                rate_grid = QGridLayout()
                rate_grid.setContentsMargins(0, 0, 0, 0)
                rate_grid.setHorizontalSpacing(6)
                rate_grid.setVerticalSpacing(6)
                for column in range(rate_columns):
                    rate_grid.setColumnStretch(column, 1)
                for index, rate in enumerate(visible_rates):
                    add_button(
                        rate_grid,
                        f"Старт {_compact_infusion_rate_display_text(rate)}",
                        index // rate_columns,
                        index % rate_columns,
                        lambda _=False, current=preset, value=rate: self._start_preset_infusion(current, value),
                    )
                row_layout.addLayout(rate_grid)
            else:
                row_layout.addWidget(_label("Скорости не указаны", size=12, color=OPERBLOCK_ORDERS_MUTED))
        elif kind in {"timed_infusion", "solvent"}:
            dose_options = _timed_infusion_dose_options(preset)
            if dose_options:
                visible_doses = dose_options[:4]
                dose_columns = 2 if len(visible_doses) > 1 else 1
                dose_grid = QGridLayout()
                dose_grid.setContentsMargins(0, 0, 0, 0)
                dose_grid.setHorizontalSpacing(6)
                dose_grid.setVerticalSpacing(6)
                for column in range(dose_columns):
                    dose_grid.setColumnStretch(column, 1)
                for index, dose in enumerate(visible_doses):
                    add_button(
                        dose_grid,
                        _quick_order_dose_display_text(dose, concentration_text),
                        index // dose_columns,
                        index % dose_columns,
                        lambda _=False, current=preset, value=dose: self._start_timed_infusion_preset(current, value),
                    )
                row_layout.addLayout(dose_grid)
            else:
                row_layout.addWidget(_label("Объем не указан", size=12, color=OPERBLOCK_ORDERS_MUTED))
        else:
            row_layout.addWidget(_label("Доступно в справочнике", size=12, color=OPERBLOCK_ORDERS_MUTED))
        frame.bind_drag_sources()
        return frame

    def _make_quick_order_row(self, template: dict, add_icon: str) -> QWidget:
        drug_name = str(template.get("drug_name") or "").strip()
        group_number = normalize_operblock_quick_order_group(template.get("group"))
        kind = normalize_operblock_quick_order_kind(template.get("kind"))
        doses = [str(dose or "").strip() for dose in template.get("doses") or [] if str(dose or "").strip()]
        rates = _normalize_infusion_rate_options(template.get("rates")) if kind == "infusion" else []
        concentration_text = str(template.get("concentration") or template.get("concentration_text") or "").strip()
        title_text, concentration_text = _quick_order_title_and_concentration(drug_name, concentration_text)
        solvent_text = _quick_order_solvent_text(template)
        title_line = f"{title_text} + {solvent_text}" if solvent_text else title_text

        frame = QFrame()
        frame.setObjectName("quickOrderRow")
        frame.setStyleSheet(
            f"""
            QFrame#quickOrderRow {{
                background-color: {BG_LIGHT};
                border: 1px solid {BORDER_LIGHT};
                border-radius: {CUSTOM_DIALOG_RADIUS};
            }}
            QPushButton {{
                background-color: #ffffff;
                border: 1px solid {BORDER_COLOR};
                border-radius: 4px;
                color: {TEXT_PRIMARY};
                padding: 2px 3px;
                font-size: 11px;
            }}
            QPushButton:hover {{
                background-color: #eef3f6;
            }}
            QPushButton:disabled {{
                color: {TEXT_MUTED};
                background-color: {BG_MAIN};
            }}
            """
        )
        row_layout = QVBoxLayout(frame)
        row_layout.setContentsMargins(5, 5, 5, 5)
        row_layout.setSpacing(5)

        drug_header = QHBoxLayout()
        drug_header.setContentsMargins(0, 0, 0, 0)
        drug_header.setSpacing(5)
        drug_label = ElidedTooltipLabel(title_line)
        drug_label.setStyleSheet(
            f"font-size: 12px; font-weight: 500; color: {TEXT_PRIMARY}; background: transparent; border: none;"
            f"{TOOLTIP_WHITE_STYLE}"
        )
        group_label = QLabel(f"{group_number}")
        group_label.setAlignment(Qt.AlignCenter)
        group_label.setFixedSize(22, 20)
        group_label.setStyleSheet(
            f"font-size: 11px; font-weight: 500; color: {COLOR_PRIMARY_DARK}; background-color: #ffffff; "
            f"border: 1px solid {BORDER_COLOR}; border-radius: 4px;"
        )
        kind_label = QLabel({"infusion": "ИНФ", "gas": "ГАЗ"}.get(kind, "БОЛ"))
        kind_label.setAlignment(Qt.AlignCenter)
        kind_label.setFixedSize(32, 20)
        kind_label.setStyleSheet(
            f"font-size: 10px; font-weight: 500; color: {TEXT_SECONDARY}; background-color: #ffffff; "
            f"border: 1px solid {BORDER_COLOR}; border-radius: 4px;"
        )
        drug_header.addWidget(drug_label, 1)
        drug_header.addWidget(kind_label, 0)
        drug_header.addWidget(group_label, 0)
        row_layout.addLayout(drug_header)
        if concentration_text:
            detail_label = ElidedTooltipLabel(concentration_text)
            detail_label.setStyleSheet(
                f"font-size: 11px; color: {TEXT_SECONDARY}; background: transparent; border: none;"
                f"{TOOLTIP_WHITE_STYLE}"
            )
            row_layout.addWidget(detail_label)

        if kind == "infusion":
            if rates:
                visible_rates = rates[:4]
                rate_columns = 2 if len(visible_rates) > 1 else 1
                rate_grid = QGridLayout()
                rate_grid.setContentsMargins(0, 0, 0, 0)
                rate_grid.setHorizontalSpacing(6)
                rate_grid.setVerticalSpacing(4)
                for column in range(rate_columns):
                    rate_grid.setColumnStretch(column, 1)
                for index, rate in enumerate(visible_rates):
                    rate_layout = QHBoxLayout()
                    rate_layout.setContentsMargins(0, 0, 0, 0)
                    rate_layout.setSpacing(4)

                    start_button = QPushButton("Старт")
                    start_button.setFixedHeight(24)
                    start_button.setCursor(Qt.PointingHandCursor)
                    start_button.setToolTip(f"Старт дозатора {drug_name}: {rate}")
                    start_button.clicked.connect(
                        lambda _=False, drug=drug_name, value=rate, concentration=concentration_text: self._start_quick_infusion(
                            drug,
                            value,
                            concentration_text=concentration,
                        )
                    )
                    self._quick_order_buttons.append(start_button)

                    rate_label = ElidedTooltipLabel(_compact_infusion_rate_display_text(rate))
                    rate_label.setMinimumWidth(0)
                    rate_label.setToolTip(rate)
                    rate_label.setStyleSheet(
                        f"font-size: 12px; color: {TEXT_PRIMARY}; background: transparent; border: none;"
                        f"{TOOLTIP_WHITE_STYLE}"
                    )
                    rate_layout.addWidget(start_button, 0)
                    rate_layout.addWidget(rate_label, 1)
                    rate_grid.addLayout(rate_layout, index // rate_columns, index % rate_columns)
                row_layout.addLayout(rate_grid)
            else:
                row_layout.addWidget(_label("Скорости не указаны", size=12, color=TEXT_SECONDARY))
        elif doses:
            for dose in doses[:4]:
                dose_layout = QHBoxLayout()
                dose_layout.setContentsMargins(0, 0, 0, 0)
                dose_layout.setSpacing(5)

                add_button = QPushButton()
                add_button.setFixedSize(24, 24)
                add_button.setCursor(Qt.PointingHandCursor)
                add_button.setToolTip(f"Добавить {build_operblock_quick_order_text(drug_name, dose)}")
                if os.path.exists(add_icon):
                    add_button.setIcon(QIcon(add_icon))
                    add_button.setIconSize(QSize(18, 18))
                add_button.clicked.connect(
                    lambda _=False, drug=drug_name, value=dose, order_kind=kind: self._add_quick_order(
                        drug,
                        value,
                        kind=order_kind,
                    )
                )
                self._quick_order_buttons.append(add_button)

                dose_label = QLabel(_quick_order_dose_display_text(dose, concentration_text))
                dose_label.setStyleSheet(
                    f"font-size: 12px; color: {TEXT_PRIMARY}; background: transparent; border: none;"
                )
                dose_layout.addWidget(add_button, 0)
                dose_layout.addWidget(dose_label, 1)
                row_layout.addLayout(dose_layout)
        else:
            row_layout.addWidget(_label("Дозировки не указаны", size=12, color=TEXT_SECONDARY))
        return frame

    def _set_quick_order_buttons_enabled(self, enabled: bool):
        for button in list(getattr(self, "_quick_order_buttons", [])):
            try:
                button.setEnabled(bool(enabled))
            except RuntimeError:
                continue

    def _capture_quick_orders_scroll_state(self) -> dict:
        scroll = getattr(self, "quick_orders_scroll", None)
        if scroll is None:
            return {"value": 0, "maximum": 0}
        bar = scroll.verticalScrollBar()
        return {"value": int(bar.value()), "maximum": int(bar.maximum())}

    def _restore_quick_orders_scroll_state(self, state: dict | None):
        if not state:
            return
        scroll = getattr(self, "quick_orders_scroll", None)
        if scroll is None:
            return
        bar = scroll.verticalScrollBar()
        old_value = int(state.get("value") or 0)
        bar.setValue(max(0, min(old_value, int(bar.maximum()))))

    def _restore_quick_orders_scroll_state_later(self, state: dict | None):
        if not state:
            return
        snapshot = dict(state)
        self._restore_quick_orders_scroll_state(snapshot)
        QTimer.singleShot(0, lambda: self._restore_quick_orders_scroll_state(snapshot))

    def _remember_quick_orders_scroll_state(self) -> dict:
        state = self._capture_quick_orders_scroll_state()
        self._pending_quick_orders_scroll_state = dict(state)
        return state

    def _open_operblock_settings(self):
        if self._is_closing:
            return
        dialog = OperBlockSettingsDialog(self)
        dialog.medications_button.clicked.connect(lambda: self._open_quick_orders_settings(dialog))
        dialog.anesthesia_types_button.clicked.connect(lambda: self._open_anesthesia_types_settings(dialog))
        dialog.team_button.clicked.connect(lambda: self._open_operblock_team_settings(dialog))
        dialog.exec()

    def _open_anesthesia_types_settings(self, dialog_parent: QWidget | None = None):
        try:
            items = load_operblock_anesthesia_types()
        except Exception as exc:
            CustomMessageBox.warning(self, "Виды пособия", f"Не удалось загрузить виды пособия: {exc}")
            return
        parent = dialog_parent if isinstance(dialog_parent, QWidget) else self
        dialog = OperBlockAnesthesiaTypesDialog(items, parent)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            save_operblock_anesthesia_types(dialog.items())
        except Exception as exc:
            CustomMessageBox.warning(self, "Виды пособия", f"Не удалось сохранить виды пособия: {exc}")
            return

    def _open_operblock_team_settings(self, dialog_parent: QWidget | None = None):
        if self._is_closing:
            return
        try:
            items = load_operblock_team()
        except Exception as exc:
            CustomMessageBox.warning(self, "Опер. бригада", f"Не удалось загрузить опер. бригаду: {exc}")
            return
        parent = dialog_parent if isinstance(dialog_parent, QWidget) else self
        dialog = OperBlockTeamDialog(items, parent)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            save_operblock_team(dialog.items())
        except Exception as exc:
            CustomMessageBox.warning(self, "Опер. бригада", f"Не удалось сохранить опер. бригаду: {exc}")
            return

    def _open_quick_orders_settings(self, dialog_parent: QWidget | None = None):
        if self._is_closing or self._write_pending:
            return
        self._load_quick_orders_data()
        parent = dialog_parent if isinstance(dialog_parent, QWidget) else self
        dialog = OperBlockMedicationPresetsDialog(self._medication_presets, parent)
        if dialog.exec() != QDialog.Accepted:
            return
        self._write_pending = True
        self.sector_8_panel.btn_settings.setEnabled(False)

        def operation():
            return save_operblock_medication_presets(dialog.templates())

        self._enqueue_write(
            "operblock_medication_presets_save",
            operation,
            on_success=lambda result: self._on_quick_orders_saved(result),
            on_error=lambda exc: self._on_quick_orders_save_error(exc),
        )

    def _on_quick_orders_saved(self, result):
        self._write_pending = False
        self.sector_8_panel.btn_settings.setEnabled(True)
        self._medication_presets = list(result or [])
        try:
            self._quick_order_templates = load_operblock_quick_orders()
        except Exception:
            self._quick_order_templates = []
        self._render_quick_orders()
        if self._current_operation_case_id:
            self._orders_force_top_on_next_apply = True
            self.refresh_protocol(force=True)

    def _on_quick_orders_save_error(self, exc: Exception):
        self._write_pending = False
        self.sector_8_panel.btn_settings.setEnabled(True)
        CustomMessageBox.warning(self, "Ошибка сохранения", str(exc))
        self._refresh_quick_orders()

    def _apply_orders(self, snapshot: dict):
        scroll_state = self._capture_orders_scroll_state()
        force_top = bool(getattr(self, "_orders_force_top_on_next_apply", False))
        self._orders_force_top_on_next_apply = False
        rows = [dict(row or {}) for row in (snapshot.get("orders") or [])]
        groups = self._build_medication_order_groups(rows)
        groups = self._filtered_medication_order_groups(groups)
        render_signature = self._orders_groups_render_signature(groups)
        source_signature = self._orders_groups_source_signature(groups)
        previous_signature = getattr(self, "_orders_render_signature", "")
        if render_signature == previous_signature:
            self._current_orders_rows = rows
            if hasattr(self, "orders_count_label"):
                self.orders_count_label.setText(str(len(groups)))
            if force_top:
                QTimer.singleShot(0, lambda: self._restore_orders_scroll_state({"value": 0, "maximum": 0, "at_bottom": False}))
            return
        source_changed = source_signature != getattr(self, "_orders_source_signature", "")
        scroll = getattr(self, "orders_scroll", None)
        try:
            self._current_orders_rows = rows
            if source_changed:
                self._update_vitals_chart_order_markers()
            if hasattr(self, "orders_count_label"):
                self.orders_count_label.setText(str(len(groups)))
            if not groups:
                self._render_empty_orders_list()
                self._orders_render_signature = render_signature
                self._orders_source_signature = source_signature
                return

            self._render_medication_groups_diff(groups)
            self._orders_render_signature = render_signature
            self._orders_source_signature = source_signature
        finally:
            if scroll is not None:
                self.orders_list.activate()
                content = scroll.widget()
                if content is not None:
                    content.adjustSize()
                if force_top:
                    scroll_state = {"value": 0, "maximum": 0, "at_bottom": False}
                QTimer.singleShot(0, lambda state=scroll_state: self._restore_orders_scroll_state(state))

    def _update_vitals_chart_order_markers(self):
        chart = getattr(self, "vitals_chart", None)
        if not chart:
            return
        start_dt = getattr(chart, "start_time", None)
        if not isinstance(start_dt, datetime):
            transform = getattr(chart, "_timeline_transform", None)
            start_dt = getattr(transform, "display_origin_at", None)
        if not isinstance(start_dt, datetime):
            start_dt = self._current_operation_start or self._current_protocol_date
        if hasattr(chart, "set_timeline_snapshot"):
            chart.set_timeline_snapshot(getattr(self, "_current_timeline_snapshot", None), start_dt, force=True)
        elif hasattr(chart, "set_operation_orders"):
            chart.set_operation_orders(getattr(self, "_current_orders_rows", []), start_dt, force=True)

    def _remove_orders_spacers(self):
        layout = getattr(self, "orders_list", None)
        if layout is None:
            return
        for index in reversed(range(layout.count())):
            item = layout.itemAt(index)
            if item is not None and item.spacerItem() is not None:
                layout.takeAt(index)

    def _render_empty_orders_list(self):
        layout = getattr(self, "orders_list", None)
        if layout is None:
            return
        self._clear_layout(layout)
        self._rendered_medication_group_widgets = {}
        self._rendered_medication_group_signatures = {}
        self._rendered_medication_group_order = []
        self._rendered_order_detail_labels = {}
        self._rendered_medication_group_total_labels = {}
        self._orders_empty_widget = _label("Введений пока нет", size=13, color=OPERBLOCK_ORDERS_MUTED)
        layout.addWidget(self._orders_empty_widget)
        layout.addStretch(1)
        self._order_action_buttons = []

    def _render_medication_groups_diff(self, groups: list[dict]):
        layout = getattr(self, "orders_list", None)
        if layout is None:
            return
        if getattr(self, "_orders_empty_widget", None) is not None:
            self._clear_layout(layout)
            self._orders_empty_widget = None
            self._rendered_medication_group_widgets = {}
            self._rendered_medication_group_signatures = {}
        self._remove_orders_spacers()

        old_widgets = dict(getattr(self, "_rendered_medication_group_widgets", {}) or {})
        old_signatures = dict(getattr(self, "_rendered_medication_group_signatures", {}) or {})
        old_order = list(getattr(self, "_rendered_medication_group_order", []) or list(old_widgets.keys()))
        new_order = [str(group.get("key") or "") for group in groups]
        new_keys = {str(group.get("key") or "") for group in groups}
        for key, widget in old_widgets.items():
            if key in new_keys:
                continue
            layout.removeWidget(widget)
            widget.deleteLater()

        new_widgets: dict[str, QWidget] = {}
        new_signatures: dict[str, str] = {}
        changed_or_new_keys = {
            str(group.get("key") or "")
            for group in groups
            if old_widgets.get(str(group.get("key") or "")) is None
            or old_signatures.get(str(group.get("key") or "")) != self._medication_group_render_signature(group)
        }
        retained_old_order = [key for key in old_order if key in new_keys and key not in changed_or_new_keys]
        retained_new_order = [key for key in new_order if key not in changed_or_new_keys]
        can_patch_in_place = retained_old_order == retained_new_order
        if old_order == new_order or can_patch_in_place:
            for index, group in enumerate(groups):
                key = str(group.get("key") or "")
                signature = self._medication_group_render_signature(group)
                widget = old_widgets.get(key)
                if key in changed_or_new_keys:
                    if widget is not None:
                        layout.removeWidget(widget)
                        widget.deleteLater()
                    widget = self._make_medication_group_card(group)
                    layout.insertWidget(index, widget)
                new_widgets[key] = widget
                new_signatures[key] = signature
        else:
            for index, group in enumerate(groups):
                key = str(group.get("key") or "")
                signature = self._medication_group_render_signature(group)
                widget = old_widgets.get(key)
                if widget is None or old_signatures.get(key) != signature:
                    if widget is not None:
                        layout.removeWidget(widget)
                        widget.deleteLater()
                    widget = self._make_medication_group_card(group)
                else:
                    layout.removeWidget(widget)
                layout.insertWidget(index, widget)
                new_widgets[key] = widget
                new_signatures[key] = signature

        self._rendered_medication_group_widgets = new_widgets
        self._rendered_medication_group_signatures = new_signatures
        self._rendered_medication_group_order = new_order
        layout.addStretch(1)
        content = self.orders_scroll.widget() if getattr(self, "orders_scroll", None) else None
        self._order_action_buttons = list(content.findChildren(QPushButton)) if content is not None else []
        self._refresh_rendered_order_widget_index()

    def _refresh_rendered_order_widget_index(self):
        self._rendered_order_detail_labels = {}
        self._rendered_medication_group_total_labels = {}
        content = self.orders_scroll.widget() if getattr(self, "orders_scroll", None) else None
        if content is None:
            return
        for label in content.findChildren(ElidedTooltipLabel):
            try:
                order_id = _safe_int(label.property("operblock_order_id"))
                if order_id:
                    self._rendered_order_detail_labels[int(order_id)] = label
                group_key = str(label.property("operblock_group_total_key") or "").strip()
                if group_key:
                    self._rendered_medication_group_total_labels[group_key] = label
            except RuntimeError:
                continue

    def _orders_groups_render_signature(self, groups: list[dict]) -> str:
        return _stable_ui_hash(
            {
                "order": [str(group.get("key") or "") for group in groups],
                "groups": {str(group.get("key") or ""): self._medication_group_render_signature(group) for group in groups},
            }
        )

    def _sync_orders_render_signatures_from_current_rows(self) -> None:
        rows = [dict(row or {}) for row in getattr(self, "_current_orders_rows", []) or []]
        groups = self._filtered_medication_order_groups(self._build_medication_order_groups(rows))
        self._orders_render_signature = self._orders_groups_render_signature(groups)
        self._orders_source_signature = self._orders_groups_source_signature(groups)
        rendered_signatures = dict(getattr(self, "_rendered_medication_group_signatures", {}) or {})
        rendered_widgets = dict(getattr(self, "_rendered_medication_group_widgets", {}) or {})
        for group in groups:
            key = str(group.get("key") or "")
            if key in rendered_widgets:
                rendered_signatures[key] = self._medication_group_render_signature(group)
                total_label = (getattr(self, "_rendered_medication_group_total_labels", {}) or {}).get(key)
                if total_label is not None:
                    try:
                        total_label.set_full_text(str(group.get("total_text") or "Итого: нет дозы"))
                    except RuntimeError:
                        pass
        self._rendered_medication_group_signatures = rendered_signatures
        if getattr(self, "orders_count_label", None) is not None:
            self.orders_count_label.setText(str(len(groups)))

    def _patch_rendered_order_detail_text(self, order_id: int) -> None:
        row = self._current_order_row_by_id(int(order_id))
        if not row:
            return
        _raw_drug_name, dose = _split_order_drug_and_dose(str(row.get("text") or ""))
        detail = _order_dose_text_with_route(dose, row, short=False) or str(row.get("text") or "").strip()
        label = (getattr(self, "_rendered_order_detail_labels", {}) or {}).get(int(order_id))
        if label is None:
            return
        try:
            label.set_full_text(detail)
        except RuntimeError:
            self._refresh_rendered_order_widget_index()

    @staticmethod
    def _orders_groups_source_signature(groups: list[dict]) -> str:
        return _stable_ui_hash(
            {
                "order": [str(group.get("key") or "") for group in groups],
                "groups": {str(group.get("key") or ""): str(group.get("source_signature") or "") for group in groups},
            }
        )

    def _filtered_medication_order_groups(self, groups: list[dict]) -> list[dict]:
        filter_kind = str(getattr(self, "_orders_filter_kind", "all") or "all")
        hide_deleted = bool(getattr(self, "_orders_hide_deleted", True))
        result: list[dict] = []
        for group in groups or []:
            entries = [
                dict(entry or {})
                for entry in (group.get("entries") or [])
                if self._medication_entry_matches_orders_filter(entry or {}, filter_kind, hide_deleted)
            ]
            if not entries:
                continue
            filtered_group = dict(group)
            filtered_group["entries"] = entries
            order_rows = [dict(entry.get("row") or {}) for entry in entries if entry.get("kind") == "order"]
            infusion_rows = [dict(entry.get("interval") or {}) for entry in entries if entry.get("kind") == "infusion"]
            filtered_group["order_rows"] = order_rows
            filtered_group["infusion_rows"] = infusion_rows
            filtered_group["has_bolus_order"] = any(str(row.get("order_kind") or "") == "bolus" for row in order_rows)
            filtered_group["has_gas_order"] = any(str(row.get("order_kind") or "") == "gas" for row in order_rows)
            filtered_group["has_gas_infusion"] = any(_is_gas_infusion(interval) for interval in infusion_rows)
            filtered_group["has_rate_infusion"] = any(_infusion_has_rate(interval) for interval in infusion_rows)
            filtered_group["has_volume_infusion"] = any(_is_volume_only_infusion(interval) for interval in infusion_rows)
            latest_values = [
                _minute_floor_dt(_parse_datetime_value(entry.get("time"))) or datetime.min
                for entry in entries
            ]
            filtered_group["latest_dt"] = max(latest_values) if latest_values else datetime.min
            sort_ids = [_safe_int(entry.get("sort_id")) or 0 for entry in entries]
            sort_ids = [value for value in sort_ids if value > 0]
            filtered_group["first_id"] = min(sort_ids) if sort_ids else None
            filtered_group["total_text"] = self._medication_group_total_text(filtered_group)
            filtered_group["source_signature"] = self._medication_group_source_signature(filtered_group)
            result.append(filtered_group)
        result.sort(
            key=lambda group: (
                group.get("latest_dt") if isinstance(group.get("latest_dt"), datetime) else datetime.min,
                int(group.get("first_id") or 0),
                str(group.get("drug_name") or "").casefold(),
            ),
            reverse=True,
        )
        return result

    @staticmethod
    def _medication_entry_matches_orders_filter(entry: dict, filter_kind: str, hide_deleted: bool) -> bool:
        kind = str(entry.get("kind") or "")
        row = entry.get("row") if isinstance(entry.get("row"), dict) else {}
        interval = entry.get("interval") if isinstance(entry.get("interval"), dict) else {}
        status = str((row or interval or {}).get("status") or "")
        if hide_deleted and status in {"deleted", "cancelled"}:
            return False
        if filter_kind == "all":
            return True
        if filter_kind == "bolus":
            return kind == "order" and str(entry.get("order_kind") or "bolus") != "gas"
        if filter_kind == "gas":
            return (kind == "order" and str(entry.get("order_kind") or "") == "gas") or (
                kind == "infusion" and _is_gas_infusion(interval)
            )
        if filter_kind == "continuous_infusion":
            return kind == "infusion" and _infusion_has_rate(interval)
        if filter_kind == "timed_infusion":
            return kind == "infusion" and not _infusion_has_rate(interval) and not _is_gas_infusion(interval)
        if filter_kind == "active":
            return kind == "infusion" and status == "active"
        return True

    @staticmethod
    def _timeline_event_numeric_id(value) -> int:
        match = re.search(r"(\d+)$", str(value or ""))
        return int(match.group(1)) if match else 0

    def _build_timeline_events(self, rows) -> list[dict]:
        events: list[dict] = []
        for raw_row in rows or []:
            row = dict(raw_row or {})
            order_dt = _minute_floor_dt(_parse_datetime_value(row.get("datetime")))
            if order_dt is None:
                continue
            raw_drug_name, dose = _split_order_drug_and_dose(str(row.get("text") or ""))
            drug_name = self._order_display_drug_name(row, raw_drug_name)
            row["drug_name"] = drug_name
            row["raw_drug_name"] = raw_drug_name
            row["dose_text"] = dose
            row["route"] = _order_route_code(row)
            order_kind = self._order_preset_kind(row)
            row["order_kind"] = order_kind
            events.append(
                {
                    "kind": "order",
                    "order_kind": order_kind,
                    "time": order_dt,
                    "drug": drug_name,
                    "detail": _order_dose_text_with_route(dose, row, short=False)
                    or str(row.get("text") or "").strip(),
                    "badge": "Газ" if order_kind == "gas" else "Болюс",
                    "row": row,
                    "sort_id": _safe_int(row.get("id")) or 0,
                }
            )

        snapshot = getattr(self, "_current_timeline_snapshot", None) or {}
        for raw_interval in snapshot.get("infusion_intervals") or []:
            interval = dict(raw_interval or {})
            status = str(interval.get("status") or "")
            if status not in {"active", "stopped"}:
                continue
            start_dt = _minute_floor_dt(_parse_datetime_value(interval.get("start_time")))
            if start_dt is None:
                continue
            drug_name = _infusion_display_drug_name(interval, "Дозатор")
            rate = "" if _is_gas_infusion(interval) else _format_infusion_rate(interval.get("current_rate_value"), interval.get("current_rate_unit"))
            declared_volume = _format_infusion_declared_volume(interval)
            gas_dose = _gas_dose_text(interval) if _is_gas_infusion(interval) else ""
            is_rate_infusion = _infusion_has_rate(interval)
            badge = "Газ" if gas_dose else "Дозатор" if is_rate_infusion else "Капельница"
            detail = f"старт {rate}" if rate else f"старт {gas_dose}" if gas_dose else declared_volume or "старт"
            start_event_id, _revision = self._infusion_identity(interval)
            events.append(
                {
                    "kind": "infusion",
                    "role": "start",
                    "time": start_dt,
                    "drug": drug_name,
                    "detail": detail,
                    "badge": badge,
                    "interval": interval,
                    "sort_id": start_event_id or 0,
                }
            )

            if _is_gas_infusion(interval):
                dose_history = _gas_dose_events(interval)
                for index, change in enumerate(dose_history):
                    change_dt = _minute_floor_dt(_parse_datetime_value((change or {}).get("event_time")))
                    if change_dt is None:
                        continue
                    if index == 0 and change_dt == start_dt:
                        continue
                    change_dose = _normalize_gas_dose_text(str((change or {}).get("dose_text") or ""))
                    events.append(
                        {
                            "kind": "infusion",
                            "role": "change",
                            "time": change_dt,
                            "drug": drug_name,
                            "detail": f"доза {change_dose}" if change_dose else "изменение дозы",
                            "badge": "Изм. доза",
                            "interval": interval,
                            "sort_id": self._timeline_event_numeric_id((change or {}).get("event_id")),
                        }
                    )
            else:
                rate_history = list(interval.get("rate_history") or [])
                for index, change in enumerate(rate_history):
                    change_dt = _minute_floor_dt(_parse_datetime_value((change or {}).get("event_time")))
                    if change_dt is None:
                        continue
                    if index == 0 and change_dt == start_dt:
                        continue
                    change_rate = _format_infusion_rate((change or {}).get("rate_value"), (change or {}).get("rate_unit"))
                    events.append(
                        {
                            "kind": "infusion",
                            "role": "change",
                            "time": change_dt,
                            "drug": drug_name,
                            "detail": f"скорость {change_rate}" if change_rate else "изменение скорости",
                            "badge": "Изм. скорость",
                            "interval": interval,
                            "sort_id": self._timeline_event_numeric_id((change or {}).get("event_id")),
                        }
                    )

            end_dt = _minute_floor_dt(_parse_datetime_value(interval.get("end_time")))
            if end_dt is not None and status == "stopped":
                events.append(
                    {
                        "kind": "infusion",
                        "role": "stop",
                        "time": end_dt,
                        "drug": drug_name,
                        "detail": "стоп",
                        "badge": "Стоп",
                        "interval": interval,
                        "sort_id": start_event_id or 0,
                    }
                )

        events.sort(
            key=lambda item: (
                _minute_floor_dt(_parse_datetime_value(item.get("time"))) or datetime.min,
                int(item.get("sort_id") or 0),
                str(item.get("badge") or ""),
            ),
            reverse=True,
        )
        return events

    def _build_medication_order_groups(self, rows) -> list[dict]:
        groups: dict[str, dict] = {}

        def ensure_group(drug_name: str) -> dict:
            clean_name = str(drug_name or "").strip() or "Без названия"
            key = clean_name.casefold()
            return groups.setdefault(
                key,
                {
                    "key": key,
                    "drug_name": clean_name,
                    "entries": [],
                    "order_rows": [],
                    "infusion_rows": [],
                    "latest_dt": datetime.min,
                    "first_id": None,
                    "has_bolus_order": False,
                    "has_gas_order": False,
                    "has_gas_infusion": False,
                    "has_rate_infusion": False,
                    "has_volume_infusion": False,
                },
            )

        for raw_row in rows or []:
            row = dict(raw_row or {})
            row_dt = _minute_floor_dt(_parse_datetime_value(row.get("datetime")))
            if row_dt is None:
                continue
            raw_drug_name, dose = _split_order_drug_and_dose(str(row.get("text") or ""))
            drug_name = self._order_display_drug_name(row, raw_drug_name)
            row["drug_name"] = drug_name
            row["raw_drug_name"] = raw_drug_name
            row["dose_text"] = dose
            row["route"] = _order_route_code(row)
            order_kind = self._order_preset_kind(row)
            row["order_kind"] = order_kind
            group = ensure_group(drug_name)
            group["order_rows"].append(row)
            group["has_bolus_order"] = bool(group["has_bolus_order"] or order_kind == "bolus")
            group["has_gas_order"] = bool(group["has_gas_order"] or order_kind == "gas")
            row_id = _safe_int(row.get("id")) or 0
            group["entries"].append(
                {
                    "kind": "order",
                    "order_kind": order_kind,
                    "time": row_dt,
                    "detail": _order_dose_text_with_route(dose, row, short=False)
                    or str(row.get("text") or "").strip(),
                    "row": row,
                    "sort_id": row_id,
                }
            )
            if row_dt > group["latest_dt"]:
                group["latest_dt"] = row_dt
            if row_id and (group["first_id"] is None or row_id < group["first_id"]):
                group["first_id"] = row_id

        snapshot = getattr(self, "_current_timeline_snapshot", None) or {}
        for raw_interval in snapshot.get("infusion_intervals") or []:
            interval = dict(raw_interval or {})
            status = str(interval.get("status") or "")
            if status not in {"active", "stopped"}:
                continue
            start_dt = _minute_floor_dt(_parse_datetime_value(interval.get("start_time")))
            end_dt = _minute_floor_dt(_parse_datetime_value(interval.get("end_time")))
            row_dt = end_dt or start_dt
            if row_dt is None:
                continue
            drug_name = _infusion_display_drug_name(interval, "Дозатор")
            group = ensure_group(drug_name)
            group["infusion_rows"].append(interval)
            group["has_gas_infusion"] = bool(group["has_gas_infusion"] or _is_gas_infusion(interval))
            group["has_rate_infusion"] = bool(group["has_rate_infusion"] or _infusion_has_rate(interval))
            group["has_volume_infusion"] = bool(group["has_volume_infusion"] or _is_volume_only_infusion(interval))
            start_event_id, _revision = self._infusion_identity(interval)
            group["entries"].append(
                {
                    "kind": "infusion",
                    "time": row_dt,
                    "detail": self._infusion_history_entry_text(interval),
                    "interval": interval,
                    "sort_id": start_event_id or 0,
                }
            )
            if row_dt > group["latest_dt"]:
                group["latest_dt"] = row_dt
            if start_event_id and (group["first_id"] is None or start_event_id < group["first_id"]):
                group["first_id"] = start_event_id

        result = list(groups.values())
        for group in result:
            group["entries"].sort(
                key=lambda item: (
                    _minute_floor_dt(_parse_datetime_value(item.get("time"))) or datetime.min,
                    int(item.get("sort_id") or 0),
                ),
                reverse=True,
            )
            group["total_text"] = self._medication_group_total_text(group)
            group["source_signature"] = self._medication_group_source_signature(group)
        result.sort(
            key=lambda group: (
                group.get("latest_dt") if isinstance(group.get("latest_dt"), datetime) else datetime.min,
                int(group.get("first_id") or 0),
                str(group.get("drug_name") or "").casefold(),
            ),
            reverse=True,
        )
        return result

    @staticmethod
    def _medication_group_source_signature(group: dict) -> str:
        order_rows = []
        for row in group.get("order_rows") or []:
            order_rows.append(
                {
                    "id": row.get("id"),
                    "datetime": row.get("datetime"),
                    "text": row.get("text"),
                    "drug_key": row.get("drug_key"),
                    "drug_display_name": row.get("drug_display_name"),
                    "order_kind": row.get("order_kind"),
                    "comment": row.get("comment"),
                    "route": _order_route_code(row),
                    "status": row.get("status"),
                    "revision": row.get("revision"),
                    "updated_at": row.get("updated_at"),
                }
            )
        infusion_rows = []
        for interval in group.get("infusion_rows") or []:
            infusion_rows.append(
                {
                    "interval_id": interval.get("interval_id"),
                    "drug_label": interval.get("drug_label"),
                    "display_label": interval.get("display_label"),
                    "start_time": interval.get("start_time"),
                    "end_time": interval.get("end_time"),
                    "status": interval.get("status"),
                    "volume_ml": interval.get("volume_ml"),
                    "current_rate_value": interval.get("current_rate_value"),
                    "current_rate_unit": interval.get("current_rate_unit"),
                    "rate_history": interval.get("rate_history") or [],
                    "event_ids": interval.get("event_ids") or [],
                    "payload": interval.get("payload") or {},
                }
            )
        return _stable_ui_hash(
            {
                "key": group.get("key"),
                "drug_name": group.get("drug_name"),
                "orders": order_rows,
                "infusions": infusion_rows,
            }
        )

    def _medication_group_render_signature(self, group: dict) -> str:
        key = str(group.get("key") or "").casefold()
        return _stable_ui_hash(
            {
                "source": str(group.get("source_signature") or ""),
                "collapsed": key in getattr(self, "_collapsed_order_group_keys", set()),
            }
        )

    def _medication_group_total_text(self, group: dict) -> str:
        parts: list[str] = []
        order_rows = list(group.get("order_rows") or [])
        infusion_rows = list(group.get("infusion_rows") or [])
        order_total = _summarize_order_total(order_rows) if order_rows else ""
        if order_total.startswith("Итого: "):
            parts.append(order_total.removeprefix("Итого: "))
        elif order_total:
            parts.append(order_total)

        total_volume = Decimal("0")
        has_volume = False
        active_count = 0
        for interval in infusion_rows:
            if str((interval or {}).get("status") or "") == "active":
                active_count += 1
            volume_text = _format_infusion_executed_volume(interval) if _infusion_has_rate(interval) else ""
            volume = _counted_infusion_volume_ml(interval or {})
            if volume is None and volume_text:
                volume = _decimal_from_ru_number(volume_text.replace("мл", ""))
            if (
                volume is None
                and _is_volume_only_infusion(interval or {})
                and str((interval or {}).get("status") or "") != "active"
            ):
                volume = _infusion_declared_volume_ml(interval or {})
            if volume is None:
                continue
            total_volume += volume
            has_volume = True
        if has_volume:
            parts.append(_format_infusion_volume_ml(total_volume))
        elif active_count:
            parts.append(f"активно: {active_count}")

        return f"Итого: {', '.join(parts)}" if parts else "Итого: нет дозы"

    def _medication_visual(self, group: dict) -> dict:
        name = str(group.get("drug_name") or "").casefold()
        if (
            group.get("has_gas_order")
            or group.get("has_gas_infusion")
            or "газ" in name
            or "sevo" in name
            or "сево" in name
            or "desfl" in name
            or "десфл" in name
            or "isofl" in name
            or "изофл" in name
            or "oxygen" in name
            or "кислород" in name
        ):
            return {
                "color": "#E0F2FE",
                "icon": "drop_white",
                "icon_file": type_icon_key("gas"),
                "icon_size": 30,
                "time_bg": "#E0F2FE",
                "time_fg": "#0369A1",
            }
        if group.get("has_rate_infusion") or "noradren" in name or "норадрен" in name:
            return {
                "color": "#FFF7ED",
                "icon": "infusion_white",
                "icon_file": type_icon_key("continuous_infusion"),
                "icon_size": 30,
                "time_bg": "#DBEAFE",
                "time_fg": "#1D4ED8",
            }
        if group.get("has_volume_infusion") or "nacl" in name or "натрия" in name or "раств" in name:
            return {
                "color": "#EAF3FF",
                "icon": "drop_white",
                "icon_file": type_icon_key("timed_infusion"),
                "icon_size": 30,
                "time_bg": "#CCFBF1",
                "time_fg": "#0F766E",
            }
        if group.get("has_bolus_order"):
            return {
                "color": "#EAFBF5",
                "icon": "syringe_white",
                "icon_file": type_icon_key("bolus"),
                "icon_size": 30,
                "time_bg": "#DCFCE7",
                "time_fg": "#15803D",
            }
        return {
            "color": "#EAFBF5",
            "icon": "syringe_white",
            "icon_file": type_icon_key("bolus"),
            "icon_size": 30,
            "time_bg": "#DCFCE7",
            "time_fg": "#15803D",
        }

    def _group_icon_frame(self, visual: dict) -> QFrame:
        frame = QFrame()
        frame.setFixedSize(44, 44)
        frame.setStyleSheet(
            f"background-color: {visual.get('color') or '#2563EB'}; border: none; border-radius: 10px;"
        )
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        icon = QLabel()
        icon.setAlignment(Qt.AlignCenter)
        icon_size = int(visual.get("icon_size") or 22)
        icon_file = visual.get("icon_file")
        pixmap = load_operblock_icon_pixmap(icon_file) if icon_file else QPixmap()
        if not pixmap.isNull():
            pixmap = pixmap.scaled(icon_size, icon_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        else:
            pixmap = self._operblock_ui_icon(str(visual.get("icon") or "syringe_white")).pixmap(icon_size, icon_size)
        if not pixmap.isNull():
            icon.setPixmap(pixmap)
        layout.addWidget(icon, 1)
        return frame

    @staticmethod
    def _active_infusion_icon_file(interval: dict) -> str:
        if _is_gas_infusion(interval or {}):
            return type_icon_key("gas")
        return type_icon_key("continuous_infusion") if _infusion_has_rate(interval or {}) else type_icon_key("timed_infusion")

    @staticmethod
    def _active_infusion_title_text(interval: dict) -> str:
        fallback = "Газ" if _is_gas_infusion(interval or {}) else "Дозатор"
        base = _infusion_display_drug_name(interval or {}, fallback)
        if not base:
            base = fallback
        if _is_gas_infusion(interval or {}):
            return base
        return base

    def _make_medication_group_card(self, group: dict) -> QWidget:
        visual = self._medication_visual(group)
        key = str(group.get("key") or group.get("drug_name") or "").casefold()
        collapsed = key in self._collapsed_order_group_keys
        frame = QFrame()
        frame.setObjectName("operblockMedicationGroupCard")
        frame.setStyleSheet(
            f"""
            QFrame#operblockMedicationGroupCard {{
                background-color: #FFFFFF;
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 12px;
            }}
            QLabel {{
                background: transparent;
                border: none;
            }}
            """
        )
        outer = QHBoxLayout(frame)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(12)
        outer.addWidget(self._group_icon_frame(visual), 0, Qt.AlignTop)

        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(9)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        title_col = QVBoxLayout()
        title_col.setContentsMargins(0, 0, 0, 0)
        title_col.setSpacing(3)
        name_label = ElidedTooltipLabel(str(group.get("drug_name") or "Без названия"))
        name_label.setStyleSheet(
            f"font-size: 15px; font-weight: 500; color: {OPERBLOCK_ORDERS_TEXT}; background: transparent; border: none;"
            f"{TOOLTIP_WHITE_STYLE}"
        )
        total_label = ElidedTooltipLabel(str(group.get("total_text") or "Итого: нет дозы"))
        total_label.setProperty("operblock_group_total_key", key)
        total_label.setStyleSheet(
            f"font-size: 12px; font-weight: 400; color: {OPERBLOCK_ORDERS_MUTED}; background: transparent; border: none;"
            f"{TOOLTIP_WHITE_STYLE}"
        )
        title_col.addWidget(name_label)
        title_col.addWidget(total_label)
        latest_dt = group.get("latest_dt")
        latest_label = QLabel(latest_dt.strftime("%H:%M") if isinstance(latest_dt, datetime) else "")
        latest_label.setStyleSheet(f"font-size: 13px; font-weight: 400; color: {OPERBLOCK_ORDERS_MUTED};")
        latest_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        toggle_button = QPushButton("⌄" if collapsed else "⌃")
        toggle_button.setFixedSize(28, 28)
        toggle_button.setCursor(Qt.PointingHandCursor)
        toggle_button.setStyleSheet(self._small_icon_button_style())
        toggle_button.clicked.connect(lambda _=False, group_key=key: self._toggle_medication_group(group_key))
        header.addLayout(title_col, 1)
        header.addWidget(latest_label, 0)
        header.addWidget(toggle_button, 0)
        body.addLayout(header)

        if not collapsed:
            for entry in group.get("entries") or []:
                body.addWidget(self._make_medication_entry_row(entry, visual))
        outer.addLayout(body, 1)
        return frame

    def _make_medication_entry_row(self, entry: dict, visual: dict) -> QWidget:
        row = QFrame()
        row.setObjectName("operblockMedicationEntryRow")
        row.setStyleSheet(
            f"""
            QFrame#operblockMedicationEntryRow {{
                background-color: #F8FAFC;
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 8px;
            }}
            QLabel {{
                background: transparent;
                border: none;
            }}
            """
        )
        layout = QHBoxLayout(row)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(8)
        time_label = QLabel(_format_order_time(entry.get("time")))
        time_label.setFixedWidth(54)
        time_label.setAlignment(Qt.AlignCenter)
        time_label.setStyleSheet(
            f"font-size: 12px; font-weight: 500; color: {visual.get('time_fg')}; "
            f"background-color: {visual.get('time_bg')}; border: none; border-radius: 6px; padding: 4px 6px;"
        )
        detail_text = str(entry.get("detail") or "")
        detail_label = ElidedTooltipLabel(detail_text)
        entry_row = entry.get("row") if isinstance(entry.get("row"), dict) else {}
        entry_order_id = _safe_int((entry_row or {}).get("id"))
        if entry_order_id:
            detail_label.setProperty("operblock_order_id", int(entry_order_id))
        detail_label.setStyleSheet(
            f"font-size: 13px; font-weight: 400; color: {OPERBLOCK_ORDERS_TEXT}; background: transparent; border: none;"
            f"{TOOLTIP_WHITE_STYLE}"
        )
        layout.addWidget(time_label, 0)
        layout.addWidget(detail_label, 1)

        if entry.get("kind") == "order":
            row_data = dict(entry.get("row") or {})
            edit_button = self._visible_order_action_button("Изменить", "pencil")
            delete_button = self._visible_order_action_button("Удалить", "trash", danger=True)
            edit_button.clicked.connect(lambda _=False, payload=row_data: self._edit_order_with_time(dict(payload)))
            delete_button.clicked.connect(lambda _=False, payload=row_data: self._delete_order(dict(payload)))
            layout.addWidget(edit_button, 0)
            layout.addWidget(delete_button, 0)
        else:
            interval = dict(entry.get("interval") or {})
            edit_button = self._visible_infusion_action_button("Изменить", "pencil")
            edit_button.clicked.connect(lambda _=False, payload=interval: self._edit_infusion_from_group_row(dict(payload)))
            layout.addWidget(edit_button, 0)
            if str(interval.get("status") or "") == "active":
                stop_button = self._visible_infusion_action_button("Стоп", "stop", danger=True)
                stop_button.clicked.connect(lambda _=False, payload=interval: self._stop_infusion(dict(payload)))
                layout.addWidget(stop_button, 0)
            delete_button = self._visible_infusion_action_button("Удалить", "trash", danger=True)
            delete_button.clicked.connect(lambda _=False, payload=interval: self._delete_infusion(dict(payload)))
            layout.addWidget(delete_button, 0)
        return row

    def _toggle_medication_group(self, group_key: str):
        key = str(group_key or "").casefold()
        if key in self._collapsed_order_group_keys:
            self._collapsed_order_group_keys.discard(key)
        else:
            self._collapsed_order_group_keys.add(key)
        self._apply_orders({"orders": getattr(self, "_current_orders_rows", [])})

    def _edit_infusion_from_group_row(self, interval: dict):
        if _is_gas_infusion(interval):
            self._change_gas_dose(interval, include_time=True)
            return
        if _infusion_has_rate(interval):
            if str(interval.get("status") or "") == "active":
                self._change_infusion_rate(interval, include_time=True)
            else:
                self._edit_infusion_start_time(interval)
            return
        self._change_infusion_volume(interval, include_time=True)

    def _small_icon_button_style(self) -> str:
        return f"""
            QPushButton {{
                background-color: #FFFFFF;
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 6px;
                color: {OPERBLOCK_ORDERS_MUTED};
                font-size: 14px;
                font-weight: 500;
            }}
            QPushButton:hover {{
                background-color: #F1F5F9;
                color: {OPERBLOCK_ORDERS_TEXT};
            }}
        """

    def _visible_action_button(self, text: str, icon_name: str, *, danger: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setIcon(self._operblock_ui_icon(icon_name))
        button.setIconSize(QSize(14, 14))
        button.setFixedHeight(30)
        button.setMinimumWidth(82 if text != "Удалить" else 92)
        button.setCursor(Qt.PointingHandCursor)
        border = "#FECACA" if danger else OPERBLOCK_ORDERS_BORDER
        color = "#DC2626" if danger else OPERBLOCK_ORDERS_TEXT
        hover = "#FFF5F5" if danger else "#F1F5F9"
        button.setStyleSheet(
            f"""
            QPushButton {{
                background-color: #FFFFFF;
                border: 1px solid {border};
                border-radius: 7px;
                color: {color};
                padding: 5px 9px;
                font-size: 12px;
                font-weight: 500;
            }}
            QPushButton:hover {{
                background-color: {hover};
            }}
            QPushButton:disabled {{
                color: {OPERBLOCK_ORDERS_MUTED};
                background-color: #F1F5F9;
                border-color: {OPERBLOCK_ORDERS_BORDER};
            }}
            """
        )
        return button

    def _visible_order_action_button(self, text: str, icon_name: str, *, danger: bool = False) -> QPushButton:
        button = self._visible_action_button(text, icon_name, danger=danger)
        self._order_action_buttons.append(button)
        return button

    def _visible_infusion_action_button(self, text: str, icon_name: str, *, danger: bool = False) -> QPushButton:
        button = self._visible_action_button(text, icon_name, danger=danger)
        self._order_action_buttons.append(button)
        return button

    @staticmethod
    def _event_badge_style(badge: str) -> str:
        bg, color = OPERBLOCK_EVENT_COLORS.get(str(badge or ""), ("#F2F6F8", "#506070"))
        return (
            f"font-size: 11px; font-weight: 500; color: {color}; background-color: {bg}; "
            "border: none; border-radius: 4px; padding: 3px 8px;"
        )

    def _make_timeline_event_row(self, event: dict) -> QWidget:
        frame = QFrame()
        frame.setObjectName("timelineEventRow")
        frame.setStyleSheet(
            f"""
            QFrame#timelineEventRow {{
                background-color: {OPERBLOCK_ORDERS_CARD_BG};
                border: none;
                border-bottom: 1px solid {OPERBLOCK_ORDERS_BORDER};
            }}
            QLabel {{
                background: transparent;
                border: none;
            }}
            QPushButton {{
                background-color: transparent;
                border: 1px solid transparent;
                border-radius: 4px;
                color: {OPERBLOCK_ORDERS_MUTED};
                font-size: 16px;
                font-weight: 500;
            }}
            QPushButton:hover {{
                background-color: #F1F5F9;
                border-color: {OPERBLOCK_ORDERS_BORDER};
                color: {OPERBLOCK_ORDERS_TEXT};
            }}
            """
        )
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(0, 7, 0, 7)
        layout.setSpacing(10)

        time_label = QLabel(_format_order_time(event.get("time")))
        time_label.setFixedWidth(52)
        time_label.setAlignment(Qt.AlignCenter)
        time_label.setStyleSheet(f"font-size: 13px; font-weight: 500; color: {OPERBLOCK_ORDERS_TEXT};")

        drug_label = ElidedTooltipLabel(str(event.get("drug") or "Без названия"))
        drug_label.setMinimumWidth(150)
        drug_label.setStyleSheet(
            f"font-size: 13px; font-weight: 500; color: {OPERBLOCK_ORDERS_TEXT}; background: transparent; border: none;"
            f"{TOOLTIP_WHITE_STYLE}"
        )

        detail_label = ElidedTooltipLabel(str(event.get("detail") or ""))
        detail_label.setMinimumWidth(80)
        detail_label.setStyleSheet(
            f"font-size: 13px; color: {OPERBLOCK_ORDERS_MUTED}; background: transparent; border: none;"
            f"{TOOLTIP_WHITE_STYLE}"
        )

        badge_label = QLabel(str(event.get("badge") or "Событие"))
        badge_label.setAlignment(Qt.AlignCenter)
        badge_label.setStyleSheet(self._event_badge_style(str(event.get("badge") or "")))

        menu_button = QPushButton("⋯")
        menu_button.setFixedSize(30, 28)
        menu_button.setCursor(Qt.PointingHandCursor)
        if event.get("kind") == "order":
            self._order_action_buttons.append(menu_button)
            menu_button.clicked.connect(
                lambda _=False, button=menu_button, row=dict(event.get("row") or {}): self._show_order_actions_menu(button, row)
            )
        else:
            self._infusion_action_buttons.append(menu_button)
            menu_button.clicked.connect(
                lambda _=False, button=menu_button, interval=dict(event.get("interval") or {}): self._show_infusion_actions_menu(
                    button,
                    interval,
                )
            )

        layout.addWidget(time_label, 0)
        layout.addWidget(drug_label, 2)
        layout.addWidget(detail_label, 2)
        layout.addWidget(badge_label, 0)
        layout.addWidget(menu_button, 0)
        return frame

    def _actions_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.setStyleSheet(
            f"""
            QMenu {{
                background-color: {OPERBLOCK_ORDERS_CARD_BG};
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 4px;
                color: {OPERBLOCK_ORDERS_TEXT};
                padding: 4px;
            }}
            QMenu::item {{
                padding: 6px 28px 6px 10px;
                border-radius: 3px;
            }}
            QMenu::item:selected {{
                background-color: #EEF3FF;
                color: {OPERBLOCK_ORDERS_ACCENT};
            }}
            """
        )
        return menu

    def _show_order_actions_menu(self, button: QPushButton, row: dict):
        menu = self._actions_menu()
        menu.addAction("Изменить", lambda payload=dict(row): self._edit_order_with_time(payload))
        menu.addSeparator()
        menu.addAction("Удалить", lambda payload=dict(row): self._delete_order(payload))
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    def _show_infusion_actions_menu(self, button: QPushButton, interval: dict):
        menu = self._actions_menu()
        status = str(interval.get("status") or "")
        menu.addAction("Изменить", lambda payload=dict(interval): self._edit_infusion_from_group_row(payload))
        if status == "active":
            menu.addAction("Стоп", lambda payload=dict(interval): self._stop_infusion(payload))
        menu.addSeparator()
        menu.addAction("Удалить", lambda payload=dict(interval): self._delete_infusion(payload))
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    def _apply_active_infusions(self, *, force_elapsed: bool = False):
        layout = getattr(self, "active_infusions_list", None)
        if layout is None:
            return
        snapshot = getattr(self, "_current_timeline_snapshot", None) or {}
        infusions = [
            dict(interval or {})
            for interval in snapshot.get("infusion_intervals") or []
            if str((interval or {}).get("status") or "") == "active"
        ]
        infusions.sort(key=lambda item: _parse_datetime_value(item.get("start_time")) or datetime.max)
        render_signature = self._active_infusions_render_signature_for(infusions)
        if render_signature == getattr(self, "_active_infusions_render_signature", "") and not force_elapsed:
            if hasattr(self, "active_infusions_count_label"):
                self.active_infusions_count_label.setText(str(len(infusions)))
            self._sync_active_infusions_scroll_height(len(infusions))
            return
        if hasattr(self, "active_infusions_count_label"):
            self.active_infusions_count_label.setText(str(len(infusions)))
        self._sync_active_infusions_scroll_height(len(infusions))
        if not infusions:
            self._clear_layout(layout)
            self._infusion_action_buttons = []
            self._rendered_active_infusion_widgets = {}
            self._rendered_active_infusion_signatures = {}
            self._rendered_active_infusion_order = []
            self._active_infusions_empty_widget = _label("Активных дозаторов, капельниц и газов нет", size=13, color=OPERBLOCK_ORDERS_MUTED)
            self._active_infusions_empty_widget.setMinimumHeight(OPERBLOCK_ACTIVE_INFUSION_CARD_MIN_HEIGHT)
            self._active_infusions_empty_widget.setAlignment(Qt.AlignCenter)
            layout.addWidget(self._active_infusions_empty_widget, 0, 0, 1, OPERBLOCK_ACTIVE_INFUSION_COLUMNS)
            self._active_infusions_render_signature = render_signature
            return
        if getattr(self, "_active_infusions_empty_widget", None) is not None:
            self._clear_layout(layout)
            self._active_infusions_empty_widget = None
            self._rendered_active_infusion_widgets = {}
            self._rendered_active_infusion_signatures = {}
            self._rendered_active_infusion_order = []

        old_widgets = dict(getattr(self, "_rendered_active_infusion_widgets", {}) or {})
        old_signatures = dict(getattr(self, "_rendered_active_infusion_signatures", {}) or {})
        old_order = list(getattr(self, "_rendered_active_infusion_order", []) or list(old_widgets.keys()))
        new_order = [self._active_infusion_key(interval) for interval in infusions]
        new_keys = {self._active_infusion_key(interval) for interval in infusions}
        for key, widget in old_widgets.items():
            if key in new_keys:
                continue
            layout.removeWidget(widget)
            widget.deleteLater()

        new_widgets: dict[str, QWidget] = {}
        new_signatures: dict[str, str] = {}
        if old_order == new_order:
            for index, interval in enumerate(infusions):
                key = self._active_infusion_key(interval)
                signature = self._active_infusion_card_signature(interval)
                widget = old_widgets.get(key)
                if widget is None or old_signatures.get(key) != signature:
                    if widget is not None:
                        layout.removeWidget(widget)
                        widget.deleteLater()
                    widget = self._make_active_infusion_card(interval)
                    layout.addWidget(
                        widget,
                        index // OPERBLOCK_ACTIVE_INFUSION_COLUMNS,
                        index % OPERBLOCK_ACTIVE_INFUSION_COLUMNS,
                    )
                new_widgets[key] = widget
                new_signatures[key] = signature
        else:
            for index, interval in enumerate(infusions):
                key = self._active_infusion_key(interval)
                signature = self._active_infusion_card_signature(interval)
                widget = old_widgets.get(key)
                if widget is None or old_signatures.get(key) != signature:
                    if widget is not None:
                        layout.removeWidget(widget)
                        widget.deleteLater()
                    widget = self._make_active_infusion_card(interval)
                else:
                    layout.removeWidget(widget)
                layout.addWidget(
                    widget,
                    index // OPERBLOCK_ACTIVE_INFUSION_COLUMNS,
                    index % OPERBLOCK_ACTIVE_INFUSION_COLUMNS,
                )
                new_widgets[key] = widget
                new_signatures[key] = signature
        self._rendered_active_infusion_widgets = new_widgets
        self._rendered_active_infusion_signatures = new_signatures
        self._rendered_active_infusion_order = new_order
        self._active_infusions_render_signature = render_signature

    def _sync_active_infusions_scroll_height(self, infusion_count: int) -> None:
        scroll = getattr(self, "active_infusions_scroll", None)
        content = getattr(self, "active_infusions_content", None)
        if scroll is None or content is None:
            return
        count = max(0, int(infusion_count or 0))
        if count <= 0:
            content_height = OPERBLOCK_ACTIVE_INFUSION_EMPTY_HEIGHT
        else:
            row_count = max(1, (count + OPERBLOCK_ACTIVE_INFUSION_COLUMNS - 1) // OPERBLOCK_ACTIVE_INFUSION_COLUMNS)
            content_height = (
                row_count * OPERBLOCK_ACTIVE_INFUSION_CARD_MIN_HEIGHT
                + max(0, row_count - 1) * OPERBLOCK_ACTIVE_INFUSION_GRID_SPACING
            )
        viewport_height = (
            OPERBLOCK_ACTIVE_INFUSION_EMPTY_HEIGHT
            if count <= 0
            else OPERBLOCK_ACTIVE_INFUSION_CARD_MIN_HEIGHT
        )
        content.setMinimumHeight(content_height)
        scroll.setMinimumHeight(viewport_height)
        scroll.setMaximumHeight(viewport_height)

    def _active_infusions_render_signature_for(self, infusions: list[dict]) -> str:
        return _stable_ui_hash(
            {
                "columns": OPERBLOCK_ACTIVE_INFUSION_COLUMNS,
                "minute": datetime.now().replace(second=0, microsecond=0).isoformat(timespec="minutes"),
                "order": [self._active_infusion_key(interval) for interval in infusions],
                "items": {
                    self._active_infusion_key(interval): self._active_infusion_source_signature(interval)
                    for interval in infusions
                },
            }
        )

    def _active_infusion_card_signature(self, interval: dict) -> str:
        return _stable_ui_hash(
            {
                "minute": datetime.now().replace(second=0, microsecond=0).isoformat(timespec="minutes"),
                "source": self._active_infusion_source_signature(interval),
            }
        )

    def _active_infusion_key(self, interval: dict) -> str:
        start_event_id, _revision = self._infusion_identity(interval)
        return str(interval.get("interval_id") or start_event_id or f"{interval.get('drug_label')}:{interval.get('start_time')}")

    @staticmethod
    def _active_infusion_source_signature(interval: dict) -> str:
        return _stable_ui_hash(
            {
                "interval_id": interval.get("interval_id"),
                "drug_label": interval.get("drug_label"),
                "display_label": interval.get("display_label"),
                "start_time": interval.get("start_time"),
                "end_time": interval.get("end_time"),
                "status": interval.get("status"),
                "volume_ml": interval.get("volume_ml"),
                "current_rate_value": interval.get("current_rate_value"),
                "current_rate_unit": interval.get("current_rate_unit"),
                "rate_history": interval.get("rate_history") or [],
                "payload": interval.get("payload") or {},
            }
        )

    def _make_active_infusion_card(self, interval: dict) -> QWidget:
        frame = QFrame()
        frame.setObjectName("operblockActiveInfusion")
        frame.setMinimumHeight(OPERBLOCK_ACTIVE_INFUSION_CARD_MIN_HEIGHT)
        frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        frame.setStyleSheet(
            f"""
            QFrame#operblockActiveInfusion {{
                background-color: #FFFFFF;
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 12px;
            }}
            QLabel {{
                background: transparent;
                border: none;
            }}
            QPushButton {{
                background-color: #ffffff;
                border: 1px solid {OPERBLOCK_ORDERS_BORDER};
                border-radius: 7px;
                color: {OPERBLOCK_ORDERS_TEXT};
                padding: 5px 10px;
                font-size: 12px;
                font-weight: 500;
            }}
            QPushButton:hover {{
                background-color: #F8FAFC;
            }}
            QPushButton:disabled {{
                color: {OPERBLOCK_ORDERS_MUTED};
                background-color: #F1F5F9;
            }}
            """
        )
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        icon_box = QFrame()
        icon_box.setFixedSize(44, 44)
        icon_box.setStyleSheet("background-color: #EAF3FF; border: none; border-radius: 10px;")
        icon_layout = QVBoxLayout(icon_box)
        icon_layout.setContentsMargins(0, 0, 0, 0)
        icon = QLabel()
        icon.setAlignment(Qt.AlignCenter)
        pixmap = load_operblock_icon_pixmap(self._active_infusion_icon_file(interval))
        if not pixmap.isNull():
            pixmap = pixmap.scaled(30, 30, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        else:
            pixmap = self._operblock_ui_icon("infusion_blue").pixmap(24, 24)
        if not pixmap.isNull():
            icon.setPixmap(pixmap)
        icon_layout.addWidget(icon, 1)
        layout.addWidget(icon_box, 0, Qt.AlignTop)

        has_rate = _infusion_has_rate(interval)
        is_active = str(interval.get("status") or "") == "active" and not interval.get("end_time")
        name = ElidedTooltipLabel(self._active_infusion_title_text(interval))
        name.setStyleSheet(
            f"font-size: 15px; font-weight: 500; color: {OPERBLOCK_ORDERS_TEXT}; background: transparent; border: none;"
            f"{TOOLTIP_WHITE_STYLE}"
        )
        rate = "" if _is_gas_infusion(interval) else _format_infusion_rate(interval.get("current_rate_value"), interval.get("current_rate_unit"))
        gas_dose = _gas_dose_text(interval) if _is_gas_infusion(interval) else ""
        duration = _format_infusion_duration(interval.get("start_time"))
        detail_parts = [f"с {_format_order_time(interval.get('start_time'))}"]
        if duration:
            detail_parts.append(duration)
        if rate:
            detail_parts.append(rate)
        if gas_dose:
            detail_parts.append(f"доза {gas_dose}")
        declared_volume = _format_infusion_declared_volume(interval)
        if not rate and not gas_dose and declared_volume:
            detail_parts.append(f"объем {declared_volume}")
        if rate:
            executed_volume = _format_infusion_executed_volume(interval)
            if executed_volume:
                detail_parts.append(f"введено: {executed_volume}")
        elif declared_volume and not is_active:
            detail_parts.append(f"введено: {declared_volume}")
        detail = ElidedTooltipLabel(" · ".join(detail_parts))
        detail.setStyleSheet(f"font-size: 12px; font-weight: 400; color: {OPERBLOCK_ORDERS_MUTED};")

        progress = QProgressBar()
        progress.setRange(0, 100)
        progress.setValue(self._active_infusion_progress_percent(interval))
        progress.setTextVisible(False)
        progress.setFixedHeight(6)
        progress.setStyleSheet(
            f"""
            QProgressBar {{
                background-color: #E2E8F0;
                border: none;
                border-radius: 3px;
            }}
            QProgressBar::chunk {{
                background-color: {OPERBLOCK_ORDERS_ACCENT};
                border-radius: 3px;
            }}
            """
        )
        volume_label = ElidedTooltipLabel(self._active_infusion_volume_text(interval))
        volume_label.setStyleSheet(f"font-size: 12px; font-weight: 400; color: {OPERBLOCK_ORDERS_TEXT};")

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(6)
        text_col.addWidget(name)
        text_col.addWidget(detail)
        text_col.addWidget(progress)
        text_col.addWidget(volume_label)
        layout.addLayout(text_col, 1)

        buttons_col = QVBoxLayout()
        buttons_col.setContentsMargins(0, 0, 0, 0)
        buttons_col.setSpacing(8)
        change_button = QPushButton("Изм.")
        change_button.setIcon(self._operblock_ui_icon("pencil"))
        change_button.setIconSize(QSize(14, 14))
        stop_button = QPushButton(OPERBLOCK_INFUSION_STOP_BUTTON_TEXT)
        stop_button.setIcon(self._operblock_ui_icon("stop"))
        stop_button.setIconSize(QSize(14, 14))
        stop_button.setStyleSheet(
            f"""
            QPushButton {{
                background-color: #FFFFFF;
                border: 1px solid #FECACA;
                border-radius: 7px;
                color: #DC2626;
                padding: 5px 10px;
                font-size: 12px;
                font-weight: 500;
            }}
            QPushButton:hover {{ background-color: #FFF5F5; }}
            QPushButton:disabled {{
                color: {OPERBLOCK_ORDERS_MUTED};
                background-color: #F1F5F9;
                border-color: {OPERBLOCK_ORDERS_BORDER};
            }}
            """
        )
        buttons = (change_button, stop_button)
        for button in buttons:
            button.setFixedHeight(30)
            button.setMinimumWidth(78)
            button.setCursor(Qt.PointingHandCursor)
            self._infusion_action_buttons.append(button)
        if _is_gas_infusion(interval):
            change_button.clicked.connect(lambda _=False, payload=dict(interval): self._change_gas_dose(payload, include_time=True))
        elif has_rate:
            change_button.clicked.connect(lambda _=False, payload=dict(interval): self._change_infusion_rate(payload))
        else:
            change_button.clicked.connect(
                lambda _=False, payload=dict(interval): self._change_infusion_volume(payload, include_time=True)
            )
        stop_button.clicked.connect(lambda _=False, payload=dict(interval): self._stop_infusion(payload))
        buttons_col.addWidget(change_button, 0)
        buttons_col.addWidget(stop_button, 0)
        buttons_col.addStretch(1)
        layout.addLayout(buttons_col, 0)
        return frame

    @staticmethod
    def _active_infusion_progress_percent(interval: dict) -> int:
        if _is_gas_infusion(interval or {}):
            return 40 if str((interval or {}).get("status") or "") == "active" and not (interval or {}).get("end_time") else 100
        declared = _infusion_declared_volume_ml(interval or {})
        executed = _infusion_volume_ml(interval or {})
        if declared is not None and declared > 0 and executed is not None:
            try:
                return max(0, min(100, int((executed / declared * Decimal("100")).to_integral_value(rounding=ROUND_HALF_UP))))
            except Exception:
                return 0
        if declared is not None and not _infusion_has_rate(interval or {}):
            if str((interval or {}).get("status") or "") == "active" and not (interval or {}).get("end_time"):
                return 0
            return 100
        return 40

    @staticmethod
    def _active_infusion_volume_text(interval: dict) -> str:
        if _is_gas_infusion(interval or {}):
            dose_text = _gas_dose_text(interval or {})
            if dose_text:
                return f"Доза газа: {dose_text}"
            return "Доза газа: --"
        if _infusion_has_rate(interval or {}):
            executed = _format_infusion_executed_volume(interval or {})
            declared = _format_infusion_declared_volume(interval or {})
            if executed and declared:
                progress = OperBlockMainWidget._active_infusion_progress_percent(interval or {})
                return f"Введено: {executed} ({progress}%)"
            if executed:
                return f"Введено: {executed}"
        declared = _format_infusion_declared_volume(interval or {})
        if declared:
            if str((interval or {}).get("status") or "") == "active" and not (interval or {}).get("end_time"):
                return f"Будет введено: {declared}"
            return f"Введено: {declared} (100%)"
        return "Будет введено: --"

    def _set_infusion_action_buttons_enabled(self, enabled: bool):
        for button in list(getattr(self, "_infusion_action_buttons", [])):
            try:
                button.setEnabled(bool(enabled))
            except RuntimeError:
                continue

    def _set_protocol_write_controls_enabled(self, enabled: bool):
        orders_enabled = bool(enabled) and self._orders_tab_enabled()
        if not hasattr(self, "save_order_button"):
            return
        self.save_order_button.setEnabled(orders_enabled)
        if hasattr(self, "order_input"):
            self.order_input.setEnabled(orders_enabled)
        if hasattr(self, "order_dose_input"):
            self.order_dose_input.setEnabled(orders_enabled)
        if hasattr(self, "order_rate_input"):
            self.order_rate_input.setEnabled(orders_enabled)
        if hasattr(self, "order_type_combo"):
            self.order_type_combo.setEnabled(orders_enabled)
        if enabled:
            self.save_order_button.setText(OPERBLOCK_ADD_ORDER_BUTTON_TEXT)
        self._set_quick_order_buttons_enabled(orders_enabled)
        self._set_order_action_buttons_enabled(orders_enabled)
        self._set_infusion_action_buttons_enabled(orders_enabled)
        self._apply_protocol_controls_state()

    def _manual_order_kind(self) -> str:
        text = str(self.order_type_combo.currentText() if hasattr(self, "order_type_combo") else "").strip().casefold()
        if "газ" in text:
            return "gas"
        if "дозатор" in text:
            return "continuous_infusion"
        if "капель" in text:
            return "timed_infusion"
        return "bolus"

    def _update_manual_order_type_fields(self, *_args) -> None:
        kind = self._manual_order_kind()
        rate_visible = kind == "continuous_infusion"
        if hasattr(self, "order_rate_input"):
            self.order_rate_input.setVisible(rate_visible)
            self.order_rate_input.setEnabled(bool(not self._write_pending) and self._orders_tab_enabled())
        if hasattr(self, "order_dose_input"):
            if kind == "continuous_infusion":
                self.order_dose_input.setPlaceholderText("Доза (мг)")
            elif kind == "timed_infusion":
                self.order_dose_input.setPlaceholderText("Объем/доза (мл, мг)")
            elif kind == "gas":
                self.order_dose_input.setPlaceholderText("Доза (MAC)")
            else:
                self.order_dose_input.setPlaceholderText("Доза (мг, мл, %)")

    def _ensure_infusion_write_context_or_warn(self) -> bool:
        if not self._current_admission_id or not self._current_operation_case_id:
            return False
        if not self._current_case_active:
            CustomMessageBox.warning(self, "Случай закрыт", "Случай в операционной закрыт. Откройте его из архива или верните на стол.")
            self.refresh_protocol(force=True)
            return False
        if not self._current_anesthesia_active:
            CustomMessageBox.warning(self, "Пособие не начато", "Сначала нажмите «Начать пособие». До начала пособия назначения недоступны.")
            self.refresh_protocol(force=True)
            return False
        return True

    def _current_operation_event_time_text(self) -> str:
        now_dt = datetime.now().replace(second=0, microsecond=0)
        start_dt = _minute_floor_dt(self._current_anesthesia_start or self._current_operation_start)
        if start_dt and now_dt < start_dt:
            now_dt = start_dt
        return now_dt.isoformat(timespec="seconds")

    def _current_operation_event_datetime(self) -> datetime:
        return (
            _minute_floor_dt(_parse_datetime_value(self._current_operation_event_time_text()))
            or datetime.now().replace(second=0, microsecond=0)
        )

    @staticmethod
    def _local_iso_minute_text(value) -> str:
        event_dt = _minute_floor_dt(_parse_datetime_value(value))
        if event_dt is None:
            event_dt = datetime.now().replace(second=0, microsecond=0)
        return event_dt.isoformat(timespec="seconds")

    def _resolve_operation_event_time_text(self, time_text: str) -> str | None:
        clean_time = str(time_text or "").strip()
        if not clean_time:
            return None
        try:
            base_date = self._current_protocol_date or self._current_operation_start or datetime.now()
            event_dt = self.operblock_vitals_service.resolve_datetime(clean_time, base_date)
        except Exception:
            return None
        event_dt = _minute_floor_dt(event_dt)
        return event_dt.isoformat(timespec="seconds") if event_dt is not None else None

    @staticmethod
    def _infusion_identity(interval: dict) -> tuple[int | None, int | None]:
        payload = interval.get("payload") if isinstance(interval.get("payload"), dict) else {}
        start_event_id = _safe_int((payload or {}).get("start_event_id"))
        if start_event_id is None:
            interval_id = str(interval.get("interval_id") or "")
            if interval_id.startswith("infusion:"):
                start_event_id = _safe_int(interval_id.split(":", 1)[1])
        expected_revision = _safe_int((payload or {}).get("start_revision"))
        return start_event_id, expected_revision

    @staticmethod
    def _infusion_preset_payload(interval: dict) -> dict | None:
        payload = interval.get("payload") if isinstance(interval.get("payload"), dict) else {}
        result = {
            key: payload.get(key)
            for key in (
                "preset_id",
                "source_drug_id",
                "label",
                "display_name",
                "latin",
                "kind",
                "concentration",
                "solvent_id",
                "solvent_label",
                "solvent_volume_ml",
                "volume_ml",
                "dose_text",
                "display_dose_text",
                "calculated_volume_ml",
                "declared_total_volume_ml",
                "duration_min",
            )
            if payload.get(key) not in (None, "", [])
        }
        return result or None

    @staticmethod
    def _latest_infusion_event_datetime(interval: dict) -> datetime | None:
        latest_dt = _minute_floor_dt(_parse_datetime_value((interval or {}).get("start_time")))
        for history_key in ("rate_history", "dose_history"):
            for item in list((interval or {}).get(history_key) or []):
                event_dt = _minute_floor_dt(_parse_datetime_value((item or {}).get("event_time")))
                if event_dt is not None and (latest_dt is None or event_dt > latest_dt):
                    latest_dt = event_dt
        return latest_dt

    def _active_gas_interval(self) -> dict | None:
        snapshot = getattr(self, "_current_timeline_snapshot", None) or {}
        intervals = [
            dict(interval or {})
            for interval in snapshot.get("infusion_intervals") or []
            if str((interval or {}).get("status") or "") == "active" and _is_gas_infusion(interval or {})
        ]
        if not intervals:
            return None
        intervals.sort(key=lambda item: _parse_datetime_value(item.get("start_time")) or datetime.max)
        return intervals[0]

    def _update_gas_dose_direct(
        self,
        interval: dict,
        dose_text: str,
        *,
        event_time: str | None = None,
        start_event_time: str | None = None,
        source_key: str = "operblock_update_gas_dose",
        on_saved=None,
    ) -> None:
        if self._write_pending or not self._ensure_infusion_write_context_or_warn():
            return
        start_event_id, expected_revision = self._infusion_identity(interval)
        if start_event_id is None or expected_revision is None:
            CustomMessageBox.warning(self, "Газ", "Не удалось проверить актуальность газа. Обновите протокол.")
            self.refresh_protocol(force=True)
            return
        clean_dose = _normalize_gas_dose_text(dose_text)
        if not clean_dose:
            CustomMessageBox.warning(self, "Газ", "Укажите дозу газа, например: 0,7 MAC.")
            return
        change_event_time = event_time or self._current_operation_event_time_text()
        if not self._validate_infusion_event_datetime_or_warn(change_event_time):
            return
        if start_event_time is not None and not self._validate_infusion_event_datetime_or_warn(start_event_time):
            return
        current_dose = _gas_dose_text(interval)
        has_rate_artifacts = bool(
            interval.get("current_rate_value")
            or interval.get("current_rate_unit")
            or list(interval.get("rate_history") or [])
        )
        if clean_dose == current_dose and event_time is None and start_event_time is None and not has_rate_artifacts:
            return
        payload = self._infusion_preset_payload(interval) or {}
        payload["kind"] = "gas"
        payload["dose_text"] = clean_dose
        payload["display_dose_text"] = clean_dose
        quick_scroll_state = self._remember_quick_orders_scroll_state()
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)
        self._restore_quick_orders_scroll_state_later(quick_scroll_state)
        write_description = f"{source_key}:{start_event_id}"
        if on_saved is None:
            self._remember_local_write_refresh_suppression(write_description, {"operblock_timeline_events"})

        def operation():
            return self.operblock_service.change_gas_dose(
                start_event_id,
                expected_revision=expected_revision,
                dose_text=clean_dose,
                event_time=change_event_time,
                start_event_time=start_event_time,
                payload=payload,
            )

        self._enqueue_write(
            write_description,
            operation,
            on_success=(
                (lambda _result: on_saved())
                if on_saved is not None
                else (
                    lambda result, sid=start_event_id, dose=clean_dose, change_dt=change_event_time, start_dt=start_event_time: self._on_gas_dose_saved_locally(
                        result,
                        sid,
                        dose_text=dose,
                        change_event_time=change_dt,
                        start_event_time=start_dt,
                    )
                )
            ),
            on_error=lambda exc: self._on_infusion_mutation_error(exc),
        )

    def _start_quick_infusion(
        self,
        drug_name: str,
        rate_text: str,
        *,
        concentration_text: str = "",
        preset_payload: dict | None = None,
    ):
        if self._write_pending or not self._ensure_infusion_write_context_or_warn():
            return
        rate_value, rate_unit = _split_infusion_rate_text(rate_text)
        if not rate_value or not rate_unit:
            CustomMessageBox.warning(self, "Дозатор", "Укажите скорость в мл/час, например: 1 мл/час.")
            return
        event_time = self._current_operation_event_time_text()
        if not self._validate_infusion_event_datetime_or_warn(event_time):
            return
        quick_scroll_state = self._remember_quick_orders_scroll_state()
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)
        self._restore_quick_orders_scroll_state_later(quick_scroll_state)

        def operation():
            return self.operblock_service.start_infusion(
                self._current_admission_id,
                self._current_operation_case_id,
                drug_name,
                rate_value,
                rate_unit,
                event_time,
                concentration_text=concentration_text,
                payload=preset_payload,
            )

        self._enqueue_write(
            f"operblock_start_infusion:{self._current_admission_id}",
            operation,
            on_success=lambda _result: self._on_infusion_mutation_saved(),
            on_error=lambda exc: self._on_infusion_mutation_error(exc),
        )

    def _start_preset_infusion(self, preset: dict, rate_text: str):
        drug_name = operblock_medication_preset_display_name(preset)
        if not drug_name:
            CustomMessageBox.warning(self, "Дозатор", "Укажите препарат для дозатора.")
            return
        self._start_quick_infusion(
            drug_name,
            rate_text,
            concentration_text=str(preset.get("concentration") or "").strip(),
            preset_payload=build_operblock_preset_payload(preset),
        )

    def _edit_infusion_start_time(self, interval: dict):
        if self._write_pending or not self._ensure_infusion_write_context_or_warn():
            return
        start_event_id, expected_revision = self._infusion_identity(interval)
        if start_event_id is None or expected_revision is None:
            CustomMessageBox.warning(self, "Дозатор", "Не удалось проверить актуальность дозатора. Обновите протокол.")
            self.refresh_protocol(force=True)
            return
        old_start_dt = _minute_floor_dt(_parse_datetime_value(interval.get("start_time")))
        if old_start_dt is None:
            CustomMessageBox.warning(self, "Дозатор", "Не удалось определить время начала дозатора. Обновите протокол.")
            self.refresh_protocol(force=True)
            return
        drug_name = _infusion_display_drug_name(interval, "Дозатор")
        dialog = TimeEditDialog(f"Время начала: {drug_name}", old_start_dt, self, field_label="Время начала")
        if dialog.exec() != QDialog.Accepted:
            return
        event_time = dialog.datetime_text()
        if not self._validate_infusion_event_datetime_or_warn(event_time):
            return
        new_start_dt = _minute_floor_dt(_parse_datetime_value(event_time))
        if new_start_dt == old_start_dt:
            return
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)

        def operation():
            return self.operblock_service.update_infusion_start_time(
                start_event_id,
                expected_revision=expected_revision,
                event_time=event_time,
            )

        write_description = f"operblock_update_infusion_start:{start_event_id}"
        self._remember_local_write_refresh_suppression(write_description, {"operblock_timeline_events"})
        self._enqueue_write(
            write_description,
            operation,
            on_success=lambda _result, sid=start_event_id, dt=event_time: self._on_infusion_start_time_saved_locally(sid, dt),
            on_error=lambda exc: self._on_infusion_mutation_error(exc),
        )

    def _change_gas_dose(self, interval: dict, *, include_time: bool = False):
        if self._write_pending or not self._ensure_infusion_write_context_or_warn():
            return
        start_event_id, expected_revision = self._infusion_identity(interval)
        if start_event_id is None or expected_revision is None:
            CustomMessageBox.warning(self, "Газ", "Не удалось проверить актуальность газа. Обновите протокол.")
            self.refresh_protocol(force=True)
            return
        current_dose = _gas_dose_text(interval)
        old_start_dt = _minute_floor_dt(_parse_datetime_value(interval.get("start_time")))
        if include_time and old_start_dt is None:
            CustomMessageBox.warning(self, "Газ", "Не удалось определить время начала газа. Обновите протокол.")
            self.refresh_protocol(force=True)
            return
        drug_name = _infusion_display_drug_name(interval, "Газ")
        dialog = GasDoseDialog(
            drug_name,
            current_dose,
            self,
            start_datetime=old_start_dt,
            min_datetime=self._current_anesthesia_start or self._current_operation_start,
            max_datetime=self._current_anesthesia_end,
            show_time=include_time,
            action_text="Сохранить",
            payload=interval.get("payload") if isinstance(interval.get("payload"), dict) else None,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        dose_text = _normalize_gas_dose_text(dialog.volume_text())
        if not dose_text:
            CustomMessageBox.warning(self, "Газ", "Укажите дозу газа, например: 0,7 MAC.")
            return
        start_event_time = None
        new_start_dt = old_start_dt
        if include_time:
            start_event_time = self._resolve_operation_event_time_text(dialog.start_time_text())
            if not self._validate_infusion_event_datetime_or_warn(start_event_time):
                return
            new_start_dt = _minute_floor_dt(_parse_datetime_value(start_event_time))
        has_rate_artifacts = bool(
            interval.get("current_rate_value")
            or interval.get("current_rate_unit")
            or list(interval.get("rate_history") or [])
        )
        dose_changed = dose_text != current_dose or has_rate_artifacts
        start_changed = bool(include_time and new_start_dt != old_start_dt)
        if not dose_changed and not start_changed:
            return
        if not dose_changed and start_changed:
            self._write_pending = True
            self._set_protocol_write_controls_enabled(False)

            def operation():
                return self.operblock_service.update_infusion_start_time(
                    start_event_id,
                    expected_revision=expected_revision,
                    event_time=start_event_time,
                )

            write_description = f"operblock_update_gas_start:{start_event_id}"
            self._remember_local_write_refresh_suppression(write_description, {"operblock_timeline_events"})
            self._enqueue_write(
                write_description,
                operation,
                on_success=lambda _result, sid=start_event_id, dt=start_event_time: self._on_infusion_start_time_saved_locally(sid, dt),
                on_error=lambda exc: self._on_infusion_mutation_error(exc),
            )
            return
        self._update_gas_dose_direct(
            interval,
            dose_text,
            start_event_time=start_event_time if start_changed else None,
            source_key="operblock_update_gas_dose",
        )

    def _change_infusion_rate(self, interval: dict, *, include_time: bool = False):
        if self._write_pending or not self._ensure_infusion_write_context_or_warn():
            return
        start_event_id, expected_revision = self._infusion_identity(interval)
        if start_event_id is None or expected_revision is None:
            CustomMessageBox.warning(self, "Дозатор", "Не удалось проверить актуальность дозатора. Обновите протокол.")
            self.refresh_protocol(force=True)
            return
        current_rate = _format_infusion_rate(interval.get("current_rate_value"), interval.get("current_rate_unit"))
        old_start_dt = _minute_floor_dt(_parse_datetime_value(interval.get("start_time")))
        if include_time and old_start_dt is None:
            CustomMessageBox.warning(self, "Дозатор", "Не удалось определить время начала дозатора. Обновите протокол.")
            self.refresh_protocol(force=True)
            return
        dialog_title = "Изменить дозатор" if include_time else f"{OPERBLOCK_INFUSION_CHANGE_BUTTON_TEXT} скорость"
        dialog = InfusionRateDialog(
            dialog_title,
            current_rate,
            self,
            drug_name=_infusion_display_drug_name(interval, "Дозатор"),
            start_datetime=old_start_dt if include_time else None,
            min_datetime=self._current_anesthesia_start or self._current_operation_start,
            max_datetime=self._current_anesthesia_end,
            action_text="Сохранить",
        )
        if dialog.exec() != QDialog.Accepted:
            return
        rate_value, rate_unit = _split_infusion_rate_text(dialog.rate_text())
        if not rate_value or not rate_unit:
            CustomMessageBox.warning(self, "Дозатор", "Укажите скорость в мл/час, например: 1 мл/час.")
            return
        normalized_rate = _format_infusion_rate(rate_value, rate_unit)
        rate_changed = normalized_rate != current_rate
        start_event_time = None
        new_start_dt = old_start_dt
        if include_time:
            start_event_time = self._resolve_operation_event_time_text(dialog.start_time_text())
            if not self._validate_infusion_event_datetime_or_warn(start_event_time):
                return
            new_start_dt = _minute_floor_dt(_parse_datetime_value(start_event_time))
        start_changed = bool(include_time and new_start_dt != old_start_dt)
        if not rate_changed and not start_changed:
            return
        change_event_time = self._current_operation_event_time_text()
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)

        def operation():
            if not rate_changed:
                return self.operblock_service.update_infusion_start_time(
                    start_event_id,
                    expected_revision=expected_revision,
                    event_time=start_event_time,
                )
            return self.operblock_service.change_infusion_rate(
                start_event_id,
                expected_revision=expected_revision,
                new_rate_value=rate_value,
                new_rate_unit=rate_unit,
                event_time=change_event_time,
                start_event_time=start_event_time if start_changed else None,
                payload=self._infusion_preset_payload(interval),
            )

        write_description = f"operblock_change_infusion:{start_event_id}"
        self._remember_local_write_refresh_suppression(write_description, {"operblock_timeline_events"})
        self._enqueue_write(
            write_description,
            operation,
            on_success=(
                (lambda _result, sid=start_event_id, dt=start_event_time: self._on_infusion_start_time_saved_locally(sid, dt))
                if not rate_changed
                else (
                    lambda result, sid=start_event_id, rv=rate_value, ru=rate_unit, change_dt=change_event_time, start_dt=start_event_time if start_changed else None: self._on_infusion_rate_saved_locally(
                        result,
                        sid,
                        rate_value=rv,
                        rate_unit=ru,
                        change_event_time=change_dt,
                        start_event_time=start_dt,
                    )
                )
            ),
            on_error=lambda exc: self._on_infusion_mutation_error(exc),
        )

    def _change_infusion_volume(self, interval: dict, *, include_time: bool = False):
        if self._write_pending or not self._ensure_infusion_write_context_or_warn():
            return
        start_event_id, expected_revision = self._infusion_identity(interval)
        if start_event_id is None or expected_revision is None:
            CustomMessageBox.warning(self, "Капельница", "Не удалось проверить актуальность капельницы. Обновите протокол.")
            self.refresh_protocol(force=True)
            return
        current_volume = _format_infusion_declared_volume(interval).replace(" мл", "")
        old_start_dt = _minute_floor_dt(_parse_datetime_value(interval.get("start_time")))
        if include_time and old_start_dt is None:
            CustomMessageBox.warning(self, "Капельница", "Не удалось определить время начала капельницы. Обновите протокол.")
            self.refresh_protocol(force=True)
            return
        drug_name = _infusion_display_drug_name(interval, "Капельница")
        dialog = InfusionVolumeDialog(
            f"{'Изменить капельницу' if include_time else 'Правка объема'}: {drug_name}",
            current_volume,
            self,
            drug_name=_infusion_display_drug_name(interval, "Капельница"),
            start_datetime=old_start_dt,
            min_datetime=self._current_anesthesia_start or self._current_operation_start,
            max_datetime=self._current_anesthesia_end,
            show_time=include_time,
            action_text="Сохранить",
            field_label="Объем: мл",
        )
        if dialog.exec() != QDialog.Accepted:
            return
        volume = _normalize_volume_ml_text(dialog.volume_text())
        if not volume:
            CustomMessageBox.warning(self, "Капельница", "Укажите объем в мл, например: 200 мл.")
            return
        event_time = None
        new_start_dt = old_start_dt
        if include_time:
            event_time = self._resolve_operation_event_time_text(dialog.start_time_text())
            if not self._validate_infusion_event_datetime_or_warn(event_time):
                return
            new_start_dt = _minute_floor_dt(_parse_datetime_value(event_time))
        if volume == current_volume and (not include_time or new_start_dt == old_start_dt):
            return
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)

        def operation():
            return self.operblock_service.update_infusion_volume(
                start_event_id,
                expected_revision=expected_revision,
                volume_ml=volume,
                event_time=event_time,
                payload=self._infusion_preset_payload(interval),
            )

        write_description = f"operblock_update_infusion_volume:{start_event_id}"
        self._remember_local_write_refresh_suppression(write_description, {"operblock_timeline_events"})
        self._enqueue_write(
            write_description,
            operation,
            on_success=lambda _result, sid=start_event_id, vol=volume, dt=event_time: self._on_infusion_volume_saved_locally(
                sid,
                volume_ml=vol,
                event_time=dt,
            ),
            on_error=lambda exc: self._on_infusion_mutation_error(exc),
        )

    def _stop_infusion(self, interval: dict):
        if self._write_pending or not self._ensure_infusion_write_context_or_warn():
            return
        start_event_id, expected_revision = self._infusion_identity(interval)
        if start_event_id is None or expected_revision is None:
            CustomMessageBox.warning(self, "Дозатор", "Не удалось проверить актуальность дозатора. Обновите протокол.")
            self.refresh_protocol(force=True)
            return
        drug_name = _infusion_display_drug_name(interval, "назначение")
        start_dt = _minute_floor_dt(_parse_datetime_value(interval.get("start_time")))
        if start_dt is None:
            CustomMessageBox.warning(self, "Время остановки", "Не удалось определить время начала назначения. Обновите протокол.")
            self.refresh_protocol(force=True)
            return
        if _is_gas_infusion(interval):
            infusion_kind = "gas"
        elif _is_volume_only_infusion(interval):
            infusion_kind = "volume"
        else:
            infusion_kind = "rate"
        dialog = InfusionStopDialog(
            drug_name,
            self,
            start_datetime=self._current_operation_event_datetime(),
            min_datetime=self._latest_infusion_event_datetime(interval) or start_dt,
            max_datetime=self._current_anesthesia_end if not self._current_anesthesia_active else None,
            infusion_kind=infusion_kind,
            payload=interval.get("payload") if isinstance(interval.get("payload"), dict) else None,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        event_time = dialog.datetime_text()
        if not self._validate_infusion_stop_datetime_or_warn(event_time, interval):
            return
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)

        def operation():
            return self.operblock_service.stop_infusion(
                start_event_id,
                expected_revision=expected_revision,
                event_time=event_time,
                payload=self._infusion_preset_payload(interval),
            )

        self._enqueue_write(
            f"operblock_stop_infusion:{start_event_id}",
            operation,
            on_success=lambda _result: self._on_infusion_mutation_saved(),
            on_error=lambda exc: self._on_infusion_mutation_error(exc),
        )

    def _delete_infusion(self, interval: dict):
        if self._write_pending or not self._ensure_infusion_write_context_or_warn():
            return
        start_event_id, expected_revision = self._infusion_identity(interval)
        if start_event_id is None or expected_revision is None:
            CustomMessageBox.warning(self, "Дозатор", "Не удалось проверить актуальность дозатора. Обновите протокол.")
            self.refresh_protocol(force=True)
            return
        drug_name = _infusion_display_drug_name(interval, "назначение")
        is_active = str(interval.get("status") or "") == "active"
        title = "Удалить активное назначение" if is_active else "Удаление назначения"
        message = (
            f"Удалить активное назначение из истории и графика?\n{drug_name}"
            if is_active
            else f"Удалить назначение из истории и графика?\n{drug_name}"
        )
        reply = CustomMessageBox.question(
            self,
            title,
            message,
            CustomMessageBox.Yes | CustomMessageBox.No,
            CustomMessageBox.No,
        )
        if reply != CustomMessageBox.Yes:
            return
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)

        def operation():
            return self.operblock_service.delete_infusion(
                start_event_id,
                expected_revision=expected_revision,
            )

        self._enqueue_write(
            f"operblock_delete_infusion:{start_event_id}",
            operation,
            on_success=lambda _result: self._on_infusion_mutation_saved(),
            on_error=lambda exc: self._on_infusion_mutation_error(exc),
        )

    def _on_infusion_mutation_saved(self):
        quick_scroll_state = getattr(self, "_pending_quick_orders_scroll_state", None)
        self._pending_quick_orders_scroll_state = None
        self._write_pending = False
        self._set_protocol_write_controls_enabled(True)
        self.refresh_protocol(force=True)
        self._restore_quick_orders_scroll_state_later(quick_scroll_state)

    def _on_infusion_mutation_error(self, exc: Exception):
        quick_scroll_state = getattr(self, "_pending_quick_orders_scroll_state", None)
        self._pending_quick_orders_scroll_state = None
        self._write_pending = False
        self._set_protocol_write_controls_enabled(True)
        title = "Конфликт данных" if isinstance(exc, (DataConflictError, OperBlockConflictError)) else "Ошибка назначения"
        CustomMessageBox.warning(self, title, str(exc))
        self.refresh_protocol(force=True)
        self._restore_quick_orders_scroll_state_later(quick_scroll_state)

    def _clear_order_columns(self):
        columns = getattr(self, "orders_columns", [])
        for column in columns:
            self._clear_layout(column)

    def _stretch_order_columns(self):
        for column in getattr(self, "orders_columns", []):
            column.addStretch(1)

    def _capture_orders_scroll_state(self) -> dict:
        scroll = getattr(self, "orders_scroll", None)
        if scroll is None:
            return {"value": 0, "maximum": 0, "at_bottom": False}
        bar = scroll.verticalScrollBar()
        maximum = int(bar.maximum())
        value = int(bar.value())
        return {
            "value": value,
            "maximum": maximum,
            "at_bottom": maximum > 0 and value >= maximum - 2,
        }

    def _restore_orders_scroll_state(self, state: dict):
        scroll = getattr(self, "orders_scroll", None)
        if scroll is None:
            return
        bar = scroll.verticalScrollBar()
        if state.get("at_bottom"):
            bar.setValue(bar.maximum())
            return
        value = int(state.get("value") or 0)
        bar.setValue(max(0, min(value, bar.maximum())))

    def _orders_column_entry_sort_key(self, entry: tuple[str, dict]) -> tuple:
        kind, group = entry
        if kind == "infusion":
            return self._infusion_history_group_time_sort_key(group)
        return self._order_group_time_sort_key(group)

    def _build_infusion_history_groups(self) -> list[dict]:
        snapshot = getattr(self, "_current_timeline_snapshot", None) or {}
        groups: dict[str, dict] = {}
        for raw_interval in snapshot.get("infusion_intervals") or []:
            interval = dict(raw_interval or {})
            status = str(interval.get("status") or "")
            if status not in {"active", "stopped"}:
                continue
            drug_name = _infusion_display_drug_name(interval, "Дозатор")
            drug_name = drug_name or "Дозатор"
            key = drug_name.casefold()
            group = groups.setdefault(
                key,
                {
                    "drug_name": drug_name,
                    "column": OPERBLOCK_INFUSION_HISTORY_COLUMN,
                    "rows": [],
                    "latest_dt": datetime.min,
                    "first_dt": datetime.max,
                    "first_id": None,
                },
            )
            group["rows"].append(interval)
            start_dt = _parse_datetime_value(interval.get("start_time"))
            end_dt = _parse_datetime_value(interval.get("end_time"))
            row_dt = end_dt or start_dt or datetime.min
            if row_dt > group["latest_dt"]:
                group["latest_dt"] = row_dt
            if start_dt is not None and start_dt < group["first_dt"]:
                group["first_dt"] = start_dt
            start_event_id, _revision = self._infusion_identity(interval)
            if start_event_id and (group["first_id"] is None or start_event_id < group["first_id"]):
                group["first_id"] = start_event_id
        result = list(groups.values())
        for group in result:
            group["rows"].sort(
                key=lambda item: (
                    _parse_datetime_value(item.get("end_time"))
                    or _parse_datetime_value(item.get("start_time"))
                    or datetime.min,
                    self._infusion_identity(item)[0] or 0,
                ),
                reverse=True,
            )
        return result

    @staticmethod
    def _infusion_history_group_time_sort_key(group: dict) -> tuple:
        latest_dt = group.get("latest_dt")
        if not isinstance(latest_dt, datetime):
            latest_dt = datetime.min
        first_id = group.get("first_id")
        if first_id is None:
            first_id = 0
        return (latest_dt, first_id, str(group.get("drug_name") or "").casefold())

    def _preset_display_name_for_id(self, preset_id: str | None) -> str:
        key = str(preset_id or "").strip()
        if not key:
            return ""
        for preset in getattr(self, "_medication_presets", []) or []:
            if str((preset or {}).get("preset_id") or "").strip() == key:
                return operblock_medication_preset_display_name(preset or {})
        return ""

    def _order_display_drug_name(self, row: dict, fallback: str) -> str:
        display_name = str(row.get("drug_display_name") or "").strip()
        if display_name:
            return display_name
        display_name = self._preset_display_name_for_id(row.get("drug_key"))
        return display_name or fallback

    def _order_preset_kind(self, row: dict) -> str:
        drug_key = str((row or {}).get("drug_key") or "").strip()
        folded_key = drug_key.casefold()
        if folded_key.startswith(("manual:gas:", "quick:gas:", "gas:")):
            return "gas"
        if drug_key:
            for preset in getattr(self, "_medication_presets", []) or []:
                if str((preset or {}).get("preset_id") or "").strip() == drug_key:
                    kind = normalize_operblock_medication_preset_kind((preset or {}).get("kind"))
                    return "gas" if kind == "gas" else "bolus"
        return "bolus"

    def _build_order_groups(self, rows: list[dict]) -> list[dict]:
        groups: dict[str, dict] = {}
        for raw_row in rows:
            row = dict(raw_row or {})
            raw_drug_name, dose = _split_order_drug_and_dose(str(row.get("text") or ""))
            drug_name = self._order_display_drug_name(row, raw_drug_name)
            row["drug_name"] = drug_name
            row["raw_drug_name"] = raw_drug_name
            row["dose_text"] = dose
            row["route"] = _order_route_code(row)
            key = str(row.get("drug_key") or drug_name).casefold()
            group = groups.setdefault(
                key,
                {
                    "drug_name": drug_name,
                    "column": self._order_column_for_row(row, raw_drug_name),
                    "rows": [],
                    "latest_dt": datetime.min,
                    "first_dt": datetime.max,
                    "first_id": None,
                },
            )
            group["rows"].append(row)
            row_dt = _order_sort_dt(row)
            if row_dt > group["latest_dt"]:
                group["latest_dt"] = row_dt
            if row_dt != datetime.min and row_dt < group["first_dt"]:
                group["first_dt"] = row_dt
            row_id = _safe_int(row.get("id"))
            if row_id and (group["first_id"] is None or row_id < group["first_id"]):
                group["first_id"] = row_id
        result = list(groups.values())
        for group in result:
            group["rows"].sort(key=lambda item: (_order_sort_dt(item), _safe_int(item.get("id")) or 0), reverse=True)
        return result

    def _order_column_for_row(self, row: dict, drug_name: str) -> int:
        drug_key = str(row.get("drug_key") or "").strip()
        if drug_key:
            for preset in getattr(self, "_medication_presets", []) or []:
                if str(preset.get("preset_id") or "").strip() != drug_key:
                    continue
                kind = normalize_operblock_medication_preset_kind(preset.get("kind"))
                if kind == "timed_infusion":
                    return OPERBLOCK_INFUSION_HISTORY_COLUMN
                break
        return self._order_column_for_drug(drug_name)

    def _order_column_for_drug(self, drug_name: str) -> int:
        key = str(drug_name or "").casefold()
        for template in getattr(self, "_quick_order_templates", []) or []:
            if str(template.get("drug_name") or "").casefold() == key:
                return normalize_operblock_quick_order_group(template.get("group"))
        return 1

    @staticmethod
    def _order_group_time_sort_key(group: dict) -> tuple:
        latest_dt = group.get("latest_dt")
        if not isinstance(latest_dt, datetime):
            latest_dt = datetime.min
        first_id = group.get("first_id")
        if first_id is None:
            first_id = 0
        return (latest_dt, first_id, str(group.get("drug_name") or "").casefold())

    def _make_order_group_card(self, group: dict) -> QWidget:
        rows = list(group.get("rows") or [])
        frame = QFrame()
        frame.setObjectName("operblockOrderGroup")
        frame.setStyleSheet(
            f"""
            QFrame#operblockOrderGroup {{
                background-color: {BG_LIGHT};
                border: 1px solid {BORDER_COLOR};
                border-radius: {CUSTOM_DIALOG_RADIUS};
            }}
            QLabel {{
                background: transparent;
                border: none;
            }}
            QPushButton {{
                background-color: #ffffff;
                border: 1px solid {BORDER_COLOR};
                border-radius: 4px;
                color: {TEXT_PRIMARY};
                padding: 2px 6px;
                font-size: 11px;
            }}
            QPushButton:hover {{
                background-color: #eef3f6;
            }}
            QPushButton:disabled {{
                color: {TEXT_MUTED};
                background-color: {BG_MAIN};
            }}
            """
        )
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 7, 8, 7)
        layout.setSpacing(5)

        header = QVBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(3)
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(6)
        name_label = ElidedTooltipLabel(str(group.get("drug_name") or "Без названия"))
        name_label.setStyleSheet(
            f"font-size: 14px; font-weight: 800; color: {COLOR_PRIMARY_DARK}; background: transparent; border: none;"
            f"{TOOLTIP_WHITE_STYLE}"
        )
        latest_label = QLabel(f"{_format_order_time(rows[-1].get('datetime'))}" if rows else "")
        latest_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        latest_label.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")
        title_row.addWidget(name_label, 1)
        title_row.addWidget(latest_label, 0)
        total_label = ElidedTooltipLabel(_summarize_order_total(rows))
        total_label.setMinimumWidth(120)
        total_label.setStyleSheet(
            f"font-size: 12px; font-weight: 700; color: {TEXT_PRIMARY}; background-color: #ffffff; "
            f"border: 1px solid {BORDER_COLOR}; border-radius: 4px; padding: 2px 7px;"
            f"{TOOLTIP_WHITE_STYLE}"
        )
        header.addLayout(title_row)
        header.addWidget(total_label)
        layout.addLayout(header)

        current_day = None
        for row in rows:
            day = _format_order_day(row.get("datetime"))
            if day != current_day:
                current_day = day
                day_label = QLabel(day)
                day_label.setStyleSheet(
                    f"font-size: 11px; font-weight: 700; color: {TEXT_SECONDARY}; "
                    f"background-color: {BG_MAIN}; border-radius: 3px; padding: 1px 6px;"
                )
                layout.addWidget(day_label, 0, Qt.AlignLeft)
            layout.addLayout(self._make_order_entry_row(row))
        return frame

    def _make_infusion_history_group_card(self, group: dict) -> QWidget:
        rows = list(group.get("rows") or [])
        frame = QFrame()
        frame.setObjectName("operblockInfusionHistoryGroup")
        frame.setStyleSheet(
            f"""
            QFrame#operblockInfusionHistoryGroup {{
                background-color: {BG_LIGHT};
                border: 1px solid {BORDER_COLOR};
                border-radius: {CUSTOM_DIALOG_RADIUS};
            }}
            QLabel {{
                background: transparent;
                border: none;
            }}
            QPushButton {{
                background-color: #ffffff;
                border: 1px solid {BORDER_COLOR};
                border-radius: 4px;
                color: {TEXT_PRIMARY};
                padding: 2px 6px;
                font-size: 11px;
            }}
            QPushButton:hover {{
                background-color: #eef3f6;
            }}
            QPushButton:disabled {{
                color: {TEXT_MUTED};
                background-color: {BG_MAIN};
            }}
            """
        )
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 7, 8, 7)
        layout.setSpacing(5)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(6)
        name_label = ElidedTooltipLabel(str(group.get("drug_name") or "Дозатор"))
        name_label.setStyleSheet(
            f"font-size: 14px; font-weight: 800; color: {COLOR_PRIMARY_DARK}; background: transparent; border: none;"
            f"{TOOLTIP_WHITE_STYLE}"
        )
        latest_dt = group.get("latest_dt")
        latest_label = QLabel(latest_dt.strftime("%H:%M") if isinstance(latest_dt, datetime) else "")
        latest_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        latest_label.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")
        title_row.addWidget(name_label, 1)
        title_row.addWidget(latest_label, 0)
        layout.addLayout(title_row)

        total_label = QLabel(self._summarize_infusion_history(rows))
        total_label.setStyleSheet(
            f"font-size: 12px; font-weight: 700; color: {TEXT_PRIMARY}; background-color: #ffffff; "
            f"border: 1px solid {BORDER_COLOR}; border-radius: 4px; padding: 2px 7px;"
        )
        layout.addWidget(total_label)

        current_day = None
        for interval in rows:
            day = _format_order_day(interval.get("start_time"))
            if day != current_day:
                current_day = day
                day_label = QLabel(day)
                day_label.setStyleSheet(
                    f"font-size: 11px; font-weight: 700; color: {TEXT_SECONDARY}; "
                    f"background-color: {BG_MAIN}; border-radius: 3px; padding: 1px 6px;"
                )
                layout.addWidget(day_label, 0, Qt.AlignLeft)
            layout.addLayout(self._make_infusion_history_entry_row(interval))
        return frame

    @staticmethod
    def _summarize_infusion_history(rows: list[dict]) -> str:
        active_count = sum(1 for row in rows if str((row or {}).get("status") or "") == "active")
        stopped_count = len(rows) - active_count
        total_volume = Decimal("0")
        has_volume = False
        for row in rows:
            volume = _counted_infusion_volume_ml(row or {})
            if volume is None:
                continue
            total_volume += volume
            has_volume = True
        if active_count and stopped_count:
            status_text = f"активно: {active_count} · Остановлено: {stopped_count}"
        elif active_count:
            status_text = f"активно: {active_count}"
        else:
            status_text = f"Остановлено: {stopped_count}"
        if has_volume:
            return f"{status_text} · итого: {_format_infusion_volume_ml(total_volume)}"
        return status_text

    def _make_infusion_history_entry_row(self, interval: dict) -> QHBoxLayout:
        entry = QHBoxLayout()
        entry.setContentsMargins(0, 0, 0, 0)
        entry.setSpacing(6)
        time_label = QLabel(_format_order_time(interval.get("start_time")))
        time_label.setFixedWidth(48)
        time_label.setAlignment(Qt.AlignCenter)
        time_label.setStyleSheet(
            f"font-size: 12px; font-weight: 800; color: {COLOR_PRIMARY_DARK}; background-color: #ffffff; "
            f"border: 1px solid {BORDER_COLOR}; border-radius: 4px; padding: 2px;"
        )
        detail_label = ElidedTooltipLabel(self._infusion_history_entry_text(interval))
        detail_label.setStyleSheet(
            f"font-size: 13px; color: {TEXT_PRIMARY}; background: transparent; border: none;"
            f"{TOOLTIP_WHITE_STYLE}"
        )
        entry.addWidget(time_label, 0)
        entry.addWidget(detail_label, 1)
        edit_button = self._infusion_history_action_button("Изменить")
        edit_button.clicked.connect(lambda _=False, payload=dict(interval): self._edit_infusion_from_group_row(payload))
        entry.addWidget(edit_button, 0)
        if str(interval.get("status") or "") == "active":
            stop_button = self._infusion_history_action_button(OPERBLOCK_INFUSION_STOP_BUTTON_TEXT)
            stop_button.clicked.connect(lambda _=False, payload=dict(interval): self._stop_infusion(payload))
            entry.addWidget(stop_button, 0)
        delete_button = self._infusion_history_action_button("Удалить", danger=True)
        delete_button.clicked.connect(lambda _=False, payload=dict(interval): self._delete_infusion(payload))
        entry.addWidget(delete_button, 0)
        return entry

    @staticmethod
    def _infusion_history_entry_text(interval: dict) -> str:
        status = str(interval.get("status") or "")
        end_time = interval.get("end_time")
        rate = "" if _is_gas_infusion(interval) else _format_infusion_rate(interval.get("current_rate_value"), interval.get("current_rate_unit"))
        gas_dose = _gas_dose_text(interval) if _is_gas_infusion(interval) else ""
        if status == "active":
            parts = ["активно", f"с {_format_order_time(interval.get('start_time'))}"]
        else:
            parts = ["Стоп"]
            if end_time:
                parts.append(f"до {_format_order_time(end_time)}")
        duration = _format_infusion_interval_duration(interval.get("start_time"), end_time)
        if duration:
            parts.append(duration)
        if gas_dose:
            parts.append(f"доза: {gas_dose}")
        elif rate:
            parts.append(rate)
        if not gas_dose and rate:
            executed_volume = _format_infusion_executed_volume(interval)
            if executed_volume:
                label = "введено" if status == "active" else "итог"
                parts.append(f"{label}: {executed_volume}")
        elif not gas_dose:
            declared_volume = _format_infusion_declared_volume(interval)
            if declared_volume:
                label = "объем" if status == "active" else "итог"
                parts.append(f"{label}: {declared_volume}")
        return " · ".join(parts)

    def _make_order_entry_row(self, row: dict) -> QHBoxLayout:
        entry = QHBoxLayout()
        entry.setContentsMargins(0, 0, 0, 0)
        entry.setSpacing(6)
        time_label = QLabel(_format_order_time(row.get("datetime")))
        time_label.setFixedWidth(48)
        time_label.setAlignment(Qt.AlignCenter)
        time_label.setStyleSheet(
            f"font-size: 12px; font-weight: 800; color: {COLOR_PRIMARY_DARK}; background-color: #ffffff; "
            f"border: 1px solid {BORDER_COLOR}; border-radius: 4px; padding: 2px;"
        )
        dose_text = _order_dose_text_with_route(str(row.get("dose_text") or row.get("text") or ""), row, short=False)
        dose_label = ElidedTooltipLabel(dose_text)
        dose_label.setStyleSheet(
            f"font-size: 13px; color: {TEXT_PRIMARY}; background: transparent; border: none;"
            f"{TOOLTIP_WHITE_STYLE}"
        )
        edit_button = self._order_action_button("Изменить")
        delete_button = self._order_action_button("Удалить", danger=True)
        edit_button.clicked.connect(lambda _=False, payload=dict(row): self._edit_order_with_time(payload))
        delete_button.clicked.connect(lambda _=False, payload=dict(row): self._delete_order(payload))
        entry.addWidget(time_label, 0)
        entry.addWidget(dose_label, 1)
        entry.addWidget(edit_button, 0)
        entry.addWidget(delete_button, 0)
        return entry

    def _order_action_button(self, text: str, *, danger: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setFixedHeight(24)
        button.setCursor(Qt.PointingHandCursor)
        if danger:
            button.setStyleSheet(DANGER_BUTTON_STYLE + "QPushButton { padding: 2px 7px; font-size: 11px; }")
        self._order_action_buttons.append(button)
        return button

    def _infusion_history_action_button(self, text: str, *, danger: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setFixedHeight(24)
        button.setCursor(Qt.PointingHandCursor)
        if danger:
            button.setStyleSheet(DANGER_BUTTON_STYLE + "QPushButton { padding: 2px 7px; font-size: 11px; }")
        self._infusion_action_buttons.append(button)
        return button

    def _set_order_action_buttons_enabled(self, enabled: bool):
        for button in list(getattr(self, "_order_action_buttons", [])):
            try:
                button.setEnabled(bool(enabled))
            except RuntimeError:
                continue

    @staticmethod
    def _manual_single_order_payload(kind: str, drug_text: str) -> dict[str, str | bool]:
        clean_kind = normalize_operblock_medication_preset_kind(kind)
        clean_label = re.sub(r"\s+", " ", str(drug_text or "").strip())
        slug = re.sub(r"[^0-9A-Za-zА-Яа-яЁё]+", "_", clean_label.casefold()).strip("_") or "order"
        return {
            "preset_id": f"manual:{clean_kind}:{slug}",
            "kind": clean_kind,
            "label": clean_label,
            "display_name": clean_label,
            "manual_order": True,
        }

    def _save_order(self):
        if not self._current_admission_id or self._write_pending:
            return
        if not self._orders_tab_enabled():
            CustomMessageBox.warning(self, "Пособие не начато", "Назначения доступны только во время анестезиологического пособия.")
            self.refresh_protocol(force=True)
            return
        drug_text = self.order_input.text().strip()
        dose_text = self.order_dose_input.text().strip() if hasattr(self, "order_dose_input") else ""
        rate_text = self.order_rate_input.text().strip() if hasattr(self, "order_rate_input") else ""
        kind = self._manual_order_kind()
        if kind == "continuous_infusion":
            self._start_manual_continuous_infusion(drug_text, dose_text, rate_text)
            return
        if kind == "timed_infusion":
            self._start_manual_timed_infusion(drug_text, dose_text)
            return
        if kind == "gas":
            self._start_manual_gas(drug_text, dose_text)
            return

        text = drug_text
        if text and dose_text and dose_text.casefold() not in text.casefold():
            text = f"{text} {dose_text}".strip()
        if not text:
            CustomMessageBox.warning(self, "Ошибка", "Текст назначения не заполнен.")
            return
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)
        self.save_order_button.setText("Сохранение...")
        preset_payload = self._manual_single_order_payload(kind, drug_text) if kind == "gas" else None

        def operation():
            return self.operblock_service.add_order(self._current_admission_id, text, preset_payload=preset_payload)

        self._enqueue_write(
            f"operblock_add_{kind}_order:{self._current_admission_id}",
            operation,
            on_success=lambda _result: self._on_order_saved(),
            on_error=lambda exc: self._on_protocol_write_error(exc, self.save_order_button, OPERBLOCK_ADD_ORDER_BUTTON_TEXT),
        )

    def _on_order_saved(self):
        self._write_pending = False
        self._set_protocol_write_controls_enabled(True)
        self._clear_manual_order_inputs()
        self.refresh_protocol(force=True)

    def _clear_manual_order_inputs(self):
        if hasattr(self, "order_input"):
            self.order_input.clear()
        if hasattr(self, "order_dose_input"):
            self.order_dose_input.clear()
        if hasattr(self, "order_rate_input"):
            self.order_rate_input.clear()

    def _on_manual_infusion_saved(self):
        self._clear_manual_order_inputs()
        self._on_infusion_mutation_saved()

    def _manual_infusion_payload(self, kind: str, dose_text: str, volume: str = "") -> dict[str, str | bool]:
        payload: dict[str, str | bool] = {"kind": kind, "manual_order": True}
        clean_dose = re.sub(r"\s+", " ", str(dose_text or "").strip())
        if clean_dose:
            payload["dose_text"] = clean_dose
            payload["display_dose_text"] = clean_dose
        if volume:
            payload["volume_ml"] = volume
            payload["declared_total_volume_ml"] = volume
        return payload

    def _start_manual_continuous_infusion(self, drug_text: str, dose_text: str, rate_text: str):
        drug_name = re.sub(r"\s+", " ", str(drug_text or "").strip())
        clean_dose = re.sub(r"\s+", " ", str(dose_text or "").strip())
        if not drug_name:
            CustomMessageBox.warning(self, "Дозатор", "Укажите препарат для дозатора.")
            return
        rate_value, rate_unit = _split_infusion_rate_text(rate_text)
        if not rate_value or not rate_unit:
            CustomMessageBox.warning(self, "Дозатор", "Укажите скорость в мл/час, например: 1 мл/час.")
            return
        if clean_dose:
            drug_name = build_operblock_quick_order_text(drug_name, clean_dose)
        event_time = self._current_operation_event_time_text()
        if not self._validate_infusion_event_datetime_or_warn(event_time):
            return
        quick_scroll_state = self._remember_quick_orders_scroll_state()
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)
        self._restore_quick_orders_scroll_state_later(quick_scroll_state)
        payload = self._manual_infusion_payload("continuous_infusion", clean_dose)

        def operation():
            return self.operblock_service.start_infusion(
                self._current_admission_id,
                self._current_operation_case_id,
                drug_name,
                rate_value,
                rate_unit,
                event_time,
                payload=payload,
            )

        self._enqueue_write(
            f"operblock_manual_dozator:{self._current_admission_id}",
            operation,
            on_success=lambda _result: self._on_manual_infusion_saved(),
            on_error=lambda exc: self._on_infusion_mutation_error(exc),
        )

    def _start_manual_timed_infusion(self, drug_text: str, dose_text: str):
        drug_name = re.sub(r"\s+", " ", str(drug_text or "").strip())
        clean_dose = re.sub(r"\s+", " ", str(dose_text or "").strip())
        if not drug_name:
            CustomMessageBox.warning(self, "Капельница", "Укажите препарат для капельницы.")
            return
        if not clean_dose:
            CustomMessageBox.warning(self, "Капельница", "Укажите объем или дозу капельницы.")
            return
        volume = _normalize_volume_ml_text(clean_dose)
        event_time = self._current_operation_event_time_text()
        if not self._validate_infusion_event_datetime_or_warn(event_time):
            return
        quick_scroll_state = self._remember_quick_orders_scroll_state()
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)
        self._restore_quick_orders_scroll_state_later(quick_scroll_state)
        payload = self._manual_infusion_payload("timed_infusion", clean_dose, volume)
        infusion_drug_name = build_operblock_quick_order_text(drug_name, clean_dose) if clean_dose and not volume else drug_name

        def operation():
            return self.operblock_service.start_infusion(
                self._current_admission_id,
                self._current_operation_case_id,
                infusion_drug_name,
                None,
                "",
                event_time,
                volume_ml=volume,
                payload=payload,
            )

        self._enqueue_write(
            f"operblock_manual_timed_infusion:{self._current_admission_id}",
            operation,
            on_success=lambda _result: self._on_manual_infusion_saved(),
            on_error=lambda exc: self._on_infusion_mutation_error(exc),
        )

    def _start_gas_infusion(
        self,
        drug_text: str,
        dose_text: str,
        *,
        payload: dict | None = None,
        source_key: str = "operblock_gas",
        on_saved=None,
    ):
        if not self._current_admission_id or self._write_pending:
            return
        if not self._ensure_infusion_write_context_or_warn():
            return
        drug_name = re.sub(r"\s+", " ", str(drug_text or "").strip())
        clean_dose = _normalize_gas_dose_text(dose_text)
        if not drug_name:
            CustomMessageBox.warning(self, "Газ", "Укажите газ.")
            return
        if not clean_dose:
            CustomMessageBox.warning(self, "Газ", "Укажите дозу газа, например: 0,7 MAC.")
            return
        success_callback = on_saved or self._on_infusion_mutation_saved
        effective_payload = dict(payload or {}) if isinstance(payload, dict) else {}
        effective_payload["kind"] = "gas"
        effective_payload["dose_text"] = clean_dose
        effective_payload["display_dose_text"] = clean_dose
        effective_payload.setdefault("label", drug_name)
        effective_payload.setdefault("display_name", drug_name)
        active_gas = self._active_gas_interval()
        if active_gas is not None:
            if not _gas_identity_matches(active_gas, drug_name, effective_payload):
                active_name = _infusion_display_drug_name(active_gas, "Газ")
                requested_name = _gas_display_name_for_payload(drug_name, effective_payload)
                CustomMessageBox.warning(
                    self,
                    "Газ уже идет",
                    (
                        f"Сейчас активен газ: {active_name}.\n"
                        "Одновременное использование двух разных газов запрещено. "
                        f"Сначала остановите активный газ, затем назначьте {requested_name}."
                    ),
                )
                return
            self._update_gas_dose_direct(
                active_gas,
                clean_dose,
                source_key=f"{source_key}_active",
                on_saved=success_callback,
            )
            return
        event_time = self._current_operation_event_time_text()
        if not self._validate_infusion_event_datetime_or_warn(event_time):
            return
        quick_scroll_state = self._remember_quick_orders_scroll_state()
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)
        self._restore_quick_orders_scroll_state_later(quick_scroll_state)

        def operation():
            return self.operblock_service.start_infusion(
                self._current_admission_id,
                self._current_operation_case_id,
                drug_name,
                None,
                "",
                event_time,
                payload=effective_payload,
            )

        self._enqueue_write(
            f"{source_key}:{self._current_admission_id}",
            operation,
            on_success=lambda _result: success_callback(),
            on_error=lambda exc: self._on_infusion_mutation_error(exc),
        )

    def _start_manual_gas(self, drug_text: str, dose_text: str):
        self._start_gas_infusion(
            drug_text,
            dose_text,
            payload={"manual_order": True},
            source_key="operblock_manual_gas",
            on_saved=self._on_manual_infusion_saved,
        )

    def _on_protocol_write_error(self, exc: Exception, button: QPushButton, label: str):
        self._write_pending = False
        self._set_protocol_write_controls_enabled(True)
        button.setText(label)
        title = "Конфликт данных" if isinstance(exc, (DataConflictError, OperBlockConflictError)) else "Ошибка сохранения"
        CustomMessageBox.warning(self, title, str(exc))
        self.refresh_protocol(force=True)

    def _validate_order_datetime_or_warn(self, value: str) -> bool:
        order_dt = _minute_floor_dt(_parse_datetime_value(value))
        if order_dt is None:
            CustomMessageBox.warning(self, "Время назначения", "Укажите корректное время введения препарата.")
            return False
        start_dt = _minute_floor_dt(self._current_anesthesia_start)
        if start_dt and order_dt < start_dt:
            CustomMessageBox.warning(
                self,
                "Время назначения",
                f"Назначение не может быть раньше начала пособия: {start_dt.strftime('%d.%m.%Y %H:%M')}.",
            )
            return False
        end_dt = _minute_floor_dt(self._current_anesthesia_end) if not self._current_anesthesia_active else None
        if end_dt and order_dt > end_dt:
            CustomMessageBox.warning(
                self,
                "Время назначения",
                f"Назначение не может быть позже окончания пособия: {end_dt.strftime('%d.%m.%Y %H:%M')}.",
            )
            return False
        return True

    def _validate_infusion_event_datetime_or_warn(self, value: str | None) -> bool:
        event_dt = _minute_floor_dt(_parse_datetime_value(value))
        if event_dt is None:
            CustomMessageBox.warning(self, "Время назначения", "Укажите корректное время начала назначения.")
            return False
        start_dt = _minute_floor_dt(self._current_anesthesia_start)
        if start_dt and event_dt < start_dt:
            CustomMessageBox.warning(
                self,
                "Время назначения",
                f"Старт назначения не может быть раньше начала пособия: {start_dt.strftime('%d.%m.%Y %H:%M')}.",
            )
            return False
        end_dt = _minute_floor_dt(self._current_anesthesia_end) if not self._current_anesthesia_active else None
        if end_dt and event_dt > end_dt:
            CustomMessageBox.warning(
                self,
                "Время назначения",
                f"Старт назначения не может быть позже окончания пособия: {end_dt.strftime('%d.%m.%Y %H:%M')}.",
            )
            return False
        return True

    def _validate_infusion_stop_datetime_or_warn(self, value: str | None, interval: dict) -> bool:
        event_dt = _minute_floor_dt(_parse_datetime_value(value))
        if event_dt is None:
            CustomMessageBox.warning(self, "Время остановки", "Укажите корректное время окончания назначения.")
            return False
        start_dt = _minute_floor_dt(_parse_datetime_value((interval or {}).get("start_time")))
        if start_dt and event_dt < start_dt:
            CustomMessageBox.warning(
                self,
                "Время остановки",
                f"Остановка назначения не может быть раньше старта: {start_dt.strftime('%d.%m.%Y %H:%M')}.",
            )
            return False
        latest_dt = self._latest_infusion_event_datetime(interval or {})
        if latest_dt and event_dt < latest_dt:
            CustomMessageBox.warning(
                self,
                "Время остановки",
                f"Остановка назначения не может быть раньше последнего изменения: {latest_dt.strftime('%d.%m.%Y %H:%M')}.",
            )
            return False
        anesthesia_start_dt = _minute_floor_dt(self._current_anesthesia_start)
        if anesthesia_start_dt and event_dt < anesthesia_start_dt:
            CustomMessageBox.warning(
                self,
                "Время остановки",
                f"Остановка назначения не может быть раньше начала пособия: {anesthesia_start_dt.strftime('%d.%m.%Y %H:%M')}.",
            )
            return False
        anesthesia_end_dt = _minute_floor_dt(self._current_anesthesia_end) if not self._current_anesthesia_active else None
        if anesthesia_end_dt and event_dt > anesthesia_end_dt:
            CustomMessageBox.warning(
                self,
                "Время остановки",
                f"Остановка назначения не может быть позже окончания пособия: {anesthesia_end_dt.strftime('%d.%m.%Y %H:%M')}.",
            )
            return False
        return True

    @staticmethod
    def _route_only_write_description(admission_id: int, order_id: int) -> str:
        return f"operblock_update_order_route:{int(admission_id)}:{int(order_id)}"

    def _remember_route_only_write(self, admission_id: int, order_id: int) -> None:
        self._route_only_write_suppressions[(int(admission_id), int(order_id))] = time.monotonic()

    def _current_order_row_by_id(self, order_id: int) -> dict | None:
        target_id = int(order_id)
        for row in list(getattr(self, "_current_orders_rows", []) or []):
            if _safe_int((row or {}).get("id")) == target_id:
                return dict(row or {})
        return None

    def _fresh_order_row(self, row: dict) -> dict:
        order_id = _safe_int((row or {}).get("id"))
        if not order_id:
            return dict(row or {})
        return self._current_order_row_by_id(order_id) or dict(row or {})

    def _patch_timeline_snapshot_order_route(self, order_id: int, route_code: str) -> bool:
        snapshot = dict(getattr(self, "_current_timeline_snapshot", None) or {})
        events = list(snapshot.get("bolus_events") or [])
        if not events:
            return False
        route_value = "im" if _normalize_order_route_code(route_code) == OPERBLOCK_ORDER_ROUTE_INTRAMUSCULAR else None
        changed = False
        patched_events: list[dict] = []
        for event in events:
            data = dict(event or {})
            event_order_id = _safe_int(data.get("source_id"))
            if str(data.get("source") or "") == "legacy_order" and event_order_id == int(order_id):
                payload = dict(data.get("payload") or {})
                data["route"] = route_value
                if route_value:
                    payload["route"] = route_value
                else:
                    payload.pop("route", None)
                data["payload"] = payload
                data["revision"] = int(data.get("revision") or 0) + 1
                changed = True
            patched_events.append(data)
        if not changed:
            return False
        snapshot["bolus_events"] = patched_events
        hash_payload = dict(snapshot)
        hash_payload.pop("generated_at", None)
        snapshot["content_hash"] = _stable_ui_hash(hash_payload)
        self._current_timeline_snapshot = snapshot
        return True

    @staticmethod
    def _refresh_timeline_snapshot_hash(snapshot: dict) -> dict:
        updated = dict(snapshot or {})
        hash_payload = dict(updated)
        hash_payload.pop("generated_at", None)
        hash_payload.pop("content_hash", None)
        updated["content_hash"] = _stable_ui_hash(hash_payload)
        return updated

    def _patch_timeline_snapshot_order(
        self,
        order_id: int,
        *,
        text: str | None = None,
        order_datetime: str | None = None,
        route_code: str | None = None,
    ) -> bool:
        snapshot = dict(getattr(self, "_current_timeline_snapshot", None) or {})
        events = list(snapshot.get("bolus_events") or [])
        if not events:
            return False
        changed = False
        patched_events: list[dict] = []
        drug_name, dose_text = _split_order_drug_and_dose(text or "") if text is not None else ("", "")
        event_time = self._local_iso_minute_text(order_datetime) if order_datetime is not None else None
        route_value = None
        if route_code is not None and _normalize_order_route_code(route_code) == OPERBLOCK_ORDER_ROUTE_INTRAMUSCULAR:
            route_value = "im"
        for event in events:
            data = dict(event or {})
            event_order_id = _safe_int(data.get("source_id"))
            if str(data.get("source") or "") == "legacy_order" and event_order_id == int(order_id):
                payload = dict(data.get("payload") or {})
                if text is not None:
                    data["drug_label"] = drug_name
                    data["dose_text"] = dose_text
                    payload["text"] = text
                    payload["drug_label"] = drug_name
                    payload["dose_text"] = dose_text
                if event_time is not None:
                    data["event_time"] = event_time
                if route_code is not None:
                    data["route"] = route_value
                    if route_value:
                        payload["route"] = route_value
                    else:
                        payload.pop("route", None)
                data["payload"] = payload
                data["revision"] = int(data.get("revision") or 0) + 1
                changed = True
            patched_events.append(data)
        if not changed:
            return False
        snapshot["bolus_events"] = patched_events
        self._current_timeline_snapshot = self._refresh_timeline_snapshot_hash(snapshot)
        return True

    def _apply_order_route_change_locally(self, order_id: int, route_code: str) -> bool:
        normalized_route = _normalize_order_route_code(route_code)
        rows = []
        found = False
        for row in list(getattr(self, "_current_orders_rows", []) or []):
            updated = dict(row or {})
            if _safe_int(updated.get("id")) == int(order_id):
                updated["route"] = normalized_route
                updated["comment"] = _order_comment_with_route(str(updated.get("comment") or ""), normalized_route)
                updated["revision"] = int(updated.get("revision") or 0) + 1
                updated["updated_at"] = datetime.now().isoformat(timespec="seconds")
                found = True
            rows.append(updated)
        if not found:
            return False
        self._patch_timeline_snapshot_order_route(order_id, normalized_route)
        self._current_orders_rows = rows
        self._patch_rendered_order_detail_text(order_id)
        self._sync_orders_render_signatures_from_current_rows()
        self._update_vitals_chart_order_markers()
        return True

    def _apply_order_edit_locally(
        self,
        order_id: int,
        text: str,
        *,
        order_datetime: str | None = None,
        route_code: str | None = None,
    ) -> bool:
        clean_text = re.sub(r"\s+", " ", str(text or "").strip())
        if not clean_text:
            return False
        rows = []
        found = False
        normalized_route = _normalize_order_route_code(route_code) if route_code is not None else None
        for row in list(getattr(self, "_current_orders_rows", []) or []):
            updated = dict(row or {})
            if _safe_int(updated.get("id")) == int(order_id):
                updated["text"] = clean_text
                if order_datetime is not None:
                    updated["datetime"] = self._local_iso_minute_text(order_datetime)
                if normalized_route is not None:
                    updated["route"] = normalized_route
                    updated["comment"] = _order_comment_with_route(str(updated.get("comment") or ""), normalized_route)
                updated["revision"] = int(updated.get("revision") or 0) + 1
                updated["updated_at"] = datetime.now().isoformat(timespec="seconds")
                raw_drug_name, dose_text = _split_order_drug_and_dose(clean_text)
                updated["drug_name"] = self._order_display_drug_name(updated, raw_drug_name)
                updated["raw_drug_name"] = raw_drug_name
                updated["dose_text"] = dose_text
                found = True
            rows.append(updated)
        if not found:
            return False
        self._patch_timeline_snapshot_order(
            order_id,
            text=clean_text,
            order_datetime=order_datetime,
            route_code=normalized_route,
        )
        self._current_orders_rows = rows
        self._apply_orders({"orders": rows})
        self._update_vitals_chart_order_markers()
        return True

    def _on_order_route_saved(self, order_id: int, route_code: str):
        self._write_pending = False
        if not self._apply_order_route_change_locally(order_id, route_code):
            self._set_protocol_write_controls_enabled(True)
            self.refresh_protocol(force=True)

    def _on_order_edit_saved(
        self,
        order_id: int,
        text: str,
        *,
        order_datetime: str | None = None,
        route_code: str | None = None,
    ):
        self._write_pending = False
        self._set_protocol_write_controls_enabled(True)
        if not self._apply_order_edit_locally(order_id, text, order_datetime=order_datetime, route_code=route_code):
            self.refresh_protocol(force=True)

    @staticmethod
    def _timeline_event_key(event_id: int | None) -> str:
        return f"timeline_event:{int(event_id)}" if event_id else ""

    def _patch_current_infusion_interval(self, start_event_id: int, updater) -> bool:
        snapshot = dict(getattr(self, "_current_timeline_snapshot", None) or {})
        intervals = list(snapshot.get("infusion_intervals") or [])
        if not intervals:
            return False
        patched_intervals: list[dict] = []
        changed = False
        for interval in intervals:
            data = dict(interval or {})
            interval_start_id, _revision = self._infusion_identity(data)
            if interval_start_id == int(start_event_id):
                data = updater(data) or data
                changed = True
            patched_intervals.append(data)
        if not changed:
            return False
        snapshot["infusion_intervals"] = patched_intervals
        self._current_timeline_snapshot = self._refresh_timeline_snapshot_hash(snapshot)
        return True

    @staticmethod
    def _bump_infusion_interval_start_revision(interval: dict) -> dict:
        payload = dict((interval or {}).get("payload") or {})
        payload["start_revision"] = int(payload.get("start_revision") or 0) + 1
        interval["payload"] = payload
        return interval

    def _set_infusion_interval_start_time(self, interval: dict, start_event_id: int, event_time: str | None) -> dict:
        if event_time is None:
            return interval
        event_time_text = self._local_iso_minute_text(event_time)
        interval["start_time"] = event_time_text
        start_key = self._timeline_event_key(start_event_id)
        for history_key in ("rate_history", "dose_history"):
            history = []
            for index, item in enumerate(list(interval.get(history_key) or [])):
                updated_item = dict(item or {})
                if index == 0 or (start_key and str(updated_item.get("event_id") or "") == start_key):
                    updated_item["event_time"] = event_time_text
                    updated_item["revision"] = int(updated_item.get("revision") or 0) + 1
                history.append(updated_item)
            if history:
                interval[history_key] = history
        return interval

    def _apply_infusion_interval_locally(self, start_event_id: int, updater) -> bool:
        if not self._patch_current_infusion_interval(start_event_id, updater):
            return False
        self._apply_active_infusions()
        self._apply_orders({"orders": getattr(self, "_current_orders_rows", [])})
        self._update_vitals_chart_order_markers()
        return True

    def _complete_local_infusion_mutation(self, applied: bool) -> None:
        quick_scroll_state = getattr(self, "_pending_quick_orders_scroll_state", None)
        self._pending_quick_orders_scroll_state = None
        self._write_pending = False
        self._set_protocol_write_controls_enabled(True)
        if not applied:
            self.refresh_protocol(force=True)
        self._restore_quick_orders_scroll_state_later(quick_scroll_state)

    def _on_infusion_start_time_saved_locally(self, start_event_id: int, event_time: str | None) -> None:
        def updater(interval: dict) -> dict:
            interval = self._set_infusion_interval_start_time(interval, start_event_id, event_time)
            return self._bump_infusion_interval_start_revision(interval)

        self._complete_local_infusion_mutation(self._apply_infusion_interval_locally(start_event_id, updater))

    def _on_infusion_rate_saved_locally(
        self,
        result,
        start_event_id: int,
        *,
        rate_value: str,
        rate_unit: str,
        change_event_time: str,
        start_event_time: str | None,
    ) -> None:
        change_event_id = _safe_int(result)
        change_event_key = self._timeline_event_key(change_event_id) or f"local_rate:{int(start_event_id)}:{change_event_time}"

        def updater(interval: dict) -> dict:
            interval = self._set_infusion_interval_start_time(interval, start_event_id, start_event_time)
            interval["current_rate_value"] = str(rate_value or "").strip()
            interval["current_rate_unit"] = str(rate_unit or "").strip()
            history = [dict(item or {}) for item in list(interval.get("rate_history") or [])]
            history = [item for item in history if str(item.get("event_id") or "") != change_event_key]
            history.append(
                {
                    "event_id": change_event_key,
                    "event_time": self._local_iso_minute_text(change_event_time),
                    "rate_value": str(rate_value or "").strip(),
                    "rate_unit": str(rate_unit or "").strip(),
                    "revision": 1,
                }
            )
            history.sort(key=lambda item: (_parse_datetime_value(item.get("event_time")) or datetime.min, str(item.get("event_id") or "")))
            interval["rate_history"] = history
            event_ids = [str(item) for item in list(interval.get("event_ids") or []) if item]
            if change_event_key not in event_ids:
                event_ids.append(change_event_key)
            interval["event_ids"] = event_ids
            return self._bump_infusion_interval_start_revision(interval)

        self._complete_local_infusion_mutation(self._apply_infusion_interval_locally(start_event_id, updater))

    def _on_gas_dose_saved_locally(
        self,
        result,
        start_event_id: int,
        *,
        dose_text: str,
        change_event_time: str,
        start_event_time: str | None,
    ) -> None:
        change_event_id = _safe_int(result)
        change_event_key = self._timeline_event_key(change_event_id) or f"local_gas:{int(start_event_id)}:{change_event_time}"
        clean_dose = _normalize_gas_dose_text(dose_text)

        def updater(interval: dict) -> dict:
            interval = self._set_infusion_interval_start_time(interval, start_event_id, start_event_time)
            interval["current_rate_value"] = None
            interval["current_rate_unit"] = None
            interval["rate_history"] = []
            payload = dict(interval.get("payload") or {})
            payload["kind"] = "gas"
            payload["dose_text"] = clean_dose
            payload["display_dose_text"] = clean_dose
            interval["payload"] = payload
            history = [dict(item or {}) for item in list(interval.get("dose_history") or [])]
            history = [item for item in history if str(item.get("event_id") or "") != change_event_key]
            history.append(
                {
                    "event_id": change_event_key,
                    "event_time": self._local_iso_minute_text(change_event_time),
                    "dose_text": clean_dose,
                    "revision": 1,
                }
            )
            history.sort(key=lambda item: (_parse_datetime_value(item.get("event_time")) or datetime.min, str(item.get("event_id") or "")))
            interval["dose_history"] = history
            event_ids = [str(item) for item in list(interval.get("event_ids") or []) if item]
            if change_event_key not in event_ids:
                event_ids.append(change_event_key)
            interval["event_ids"] = event_ids
            return self._bump_infusion_interval_start_revision(interval)

        self._complete_local_infusion_mutation(self._apply_infusion_interval_locally(start_event_id, updater))

    def _on_infusion_volume_saved_locally(
        self,
        start_event_id: int,
        *,
        volume_ml: str,
        event_time: str | None,
    ) -> None:
        clean_volume = _normalize_volume_ml_text(volume_ml)

        def updater(interval: dict) -> dict:
            interval = self._set_infusion_interval_start_time(interval, start_event_id, event_time)
            interval["volume_ml"] = clean_volume
            drug_label = _infusion_display_drug_name(interval, "Капельница")
            interval["display_label"] = f"{drug_label} {clean_volume} мл".strip()
            payload = dict(interval.get("payload") or {})
            payload["volume_ml"] = clean_volume
            payload["declared_total_volume_ml"] = clean_volume
            interval["payload"] = payload
            return self._bump_infusion_interval_start_revision(interval)

        self._complete_local_infusion_mutation(self._apply_infusion_interval_locally(start_event_id, updater))

    @staticmethod
    def _bolus_order_dialog_parts(row: dict) -> tuple[str, str]:
        clean_text = re.sub(r"\s+", " ", str((row or {}).get("text") or "").strip())
        drug_name = re.sub(
            r"\s+",
            " ",
            str((row or {}).get("drug_name") or (row or {}).get("drug_label") or "").strip(),
        )
        dose_text = _normalize_bolus_dose_text(str((row or {}).get("dose_text") or "").strip())
        if drug_name and dose_text:
            return drug_name, dose_text
        if drug_name and clean_text.casefold().startswith(drug_name.casefold()):
            tail = clean_text[len(drug_name) :].strip(" -:;·")
            if tail and not dose_text:
                dose_text = _normalize_bolus_dose_text(tail)
            return drug_name, dose_text
        parsed_drug, parsed_dose = _split_order_drug_and_dose(clean_text)
        if parsed_dose:
            if not dose_text:
                dose_text = _normalize_bolus_dose_text(parsed_dose)
            if not drug_name:
                drug_name = parsed_drug
        elif not dose_text:
            trailing_number = re.search(r"(?P<dose>\d+(?:[.,]\d+)?)$", clean_text)
            if trailing_number:
                prefix = clean_text[: trailing_number.start()].strip()
                if prefix:
                    dose_text = _normalize_bolus_dose_text(trailing_number.group("dose"))
                    if not drug_name:
                        drug_name = prefix
        if not drug_name:
            drug_name = clean_text
        return drug_name or "Препарат", dose_text


    def _edit_order_with_time(self, row: dict):
        if not self._current_admission_id or self._write_pending:
            return
        order_id = _safe_int(row.get("id"))
        if not order_id:
            return
        row = self._fresh_order_row(row)
        old_datetime = _minute_floor_dt(_parse_datetime_value(row.get("datetime")))
        if old_datetime is None:
            CustomMessageBox.warning(self, "Время назначения", "Не удалось определить время назначения. Обновите протокол.")
            self.refresh_protocol(force=True)
            return
        old_text = str(row.get("text") or "").strip()
        if not old_text:
            CustomMessageBox.warning(self, "Ошибка", "Текст назначения не заполнен.")
            return
        old_route = _order_route_code(row)
        drug_name, dose_text = self._bolus_order_dialog_parts(row)
        dialog = BolusEditDialog(
            drug_name,
            dose_text,
            self,
            base_datetime=old_datetime,
            min_datetime=self._current_anesthesia_start or self._current_operation_start,
            max_datetime=self._current_anesthesia_end,
            route_code=old_route,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        text = dialog.text()
        if not text:
            CustomMessageBox.warning(self, "Ошибка", "Текст назначения не заполнен.")
            return
        order_datetime = dialog.datetime_text()
        if not self._validate_order_datetime_or_warn(order_datetime):
            self.refresh_protocol(force=True)
            return
        new_datetime = _minute_floor_dt(_parse_datetime_value(order_datetime))
        route_code = dialog.route_code()
        if text == old_text and new_datetime == old_datetime and route_code == old_route:
            return
        route_only = text == old_text and new_datetime == old_datetime and route_code != old_route
        expected_revision = None if route_only else int(row.get("revision") or 0)
        self._write_pending = True
        if not route_only:
            self._set_protocol_write_controls_enabled(False)
        if route_only:
            self._remember_route_only_write(self._current_admission_id, order_id)

        def operation():
            return self.operblock_service.update_order_text(
                self._current_admission_id,
                order_id,
                text,
                None if route_only else order_datetime,
                expected_revision=expected_revision,
                route=route_code,
            )

        write_description = (
            self._route_only_write_description(self._current_admission_id, order_id)
            if route_only
            else f"operblock_update_order:{self._current_admission_id}:{order_id}"
        )
        if not route_only:
            self._remember_local_write_refresh_suppression(write_description, {"orders"})
        on_success = (
            (lambda _result, oid=order_id, route=route_code: self._on_order_route_saved(oid, route))
            if route_only
            else (
                lambda _result, oid=order_id, new_text=text, dt=order_datetime, route=route_code: self._on_order_edit_saved(
                    oid,
                    new_text,
                    order_datetime=dt,
                    route_code=route,
                )
            )
        )
        self._enqueue_write(
            write_description,
            operation,
            on_success=on_success,
            on_error=lambda exc: self._on_order_mutation_error(exc),
        )

    def _edit_order(self, row: dict):
        if not self._current_admission_id or self._write_pending:
            return
        order_id = _safe_int(row.get("id"))
        if not order_id:
            return
        row = self._fresh_order_row(row)
        old_route = _order_route_code(row)
        old_text = str(row.get("text") or "").strip()
        drug_name, dose_text = self._bolus_order_dialog_parts(row)
        dialog = BolusEditDialog(
            drug_name,
            dose_text,
            self,
            route_code=old_route,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        text = dialog.text()
        if not text:
            CustomMessageBox.warning(self, "Ошибка", "Текст назначения не заполнен.")
            return
        route_code = dialog.route_code()
        if text == old_text and route_code == old_route:
            return
        route_only = text == old_text and route_code != old_route
        expected_revision = None if route_only else int(row.get("revision") or 0)
        self._write_pending = True
        if not route_only:
            self._set_protocol_write_controls_enabled(False)
        if route_only:
            self._remember_route_only_write(self._current_admission_id, order_id)

        def operation():
            return self.operblock_service.update_order_text(
                self._current_admission_id,
                order_id,
                text,
                None,
                expected_revision=expected_revision,
                route=route_code,
            )

        write_description = (
            self._route_only_write_description(self._current_admission_id, order_id)
            if route_only
            else f"operblock_update_order:{self._current_admission_id}:{order_id}"
        )
        if not route_only:
            self._remember_local_write_refresh_suppression(write_description, {"orders"})
        on_success = (
            (lambda _result, oid=order_id, route=route_code: self._on_order_route_saved(oid, route))
            if route_only
            else (
                lambda _result, oid=order_id, new_text=text, route=route_code: self._on_order_edit_saved(
                    oid,
                    new_text,
                    route_code=route,
                )
            )
        )
        self._enqueue_write(
            write_description,
            operation,
            on_success=on_success,
            on_error=lambda exc: self._on_order_mutation_error(exc),
        )

    def _edit_order_time(self, row: dict):
        if not self._current_admission_id or self._write_pending:
            return
        order_id = _safe_int(row.get("id"))
        if not order_id:
            return
        old_datetime = _minute_floor_dt(_parse_datetime_value(row.get("datetime")))
        if old_datetime is None:
            CustomMessageBox.warning(self, "Время назначения", "Не удалось определить время назначения. Обновите протокол.")
            self.refresh_protocol(force=True)
            return
        text = str(row.get("text") or "").strip()
        if not text:
            CustomMessageBox.warning(self, "Ошибка", "Текст назначения не заполнен.")
            return
        dialog = TimeEditDialog("Время назначения", old_datetime, self, field_label="Время введения")
        if dialog.exec() != QDialog.Accepted:
            return
        order_datetime = dialog.datetime_text()
        if not self._validate_order_datetime_or_warn(order_datetime):
            self.refresh_protocol(force=True)
            return
        new_datetime = _minute_floor_dt(_parse_datetime_value(order_datetime))
        if new_datetime == old_datetime:
            return
        expected_revision = int(row.get("revision") or 0)
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)

        def operation():
            return self.operblock_service.update_order_text(
                self._current_admission_id,
                order_id,
                text,
                order_datetime,
                expected_revision=expected_revision,
            )

        write_description = f"operblock_update_order_time:{self._current_admission_id}:{order_id}"
        self._remember_local_write_refresh_suppression(write_description, {"orders"})
        self._enqueue_write(
            write_description,
            operation,
            on_success=lambda _result, oid=order_id, new_text=text, dt=order_datetime: self._on_order_edit_saved(
                oid,
                new_text,
                order_datetime=dt,
            ),
            on_error=lambda exc: self._on_order_mutation_error(exc),
        )

    def _delete_order(self, row: dict):
        if not self._current_admission_id or self._write_pending:
            return
        order_id = _safe_int(row.get("id"))
        if not order_id:
            return
        text = (
            _build_order_text_for_display(str(row.get("drug_name") or ""), str(row.get("dose_text") or ""))
            if row.get("drug_name")
            else str(row.get("text") or "").strip()
        )
        reply = CustomMessageBox.question(
            self,
            "Удаление назначения",
            f"Удалить назначение?\n{text}",
            CustomMessageBox.Yes | CustomMessageBox.No,
            CustomMessageBox.No,
        )
        if reply != CustomMessageBox.Yes:
            return
        expected_revision = int(row.get("revision") or 0)
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)

        def operation():
            return self.operblock_service.delete_order(
                self._current_admission_id,
                order_id,
                expected_revision=expected_revision,
            )

        self._enqueue_write(
            f"operblock_delete_order:{self._current_admission_id}:{order_id}",
            operation,
            on_success=lambda _result: self._on_order_mutation_saved(),
            on_error=lambda exc: self._on_order_mutation_error(exc),
        )

    def _on_order_mutation_saved(self):
        self._write_pending = False
        self._set_protocol_write_controls_enabled(True)
        self._orders_force_top_on_next_apply = True
        self.refresh_protocol(force=True)

    def _on_order_mutation_error(self, exc: Exception):
        self._write_pending = False
        self._set_protocol_write_controls_enabled(True)
        title = "Конфликт данных" if isinstance(exc, (DataConflictError, OperBlockConflictError)) else "Ошибка назначения"
        CustomMessageBox.warning(self, title, str(exc))
        self.refresh_protocol(force=True)

    def _add_quick_order(self, drug_name: str, dose: str, *, kind: str = "bolus"):
        if not self._current_admission_id or self._write_pending:
            return
        if not self._orders_tab_enabled():
            CustomMessageBox.warning(self, "Пособие не начато", "Назначения доступны только во время анестезиологического пособия.")
            self.refresh_protocol(force=True)
            return
        normalized_kind = "gas" if normalize_operblock_quick_order_kind(kind) == "gas" else "bolus"
        if normalized_kind == "gas":
            self._start_gas_infusion(
                drug_name,
                dose,
                payload={"manual_order": False, "quick_order": True},
                source_key="operblock_quick_gas_order",
            )
            return
        text = build_operblock_quick_order_text(drug_name, dose)
        if not text:
            return
        quick_scroll_state = self._remember_quick_orders_scroll_state()
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)
        self._restore_quick_orders_scroll_state_later(quick_scroll_state)

        def operation():
            return self.operblock_service.add_order(self._current_admission_id, text)

        self._enqueue_write(
            f"operblock_quick_{normalized_kind}_order:{self._current_admission_id}",
            operation,
            on_success=lambda _result: self._on_quick_order_saved(),
            on_error=lambda exc: self._on_quick_order_error(exc),
        )

    def _add_preset_bolus(self, preset: dict, dose: str):
        if not self._current_admission_id or self._write_pending:
            return
        if not self._orders_tab_enabled():
            CustomMessageBox.warning(self, "Пособие не начато", "Назначения доступны только во время анестезиологического пособия.")
            self.refresh_protocol(force=True)
            return
        text = build_operblock_preset_bolus_text(preset, dose)
        if not text:
            return
        quick_scroll_state = self._remember_quick_orders_scroll_state()
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)
        self._restore_quick_orders_scroll_state_later(quick_scroll_state)
        preset_payload = build_operblock_preset_payload(preset)

        def operation():
            return self.operblock_service.add_order(self._current_admission_id, text, preset_payload=preset_payload)

        self._enqueue_write(
            f"operblock_preset_bolus:{self._current_admission_id}",
            operation,
            on_success=lambda _result: self._on_quick_order_saved(),
            on_error=lambda exc: self._on_quick_order_error(exc),
        )

    def _add_preset_gas(self, preset: dict, dose: str):
        if not self._current_admission_id or self._write_pending:
            return
        if not self._orders_tab_enabled():
            CustomMessageBox.warning(self, "Пособие не начато", "Назначения доступны только во время анестезиологического пособия.")
            self.refresh_protocol(force=True)
            return
        drug_name = operblock_medication_preset_display_name(preset)
        preset_payload = build_operblock_preset_payload(preset)
        self._start_gas_infusion(
            drug_name,
            dose,
            payload=preset_payload,
            source_key="operblock_preset_gas",
        )

    def _start_timed_infusion_preset(self, preset: dict, dose_text: str = ""):
        if not self._current_admission_id or self._write_pending:
            return
        drug_name = operblock_medication_preset_display_name(preset)
        if not drug_name:
            CustomMessageBox.warning(self, "Капельница", "Укажите препарат для капельницы.")
            return
        if not self._ensure_infusion_write_context_or_warn():
            return
        selected_dose = re.sub(r"\s+", " ", str(dose_text or "").strip())
        if not selected_dose:
            dose_options = _timed_infusion_dose_options(preset)
            selected_dose = dose_options[0] if dose_options else ""
        concentration_text = str(preset.get("concentration") or "").strip()
        dose_volume = _quick_order_dose_volume_ml(selected_dose, concentration_text)
        solvent_volume = _source_solvent_volume_ml(preset)
        total_volume = _timed_infusion_total_volume_ml(preset, selected_dose, concentration_text)
        volume = _volume_text_without_unit(total_volume)
        if not volume:
            if _quick_order_mass_dose_component(selected_dose):
                CustomMessageBox.warning(
                    self,
                    "Капельница",
                    "Для дозировки в мг/г укажите концентрацию препарата или объем капельницы в настройках быстрого назначения.",
                )
            else:
                CustomMessageBox.warning(self, "Капельница", "Укажите объем капельницы в настройках быстрого назначения.")
            return
        event_time = self._current_operation_event_time_text()
        if not self._validate_infusion_event_datetime_or_warn(event_time):
            return
        quick_scroll_state = self._remember_quick_orders_scroll_state()
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)
        self._restore_quick_orders_scroll_state_later(quick_scroll_state)
        preset_payload = build_operblock_preset_payload(preset)
        preset_payload["volume_ml"] = volume
        preset_payload["declared_total_volume_ml"] = volume
        if selected_dose:
            preset_payload["dose_text"] = selected_dose
            preset_payload["display_dose_text"] = _quick_order_dose_display_text(selected_dose, concentration_text)
            if dose_volume is not None:
                preset_payload["calculated_volume_ml"] = _volume_text_without_unit(dose_volume)
        if solvent_volume is not None:
            preset_payload["solvent_volume_ml"] = _volume_text_without_unit(solvent_volume)

        def operation():
            return self.operblock_service.start_infusion(
                self._current_admission_id,
                self._current_operation_case_id,
                drug_name,
                None,
                "",
                event_time,
                concentration_text=concentration_text,
                volume_ml=volume,
                payload=preset_payload,
            )

        self._enqueue_write(
            f"operblock_timed_infusion:{self._current_admission_id}",
            operation,
            on_success=lambda _result: self._on_infusion_mutation_saved(),
            on_error=lambda exc: self._on_infusion_mutation_error(exc),
        )

    def _add_timed_infusion_preset(self, preset: dict):
        self._start_timed_infusion_preset(preset, "")

    def _on_quick_order_saved(self):
        quick_scroll_state = getattr(self, "_pending_quick_orders_scroll_state", None)
        self._pending_quick_orders_scroll_state = None
        self._write_pending = False
        self._set_protocol_write_controls_enabled(True)
        self.refresh_protocol(force=True)
        self._restore_quick_orders_scroll_state_later(quick_scroll_state)

    def _on_quick_order_error(self, exc: Exception):
        quick_scroll_state = getattr(self, "_pending_quick_orders_scroll_state", None)
        self._pending_quick_orders_scroll_state = None
        self._write_pending = False
        self._set_protocol_write_controls_enabled(True)
        title = "Конфликт данных" if isinstance(exc, (DataConflictError, OperBlockConflictError)) else "Ошибка сохранения"
        CustomMessageBox.warning(self, title, str(exc))
        self.refresh_protocol(force=True)
        self._restore_quick_orders_scroll_state_later(quick_scroll_state)

    def _run_stage_action(self, action_name: str, operation, success_message: str = ""):
        if self._write_pending:
            return
        self._write_pending = True
        self._apply_protocol_controls_state()
        self._enqueue_write(
            action_name,
            operation,
            on_success=lambda _result: self._on_stage_action_success(success_message),
            on_error=lambda exc: self._on_stage_action_error(exc),
        )

    def _on_stage_action_success(self, message: str = ""):
        self._write_pending = False
        if message:
            CustomMessageBox.information(self, "Оперблок", message)
        self.refresh_protocol(force=True)
        self.refresh_board(force=True)

    def _on_stage_action_error(self, exc: Exception):
        self._write_pending = False
        title = "Конфликт данных" if isinstance(exc, (DataConflictError, OperBlockConflictError)) else "Оперблок"
        CustomMessageBox.warning(self, title, str(exc))
        if self._current_operation_case_id:
            self.refresh_protocol(force=True)
        self.refresh_board(force=True)

    def _start_anesthesia(self):
        if not self._current_operation_case_id:
            return
        case_id = int(self._current_operation_case_id)
        try:
            anesthesia_types = load_operblock_anesthesia_types()
        except Exception as exc:
            CustomMessageBox.warning(self, "Начать пособие", f"Не удалось загрузить виды пособия: {exc}")
            return
        try:
            anesthesiologists = load_operblock_anesthesiologists()
            anesthetists = load_operblock_anesthetists()
        except Exception as exc:
            CustomMessageBox.warning(self, "Начать пособие", f"Не удалось загрузить сотрудников для пособия: {exc}")
            return
        dialog = StartAnesthesiaDialog(anesthesia_types, anesthesiologists, anesthetists, self)
        if dialog.exec() != QDialog.Accepted:
            return
        assistance_type = dialog.selected_assistance_type()
        anesthesiologist = dialog.selected_anesthesiologist()
        anesthetist = dialog.selected_anesthetist()
        self._run_stage_action(
            f"operblock_start_anesthesia:{case_id}",
            lambda: self.operblock_service.start_anesthesia(
                case_id,
                assistance_type,
                anesthesiologist=anesthesiologist,
                anesthetist=anesthetist,
            ),
            "Анестезиологическое пособие начато.",
        )

    def _end_anesthesia(self):
        if not self._current_operation_case_id:
            return
        case_id = int(self._current_operation_case_id)
        self._run_stage_action(
            f"operblock_end_anesthesia:{case_id}",
            lambda: self.operblock_service.end_anesthesia(case_id),
            "Анестезиологическое пособие завершено.",
        )

    def _start_surgery(self):
        if not self._current_operation_case_id:
            return
        case_id = int(self._current_operation_case_id)
        try:
            surgeons = load_operblock_surgeons()
            operating_nurses = load_operblock_operating_nurses()
        except Exception as exc:
            CustomMessageBox.warning(self, "Начать операцию", f"Не удалось загрузить сотрудников для операции: {exc}")
            return
        dialog = StartSurgeryDialog(surgeons, operating_nurses, self)
        if dialog.exec() != QDialog.Accepted:
            return
        operation_name = dialog.operation_name()
        surgeons = dialog.selected_surgeons()
        operating_nurse = dialog.selected_operating_nurse()
        self._run_stage_action(
            f"operblock_start_surgery:{case_id}",
            lambda: self.operblock_service.start_surgery(
                case_id,
                operation_name=operation_name,
                surgeons=surgeons,
                operating_nurse=operating_nurse,
            ),
        )

    def _end_surgery(self):
        if not self._current_operation_case_id:
            return
        case_id = int(self._current_operation_case_id)
        self._run_stage_action(
            f"operblock_end_surgery:{case_id}",
            lambda: self.operblock_service.end_surgery(case_id),
        )

    def _undo_last_action(self):
        if not self._current_operation_case_id or self._write_pending:
            return
        case_id = int(self._current_operation_case_id)

        def operation():
            return self.operblock_service.undo_last_action(case_id)

        self._write_pending = True
        self._apply_protocol_controls_state()
        self._enqueue_write(
            f"operblock_undo_last:{case_id}",
            operation,
            on_success=lambda result: self._on_undo_last_success(result),
            on_error=lambda exc: self._on_stage_action_error(exc),
        )

    def _on_undo_last_success(self, result):
        self._write_pending = False
        message = str((result or {}).get("message") or "Последнее действие отменено.")
        CustomMessageBox.information(self, "Отмена действия", message)
        self.refresh_protocol(force=True)
        self.refresh_board(force=True)

    def _confirm_release_current_case(self):
        if self._current_operation_case_id:
            self._confirm_release_case(self._current_operation_case_id)

    def _confirm_release_case(self, operation_case_id: int):
        if self._write_pending:
            return
        reply = CustomMessageBox.question(
            self,
            "Освободить стол",
            "Действительно освободить операционный стол?\n"
            "Пациент будет перенесён в архив операционных пациентов.",
            CustomMessageBox.Yes | CustomMessageBox.No,
            CustomMessageBox.No,
        )
        if reply != CustomMessageBox.Yes:
            return
        self._write_pending = True

        def operation():
            return self.operblock_service.release_operation_table(operation_case_id)

        self._enqueue_write(
            f"operblock_release_operation_table:{operation_case_id}",
            operation,
            on_success=lambda _result: self._on_release_case_success(operation_case_id),
            on_error=lambda exc: self._on_release_case_error(exc),
        )

    def _on_release_case_success(self, operation_case_id: int):
        self._write_pending = False
        self.refresh_board(force=True)
        if self._current_operation_case_id == int(operation_case_id):
            self._show_board()

    def _on_release_case_error(self, exc: Exception):
        self._write_pending = False
        CustomMessageBox.warning(self, "Освободить стол", str(exc))
        self.refresh_board(force=True)
        if self._current_operation_case_id:
            self.refresh_protocol(force=True)

    def _enqueue_write(self, description: str, operation, on_success, on_error):
        if not self.data_service:
            try:
                result = operation()
            except Exception as exc:
                on_error(exc)
            else:
                on_success(result)
            return
        self.data_service.enqueue_write(description, operation, on_success=on_success, on_error=on_error)

    def _cleanup_route_only_write_suppressions(self) -> None:
        now = time.monotonic()
        ttl = float(OPERBLOCK_ROUTE_ONLY_REFRESH_SUPPRESS_SECONDS)
        self._route_only_write_suppressions = {
            key: started
            for key, started in (getattr(self, "_route_only_write_suppressions", {}) or {}).items()
            if now - float(started or 0.0) <= ttl
        }

    def _remember_local_write_refresh_suppression(self, description: str, entities) -> None:
        source = str(description or "").strip()
        if not source:
            return
        self._local_write_refresh_suppressions[source] = {
            "started": time.monotonic(),
            "entities": {str(entity) for entity in (entities or []) if entity},
        }

    def _cleanup_local_write_refresh_suppressions(self) -> None:
        now = time.monotonic()
        ttl = float(OPERBLOCK_LOCAL_WRITE_REFRESH_SUPPRESS_SECONDS)
        self._local_write_refresh_suppressions = {
            source: data
            for source, data in (getattr(self, "_local_write_refresh_suppressions", {}) or {}).items()
            if now - float((data or {}).get("started") or 0.0) <= ttl
        }

    def _should_skip_local_write_refresh(self, payload: dict) -> bool:
        self._cleanup_local_write_refresh_suppressions()
        suppressions = dict(getattr(self, "_local_write_refresh_suppressions", {}) or {})
        if not suppressions:
            return False
        sources = set(self._payload_force_sources(payload or {}))
        matched_sources = [source for source in sources if source in suppressions]
        if not matched_sources:
            return False
        expected_entities: set[str] = set()
        for source in matched_sources:
            expected_entities.update(set((suppressions.get(source) or {}).get("entities") or set()))

        changes = list((payload or {}).get("changes") or [])
        changed_entities = {
            str((change or {}).get("entity_name") or "")
            for change in changes
            if (change or {}).get("entity_name")
        }
        if not changed_entities:
            changed_entities = {
                str(entity)
                for entity in ((payload or {}).get("entities") or (payload or {}).get("changed_entities") or [])
                if entity
            }
        can_skip = not changed_entities or changed_entities.issubset(expected_entities)
        if can_skip:
            for source in matched_sources:
                self._local_write_refresh_suppressions.pop(source, None)
        return can_skip

    @staticmethod
    def _payload_force_sources(payload: dict) -> list[str]:
        sources: list[str] = []
        raw_many = (payload or {}).get("force_sources") or []
        if isinstance(raw_many, (list, tuple, set)):
            sources.extend(str(item) for item in raw_many if item)
        raw_one = (payload or {}).get("force_source")
        if raw_one:
            sources.append(str(raw_one))
        return list(dict.fromkeys(sources))

    @staticmethod
    def _route_only_keys_from_sources(sources: list[str]) -> set[tuple[int, int]]:
        prefix = "operblock_update_order_route:"
        keys: set[tuple[int, int]] = set()
        for source in sources:
            text = str(source or "")
            if not text.startswith(prefix):
                continue
            parts = text[len(prefix) :].split(":")
            if len(parts) < 2:
                continue
            admission_id = _safe_int(parts[0])
            order_id = _safe_int(parts[1])
            if admission_id and order_id:
                keys.add((int(admission_id), int(order_id)))
        return keys

    def _should_skip_route_only_refresh(self, payload: dict) -> bool:
        self._cleanup_route_only_write_suppressions()
        pending = set((getattr(self, "_route_only_write_suppressions", {}) or {}).keys())
        source_keys = self._route_only_keys_from_sources(self._payload_force_sources(payload or {}))
        candidate_keys = pending | source_keys
        if not candidate_keys:
            return False

        changes = list((payload or {}).get("changes") or [])
        if changes:
            changed_keys: set[tuple[int, int]] = set()
            for change in changes:
                entity = str((change or {}).get("entity_name") or "")
                if entity != "orders":
                    return False
                admission_id = _safe_int((change or {}).get("admission_id"))
                order_id = _safe_int((change or {}).get("entity_id"))
                if not admission_id or not order_id:
                    return False
                key = (int(admission_id), int(order_id))
                if key not in candidate_keys:
                    return False
                changed_keys.add(key)
            return True

        entities = {
            str(entity)
            for entity in ((payload or {}).get("entities") or (payload or {}).get("changed_entities") or [])
            if entity
        }
        if entities and entities - {"orders"}:
            return False
        if source_keys or pending:
            return True
        return False

    def _on_changes_detected(self, payload: dict):
        if self._is_closing:
            return
        if self._should_skip_route_only_refresh(payload):
            return
        if self._should_skip_local_write_refresh(payload):
            return
        entities = set(payload.get("entities") or payload.get("changed_entities") or [])
        watched = {
            "operation_cases",
            "operation_table_assignments",
            "operating_tables",
            "vitals",
            "orders",
            "operblock_timeline_events",
            "administrations",
        }
        if not entities or entities.intersection(watched):
            QTimer.singleShot(0, lambda: self.auto_refresh(force=False))

    def _show_report_placeholder(self):
        CustomMessageBox.information(
            self,
            "Отчет за операцию",
            "Отчет за операцию будет сформирован на следующем этапе",
        )

    def _show_board(self):
        self._set_protocol_chrome(False)
        self.stack.setCurrentWidget(self.board_page)
        self._current_operation_case_id = None
        self._current_admission_id = None
        self._current_operation_start = None
        self._current_operation_end = None
        self._current_case_active = False
        self._current_stage_state = {}
        self._current_anesthesia_start = None
        self._current_anesthesia_end = None
        self._current_surgery_start = None
        self._current_surgery_end = None
        self._current_anesthesia_active = False
        self._current_surgery_active = False
        self._current_anesthesia_assistance_type = ""
        self._current_operation_name = ""
        self._update_protocol_title_label()
        self._update_operblock_staff_legend()
        self._vitals_context_key = None
        self._current_orders_rows = []
        self._current_timeline_snapshot = None
        self._apply_active_infusions()
        self.operblock_vitals_service.set_operation_context(
            operation_case_id=None,
            admission_id=None,
            started_at=None,
            ended_at=None,
        )
        if getattr(self, "vitals_chart", None):
            self.vitals_chart.set_visible_hours(OPERBLOCK_INITIAL_CHART_HOURS)
            if hasattr(self.vitals_chart, "set_timeline_snapshot"):
                self.vitals_chart.set_timeline_snapshot(None, None, force=True)
            elif hasattr(self.vitals_chart, "set_operation_orders"):
                self.vitals_chart.set_operation_orders([], None)
        self.refresh_board(force=True)

    def _set_protocol_chrome(self, enabled: bool):
        self.sector_8_panel.set_protocol_mode(
            enabled,
            launcher_back=bool(getattr(self, "_role_launcher_mode", False) and not enabled),
        )

    def set_role_launcher_mode(self, enabled: bool):
        self._role_launcher_mode = bool(enabled)
        self._set_protocol_chrome(self.stack.currentWidget() == self.protocol_page)

    def apply_display_settings(self):
        if hasattr(self, "sector_8_panel"):
            self.sector_8_panel.apply_display_settings()
            self._set_protocol_chrome(self.stack.currentWidget() == self.protocol_page)
        self._apply_protocol_tab_display_settings()

    def _update_protocol_current_time_label(self):
        now_dt = datetime.now()
        label = getattr(self, "protocol_current_time_label", None)
        if label is not None:
            label.setText(f"Текущее время: {now_dt.strftime('%H:%M')}")
        self._refresh_elapsed_infusion_amounts_if_needed(now_dt)

    def _refresh_elapsed_infusion_amounts_if_needed(self, now_dt: datetime):
        if self._write_pending or self.stack.currentWidget() != self.protocol_page:
            return
        snapshot = getattr(self, "_current_timeline_snapshot", None) or {}
        if not any(str((interval or {}).get("status") or "") == "active" for interval in snapshot.get("infusion_intervals") or []):
            return
        minute_key = now_dt.strftime("%Y-%m-%dT%H:%M")
        if minute_key == getattr(self, "_last_infusion_elapsed_refresh_minute", ""):
            return
        self._last_infusion_elapsed_refresh_minute = minute_key
        self._apply_active_infusions(force_elapsed=True)

    def on_back_clicked(self):
        current_widget = self.stack.currentWidget()
        if current_widget in (self.protocol_page, self.archive_page):
            self._show_board()
            return
        if current_widget == self.board_page and getattr(self, "_role_launcher_mode", False):
            parent = self.parent()
            if parent is not None and hasattr(parent, "setCurrentIndex"):
                parent.setCurrentIndex(0)

    def shutdown(self):
        self._is_closing = True
        timer = getattr(self, "_protocol_clock_timer", None)
        if timer is not None:
            timer.stop()

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)

    def _set_patient_photo(self, label: QLabel, gender: str | None):
        apply_metrics = self._current_board_apply_metrics
        metric_fields = dict((apply_metrics or {}).get("current_card_fields") or {})
        metric_started = operblock_startup_metrics.timer_start() if apply_metrics is not None else 0.0
        if apply_metrics is not None:
            apply_metrics["photo_count"] = int(apply_metrics.get("photo_count") or 0) + 1
        try:
            path_started = operblock_startup_metrics.timer_start() if apply_metrics is not None else 0.0
            gender_text = str(gender or "").lower()
            if gender_text.startswith("жен"):
                name = "woman.png"
            elif gender_text.startswith("муж") or gender_text.startswith("м"):
                name = "man.png"
            else:
                name = "noman.png"
            path = os.path.join(get_patient_assets_dir(), "Patients", name)
            if not os.path.isfile(path):
                path = os.path.join(get_patient_assets_dir(), "Patients", "noman.png")
            operblock_startup_metrics.record_since(
                "board_apply_card_photo_path_ms",
                path_started,
                source="operblock_widget",
                **metric_fields,
            )
            if os.path.isfile(path):
                load_started = operblock_startup_metrics.timer_start() if apply_metrics is not None else 0.0
                target_size = label.size()
                try:
                    stat = os.stat(path)
                    cache_key = (
                        path,
                        int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
                        int(stat.st_size),
                        int(target_size.width()),
                        int(target_size.height()),
                    )
                except Exception:
                    cache_key = None
                cached = self._board_photo_thumbnail_cache.get(cache_key) if cache_key is not None else None
                if cached is not None and not cached.isNull():
                    if apply_metrics is not None:
                        apply_metrics["photo_cache_hit_count"] = int(apply_metrics.get("photo_cache_hit_count") or 0) + 1
                        operblock_startup_metrics.record_since(
                            "board_apply_card_photo_pixmap_load_ms",
                            load_started,
                            source="operblock_widget",
                            cache_hit=True,
                            **metric_fields,
                        )
                        operblock_startup_metrics.record_duration(
                            "board_apply_card_photo_scaled_ms",
                            0.0,
                            source="operblock_widget",
                            cache_hit=True,
                            **metric_fields,
                        )
                    label.setPixmap(cached)
                    return
                if apply_metrics is not None:
                    apply_metrics["photo_cache_miss_count"] = int(apply_metrics.get("photo_cache_miss_count") or 0) + 1
                reader = QImageReader(path)
                reader.setAutoTransform(True)
                source_size = reader.size()
                if source_size.isValid() and target_size.isValid() and not target_size.isEmpty():
                    scaled_size = source_size.scaled(target_size, Qt.KeepAspectRatio)
                    if scaled_size.isValid() and not scaled_size.isEmpty():
                        reader.setScaledSize(scaled_size)
                image = reader.read()
                if image.isNull():
                    source_pixmap = QPixmap(path)
                else:
                    source_pixmap = QPixmap.fromImage(image)
                operblock_startup_metrics.record_since(
                    "board_apply_card_photo_pixmap_load_ms",
                    load_started,
                    source="operblock_widget",
                    cache_hit=False,
                    **metric_fields,
                )
                scale_started = operblock_startup_metrics.timer_start() if apply_metrics is not None else 0.0
                pixmap = source_pixmap.scaled(target_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                operblock_startup_metrics.record_since(
                    "board_apply_card_photo_scaled_ms",
                    scale_started,
                    source="operblock_widget",
                    **metric_fields,
                )
                if not pixmap.isNull():
                    if cache_key is not None:
                        self._board_photo_thumbnail_cache[cache_key] = pixmap
                    label.setPixmap(pixmap)
                    return
            if apply_metrics is not None:
                apply_metrics["missing_photo_count"] = int(apply_metrics.get("missing_photo_count") or 0) + 1
            label.setText("Фото")
        finally:
            operblock_startup_metrics.record_since(
                "board_apply_card_photo_total_ms",
                metric_started,
                source="operblock_widget",
                **metric_fields,
            )

    @staticmethod
    def _clear_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            child_layout = item.layout()
            if widget:
                widget.deleteLater()
            if child_layout:
                OperBlockMainWidget._clear_layout(child_layout)
