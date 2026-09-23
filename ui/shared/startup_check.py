"""Keep Qt responsive while a read-only child owns a startup scan."""
from __future__ import annotations

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
