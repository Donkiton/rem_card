from __future__ import annotations

import json
import math
import random
import time
import weakref
from collections import OrderedDict
from typing import Any, Callable

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QAbstractButton, QCheckBox, QFrame, QRadioButton, QStackedWidget, QWidget

from rem_card.ui.shared.decor_settings import (
    active_decor_event,
    decor_file_path,
    normalize_decor_event,
)


ContextProvider = Callable[[], dict[str, Any]]
TargetProvider = Callable[[dict[str, Any]], QWidget | None]
_INTENSITY_BASELINE_PERCENT = 30.0
_INTENSITY_SCALE = 100.0 / _INTENSITY_BASELINE_PERCENT
_MAX_PARTICLES = 420
_MAX_FALLING_DRIFT_CHUNKS = 360
_SURFACE_RELEASE_CHUNK_LIMIT = 22
_SURFACE_SCAN_INTERVAL_SEC = 0.18
_SURFACE_SCAN_FALLBACK_SEC = 2.4
_MAX_BUTTON_SURFACES = 36
_SURFACE_WIND_CHUNK_FRAME_LIMIT = 48
_PIXMAP_CACHE_LIMIT = 96
_PIXMAP_SIZE_STEP = 4
_DRIFT_PATH_CACHE_LIMIT = 96
_ACTIVE_EVENT_CACHE_TTL_SEC = 1.0
_ACTIVE_EVENT_CACHE: dict[str, Any] = {"timestamp": 0.0, "event": None}


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
        self._pixmap_cache: OrderedDict[tuple[str, int], QPixmap] = OrderedDict()
        self._drift_path_cache: OrderedDict[tuple[Any, ...], QPainterPath] = OrderedDict()
        self._bottom_bins: list[float] = []
        self._surface_bins: dict[str, list[float]] = {}
        self._surface_rects: dict[str, QRectF] = {}
        self._surfaces: list[dict[str, Any]] = []
        self._falling_drift_chunks: list[dict[str, Any]] = []
        self._trail: list[dict[str, Any]] = []
        self._last_ts = time.monotonic()
        self._last_mouse_pos: QPointF | None = None
        self._last_surface_scan_ts = 0.0
        self._surface_scan_dirty = True
        self._surface_filter_widgets: weakref.WeakSet[QWidget] = weakref.WeakSet()
        self._frame_target_rect: QRect | None = None

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
        event_type = event.type()
        if obj is self.parent() and event_type in {QEvent.Resize, QEvent.Show, QEvent.LayoutRequest}:
            parent = self.parentWidget()
            if parent is not None:
                self.setGeometry(parent.rect())
                self.raise_()
            self._frame_target_rect = None
            self._mark_surface_scan_dirty()
        elif self._is_surface_filter_widget(obj) and event_type in {
            QEvent.Resize,
            QEvent.Move,
            QEvent.Show,
            QEvent.Hide,
            QEvent.LayoutRequest,
            QEvent.ChildAdded,
            QEvent.ChildRemoved,
            QEvent.Destroy,
        }:
            self._mark_surface_scan_dirty()
        return super().eventFilter(obj, event)

    def set_forced_event(self, event: dict[str, Any] | None) -> None:
        self._forced_event = normalize_decor_event(event) if event else None
        self.reload_settings()

    def reload_settings(self) -> None:
        try:
            event = self._forced_event or _cached_active_decor_event()
        except Exception:
            event = None
        if event is not None:
            event = normalize_decor_event(event)

        if event is None or not self._zone_enabled(event):
            self._event = None
            self._event_signature = ""
            self._particles.clear()
            self._falling_drift_chunks.clear()
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
            self._surface_rects.clear()
            self._surfaces.clear()
            self._falling_drift_chunks.clear()
            self._pixmap_cache.clear()
            self._drift_path_cache.clear()
            self._last_surface_scan_ts = 0.0
            self._surface_scan_dirty = True
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
        self._paint_falling_drifts(painter)
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
        target = self._resolve_target_rect()
        self._frame_target_rect = QRect(target)
        if target.width() <= 8 or target.height() <= 8:
            self._frame_target_rect = None
            return

        should_scan_surfaces = self._surface_scan_dirty or now - self._last_surface_scan_ts > _SURFACE_SCAN_FALLBACK_SEC
        if should_scan_surfaces and now - self._last_surface_scan_ts > _SURFACE_SCAN_INTERVAL_SEC:
            self._last_surface_scan_ts = now
            self._collect_surfaces(target)
        self._ensure_particle_count()
        mouse_pos, mouse_delta = self._mouse_state(target, dt)
        self._update_particles(target, dt, now, mouse_pos, mouse_delta)
        self._update_falling_drifts(target, dt)
        self._update_trail(dt)
        self._blow_drifts(mouse_pos, mouse_delta, dt)
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
        if self._frame_target_rect is not None:
            return QRect(self._frame_target_rect)
        return self._resolve_target_rect()

    def _resolve_target_rect(self) -> QRect:
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

    def _mark_surface_scan_dirty(self) -> None:
        self._surface_scan_dirty = True

    def _install_surface_event_filter(self, widget: QWidget) -> None:
        try:
            if widget in self._surface_filter_widgets:
                return
            widget.installEventFilter(self)
            self._surface_filter_widgets.add(widget)
        except Exception:
            pass

    def _is_surface_filter_widget(self, obj: Any) -> bool:
        try:
            return obj in self._surface_filter_widgets
        except Exception:
            return False

    def _desired_particle_count(self) -> int:
        event = self._event or self._forced_event or {}
        intensity = _effective_intensity(event.get("intensity"), 34)
        if intensity <= 0:
            return 0
        return max(4, min(_MAX_PARTICLES, int(8 + intensity * 1.15)))

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
            "vy": 0.0,
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

    def _update_particles(self, target: QRect, dt: float, now: float, mouse_pos: QPointF | None, mouse_delta: QPointF) -> None:
        event = self._event or {}
        intensity = _effective_intensity(event.get("intensity"), 34)
        wind_strength = _int(event.get("wind_strength"), 52) / 100.0
        surfaces = tuple(self._surfaces)
        snowdrifts = event.get("snowdrifts") or {}
        surface_chance = max(0.01, min(0.34, _int(snowdrifts.get("surface_intensity"), 35) / 190.0))
        for particle in self._particles:
            particle["previous_y"] = float(particle.get("y") or 0.0)
            size = float(particle.get("size") or 20.0)
            weight = float(particle.get("weight") or 1.0)
            fall_speed = (18 + weight * 34) * (0.72 + intensity / 140.0)
            drift = math.sin(now * 0.9 + float(particle.get("phase") or 0.0)) * (8 + intensity * 0.11)
            particle["vx"] = float(particle.get("vx") or 0.0) * 0.982 + drift * dt
            particle["vy"] = float(particle.get("vy") or 0.0) * 0.92

            if mouse_pos is not None:
                self._apply_cursor_wind(particle, mouse_pos, mouse_delta, wind_strength, dt)

            particle["x"] = float(particle.get("x") or 0.0) + float(particle.get("vx") or 0.0) * dt
            particle["y"] = float(particle.get("y") or 0.0) + (fall_speed + float(particle.get("vy") or 0.0)) * dt
            particle["rotation"] = float(particle.get("rotation") or 0.0) + float(particle.get("spin") or 0.0) * dt

            if float(particle["x"]) < target.left() - size:
                particle["x"] = target.right() + size
            elif float(particle["x"]) > target.right() + size:
                particle["x"] = target.left() - size

            settled = False
            for surface in surfaces:
                if self._particle_hits_surface(particle, surface, surface_chance):
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
        movement = min(120.0, math.hypot(mouse_delta.x(), mouse_delta.y()))
        if movement < 0.35 or wind_strength <= 0.0:
            return
        radius = 138 + wind_strength * 72
        if distance > radius:
            return
        factor = (1.0 - distance / radius) ** 2
        direction_x = mouse_delta.x() / max(movement, 1.0)
        direction_y = mouse_delta.y() / max(movement, 1.0)
        radial_x = dx / max(distance, 1.0)
        radial_y = dy / max(distance, 1.0)
        tangent_x = -dy / max(distance, 1.0)
        sweep = factor * (150.0 + movement * 18.0) * wind_strength
        particle["vx"] = float(particle.get("vx") or 0.0) + (
            direction_x * sweep + radial_x * sweep * 0.32 + tangent_x * sweep * 0.16
        ) * dt
        particle["vy"] = float(particle.get("vy") or 0.0) + (direction_y * sweep * 0.55 + radial_y * sweep * 0.24) * dt
        particle["spin"] = float(particle.get("spin") or 0.0) + (direction_x + tangent_x) * sweep * 0.7 * dt

    def _particle_hits_surface(self, particle: dict[str, Any], surface: dict[str, Any], surface_chance: float) -> bool:
        rect: QRectF = surface["rect"]
        x = float(particle.get("x") or 0.0)
        previous_bottom = float(particle.get("previous_y") or 0.0) + float(particle.get("size") or 0.0) * 0.5
        current_bottom = float(particle.get("y") or 0.0) + float(particle.get("size") or 0.0) * 0.5
        if not (rect.left() <= x <= rect.right()):
            return False
        surface_y = rect.top()
        if not (previous_bottom <= surface_y <= current_bottom):
            return False
        return random.random() < surface_chance

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
        movement = math.hypot(delta.x(), delta.y())
        if movement > 0.55:
            self._trail.append(
                {
                    "x": pos.x(),
                    "y": pos.y(),
                    "ttl": 0.68,
                    "max_ttl": 0.68,
                    "power": min(1.0, movement / 36.0),
                    "dx": delta.x() / max(movement, 1.0),
                    "dy": delta.y() / max(movement, 1.0),
                    "phase": random.uniform(-1.0, 1.0),
                }
            )
        return pos, delta

    def _update_trail(self, dt: float) -> None:
        for item in self._trail:
            item["ttl"] = float(item.get("ttl") or 0.0) - dt
        self._trail = [item for item in self._trail if float(item.get("ttl") or 0.0) > 0.0][-28:]

    def _collect_surfaces(self, target: QRect) -> None:
        root = self._surface_root_widget()
        if root is None:
            self._release_missing_surface_bins(set())
            self._surfaces = []
            self._surface_scan_dirty = False
            return
        self._install_surface_event_filter(root)
        surfaces: list[dict[str, Any]] = []
        for child in root.findChildren(QAbstractButton):
            if not _is_snow_surface_button(child):
                continue
            self._install_surface_event_filter(child)
            rect = self._button_surface_rect(child, target)
            if rect is None:
                continue
            key = f"button:{id(child)}"
            surfaces.append({"key": key, "rect": rect})
            if len(surfaces) >= _MAX_BUTTON_SURFACES:
                break
        self._surfaces = sorted(surfaces, key=lambda item: item["rect"].top())
        for surface in self._surfaces:
            key = str(surface["key"])
            self._surface_rects[key] = QRectF(surface["rect"])
            bins = max(8, min(80, int(surface["rect"].width() / 18)))
            if key not in self._surface_bins or len(self._surface_bins[key]) != bins:
                self._surface_bins[key] = [0.0 for _ in range(bins)]
        valid_keys = {str(surface["key"]) for surface in self._surfaces}
        self._release_missing_surface_bins(valid_keys)
        self._surface_scan_dirty = False

    def _button_surface_rect(self, button: QAbstractButton, target: QRect) -> QRectF | None:
        top_left = self.mapFromGlobal(button.mapToGlobal(QPoint(0, 0)))
        rect = QRect(top_left, button.size()).intersected(target)
        if rect.width() < 28 or rect.height() < 18:
            return None
        if rect.top() < target.top() + 8 or rect.top() > target.bottom() - 34:
            return None
        return QRectF(rect.left(), rect.top(), rect.width(), 5)

    def _release_missing_surface_bins(self, valid_keys: set[str]) -> None:
        for key in list(self._surface_bins):
            if key not in valid_keys:
                bins = self._surface_bins.pop(key, None)
                rect = self._surface_rects.pop(key, None)
                if rect is not None and bins:
                    self._release_surface_drift(rect, bins)
        for key in list(self._surface_rects):
            if key not in valid_keys:
                self._surface_rects.pop(key, None)

    def _release_surface_drift(self, rect: QRectF, bins: list[float]) -> None:
        event = self._event or {}
        snowdrifts = event.get("snowdrifts") or {}
        if not bool(snowdrifts.get("enabled", True)):
            return
        self._falling_drift_chunks.extend(_surface_drift_chunks_from_bins(rect, bins))
        overflow = len(self._falling_drift_chunks) - _MAX_FALLING_DRIFT_CHUNKS
        if overflow > 0:
            for chunk in self._falling_drift_chunks[:overflow]:
                self._deposit_falling_drift(
                    float(chunk.get("x") or 0.0),
                    float(chunk.get("amount") or 0.0),
                    float(chunk.get("phase") or 0.0),
                )
            del self._falling_drift_chunks[:overflow]

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
        _deposit_weighted_drift(
            bins,
            index,
            amount * accumulation * 2.26,
            float(max_height),
            ((0, 1.0), (-1, 0.45), (1, 0.45), (-2, 0.18), (2, 0.18)),
            roughness_seed=0.11,
        )

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
        max_height = _int(snowdrifts.get("max_height"), 42) * 0.24
        accumulation = _int(snowdrifts.get("accumulation"), 38) / 100.0
        index = max(0, min(len(bins) - 1, int((x - rect.left()) / max(rect.width(), 1.0) * len(bins))))
        _deposit_weighted_drift(
            bins,
            index,
            amount * accumulation * 0.84,
            float(max_height),
            ((0, 0.8), (-1, 0.32), (1, 0.32), (-2, 0.12), (2, 0.12)),
            roughness_seed=4.7,
        )

    def _blow_drifts(self, mouse_pos: QPointF | None, mouse_delta: QPointF, dt: float) -> None:
        if mouse_pos is None:
            return
        movement = math.hypot(mouse_delta.x(), mouse_delta.y())
        if movement < 0.4:
            return
        event = self._event or {}
        snowdrifts = event.get("snowdrifts") or {}
        if not bool(snowdrifts.get("enabled", True)):
            return
        wind_strength = _int(event.get("wind_strength"), 52) / 100.0
        if wind_strength <= 0.0:
            return
        direction_x = mouse_delta.x() / max(movement, 1.0)
        radius = 126 + wind_strength * 92
        power = min(5.8, movement / 10.0) * wind_strength * dt * 34.0
        target = self._target_rect()
        bins = self._ensure_bottom_bins(target)
        _transport_drift_bins(
            bins,
            left=float(target.left()),
            y=float(target.bottom()),
            width=float(target.width()),
            mouse_pos=mouse_pos,
            radius=radius,
            power=power,
            direction_x=direction_x,
            max_height=float(_int(snowdrifts.get("max_height"), 42)) * 1.12,
        )
        remaining_wind_chunks = _SURFACE_WIND_CHUNK_FRAME_LIMIT
        for surface in self._surfaces:
            if remaining_wind_chunks <= 0:
                break
            key = str(surface.get("key") or "")
            rect: QRectF = surface["rect"]
            surface_bins = self._surface_bins.get(key)
            if surface_bins:
                chunks = _wind_blown_surface_chunks_from_bins(
                    surface_bins,
                    rect,
                    mouse_pos=mouse_pos,
                    radius=radius * 0.76,
                    power=power * 0.82,
                    direction_x=direction_x,
                    max_chunks=remaining_wind_chunks,
                )
                self._falling_drift_chunks.extend(chunks)
                remaining_wind_chunks -= len(chunks)
        overflow = len(self._falling_drift_chunks) - _MAX_FALLING_DRIFT_CHUNKS
        if overflow > 0:
            for chunk in self._falling_drift_chunks[:overflow]:
                self._deposit_falling_drift(
                    float(chunk.get("x") or 0.0),
                    float(chunk.get("amount") or 0.0),
                    float(chunk.get("phase") or 0.0),
                )
            del self._falling_drift_chunks[:overflow]

    def _update_falling_drifts(self, target: QRect, dt: float) -> None:
        if not self._falling_drift_chunks:
            return
        remaining: list[dict[str, Any]] = []
        for chunk in self._falling_drift_chunks:
            size = float(chunk.get("size") or 12.0)
            chunk["vy"] = float(chunk.get("vy") or 0.0) + (330.0 + size * 11.0) * dt
            chunk["vx"] = float(chunk.get("vx") or 0.0) * 0.982
            chunk["x"] = float(chunk.get("x") or 0.0) + float(chunk.get("vx") or 0.0) * dt
            chunk["y"] = float(chunk.get("y") or 0.0) + float(chunk.get("vy") or 0.0) * dt
            chunk["rotation"] = float(chunk.get("rotation") or 0.0) + float(chunk.get("spin") or 0.0) * dt
            if float(chunk["x"]) < target.left() - size:
                chunk["x"] = target.right() + size
            elif float(chunk["x"]) > target.right() + size:
                chunk["x"] = target.left() - size
            if float(chunk["y"]) >= target.bottom() - size * 0.25:
                self._deposit_falling_drift(
                    float(chunk.get("x") or 0.0),
                    float(chunk.get("amount") or 0.0),
                    float(chunk.get("phase") or 0.0),
                )
                continue
            remaining.append(chunk)
        self._falling_drift_chunks = remaining

    def _deposit_falling_drift(self, x: float, amount: float, seed: float = 0.0) -> None:
        if amount <= 0.0:
            return
        event = self._event or {}
        snowdrifts = event.get("snowdrifts") or {}
        if not bool(snowdrifts.get("enabled", True)):
            return
        target = self._target_rect()
        bins = self._ensure_bottom_bins(target)
        if not bins:
            return
        max_height = float(_int(snowdrifts.get("max_height"), 42)) * 1.12
        index = max(0, min(len(bins) - 1, int((x - target.left()) / max(target.width(), 1) * len(bins))))
        center_shift = int(round((_pseudo_unit(seed, 0.17) - 0.5) * 3.0))
        profile = _falling_drift_deposit_profile(seed)
        _deposit_weighted_drift(
            bins,
            index + center_shift,
            float(amount),
            max_height,
            profile,
            roughness_seed=seed + 9.0,
        )

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

    def _paint_falling_drifts(self, painter: QPainter) -> None:
        if not self._falling_drift_chunks:
            return
        for chunk in self._falling_drift_chunks:
            size = float(chunk.get("size") or 12.0)
            painter.save()
            painter.setOpacity(max(0.22, min(0.92, float(chunk.get("opacity") or 0.7))))
            painter.translate(float(chunk.get("x") or 0.0), float(chunk.get("y") or 0.0))
            painter.rotate(float(chunk.get("rotation") or 0.0))
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(79, 134, 170, 150))
            painter.drawEllipse(QPointF(0, 1.4), size * 0.52, size * 0.3)
            painter.setBrush(QColor(225, 246, 255, 218))
            painter.drawEllipse(QPointF(-size * 0.08, -size * 0.04), size * 0.42, size * 0.22)
            painter.setPen(QPen(QColor(250, 254, 255, 180), max(1.0, size * 0.08), Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(QPointF(-size * 0.26, -size * 0.08), QPointF(size * 0.22, -size * 0.1))
            painter.restore()

    def _paint_fallback_particle(self, painter: QPainter, size: int) -> None:
        radius = size / 2.2
        for color, width_scale in ((QColor(72, 124, 158, 170), 6.5), (QColor(250, 254, 255, 235), 11.0)):
            painter.setPen(QPen(color, max(1, int(size / width_scale)), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            for angle in range(0, 180, 45):
                painter.save()
                painter.rotate(angle)
                painter.drawLine(QPointF(-radius, 0), QPointF(radius, 0))
                painter.restore()

    def _paint_trail(self, painter: QPainter) -> None:
        if not self._trail:
            return
        for item in self._trail:
            max_ttl = max(0.1, float(item.get("max_ttl") or 0.68))
            ttl = max(0.0, min(max_ttl, float(item.get("ttl") or 0.0)))
            power = max(0.0, min(1.0, float(item.get("power") or 0.0)))
            fade = ttl / max_ttl
            alpha = int(115 * fade * power)
            if alpha <= 2:
                continue
            x = float(item.get("x") or 0.0)
            y = float(item.get("y") or 0.0)
            dx = float(item.get("dx") or 1.0)
            dy = float(item.get("dy") or 0.0)
            length = math.hypot(dx, dy)
            dx, dy = (dx / length, dy / length) if length > 0.001 else (1.0, 0.0)
            nx, ny = -dy, dx
            age = 1.0 - fade
            base_length = 30.0 + power * 72.0
            phase = float(item.get("phase") or 0.0)
            for lane, lane_alpha in ((-1, 0.46), (0, 1.0), (1, 0.58)):
                offset = (lane * (7.0 + power * 10.0)) + math.sin(age * math.tau + phase + lane) * 4.0
                wave = (10.0 + power * 16.0) * (1 if lane <= 0 else -1)
                start = QPointF(x - dx * base_length * 0.72 + nx * offset, y - dy * base_length * 0.72 + ny * offset)
                ctrl1 = QPointF(
                    x - dx * base_length * 0.28 + nx * (offset + wave),
                    y - dy * base_length * 0.28 + ny * (offset + wave),
                )
                ctrl2 = QPointF(
                    x + dx * base_length * 0.18 + nx * (offset - wave * 0.55),
                    y + dy * base_length * 0.18 + ny * (offset - wave * 0.55),
                )
                end = QPointF(x + dx * base_length * 0.42 + nx * offset * 0.35, y + dy * base_length * 0.42 + ny * offset * 0.35)
                path = QPainterPath(start)
                path.cubicTo(ctrl1, ctrl2, end)
                painter.setPen(
                    QPen(
                        QColor(82, 153, 196, int(alpha * lane_alpha * 0.72)),
                        3.0 + power * 5.5,
                        Qt.SolidLine,
                        Qt.RoundCap,
                        Qt.RoundJoin,
                    )
                )
                painter.drawPath(path)
                painter.setPen(
                    QPen(
                        QColor(238, 250, 255, int(alpha * lane_alpha * 0.52)),
                        1.0 + power * 2.0,
                        Qt.SolidLine,
                        Qt.RoundCap,
                        Qt.RoundJoin,
                    )
                )
                painter.drawPath(path)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(95, 165, 202, int(alpha * 0.34)))
            for dot_index in range(3):
                dot_phase = phase + dot_index * 1.7
                dot_offset = math.sin(dot_phase) * (9.0 + power * 8.0)
                dot_back = base_length * (0.58 - dot_index * 0.18)
                painter.drawEllipse(
                    QPointF(x - dx * dot_back + nx * dot_offset, y - dy * dot_back + ny * dot_offset),
                    1.5 + power * 1.8,
                    1.5 + power * 1.8,
                )

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
        path = self._cached_drift_path(bins, baseline, bottom=bottom)
        if bottom:
            painter.fillPath(path, QColor(114, 176, 211, 196))
            painter.setPen(QPen(QColor(79, 134, 170, 216), 1.5))
            painter.drawPath(path)
            painter.setPen(QPen(QColor(248, 253, 255, 218), 0.9))
            painter.drawPath(path)
            return

        painter.fillPath(path, QColor(114, 176, 211, 172))
        painter.setPen(QPen(QColor(79, 134, 170, 196), 1.2))
        painter.drawPath(path)
        painter.setPen(QPen(QColor(248, 253, 255, 206), 0.9))
        painter.drawPath(path)

    def _cached_drift_path(self, bins: list[float], baseline: QRectF, *, bottom: bool) -> QPainterPath:
        max_height = max(bins)
        signature = (
            id(bins),
            bool(bottom),
            round(float(baseline.left()), 1),
            round(float(baseline.top()), 1),
            round(float(baseline.width()), 1),
            len(bins),
            tuple(int(max(0.0, float(height)) * 4.0) for height in bins),
        )
        cached = self._drift_path_cache.get(signature)
        if cached is not None:
            self._drift_path_cache.move_to_end(signature)
            return cached

        roughness_seed = 0.0 if bottom else baseline.left() * 0.013
        visual_heights = [
            _visual_drift_height(float(height), index, float(max_height), roughness_seed)
            for index, height in enumerate(bins)
        ]
        path = _smooth_drift_path(
            baseline,
            visual_heights,
            bottom_padding=4.0 if bottom else 2.0,
            top_smoothing=0.34 if bottom else 0.42,
        )
        self._drift_path_cache[signature] = path
        self._drift_path_cache.move_to_end(signature)
        while len(self._drift_path_cache) > _DRIFT_PATH_CACHE_LIMIT:
            self._drift_path_cache.popitem(last=False)
        return path

    def _particle_pixmap(self, file_name: str, size: int) -> QPixmap:
        cached_size = _quantized_particle_size(size)
        key = (file_name, cached_size)
        cached = self._pixmap_cache.get(key)
        if cached is not None:
            self._pixmap_cache.move_to_end(key)
            return cached
        path = decor_file_path(file_name) if file_name else ""
        pixmap = QPixmap(path) if path else QPixmap()
        if not pixmap.isNull():
            pixmap = pixmap.scaled(cached_size, cached_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        else:
            pixmap = _fallback_snowflake_pixmap(cached_size)
        self._pixmap_cache[key] = pixmap
        self._pixmap_cache.move_to_end(key)
        while len(self._pixmap_cache) > _PIXMAP_CACHE_LIMIT:
            self._pixmap_cache.popitem(last=False)
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
    radius = size * 0.38
    for color, width_scale in ((QColor(72, 124, 158, 170), 6.5), (QColor(250, 254, 255, 235), 11.0)):
        pen = QPen(color, max(1, int(size / width_scale)), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        painter.setPen(pen)
        for angle in range(0, 180, 30):
            painter.save()
            painter.rotate(angle)
            painter.drawLine(QPointF(-radius, 0), QPointF(radius, 0))
            painter.drawLine(QPointF(radius * 0.55, 0), QPointF(radius * 0.34, radius * 0.16))
            painter.drawLine(QPointF(radius * 0.55, 0), QPointF(radius * 0.34, -radius * 0.16))
            painter.restore()
    painter.end()
    return QPixmap.fromImage(image)


def _effective_intensity(value: Any, default: int) -> float:
    return max(0.0, float(_int(value, default)) * _INTENSITY_SCALE)


def _quantized_particle_size(size: int) -> int:
    normalized = max(8, int(size or 0))
    return max(8, ((normalized + _PIXMAP_SIZE_STEP // 2) // _PIXMAP_SIZE_STEP) * _PIXMAP_SIZE_STEP)


def _cached_active_decor_event() -> dict[str, Any] | None:
    now = time.monotonic()
    cached_at = float(_ACTIVE_EVENT_CACHE.get("timestamp") or 0.0)
    if now - cached_at <= _ACTIVE_EVENT_CACHE_TTL_SEC:
        return _ACTIVE_EVENT_CACHE.get("event")
    event = active_decor_event()
    _ACTIVE_EVENT_CACHE["timestamp"] = now
    _ACTIVE_EVENT_CACHE["event"] = event
    return event


def _is_snow_surface_button(widget: QAbstractButton) -> bool:
    if isinstance(widget, (QCheckBox, QRadioButton)):
        return False
    if not widget.isVisible():
        return False
    if widget.width() < 28 or widget.height() < 18:
        return False
    return True


def _surface_drift_chunks_from_bins(rect: QRectF, bins: list[float]) -> list[dict[str, Any]]:
    if not bins:
        return []
    active = [(index, max(0.0, float(value or 0.0))) for index, value in enumerate(bins)]
    active = [(index, value) for index, value in active if value > 0.25]
    if not active:
        return []

    group_size = max(2, math.ceil(len(active) / _SURFACE_RELEASE_CHUNK_LIMIT))
    step = max(1.0, float(rect.width()) / len(bins))
    chunks: list[dict[str, Any]] = []
    for group_start in range(0, len(active), group_size):
        group = active[group_start : group_start + group_size]
        amount = sum(value for _, value in group)
        if amount <= 0.25:
            continue
        weighted_index = sum((index + 0.5) * value for index, value in group) / amount
        peak = max(value for _, value in group)
        seed = _pseudo_unit(rect.left(), rect.top(), group_start, amount)
        side_seed = _pseudo_unit(seed, amount, weighted_index)
        x_jitter = (seed - 0.5) * step * max(1.0, group_size * 1.4)
        x = float(rect.left()) + weighted_index * step + x_jitter
        x = max(float(rect.left()), min(float(rect.right()), x))
        size = max(10.0, min(36.0, 8.0 + math.sqrt(amount) * 5.2))
        start_lift = 5.0 + peak * (0.8 + side_seed * 0.7) + _pseudo_unit(seed, 11.0) * 28.0
        chunks.append(
            {
                "x": x,
                "y": float(rect.top()) - start_lift,
                "vx": (seed - 0.5) * 92.0,
                "vy": 118.0 + math.sqrt(amount) * 18.0 + _pseudo_unit(seed, 23.0) * 72.0,
                "size": size,
                "amount": amount,
                "rotation": (side_seed - 0.5) * 52.0,
                "spin": (seed - 0.5) * 110.0,
                "phase": seed,
                "opacity": max(0.46, min(0.88, 0.48 + amount / 18.0)),
            }
        )
    return chunks


def _falling_drift_deposit_profile(seed: float) -> tuple[tuple[int, float], ...]:
    center = 0.31 + _pseudo_unit(seed, 1.0) * 0.22
    left = 0.12 + _pseudo_unit(seed, 2.0) * 0.14
    right = 0.12 + _pseudo_unit(seed, 3.0) * 0.14
    far_left = 0.05 + _pseudo_unit(seed, 4.0) * 0.07
    far_right = 0.05 + _pseudo_unit(seed, 5.0) * 0.07
    tail = max(0.0, 1.0 - center - left - right - far_left - far_right)
    profile = ((0, center), (-1, left), (1, right), (-2, far_left), (2, far_right), (3, tail * 0.58), (-3, tail * 0.42))
    total = sum(fraction for _, fraction in profile)
    if total <= 0.0:
        return ((0, 1.0),)
    return tuple((offset, fraction / total) for offset, fraction in profile)


def _pseudo_unit(*values: float) -> float:
    seed = 0.0
    for index, value in enumerate(values, start=1):
        seed += float(value) * (12.9898 + index * 7.233)
    return math.sin(seed) * 43758.5453 % 1.0


def _wind_blown_surface_chunks_from_bins(
    bins: list[float],
    rect: QRectF,
    *,
    mouse_pos: QPointF,
    radius: float,
    power: float,
    direction_x: float,
    max_chunks: int | None = None,
) -> list[dict[str, Any]]:
    if not bins or radius <= 0.0 or power <= 0.0 or rect.width() <= 0.0:
        return []
    step = max(1.0, float(rect.width()) / len(bins))
    chunks: list[dict[str, Any]] = []
    for index, value in enumerate(list(bins)):
        if max_chunks is not None and len(chunks) >= max(0, max_chunks):
            break
        if value <= 0.03:
            continue
        x = float(rect.left()) + (index + 0.5) * step
        distance = math.hypot(x - mouse_pos.x(), float(rect.top()) - mouse_pos.y())
        if distance > radius:
            continue
        falloff = (1.0 - distance / radius) ** 2
        amount = min(float(value), power * falloff * (0.55 + min(1.0, float(value) / 8.0) * 0.45))
        if amount <= 0.04:
            continue
        bins[index] = max(0.0, float(bins[index]) - amount)

        seed = _pseudo_unit(rect.left(), rect.top(), index, amount, direction_x)
        side = _pseudo_unit(seed, 17.0) - 0.5
        size = max(7.0, min(24.0, 5.8 + math.sqrt(amount) * 5.0))
        chunks.append(
            {
                "x": x + side * step * 0.75,
                "y": float(rect.top()) - max(2.0, float(value) * 0.45) - seed * 10.0,
                "vx": direction_x * (70.0 + power * 18.0) + side * 70.0,
                "vy": 96.0 + seed * 86.0 + math.sqrt(amount) * 16.0,
                "size": size,
                "amount": amount,
                "rotation": side * 56.0,
                "spin": side * 130.0,
                "phase": seed,
                "opacity": max(0.4, min(0.82, 0.44 + amount / 9.0)),
            }
        )
    return chunks


def _transport_drift_bins(
    bins: list[float],
    *,
    left: float,
    y: float,
    width: float,
    mouse_pos: QPointF,
    radius: float,
    power: float,
    direction_x: float,
    max_height: float,
) -> None:
    if not bins or radius <= 0.0 or power <= 0.0 or width <= 0.0:
        return
    capacity_limit = max(0.0, float(max_height))
    if capacity_limit <= 0.0:
        return

    source = [max(0.0, float(value or 0.0)) for value in bins]
    result = list(source)
    step = width / len(source)
    for index, value in enumerate(source):
        if value <= 0.01:
            continue
        x = left + (index + 0.5) * step
        distance = math.hypot(x - mouse_pos.x(), y - mouse_pos.y())
        if distance > radius:
            continue
        falloff = (1.0 - distance / radius) ** 2
        amount = min(value, power * falloff * (0.65 + min(1.0, value / capacity_limit) * 0.35))
        if amount <= 0.01:
            continue

        taken = min(amount, result[index])
        if taken <= 0.01:
            continue
        result[index] -= taken

        effective_direction = direction_x
        if abs(effective_direction) < 0.18:
            effective_direction = 0.55 if x >= mouse_pos.x() else -0.55
        sign = 1 if effective_direction >= 0.0 else -1
        travel = max(1, int(round(abs(effective_direction) * (2.0 + falloff * 5.0))))
        center = max(0, min(len(result) - 1, index + sign * travel))
        remaining = taken

        for target_index, fraction in (
            (center, 0.58),
            (max(0, min(len(result) - 1, center + sign)), 0.27),
            (max(0, min(len(result) - 1, center - sign)), 0.15),
        ):
            quota = taken * fraction
            added = _deposit_drift_amount(result, target_index, quota, capacity_limit)
            remaining -= added

        search_limit = min(len(result), 10)
        for offset in range(2, search_limit):
            if remaining <= 0.01:
                break
            target_index = max(0, min(len(result) - 1, center + sign * offset))
            added = _deposit_drift_amount(result, target_index, remaining, capacity_limit)
            remaining -= added

        if remaining > 0.0:
            result[index] += remaining

    bins[:] = [max(0.0, value) for value in result]


def _deposit_weighted_drift(
    bins: list[float],
    center_index: int,
    amount: float,
    capacity_limit: float,
    profile: tuple[tuple[int, float], ...],
    *,
    roughness_seed: float,
) -> float:
    if amount <= 0.0 or not bins or capacity_limit <= 0.0:
        return 0.0
    deposited = 0.0
    total_weight = sum(max(0.0, fraction) for _, fraction in profile)
    if total_weight <= 0.0:
        return 0.0
    for offset, fraction in profile:
        if fraction <= 0.0:
            continue
        pos = center_index + offset
        if 0 <= pos < len(bins):
            deposited += _deposit_drift_amount(
                bins,
                pos,
                amount * (fraction / total_weight),
                capacity_limit,
                roughness_seed=roughness_seed,
            )

    remaining = max(0.0, amount - deposited)
    for distance in range(1, len(bins)):
        if remaining <= 0.01:
            break
        for pos in (center_index - distance, center_index + distance):
            if 0 <= pos < len(bins):
                added = _deposit_drift_amount(
                    bins,
                    pos,
                    remaining,
                    capacity_limit,
                    roughness_seed=roughness_seed,
                )
                deposited += added
                remaining -= added
                if remaining <= 0.01:
                    break
    return deposited


def _deposit_drift_amount(
    bins: list[float],
    index: int,
    amount: float,
    capacity_limit: float,
    *,
    roughness_seed: float = 0.0,
) -> float:
    if amount <= 0.0 or not (0 <= index < len(bins)):
        return 0.0
    capacity = max(0.0, _drift_bin_capacity(index, capacity_limit, roughness_seed) - bins[index])
    added = min(amount, capacity)
    if added > 0.0:
        bins[index] += added
    return added


def _drift_bin_capacity(index: int, capacity_limit: float, roughness_seed: float = 0.0) -> float:
    if capacity_limit <= 0.0:
        return 0.0
    wave = math.sin(index * 0.61 + roughness_seed * 2.7) * 0.14
    ridge = math.sin(index * 0.19 + roughness_seed * 5.1) * 0.08
    grain = (_pseudo_unit(index, roughness_seed, 37.0) - 0.5) * 0.13
    factor = max(0.58, min(1.0, 0.84 + wave + ridge + grain))
    return capacity_limit * factor


def _visual_drift_height(height: float, index: int, max_height: float, roughness_seed: float = 0.0) -> float:
    if height <= 0.0 or max_height <= 0.0:
        return max(0.0, height)
    cap = _drift_bin_capacity(index, max_height, roughness_seed)
    if height > cap:
        return cap
    fullness = height / max(max_height, 1.0)
    if fullness < 0.55:
        return height
    lift = 1.0 - (_pseudo_unit(index, roughness_seed, 71.0) * 0.12 * fullness)
    return min(height, max(0.0, cap * 1.02), height * lift)


def _smooth_drift_path(
    baseline: QRectF,
    heights: list[float],
    *,
    bottom_padding: float,
    top_smoothing: float,
) -> QPainterPath:
    bottom_y = baseline.top() + float(bottom_padding)
    path = QPainterPath(QPointF(baseline.left(), bottom_y))
    if not heights:
        path.lineTo(QPointF(baseline.right(), bottom_y))
        path.closeSubpath()
        return path

    rounded_heights = _rounded_drift_heights(heights, top_smoothing)
    step = max(1.0, baseline.width() / len(rounded_heights))
    points = [
        QPointF(
            baseline.left() + min(baseline.width(), (index + 0.5) * step),
            baseline.top() - max(0.0, float(height)),
        )
        for index, height in enumerate(rounded_heights)
    ]
    if not points:
        path.lineTo(QPointF(baseline.right(), bottom_y))
        path.closeSubpath()
        return path

    path.lineTo(QPointF(baseline.left(), points[0].y()))
    previous = QPointF(baseline.left(), points[0].y())
    for point in points:
        midpoint = QPointF((previous.x() + point.x()) * 0.5, (previous.y() + point.y()) * 0.5)
        path.quadTo(previous, midpoint)
        previous = point
    right_top = QPointF(baseline.right(), points[-1].y())
    midpoint = QPointF((previous.x() + right_top.x()) * 0.5, (previous.y() + right_top.y()) * 0.5)
    path.quadTo(previous, midpoint)
    path.quadTo(right_top, right_top)
    path.lineTo(QPointF(baseline.right(), bottom_y))
    path.closeSubpath()
    return path


def _rounded_drift_heights(heights: list[float], smoothing: float) -> list[float]:
    if len(heights) < 3:
        return [max(0.0, float(height)) for height in heights]
    amount = max(0.0, min(0.8, float(smoothing)))
    rounded: list[float] = []
    for index, raw_height in enumerate(heights):
        height = max(0.0, float(raw_height))
        previous = max(0.0, float(heights[index - 1])) if index > 0 else height
        next_height = max(0.0, float(heights[index + 1])) if index < len(heights) - 1 else height
        neighbor_average = previous * 0.28 + height * 0.44 + next_height * 0.28
        rounded.append(height * (1.0 - amount) + neighbor_average * amount)
    return rounded


def _int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)
