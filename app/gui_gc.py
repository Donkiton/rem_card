"""Keep Python cyclic garbage collection on the Qt GUI thread.

Reference counting is unchanged. Workers must never own the last reference to
a GUI object or call gc.collect() themselves; callbacks diagnose, not intercept,
explicit collection by third-party code.
"""
from __future__ import annotations

import gc
import threading
import time
import traceback
from collections import Counter
from pathlib import Path

from PySide6 import __version__ as pyside_version
from PySide6.QtCore import QEvent, QObject, QThread, QTimer, qVersion


POLL_INTERVAL_MS = 1000
FULL_COLLECTION_INTERVAL_SEC = 60.0
SLOW_COLLECTION_MS = 100.0
USER_IDLE_SEC = 5.0
DEFERRED_WARNING_SEC = 60.0


_USER_ACTIVITY_EVENTS = frozenset({
    QEvent.Type.KeyPress,
    QEvent.Type.KeyRelease,
    QEvent.Type.MouseButtonDblClick,
    QEvent.Type.MouseButtonPress,
    QEvent.Type.MouseButtonRelease,
    QEvent.Type.MouseMove,
    QEvent.Type.TabletMove,
    QEvent.Type.TabletPress,
    QEvent.Type.TabletRelease,
    QEvent.Type.TouchBegin,
    QEvent.Type.TouchEnd,
    QEvent.Type.TouchUpdate,
    QEvent.Type.Wheel,
})


def disable_automatic_gui_gc() -> None:
    # Call before importing runtime services: their loggers can start workers.
    gc.disable()


