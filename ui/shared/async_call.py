import threading
from typing import Any, Callable, ClassVar

from PySide6.QtCore import QThread, Signal


class AsyncCallThread(QThread):
    succeeded = Signal(object)
    failed = Signal(object)
    _keepalive_lock: ClassVar[threading.Lock] = threading.Lock()
    _keepalive_threads: ClassVar[set] = set()

    def __init__(self, fn: Callable[..., Any], /, *args, parent=None, **kwargs):
        super().__init__(parent)
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._keepalive_connected = False

    def start(self, priority=None):
        with self._keepalive_lock:
            self._keepalive_threads.add(self)
        if not self._keepalive_connected:
            self.finished.connect(self._release_keepalive)
            self._keepalive_connected = True
        try:
            if priority is None:
                super().start()
            else:
                super().start(priority)
        except Exception:
            self._release_keepalive()
            raise

    def _release_keepalive(self):
        with self._keepalive_lock:
            self._keepalive_threads.discard(self)

    def run(self):
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception as exc:
            self.failed.emit(exc)
            return
        self.succeeded.emit(result)
