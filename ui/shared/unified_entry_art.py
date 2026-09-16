"""Small native decorative marks for the welcome screen."""
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap, QLinearGradient
from PySide6.QtWidgets import QWidget


def pulse(painter, rect, color):
    path = QPainterPath(QPointF(rect.left(), rect.center().y()))
    for x, y in ((.60, .5), (.63, .40), (.65, .60), (.68, .02),
                 (.71, .97), (.74, .38), (.77, .60), (.80, .5), (1, .5)):
        # Keep the QRS complex narrow even when the decorative line spans a header.
        if x < 1:
            x = .78 + (x - .68) * min(1, 160 / max(1, rect.width()))
        path.lineTo(rect.left() + rect.width() * x, rect.top() + rect.height() * y)
    gradient = QLinearGradient(rect.topLeft(), rect.topRight())
    for at, alpha in ((0, 0), (.45, 95), (.68, 255), (.85, 120), (1, 0)):
        shade = QColor(color)
        shade.setAlpha(alpha)
        gradient.setColorAt(at, shade)
    painter.setPen(QPen(gradient, 1.2))
    painter.setBrush(Qt.NoBrush)
    painter.drawPath(path)


class PulseLine(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(35)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        pulse(painter, QRectF(self.rect()).adjusted(0, 2, 0, -2), QColor('#0787da' if self.property('entry_theme') == 'light' else '#8ad9ff'))


def role_icon(role, color):
    """Render consistent outline icons; the caller caches their native-DPI size."""
    image = QPixmap(256, 256)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.scale(2.56, 2.56)
    painter.setPen(QPen(QColor(color), 3.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    painter.setBrush(Qt.NoBrush)
    path = QPainterPath()
    if role == 'doctor':
        path.moveTo(27, 13)
        path.cubicTo(8, 9, 12, 47, 33, 53)
        path.cubicTo(33, 61, 38, 64, 40, 65)
        path.cubicTo(42, 101, 83, 96, 77, 59)
        painter.drawPath(path)
        path = QPainterPath(QPointF(44, 13))
        path.cubicTo(67, 9, 62, 47, 40, 53)
        path.lineTo(40, 65)
        painter.drawPath(path)
        for x in (27, 44):
            painter.drawEllipse(QPointF(x, 13), 3, 3)
        painter.drawEllipse(QPointF(77, 51), 8, 8)
        painter.drawEllipse(QPointF(77, 51), 3, 3)
    elif role == 'nurse':
        path.moveTo(9, 30)
        path.lineTo(21, 24)
        path.lineTo(21, 18)
        path.cubicTo(40, 10, 60, 10, 79, 18)
        path.lineTo(79, 24)
        path.lineTo(91, 30)
        path.lineTo(77, 69)
        path.quadTo(50, 60, 23, 69)
        path.closeSubpath()
        painter.drawPath(path)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(color))
        painter.drawRect(QRectF(45, 27, 11, 29))
        painter.drawRect(QRectF(36, 36, 29, 11))
    else:
        if 'planned' in role:
            painter.drawRoundedRect(QRectF(9, 20, 62, 66), 4, 4)
            painter.drawLine(QPointF(9, 37), QPointF(71, 37))
            for x in (23, 56):
                painter.drawRoundedRect(QRectF(x, 12, 5, 17), 2, 2)
            for x in (22, 37, 52):
                for y in (49, 62, 75):
                    painter.fillRect(QRectF(x, y, 5, 5), QColor(color))
            painter.save()
            painter.translate(37, 13)
            painter.scale(.63, .88)
        path.moveTo(14, 87)
        path.cubicTo(22, 75, 44, 47, 56, 34)
        path.lineTo(60, 38)
        path.cubicTo(48, 56, 29, 82, 14, 87)
        path.closeSubpath()
        painter.drawPath(path)
        path = QPainterPath(QPointF(56, 34))
        path.cubicTo(66, 18, 78, 20, 85, 9)
        path.cubicTo(84, 25, 72, 37, 60, 38)
        painter.drawPath(path)
        if 'planned' in role:
            painter.restore()
    painter.end()
    return image
