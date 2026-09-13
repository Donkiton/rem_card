"""Компактный доступный переключатель светлой и тёмной темы."""

from __future__ import annotations

import os

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QSizePolicy, QMessageBox


FULL_RUNTIME_THEME_ENV = "REMCARD_FULL_RUNTIME_THEME"

# Палитра намеренно находится рядом с отрисовкой переключателя: он не зависит
# от разбора QSS и остаётся читаемым до применения глобальной темы приложения.
LIGHT_COLORS = {
    "track": "#e9ecef",
    "surface": "#ffffff",
    "border": "#bdc3c7",
    "accent": "#2f6fa3",
    "text": "#243447",
}
DARK_COLORS = {
    "track": "#19232f",
    "surface": "#222e3c",
    "border": "#485a70",
    "accent": "#8ab8ff",
    "text": "#d8e4f3",
}


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

    def __init__(self, parent=None, *, manager=None):
        super().__init__(parent)
        self.setObjectName("theme_mode_switch")
        self.setCheckable(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setFixedHeight(32)
        self.setMinimumWidth(128)
        self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName("Переключатель темы: Светлая / Тёмная")
        self.setAccessibleDescription("Пробел переключает светлую и тёмную тему")
        self.setToolTip("Светлая / Тёмная тема")

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

    def sizeHint(self) -> QSize:
        return QSize(132, 32)

    def minimumSizeHint(self) -> QSize:
        return QSize(128, 32)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        del event
        colors = DARK_COLORS if self._mode == "dark" else LIGHT_COLORS
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        bounds = self.rect().adjusted(1, 1, -1, -1)
        radius = bounds.height() / 2
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(colors["track"]))
        painter.drawRoundedRect(bounds, radius, radius)
        inner_bounds = bounds.adjusted(1, 1, -1, -1)
        painter.setBrush(QColor(colors["surface"]))
        painter.drawRoundedRect(inner_bounds, max(0, radius - 1), max(0, radius - 1))
        painter.setPen(QPen(QColor(colors["border"]), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(bounds, radius, radius)

        half = bounds.width() / 2
        thumb = bounds.adjusted(2, 2, -2, -2)
        if self._mode == "dark":
            thumb.setLeft(int(bounds.left() + half + 1))
        else:
            thumb.setRight(int(bounds.left() + half - 1))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(colors["accent"]))
        painter.drawRoundedRect(thumb, thumb.height() / 2, thumb.height() / 2)

        painter.setFont(self.font())
        metrics = QFontMetrics(self.font())
        labels = (("Светлая", bounds.left(), int(bounds.left() + half)), ("Тёмная", int(bounds.left() + half), bounds.right()))
        for label, left, right in labels:
            selected = (label == "Тёмная") == (self._mode == "dark")
            color = colors["track"] if selected else colors["text"]
            painter.setPen(QColor(color))
            text_rect = bounds.adjusted(0, 0, 0, 0)
            text_rect.setLeft(left)
            text_rect.setRight(right)
            painter.drawText(text_rect, Qt.AlignCenter, metrics.elidedText(label, Qt.ElideRight, max(1, right - left - 6)))

        if self.hasFocus():
            focus_color = colors["accent"]
            painter.setPen(QPen(QColor(focus_color), 2))
            painter.setBrush(Qt.NoBrush)
            focus_bounds = self.rect().adjusted(0, 0, -1, -1)
            painter.drawRoundedRect(focus_bounds, radius + 1, radius + 1)

        if not self.isEnabled():
            painter.fillRect(self.rect(), QColor(255, 255, 255, 95))
        painter.end()


# Descriptive aliases keep imports stable if a caller prefers button wording.
ThemeSwitchButton = ThemeSwitch
ThemeModeSwitch = ThemeSwitch
