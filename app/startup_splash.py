from __future__ import annotations

from PySide6.QtCore import QRectF, QUrl, Qt
from PySide6.QtGui import QCloseEvent, QCursor, QPainter, QPainterPath, QPaintEvent, QRegion
from PySide6.QtMultimedia import QMediaPlayer, QVideoFrame, QVideoSink
from PySide6.QtWidgets import QApplication, QWidget


STARTUP_SPLASH_SIZE = 300
STARTUP_SPLASH_SOURCE_CROP = 0.052


class StartupVideoSplash(QWidget):
    """Безрамочная зацикленная видеозаставка на время запуска приложения."""

    def __init__(self, video_path: str):
        flags = (
            Qt.WindowType.SplashScreen
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.WindowTransparentForInput
        )
        super().__init__(None, flags)

        self.setObjectName("startupVideoSplash")
        self.setFixedSize(STARTUP_SPLASH_SIZE, STARTUP_SPLASH_SIZE)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMask(QRegion(self.rect(), QRegion.RegionType.Ellipse))

        self._current_frame = None
        self._video_sink = QVideoSink(self)
        self._video_sink.videoFrameChanged.connect(self._set_video_frame)

        self._player = QMediaPlayer(self)
        self._player.setVideoOutput(self._video_sink)
        self._player.setLoops(QMediaPlayer.Loops.Infinite)
        self._player.setSource(QUrl.fromLocalFile(video_path))

    def _set_video_frame(self, frame: QVideoFrame) -> None:
        if not frame.isValid():
            return
        image = frame.toImage()
        if image.isNull():
            return
        self._current_frame = image
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        if self._current_frame is None:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        target = QRectF(self.rect())
        clip_path = QPainterPath()
        clip_path.addEllipse(target)
        painter.setClipPath(clip_path)

        image = self._current_frame
        crop_x = image.width() * STARTUP_SPLASH_SOURCE_CROP
        crop_y = image.height() * STARTUP_SPLASH_SOURCE_CROP
        source = QRectF(
            crop_x,
            crop_y,
            image.width() - (crop_x * 2),
            image.height() - (crop_y * 2),
        )
        painter.drawImage(target, image, source)

    def show_and_start(self, app: QApplication) -> None:
        screen = app.screenAt(QCursor.pos()) or app.primaryScreen()
        if screen is not None:
            screen_center = screen.availableGeometry().center()
            self.move(screen_center - self.rect().center())

        self.show()
        self.raise_()
        self._player.play()
        app.processEvents()

    def finish(self, _window=None) -> None:
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._player.stop()
        self._player.setVideoOutput(None)
        super().closeEvent(event)
