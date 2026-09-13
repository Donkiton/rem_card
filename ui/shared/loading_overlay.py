from rem_card.ui.styles.theme_runtime import set_widget_style, themed_qcolor
import math

from PySide6.QtCore import Qt, QPointF, QTimer
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget


class LoadingSpinner(QWidget):
    """Transparent, theme-aware dots; no animation work while hidden."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(48, 48)
        self.setAccessibleName("Загрузка")
        self._phase = 0
        self._timer = QTimer(self)
        self._timer.setInterval(80)
        self._timer.timeout.connect(self._advance)

    def _advance(self):
        self._phase = (self._phase + 1) % 10
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        color = themed_qcolor("#2563eb", "border.focus")
        for index in range(10):
            angle = index * math.tau / 10 - math.pi / 2
            color.setAlphaF(0.18 + 0.82 * ((index - self._phase) % 10) / 9)
            painter.setBrush(color)
            painter.drawEllipse(QPointF(24 + 15 * math.cos(angle), 24 + 15 * math.sin(angle)), 3, 3)


class LoadingOverlay(QWidget):
    """Small in-app loading overlay shown above the current window content."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("LoadingOverlay")
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.hide()

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setAlignment(Qt.AlignCenter)

        self.panel = QFrame(self)
        self.panel.setObjectName("LoadingOverlayPanel")
        self.panel.setMinimumWidth(270)
        self.panel.setMaximumWidth(420)

        panel_layout = QHBoxLayout(self.panel)
        panel_layout.setContentsMargins(18, 14, 20, 14)
        panel_layout.setSpacing(12)

        self.spinner_label = LoadingSpinner(self.panel)
        panel_layout.addWidget(self.spinner_label, 0, Qt.AlignVCenter)

        self.message_label = QLabel("Загрузка...", self.panel)
        self.message_label.setObjectName("LoadingOverlayMessage")
        self.message_label.setWordWrap(True)
        panel_layout.addWidget(self.message_label, 1, Qt.AlignVCenter)

        root_layout.addWidget(self.panel, 0, Qt.AlignCenter)

        set_widget_style(self, """
            QWidget#LoadingOverlay {
                background-color: rgba(248, 249, 250, 74);
            }
            QFrame#LoadingOverlayPanel {
                background-color: rgba(255, 255, 255, 238);
                border: 1px solid rgba(120, 135, 150, 150);
                border-radius: 8px;
            }
            QLabel#LoadingOverlayMessage {
                color: #263238;
                font-size: 14px;
                font-weight: 700;
            }
            """)

    def show_loading(self, message: str = "Загрузка..."):
        self.message_label.setText(str(message or "Загрузка..."))
        parent = self.parentWidget()
        if parent is not None:
            self.setGeometry(parent.rect())
        self.show()
        self.raise_()

    def hide_loading(self):
        self.hide()


def show_app_loading(
    widget,
    message: str = "Загрузка...",
    *,
    key: str | None = None,
    auto_hide_ms: int | None = None,
    process_events: bool = False,
) -> str | None:
    if widget is None:
        return None
    window = widget.window()
    show = getattr(window, "show_loading_indicator", None)
    if not callable(show):
        return None
    return show(
        message,
        key=key,
        auto_hide_ms=auto_hide_ms,
        process_events=process_events,
    )


def hide_app_loading(widget, key: str | None = None, *, delay_ms: int = 0) -> None:
    if widget is None:
        return
    window = widget.window()
    hide = getattr(window, "hide_loading_indicator", None)
    if callable(hide):
        hide(key, delay_ms=delay_ms)
