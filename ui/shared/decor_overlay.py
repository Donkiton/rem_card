from __future__ import annotations

import json
import math
import random
import time
from typing import Any, Callable

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QFrame, QStackedWidget, QWidget

from rem_card.ui.shared.decor_settings import (
    active_decor_event,
    decor_file_path,
    normalize_decor_event,
)


ContextProvider = Callable[[], dict[str, Any]]
TargetProvider = Callable[[dict[str, Any]], QWidget | None]


class DecorOverlayWidget(QWidget):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        context_provider: ContextProvider | None = None,
        target_provider: TargetProvider | None = None,
        forced_event: dict[str, Any] | None = None,
    ):
        super().__init__(parent)
        self._context_provider = context_provider
        self._target_provider = target_provider
        self._forced_event = normalize_decor_event(forced_event) if forced_event else None
        self._event: dict[str, Any] | None = None
        self._event_signature = ""
        self._particles: list[dict[str, Any]] = []
        self._pixmap_cache: dict[tuple[str, int], QPixmap] = {}
        self._bottom_bins: list[float] = []
        self._surface_bins: dict[str, list[float]] = {}
        self._surfaces: list[dict[str, Any]] = []
        self._trail: list[dict[str, Any]] = []
        self._last_ts = time.monotonic()
        self._last_mouse_pos: QPointF | None = None
        self._last_surface_scan_ts = 0.0

        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.hide()

        self._frame_timer = QTimer(self)
        self._frame_timer.setInterval(33)
        self._frame_timer.timeout.connect(self._tick)

        self._settings_timer = QTimer(self)
        self._settings_timer.setInterval(15000)
        self._settings_timer.timeout.connect(self.reload_settings)
        self._settings_timer.start()

        if parent is not None:
            parent.installEventFilter(self)
            self.setGeometry(parent.rect())
        QTimer.singleShot(0, self.reload_settings)

    def eventFilter(self, obj, event):
        if obj is self.parent() and event.type() in {QEvent.Resize, QEvent.Show, QEvent.LayoutRequest}:
            parent = self.parentWidget()
            if parent is not None:
                self.setGeometry(parent.rect())
                self.raise_()
        return super().eventFilter(obj, event)

    def set_forced_event(self, event: dict[str, Any] | None) -> None:
        self._forced_event = normalize_decor_event(event) if event else None
        self.reload_settings()

    def reload_settings(self) -> None:
        try:
            event = self._forced_event or active_decor_event()
        except Exception:
            event = None
        if event is not None:
            event = normalize_decor_event(event)

        if event is None or not self._zone_enabled(event):
            self._event = None
            self._event_signature = ""
            self._particles.clear()
            self.hide()
            self._frame_timer.stop()
            return

        signature = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
        changed = signature != self._event_signature
        self._event = event
        self._event_signature = signature
        if changed:
            self._particles.clear()
            self._bottom_bins.clear()
            self._surface_bins.clear()
            self._surfaces.clear()
            self._pixmap_cache.clear()
            self._last_surface_scan_ts = 0.0
        self._ensure_particle_count(initial=changed)
        self.show()
        self.raise_()
        if not self._frame_timer.isActive():
            self._last_ts = time.monotonic()
            self._frame_timer.start()

    def paintEvent(self, event):
        if self._event is None:
            return
        target = self._target_rect()
        if target.width() <= 8 or target.height() <= 8:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.setClipRect(target)
        self._paint_particles(painter)
        self._paint_trail(painter)
        self._paint_drifts(painter, target)

    def _tick(self) -> None:
        event = self._event
        if event is None:
            return
        if not self._zone_enabled(event):
            self.reload_settings()
            return
        now = time.monotonic()
        dt = min(0.08, max(0.001, now - self._last_ts))
        self._last_ts = now
        target = self._target_rect()
        if target.width() <= 8 or target.height() <= 8:
            return

        if now - self._last_surface_scan_ts > 1.2:
            self._last_surface_scan_ts = now
            self._collect_surfaces(target)
        self._ensure_particle_count()
        mouse_pos, mouse_delta = self._mouse_state(target, dt)
        self._update_particles(target, dt, mouse_pos, mouse_delta)
        self._update_trail(dt)
        self._erode_drifts(mouse_pos, mouse_delta, dt)
        self.update(target.adjusted(-4, -4, 4, 4))

    def _zone_enabled(self, event: dict[str, Any]) -> bool:
        if self._forced_event is not None:
            return True
        zone = str((event or {}).get("zone") or "all")
        context = self._context()
        role = str(context.get("role") or "").lower()
        mode = str(context.get("mode") or "").lower()
        if zone == "all":
            return role not in {"welcome", "unknown"}
        if zone == "remcard":
            return role in {"doctor", "nurse"}
        if zone == "operblock":
            return role.startswith("operblock")
        if zone == "w1":
            return role in {"doctor", "nurse"} and mode == "beds"
        return True

    def _context(self) -> dict[str, Any]:
        if self._context_provider is None:
            return {}
        try:
            context = self._context_provider()
            return context if isinstance(context, dict) else {}
        except Exception:
            return {}

    def _target_rect(self) -> QRect:
        event = self._event or self._forced_event or {}
        target_widget = None
        if self._target_provider is not None:
            try:
                target_widget = self._target_provider(event)
            except Exception:
                target_widget = None
        if target_widget is None or not target_widget.isVisible():
            return self.rect()
        top_left = self.mapFromGlobal(target_widget.mapToGlobal(QPoint(0, 0)))
        rect = QRect(top_left, target_widget.size()).intersected(self.rect())
        if rect.width() < 20 or rect.height() < 20:
            return self.rect()
        return rect

    def _desired_particle_count(self) -> int:
        event = self._event or self._forced_event or {}
        intensity = _int(event.get("intensity"), 34)
        if intensity <= 0:
            return 0
        return max(4, min(150, int(8 + intensity * 1.15)))

    def _ensure_particle_count(self, *, initial: bool = False) -> None:
        target = self._target_rect()
        desired = self._desired_particle_count()
        while len(self._particles) < desired:
            self._particles.append(self._new_particle(target, initial=initial))
        if len(self._particles) > desired:
            del self._particles[desired:]

    def _new_particle(self, target: QRect, *, initial: bool = False) -> dict[str, Any]:
        particle_def = self._pick_particle_def()
        size = max(6, int(float(particle_def.get("size") or 24) * random.uniform(0.82, 1.18)))
        weight = max(0.1, min(5.0, float(particle_def.get("weight") or 1.0)))
        x = random.uniform(target.left(), target.right())
        y = random.uniform(target.top(), target.bottom()) if initial else target.top() - random.uniform(size, target.height() * 0.25)
        return {
            "x": x,
            "y": y,
            "previous_y": y,
            "vx": random.uniform(-8, 8),
            "phase": random.uniform(0, math.tau),
            "rotation": random.uniform(0, 360),
            "spin": random.uniform(-34, 34),
            "size": size,
            "weight": weight,
            "file": str(particle_def.get("file") or ""),
            "opacity": random.uniform(0.58, 0.92),
        }

    def _pick_particle_def(self) -> dict[str, Any]:
        event = self._event or self._forced_event or {}
        particles = [item for item in event.get("particles") or [] if isinstance(item, dict)]
        if not particles:
            return {"file": "", "size": 18, "weight": 1.0}
        return random.choice(particles)

    def _reset_particle(self, particle: dict[str, Any], target: QRect) -> None:
        replacement = self._new_particle(target, initial=False)
        particle.clear()
        particle.update(replacement)

    def _update_particles(self, target: QRect, dt: float, mouse_pos: QPointF | None, mouse_delta: QPointF) -> None:
        event = self._event or {}
        intensity = _int(event.get("intensity"), 34)
        wind_strength = _int(event.get("wind_strength"), 52) / 100.0
        surfaces = list(self._surfaces)
        for particle in self._particles:
            particle["previous_y"] = float(particle.get("y") or 0.0)
            size = float(particle.get("size") or 20.0)
            weight = float(particle.get("weight") or 1.0)
            fall_speed = (18 + weight * 34) * (0.72 + intensity / 140.0)
            drift = math.sin(time.monotonic() * 0.9 + float(particle.get("phase") or 0.0)) * (8 + intensity * 0.11)
            particle["vx"] = float(particle.get("vx") or 0.0) * 0.988 + drift * dt

            if mouse_pos is not None:
                self._apply_cursor_wind(particle, mouse_pos, mouse_delta, wind_strength, dt)

            particle["x"] = float(particle.get("x") or 0.0) + float(particle.get("vx") or 0.0) * dt
            particle["y"] = float(particle.get("y") or 0.0) + fall_speed * dt
            particle["rotation"] = float(particle.get("rotation") or 0.0) + float(particle.get("spin") or 0.0) * dt

            if float(particle["x"]) < target.left() - size:
                particle["x"] = target.right() + size
            elif float(particle["x"]) > target.right() + size:
                particle["x"] = target.left() - size

            settled = False
            for surface in surfaces:
                if self._particle_hits_surface(particle, surface):
                    self._add_surface_drift(surface, float(particle["x"]), weight * size * 0.014)
                    settled = True
                    break
            if settled or float(particle["y"]) > target.bottom() + size:
                if not settled:
                    self._add_bottom_drift(float(particle["x"]), weight * size * 0.026)
                self._reset_particle(particle, target)

    def _apply_cursor_wind(
        self,
        particle: dict[str, Any],
        mouse_pos: QPointF,
        mouse_delta: QPointF,
        wind_strength: float,
        dt: float,
    ) -> None:
        px = float(particle.get("x") or 0.0)
        py = float(particle.get("y") or 0.0)
        dx = px - mouse_pos.x()
        dy = py - mouse_pos.y()
        distance = math.hypot(dx, dy)
        radius = 132 + wind_strength * 48
        if distance > radius:
            return
        factor = (1.0 - distance / radius) ** 2
        tangent_x = -dy / max(distance, 1.0)
        tangent_y = dx / max(distance, 1.0)
        movement = min(90.0, math.hypot(mouse_delta.x(), mouse_delta.y()))
        force = factor * (80 + movement * 2.2) * wind_strength
        particle["vx"] = float(particle.get("vx") or 0.0) + (tangent_x * force + mouse_delta.x() * 1.1) * dt
        particle["y"] = float(particle.get("y") or 0.0) + (tangent_y * force * 0.28 - abs(mouse_delta.y()) * 0.18) * dt
        particle["spin"] = float(particle.get("spin") or 0.0) + tangent_x * force * 0.7 * dt

    def _particle_hits_surface(self, particle: dict[str, Any], surface: dict[str, Any]) -> bool:
        rect: QRectF = surface["rect"]
        x = float(particle.get("x") or 0.0)
        previous_bottom = float(particle.get("previous_y") or 0.0) + float(particle.get("size") or 0.0) * 0.5
        current_bottom = float(particle.get("y") or 0.0) + float(particle.get("size") or 0.0) * 0.5
        if not (rect.left() <= x <= rect.right()):
            return False
        surface_y = rect.top()
        if not (previous_bottom <= surface_y <= current_bottom):
            return False
        snowdrifts = (self._event or {}).get("snowdrifts") or {}
        chance = max(0.02, min(0.55, _int(snowdrifts.get("surface_intensity"), 35) / 120.0))
        return random.random() < chance

    def _mouse_state(self, target: QRect, dt: float) -> tuple[QPointF | None, QPointF]:
        pos = QPointF(self.mapFromGlobal(QCursor.pos()))
        if not target.adjusted(-80, -80, 80, 80).contains(pos.toPoint()):
            self._last_mouse_pos = None
            return None, QPointF(0, 0)
        previous = self._last_mouse_pos
        self._last_mouse_pos = pos
        if previous is None:
            return pos, QPointF(0, 0)
        delta = QPointF((pos.x() - previous.x()) / max(dt * 60.0, 0.1), (pos.y() - previous.y()) / max(dt * 60.0, 0.1))
        if math.hypot(delta.x(), delta.y()) > 0.55:
            self._trail.append({"x": pos.x(), "y": pos.y(), "ttl": 0.55, "power": min(1.0, math.hypot(delta.x(), delta.y()) / 42.0)})
        return pos, delta

    def _update_trail(self, dt: float) -> None:
        for item in self._trail:
            item["ttl"] = float(item.get("ttl") or 0.0) - dt
        self._trail = [item for item in self._trail if float(item.get("ttl") or 0.0) > 0.0][-18:]

    def _collect_surfaces(self, target: QRect) -> None:
        root = self._surface_root_widget()
        if root is None:
            self._surfaces = []
            return
        surfaces: list[dict[str, Any]] = []
        seen_y: list[int] = []
        for child in root.findChildren(QWidget):
            if child is self or not child.isVisible():
                continue
            if child.width() < 170 or child.height() < 46:
                continue
            top_left = self.mapFromGlobal(child.mapToGlobal(QPoint(0, 0)))
            rect = QRect(top_left, child.size()).intersected(target)
            if rect.width() < 160 or rect.height() < 24:
                continue
            if rect.width() > target.width() * 0.96 and rect.height() > target.height() * 0.86:
                continue
            y = rect.top()
            if y < target.top() + 28 or y > target.bottom() - 48:
                continue
            if any(abs(y - existing) < 16 for existing in seen_y):
                continue
            seen_y.append(y)
            key = f"{rect.left()}:{rect.top()}:{rect.width()}"
            surfaces.append({"key": key, "rect": QRectF(rect.left(), rect.top(), rect.width(), 5)})
            if len(surfaces) >= 12:
                break
        self._surfaces = sorted(surfaces, key=lambda item: item["rect"].top())
        for surface in self._surfaces:
            key = str(surface["key"])
            bins = max(8, min(80, int(surface["rect"].width() / 18)))
            if key not in self._surface_bins or len(self._surface_bins[key]) != bins:
                self._surface_bins[key] = [0.0 for _ in range(bins)]
        valid_keys = {str(surface["key"]) for surface in self._surfaces}
        for key in list(self._surface_bins):
            if key not in valid_keys:
                self._surface_bins.pop(key, None)

    def _surface_root_widget(self) -> QWidget | None:
        event = self._event or self._forced_event or {}
        target_widget = None
        if self._target_provider is not None:
            try:
                target_widget = self._target_provider(event)
            except Exception:
                target_widget = None
        if target_widget is not None:
            return target_widget
        parent = self.parentWidget()
        if isinstance(parent, QStackedWidget):
            return parent.currentWidget()
        return parent

    def _ensure_bottom_bins(self, target: QRect) -> list[float]:
        bins = max(24, min(120, int(target.width() / 16)))
        if len(self._bottom_bins) != bins:
            self._bottom_bins = [0.0 for _ in range(bins)]
        return self._bottom_bins

    def _add_bottom_drift(self, x: float, amount: float) -> None:
        event = self._event or {}
        snowdrifts = event.get("snowdrifts") or {}
        if not bool(snowdrifts.get("enabled", True)):
            return
        target = self._target_rect()
        bins = self._ensure_bottom_bins(target)
        if not bins:
            return
        max_height = _int(snowdrifts.get("max_height"), 42)
        accumulation = _int(snowdrifts.get("accumulation"), 38) / 100.0
        index = max(0, min(len(bins) - 1, int((x - target.left()) / max(target.width(), 1) * len(bins))))
        for offset, factor in ((0, 1.0), (-1, 0.45), (1, 0.45), (-2, 0.18), (2, 0.18)):
            pos = index + offset
            if 0 <= pos < len(bins):
                bins[pos] = min(max_height, bins[pos] + amount * accumulation * factor)

    def _add_surface_drift(self, surface: dict[str, Any], x: float, amount: float) -> None:
        event = self._event or {}
        snowdrifts = event.get("snowdrifts") or {}
        if not bool(snowdrifts.get("enabled", True)):
            return
        key = str(surface.get("key") or "")
        rect: QRectF = surface["rect"]
        bins = self._surface_bins.get(key)
        if not bins:
            return
        max_height = _int(snowdrifts.get("max_height"), 42) * 0.42
        accumulation = _int(snowdrifts.get("accumulation"), 38) / 100.0
        index = max(0, min(len(bins) - 1, int((x - rect.left()) / max(rect.width(), 1.0) * len(bins))))
        for offset, factor in ((0, 0.8), (-1, 0.32), (1, 0.32)):
            pos = index + offset
            if 0 <= pos < len(bins):
                bins[pos] = min(max_height, bins[pos] + amount * accumulation * factor)

    def _erode_drifts(self, mouse_pos: QPointF | None, mouse_delta: QPointF, dt: float) -> None:
        if mouse_pos is None:
            return
        movement = math.hypot(mouse_delta.x(), mouse_delta.y())
        if movement < 0.4:
            return
        wind_strength = _int((self._event or {}).get("wind_strength"), 52) / 100.0
        radius = 116 + wind_strength * 72
        power = min(2.8, movement / 20.0) * wind_strength * dt * 24.0
        target = self._target_rect()
        bins = self._ensure_bottom_bins(target)
        self._erode_bins(bins, target.left(), target.bottom(), target.width(), mouse_pos, radius, power)
        for surface in self._surfaces:
            key = str(surface.get("key") or "")
            rect: QRectF = surface["rect"]
            surface_bins = self._surface_bins.get(key)
            if surface_bins:
                self._erode_bins(surface_bins, rect.left(), rect.top(), rect.width(), mouse_pos, radius * 0.72, power * 0.58)

    def _erode_bins(
        self,
        bins: list[float],
        left: float,
        y: float,
        width: float,
        mouse_pos: QPointF,
        radius: float,
        power: float,
    ) -> None:
        if not bins:
            return
        for index, value in enumerate(list(bins)):
            if value <= 0:
                continue
            x = left + (index + 0.5) / len(bins) * width
            distance = math.hypot(x - mouse_pos.x(), y - mouse_pos.y())
            if distance > radius:
                continue
            loss = min(value, power * (1.0 - distance / radius) ** 2)
            bins[index] = max(0.0, value - loss)

    def _paint_particles(self, painter: QPainter) -> None:
        for particle in self._particles:
            size = int(particle.get("size") or 20)
            pixmap = self._particle_pixmap(str(particle.get("file") or ""), size)
            painter.save()
            painter.setOpacity(max(0.15, min(1.0, float(particle.get("opacity") or 0.8))))
            painter.translate(float(particle.get("x") or 0.0), float(particle.get("y") or 0.0))
            painter.rotate(float(particle.get("rotation") or 0.0))
            if not pixmap.isNull():
                painter.drawPixmap(QRect(-size // 2, -size // 2, size, size), pixmap)
            else:
                self._paint_fallback_particle(painter, size)
            painter.restore()

    def _paint_fallback_particle(self, painter: QPainter, size: int) -> None:
        painter.setPen(QPen(QColor(245, 250, 255, 210), max(1, int(size / 9))))
        radius = size / 2.2
        for angle in range(0, 180, 45):
            painter.save()
            painter.rotate(angle)
            painter.drawLine(QPointF(-radius, 0), QPointF(radius, 0))
            painter.restore()

    def _paint_trail(self, painter: QPainter) -> None:
        if not self._trail:
            return
        for item in self._trail:
            ttl = max(0.0, min(0.55, float(item.get("ttl") or 0.0)))
            power = max(0.0, min(1.0, float(item.get("power") or 0.0)))
            alpha = int(95 * (ttl / 0.55) * power)
            if alpha <= 1:
                continue
            painter.setPen(QPen(QColor(180, 220, 255, alpha), 2.0 + power * 4.0, Qt.SolidLine, Qt.RoundCap))
            x = float(item.get("x") or 0.0)
            y = float(item.get("y") or 0.0)
            painter.drawArc(QRectF(x - 24, y - 16, 48, 32), 20 * 16, 250 * 16)

    def _paint_drifts(self, painter: QPainter, target: QRect) -> None:
        event = self._event or {}
        snowdrifts = event.get("snowdrifts") or {}
        if not bool(snowdrifts.get("enabled", True)):
            return
        bottom_bins = self._ensure_bottom_bins(target)
        self._paint_bin_drift(
            painter,
            bottom_bins,
            QRectF(target.left(), target.bottom() - 1, target.width(), 1),
            bottom=True,
        )
        for surface in self._surfaces:
            bins = self._surface_bins.get(str(surface.get("key") or ""))
            if bins:
                self._paint_bin_drift(painter, bins, surface["rect"], bottom=False)

    def _paint_bin_drift(self, painter: QPainter, bins: list[float], baseline: QRectF, *, bottom: bool) -> None:
        if not bins or max(bins, default=0.0) <= 0.25:
            return
        width = max(1.0, baseline.width())
        step = width / len(bins)
        path = QPainterPath()
        if bottom:
            path.moveTo(baseline.left(), baseline.top() + 4)
            for index, height in enumerate(bins):
                x = baseline.left() + index * step
                path.lineTo(x, baseline.top() - float(height))
            path.lineTo(baseline.right(), baseline.top() + 4)
            path.closeSubpath()
            painter.fillPath(path, QColor(246, 251, 255, 172))
            painter.setPen(QPen(QColor(255, 255, 255, 128), 1.4))
            painter.drawPath(path)
            return

        max_height = max(bins)
        rect = QRectF(baseline.left(), baseline.top() - max_height - 1, baseline.width(), max_height + 4)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(248, 252, 255, 94))
        painter.drawRoundedRect(rect, 5, 5)
        painter.setPen(QPen(QColor(255, 255, 255, 112), 1.0))
        painter.drawLine(QPointF(rect.left(), baseline.top()), QPointF(rect.right(), baseline.top()))

    def _particle_pixmap(self, file_name: str, size: int) -> QPixmap:
        key = (file_name, size)
        cached = self._pixmap_cache.get(key)
        if cached is not None:
            return cached
        path = decor_file_path(file_name) if file_name else ""
        pixmap = QPixmap(path) if path else QPixmap()
        if not pixmap.isNull():
            pixmap = pixmap.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        else:
            pixmap = _fallback_snowflake_pixmap(size)
        self._pixmap_cache[key] = pixmap
        return pixmap


class DecorPreviewFrame(QFrame):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMinimumHeight(160)
        self.setObjectName("DecorPreviewFrame")
        self.setStyleSheet(
            """
            QFrame#DecorPreviewFrame {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #eef6fb, stop:1 #f9fbfd);
                border: 1px solid #c7d1da;
                border-radius: 8px;
            }
            """
        )
        self.overlay = DecorOverlayWidget(self, forced_event=None)
        self.overlay.setGeometry(self.rect())

    def set_event(self, event: dict[str, Any] | None) -> None:
        self.overlay.set_forced_event(event)

    def resizeEvent(self, event):
        self.overlay.setGeometry(self.rect())
        self.overlay.raise_()
        super().resizeEvent(event)


def _fallback_snowflake_pixmap(size: int) -> QPixmap:
    image = QImage(max(8, size), max(8, size), QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.translate(image.width() / 2, image.height() / 2)
    pen = QPen(QColor(245, 251, 255, 220), max(1, int(size / 11)))
    painter.setPen(pen)
    radius = size * 0.38
    for angle in range(0, 180, 30):
        painter.save()
        painter.rotate(angle)
        painter.drawLine(QPointF(-radius, 0), QPointF(radius, 0))
        painter.drawLine(QPointF(radius * 0.55, 0), QPointF(radius * 0.34, radius * 0.16))
        painter.drawLine(QPointF(radius * 0.55, 0), QPointF(radius * 0.34, -radius * 0.16))
        painter.restore()
    painter.end()
    return QPixmap.fromImage(image)


def _int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)
