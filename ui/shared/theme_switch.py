"""Компактный доступный переключатель светлой и тёмной темы."""

from __future__ import annotations

import os
import math

from PySide6.QtCore import QEasingCurve, QPointF, QRectF, QSize, Qt, Signal, QVariantAnimation
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient
from PySide6.QtWidgets import QAbstractButton, QSizePolicy, QMessageBox, QStyle


FULL_RUNTIME_THEME_ENV = "REMCARD_FULL_RUNTIME_THEME"

# Палитра намеренно находится рядом с отрисовкой переключателя: он не зависит
# от разбора QSS и остаётся читаемым до применения глобальной темы приложения.
def _blend(light: str, dark: str, progress: float) -> QColor:
    first, last = QColor(light), QColor(dark)
    return QColor.fromRgbF(*(
        a + (b - a) * progress
        for a, b in zip(first.getRgbF(), last.getRgbF())
    ))


def runtime_theme_enabled() -> bool:
    """Возвращает состояние главного флага динамической темы."""

    # Runtime theme включён по умолчанию; явный ``=0`` остаётся безопасным
    # master-off для сборок и аварийного отката.
    return str(os.environ.get(FULL_RUNTIME_THEME_ENV, "1")).strip().lower() not in {"0", "false", "no", "off"}


def get_theme_manager():
    """Лениво получить единственный менеджер темы."""
    from rem_card.ui.styles.theme_manager import get_theme_manager as factory
    return factory()


def _manager_enabled(manager) -> bool:
    value = getattr(manager, "enabled", True)
    try:
        return bool(value() if callable(value) else value)
    except Exception:
        return True


def _manager_mode(manager) -> str:
    try:
        mode = str(getattr(manager, "mode", "light") or "light").strip().lower()
    except Exception:
        mode = "light"
    return "dark" if mode == "dark" else "light"


