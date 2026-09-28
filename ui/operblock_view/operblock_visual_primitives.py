from __future__ import annotations
from rem_card.ui.styles.theme_runtime import themed_qcolor
from rem_card.ui.styles.theme_runtime import set_widget_style

import os

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QWidget,
)

from rem_card.app.paths import get_icon_dir
from rem_card.services.operblock_route_settings import (
    OPERBLOCK_DEFAULT_ROUTE_CODE,
)
from rem_card.ui.shared.display_settings_storage import (
    DisplaySettingsStorage,
    SECTOR8_BUTTON_SIDE_LEFT,
    SECTOR8_BUTTON_SIDE_RIGHT,
    ordered_visible_ids_by_side,
    role_display_settings_from_payload,
)
from rem_card.ui.styles.theme import (
    BG_LIGHT,
    BG_MAIN,
    BORDER_COLOR,
    BORDER_LIGHT,
    COLOR_DANGER,
    CUSTOM_DIALOG_RADIUS,
    STYLE_PATIENT_FORM_CANCEL_BUTTON,
    STYLE_PATIENT_FORM_SAVE_BUTTON,
    STYLE_SECTOR8_BUTTON,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)


OPERBLOCK_VITAL_SETTINGS = {"ad": 1, "pulse": 1, "temp": 0, "spo2": 1, "rr": 0, "cvp": 0}
OPERBLOCK_IDLE_DIAGNOSTIC_THRESHOLD_SEC = 300.0
OPERBLOCK_INITIAL_CHART_HOURS = 3
OPERBLOCK_CHART_EXPAND_THRESHOLD_MINUTES = 20
OPERBLOCK_BOARD_MEDICATION_SCROLL_MAX_HEIGHT = 262
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
OPERBLOCK_ORDERS_SORT_OPTIONS = (
    ("time_desc", "По времени (новые сверху)"),
    ("time_asc", "По времени (старые сверху)"),
    ("drug", "По препарату"),
    ("active_only", "Только активные"),
)
OPERBLOCK_ORDER_ROUTE_DEFAULT = OPERBLOCK_DEFAULT_ROUTE_CODE
OPERBLOCK_ORDER_ROUTE_INTRAMUSCULAR = "im"
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
        background-color: transparent;
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
    set_widget_style(label, f"font-size: {size}px; font-weight: {weight}; color: {color}; background: transparent; border: none;")
    return label


