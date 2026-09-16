"""Animate live Qt widgets at their current size, without scaling screenshots."""
from PySide6.QtCore import QObject, QRect, Qt, QPropertyAnimation, QEasingCurve, QTimer
from functools import wraps


def after_window_transition(method):
    """Delay optional UI prewarming while its top-level window is resizing."""
    @wraps(method)
    def run(widget):
        if getattr(widget, '_is_closing', False):
            return
        transition = getattr(widget.window(), '_transition', None)
        if transition is not None and transition.running:
            # Context-bound callback is discarded if the role is destroyed.
            QTimer.singleShot(80, widget, lambda: run(widget))
            return
        return method(widget)
    return run


class _FrameAnimation(QPropertyAnimation):
    """Qt's normal timed animation; painting remains coalesced by its event loop."""


class WindowTransition(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.animation = None
        self._entry_page = None

    @property
    def running(self):
        return self.animation is not None

    def cancel(self):
        if self.animation:
            self.animation.stop()
            self.animation.deleteLater()
            self.animation = None
        page, self._entry_page = self._entry_page, None
        if page is not None:
            page.end_window_transition()
        self.window.entry_chrome.set_transition_active(False)

    def start(self, target, prepare, finished, *, maximized=False, normal_rect=None, prepare_before_resize=False):
        self.cancel()
        window = self.window
        start = QRect(window.geometry())
        window.entry_chrome.set_transition_active(True)
        window.setUpdatesEnabled(False)
        try:
            # Keep the visible rectangle when leaving native maximized state.
            window.setWindowState(window.windowState() & ~Qt.WindowMaximized & ~Qt.WindowMinimized)
            window._is_custom_maximized = False
            window.setGeometry(start)
            # Animate the actual window continuously. A larger temporary native
            # host followed by reparenting produced visible jumps before/after
            # the child animation, even though the child frames were smooth.
            if prepare_before_resize:
                # Returning to the chooser can resize its lightweight widgets;
                # the clinical page stays hidden and does not relayout per frame.
                prepare()
            page = getattr(window, 'welcome', None)
            if page is not None:
                self._entry_page = page
                margins = window.entry_chrome.layout().contentsMargins()
                target_width = target.width() - margins.left() - margins.right()
                page.begin_window_transition(target_width if prepare_before_resize else None)
        except BaseException:
            self.cancel()
            raise
        finally:
            window.setUpdatesEnabled(True)
        animation = _FrameAnimation(window, b'geometry', self)
        self.animation = animation
        # Time bounded transition; do not force every intermediate size to repaint.
        animation.setDuration(300)
        animation.setEasingCurve(QEasingCurve.InOutSine)
        animation.setStartValue(start)
        animation.setEndValue(target)

        def complete():
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
