from __future__ import annotations

from PySide6.QtWidgets import (
    QApplication,
    QProxyStyle,
    QPushButton,
    QStyle,
    QStyleFactory,
    QTabBar,
    QToolButton,
)


_PROXY_ATTR = "_remcard_no_button_focus_rect_style"
_ORIGINAL_STYLE_KEY_ATTR = "_remcard_original_style_key"
_THEME_STYLE_KEY_ATTR = "_remcard_theme_style_key"
_THEME_BASE_STYLE_ATTR = "_remcard_theme_base_style"


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
    if target_app is None or getattr(target_app, _PROXY_ATTR, None) is not None:
        return

    style_key = str(target_app.style().objectName() or "").strip()
    base_style = QStyleFactory.create(style_key) if style_key else None
    style = NoButtonFocusRectStyle(base_style) if base_style is not None else NoButtonFocusRectStyle()
    setattr(target_app, _ORIGINAL_STYLE_KEY_ATTR, style_key)
    setattr(target_app, _THEME_STYLE_KEY_ATTR, style_key.lower())
    setattr(target_app, _THEME_BASE_STYLE_ATTR, base_style)
    setattr(target_app, _PROXY_ATTR, style)
    target_app.setStyle(style)


def apply_application_theme_style(app: QApplication | None, mode: str) -> None:
    """Use Fusion for dark colors and the original native style for light mode."""
    target_app = app or QApplication.instance()
    if target_app is None:
        return
    install_no_button_focus_rect_style(target_app)
    original_key = str(getattr(target_app, _ORIGINAL_STYLE_KEY_ATTR, "") or "").strip()
    requested_key = "Fusion" if str(mode).strip().lower() == "dark" else original_key
    normalized_key = requested_key.lower()
    if getattr(target_app, _THEME_STYLE_KEY_ATTR, None) == normalized_key:
        return

    base_style = QStyleFactory.create(requested_key) if requested_key else None
    proxy = getattr(target_app, _PROXY_ATTR)
    proxy.setBaseStyle(base_style)
    # PySide does not keep a Python ownership reference for setBaseStyle().
    # Without this reference the new base can be collected on function return
    # and QProxyStyle silently falls back to its previous native style.
    setattr(target_app, _THEME_BASE_STYLE_ATTR, base_style)
    setattr(target_app, _THEME_STYLE_KEY_ATTR, normalized_key)
