from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget


def _scaled(points: list[tuple[float, float]], size: int) -> list[QPointF]:
    return [QPointF(x * size / 24.0, y * size / 24.0) for x, y in points]


def _draw_polyline(painter: QPainter, points: list[tuple[float, float]], size: int) -> None:
    scaled = _scaled(points, size)
    for first, second in zip(scaled, scaled[1:]):
        painter.drawLine(first, second)


@lru_cache(maxsize=256)
def line_icon(kind: str, color: str = "#2563eb", size: int = 16) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color), max(1.2, size / 12.0))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)

    def r(x: float, y: float, w: float, h: float) -> QRectF:
        return QRectF(x * size / 24.0, y * size / 24.0, w * size / 24.0, h * size / 24.0)

    if kind == "folder":
        painter.drawRoundedRect(r(3, 6.5, 18, 13), 2.5, 2.5)
        _draw_polyline(painter, [(4, 8), (9, 8), (11, 6), (15, 6), (17, 8)], size)
        painter.drawLine(QPointF(size * 12 / 24, size * 11 / 24), QPointF(size * 12 / 24, size * 17 / 24))
        painter.drawLine(QPointF(size * 9 / 24, size * 14 / 24), QPointF(size * 15 / 24, size * 14 / 24))
    elif kind == "user":
        painter.drawEllipse(r(8.25, 4, 7.5, 7.5))
        painter.drawArc(r(5.5, 13, 13, 7.5), 20 * 16, 140 * 16)
    elif kind == "card":
        painter.drawRoundedRect(r(5, 4, 14, 16), 2.2, 2.2)
        painter.drawLine(QPointF(size * 14 / 24, size * 5 / 24), QPointF(size * 14 / 24, size * 9 / 24))
        painter.drawLine(QPointF(size * 16 / 24, size * 7 / 24), QPointF(size * 12 / 24, size * 7 / 24))
        painter.drawLine(QPointF(size * 8 / 24, size * 13 / 24), QPointF(size * 16 / 24, size * 13 / 24))
        painter.drawLine(QPointF(size * 8 / 24, size * 16 / 24), QPointF(size * 14 / 24, size * 16 / 24))
    elif kind == "clipboard":
        painter.drawRoundedRect(r(6, 5, 12, 15), 2, 2)
        painter.drawRoundedRect(r(9, 3.5, 6, 4), 1.5, 1.5)
        painter.drawLine(QPointF(size * 9 / 24, size * 12 / 24), QPointF(size * 15 / 24, size * 12 / 24))
        painter.drawLine(QPointF(size * 9 / 24, size * 15 / 24), QPointF(size * 14 / 24, size * 15 / 24))
    elif kind == "diagnosis":
        painter.drawRoundedRect(r(6, 5, 12, 14), 2, 2)
        painter.drawEllipse(r(9, 8, 6, 6))
        painter.drawLine(QPointF(size * 12 / 24, size * 10 / 24), QPointF(size * 12 / 24, size * 14 / 24))
        painter.drawLine(QPointF(size * 10 / 24, size * 12 / 24), QPointF(size * 14 / 24, size * 12 / 24))
    elif kind == "search":
        painter.drawEllipse(r(5.5, 5.5, 9.5, 9.5))
        painter.drawLine(QPointF(size * 14 / 24, size * 14 / 24), QPointF(size * 19 / 24, size * 19 / 24))
    elif kind == "calendar":
        painter.drawRoundedRect(r(4, 5.5, 16, 14.5), 2, 2)
        painter.drawLine(QPointF(size * 4 / 24, size * 10 / 24), QPointF(size * 20 / 24, size * 10 / 24))
        painter.drawLine(QPointF(size * 8 / 24, size * 3.5 / 24), QPointF(size * 8 / 24, size * 7.5 / 24))
        painter.drawLine(QPointF(size * 16 / 24, size * 3.5 / 24), QPointF(size * 16 / 24, size * 7.5 / 24))
    elif kind == "clock":
        painter.drawEllipse(r(5, 5, 14, 14))
        painter.drawLine(QPointF(size * 12 / 24, size * 8 / 24), QPointF(size * 12 / 24, size * 12 / 24))
        painter.drawLine(QPointF(size * 12 / 24, size * 12 / 24), QPointF(size * 15 / 24, size * 14 / 24))
    elif kind == "info":
        painter.drawEllipse(r(5, 5, 14, 14))
        painter.drawPoint(QPointF(size * 12 / 24, size * 9 / 24))
        painter.drawLine(QPointF(size * 12 / 24, size * 12 / 24), QPointF(size * 12 / 24, size * 16 / 24))
    elif kind == "save":
        painter.drawRoundedRect(r(5, 4.5, 14, 15), 2, 2)
        painter.drawRect(r(8, 4.5, 8, 5))
        painter.drawRoundedRect(r(8, 13, 8, 5.5), 1.2, 1.2)
    elif kind == "x":
        painter.drawLine(QPointF(size * 7 / 24, size * 7 / 24), QPointF(size * 17 / 24, size * 17 / 24))
        painter.drawLine(QPointF(size * 17 / 24, size * 7 / 24), QPointF(size * 7 / 24, size * 17 / 24))
    elif kind == "male":
        painter.drawEllipse(r(6.5, 10, 7.5, 7.5))
        painter.drawLine(QPointF(size * 13 / 24, size * 11 / 24), QPointF(size * 18 / 24, size * 6 / 24))
        _draw_polyline(painter, [(14.5, 6), (18, 6), (18, 9.5)], size)
    elif kind == "female":
        painter.drawEllipse(r(8, 4.5, 8, 8))
        painter.drawLine(QPointF(size * 12 / 24, size * 12.5 / 24), QPointF(size * 12 / 24, size * 19 / 24))
        painter.drawLine(QPointF(size * 9 / 24, size * 16 / 24), QPointF(size * 15 / 24, size * 16 / 24))
    elif kind == "pen":
        _draw_polyline(painter, [(5, 18), (8, 18), (18, 8), (16, 6), (6, 16), (5, 18)], size)
        painter.drawLine(QPointF(size * 14 / 24, size * 8 / 24), QPointF(size * 16 / 24, size * 10 / 24))
    else:
        painter.drawRoundedRect(r(5, 5, 14, 14), 3, 3)

    painter.end()
    return QIcon(pixmap)