class _OperBlockEmptyRoomIllustration(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("OperBlockEmptyStateIllustration")
        self.setFixedSize(144, 144)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def paintEvent(self, event):  # noqa: N802
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        circle_rect = QRectF(3, 3, 138, 138)
        clip = QPainterPath()
        clip.addEllipse(circle_rect)
        painter.setClipPath(clip)
        painter.fillPath(clip, themed_qcolor("#EEF7FF", "background"))

        painter.setPen(Qt.NoPen)
        painter.setBrush(themed_qcolor("#FFFFFF", "background"))
        painter.drawRoundedRect(QRectF(25, 24, 44, 34), 6, 6)
        painter.drawRoundedRect(QRectF(75, 24, 44, 34), 6, 6)
        painter.setBrush(themed_qcolor("#D9EFFF", "background"))
        painter.drawRect(QRectF(68, 24, 2, 34))

        painter.setBrush(themed_qcolor("#E2E8F0", "background"))
        painter.drawRect(QRectF(0, 102, 144, 42))
        painter.setPen(QPen(themed_qcolor("#C7D4E2", "text"), 1))
        painter.drawLine(20, 115, 124, 115)

        painter.setPen(QPen(themed_qcolor("#8AA3B8", "text"), 4, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(45, 71, 72, 84)
        painter.setBrush(themed_qcolor("#FFFFFF", "background"))
        painter.setPen(QPen(themed_qcolor("#BED0DE", "text"), 2))
        painter.drawRoundedRect(QRectF(42, 80, 66, 20), 10, 10)
        painter.setBrush(themed_qcolor("#BFE3EA", "background"))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(QRectF(50, 84, 48, 12), 6, 6)
        painter.setPen(QPen(themed_qcolor("#8AA3B8", "text"), 3, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(58, 100, 50, 116)
        painter.drawLine(93, 100, 101, 116)

        painter.setPen(QPen(themed_qcolor("#7C93A7", "text"), 3, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(98, 68, 98, 92)
        painter.setBrush(themed_qcolor("#FFFFFF", "background"))
        painter.setPen(QPen(themed_qcolor("#AFC3D3", "text"), 2))
        painter.drawRoundedRect(QRectF(102, 64, 23, 18), 4, 4)
        painter.setBrush(themed_qcolor("#93C5FD", "background"))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(QRectF(106, 68, 15, 10), 2, 2)

        painter.setPen(QPen(themed_qcolor("#9AAFC1", "text"), 3, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(72, 20, 72, 45)
        painter.drawLine(72, 45, 58, 58)
        painter.drawLine(72, 45, 86, 58)
        painter.setBrush(themed_qcolor("#FDFDFE", "background"))
        painter.setPen(QPen(themed_qcolor("#BAC8D6", "text"), 2))
        painter.drawEllipse(QRectF(49, 54, 18, 12))
        painter.drawEllipse(QRectF(77, 54, 18, 12))
        painter.setBrush(themed_qcolor("#DDF4FF", "background"))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QRectF(54, 58, 8, 5))
        painter.drawEllipse(QRectF(82, 58, 8, 5))

        painter.setClipping(False)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(themed_qcolor("#BBD7EA", "text"), 2))
        painter.drawEllipse(circle_rect)
        painter.end()


class _OperBlockCircleIcon(QWidget):
    def __init__(
        self,
        kind: str,
        *,
        background: str,
        border: str,
        foreground: str,
        size: int,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._kind = str(kind or "")
        self._background = QColor(background)
        self._border = QColor(border)
        self._foreground = QColor(foreground)
        self.setFixedSize(size, size)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def paintEvent(self, event):  # noqa: N802
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        rect = QRectF(1, 1, self.width() - 2, self.height() - 2)
        painter.setBrush(self._background)
        painter.setPen(QPen(self._border, 1.2))
        painter.drawEllipse(rect)

        painter.setPen(QPen(self._foreground, 2.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        width = float(self.width())
        height = float(self.height())
        if self._kind == "check":
            painter.drawLine(QPointF(width * 0.31, height * 0.51), QPointF(width * 0.44, height * 0.64))
            painter.drawLine(QPointF(width * 0.44, height * 0.64), QPointF(width * 0.70, height * 0.37))
        else:
            painter.drawLine(QPointF(width * 0.50, height * 0.43), QPointF(width * 0.50, height * 0.68))
            painter.drawPoint(QPointF(width * 0.50, height * 0.30))
        painter.end()


class _OperBlockBoardProgressStepper(QWidget):
    def __init__(self, stages: list[str], active_index: int, fill_fraction: float, parent: QWidget | None = None):
        super().__init__(parent)
        self._stages = list(stages)
        self._active_index = int(active_index)
        self._fill_fraction = max(0.0, min(1.0, float(fill_fraction)))
        self.setFixedHeight(92)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def paintEvent(self, event):  # noqa: N802
        super().paintEvent(event)
        if not self._stages:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        width = max(1, self.width())
        circle = 40
        radius = circle / 2.0
        center_y = 28.0
        label_width = 112.0
        left = max(radius + 6.0, label_width / 2.0)
        right = max(left + 1.0, width - left)
        span = right - left
        points = [left + (span * index / max(1, len(self._stages) - 1)) for index in range(len(self._stages))]

        line_pen = QPen(themed_qcolor("#9AA8B8", "text"), 2, Qt.SolidLine, Qt.RoundCap)
        painter.setPen(line_pen)
        painter.drawLine(int(points[0]), int(center_y), int(points[-1]), int(center_y))

        active_end = points[0] + span * self._fill_fraction
        active_pen = QPen(themed_qcolor("#2563EB", "text"), 2, Qt.SolidLine, Qt.RoundCap)
        painter.setPen(active_pen)
        painter.drawLine(int(points[0]), int(center_y), int(active_end), int(center_y))

        circle_font = QFont()
        circle_font.setPointSize(10)
        circle_font.setWeight(QFont.Weight.Bold)
        label_font = QFont()
        label_font.setPointSize(9)
        label_font.setWeight(QFont.Weight.DemiBold)

        for index, stage in enumerate(self._stages):
            is_active = index <= self._active_index or self._active_index == len(self._stages) - 1
            border = themed_qcolor("#2563EB" if is_active else "#CBD5E1", "text")
            text = themed_qcolor("#2563EB" if is_active else "#94A3B8", "text")
            circle_rect = QRectF(points[index] - radius, center_y - radius, circle, circle)
            painter.setPen(QPen(border, 2))
            painter.setBrush(themed_qcolor("#FFFFFF", "background"))
            painter.drawEllipse(circle_rect)
            painter.setFont(circle_font)
            painter.setPen(text)
            painter.drawText(circle_rect, Qt.AlignCenter, str(index + 1))

            label_rect = QRectF(points[index] - label_width / 2.0, center_y + radius + 11.0, label_width, 24.0)
            painter.setFont(label_font)
            painter.setPen(themed_qcolor("#1F2D3D", "text"))
            painter.drawText(label_rect, Qt.AlignHCenter | Qt.AlignTop, stage)

        painter.end()
class OperBlockSector8Panel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.icon_dir = get_icon_dir()
        self._protocol_mode = False
        self._launcher_back = False
        self._roles_available = False
        self._display_visible: dict[str, bool] = {}
        self._display_order: list[str] = []
        self._init_ui()

    def _init_ui(self):
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(10, 0, 10, 0)
        self.layout.setSpacing(10)

        self.btn_archive = self._button(" Архив", "binder.png")
        self.btn_refresh = self._button(" Обновить", "refresh.png")
        self.btn_user_report = self._button(" Репорт", "warning.png")
        self.btn_user_reports = self._button(" Репорты", "reports.png")
        self.btn_settings = self._button(" Настройки", "settings.png")
        self.btn_back = self._button(" Назад", "back.png")
        self.btn_roles = self._button(" Роли", "role.png")
        self.btn_exit = self._button(" Выход", "exit.png")
        self._button_widgets = {
            "archive": self.btn_archive,
            "refresh": self.btn_refresh,
            "user_report": self.btn_user_report,
            "user_reports": self.btn_user_reports,
            "settings": self.btn_settings,
            "back": self.btn_back,
            "roles": self.btn_roles,
            "exit": self.btn_exit,
        }
        self._reports_count_timer = QTimer(self)
        self._reports_count_timer.timeout.connect(self.refresh_user_reports_count)
        self._reports_count_timer.start(60000)
        self.apply_display_settings()
        QTimer.singleShot(0, self.refresh_user_reports_count)

    def _button(self, text: str, icon_name: str) -> QPushButton:
        button = QPushButton(text, self)
        icon_path = os.path.join(self.icon_dir, icon_name)
        if os.path.exists(icon_path):
            button.setIcon(QIcon(icon_path))
            button.setIconSize(button.iconSize())
        button.setMinimumHeight(32)
        button.setCursor(Qt.PointingHandCursor)
        set_widget_style(button, STYLE_SECTOR8_BUTTON)
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
            left_order = ordered_visible_ids_by_side(section, SECTOR8_BUTTON_SIDE_LEFT)
            right_order = ordered_visible_ids_by_side(section, SECTOR8_BUTTON_SIDE_RIGHT)
        except Exception:
            order = list(getattr(self, "_button_widgets", {}).keys())
            visible = {button_id: True for button_id in order}
            left_order = [
                button_id
                for button_id in order
                if button_id in {"user_report", "user_reports"} and bool(visible.get(button_id, True))
            ]
            right_order = [
                button_id
                for button_id in order
                if button_id not in {"user_report", "user_reports"} and bool(visible.get(button_id, True))
            ]

        self._display_order = [button_id for button_id in order if button_id in self._button_widgets]
        for button_id in self._button_widgets:
            if button_id not in self._display_order:
                self._display_order.append(button_id)
        self._display_visible = {
            button_id: bool(visible.get(button_id, True))
            for button_id in self._button_widgets
        }

        self._clear_layout()
        for button in self._button_widgets.values():
            button.setVisible(False)
        for button_id in left_order:
            button = self._button_widgets.get(button_id)
            if button is None or (button_id == "roles" and not self._roles_available):
                continue
            if self._display_visible.get(button_id, True):
                self.layout.addWidget(button)
                button.setVisible(True)
        self.layout.addStretch(1)
        for button_id in right_order:
            button = self._button_widgets.get(button_id)
            if button is None or (button_id == "roles" and not self._roles_available):
                continue
            if self._display_visible.get(button_id, True):
                self.layout.addWidget(button)
                button.setVisible(True)
        self._apply_back_visibility()
        self.updateGeometry()

    def set_roles_available(self, available: bool):
        available = bool(available)
        if self._roles_available == available:
            return
        self._roles_available = available
        self.apply_display_settings()

    def _apply_back_visibility(self):
        visible_by_settings = bool(self._display_visible.get("back", True))
        self.btn_back.setVisible(visible_by_settings)

    def set_protocol_mode(self, enabled: bool, *, launcher_back: bool = False):
        self._protocol_mode = bool(enabled)
        self._launcher_back = bool(launcher_back)
        self._apply_back_visibility()

    def refresh_user_reports_count(self):
        button = getattr(self, "btn_user_reports", None)
        if button is None:
            return
        try:
            from rem_card.services.user_reports import UserReportsService

            count = UserReportsService().count_new_reports()
        except Exception:
            count = 0
        button.setText(f" Репорты ({count})" if count else " Репорты")
        button.setToolTip(f"Новых репортов: {count}" if count else "Новых репортов нет")


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


class FittingSingleLineLabel(QLabel):
    def __init__(
        self,
        text: str = "",
        *,
        max_pixel_size: int = 16,
        min_pixel_size: int = 16,
        weight: int = 700,
        color: str = TEXT_PRIMARY,
        parent=None,
    ):
        super().__init__(parent)
        self._full_text = ""
        self._max_pixel_size = max(1, int(max_pixel_size))
        self._min_pixel_size = max(1, min(int(min_pixel_size), self._max_pixel_size))
        self._weight = int(weight)
        self._current_pixel_size = 0
        self.setWordWrap(False)
        self.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.setMinimumWidth(220)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        set_widget_style(self, f"color: {color}; background: transparent; border: none;")
        self.set_full_text(text)

    def setText(self, text: str):
        self.set_full_text(text)

    def set_full_text(self, text: str):
        self._full_text = str(text or "")
        self.setToolTip(self._full_text)
        if self.text() != self._full_text:
            super().setText(self._full_text)
        self._fit_font_to_width()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_font_to_width()

    def _fit_font_to_width(self):
        width = max(0, self.contentsRect().width())
        target_size = self._max_pixel_size
        if width > 0 and self._full_text:
            target_size = self._min_pixel_size
            for size in range(self._max_pixel_size, self._min_pixel_size - 1, -1):
                font = QFont(self.font())
                font.setPixelSize(size)
                font.setBold(self._weight >= 600)
                if QFontMetrics(font).horizontalAdvance(self._full_text) <= width:
                    target_size = size
                    break
        if self._current_pixel_size == target_size:
            return
        font = QFont(self.font())
        font.setPixelSize(target_size)
        font.setBold(self._weight >= 600)
        self.setFont(font)
        self._current_pixel_size = target_size


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
