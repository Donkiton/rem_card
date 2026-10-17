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


def responsive_startup_probe(path, cancel, *, probe=None, progress_state=None, preparing=None):
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
    last_progress = [None]
    def tick():
        value = progress_state[0] if progress_state else None
        if preparing and value and value != last_progress[0]:
            last_progress[0] = value
            stage = value.get("stage")
            total = value.get("total", 0)
            if stage == "backup" and total:
                percent = max(0, min(100, int(100 * (total - value["remaining"]) / total)))
                preparing(f"Создание копии базы: {percent}%…")
            elif stage in {"quick_check", "source_corruption_confirmation", "direct_check"}:
                preparing("Проверка целостности базы…")
            elif stage == "source_confirmation":
                preparing("Подтверждение состояния базы…")
        if finished.is_set():
            loop.quit()
    timer.timeout.connect(tick)
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
        # The child may have finished between the last cancellation probe and
        # the GUI consuming its result. Do not orphan its validated candidate.
        try:
            from rem_card.app.startup_snapshot import discard
            discard(json.loads(result[1]).get("snapshot"))
        except (ValueError, TypeError, AttributeError, IndexError):
            pass
        raise StartupCheckAborted()
    return result


def role_startup_check_runner(cancel, preparing):
    """Use the same cancellable child for full scans and cheap admission reads."""
    def probe(path, mode):
        progress = [None]
        return responsive_startup_probe(path, cancel, probe=lambda path, cancel: run_startup_probe(
            path, cancel, admission_mode=mode, progress=lambda value: progress.__setitem__(0, value)),
            progress_state=progress, preparing=preparing)

    def check(path):
        preparing("Создание проверенной копии базы…")
        check.admission_receipt = None
        check.full_checked_at_epoch = None
        check.discard_snapshot()
        outcome = probe(path, "direct_admission" if check.force_full_scan else "snapshot")
        if outcome[0] and outcome[1] != "ok":
            payload = json.loads(outcome[1])
            check.admission_receipt = payload["admission"]
            check.startup_snapshot = payload["snapshot"]
            check.full_checked_at_epoch = payload.get("full_checked_at_epoch")
            check.requires_confirmation = True
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
    check.startup_snapshot = None
    check.full_checked_at_epoch = None
    check.requires_confirmation = False
    check.force_full_scan = False
    def discard_snapshot():
        from rem_card.app.startup_snapshot import discard
        discard(check.startup_snapshot)
        check.startup_snapshot = None
    check.discard_snapshot = discard_snapshot
    check.admission_probe = admission_probe
    check.wait_retry = lambda seconds: responsive_startup_wait(cancel, seconds)
    return check
