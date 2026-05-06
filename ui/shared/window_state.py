from PySide6.QtCore import QEvent, QPoint, QRect, QSettings, Qt
from PySide6.QtWidgets import QApplication, QPushButton, QWidget


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
        self._resize_edges = Qt.Edge(0)
        self._resize_start_pos = QPoint()
        self._resize_start_geometry = QRect()
        self._frameless_filter_widget_ids = set()

    def _settings(self) -> QSettings:
        return QSettings("MyHospital", "RemCard")

    def _restore_saved_geometry(self) -> None:
        self._install_saved_frameless_child_filters()
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
        if pos.x() <= margin:
            edges |= Qt.LeftEdge
        if pos.x() >= self.width() - margin - 1:
            edges |= Qt.RightEdge
        if pos.y() <= margin:
            edges |= Qt.TopEdge
        if pos.y() >= self.height() - margin - 1:
            edges |= Qt.BottomEdge
        return edges

    def _install_saved_frameless_child_filters(self) -> None:
        installed = getattr(self, "_frameless_filter_widget_ids", set())
        for child in self.findChildren(QWidget):
            child_id = id(child)
            if child_id in installed:
                continue
            child.installEventFilter(self)
            child.setMouseTracking(True)
            installed.add(child_id)
        self._frameless_filter_widget_ids = installed

    def _event_pos_on_dialog(self, obj, event) -> QPoint:
        if hasattr(event, "position"):
            local_pos = event.position().toPoint()
        else:
            local_pos = event.pos()
        if obj is self or not hasattr(obj, "mapTo"):
            return local_pos
        return obj.mapTo(self, local_pos)

    def _set_resize_cursor(self, edges) -> None:
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

    def _start_resize(self, edges, global_pos: QPoint) -> None:
        self._resizing = True
        self._resize_edges = edges
        self._resize_start_pos = global_pos
        self._resize_start_geometry = self.geometry()
        self.grabMouse()

    def _start_drag(self, global_pos: QPoint) -> None:
        self._drag_pos = global_pos - self.pos()
        self._dragging = True
        self.grabMouse()

    def _finish_mouse_operation(self) -> None:
        self._resizing = False
        self._resize_edges = Qt.Edge(0)
        self._dragging = False
        self.releaseMouse()

    def _is_drag_start(self, pos) -> bool:
        if pos.y() > getattr(self, "_drag_area_height", self.DRAG_AREA_HEIGHT):
            return False
        child = self.childAt(pos)
        return not isinstance(child, QPushButton)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            edges = self._get_resize_edges(event.pos())
            if edges:
                self._start_resize(edges, event.globalPosition().toPoint())
                event.accept()
                return
            elif self._is_drag_start(event.pos()):
                self._start_drag(event.globalPosition().toPoint())
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and (self._resizing or self._dragging):
            self._finish_mouse_operation()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        edges = getattr(self, "_resize_edges", Qt.Edge(0)) if self._resizing else self._get_resize_edges(event.pos())
        self._set_resize_cursor(edges)

        if event.buttons() & Qt.LeftButton:
            if self._resizing:
                self._resize_by_mouse(event.globalPosition().toPoint())
                event.accept()
                return
            elif self._dragging:
                self.move(event.globalPosition().toPoint() - self._drag_pos)
                event.accept()
                return
        super().mouseMoveEvent(event)

    def eventFilter(self, obj, event):
        event_type = event.type()
        if event_type == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            pos = self._event_pos_on_dialog(obj, event)
            edges = self._get_resize_edges(pos)
            if edges:
                self._start_resize(edges, event.globalPosition().toPoint())
                return True
            if self._is_drag_start(pos):
                self._start_drag(event.globalPosition().toPoint())
                return True

        if event_type == QEvent.MouseMove:
            pos = self._event_pos_on_dialog(obj, event)
            edges = getattr(self, "_resize_edges", Qt.Edge(0)) if self._resizing else self._get_resize_edges(pos)
            self._set_resize_cursor(edges)
            if event.buttons() & Qt.LeftButton:
                if self._resizing:
                    self._resize_by_mouse(event.globalPosition().toPoint())
                    return True
                if self._dragging:
                    self.move(event.globalPosition().toPoint() - self._drag_pos)
                    return True

        if event_type == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton and (self._resizing or self._dragging):
            self._finish_mouse_operation()
            return True

        return super().eventFilter(obj, event)

    def _resize_by_mouse(self, global_pos: QPoint) -> None:
        edges = getattr(self, "_resize_edges", Qt.Edge(0))
        if not edges:
            return

        delta = global_pos - self._resize_start_pos
        geometry = QRect(self._resize_start_geometry)
        min_width = max(1, self.minimumWidth())
        min_height = max(1, self.minimumHeight())

        if edges & Qt.LeftEdge:
            new_left = geometry.left() + delta.x()
            if geometry.right() - new_left + 1 >= min_width:
                geometry.setLeft(new_left)
        if edges & Qt.RightEdge:
            new_right = geometry.right() + delta.x()
            if new_right - geometry.left() + 1 >= min_width:
                geometry.setRight(new_right)
        if edges & Qt.TopEdge:
            new_top = geometry.top() + delta.y()
            if geometry.bottom() - new_top + 1 >= min_height:
                geometry.setTop(new_top)
        if edges & Qt.BottomEdge:
            new_bottom = geometry.bottom() + delta.y()
            if new_bottom - geometry.top() + 1 >= min_height:
                geometry.setBottom(new_bottom)

        self.setGeometry(geometry)

    def done(self, result: int) -> None:
        self._save_saved_geometry()
        super().done(result)

    def closeEvent(self, event) -> None:
        self._save_saved_geometry()
        super().closeEvent(event)
