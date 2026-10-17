"""Cancellable, read-only startup scan. Recovery remains in the owning process."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import multiprocessing
import json
import os
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


def _read_only_probe(path, sender, diagnostics, admission_mode=None, workspace=None, timeout=90.0):
    # Never run policy writes, recovery, migration or bootstrap in this child.
    from rem_card.app.startup_db_guard import _check_quick_direct
    from rem_card.app.startup_diagnostics import startup_attempt
    from rem_card.app.local_metrics import flush_metrics
    try:
        with startup_attempt(diagnostics.get("session_id", ""), diagnostics.get("role", "")):
            if admission_mode == "direct_admission":
                from rem_card.app.startup_snapshot import check_direct_admission
                sender.send(check_direct_admission(path, progress=lambda value: sender.send({"progress": value})))
            elif admission_mode == "snapshot":
                from rem_card.app.startup_snapshot import check_snapshot
                sender.send(check_snapshot(path, workspace, lease_duration_sec=timeout * 2 + 3,
                                           progress=lambda value: sender.send({"progress": value})))
            elif admission_mode == "light":
                from rem_card.app.role_admission import read_only_admission_probe
                sender.send(read_only_admission_probe(path))
            else:
                sender.send(_check_quick_direct(path, capture_admission=admission_mode == "full"))
    finally:
        flush_metrics(timeout=0.5)
        sender.close()


def _receive_result(receiver, progress):
    try:
        result = receiver.recv()
    except EOFError:
        raise StartupCheckAborted("worker_failed") from None
    if isinstance(result, dict) and "progress" in result:
        if progress:
            progress(result["progress"])
        return None
    return result


def _validated_snapshot_result(result, workspace, path, snapshot_mode) -> bool:
    if (not isinstance(result, tuple) or len(result) != 3
            or not isinstance(result[0], bool) or not isinstance(result[2], bool)):
        raise StartupCheckAborted("invalid_result")
    if not snapshot_mode or not result[0] or result[1] == "ok":
        return False
    try:
        payload = json.loads(result[1])
        state = payload.get("admission")
        required = {"identity", "schema", "migration", "revision", "markers", "db_profile"}
        if (not isinstance(state, dict) or not required.issubset(state)
                or state["identity"].get("path") != os.path.normcase(os.path.abspath(path))):
            raise StartupCheckAborted("invalid_result")
        snapshot = payload.get("snapshot", {})
        if snapshot is None:
            return False
        valid = (workspace is not None and snapshot.get("workspace") == workspace
                 and snapshot.get("path") == os.path.join(workspace, "validated.db")
                 and isinstance(payload.get("admission"), dict)
                 and snapshot.get("source") == payload["admission"])
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise StartupCheckAborted("invalid_result")
    return True


def _stop_probe(process, path, workspace):
    forced = process.is_alive() or process.exitcode != 0
    worker_pid = process.pid
    if process.is_alive():
        process.terminate()
        process.join(1.0)
    if process.is_alive():
        process.kill()
        process.join(1.0)
    if process.is_alive():
        raise StartupCheckAborted("cleanup_failed", cleanup_failed=True)
    process.close()
    if workspace and forced:
        from rem_card.app.startup_snapshot import cleanup_stopped_worker
        if not cleanup_stopped_worker(path, worker_pid):
            raise StartupCheckAborted("cleanup_failed", cleanup_failed=True)


def _await_probe(process, receiver, cancel, deadline, progress):
    result = None
    while True:
        if cancel.is_set():
            raise StartupCheckAborted()
        if time.monotonic() >= deadline:
            raise StartupCheckAborted("timeout")
        if result is None and receiver.poll(0.025):
            result = _receive_result(receiver, progress)
        process.join(0.025)
        if process.is_alive():
            continue
        while result is None and receiver.poll():
            result = _receive_result(receiver, progress)
        if process.exitcode != 0 or result is None:
            raise StartupCheckAborted("worker_failed")
        if cancel.is_set():
            raise StartupCheckAborted()
        return result


def run_startup_probe(path: str, cancel: threading.Event, *, timeout=90.0,
                      _target=None, admission_mode=None, progress=None) -> tuple[bool, str, bool]:
    """Return only after the child has exited and released its SQLite handles."""
    context = multiprocessing.get_context("spawn")
    from rem_card.app.startup_diagnostics import startup_context
    receiver, sender = context.Pipe(duplex=False)
    workspace = None
    if admission_mode == "snapshot":
        from rem_card.app.startup_snapshot import create_workspace
        try:
            workspace = create_workspace()
        except OSError:
            admission_mode = "direct_admission"
    process = context.Process(target=_target or _read_only_probe,
                              args=(path, sender) if _target else (path, sender, startup_context(), admission_mode, workspace, timeout),
                              name="RemCardStartupCheck")
    started = keep_snapshot = False
    try:
        if cancel.is_set():
            raise StartupCheckAborted()
        deadline = time.monotonic() + timeout
        process.start()
        started = True
        sender.close()
        result = _await_probe(process, receiver, cancel, deadline, progress)
        keep_snapshot = _validated_snapshot_result(result, workspace, path,
                                                    admission_mode in {"snapshot", "direct_admission"})
        return result
    finally:
        sender.close()
        receiver.close()
        if started:
            _stop_probe(process, path, workspace)
        if workspace and not keep_snapshot:
            from rem_card.app.startup_snapshot import discard
            discard({"workspace": workspace})
