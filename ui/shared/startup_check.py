"""Keep Qt responsive while a read-only child owns a startup scan."""
from __future__ import annotations

import json
import threading
import time
from contextvars import copy_context
from PySide6.QtCore import QEventLoop, QTimer
from rem_card.app.startup_check_worker import run_startup_probe
from rem_card.app.startup_check_worker import StartupCheckAborted


def responsive_startup_wait(cancel, seconds):
    deadline = time.monotonic() + seconds
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(20)
    timer.timeout.connect(lambda: loop.quit() if cancel.is_set() or time.monotonic() >= deadline else None)
    timer.start()
    try:
        while not cancel.is_set() and time.monotonic() < deadline:
            loop.exec()
    finally:
        timer.stop()
    if cancel.is_set():
        raise StartupCheckAborted()


def responsive_startup_probe(path, cancel, *, probe=None):
    # The surrounding shell retains its admission lease and busy flag for the
    # entire nested event loop. No runtime is constructed until this returns.
    outcome = []
    finished = threading.Event()
    def run():
        try:
            outcome.append((True, (probe or run_startup_probe)(path, cancel)))
        except Exception as exc:
            outcome.append((False, exc))
        finally:
            finished.set()
    context = copy_context()
    thread = threading.Thread(target=lambda: context.run(run), name="RemCardStartupProbeOwner", daemon=True)
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(20)
    timer.timeout.connect(lambda: loop.quit() if finished.is_set() else None)
    thread.start()
    timer.start()
    try:
        while not finished.is_set():
            loop.exec()
        thread.join()
    finally:
        timer.stop()
    ok, result = outcome[0]
    if not ok:
        raise result
    if cancel.is_set():
        raise StartupCheckAborted()
    return result


def role_startup_check_runner(cancel, preparing):
    """Use the same cancellable child for full scans and cheap admission reads."""
    def probe(path, mode):
        return responsive_startup_probe(path, cancel, probe=lambda path, cancel: run_startup_probe(
            path, cancel, admission_mode=mode))

    def check(path):
        preparing("Проверка целостности базы…")
        check.admission_receipt = None
        outcome = probe(path, "full")
        if outcome[0] and outcome[1] != "ok":
            check.admission_receipt = json.loads(outcome[1])
            return True, "ok", False
        return outcome

    def admission_probe(path):
        preparing("Проверка доступности базы…")
        outcome = probe(path, "light")
        return json.loads(outcome[1]) if outcome[0] else None

    def check_cancelled():
        if cancel.is_set():
            raise StartupCheckAborted()

    check.check_cancelled = check_cancelled
    check.admission_probe = admission_probe
    check.wait_retry = lambda seconds: responsive_startup_wait(cancel, seconds)
    return check
