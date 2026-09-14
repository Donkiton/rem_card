"""Short, non-blocking transitions between saved window rectangles."""
from PySide6.QtCore import QObject, QEvent, QRect, Qt, QPropertyAnimation, QEasingCurve, QSequentialAnimationGroup, QVariantAnimation
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QWidget


class _TransitionCover(QWidget):
    def __init__(self, parent, snapshot):
        super().__init__(parent)
        self.snapshot = snapshot
        self.opacity = 1.0
        self.setCursor(Qt.ArrowCursor)
        self.setGeometry(parent.rect())

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.setOpacity(self.opacity)
        painter.drawPixmap(self.rect(), self.snapshot)


class WindowTransition(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.animation = None
        self.cover = None
        window.installEventFilter(self)

    @property
    def running(self):
        return self.animation is not None

    def eventFilter(self, obj, event):
        if obj is self.window and event.type() == QEvent.Resize and self.cover:
            self.cover.setGeometry(self.window.entry_chrome.rect())
        return False

    def cancel(self):
        if self.animation:
            self.animation.stop()
            self.animation.deleteLater()
            self.animation = None
        self.window.entry_chrome.content.show()
        if self.cover:
            self.cover.hide()
            self.cover.deleteLater()
            self.cover = None

    def start(self, target, prepare, finished, *, maximized=False, normal_rect=None):
        self.cancel()
        window = self.window
        start = QRect(window.geometry())
        self.cover = _TransitionCover(window.entry_chrome, window.entry_chrome.grab())
        window.setUpdatesEnabled(False)
        try:
            # Keep the visible rectangle when leaving native maximized state.
            window.setWindowState(window.windowState() & ~Qt.WindowMaximized & ~Qt.WindowMinimized)
            window._is_custom_maximized = False
            window.setGeometry(start)
            window.entry_chrome.content.hide()
            self.cover.setGeometry(window.entry_chrome.rect())
            self.cover.show()
            self.cover.raise_()
        finally:
            window.setUpdatesEnabled(True)
        group = QSequentialAnimationGroup(self)
        self.animation = group
        animation = QPropertyAnimation(window, b'geometry', group)
        animation.setDuration(240)
        animation.setEasingCurve(QEasingCurve.InOutCubic)
        animation.setStartValue(start)
        animation.setEndValue(target)
        group.addAnimation(animation)
        fade = QVariantAnimation(group)
        fade.setDuration(80)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        fade.setEasingCurve(QEasingCurve.InOutCubic)
        group.addAnimation(fade)

        def reveal():
            # Resize expensive clinical layouts once, after the geometry animation.
            prepare()
            window.entry_chrome.content.show()
            window.entry_chrome.layout().activate()
            window.entry_chrome.content.layout().activate()
            self.cover.raise_()

        animation.finished.connect(reveal)

        def frame(value):
            if self.cover:
                self.cover.setGeometry(window.entry_chrome.rect())
                self.cover.opacity = 1.0 - value
                self.cover.update()

        def complete():
            window.setGeometry(target)
            window._is_custom_maximized = maximized
            if maximized and normal_rect is not None:
                window._custom_normal_geometry = QRect(normal_rect)
            window.entry_chrome._update_masks()
            self.cancel()
            finished()

        fade.valueChanged.connect(frame)
        group.finished.connect(complete)
        group.start()
