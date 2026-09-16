"""Animate live Qt widgets at their current size, without scaling screenshots."""
from PySide6.QtCore import QObject, QRect, Qt, QPropertyAnimation, QEasingCurve, QTimer, QElapsedTimer


class _FrameAnimation(QPropertyAnimation):
    """Sample live geometry independently of Qt's shared 16 ms animation tick."""

    def __init__(self, *args):
        super().__init__(*args)
        self._clock = QElapsedTimer()
        self._offset = 0
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(8)
        self._timer.timeout.connect(self._advance)
        self.finished.connect(self._timer.stop)

    def start(self):
        super().start()
        super().pause()
        self.resume()

    def pause(self):
        self._timer.stop()

    def resume(self):
        self._offset = self.currentTime()
        self._clock.start()
        self._timer.start()

    def stop(self):
        self._timer.stop()
        super().stop()

    def _advance(self):
        self.setCurrentTime(min(self.duration(), self._offset + self._clock.elapsed()))


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
        # About five extra live frames at the measured 24 ms paint cadence.
        # The event loop stays active throughout the transition.
        animation.setDuration(300)
        animation.setEasingCurve(QEasingCurve.InOutSine)
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