class GuiGarbageCollector(QObject):
    def __init__(self, application, logger):
        if QThread.currentThread() != application.thread():
            raise RuntimeError("GUI garbage collector must be installed on the GUI thread")
        super().__init__(application)
        gc.disable()
        self._application = application
        self._logger = logger
        self._gui_native_id = threading.get_native_id()
        self._collecting = False
        self._gc_active = False
        self._closed = False
        self._sequence = 0
        self._minor_collections = 0
        self._last_full = time.monotonic()
        self._last_user_activity = float("-inf")
        self._deferred_since = None
        self._last_deferred_warning = 0.0
        self._callback = self._observe_gc
        gc.callbacks.append(self._callback)
        application.installEventFilter(self)
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll)
        application.aboutToQuit.connect(self.close)
        self._timer.start()
        self._log("info", "installed", qt=qVersion(), pyside=pyside_version,
                  automatic_gc=False, threshold=gc.get_threshold(),
                  poll_ms=POLL_INTERVAL_MS, full_interval_sec=FULL_COLLECTION_INTERVAL_SEC,
                  user_idle_sec=USER_IDLE_SEC)

    def eventFilter(self, watched, event):
        """Record input with a constant-time application event filter."""
        try:
            if event.type() in _USER_ACTIVITY_EVENTS:
                self._last_user_activity = time.monotonic()
        except Exception:
            # Activity diagnostics must never consume or break a Qt event.
            pass
        return False

    def _log(self, level, phase, **fields):
        # Diagnostics must not interrupt collection or touch Qt on a worker.
        try:
            getattr(self._logger, level)(
                "[GuiGC] phase=%s gui_native_id=%s native_id=%s python_thread_id=%s details=%s",
                phase, self._gui_native_id, threading.get_native_id(),
                threading.get_ident(), fields,
            )
        except Exception:
            pass

    def _observe_gc(self, phase, info):
        if self._closed:
            return
        self._gc_active = phase == "start"
        if phase != "start":
            return
        if threading.get_native_id() != self._gui_native_id:
            # Names/locations only: never locals, repr(widget), titles or paths
            # supplied by patients. No Qt inspection from this callback.
            frames = traceback.extract_stack(limit=12)
            stack = [(Path(frame.filename).name, frame.lineno, frame.name) for frame in frames]
            self._log("error", "unexpected_worker_collection",
                      generation=info.get("generation"), automatic_gc=gc.isenabled(), stack=stack)

    def _window_classes(self):
        try:
            classes = Counter(type(widget).__name__ for widget in self._application.topLevelWidgets())
            return dict(classes.most_common(20))
        except Exception:
            return {"unavailable": 1}

    def _runtime_objects(self):
        """Yield the small, known runtime ownership graph without walking all widgets."""
        seen = set()
        pending = list(self._application.topLevelWidgets())
        while pending:
            value = pending.pop()
            if value is None or id(value) in seen:
                continue
            seen.add(id(value))
            yield value
            for name in (
                "container", "role_window", "doctor_main", "nurse_main",
                "operblock_main", "admin_main",
            ):
                child = getattr(value, name, None)
                if child is not None:
                    pending.append(child)
            pending.extend(list(getattr(value, "_owned_containers", ()) or ()))

    def _foreground_busy_reason(self, runtime_objects):
        modal = self._application.activeModalWidget()
        if modal is not None:
            return "modal"
        if self._application.activePopupWidget() is not None:
            return "popup"

        for value in runtime_objects:
            if any(bool(getattr(value, name, False)) for name in (
                "_busy", "_leaving", "_closing", "_entry_setup_active",
                "_drain_in_flight",
            )):
                return "foreground_busy"
            transition = getattr(value, "_transition", None)
            if transition is not None and bool(getattr(transition, "running", False)):
                return "foreground_transition"
        return ""

    def _prewarm_busy(self, runtime_objects):
        for value in runtime_objects:
            prewarmer = getattr(value, "_card_ui_prewarmer", None)
            if prewarmer is not None and bool(getattr(prewarmer, "active", False)):
                return True
            if (
                bool(getattr(value, "_journal_prewarm_started", False))
                and not bool(getattr(value, "_journal_prewarm_done", False))
            ):
                return True
        return False

    def _database_busy_reason(self, runtime_objects):
        services = []
        seen = set()
        for value in runtime_objects:
            service = getattr(value, "data_service", None)
            if service is None or id(service) in seen:
                continue
            seen.add(id(service))
            services.append(service)
        for service in services:
            try:
                if not bool(service.is_write_queue_idle()):
                    return "write_queue"
            except Exception:
                # An unknown write state is not a safe collection window.
                return "write_probe_error"

        try:
            from rem_card.app.foreground_activity import should_defer_background_io
            deferred, reason, _age = should_defer_background_io(
                idle_window_sec=0.0,
                names=None,
            )
            if deferred:
                return f"database_foreground:{reason}"
        except Exception:
            return "database_foreground_probe_error"
        return ""

    def _defer_reason(self, now):
        runtime_objects = tuple(self._runtime_objects())
        reason = self._foreground_busy_reason(runtime_objects)
        if reason:
            return reason
        if self._prewarm_busy(runtime_objects):
            return "prewarm"
        reason = self._database_busy_reason(runtime_objects)
        if reason:
            return reason
        if now - self._last_user_activity < USER_IDLE_SEC:
            # Even gen0 can run finalizers for seconds. An overdue collection
            # is reported, never forced into an actively used clinical screen.
            return "recent_user_input"
        return ""

    def _note_deferred(self, *, now, generation, reason):
        if self._deferred_since is None:
            self._deferred_since = now
            self._log("info", "deferred", generation=generation, reason=reason)
            return
        elapsed = now - self._deferred_since
        if elapsed >= DEFERRED_WARNING_SEC and now - self._last_deferred_warning >= DEFERRED_WARNING_SEC:
            self._last_deferred_warning = now
            self._log(
                "warning", "deferred_overdue", generation=generation,
                reason=reason, deferred_sec=round(elapsed, 3), count=gc.get_count(),
            )

    def _due_collection(self, now):
        if now - self._last_full >= FULL_COLLECTION_INTERVAL_SEC:
            return 2, "periodic_full"
        if gc.get_count()[0] >= max(1, gc.get_threshold()[0]):
            return (1 if self._minor_collections >= 9 else 0), "allocation_threshold"
        return None

    def _poll(self):
        if self._closed or self._collecting or self._gc_active:
            return
        if gc.isenabled():
            gc.disable()
            self._log("error", "automatic_gc_reenabled")
        now = time.monotonic()
        due = self._due_collection(now)
        if due is None:
            self._deferred_since = None
            return
        generation, reason = due
        defer_reason = self._defer_reason(now)
        if defer_reason:
            self._note_deferred(
                now=now, generation=generation, reason=defer_reason,
            )
            return
        self._deferred_since = None
        self.collect(generation, reason=reason)

    def collect(self, generation=2, *, reason="requested") -> int:
        if threading.get_native_id() != self._gui_native_id:
            self._log("error", "collection_request_rejected", generation=generation)
            raise RuntimeError("Garbage collection must run on the GUI thread")
        if self._closed or self._collecting or self._gc_active:
            return 0
        self._collecting = True
        self._sequence += 1
        sequence = self._sequence
        started = time.perf_counter()
        count_before = gc.get_count()
        try:
            # The begin record survives a native failure inside gc.collect().
            # Only full collections produce routine records (two per minute).
            if generation == 2:
                self._log("info", "begin", sequence=sequence, generation=generation,
                          reason=reason, count=count_before, windows=self._window_classes())
            collected = gc.collect(generation)
            elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
            if generation == 2 or elapsed_ms >= SLOW_COLLECTION_MS:
                self._log("warning" if elapsed_ms >= SLOW_COLLECTION_MS else "info", "end",
                          sequence=sequence, generation=generation, reason=reason,
                          collected=collected, uncollectable=len(gc.garbage),
                          elapsed_ms=elapsed_ms, count_before=count_before, count_after=gc.get_count())
            if generation == 2:
                self._last_full = time.monotonic()
            self._minor_collections = self._minor_collections + 1 if generation == 0 else 0
            return collected
        finally:
            self._collecting = False

    def close(self):
        if threading.get_native_id() != self._gui_native_id:
            raise RuntimeError("GUI garbage collector must be stopped on the GUI thread")
        if self._closed:
            return
        self._timer.stop()
        self._closed = True
        self._application.removeEventFilter(self)
        if self._callback in gc.callbacks:
            gc.callbacks.remove(self._callback)
        # Do NOT re-enable automatic GC: Qt windows and retiring workers may
        # still be alive after app.exec() returns. Process exit collects on main.
        self._log("info", "stopped", automatic_gc=gc.isenabled(), collections=self._sequence)


def install_gui_gc(application, *, logger=None) -> GuiGarbageCollector:
    existing = getattr(application, "_remcard_gui_gc", None)
    if existing is not None:
        return existing
    if logger is None:
        from rem_card.app.logger import logger
    controller = GuiGarbageCollector(application, logger)
    # Keep both Qt and Python ownership for the entire QApplication lifetime.
    application._remcard_gui_gc = controller
    return controller
