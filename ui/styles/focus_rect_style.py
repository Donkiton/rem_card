from __future__ import annotations

from PySide6.QtWidgets import QApplication, QProxyStyle, QPushButton, QStyle, QTabBar, QToolButton


class NoButtonFocusRectStyle(QProxyStyle):
    """Suppress native focus rectangles on clickable button-like controls."""

    _TARGET_WIDGET_TYPES = (QPushButton, QToolButton, QTabBar)

    def drawPrimitive(self, element, option, painter, widget=None):
        if (
            element == QStyle.PrimitiveElement.PE_FrameFocusRect
            and isinstance(widget, self._TARGET_WIDGET_TYPES)
        ):
            return
        super().drawPrimitive(element, option, painter, widget)


def install_no_button_focus_rect_style(app: QApplication | None = None) -> None:
    target_app = app or QApplication.instance()
    if target_app is None or getattr(target_app, "_remcard_no_button_focus_rect_style", None) is not None:
        return

    style = NoButtonFocusRectStyle(target_app.style())
    target_app.setStyle(style)
    target_app._remcard_no_button_focus_rect_style = style