class IconBadge(QLabel):
    def __init__(
        self,
        kind: str,
        color: str,
        bg_color: str = "transparent",
        *,
        side: int = 22,
        icon_size: int = 14,
        parent=None,
    ):
        super().__init__(parent)
        self.setFixedSize(side, side)
        self.setPixmap(line_icon(kind, color, icon_size).pixmap(QSize(icon_size, icon_size)))
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet(
            f"background-color: {bg_color}; border: none; border-radius: {max(3, side // 5)}px;"
        )


class GenderSegmentedControl(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._current = "Мужской"
        self.setFixedHeight(34)
        self.setMinimumWidth(270)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.male_button = self._make_button("Мужской", "male")
        self.female_button = self._make_button("Женский", "female")
        layout.addWidget(self.male_button, 1)
        layout.addWidget(self.female_button, 1)

        self.male_button.clicked.connect(lambda: self.setCurrentText("Мужской"))
        self.female_button.clicked.connect(lambda: self.setCurrentText("Женский"))
        self._sync_buttons()

    def _make_button(self, text: str, icon_kind: str) -> QPushButton:
        button = QPushButton(text)
        button.setCheckable(True)
        button.setCursor(Qt.PointingHandCursor)
        button.setFixedHeight(34)
        icon_color = "#1677ff" if icon_kind == "male" else "#ec5cc6"
        button.setIcon(line_icon(icon_kind, icon_color, 17))
        button.setIconSize(QSize(17, 17))
        return button

    def _sync_buttons(self) -> None:
        self.male_button.setChecked(self._current == "Мужской")
        self.female_button.setChecked(self._current == "Женский")
        base = """
            QPushButton {
                border: 1px solid #dbe5ef;
                border-radius: 5px;
                background: #ffffff;
                color: #253858;
                font-size: 13px;
                font-weight: 700;
                padding: 0px 10px;
            }
            QPushButton:hover {
                border-color: #b8d7ff;
                background: #f7fbff;
            }
        """
        selected = """
            QPushButton {
                border: 1px solid #b8d7ff;
                border-radius: 5px;
                background: #e8f3ff;
                color: #1267da;
                font-size: 13px;
                font-weight: 800;
                padding: 0px 10px;
            }
        """
        self.male_button.setStyleSheet(selected if self._current == "Мужской" else base)
        self.female_button.setStyleSheet(selected if self._current == "Женский" else base)

    def currentText(self) -> str:
        return self._current

    def setCurrentText(self, value: str) -> None:
        self._current = "Женский" if str(value).strip() == "Женский" else "Мужской"
        self._sync_buttons()
