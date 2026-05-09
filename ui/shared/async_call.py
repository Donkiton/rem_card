import threading
from typing import Any, Callable, ClassVar

from PySide6.QtCore import QObject, Signal


class AsyncCallThread(QObject):
    succeeded = Signal(object)
    failed = Signal(object)
    finished = Signal()
    _keepalive_lock: ClassVar[threading.Lock] = threading.Lock()
    _keepalive_threads: ClassVar[set] = set()

    def __init__(self, fn: Callable[..., Any], /, *args, parent=None, **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._keepalive_connected = False
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._cancel_requested = threading.Event()

    def start(self, priority=None):
        del priority
        with self._state_lock:
            if self._running:
                return
            self._cancel_requested.clear()
            self._running = True
            self._thread = threading.Thread(target=self._run_wrapper, daemon=True)

        with self._keepalive_lock:
            self._keepalive_threads.add(self)
        if not self._keepalive_connected:
            self.finished.connect(self._release_keepalive)
            self._keepalive_connected = True
        try:
            self._thread.start()
        except Exception:
            with self._state_lock:
                self._running = False
                self._thread = None
            self._release_keepalive()
            raise

    def isRunning(self) -> bool:
        with self._state_lock:
            thread = self._thread
            return bool(self._running and thread is not None and thread.is_alive())

    def quit(self):
        self._cancel_requested.set()

    def wait(self, timeout_ms: int | None = None) -> bool:
        with self._state_lock:
            thread = self._thread
        if thread is None or thread is threading.current_thread():
            return True
        timeout = None if timeout_ms is None else max(0.0, timeout_ms / 1000.0)
        thread.join(timeout)
        return not thread.is_alive()

    def terminate(self):
        self.quit()

    def _release_keepalive(self):
        with self._keepalive_lock:
            self._keepalive_threads.discard(self)

    def _run_wrapper(self):
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception as exc:
            self.failed.emit(exc)
        else:
            self.succeeded.emit(result)
        finally:
            with self._state_lock:
                self._running = False
            self.finished.emit()
