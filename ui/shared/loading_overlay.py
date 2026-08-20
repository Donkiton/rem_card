import os

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QMovie
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget


class LoadingOverlay(QWidget):
    """Small in-app loading overlay shown above the current window content."""

    def __init__(self, parent=None, gif_path: str | None = None):
        super().__init__(parent)
        self._movie = None
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

        self.spinner_label = QLabel(self.panel)
        self.spinner_label.setFixedSize(48, 48)
        self.spinner_label.setAlignment(Qt.AlignCenter)
        panel_layout.addWidget(self.spinner_label, 0, Qt.AlignVCenter)

        self.message_label = QLabel("Загрузка...", self.panel)
        self.message_label.setObjectName("LoadingOverlayMessage")
        self.message_label.setWordWrap(True)
        panel_layout.addWidget(self.message_label, 1, Qt.AlignVCenter)

        root_layout.addWidget(self.panel, 0, Qt.AlignCenter)

        if gif_path and os.path.exists(gif_path):
            self._movie = QMovie(gif_path)
            self._movie.setScaledSize(QSize(48, 48))
            self.spinner_label.setMovie(self._movie)
        else:
            self.spinner_label.setText("...")

        self.setStyleSheet(
            """
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
            """
        )

    def show_loading(self, message: str = "Загрузка..."):
        self.message_label.setText(str(message or "Загрузка..."))
        parent = self.parentWidget()
        if parent is not None:
            self.setGeometry(parent.rect())
        self.show()
        self.raise_()
        if self._movie is not None:
            self._movie.start()

    def hide_loading(self):
        if self._movie is not None:
            self._movie.stop()
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
