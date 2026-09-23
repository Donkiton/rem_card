"""Standalone entry pages for the unified RemCard window.

The shell owns the native title frame and decides when each startup stage is
complete.  These widgets deliberately have no database or navigation
dependencies, which keeps the first rendered frame lightweight.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re

from PySide6.QtCore import QRectF, Qt, QTimer, Signal, QPointF, QPropertyAnimation, Property, QEasingCurve, QSize
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPixmap, QPen, QPainterPath, QFontMetricsF
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QGraphicsDropShadowEffect,
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
from rem_card.ui.shared.unified_entry_art import PulseLine, pulse
from rem_card.ui.shared.theme_switch import ThemeSwitch


_ROOT_DIR = Path(__file__).resolve().parents[2]
_ICON_DIR = _ROOT_DIR / "icon"
_BACKGROUND_PATH = _ICON_DIR / "unified_entry_background_v2.png"
_LIGHT_COLORS = {
    '#f7fcff': '#101543', '#f3fbff': '#101543', '#d6edff': '#152447',
    '#e1f5ff': '#142647', '#cfefff': '#214568', '#9dd5f5': '#244467',
    '#9bddff': '#0787da', '#badff9': '#304e70', '#c4dce9': '#385573',
    '#bce7ff': '#233f60', '#93c9ef': '#355777', '#65a6cb': '#416b89',
    '#e9f5ff': '#142647', '#91dabc': '#176749', '#ffc2c8': '#a02b40',
    '#ffbbc3': '#a02b40', '#c3dce9': '#345372', '#b9d3e3': '#345372',
    '#f1f7ff': '#101543', '#afd9fa': '#345372', '#a8dfff': '#345372',
}


def _entry_style(style, mode):
    if mode != 'light':
        return style
    style = style.replace('rgba(21, 92, 135, 218)', 'rgba(210, 233, 249, 230)')
    return re.sub(r'#[0-9a-fA-F]{6}', lambda m: _LIGHT_COLORS.get(m[0].lower(), m[0]), style)


class _InstitutionLabel(QLabel):
    """Keep long organization names from displacing the clock or brand."""

    def sizeHint(self):
        size = super().sizeHint()
        size.setWidth(min(380, size.width()))
        return size

    def minimumSizeHint(self):
        return QSize(40, super().minimumSizeHint().height())

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setFont(self.font())
        painter.setPen(QColor('#142647' if self.property('entry_theme') == 'light' else '#e1f5ff'))
        text = self.fontMetrics().elidedText(self.text(), Qt.ElideRight, self.width())
        painter.drawText(self.rect(), Qt.AlignLeft | Qt.AlignVCenter, text)


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

        if getattr(self, '_welcome_art', False):
            painter.fillRect(rect, QColor(255, 252, 247, 22) if getattr(self, '_theme_mode', 'dark') == 'light' else QColor(2, 17, 34, 42))
            return
        if getattr(self, '_theme_mode', 'dark') == 'light':
            painter.fillRect(rect, QColor(255, 252, 247, 95))
            return
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
        accent: str,
        parent=None,
    ):
        super().__init__(parent)
        self.role_key = role_key
        self._theme_mode = 'dark'
        self.title = title
        self.description = description
        atlas = QPixmap(str(_ICON_DIR / 'unified_role_photos_v3.png'))
        index = (ROLE_DOCTOR, ROLE_NURSE, ROLE_OPERBLOCK_EMERGENCY, ROLE_OPERBLOCK_PLANNED).index(role_key)
        self._photo = atlas.copy((index % 2) * (atlas.width() // 2),
                                 (index // 2) * (atlas.height() // 2),
                                 atlas.width() // 2, atlas.height() // 2) if not atlas.isNull() else QPixmap()
        self._accent = QColor(accent)
        self._dark_accent = accent
        light_atlas = QPixmap(str(_ICON_DIR / 'unified_role_photos_light_v3.png'))
        light_photo = light_atlas.copy((index % 2) * (light_atlas.width() // 2),
                                      (index // 2) * (light_atlas.height() // 2),
                                      light_atlas.width() // 2, light_atlas.height() // 2) if not light_atlas.isNull() else QPixmap()
        self._photos = {'dark': self._photo, 'light': light_photo}
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumSize(178, 360)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAccessibleName(title)
        self.setAccessibleDescription(description)
        self._hover = 0.0
        self._animation = QPropertyAnimation(self, b'hoverAmount', self)
        self._animation.setDuration(160)
        self._animation.setEasingCurve(QEasingCurve.OutCubic)

    def _get_hover(self):
        return self._hover

    def set_theme(self, mode):
        self._theme_mode = mode
        accents = {ROLE_DOCTOR: '#167ddb', ROLE_NURSE: '#169d8f',
                   ROLE_OPERBLOCK_EMERGENCY: '#8053bb', ROLE_OPERBLOCK_PLANNED: '#687783'}
        self._accent = QColor(accents[self.role_key] if mode == 'light' else self._dark_accent)
        self._photo = self._photos[mode]
        self.update()

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

        light_mode = self._theme_mode == 'light'
        fill = QColor('#f5f7fb') if light_mode else QColor(self._accent).darker(340)
        fill.setAlpha(245)
        border = QColor(self._accent)
        border.setAlpha(255 if focused else int(205 + hovered * 50))
        if hovered or focused:
            for spread in (6, 4, 2):
                glow = QColor(self._accent)
                glow.setAlpha(int((14 + hovered * 16) / (spread / 2)))
                painter.setPen(QPen(glow, spread * 2))
                painter.setBrush(Qt.NoBrush)
                painter.drawRoundedRect(rect, 14, 14)
        if not self.isEnabled():
            fill = QColor(237, 240, 244, 190) if light_mode else QColor(8, 29, 49, 150)
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
        if not self._photo.isNull():
            painter.save()
            clip = QPainterPath()
            clip.addRoundedRect(rect, 18, 18)
            painter.setClipPath(clip)
            painter.setOpacity((.92 if light_mode else .68) if self.isEnabled() else .35)
            source = QRectF(self._photo.rect())
            ratio = rect.width() / rect.height()
            if source.width() / source.height() > ratio:
                source.setLeft((source.width() - source.height() * ratio) / 2)
                source.setWidth(source.height() * ratio)
            else:
                source.setHeight(source.width() / ratio)
            painter.drawPixmap(rect, self._photo, source)
            overlay = QLinearGradient(rect.topLeft(), rect.bottomLeft())
            overlay.setColorAt(0, QColor(4, 22, 41, 0))
            overlay.setColorAt(.5, QColor(fill.red(), fill.green(), fill.blue(), 100))
            overlay.setColorAt(1, QColor(fill.red(), fill.green(), fill.blue(), 235))
            painter.fillRect(rect, overlay)
            painter.restore()
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(border, 1.4 + hovered))
        painter.drawRoundedRect(rect, 18, 18)

        inset = max(22, min(48, rect.width() * .125))
        title_rect = QRectF(rect.left() + inset, rect.top() + rect.height() * .46, rect.width() - inset - 16, 35)
        title_font = QFont("Segoe UI", -1, QFont.DemiBold)
        title_font.setPixelSize(24)
        while QFontMetricsF(title_font).horizontalAdvance(self.title) > title_rect.width() and title_font.pixelSize() > 15:
            title_font.setPixelSize(title_font.pixelSize()-1)
        painter.setFont(title_font)
        painter.setPen(QColor('#101543' if light_mode else '#f3fbff') if self.isEnabled() else QColor('#667789' if light_mode else '#a2b4c0'))
        painter.drawText(title_rect, Qt.AlignLeft | Qt.AlignVCenter | Qt.TextSingleLine, self.title)

        description_rect = QRectF(title_rect.left(), title_rect.bottom() + 3, title_rect.width(), rect.bottom()-title_rect.bottom()-64)
        description_font = QFont('Segoe UI')
        description_font.setPixelSize(16 if rect.width() >= 295 else 14)
        painter.setFont(description_font)
        painter.setPen(QColor('#345372' if light_mode else '#b9d3e3') if self.isEnabled() else QColor('#708090' if light_mode else '#8496a3'))
        painter.drawText(
            description_rect,
            Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap,
            self.description,
        )
        arrow_rect = QRectF(title_rect.left(), rect.bottom() - 58, 44, 44)
        pulse(painter, QRectF(arrow_rect.right() + 24, rect.bottom() - 66,
                             max(15, rect.right() - arrow_rect.right() - 42), 48), self._accent)
        arrow_fill = QLinearGradient(arrow_rect.topLeft(), arrow_rect.bottomRight())
        arrow_fill.setColorAt(0, self._accent.lighter(115) if light_mode else self._accent.darker(115))
        arrow_fill.setColorAt(1, self._accent if light_mode else self._accent.darker(220))
        painter.setPen(QPen(self._accent.lighter(165), 1))
        painter.setBrush(arrow_fill)
        painter.drawEllipse(arrow_rect)
        cx, cy = arrow_rect.center().x(), arrow_rect.center().y()
        painter.setPen(QPen(QColor('#ffffff'), 2.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        if self.property('preparing'):
            painter.setBrush(Qt.NoBrush)
            painter.drawArc(arrow_rect.adjusted(10, 10, -10, -10), int(self.property('phase') or 0) * 16, 250 * 16)
            return
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
            painter.scale(self.width() / 26, self.height() / 26)
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
        self._theme_mode = 'dark'
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
        self._glow = QGraphicsDropShadowEffect(self.marker)
        self._glow.setOffset(0, 0)
        self._glow.setBlurRadius(18)
        self._glow.setColor(QColor('#35cfff'))
        self.marker.setGraphicsEffect(self._glow)
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
        self._glow.setEnabled(state == self.ACTIVE)
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
            self.PENDING: ("transparent", "#78a0bb", "#b7cddd"),
            self.ACTIVE: ("#087bb0", "#65d4ff", "#f2fbff"),
            self.COMPLETE: ("#16755f", "#62e1be", "#e9fff8"),
            self.ERROR: ("#8d3944", "#ff9ea8", "#fff3f4"),
        }[state]
        if self._theme_mode == 'light' and state == self.PENDING:
            colors = ('transparent', '#8d99a8', '#8d99a8')
        self.marker.setStyleSheet(
            f"background: {colors[0]}; border: 1px solid {colors[1]}; border-radius: {self.marker.width() // 2}px; "
            f"color: {colors[2]}; font: 700 13px 'Segoe UI';"
        )
        label_color = ('#a02b40' if state == self.ERROR else '#244467') if self._theme_mode == 'light' else colors[2]
        self.label.setStyleSheet(f"color: {label_color}; font: {getattr(self, '_font_size', 18)}px 'Segoe UI';")

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setPen(QPen(QColor('#7894ad' if self._theme_mode == 'light' else '#427fae'), 1, Qt.DashLine))
        x = self.marker.geometry().center().x()
        painter.drawLine(x, 0, x, self.marker.y() - 3)
        painter.drawLine(x, self.marker.geometry().bottom() + 3, x, self.height())


class _EntryPageBase(_EntryBackdrop):
    """Shared transparent content and top/floor treatment for entry pages."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._theme_mode = 'dark'
        self._backdrops = {}
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

    def _refresh_theme_labels(self):
        for widget in self.findChildren(QLabel) + self.findChildren(QPushButton):
            if widget.objectName().startswith('UnifiedStartupStage'):
                continue
            if not hasattr(widget, '_entry_dark_style'):
                widget._entry_dark_style = widget.styleSheet()
            widget.setStyleSheet(_entry_style(widget._entry_dark_style, self._theme_mode))
            widget.setProperty('entry_theme', self._theme_mode)
        for mark in self.findChildren(HeartMark) + self.findChildren(PulseLine):
            mark.setProperty('entry_theme', self._theme_mode)
            mark.update()

    def set_theme(self, mode):
        self._theme_mode = 'light' if mode == 'light' else 'dark'
        key = (self._theme_mode, getattr(self, '_welcome_art', False))
        if key not in self._backdrops:
            name = ('unified_welcome_background_light_v3.png' if self._theme_mode == 'light' else
                    ('unified_welcome_background_v3.png' if key[1] else _BACKGROUND_PATH.name))
            self._backdrops[key] = QPixmap(str(_ICON_DIR / name))
        self._background = self._backdrops[key]
        self._refresh_theme_labels()
        for row in self.findChildren(_StageRow):
            row._theme_mode = self._theme_mode
            row.set_state(row.state)
        self.update()

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
    theme_changed = Signal(str)
    role_selected = Signal(str)
    settings_requested = Signal()
    about_requested = Signal()
    update_requested = Signal()
    cancel_entry_requested = Signal()

    _ROLE_DETAILS = (
        (ROLE_DOCTOR, "Врач", "Ведение пациентов,\nназначения, процедуры,\nанализы и отчёты", "#35b9ff"),
        (ROLE_NURSE, "Медсестра", "Выполнение назначений,\nмониторинг и учёт\nманипуляций", "#5ee3d4"),
        (ROLE_OPERBLOCK_PLANNED, "Плановый оперблок", "Плановые вмешательства,\nанестезия и\nоперационные карты", "#94bde5"),
        (ROLE_OPERBLOCK_EMERGENCY, "Экстренный оперблок", "Неотложные вмешательства,\nанестезия и\nоперационные карты", "#b793ff"),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._welcome_art = True
        self._background = QPixmap(str(_ICON_DIR / 'unified_welcome_background_v3.png'))
        self._access_blocked = False
        self._access_message = ""
        self._preparing_role = ''
        self._preparing_message = ''
        self._compact_grid = None
        self._compact_type = None
        self._window_transition_active = False
        self._build_ui()
        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(1000)
        self._clock_timer.timeout.connect(self._refresh_clock)
        self._clock_timer.start()
        self._refresh_clock()
        self._update_role_grid()
        self._preparing_timer = QTimer(self)
        self._preparing_timer.setInterval(60)
        self._preparing_timer.timeout.connect(self._advance_preparing)
        self.theme_switch.mode_changed.connect(self.set_theme)
        self.set_theme(self.theme_switch.mode)

    def set_theme(self, mode):
        super().set_theme(mode)
        for card in self._role_cards:
            card.set_theme(self._theme_mode)
        color = '#142647' if self._theme_mode == 'light' else '#e5f4ff'
        self.about_button.setIcon(entry_action_icon('about', color))
        self.clock_icon.setPixmap(entry_action_icon('clock', color).pixmap(24, 24))
        self.theme_changed.emit(self._theme_mode)

    def _advance_preparing(self):
        card = self.role_buttons.get(self._preparing_role)
        if card:
            card.setProperty('phase', (int(card.property('phase') or 0) + 24) % 360)
            card.update()

    def set_preparing(self, role='', message=''):
        self._preparing_role = role
        self.cancel_entry_button.setVisible(bool(role))
        self.cancel_entry_button.setEnabled(bool(role))
        self._preparing_message = message or 'Подготовка рабочего места…'
        for key, card in self.role_buttons.items():
            card.setProperty('preparing', key == role)
            card.update()
        if role:
            self._preparing_timer.start()
        else:
            self._preparing_timer.stop()
            self.end_window_transition()
        for button in (self.theme_switch, self.about_button, self.update_button):
            button.setEnabled(not bool(role))
        self.theme_switch.setEnabled(not bool(role) and self.theme_switch.runtime_enabled)
        self.set_access_state(self._access_message, self._access_blocked)

    def _build_ui(self) -> None:
        header = QHBoxLayout()
        header.setSpacing(12)
        identity = QVBoxLayout()
        identity.setSpacing(8)
        brand_row = QHBoxLayout()
        brand_row.setSpacing(18)
        self.logo_label = HeartMark(self.content)
        self.logo_label.setFixedSize(74, 74)
        brand_row.addWidget(self.logo_label)
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
        brand_row.addLayout(brand_box)
        brand_row.addStretch(1)
        identity.addLayout(brand_row)
        self.tagline = QLabel('Больше, чем карта.\nПоддержка там, где это важно.', self.content)
        self.tagline.setStyleSheet("color: #9dd5f5; font: 15px 'Segoe UI';")
        identity.addWidget(self.tagline)
        header.addLayout(identity, 1)

        self.hospital_label = _InstitutionLabel(self.content)
        self.hospital_label.setStyleSheet("color: #e1f5ff; font: 16px 'Segoe UI';")
        self.hospital_label.setWordWrap(False)
        self.hospital_label.setMaximumWidth(380)
        self.context_label = QLabel("ОАРИТ", self.content)
        self.context_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.context_label.setStyleSheet("color: #e1f5ff; font: 16px 'Segoe UI';")
        self.clock_label = QLabel(self.content)
        self.clock_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.clock_label.setMinimumWidth(138)
        self.clock_label.setStyleSheet("color: #e1f5ff; font: 16px 'Segoe UI';")
        metadata = QHBoxLayout()
        metadata.setSpacing(14)

        def separator():
            line = QFrame(self.content)
            line.setFixedSize(1, 20)
            line.setStyleSheet('background: #7ba4c4; border: none;')
            return line

        self.hospital_separator = separator()
        metadata.addWidget(self.hospital_label)
        metadata.addWidget(self.hospital_separator)
        metadata.addWidget(self.context_label)
        metadata.addWidget(separator())
        clock = QLabel(self.content)
        self.clock_icon = clock
        clock.setPixmap(entry_action_icon('clock').pixmap(24, 24))
        clock.setFixedSize(24, 24)
        clock_row = QHBoxLayout()
        clock_row.setSpacing(9)
        clock_row.addWidget(clock)
        clock_row.addWidget(self.clock_label)
        metadata.addLayout(clock_row)
        context = QVBoxLayout()
        context.setSpacing(6)
        context.addLayout(metadata)
        self.signature = QLabel('Люди. Знания. Жизнь.', self.content)
        self.signature.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.signature.setStyleSheet("color: #9bddff; font: italic 26px 'Segoe Script';")
        context.addWidget(self.signature)
        context.addWidget(PulseLine(self.content))
        context.addStretch(1)
        header.addLayout(context)
        self.root_layout.addLayout(header)

        self.institution_label = QLabel(self.content)
        self.institution_label.setObjectName("UnifiedEntryInstitution")
        self.institution_label.setStyleSheet("color: #c4dce9; font: 11px 'Segoe UI';")
        self.institution_label.setWordWrap(True)
        self.root_layout.addWidget(self.institution_label)

        self.root_layout.addSpacing(4)
        heading = QLabel("Добро пожаловать", self.content)
        self.heading = heading
        heading.setAlignment(Qt.AlignCenter)
        heading.setStyleSheet("color: #f7fcff; font: 600 38px 'Segoe UI';")
        self.root_layout.addWidget(heading)
        helper = QLabel(
            "Единое приложение для всех специалистов отделения реанимации.\n"
            "Выберите свою роль, чтобы продолжить работу.",
            self.content,
        )
        self.helper = helper
        helper.setAlignment(Qt.AlignCenter)
        helper.setStyleSheet("color: #badff9; font: 18px 'Segoe UI';")
        self.root_layout.addWidget(helper)

        self.root_layout.addSpacing(18)
        self.roles_area = QWidget(self.content)
        self.roles_area.setMinimumHeight(360)
        self.roles_grid = QGridLayout(self.roles_area)
        self.roles_grid.setContentsMargins(0, 0, 0, 0)
        self.roles_grid.setHorizontalSpacing(2)
        self.roles_grid.setVerticalSpacing(14)
        self.role_buttons: dict[str, _RoleCard] = {}
        self._role_cards: list[_RoleCard] = []
        for role_key, title, description, accent in self._ROLE_DETAILS:
            card = _RoleCard(role_key, title, description, accent, self.roles_area)
            card.clicked.connect(lambda _checked=False, key=role_key: self.role_selected.emit(key))
            self.role_buttons[role_key] = card
            self._role_cards.append(card)
        self.root_layout.addWidget(self.roles_area, 5)

        self.cancel_entry_button = self._footer_button("Отменить вход")
        self.cancel_entry_button.setVisible(False)
        self.cancel_entry_button.clicked.connect(self.cancel_entry_requested)
        self.root_layout.addWidget(self.cancel_entry_button, 0, Qt.AlignCenter)

        self.access_label = QLabel(self.content)
        self.access_label.setObjectName("UnifiedEntryAccessState")
        self.access_label.setAlignment(Qt.AlignCenter)
        self.access_label.setWordWrap(True)
        self.access_label.setFixedHeight(40)
        self.root_layout.addSpacing(6)

        footer = QGridLayout()
        footer.setContentsMargins(0, 14, 0, 10)
        footer.setHorizontalSpacing(12)
        self.care_label = QLabel('INTENSIVE CARE\nCRITICAL\nLIFE SUPPORT', self.content)
        self.care_label.setStyleSheet("color: #65a6cb; font: 13px 'Segoe UI'; letter-spacing: 2px; "
                                     "border-left: 1px solid #3598d2; padding-left: 15px;")
        self.care_label.setFixedHeight(60)
        footer.addWidget(self.care_label, 0, 0, 2, 1, Qt.AlignLeft | Qt.AlignBottom)
        footer.setRowMinimumHeight(0, 60)
        self.theme_switch = ThemeSwitch(self.content, load_saved_mode=True)
        # Reuse the settings control and its shared manager/persistence exactly.
        self.theme_switch.setFixedSize(144, 57)
        # Keep the quote and decorative footer in place; actions live in the
        # separate bottom row alongside the reserved status area.
        footer.setRowMinimumHeight(1, 57)
        quote_box = QVBoxLayout()
        quote_box.setContentsMargins(0, 16, 0, 8)
        quote_box.setSpacing(16)
        quote_box.addStretch(1)
        accent = QFrame(self.content)
        accent.setFixedSize(64, 3)
        accent.setStyleSheet('border-radius: 1px; background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #59dcfa, stop:1 #b475e3);')
        quote_box.addWidget(accent, 0, Qt.AlignHCenter)
        self.quote_label = QLabel('«Медицина — это не только наука. Это структура, команда и люди.»', self.content)
        self.quote_label.setWordWrap(True)
        self.quote_label.setAlignment(Qt.AlignCenter)
        self.quote_label.setStyleSheet("color: #bce7ff; font: italic 16px 'Segoe UI';")
        quote_box.addWidget(self.quote_label)
        self.quote_author = QLabel('М.И. Шевчук', self.content)
        self.quote_author.setAlignment(Qt.AlignCenter)
        self.quote_author.setStyleSheet("color: #93c9ef; font: italic 16px 'Segoe UI';")
        quote_box.addWidget(self.quote_author)
        quote_box.addStretch(1)
        footer.addLayout(quote_box, 0, 1, 2, 1)
        self.about_button = self._footer_button("О программе")
        self.about_button.setText("")
        self.about_button.setAccessibleName("О программе")
        self.about_button.setFixedSize(40, 40)
        self.about_button.setStyleSheet(self.about_button.styleSheet() + "QPushButton { padding: 0; }")
        self.update_button = self._footer_button("Обновить")
        self.update_button.hide()
        self.about_button.clicked.connect(self.about_requested)
        self.update_button.clicked.connect(self.update_requested)
        footer.setColumnStretch(0, 1)
        footer.setColumnStretch(1, 4)
        footer.setColumnStretch(2, 1)
        footer.setRowStretch(0, 1)
        self.root_layout.addLayout(footer, 3)
        bottom_actions = QGridLayout()
        bottom_actions.setContentsMargins(0, 0, 0, 0)
        bottom_actions.setHorizontalSpacing(12)
        bottom_actions.addWidget(self.theme_switch, 0, 0, Qt.AlignLeft | Qt.AlignBottom)
        bottom_actions.addWidget(self.access_label, 0, 1, Qt.AlignCenter)
        bottom_actions.addWidget(self.about_button, 0, 2, Qt.AlignRight | Qt.AlignVCenter)
        bottom_actions.addWidget(self.update_button, 0, 2, Qt.AlignRight | Qt.AlignVCenter)
        bottom_actions.setColumnStretch(0, 1)
        bottom_actions.setColumnStretch(1, 4)
        bottom_actions.setColumnStretch(2, 1)
        self.root_layout.addLayout(bottom_actions)
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
            card.setEnabled(not (self._access_blocked or self._preparing_role))
        self.access_label.setText(self._preparing_message if self._preparing_role else self._access_message)
        if self._preparing_role:
            self.access_label.setStyleSheet("color: #cfefff; font: 600 14px 'Segoe UI';")
        elif self._access_blocked:
            self.access_label.setStyleSheet("color: #ffc2c8; font: 600 12px 'Segoe UI';")
        else:
            self.access_label.setStyleSheet("color: #91dabc; font: 600 12px 'Segoe UI';")
        self.access_label._entry_dark_style = self.access_label.styleSheet()
        self.access_label.setStyleSheet(_entry_style(self.access_label._entry_dark_style, self._theme_mode))
        # Reserve the status strip even while empty so the quote stays still.
        self.access_label.setVisible(True)

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
        self.roles_area.setMaximumHeight(460)
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
        QWidget.resizeEvent(self, event)
        self._apply_responsive_layout()

    def begin_window_transition(self, target_width=None):
        if target_width is not None:
            self._window_transition_active = False
            self._apply_responsive_layout(target_width)
        self._window_transition_active = True

    def end_window_transition(self):
        # The resize can finish before the first clinical data arrives. Keep
        # the visible chooser's margins/type sizes stable throughout that wait.
        if self._preparing_role and self.isVisible():
            return
        self._window_transition_active = False
        self._apply_responsive_layout()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._window_transition_active = False
        self._apply_responsive_layout()

    def _apply_responsive_layout(self, width=None):
        if self._window_transition_active:
            return
        width = self.width() if width is None else width
        margin = max(28, min(54, round(width * .03)))
        compact = width < 1300
        self.root_layout.setContentsMargins(margin, 14 if compact else 18, margin, 6)
        if compact != self._compact_type:
            self._compact_type = compact
            side = 60 if compact else 74
            self.logo_label.setFixedSize(side, side)
            self.heading.setStyleSheet(f"color: #f7fcff; font: 600 {32 if compact else 38}px 'Segoe UI';")
            self.helper.setStyleSheet(f"color: #badff9; font: {16 if compact else 18}px 'Segoe UI';")
            self.signature.setStyleSheet(f"color: #9bddff; font: italic {22 if compact else 26}px 'Segoe Script';")
            for label in (self.heading, self.helper, self.signature):
                label._entry_dark_style = label.styleSheet()
            self._refresh_theme_labels()
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
        self.set_theme('light')

    def _build_ui(self) -> None:
        self.startup_logo_label = HeartMark(self.content)
        self.title_label = QLabel("РЕМКАРТА", self.content)
        self.subtitle_label = QLabel("Реанимационная карта", self.content)
        self.loading_title = QLabel("Загрузка системы…", self.content)
        self.loading_title.setAlignment(Qt.AlignCenter)
        self.progress_bar = QProgressBar(self.content)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        self.progress_bar.setAlignment(Qt.AlignCenter)
        glow = QGraphicsDropShadowEffect(self.progress_bar)
        glow.setOffset(0, 0)
        glow.setBlurRadius(22)
        glow.setColor(QColor('#41d4ff'))
        self.progress_bar.setGraphicsEffect(glow)
        self.stage_rows = [_StageRow(i + 1, title, self.content)
                           for i, title in enumerate(self.STAGES)]
        self.message_label = QLabel("Ожидание запуска", self.content)
        self.message_label.setWordWrap(True)
        self.error_label = QLabel(self.content)
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        self.institution_label = QLabel(self.content)
        self.institution_label.setAlignment(Qt.AlignRight | Qt.AlignBottom)
        self.institution_label.setWordWrap(True)
        self.care_label = QLabel("З А Б О Т А\n\nТ Е Х Н О Л О Г И И\n\nЖ И З Н Ь", self.content)
        self.solutions_label = QLabel("С О В Р Е М Е Н Н Ы Е   Р Е Ш Е Н И Я\nД Л Я   К Р И Т И Ч Е С К И Х   С И Т У А Ц И Й", self.content)
        self._refresh_institution_labels()
        self._layout_startup()

    def set_theme(self, mode):
        super().set_theme(mode)
        if self._theme_mode == 'light':
            name = _ICON_DIR / 'unified_startup_background_light_v4.png'
            if name.is_file():
                if not hasattr(self, '_light_startup_background'):
                    self._light_startup_background = QPixmap(str(name))
                self._background = self._light_startup_background
        self._layout_startup()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'loading_title'):
            self._layout_startup()

    def _layout_startup(self):
        w, h = self.width(), self.height()
        scale = min(w / 1536, h / 984)
        left = (w - 620 * scale) / 2
        top = h * .145
        light = self._theme_mode == 'light'
        primary = '#101b46' if light else '#f1f7ff'
        secondary = '#495e7b' if light else '#a8d2f2'
        accent = '#007ed5' if light else '#69dfff'
        def place(widget, x, y, width, height):
            widget.setGeometry(round(x), round(y), round(width), round(height))
        def font(widget, size, color, weight=400):
            widget.setStyleSheet(f"color:{color}; background:transparent; font:{weight} {max(10, round(size * scale))}px 'Segoe UI';")
        place(self.startup_logo_label, left, top, 154*scale, 154*scale)
        place(self.title_label, left+176*scale, top+12*scale, 470*scale, 78*scale)
        place(self.subtitle_label, left+176*scale, top+92*scale, 470*scale, 44*scale)
        font(self.title_label, 60, primary, 700)
        font(self.subtitle_label, 30, accent)
        place(self.loading_title, left, top+174*scale, 620*scale, 48*scale)
        font(self.loading_title, 30, primary, 600)
        place(self.progress_bar, left, top+236*scale, 620*scale, 40*scale)
        radius = max(8, round(20*scale))
        self.progress_bar.setStyleSheet(
            f"QProgressBar {{ color:{primary}; background:{'rgba(240,250,255,185)' if light else 'rgba(0,31,68,185)'}; "
            f"border:2px solid #39ceff; border-radius:{radius}px; font:{round(18*scale)}px 'Segoe UI'; }}"
            f"QProgressBar::chunk {{ border-radius:{max(6,radius-2)}px; background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #9beeff,stop:1 #159de9); }}")
        for index, row in enumerate(self.stage_rows):
            row.setFixedHeight(round(64*scale))
            place(row, left+12*scale, top+(300+64*index)*scale, 600*scale, 64*scale)
            row.marker.setFixedSize(round(36*scale), round(36*scale))
            row.layout().setSpacing(round(24*scale))
            row._font_size = max(13, round(21*scale))
            row.set_state(row.state)
            row.label.setStyleSheet(f"color:{primary if row.state == row.ACTIVE else ('#b73349' if row.state == row.ERROR else secondary)}; font:{max(13,round(21*scale))}px 'Segoe UI';")
        place(self.message_label, left+16*scale, top+636*scale, 600*scale, 34*scale)
        font(self.message_label, 15, secondary)
        place(self.error_label, left+16*scale, top+674*scale, 620*scale, 58*scale)
        font(self.error_label, 15, '#b73349' if light else '#ffbbc3')
        place(self.care_label, w*.76, h*.475, w*.20, 120*scale)
        font(self.care_label, 13, '#7894ad' if light else '#427fae')
        place(self.solutions_label, w*.032, h-86*scale, w*.40, 56*scale)
        font(self.solutions_label, 12, '#265c91' if light else '#659bca')
        place(self.institution_label, w*.43, h-76*scale, w*.54, 56*scale)
        font(self.institution_label, 17, primary)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.scale(self.width()/1536, self.height()/984)
        light = self._theme_mode == 'light'
        painter.setPen(QPen(QColor(255,255,255,185) if light else QColor(19,124,192,95), 1.2))
        painter.setBrush(Qt.NoBrush)
        for points in (
            ((240,340),(240,180),(285,122),(308,120),(374,45),(920,45),(968,0)),
            ((780,557),(1020,557),(1155,425),(1230,425),(1340,310)),
            ((0,830),(340,830),(395,885),(1028,885),(1136,790),(1190,790),(1270,710)),
        ):
            path = QPainterPath(QPointF(*points[0]))
            for point in points[1:]:
                path.lineTo(*point)
            painter.drawPath(path)
        pulse(painter, QRectF(970,190,370,150), QColor('#51d0ff' if light else '#117eb8'))
        painter.setPen(QPen(QColor('#388fc4'), 1))
        painter.drawLine(QPointF(50,944), QPointF(120,944))

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
