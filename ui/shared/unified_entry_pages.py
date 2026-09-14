"""Standalone entry pages for the unified RemCard window.

The shell owns the native title frame and decides when each startup stage is
complete.  These widgets deliberately have no database or navigation
dependencies, which keeps the first rendered frame lightweight.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QRectF, Qt, QTimer, Signal, QPointF, QPropertyAnimation, Property, QEasingCurve, QSize
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPixmap, QPen, QPainterPath, QFontMetricsF
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from rem_card.app.roles import (
    ROLE_DOCTOR,
    ROLE_NURSE,
    ROLE_OPERBLOCK_EMERGENCY,
    ROLE_OPERBLOCK_PLANNED,
)
from rem_card.ui.shared.unified_chrome import HeartMark, entry_action_icon


_ROOT_DIR = Path(__file__).resolve().parents[2]
_ICON_DIR = _ROOT_DIR / "icon"
_BACKGROUND_PATH = _ICON_DIR / "unified_entry_background_v2.png"


class _EntryBackdrop(QWidget):
    """Atmospheric background that stays useful when the optional PNG is absent."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._background = QPixmap(str(_BACKGROUND_PATH)) if _BACKGROUND_PATH.is_file() else QPixmap()

    def paintEvent(self, event):  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        rect = self.rect()

        if not self._background.isNull():
            source_size = self._background.size()
            scale = max(rect.width() / source_size.width(), rect.height() / source_size.height())
            source = QRectF(
                (source_size.width() - rect.width() / scale) / 2,
                (source_size.height() - rect.height() / scale) / 2,
                rect.width() / scale,
                rect.height() / scale,
            )
            painter.drawPixmap(rect, self._background, source.toRect())
        else:
            gradient = QLinearGradient(0, 0, rect.width(), rect.height())
            gradient.setColorAt(0.0, QColor("#071b32"))
            gradient.setColorAt(0.48, QColor("#0c3557"))
            gradient.setColorAt(1.0, QColor("#061426"))
            painter.fillRect(rect, gradient)

        shade = QLinearGradient(0, 0, rect.width(), 0)
        shade.setColorAt(0.0, QColor(2, 23, 43, 105))
        shade.setColorAt(0.45, QColor(2, 21, 42, 105))
        shade.setColorAt(1.0, QColor(1, 15, 31, 105))
        painter.fillRect(rect, shade)
        painter.fillRect(rect, QColor(3, 17, 33, 48))


