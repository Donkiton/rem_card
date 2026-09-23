"""Shared blue window chrome for the entry shell and its setup dialogs."""
from PySide6.QtCore import Qt, QEvent, QRectF, QPointF
from PySide6.QtGui import QColor, QPainter, QPen, QLinearGradient, QPainterPath, QRegion, QPixmap, QIcon
from PySide6.QtWidgets import QApplication, QWidget, QHBoxLayout, QVBoxLayout, QLabel, QAbstractButton

from rem_card.app.version import APP_VERSION


def entry_action_icon(kind, color='#e5f4ff'):
    import math
    pixmap = QPixmap(28, 28)
    pixmap.fill(Qt.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(color), 1.7, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    if kind == 'settings':
        shape = QPainterPath()
        for i in range(32):
            angle = math.pi * 2 * i / 32
            radius = 11 if i % 4 in (1, 2) else 8.5
            point = QPointF(14 + radius * math.cos(angle), 14 + radius * math.sin(angle))
            shape.moveTo(point) if i == 0 else shape.lineTo(point)
        shape.closeSubpath()
        p.drawPath(shape)
        p.drawEllipse(QRectF(10, 10, 8, 8))
    elif kind == 'clock':
        p.drawEllipse(QRectF(3, 3, 22, 22))
        p.drawLine(QPointF(14, 7), QPointF(14, 14))
        p.drawLine(QPointF(14, 14), QPointF(19, 14))
    else:
        p.drawEllipse(QRectF(3, 3, 22, 22))
        p.drawPoint(QPointF(14, 9))
        p.drawLine(QPointF(14, 13), QPointF(14, 20))
    p.end()
    return QIcon(pixmap)


class HeartMark(QWidget):
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        light = self.property('entry_theme') == 'light'
        side = min(self.width(), self.height())
        p.translate((self.width() - side) / 2, (self.height() - side) / 2)
        p.scale(side / 100, side / 100)
        shape = QPainterPath(QPointF(50, 91))
        shape.cubicTo(40, 77, 4, 51, 4, 29)
        shape.cubicTo(4, 3, 37, 0, 50, 23)
        shape.cubicTo(63, 0, 96, 3, 96, 29)
        shape.cubicTo(96, 51, 60, 77, 50, 91)
        gradient = QLinearGradient(0, 5, 95, 92)
        gradient.setColorAt(0, QColor('#db4358' if light else '#b7eeff'))
        gradient.setColorAt(1, QColor('#c91e36' if light else '#4db4f2'))
        p.setPen(Qt.NoPen)
        p.setBrush(gradient)
        p.drawPath(shape)
        p.setPen(QPen(QColor('#ffffff' if light else '#063352'), 5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        pulse = QPainterPath(QPointF(23, 51))
        for x, y in ((36, 51), (42, 40), (48, 65), (55, 31), (61, 53), (76, 53)):
            pulse.lineTo(x, y)
        p.drawPath(pulse)


class _WindowButton(QAbstractButton):
    def __init__(self, kind, owner, parent):
        super().__init__(parent)
        self.kind, self.owner = kind, owner
        self.setFixedSize(42, 28)
        self.setFocusPolicy(Qt.ClickFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName({'close': 'Закрыть', 'min': 'Свернуть', 'max': 'Развернуть'}[kind])
        self.clicked.connect(self.perform)

    def perform(self):
        if self.kind == 'close':
            self.owner.close()
        elif self.kind == 'min':
            self.owner.showMinimized()
        elif getattr(self.owner, '_is_custom_maximized', False):
            self.owner._is_custom_maximized = False
            self.owner.setGeometry(self.owner._custom_normal_geometry)
        elif self.owner.isMaximized():
            self.owner.showNormal()
        else:
            self.owner.showMaximized()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        light = self.parentWidget().property('entry_theme') == 'light'
        if self.underMouse() or self.hasFocus():
            p.fillRect(self.rect(), QColor('#b44157' if self.kind == 'close' else ('#d5e7f3' if light else '#245b83')))
        p.setPen(QPen(QColor('#162b42' if light else '#e1f1ff'), 1.6))
        x, y = self.width()/2, self.height()/2
        if self.kind == 'close':
            p.drawLine(QPointF(x-6, y-6), QPointF(x+6, y+6))
            p.drawLine(QPointF(x+6, y-6), QPointF(x-6, y+6))
        elif self.kind == 'min':
            p.drawLine(QPointF(x-7, y), QPointF(x+7, y))
        else:
            p.drawRect(QRectF(x-6, y-6, 12, 12))


class EntryTitleBar(QWidget):
    def __init__(self, owner, title=None, controls=True):
        super().__init__()
        self.owner = owner
        self.setFixedHeight(30)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(17, 0, 0, 0)
        mark = HeartMark()
        mark.setFixedSize(24, 24)
        layout.addWidget(mark)
        label = QLabel(title or f'РЕМКАРТА  v{APP_VERSION}')
        label.setStyleSheet("color:#eff8ff; background:transparent; font:600 16px 'Segoe UI';")
        layout.addWidget(label, 1)
        if controls:
            layout.addWidget(_WindowButton('min', owner, self))
            layout.addWidget(_WindowButton('max', owner, self))
        layout.addWidget(_WindowButton('close', owner, self))

    def set_theme(self, mode):
        self.setProperty('entry_theme', mode)
        for label in self.findChildren(QLabel):
            label.setStyleSheet(f"color:{'#101c46' if mode == 'light' else '#eff8ff'}; background:transparent; font:600 16px 'Segoe UI';")
        for mark in self.findChildren(HeartMark):
            mark.setProperty('entry_theme', mode)
            mark.update()
        for button in self.findChildren(_WindowButton):
            button.update()
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        light = self.property('entry_theme') == 'light'
        gradient = QLinearGradient(0, 0, self.width(), self.height())
        gradient.setColorAt(0, QColor('#f6f4f1' if light else '#153d60'))
        gradient.setColorAt(1, QColor('#eeeae6' if light else '#092945'))
        p.fillRect(self.rect(), gradient)
        p.setPen(QColor('#c9d3de' if light else '#507a9a'))
        p.drawLine(0, self.height()-1, self.width(), self.height()-1)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.owner.windowHandle():
            self.owner.windowHandle().startSystemMove()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton and not self.owner.property('entry_dialog'):
            if getattr(self.owner, '_is_custom_maximized', False):
                self.owner._is_custom_maximized = False
                self.owner.setGeometry(self.owner._custom_normal_geometry)
            else:
                self.owner.showNormal() if self.owner.isMaximized() else self.owner.showMaximized()


class EntryChrome(QWidget):
    def __init__(self, owner, body, *, title=None, dialog=False):
        super().__init__(owner)
        self.owner = owner
        self.role_mode = False
        self._mask_keys = {}
        self._transition_active = False
        owner.setProperty('entry_dialog', dialog)
        owner.setWindowFlag(Qt.FramelessWindowHint, True)
        owner.setAttribute(Qt.WA_TranslucentBackground, True)
        QApplication.instance().installEventFilter(self)
        owner.destroyed.connect(self._remove_application_event_filter)
        self.setMouseTracking(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(0)
        self.content = QWidget(self)
        layout.addWidget(self.content)
        inner = QVBoxLayout(self.content)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(0)
        self.title_bar = EntryTitleBar(owner, title, controls=not dialog)
        inner.addWidget(self.title_bar)
        inner.addWidget(body, 1)

    def _remove_application_event_filter(self, *_args):
        application = QApplication.instance()
        if application is not None:
            application.removeEventFilter(self)

    def set_role_mode(self, enabled):
        self.role_mode = bool(enabled)
        self.title_bar.setVisible(not enabled)
        margin = 1 if enabled else 3
        self.layout().setContentsMargins(margin, margin, margin, margin)
        self.unsetCursor()
        self.layout().activate()
        self._update_masks()
        self.update()

    def set_theme(self, mode):
        self.setProperty('entry_theme', mode)
        self.title_bar.set_theme(mode)
        self.update()

    def set_transition_active(self, active):
        self._transition_active = bool(active)
        if active:
            # The translucent backing already clips the rounded frame. Rebuilding
            # the native window region during every resize forces extra paints.
            self.owner.clearMask()
            self._mask_keys.pop(self.owner, None)
        else:
            self._update_masks()

    def _update_masks(self):
        maximized = self.owner.isMaximized() or getattr(self.owner, '_is_custom_maximized', False)
        radius = 0 if maximized else (5 if self.role_mode else 13)
        for widget, rounding in ((self.owner, radius), (self.content, max(0, radius - (1 if self.role_mode else 3)))):
            if widget is self.owner and self._transition_active:
                continue
            key = (widget.width(), widget.height(), rounding)
            if self._mask_keys.get(widget) == key:
                continue
            path = QPainterPath()
            path.addRoundedRect(QRectF(widget.rect()), rounding, rounding)
            region = QRegion(path.toFillPolygon().toPolygon())
            if widget is self.owner and rounding:
                # A native region has binary edges. Leave the translucent
                # painter's antialiased border intact instead of cutting its
                # partially covered corner pixels off at integer coordinates.
                base_region = region
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        region = region.united(base_region.translated(dx, dy))
            widget.setMask(region)
            self._mask_keys[widget] = key

    def eventFilter(self, obj, event):
        if obj in (self.owner, getattr(self, 'content', None)) and event.type() in (QEvent.Resize, QEvent.WindowStateChange, QEvent.Show):
            if hasattr(self, 'content'):
                self._update_masks()
        if event.type() in (QEvent.Enter, QEvent.MouseMove, QEvent.HoverMove) and isinstance(obj, QWidget) and self.isAncestorOf(obj):
            self.unsetCursor()
        # QApplication filters can receive Qt-internal QWidgetItem objects.
        # The filter does not consume events, and QObject.eventFilter cannot
        # accept those layout items as its watched QObject argument.
        return False

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        light = self.property('entry_theme') == 'light'
        p.setBrush(self.palette().window() if self.role_mode else QColor('#f4f1ed' if light else '#0c2944'))
        p.setPen(QPen(self.palette().mid().color() if self.role_mode else QColor('#a5b9cb' if light else '#81b4d6'), 1.2))
        radius = 4 if self.role_mode else 12
        p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), radius, radius)

    def _edges(self, point):
        edges = Qt.Edges()
        if self.owner.isMaximized() or getattr(self.owner, '_is_custom_maximized', False):
            return edges
        if point.x() < 7: edges |= Qt.LeftEdge
        if point.x() > self.width()-7: edges |= Qt.RightEdge
        if point.y() < 7: edges |= Qt.TopEdge
        if point.y() > self.height()-7: edges |= Qt.BottomEdge
        return edges

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not self.owner.isMaximized() and self.owner.windowHandle():
            edges = self._edges(event.position())
            if edges:
                self.owner.windowHandle().startSystemResize(edges)

    def mouseMoveEvent(self, event):
        edges = self._edges(event.position())
        if edges in (Qt.LeftEdge | Qt.TopEdge, Qt.RightEdge | Qt.BottomEdge): cursor = Qt.SizeFDiagCursor
        elif edges in (Qt.RightEdge | Qt.TopEdge, Qt.LeftEdge | Qt.BottomEdge): cursor = Qt.SizeBDiagCursor
        elif edges & (Qt.LeftEdge | Qt.RightEdge): cursor = Qt.SizeHorCursor
        elif edges & (Qt.TopEdge | Qt.BottomEdge): cursor = Qt.SizeVerCursor
        else: cursor = Qt.ArrowCursor
        self.setCursor(cursor)

    def leaveEvent(self, event):
        self.unsetCursor()
        super().leaveEvent(event)
