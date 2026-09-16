"""Shared workspace backdrop with a local, theme-paired image cache."""
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QStackedWidget

from rem_card.app.paths import get_icon_dir
from pathlib import Path


FIXED_WORKSPACE_BACKGROUND = True

# Two shared decoded images, reused by both role shells; paths in the media
# cache are immutable. Include the stat signature for developer asset updates.
_decoded_pair_key = None
_decoded_pair = None


def _load_decoded_pair(light_path, dark_path):
    global _decoded_pair_key, _decoded_pair
    try:
        key = tuple((str(p), Path(p).stat().st_mtime_ns, Path(p).stat().st_size)
                    for p in (light_path, dark_path))
    except OSError:
        return None
    if key != _decoded_pair_key:
        light, dark = QPixmap(str(light_path)), QPixmap(str(dark_path))
        if light.isNull() or dark.isNull():
            return None
        _decoded_pair_key, _decoded_pair = key, (light, dark)
    return _decoded_pair


def workspace_surface_style(style):
    return style.replace('background-color: #f8f9fa;', 'background-color: transparent;') if FIXED_WORKSPACE_BACKGROUND else style


class WorkspaceStack(QStackedWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty('workspaceBackdrop', True)
        self.setContentsMargins(0, 5, 0, 0)
        self._background, self._dark_background = QPixmap(), QPixmap()
        self._pair_key = None
        self._scaled = QPixmap()
        self._cache_key = None
        from rem_card.ui.styles.theme_runtime import register_theme_callback
        register_theme_callback(self, self._refresh_background)
        from rem_card.ui.shared.workspace_background_manager import background_manager
        self._manager = background_manager()
        self._manager.changed.connect(self._load_pair)
        self._load_pair()
        if self._background.isNull():
            self._background, self._dark_background = _load_decoded_pair(
                Path(get_icon_dir()) / 'workspace_background_light.png',
                Path(get_icon_dir()) / 'workspace_background_dark.png') or (QPixmap(), QPixmap())

    def _load_pair(self):
        _entry, paths = self._manager.repository.active(self._manager.catalog)
        key = (paths['light'], paths['dark'])
        if key == self._pair_key:
            return
        pair = _load_decoded_pair(paths['light'], paths['dark'])
        if pair is None:
            return
        light, dark = pair
        self._background, self._dark_background = light, dark
        self._pair_key = key
        self._cache_key = None
        self.update()

    def _refresh_background(self):
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if FIXED_WORKSPACE_BACKGROUND and not self._background.isNull():
            ratio = self.devicePixelRatioF()
            from rem_card.ui.styles.theme_runtime import current_mode
            mode = current_mode()
            key = (self.width(), self.height(), ratio, mode)
            if key != self._cache_key:
                source = self._dark_background if mode == 'dark' and not self._dark_background.isNull() else self._background
                self._scaled = source.scaled(
                    QSize(round(self.width() * ratio), round(self.height() * ratio)),
                    Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation,
                )
                self._scaled.setDevicePixelRatio(ratio)
                self._cache_key = key
            painter = QPainter(self)
            painter.drawPixmap(round((self.width() - self._scaled.width() / ratio) / 2),
                               round((self.height() - self._scaled.height() / ratio) / 2), self._scaled)