class _RoleCard(QPushButton):
    """A full-card native button, rather than a frame with a hidden click target."""

    def __init__(
        self,
        role_key: str,
        title: str,
        description: str,
        icon_name: str,
        accent: str,
        parent=None,
    ):
        super().__init__(parent)
        self.role_key = role_key
        self.title = title
        self.description = description
        self._icon = QPixmap(str(_ICON_DIR / icon_name))
        self._accent = QColor(accent)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumSize(178, 300)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAccessibleName(title)
        self.setAccessibleDescription(description)
        self._hover = 0.0
        self._animation = QPropertyAnimation(self, b'hoverAmount', self)
        self._animation.setDuration(160)
        self._animation.setEasingCurve(QEasingCurve.OutCubic)

    def _get_hover(self):
        return self._hover

    def _set_hover(self, value):
        self._hover = float(value)
        self.update()

    hoverAmount = Property(float, _get_hover, _set_hover)

    def enterEvent(self, event):
        self._animation.stop()
        self._animation.setStartValue(self._hover)
        self._animation.setEndValue(1.0)
        self._animation.start()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._animation.stop()
        self._animation.setStartValue(self._hover)
        self._animation.setEndValue(0.0)
        self._animation.start()
        super().leaveEvent(event)

    def paintEvent(self, event):  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        rect = QRectF(self.rect()).adjusted(7, 7, -7, -7)
        hovered = self._hover if self.isEnabled() else 0
        focused = self.hasFocus()

        fill = QColor(self._accent).darker(240)
        fill.setAlpha(int(172 + hovered * 38))
        border = QColor(self._accent)
        border.setAlpha(255 if focused else int(145 + hovered * 110))
        if hovered or focused:
            for spread in (6, 4, 2):
                glow = QColor(self._accent)
                glow.setAlpha(int((14 + hovered * 16) / (spread / 2)))
                painter.setPen(QPen(glow, spread * 2))
                painter.setBrush(Qt.NoBrush)
                painter.drawRoundedRect(rect, 14, 14)
        if not self.isEnabled():
            fill = QColor(8, 29, 49, 150)
            border = QColor(119, 145, 161, 88)
        painter.setPen(Qt.NoPen)
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        light = QColor(fill)
        light.setAlpha(min(235, fill.alpha() + 12))
        dark = QColor(fill)
        dark.setAlpha(max(98, fill.alpha() - 36))
        gradient.setColorAt(0.0, light)
        gradient.setColorAt(1.0, dark)
        painter.setBrush(gradient)
        painter.drawRoundedRect(rect, 18, 18)
        painter.setPen(QPen(border, 1.4 + hovered))
        painter.drawRoundedRect(rect, 18, 18)

        icon_side = min(106, int(rect.height() * .31))
        icon_rect = QRectF(rect.center().x() - icon_side / 2, rect.top() + 27, icon_side, icon_side)
        if not self._icon.isNull():
            scaled = self._icon.scaled(
                int(icon_side), int(icon_side), Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            painter.drawPixmap(
                int(icon_rect.center().x() - scaled.width() / 2), int(icon_rect.top()), scaled
            )
        else:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#55ccff"))
            painter.drawEllipse(icon_rect)
            painter.setPen(QColor("#04213a"))
            painter.setFont(QFont("Segoe UI", max(18, icon_side // 2), QFont.Bold))
            painter.drawText(icon_rect, Qt.AlignCenter, "+")

        title_rect = QRectF(rect.left() + 10, icon_rect.bottom() + 12, rect.width() - 20, 38)
        title_font = QFont("Segoe UI", 18, QFont.DemiBold)
        while QFontMetricsF(title_font).horizontalAdvance(self.title) > title_rect.width() and title_font.pointSize() > 10:
            title_font.setPointSize(title_font.pointSize()-1)
        painter.setFont(title_font)
        painter.setPen(QColor("#f3fbff") if self.isEnabled() else QColor("#a2b4c0"))
        painter.drawText(title_rect, Qt.AlignCenter | Qt.TextSingleLine, self.title)

        description_rect = QRectF(rect.left() + 16, title_rect.bottom() + 8, rect.width() - 32, max(40, rect.bottom()-title_rect.bottom()-72))
        painter.setFont(QFont("Segoe UI", 12 if rect.height() > 300 else 9))
        painter.setPen(QColor("#b9d3e3") if self.isEnabled() else QColor("#8496a3"))
        painter.drawText(
            description_rect,
            Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap,
            self.description,
        )
        arrow_rect = QRectF(rect.center().x() - 20, rect.bottom() - 56, 40, 40)
        arrow_fill = QLinearGradient(arrow_rect.topLeft(), arrow_rect.bottomRight())
        arrow_fill.setColorAt(0, self._accent.lighter(150))
        arrow_fill.setColorAt(1, self._accent.darker(150))
        painter.setPen(QPen(self._accent.lighter(165), 1))
        painter.setBrush(arrow_fill)
        painter.drawEllipse(arrow_rect)
        cx, cy = arrow_rect.center().x(), arrow_rect.center().y()
        painter.setPen(QPen(QColor('#ffffff'), 3.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawLine(QPointF(cx-8, cy), QPointF(cx+7, cy))
        arrow = QPainterPath(QPointF(cx+1, cy-6))
        arrow.lineTo(cx+7, cy)
        arrow.lineTo(cx+1, cy+6)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(arrow)


class _StageMarker(QLabel):
    def paintEvent(self, event):
        super().paintEvent(event)
        if self.property("complete"):
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(QPen(QColor("#e9fff8"), 2.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.drawPolyline([QPointF(7, 13), QPointF(11, 17), QPointF(19, 9)])


class _StageRow(QFrame):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETE = "complete"
    ERROR = "error"

    def __init__(self, number: int, title: str, parent=None):
        super().__init__(parent)
        self._number = number
        self._state = self.PENDING
        self.setObjectName("UnifiedStartupStage")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(44)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.marker = _StageMarker(str(number), self)
        self.marker.setObjectName("UnifiedStartupStageMarker")
        self.marker.setAlignment(Qt.AlignCenter)
        self.marker.setFixedSize(26, 26)
        self.label = QLabel(title, self)
        self.label.setObjectName("UnifiedStartupStageLabel")
        self.label.setWordWrap(True)
        layout.addWidget(self.marker)
        layout.addWidget(self.label, 1)
        self.set_state(self.PENDING)

    @property
    def state(self) -> str:
        return self._state

    def set_state(self, state: str) -> None:
        self._state = state
        marker_text = {
            self.PENDING: "·",
            self.ACTIVE: "…",
            self.COMPLETE: "",
            self.ERROR: "!",
        }[state]
        self.marker.setText(marker_text)
        self.marker.setProperty("complete", state == self.COMPLETE)
        self.marker.update()
        colors = {
            self.PENDING: ("#163b59", "#78a0bb", "#b7cddd"),
            self.ACTIVE: ("#087bb0", "#65d4ff", "#f2fbff"),
            self.COMPLETE: ("#16755f", "#62e1be", "#e9fff8"),
            self.ERROR: ("#8d3944", "#ff9ea8", "#fff3f4"),
        }[state]
        self.marker.setStyleSheet(
            f"background: {colors[0]}; border: 1px solid {colors[1]}; border-radius: 13px; "
            f"color: {colors[2]}; font: 700 13px 'Segoe UI';"
        )
        self.label.setStyleSheet(f"color: {colors[2]}; font: 18px 'Segoe UI';")


class _EntryPageBase(_EntryBackdrop):
    """Shared transparent content and top/floor treatment for entry pages."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._institution_full_name = ""
        self._institution_short_name = ""
        self.content = QWidget(self)
        self.content.setObjectName("UnifiedEntryContent")
        self.content.setAttribute(Qt.WA_StyledBackground, True)
        self.content.setStyleSheet("QWidget#UnifiedEntryContent { background: transparent; }")
        self.root_layout = QVBoxLayout(self.content)
        self.root_layout.setContentsMargins(54, 28, 54, 22)
        self.root_layout.setSpacing(0)

        page_layout = QVBoxLayout(self)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.addWidget(self.content)

    def set_institution(self, full_name: str, short_name: str) -> None:
        self._institution_full_name = " ".join(str(full_name or "").split())
        self._institution_short_name = " ".join(str(short_name or "").split())
        self._refresh_institution_labels()

    def _refresh_institution_labels(self) -> None:
        pass

    def resizeEvent(self, event):  # noqa: N802 - Qt API
        super().resizeEvent(event)
        margin = 24 if self.width() < 1100 else 36
        self.root_layout.setContentsMargins(margin, 24, margin, 18)


class WelcomePage(_EntryPageBase):
    role_selected = Signal(str)
    settings_requested = Signal()
    about_requested = Signal()
    update_requested = Signal()

    _ROLE_DETAILS = (
        (ROLE_DOCTOR, "Врач", "Ведение пациентов,\nназначения, процедуры,\nанализы и отчёты", "entry_doctor_glass.png", "#35b9ff"),
        (ROLE_NURSE, "Медсестра", "Выполнение назначений,\nмониторинг и учёт\nманипуляций", "entry_nurse_glass.png", "#5ee3d4"),
        (ROLE_OPERBLOCK_EMERGENCY, "Экстренный оперблок", "Неотложные вмешательства,\nанестезия и\nоперационные карты", "entry_scalpel_glass.png", "#b793ff"),
        (ROLE_OPERBLOCK_PLANNED, "Плановый оперблок", "Плановые вмешательства,\nанестезия и\nоперационные карты", "entry_scalpel_glass.png", "#94bde5"),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._access_blocked = False
        self._access_message = ""
        self._compact_grid = None
        self._build_ui()
        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(1000)
        self._clock_timer.timeout.connect(self._refresh_clock)
        self._clock_timer.start()
        self._refresh_clock()
        self._update_role_grid()

    def _build_ui(self) -> None:
        header = QHBoxLayout()
        header.setSpacing(12)
        self.logo_label = HeartMark(self.content)
        self.logo_label.setFixedSize(82, 82)
        header.addWidget(self.logo_label)
        brand_box = QVBoxLayout()
        brand_box.setSpacing(0)
        self.title_label = QLabel("РЕМКАРТА", self.content)
        self.title_label.setObjectName("UnifiedEntryTitle")
        self.title_label.setStyleSheet("color: #f7fcff; font: 700 34px 'Segoe UI'; letter-spacing: 1px;")
        self.subtitle_label = QLabel("Реанимационная карта", self.content)
        self.subtitle_label.setObjectName("UnifiedEntrySubtitle")
        self.subtitle_label.setStyleSheet("color: #d6edff; font: 17px 'Segoe UI';")
        brand_box.addWidget(self.title_label)
        brand_box.addWidget(self.subtitle_label)
        header.addLayout(brand_box, 1)

        self.hospital_label = QLabel(self.content)
        self.hospital_label.setStyleSheet("color: #e1f5ff; font: 18px 'Segoe UI';")
        self.context_label = QLabel("ОАРИТ", self.content)
        self.context_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.context_label.setStyleSheet("color: #e1f5ff; font: 18px 'Segoe UI';")
        self.clock_label = QLabel(self.content)
        self.clock_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.clock_label.setMinimumWidth(138)
        self.clock_label.setStyleSheet("color: #cfefff; font: 18px 'Segoe UI';")
        metadata = QHBoxLayout()
        metadata.setSpacing(18)

        def separator():
            line = QFrame(self.content)
            line.setFixedSize(1, 24)
            line.setStyleSheet('background: #7ba4c4; border: none;')
            return line

        self.hospital_separator = separator()
        metadata.addWidget(self.hospital_label)
        metadata.addWidget(self.hospital_separator)
        metadata.addWidget(self.context_label)
        metadata.addWidget(separator())
        clock = QLabel(self.content)
        clock.setPixmap(entry_action_icon('clock').pixmap(24, 24))
        clock.setFixedSize(24, 24)
        clock_row = QHBoxLayout()
        clock_row.setSpacing(9)
        clock_row.addWidget(clock)
        clock_row.addWidget(self.clock_label)
        metadata.addLayout(clock_row)
        header.addLayout(metadata)
        self.root_layout.addLayout(header)

        self.institution_label = QLabel(self.content)
        self.institution_label.setObjectName("UnifiedEntryInstitution")
        self.institution_label.setStyleSheet("color: #c4dce9; font: 11px 'Segoe UI';")
        self.institution_label.setWordWrap(True)
        self.root_layout.addWidget(self.institution_label)

        self.root_layout.addStretch(1)
        heading = QLabel("Добро пожаловать в РЕМКАРТУ", self.content)
        heading.setAlignment(Qt.AlignCenter)
        heading.setStyleSheet("color: #f7fcff; font: 600 34px 'Segoe UI';")
        self.root_layout.addWidget(heading)
        helper = QLabel(
            "Единое приложение для всех специалистов отделения реанимации.\n"
            "Выберите свою роль, чтобы продолжить работу.",
            self.content,
        )
        helper.setAlignment(Qt.AlignCenter)
        helper.setStyleSheet("color: #badff9; font: 20px 'Segoe UI'; line-height: 1.5;")
        self.root_layout.addWidget(helper)

        self.root_layout.addSpacing(18)
        self.roles_area = QWidget(self.content)
        self.roles_grid = QGridLayout(self.roles_area)
        self.roles_grid.setContentsMargins(0, 0, 0, 0)
        self.roles_grid.setHorizontalSpacing(4)
        self.roles_grid.setVerticalSpacing(14)
        self.role_buttons: dict[str, _RoleCard] = {}
        self._role_cards: list[_RoleCard] = []
        for role_key, title, description, icon_name, accent in self._ROLE_DETAILS:
            card = _RoleCard(role_key, title, description, icon_name, accent, self.roles_area)
            card.clicked.connect(lambda _checked=False, key=role_key: self.role_selected.emit(key))
            self.role_buttons[role_key] = card
            self._role_cards.append(card)
        self.root_layout.addWidget(self.roles_area, 5)
        self.root_layout.addStretch(1)

        self.access_label = QLabel(self.content)
        self.access_label.setObjectName("UnifiedEntryAccessState")
        self.access_label.setAlignment(Qt.AlignCenter)
        self.access_label.setWordWrap(True)
        self.access_label.setMinimumHeight(22)
        self.root_layout.addWidget(self.access_label)
        self.root_layout.addSpacing(6)

        footer = QHBoxLayout()
        footer.setSpacing(9)
        self.settings_button = self._footer_button("Настройки")
        self.about_button = self._footer_button("О программе")
        self.update_button = self._footer_button("Обновить")
        self.update_button.hide()
        self.settings_button.clicked.connect(self.settings_requested)
        self.about_button.clicked.connect(self.about_requested)
        self.update_button.clicked.connect(self.update_requested)
        footer.addWidget(self.settings_button)
        footer.addStretch(1)
        footer.addWidget(self.about_button)
        footer.addWidget(self.update_button)
        self.root_layout.addLayout(footer)
        self._refresh_institution_labels()
        self.set_access_state()

    def _footer_button(self, text: str) -> QPushButton:
        button = QPushButton(text, self.content)
        if text in ('Настройки', 'О программе'):
            button.setIcon(entry_action_icon('settings' if text == 'Настройки' else 'about'))
            button.setIconSize(QSize(26, 26))
        button.setCursor(Qt.PointingHandCursor)
        button.setMinimumHeight(40)
        button.setStyleSheet(
            "QPushButton { color: #e9f5ff; background: transparent; "
            "border: 1px solid transparent; border-radius: 8px; padding: 0 13px; "
            "font: 17px 'Segoe UI'; }"
            "QPushButton:hover { background: rgba(21, 92, 135, 218); border-color: #65d4ff; }"
            "QPushButton:focus { border: 2px solid #65d4ff; }"
            "QPushButton:disabled { color: #718897; border-color: rgba(113, 136, 151, 80); }"
        )
        return button

    def _refresh_clock(self) -> None:
        self.clock_label.setText(datetime.now().strftime("%d.%m.%Y  %H:%M"))

    def _refresh_institution_labels(self) -> None:
        short = self._institution_short_name
        self.hospital_label.setText(short)
        self.hospital_label.setVisible(bool(short))
        self.hospital_separator.setVisible(bool(short))
        self.institution_label.setText(self._institution_full_name or short)
        self.institution_label.setVisible(False)

    def set_access_state(self, message: str = "", blocked: bool = False) -> None:
        self._access_blocked = bool(blocked)
        self._access_message = str(message or "").strip()
        for card in self._role_cards:
            card.setEnabled(not self._access_blocked)
        self.access_label.setText(self._access_message)
        if self._access_blocked:
            self.access_label.setStyleSheet("color: #ffc2c8; font: 600 12px 'Segoe UI';")
        else:
            self.access_label.setStyleSheet("color: #91dabc; font: 600 12px 'Segoe UI';")
        self.access_label.setVisible(bool(self._access_message))

    def set_update_available(self, version: str = "") -> None:
        version = str(version or "").strip()
        self.update_button.setVisible(bool(version))
        self.about_button.setVisible(not bool(version))
        self.update_button.setText(f"Обновить до {version}" if version else "Обновить")

    def _update_role_grid(self) -> None:
        compact = False  # All four roles stay in one row, including compact desktop windows.
        if compact == self._compact_grid:
            return
        self._compact_grid = compact
        self.roles_area.setMaximumHeight(365)
        while self.roles_grid.count():
            self.roles_grid.takeAt(0)
        for row in range(2):
            self.roles_grid.setRowStretch(row, 0)
        for column in range(4):
            self.roles_grid.setColumnStretch(column, 0)
        columns = 2 if compact else 4
        for index, card in enumerate(self._role_cards):
            self.roles_grid.addWidget(card, index // columns, index % columns)
        for column in range(columns):
            self.roles_grid.setColumnStretch(column, 1)
        rows = 2 if compact else 1
        for row in range(rows):
            self.roles_grid.setRowStretch(row, 1)

    def resizeEvent(self, event):  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._update_role_grid()


class StartupPage(_EntryPageBase):
    """Startup feedback with five explicit completion points controlled by the shell."""

    STAGES = (
        "Проверяем конфигурацию",
        "Проверяем совместимость базы",
        "Загружаем настройки учреждения",
        "Подготавливаем модули",
        "Завершаем запуск",
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._active_stage: int | None = None
        self._last_stage: int | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        self.root_layout.addStretch(1)
        panel = QFrame(self.content)
        panel.setObjectName("UnifiedStartupPanel")
        panel.setMaximumWidth(690)
        panel.setMinimumWidth(600)
        panel.setStyleSheet(
            "QFrame#UnifiedStartupPanel { background: transparent; border: none; }"
        )
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(38, 30, 38, 28)
        panel_layout.setSpacing(9)
        brand = QHBoxLayout()
        self.startup_logo_label = HeartMark(panel)
        self.startup_logo_label.setFixedSize(116, 116)
        brand.addWidget(self.startup_logo_label)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        self.title_label = QLabel("РЕМКАРТА", panel)
        self.title_label.setStyleSheet("color: #f7fcff; font: 700 51px 'Segoe UI'; letter-spacing: 1px;")
        self.subtitle_label = QLabel("Реанимационная карта", panel)
        self.subtitle_label.setStyleSheet("color: #a8dfff; font: 25px 'Segoe UI';")
        brand_text.addWidget(self.title_label)
        brand_text.addWidget(self.subtitle_label)
        brand.addLayout(brand_text, 1)
        panel_layout.addLayout(brand)
        panel_layout.addSpacing(22)

        self.stage_rows: list[_StageRow] = []
        for number, title in enumerate(self.STAGES, start=1):
            row = _StageRow(number, title, panel)
            self.stage_rows.append(row)
            panel_layout.addWidget(row)

        self.progress_bar = QProgressBar(panel)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%p%")
        self.progress_bar.setAlignment(Qt.AlignCenter)
        self.progress_bar.setFixedHeight(24)
        self.progress_bar.setStyleSheet(
            "QProgressBar { color: #e9faff; background: #102d45; border: 1px solid #4e94b9; "
            "border-radius: 11px; font: 700 12px 'Segoe UI'; }"
            "QProgressBar::chunk { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #9feaff,stop:0.5 #74c7ff,stop:1 #4aabf6); border-radius: 10px; }"
        )
        panel_layout.addSpacing(8)
        panel_layout.addWidget(self.progress_bar)
        panel_layout.removeWidget(self.progress_bar)
        loading_title = QLabel("Загрузка системы…", panel)
        loading_title.setAlignment(Qt.AlignCenter)
        loading_title.setStyleSheet("color:#f1f7ff; font:600 26px 'Segoe UI'; margin-bottom:8px;")
        panel_layout.insertWidget(2, loading_title)
        panel_layout.insertWidget(3, self.progress_bar)
        panel_layout.insertSpacing(4, 12)
        self.message_label = QLabel("Ожидание запуска", panel)
        self.message_label.setObjectName("UnifiedStartupMessage")
        self.message_label.setWordWrap(True)
        self.message_label.setMinimumHeight(34)
        self.message_label.setStyleSheet("color: #c3dce9; font: 12px 'Segoe UI';")
        panel_layout.addWidget(self.message_label)
        self.error_label = QLabel(panel)
        self.error_label.setObjectName("UnifiedStartupError")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #ffbbc3; font: 600 12px 'Segoe UI';")
        self.error_label.hide()
        panel_layout.addWidget(self.error_label)

        centered = QHBoxLayout()
        centered.addStretch(1)
        centered.addWidget(panel)
        centered.addStretch(1)
        self.root_layout.addLayout(centered)
        self.root_layout.addStretch(1)
        self.institution_label = QLabel(self.content)
        self.institution_label.setAlignment(Qt.AlignRight | Qt.AlignBottom)
        self.institution_label.setWordWrap(True)
        self.institution_label.setStyleSheet("color: #afd9fa; font: 16px 'Segoe UI';")
        self.root_layout.addWidget(self.institution_label)
        self._refresh_institution_labels()

    def _refresh_institution_labels(self) -> None:
        department = "Отделение анестезиологии, реанимации и интенсивной терапии"
        full = self._institution_full_name
        self.institution_label.setText(f"{full}\n{department}" if full else department)

    def set_stage(self, index: int, message: str = "") -> None:
        if not 0 <= int(index) < len(self.stage_rows):
            return
        stage_index = int(index)
        self._active_stage = stage_index
        self._last_stage = stage_index
        self.error_label.hide()
        for position, row in enumerate(self.stage_rows):
            if position == stage_index:
                row.set_state(_StageRow.ACTIVE)
            elif row.state in (_StageRow.ACTIVE, _StageRow.ERROR):
                row.set_state(_StageRow.PENDING)
        self._sync_progress()
        if message:
            self.message_label.setText(str(message))

    def complete_stage(self, index: int) -> None:
        if not 0 <= int(index) < len(self.stage_rows):
            return
        stage_index = int(index)
        self.stage_rows[stage_index].set_state(_StageRow.COMPLETE)
        if self._active_stage == stage_index:
            self._active_stage = None
        self._sync_progress()

    def set_error(self, message) -> None:
        text = str(message or "Не удалось завершить запуск.").strip()
        stage_index = self._active_stage if self._active_stage is not None else self._last_stage
        if stage_index is not None:
            self.stage_rows[stage_index].set_state(_StageRow.ERROR)
        self.error_label.setText(text)
        self.error_label.show()
        self.message_label.setText("Запуск остановлен")

    def reset(self) -> None:
        """Return the startup page to its explicit, reusable initial state."""
        self._active_stage = None
        self._last_stage = None
        for row in self.stage_rows:
            row.set_state(_StageRow.PENDING)
        self.error_label.clear()
        self.error_label.hide()
        self.message_label.setText("Ожидание запуска")
        self._sync_progress()

    def _sync_progress(self) -> None:
        completed = sum(row.state == _StageRow.COMPLETE for row in self.stage_rows)
        self.progress_bar.setValue(round(completed * 100 / len(self.stage_rows)))
