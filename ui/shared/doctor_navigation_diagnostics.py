"""Event-driven doctor diagnostics; no patient text, polling or GUI disk writes."""
from __future__ import annotations

import atexit
import json
import os
import queue
import sys
import threading
import time
from collections import deque
from datetime import datetime

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid


class _DiagnosticWriter:
    def __init__(self, sink=None, capacity=256):
        self.queue = queue.Queue(maxsize=capacity)
        self.sink = sink or self._log
        self.dropped = 0
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name="DoctorNavigationLog", daemon=True)
        self.thread.start()

    @staticmethod
    def _log(payload):
        from rem_card.app.runtime_log_storage import append_log_lines
        from rem_card.app.runtime_paths import get_writable_runtime_logs_dir

        # A separate segment avoids sharing a slow file-handler lock with GUI logs.
        stamp = datetime.fromtimestamp(payload.get("event_time", time.time())).strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
        append_log_lines(get_writable_runtime_logs_dir(), "doctor_navigation", [
            f"{stamp} | INFO | [DoctorNavigation] {json.dumps(payload, ensure_ascii=True)}\n",
        ])

    def submit(self, payload):
        # Called only on the GUI thread. Full/slow logging never blocks that thread.
        try:
            self.queue.put_nowait({**payload, "dropped_before": self.dropped})
            self.dropped = 0
        except queue.Full:
            self.dropped += 1

    def _run(self):
        while not self.stopped.is_set() or not self.queue.empty():
            try:
                payload = self.queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self.sink(payload)
            except Exception:
                pass  # Diagnostic failure must not affect clinical UI.
            finally:
                self.queue.task_done()

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=0.2)


_writer = None


def _submit(payload):
    global _writer
    if _writer is None:
        _writer = _DiagnosticWriter()
        atexit.register(_writer.close)
    _writer.submit(payload)


def _object_kind(obj):
    return type(obj).__name__ if obj is not None and isValid(obj) else None


def _caller_stack():
    # No source lines, locals or exception values (which may contain clinical data).
    frames = []
    frame = sys._getframe(1)
    try:
        for _ in range(12):
            if frame is None:
                break
            frames.append(f"{os.path.basename(frame.f_code.co_filename)}:{frame.f_lineno}:{frame.f_code.co_name}")
            frame = frame.f_back
    finally:
        del frame
    return frames


class DoctorNavigationDiagnostics(QObject):
    """Attached exclusively by DoctorRemCardWidget, removed on role shutdown."""

    def __init__(self, owner, *, submit=None, clock=time.monotonic):
        super().__init__(owner)
        self.owner = owner
        self.submit = submit or _submit
        self.clock = clock
        self.recent = deque(maxlen=16)
        self.closed = False
        self._last_state = {}
        self._budget_started = clock()
        self._budget_count = 0
        self._suppressed = 0
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def close(self):
        if self.closed:
            return
        self.closed = True
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self.recent.clear()

    def action(self, name):
        if not self.closed:
            self.recent.append({"time": self.clock(), "action": name})

    def transition(self, previous, current):
        try:
            if previous != current:
                self._record("mode_changed", previous=previous, current=current,
                             callers=_caller_stack())
        except Exception:
            pass

    def _record(self, event, **fields):
        if self.closed or not isValid(self.owner):
            return
        now = self.clock()
        if now - self._budget_started >= 1.0:
            self._budget_started, self._budget_count = now, 0
        if self._budget_count >= 20:
            self._suppressed += 1
            return
        self._budget_count += 1
        app = QApplication.instance()
        window = self.owner.window()
        layout = getattr(self.owner, "layout_manager", None)
        sector = getattr(layout, "sector_2b", None)
        tab_getter = getattr(sector, "current_tab_name", None)
        tab = tab_getter() if callable(tab_getter) and isValid(sector) else None
        from rem_card.services.crash_reports import current_crash_session_id

        self.submit({
            "event": event, "event_time": time.time(), "role": "doctor",
            "session_id": current_crash_session_id(),
            "mode": getattr(self.owner, "_selection_mode", None),
            "admission_id": getattr(self.owner, "admission_id", None),
            "tab": tab, "active_window": _object_kind(app.activeWindow()),
            "focus_widget": _object_kind(app.focusWidget()),
            "modal_window": _object_kind(app.activeModalWidget()),
            "popup_window": _object_kind(app.activePopupWidget()),
            "mouse_grabber": _object_kind(QWidget.mouseGrabber()),
            "keyboard_grabber": _object_kind(QWidget.keyboardGrabber()),
            "enabled": self.owner.isEnabled(), "visible": self.owner.isVisible(),
            "minimized": window.isMinimized(), "active": window.isActiveWindow(),
            "suppressed_before": self._suppressed,
            "recent_input": [{**{k: v for k, v in item.items() if k != "time"},
                              "age_ms": round((now - item["time"]) * 1000)}
                             for item in self.recent if now - item["time"] <= 10],
            **fields,
        })
        self._suppressed = 0

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind not in (QEvent.MouseButtonPress, QEvent.KeyPress, QEvent.WindowActivate,
                        QEvent.WindowDeactivate, QEvent.WindowStateChange, QEvent.FocusIn, QEvent.FocusOut):
            return False
        if self.closed or not isValid(self.owner):
            return False
        try:
            if not self.owner.isVisible():
                return False
            if kind == QEvent.MouseButtonPress:
                self.recent.append({"time": self.clock(), "mouse_button": event.button().name,
                                    "target": _object_kind(obj)})
            elif kind == QEvent.KeyPress:
                if (event.key() == Qt.Key_F12
                        and event.modifiers() == (Qt.ControlModifier | Qt.AltModifier)):
                    self._record("manual_snapshot", callers=_caller_stack())
                elif event.key() in (Qt.Key_Escape, Qt.Key_Back, Qt.Key_Backspace):
                    self.recent.append({"time": self.clock(), "navigation_key": int(event.key()),
                                        "target": _object_kind(obj)})
            elif kind in (QEvent.WindowActivate, QEvent.WindowDeactivate, QEvent.WindowStateChange,
                          QEvent.FocusIn, QEvent.FocusOut):
                signature = (kind, _object_kind(obj))
                now = self.clock()
                if now - self._last_state.get(signature, -10) >= 1:
                    if len(self._last_state) >= 64:
                        self._last_state.clear()
                    self._last_state[signature] = now
                    self._record(kind.name, target=_object_kind(obj))
        except Exception:
            # Qt teardown can invalidate objects between callbacks.
            pass
        return False  # Observe only; never consume or redirect user input.
