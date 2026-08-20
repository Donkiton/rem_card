from __future__ import annotations

from PySide6.QtCore import QObject, QPoint, QEvent, Qt
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QHelpEvent, QPalette
from PySide6.QtWidgets import QLabel, QToolTip, QWidget


TOOLTIP_BG = "#ffffff"
TOOLTIP_TEXT = "#000000"
TOOLTIP_BORDER = "#bdc3c7"


class ReadableTooltipFilter(QObject):
    def __init__(self, app):
        super().__init__(app)
        self._tooltip = QLabel()
        self._tooltip.setObjectName("remcard_readable_tooltip")
        self._tooltip.setWindowFlags(
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self._tooltip.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self._tooltip.setTextFormat(Qt.TextFormat.AutoText)
        self._tooltip.setWordWrap(False)
        self._tooltip.setMargin(0)
        self._tooltip.setAutoFillBackground(True)
        self._apply_style()

    def eventFilter(self, watched, event):
        event_type = event.type()
        if event_type == QEvent.Type.ToolTip and isinstance(watched, QWidget):
            text = watched.toolTip()
            if not text:
                self._hide()
                return False
            if isinstance(event, QHelpEvent):
                self._show(text, event.globalPos())
            else:
                self._show(text, QCursor.pos())
            return True

        if event_type in {
            QEvent.Type.Leave,
            QEvent.Type.Hide,
            QEvent.Type.MouseButtonPress,
            QEvent.Type.WindowDeactivate,
        }:
            self._hide()
        return False

    def refresh_style(self) -> None:
        self._apply_style()

    def _apply_style(self) -> None:
        palette = self._tooltip.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor(TOOLTIP_BG))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(TOOLTIP_TEXT))
        self._tooltip.setPalette(palette)
        self._tooltip.setStyleSheet(
            f"""
            QLabel#remcard_readable_tooltip {{
                background-color: {TOOLTIP_BG};
                color: {TOOLTIP_TEXT};
                border: 1px solid {TOOLTIP_BORDER};
                padding: 4px 6px;
                font-size: 13px;
                font-family: "Segoe UI", Roboto, Arial, sans-serif;
            }}
            """
        )

    def _show(self, text: str, global_pos: QPoint) -> None:
        QToolTip.hideText()
        self._tooltip.setText(text)
        self._tooltip.adjustSize()
        self._tooltip.move(self._bounded_pos(global_pos))
        self._tooltip.show()
        self._tooltip.raise_()

    def _hide(self) -> None:
        if self._tooltip.isVisible():
            self._tooltip.hide()

    def _bounded_pos(self, global_pos: QPoint) -> QPoint:
        pos = QPoint(global_pos.x() + 12, global_pos.y() + 18)
        screen = QGuiApplication.screenAt(global_pos) or QGuiApplication.primaryScreen()
        if screen is None:
            return pos

        available = screen.availableGeometry()
        width = self._tooltip.width()
        height = self._tooltip.height()
        if pos.x() + width > available.right():
            pos.setX(max(available.left(), global_pos.x() - width - 12))
        if pos.y() + height > available.bottom():
            pos.setY(max(available.top(), global_pos.y() - height - 12))
        return pos


def apply_tooltip_palette(app) -> None:
    if app is None:
        return
    palette = app.palette()
    for group in (
        QPalette.ColorGroup.Active,
        QPalette.ColorGroup.Inactive,
        QPalette.ColorGroup.Disabled,
    ):
        palette.setColor(group, QPalette.ColorRole.ToolTipBase, QColor(TOOLTIP_BG))
        palette.setColor(group, QPalette.ColorRole.ToolTipText, QColor(TOOLTIP_TEXT))
    app.setPalette(palette)
    QToolTip.setPalette(palette)

    tooltip_filter = getattr(app, "_remcard_readable_tooltip_filter", None)
    if tooltip_filter is None:
        tooltip_filter = ReadableTooltipFilter(app)
        app.installEventFilter(tooltip_filter)
        setattr(app, "_remcard_readable_tooltip_filter", tooltip_filter)
    else:
        tooltip_filter.refresh_style()