class ThemeSwitch(QAbstractButton):
    """Native Qt checkable button с двумя явно подписанными состояниями.

    Состояние ``checked`` соответствует тёмной теме. QAbstractButton сам
    обеспечивает стандартную обработку мыши, Tab и Space; собственная
    отрисовка нужна только для компактного двухпозиционного вида.
    """

    mode_changed = Signal(str)

    def __init__(self, parent=None, *, manager=None, load_saved_mode=False):
        super().__init__(parent)
        self.setObjectName("theme_mode_switch")
        self.setCheckable(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setFixedSize(self.sizeHint())
        self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName("Переключатель темы: Светлая / Тёмная")
        self.setAccessibleDescription("Пробел переключает светлую и тёмную тему")
        self.setToolTip("Светлая / Тёмная тема")

        self._position = 0.0
        self._animation = QVariantAnimation(self)
        self._animation.setEasingCurve(QEasingCurve.InOutCubic)
        self._animation.valueChanged.connect(self._set_position)
        self._manager = None
        self._mode = "light"
        self._runtime_enabled = runtime_theme_enabled()

        # При отключённом master-флаге менеджер не создаётся: создание кнопки
        # не должно инициировать чтение настроек или рабочей базы.
        if self._runtime_enabled:
            try:
                self._manager = manager if manager is not None else get_theme_manager()
                self._runtime_enabled = _manager_enabled(self._manager)
                if self._runtime_enabled:
                    if load_saved_mode:
                        # The chooser appears before any clinical role applies a theme.
                        self._manager.settings_for_role()
                    signal = getattr(self._manager, "theme_changed", None)
                    if signal is not None and hasattr(signal, "connect"):
                        signal.connect(self._on_theme_changed)
                    self._on_theme_changed(_manager_mode(self._manager))
            except Exception:
                self._manager = None
                self._runtime_enabled = False

        self.set_runtime_enabled(self._runtime_enabled)

        self.clicked.connect(self._on_clicked)

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def runtime_enabled(self) -> bool:
        return self._runtime_enabled

    def set_runtime_enabled(self, enabled: bool) -> None:
        self._runtime_enabled = bool(enabled)
        self.setEnabled(self._runtime_enabled)
        self.setVisible(self._runtime_enabled)
        self.update()

    def _on_theme_changed(self, mode: str) -> None:
        normalized = "dark" if str(mode or "").strip().lower() == "dark" else "light"
        changed = normalized != self._mode
        self._mode = normalized
        self.setChecked(normalized == "dark")
        target = float(normalized == "dark")
        # Повторный сигнал менеджера не перезапускает текущий переход.
        if changed or not self.isVisible():
            self._animation.stop()
            if self.isVisible() and self.style().styleHint(QStyle.SH_Widget_Animate, None, self):
                start = self._position
                self._animation.blockSignals(True)
                self._animation.setDuration(max(100, round(320 * abs(target - start))))
                self._animation.setStartValue(start)
                self._animation.setEndValue(target)
                self._animation.blockSignals(False)
                self._animation.start()
            else:
                self._set_position(target)
        # Названия обеих позиций остаются доступными скринридеру; выбранное
        # состояние сообщает стандартный checkable-интерфейс Qt.
        self.setAccessibleName("Переключатель темы: Светлая / Тёмная")
        self.setAccessibleDescription(
            f"Выбрана {'тёмная' if normalized == 'dark' else 'светлая'} тема. Пробел переключает тему"
        )
        self.update()
        if changed:
            self.mode_changed.emit(normalized)

    def _on_clicked(self, checked: bool) -> None:
        requested = "dark" if checked else "light"
        if self._manager is not None:
            try:
                self._manager.set_mode(requested, save=True)
            except Exception as exc:
                # Ошибка сохранения откатывает manager, ошибка применения может
                # возникнуть после сохранения. Показываем фактический режим.
                self._on_theme_changed(_manager_mode(self._manager))
                QMessageBox.warning(self, "Цветовая тема", f"Не удалось применить тему.\n{exc}")
                return
        self._on_theme_changed(requested)

    def _set_position(self, value) -> None:
        self._position = float(value)
        self.update()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._animation.stop()
        self._set_position(float(self._mode == "dark"))
        super().hideEvent(event)

    def sizeHint(self) -> QSize:
        return QSize(96, 38)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    @staticmethod
    def _sun(painter: QPainter, center: QPointF, radius: float) -> None:
        painter.setPen(QPen(QColor("#ffcc55"), 1.5, Qt.SolidLine, Qt.RoundCap))
        for index in range(12):
            angle = index * math.pi / 6
            painter.drawLine(
                center + QPointF(math.cos(angle), math.sin(angle)) * radius * .73,
                center + QPointF(math.cos(angle), math.sin(angle)) * radius,
            )
        glow = QRadialGradient(center - QPointF(radius * .22, radius * .25), radius)
        glow.setColorAt(0, QColor("#fff8b4"))
        glow.setColorAt(.5, QColor("#ffdc55"))
        glow.setColorAt(1, QColor("#eea125"))
        painter.setBrush(glow)
        painter.setPen(QPen(QColor("#ffe99c"), .8))
        painter.drawEllipse(center, radius * .55, radius * .55)

    @staticmethod
    def _moon(painter: QPainter, center: QPointF, radius: float) -> None:
        disc = QPainterPath()
        disc.addEllipse(center, radius, radius)
        cutout = QPainterPath()
        cutout.addEllipse(center + QPointF(-radius * .50, -radius * .28), radius * .91, radius * .91)
        gradient = QLinearGradient(center.x() - radius, center.y() - radius,
                                   center.x() + radius, center.y() + radius)
        gradient.setColorAt(0, QColor("#fff5c9"))
        gradient.setColorAt(.55, QColor("#fff6e4"))
        gradient.setColorAt(1, QColor("#a5afff"))
        painter.setPen(QPen(QColor("#fff3d8"), .6))
        painter.setBrush(gradient)
        painter.drawPath(disc.subtracted(cutout))

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        del event
        t = self._position
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        # Единая система координат сохраняет пропорции и ход анимации.
        painter.scale(self.width() / 160, self.height() / 63)
        track = QRectF(5, 5, 150, 52)
        edge = QLinearGradient(track.topLeft(), track.topRight())
        edge.setColorAt(0, QColor("#efbd60"))
        edge.setColorAt(.45, QColor("#e4dcc6"))
        edge.setColorAt(1, QColor("#719fff"))
        # Несколько тонких контуров вместо дорогого blur-эффекта.
        painter.setBrush(Qt.NoBrush)
        for width, alpha in ((8, 12), (5, 22), (3, 40)):
            painter.setPen(QPen(_blend("#efbf64", "#548cff", t), width))
            painter.setOpacity(alpha / 255)
            painter.drawRoundedRect(track, 26, 26)
        painter.setOpacity(1)
        sky = QLinearGradient(track.topLeft(), track.topRight())
        sky.setColorAt(0, _blend("#f6cf7d", "#705b3c", t))
        sky.setColorAt(.48, _blend("#526080", "#182b50", t))
        sky.setColorAt(1, QColor("#0a1940"))
        painter.setBrush(sky)
        painter.setPen(QPen(edge, 1.4))
        painter.drawRoundedRect(track, 26, 26)
        painter.setPen(QPen(QColor(255, 255, 255, 65), .7))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(track.adjusted(2, 2, -2, -2), 24, 24)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#ffe8b0"))
        for x, y, r in ((65, 17, 1), (83, 39, 1.3), (97, 15, .8), (73, 45, .7), (133, 18, .8)):
            painter.drawEllipse(QPointF(x, y), r, r)
        painter.setOpacity(.58 + .30 * t)
        self._sun(painter, QPointF(31, 31), 14)
        painter.setOpacity(.88 - .30 * t)
        self._moon(painter, QPointF(129, 31), 14)
        painter.setOpacity(1)
        center = QPointF(31 + 98 * t, 31)
        thumb = QRadialGradient(center - QPointF(9, 13), 51)
        thumb.setColorAt(0, _blend("#fffefa", "#487ed9", t))
        thumb.setColorAt(.55, _blend("#fff1cb", "#102b69", t))
        thumb.setColorAt(1, _blend("#f4ca7e", "#071638", t))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 8, 30, 65))
        painter.drawEllipse(center + QPointF(0, 2), 24, 24)
        painter.setBrush(thumb)
        painter.setPen(QPen(_blend("#fffdf0", "#b4dcff", t), 1.5))
        painter.drawEllipse(center, 23.5, 23.5)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(_blend("#ffda8c", "#468fff", t), .8))
        painter.drawEllipse(center, 21.8, 21.8)
        painter.setOpacity(1 - t)
        self._sun(painter, center, 17)
        painter.setOpacity(t)
        self._moon(painter, center + QPointF(2, 0), 15)
        painter.setOpacity(1)
        painter.end()


# Descriptive aliases keep imports stable if a caller prefers button wording.
ThemeSwitchButton = ThemeSwitch
ThemeModeSwitch = ThemeSwitch
