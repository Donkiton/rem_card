"""Animate live Qt widgets at their current size, without scaling screenshots."""
from PySide6.QtCore import QObject, QRect, Qt, QPropertyAnimation, QEasingCurve


class WindowTransition(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.animation = None

    @property
    def running(self):
        return self.animation is not None

    def cancel(self):
        if self.animation:
            self.animation.stop()
            self.animation.deleteLater()
            self.animation = None

    def start(self, target, prepare, finished, *, maximized=False, normal_rect=None, prepare_before_resize=False):
        self.cancel()
        window = self.window
        start = QRect(window.geometry())
        window.setUpdatesEnabled(False)
        try:
            # Keep the visible rectangle when leaving native maximized state.
            window.setWindowState(window.windowState() & ~Qt.WindowMaximized & ~Qt.WindowMinimized)
            window._is_custom_maximized = False
            window.setGeometry(start)
            if prepare_before_resize:
                # Returning to the chooser can resize its lightweight widgets;
                # the clinical page stays hidden and does not relayout per frame.
                prepare()
        finally:
            window.setUpdatesEnabled(True)
        animation = QPropertyAnimation(window, b'geometry', self)
        self.animation = animation
        # Preserve the previous total duration (120 ms resize + 40 ms fade).
        # Qt lays out and paints text/icons at native resolution on every frame.
        animation.setDuration(160)
        animation.setEasingCurve(QEasingCurve.InOutCubic)
        animation.setStartValue(start)
        animation.setEndValue(target)

        def complete():
            window.setGeometry(target)
            window._is_custom_maximized = maximized
            if maximized and normal_rect is not None:
                window._custom_normal_geometry = QRect(normal_rect)
            if not prepare_before_resize:
                prepare()
            window.entry_chrome.layout().activate()
            window.entry_chrome.content.layout().activate()
            window.entry_chrome._update_masks()
            self.cancel()
            finished()

        animation.finished.connect(complete)
        animation.start()
