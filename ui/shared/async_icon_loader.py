from __future__ import annotations

import itertools
import queue
import threading
import weakref
from dataclasses import dataclass
from typing import Any, Callable, Hashable

from PySide6.QtCore import QCoreApplication, QObject, Signal, Slot
from PySide6.QtGui import QPixmap

from rem_card.app.logger import logger
_REQUEST_TOKEN_PROPERTY = "_remcard_async_icon_request_token"
_WORKER_COUNT = 2


@dataclass
class _Waiter:
    receiver_ref: weakref.ReferenceType
    token: int
    apply: Callable[[QObject, QPixmap], None]


@dataclass
class _Inflight:
    waiters: list[_Waiter]
    finalize: Callable[[Any], QPixmap]


class _DaemonWorkPool:
    def __init__(self, worker_count: int = _WORKER_COUNT):
        self._worker_count = max(1, int(worker_count))
        self._queue: queue.Queue[Callable[[], None] | None] = queue.Queue(maxsize=256)
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def submit(self, callback: Callable[[], None]) -> bool:
        with self._lock:
            self._threads = [thread for thread in self._threads if thread.is_alive()]
            if len(self._threads) < self._worker_count:
                for index in range(len(self._threads), self._worker_count):
                    thread = threading.Thread(
                        target=self._run,
                        name=f"AsyncIconWorker-{index + 1}",
                        daemon=True,
                    )
                    self._threads.append(thread)
                    thread.start()
        try:
            self._queue.put_nowait(callback)
            return True
        except queue.Full:
            return False

    def _run(self) -> None:
        while True:
            callback = self._queue.get()
            try:
                if callback is None:
                    return
                try:
                    callback()
                except BaseException as exc:
                    logger.debug("[AsyncIconLoader] worker callback failed: %s", exc)
            finally:
                self._queue.task_done()


_WORK_POOL = _DaemonWorkPool()


class AsyncIconLoader(QObject):
    """Deduplicates icon I/O while keeping QPixmap creation on the GUI thread."""

    _result_ready = Signal(object)

    def __init__(self):
        super().__init__()
        self._inflight: dict[Hashable, _Inflight] = {}
        self._tokens = itertools.count(1)
        self._accept_results = True
        self._result_ready.connect(self._on_result)
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.cancel_all)

    def begin_request(self, receiver: QObject) -> int | None:
        token = next(self._tokens)
        try:
            receiver.setProperty(_REQUEST_TOKEN_PROPERTY, token)
        except (RuntimeError, TypeError):
            return None
        return token

    def request(
        self,
        receiver: QObject,
        request_key: Hashable,
        load: Callable[[], Any],
        finalize: Callable[[Any], QPixmap],
        apply: Callable[[QObject, QPixmap], None],
        *,
        token: int | None = None,
    ) -> bool:
        if not self._accept_results:
            return False
        if token is None:
            token = self.begin_request(receiver)
        if token is None:
            return False
        try:
            receiver_ref = weakref.ref(receiver)
        except TypeError:
            return False

        waiter = _Waiter(receiver_ref=receiver_ref, token=token, apply=apply)
        existing = self._inflight.get(request_key)
        if existing is not None:
            existing.waiters = [
                item
                for item in existing.waiters
                if item.receiver_ref() is not None and item.receiver_ref() is not receiver
            ]
            existing.waiters.append(waiter)
            return True

        def run():
            try:
                return request_key, load(), None
            except Exception as exc:  # delivered to the GUI thread as data
                return request_key, None, exc

        self._inflight[request_key] = _Inflight(
            waiters=[waiter],
            finalize=finalize,
        )
        def execute() -> None:
            if not self._accept_results:
                return
            payload = run()
            if self._accept_results:
                self._result_ready.emit(payload)

        if not _WORK_POOL.submit(execute):
            self._inflight.pop(request_key, None)
            return False
        return True

    @Slot(object)
    def _on_result(self, payload) -> None:
        try:
            request_key, prepared, error = payload
        except Exception:
            return
        inflight = self._inflight.pop(request_key, None)
        if inflight is None:
            return
        if error is not None:
            logger.debug("[AsyncIconLoader] request failed key=%s: %s", request_key, error)
            return
        try:
            pixmap = inflight.finalize(prepared)
        except Exception as exc:
            logger.debug("[AsyncIconLoader] finalize failed key=%s: %s", request_key, exc)
            return
        if pixmap.isNull():
            return
        for waiter in inflight.waiters:
            receiver = waiter.receiver_ref()
            if receiver is None:
                continue
            try:
                if int(receiver.property(_REQUEST_TOKEN_PROPERTY) or 0) != waiter.token:
                    continue
                waiter.apply(receiver, QPixmap(pixmap))
            except Exception as exc:
                logger.debug("[AsyncIconLoader] apply failed key=%s: %s", request_key, exc)
                continue

    @Slot()
    def cancel_all(self) -> None:
        self._accept_results = False
        self._inflight.clear()

    @property
    def pending_count(self) -> int:
        return len(self._inflight)

    @property
    def is_active(self) -> bool:
        return bool(self._accept_results)


_LOADER_LOCK = threading.Lock()
_LOADER: AsyncIconLoader | None = None


def get_async_icon_loader() -> AsyncIconLoader:
    global _LOADER
    with _LOADER_LOCK:
        if _LOADER is None or not _LOADER.is_active:
            _LOADER = AsyncIconLoader()
        return _LOADER


def begin_async_icon_request(receiver: QObject) -> int | None:
    return get_async_icon_loader().begin_request(receiver)


def request_async_icon(
    receiver: QObject,
    request_key: Hashable,
    load: Callable[[], Any],
    finalize: Callable[[Any], QPixmap],
    apply: Callable[[QObject, QPixmap], None] | None = None,
    *,
    token: int | None = None,
) -> bool:
    callback = apply or (lambda target, pixmap: target.setPixmap(pixmap))
    return get_async_icon_loader().request(
        receiver,
        request_key,
        load,
        finalize,
        callback,
        token=token,
    )
