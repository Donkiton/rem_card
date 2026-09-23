"""Cancellable, read-only startup scan. Recovery remains in the owning process."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import multiprocessing
import threading
import time


class StartupCheckAborted(RuntimeError):
    def __init__(self, reason="cancelled", *, cleanup_failed=False):
        self.reason = reason
        self.cleanup_failed = cleanup_failed
        super().__init__("Вход отменён." if reason == "cancelled" else
                         "Проверка базы не завершена. Повторите вход после проверки соединения.")


_runner = ContextVar("startup_readonly_check_runner", default=None)


@contextmanager
def startup_check_runner(runner):
    token = _runner.set(runner)
    try:
        yield
    finally:
        _runner.reset(token)


def current_startup_check_runner():
    return _runner.get()


def check_startup_cancelled():
    check = getattr(_runner.get(), "check_cancelled", None)
    if check:
        check()


def _read_only_probe(path, sender, diagnostics):
    # Never run policy writes, recovery, migration or bootstrap in this child.
    from rem_card.app.startup_db_guard import _check_quick_direct
    from rem_card.app.startup_diagnostics import startup_attempt
    from rem_card.app.local_metrics import flush_metrics
    try:
        with startup_attempt(diagnostics.get("session_id", ""), diagnostics.get("role", "")):
            sender.send(_check_quick_direct(path))
    finally:
        flush_metrics(timeout=0.5)
        sender.close()


def run_startup_probe(path: str, cancel: threading.Event, *, timeout=90.0,
                      _target=None) -> tuple[bool, str, bool]:
    """Return only after the child has exited and released its SQLite handles."""
    context = multiprocessing.get_context("spawn")
    from rem_card.app.startup_diagnostics import startup_context
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_target or _read_only_probe,
                              args=(path, sender) if _target else (path, sender, startup_context()),
                              name="RemCardStartupCheck")
    started = False
    try:
        if cancel.is_set():
            raise StartupCheckAborted()
        deadline = time.monotonic() + timeout
        process.start()
        started = True
        sender.close()
        result = None
        while True:
            if cancel.is_set():
                raise StartupCheckAborted()
            if time.monotonic() >= deadline:
                raise StartupCheckAborted("timeout")
            if result is None and receiver.poll(0.025):
                try:
                    result = receiver.recv()
                except EOFError:
                    raise StartupCheckAborted("worker_failed") from None
            process.join(0.025)
            if not process.is_alive():
                if result is None and receiver.poll():
                    try:
                        result = receiver.recv()
                    except EOFError:
                        pass
                if process.exitcode != 0 or result is None:
                    raise StartupCheckAborted("worker_failed")
                if not isinstance(result, tuple) or len(result) != 3:
                    raise StartupCheckAborted("invalid_result")
                if cancel.is_set():
                    raise StartupCheckAborted()
                return result
    finally:
        sender.close()
        receiver.close()
        if started:
            if process.is_alive():
                process.terminate()
                process.join(1.0)
            if process.is_alive():
                process.kill()
                process.join(1.0)
            if process.is_alive():
                raise StartupCheckAborted("cleanup_failed", cleanup_failed=True)
            process.close()
