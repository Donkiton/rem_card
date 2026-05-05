from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtWidgets import QApplication, QPushButton


class SavedFramelessDialogMixin:
    """Общее поведение для frameless-окон: resize, drag и сохранение геометрии."""

    SETTINGS_GEOMETRY_KEY = ""
    RESIZE_MARGIN = 10
    DRAG_AREA_HEIGHT = 76

    def _init_saved_frameless_dialog(
        self,
        settings_key: str,
        *,
        resize_margin: int = 10,
        drag_area_height: int = 76,
    ) -> None:
        self._geometry_settings_key = settings_key
        self._resize_margin = int(resize_margin)
        self._drag_area_height = int(drag_area_height)
        self._drag_pos = QPoint()
        self._dragging = False
        self._resizing = False

    def _settings(self) -> QSettings:
        return QSettings("MyHospital", "RemCard")

    def _restore_saved_geometry(self) -> None:
        key = getattr(self, "_geometry_settings_key", "")
        if not key:
            return
        value = self._settings().value(key)
        if value is None:
            return
        try:
            restored = self.restoreGeometry(value)
        except Exception:
            restored = False
        if restored and self._is_on_available_screen():
            return
        self._center_on_available_screen()

    def _save_saved_geometry(self) -> None:
        key = getattr(self, "_geometry_settings_key", "")
        if not key:
            return
        settings = self._settings()
        settings.setValue(key, self.saveGeometry())
        settings.sync()

    def _is_on_available_screen(self) -> bool:
        app = QApplication.instance()
        screens = app.screens() if app is not None else []
        if not screens:
            return True
        frame = self.frameGeometry()
        return any(screen.availableGeometry().intersects(frame) for screen in screens)

    def _center_on_available_screen(self) -> None:
        app = QApplication.instance()
        screen = None
        parent = self.parentWidget()
        if parent is not None:
            screen = QApplication.screenAt(parent.frameGeometry().center())
        if screen is None and app is not None:
            screen = app.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        frame = self.frameGeometry()
        frame.moveCenter(area.center())
        self.move(frame.topLeft())

    def _get_resize_edges(self, pos):
        margin = getattr(self, "_resize_margin", self.RESIZE_MARGIN)
        edges = Qt.Edge(0)
        if pos.x() < margin:
            edges |= Qt.LeftEdge
        if pos.x() > self.width() - margin:
            edges |= Qt.RightEdge
        if pos.y() < margin:
            edges |= Qt.TopEdge
        if pos.y() > self.height() - margin:
            edges |= Qt.BottomEdge
        return edges

    def _is_drag_start(self, pos) -> bool:
        if pos.y() > getattr(self, "_drag_area_height", self.DRAG_AREA_HEIGHT):
            return False
        child = self.childAt(pos)
        return not isinstance(child, QPushButton)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            edges = self._get_resize_edges(event.pos())
            if edges:
                self._resizing = True
                handle = self.windowHandle()
                if handle is not None:
                    handle.startSystemResize(edges)
            elif self._is_drag_start(event.pos()):
                self._drag_pos = event.globalPosition().toPoint() - self.pos()
                self._dragging = True
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self._resizing = False
        if event.button() == Qt.LeftButton:
            self._dragging = False
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        edges = self._get_resize_edges(event.pos())
        if edges == (Qt.LeftEdge | Qt.TopEdge) or edges == (Qt.RightEdge | Qt.BottomEdge):
            self.setCursor(Qt.SizeFDiagCursor)
        elif edges == (Qt.RightEdge | Qt.TopEdge) or edges == (Qt.LeftEdge | Qt.BottomEdge):
            self.setCursor(Qt.SizeBDiagCursor)
        elif edges & (Qt.LeftEdge | Qt.RightEdge):
            self.setCursor(Qt.SizeHorCursor)
        elif edges & (Qt.TopEdge | Qt.BottomEdge):
            self.setCursor(Qt.SizeVerCursor)
        else:
            self.setCursor(Qt.ArrowCursor)

        if event.buttons() & Qt.LeftButton and self._dragging and not self._resizing:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(event)

    def done(self, result: int) -> None:
        self._save_saved_geometry()
        super().done(result)

    def closeEvent(self, event) -> None:
        self._save_saved_geometry()
        super().closeEvent(event)
