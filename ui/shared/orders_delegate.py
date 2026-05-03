import os
import re
from PySide6.QtWidgets import QStyledItemDelegate, QStyleOptionViewItem, QStyle
from PySide6.QtCore import Qt, QRect, QPoint
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QPixmap, QPolygon
from datetime import datetime, timedelta
from ...data.dto.remcard_dto import OrderType, OrderStatus
from ...services.order_domain_service import (
    NURSE_MARK_EXECUTED,
    NURSE_MARK_NOT_EXECUTED,
)
from ..styles.theme import BG_ALT_ROW

class OrdersDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.now_line_color = QColor(231, 76, 60, 150) # Прозрачный красный
        self._icon_cache = {}
        self._scaled_icon_cache = {}
        from rem_card.app.paths import get_icon_dir
        self._mark_icon_paths = {
            NURSE_MARK_EXECUTED: os.path.join(get_icon_dir(), "done.png"),
            NURSE_MARK_NOT_EXECUTED: os.path.join(get_icon_dir(), "notdone.png"),
        }
        self._save_icon_path = os.path.join(get_icon_dir(), "savecard.png")

    def _get_icon_pixmap(self, path: str):
        if not path:
            return None
        if path in self._icon_cache:
            return self._icon_cache[path]

        if not os.path.exists(path):
            self._icon_cache[path] = None
            return None

        pm = QPixmap(path)
        if pm.isNull():
            self._icon_cache[path] = None
            return None

        self._icon_cache[path] = pm
        return pm

    def _get_scaled_icon_pixmap(self, path: str, size: int):
        size = int(size)
        if size <= 0:
            return None

        cache_key = (path, size)
        if cache_key in self._scaled_icon_cache:
            return self._scaled_icon_cache[cache_key]

        pm = self._get_icon_pixmap(path)
        if pm is None:
            self._scaled_icon_cache[cache_key] = None
            return None

        scaled = pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._scaled_icon_cache[cache_key] = scaled
        return scaled

    def _get_mark_pixmap(self, mark: str):
        return self._get_icon_pixmap(self._mark_icon_paths.get(mark))

    def _get_scaled_mark_pixmap(self, mark: str, size: int):
        return self._get_scaled_icon_pixmap(self._mark_icon_paths.get(mark), size)

    def _get_scaled_savecard_pixmap(self, size: int = 16):
        return self._get_scaled_icon_pixmap(self._save_icon_path, size)

    def _format_duration(self, minutes: int) -> str:
        if not minutes or minutes <= 0:
            return ""
        if minutes < 60:
            return f"{minutes} мин."
        else:
            hours = round(minutes / 60.0, 1)
            hours_str = str(hours).replace('.', ',')
            return f"{hours_str} ч."

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index):
        model = index.model()
        if not model:
            return

        self._paint_grid(painter, option.rect)
        order_or_admin = model.data(index, Qt.UserRole)

        if index.column() == 0:
            self._paint_order_column(painter, option, order_or_admin, model)
            return

        time_slots = getattr(model, 'time_slots', [])
        col_idx = index.column() - 1
        if col_idx < 0 or col_idx >= len(time_slots):
            return

        self._paint_time_cell(
            painter,
            option,
            order_or_admin,
            time_slots[col_idx],
            datetime.now(),
        )

    def _paint_grid(self, painter: QPainter, rect: QRect):
        painter.save()
        painter.setPen(QPen(QColor(220, 221, 225), 0.5))
        painter.drawLine(rect.left(), rect.top(), rect.right(), rect.top())
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())
        painter.drawLine(rect.left(), rect.top(), rect.left(), rect.bottom())
        painter.drawLine(rect.right(), rect.top(), rect.right(), rect.bottom())
        painter.restore()

    def _build_order_display(self, order, model):
        latin = order.latin if hasattr(order, 'latin') and order.latin else "Без названия"
        dose_val = order.dose_value if hasattr(order, 'dose_value') and order.dose_value is not None else 0
        dose_unit = order.dose_unit if hasattr(order, 'dose_unit') and order.dose_unit else ""
        order_type = order.type if hasattr(order, 'type') else OrderType.PROCEDURE
        drug_key = str(getattr(order, 'drug_key', '') or '').strip().lower()

        if drug_key in ('ruchnoivvod', 'plasma', 'blood') or re.match(r'^[A-Za-z]+\. ', latin.strip()):
            prefix = ""
        else:
            prefix = "S. " if order_type != OrderType.PROCEDURE else ""

        dose_str = f"{dose_val:g} {dose_unit}".strip()
        if dose_str == "0":
            dose_str = ""

        if getattr(order, 'is_per_kg', False) and dose_str:
            dose_str = f"{dose_str}/кг"

        line2 = self._parse_order_comment(
            getattr(order, 'comment', "") or "",
            getattr(order, 'duration_min', 0),
        )
        if getattr(order, "_pending_delete", False):
            line2 = f"Удаление... {line2}".strip()

        return {
            "line1": f"{prefix}{latin} {dose_str}".strip(),
            "line2": line2,
            "show_savecard": bool(
                getattr(order, 'is_finalized', False)
                and not getattr(model, 'has_any_draft', False)
            ),
        }

    def _parse_order_comment(self, comment_text: str, fallback_duration) -> str:
        line2_parts = []

        diluent = comment_text
        diluent = re.sub(r'\[ROUTE:.*?\]', '', diluent)
        diluent = re.sub(r'\[DUR:.*?\]', '', diluent)
        diluent = diluent.replace("[RU]", "").strip()

        if diluent and not diluent.isspace():
            if diluent.startswith("+"):
                diluent = diluent[1:].strip()

            if diluent:
                if diluent.startswith("S. "):
                    diluent = diluent[3:].strip()

                if " - " not in diluent:
                    diluent = re.sub(r'\s+(\d+)\s*(мл|ml)', r' - \1 \2', diluent)

                line2_parts.append(f"S. {diluent}")

        route_dur_parts = []
        route_match = re.search(r'\[ROUTE:(.*?)\]', comment_text)
        if route_match:
            route_dur_parts.append(route_match.group(1))

        dur_match = re.search(r'\[DUR:(.*?)\]', comment_text)
        duration_val = 0
        if dur_match:
            try:
                duration_val = int(dur_match.group(1))
            except Exception:
                pass
        else:
            duration_val = fallback_duration

        try:
            duration_val = int(duration_val)
        except Exception:
            duration_val = 0

        if duration_val == -1:
            route_dur_parts.append("до конца суток")
        elif duration_val > 0:
            route_dur_parts.append(self._format_duration(duration_val))

        if route_dur_parts:
            line2_parts.append(" - ".join(route_dur_parts))

        if comment_text and not route_match and not dur_match and not "[RU]" in comment_text:
            old_text = comment_text.strip()
            match = re.search(r"(\d+)\s*мин", old_text)
            if match:
                mins = int(match.group(1))
                formatted = self._format_duration(mins)
                old_text = old_text.replace(match.group(0), formatted)
            if old_text and old_text not in line2_parts:
                if not diluent or old_text != diluent:
                    line2_parts = [old_text]

        return ", ".join(line2_parts)

    def _paint_order_column(self, painter: QPainter, option: QStyleOptionViewItem, order, model):
        if not order:
            return

        painter.save()
        if getattr(order, "_pending_delete", False):
            painter.fillRect(option.rect, QColor(255, 245, 232))
        elif option.features & QStyleOptionViewItem.Alternate:
            painter.fillRect(option.rect, QColor(BG_ALT_ROW))
        else:
            painter.fillRect(option.rect, Qt.white)

        painter.setPen(QPen(QColor(220, 221, 225), 0.5))
        painter.drawRect(option.rect)

        display = self._build_order_display(order, model)
        self._draw_order_text(painter, option.rect, display["line1"], display["line2"])

        if display["show_savecard"]:
            pixmap = self._get_scaled_savecard_pixmap(16)
            if pixmap is not None:
                icon_x = option.rect.right() - 20
                icon_y = option.rect.top() + 5
                painter.drawPixmap(icon_x, icon_y, pixmap)

        painter.restore()

    def _draw_order_text(self, painter: QPainter, rect: QRect, line1_text: str, line2_text: str):
        painter.setPen(Qt.black)
        rect1 = QRect(rect.left() + 5, rect.top() + 5, rect.width() - 10, 20)
        font_metrics = painter.fontMetrics()
        elided_line1 = font_metrics.elidedText(line1_text, Qt.ElideRight, rect1.width())
        painter.drawText(rect1, Qt.AlignLeft | Qt.AlignVCenter, elided_line1)

        if line2_text:
            painter.setPen(QColor(120, 120, 120))
            rect2 = QRect(rect.left() + 5, rect.top() + 25, rect.width() - 10, 15)
            elided_line2 = font_metrics.elidedText(line2_text, Qt.ElideRight, rect2.width())
            painter.drawText(rect2, Qt.AlignLeft | Qt.AlignVCenter, elided_line2)

    def _paint_time_cell(self, painter: QPainter, option: QStyleOptionViewItem, admin, hour_dt, now: datetime):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, False)
        rect = option.rect

        if admin:
            self._paint_admin(painter, rect, admin)

        self._draw_now_marker(painter, rect, hour_dt, now)
        self._draw_hour_separator(painter, rect, hour_dt)

        painter.restore()

    def _is_admin_pending(self, admin) -> bool:
        return bool(getattr(admin, "_pending_cell_action", None) or hasattr(admin, "_pending_mark"))

    def _paint_admin(self, painter: QPainter, rect: QRect, admin):
        pending = self._is_admin_pending(admin)
        if admin.status == "cancelled":
            self._paint_cancelled_admin(painter, rect, pending=pending)
        elif admin.status == "planned":
            self._paint_planned_admin(painter, rect, admin, pending=pending)
        elif admin.status == "deleted":
            pass

    def _paint_cancelled_admin(self, painter: QPainter, rect: QRect, *, pending: bool = False):
        painter.setPen(QColor(95, 106, 117) if pending else Qt.black)
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignCenter, "Отм")
        if pending:
            self._draw_pending_corner(painter, rect)

    def _paint_planned_admin(self, painter: QPainter, rect: QRect, admin, *, pending: bool = False):
        painter.save()
        painter.setPen(QColor(95, 106, 117) if pending else Qt.black)
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)

        mark = getattr(admin, "comment", "") or ""
        cell_role = admin.cell_role
        if cell_role != "body":
            self._draw_role_mark_or_x(painter, rect, cell_role, mark)

        if cell_role in ("start", "body", "end"):
            self._draw_chain(painter, rect, cell_role, mark, pending=pending)

        if pending:
            self._draw_pending_corner(painter, rect)

        painter.restore()

    def _draw_role_mark_or_x(self, painter: QPainter, rect: QRect, cell_role: str, mark: str):
        mark_pixmap = None
        if cell_role in ("start", "single", "end"):
            icon_size = min(28, max(14, min(rect.width(), rect.height()) - 6))
            mark_pixmap = self._get_scaled_mark_pixmap(mark, icon_size)

        if mark_pixmap is not None:
            self._draw_centered_pixmap(painter, rect, mark_pixmap)
        else:
            painter.drawText(rect, Qt.AlignCenter, "X")

    def _draw_chain(self, painter: QPainter, rect: QRect, cell_role: str, mark: str, *, pending: bool = False):
        line_y = int(rect.center().y())
        cx = int(rect.center().x())
        painter.setPen(QPen(QColor(95, 106, 117) if pending else Qt.black, 1.5))

        if cell_role == "start":
            painter.drawLine(cx + 10, line_y, rect.right(), line_y)
        elif cell_role == "body":
            painter.drawLine(rect.left(), line_y, rect.right(), line_y)
        elif cell_role == "end":
            painter.drawLine(rect.left(), line_y, cx - 10, line_y)
            self._draw_chain_arrow(painter, cx, line_y, pending=pending)

        if cell_role in ("body", "end"):
            base_icon_size = min(24, max(14, min(rect.width(), rect.height()) - 8))
            icon_size = max(10, int(round(base_icon_size * 0.7))) if cell_role == "body" else base_icon_size
            body_mark_pixmap = self._get_scaled_mark_pixmap(mark, icon_size)
            if body_mark_pixmap is not None:
                self._draw_centered_pixmap(painter, rect, body_mark_pixmap)

    def _draw_chain_arrow(self, painter: QPainter, cx: int, line_y: int, *, pending: bool = False):
        painter.setBrush(QColor(95, 106, 117) if pending else Qt.black)
        arrow_size = 5
        arrow = QPolygon([
            QPoint(cx - 10, line_y),
            QPoint(cx - 10 - arrow_size, line_y - arrow_size),
            QPoint(cx - 10 - arrow_size, line_y + arrow_size),
        ])
        painter.drawPolygon(arrow)

    def _draw_centered_pixmap(self, painter: QPainter, rect: QRect, pixmap: QPixmap):
        x = rect.left() + (rect.width() - pixmap.width()) // 2
        y = rect.top() + (rect.height() - pixmap.height()) // 2
        painter.drawPixmap(x, y, pixmap)

    def _draw_pending_corner(self, painter: QPainter, rect: QRect):
        painter.save()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(95, 106, 117)))
        radius = 2
        dot_rect = QRect(rect.right() - 6, rect.top() + 3, radius * 2, radius * 2)
        painter.drawEllipse(dot_rect)
        painter.restore()

    def _draw_now_marker(self, painter: QPainter, rect: QRect, hour_dt, now: datetime):
        if now >= hour_dt and now < (hour_dt + timedelta(hours=1)):
            minute_ratio = now.minute / 60.0
            pos_x = rect.left() + int(rect.width() * minute_ratio)

            painter.setPen(QPen(self.now_line_color, 2))
            painter.drawLine(pos_x, rect.top(), pos_x, rect.bottom())

    def _draw_hour_separator(self, painter: QPainter, rect: QRect, hour_dt):
        if hour_dt.hour in [13, 19, 1]:
            painter.setPen(QPen(Qt.black, 1.5))
            x = rect.right() + 1.0
            painter.drawLine(x, rect.top(), x, rect.bottom())
