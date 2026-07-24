import getpass
import json
import os
import queue
import socket
import threading
import time
from datetime import datetime
from typing import Any, Optional

from rem_card.app.runtime_paths import get_writable_runtime_logs_dir
from rem_card.app.version import APP_VERSION


_AUDIT_MIRROR_QUEUE: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue(maxsize=1000)
_AUDIT_MIRROR_THREAD: threading.Thread | None = None
_AUDIT_MIRROR_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _json_default(value: Any) -> str:
    return str(value)


def _append_jsonl(path: str, payload: dict[str, Any]) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, default=_json_default) + "\n")
        return True
    except Exception:
        return False


def _audit_mirror_worker() -> None:
    while True:
        path, payload = _AUDIT_MIRROR_QUEUE.get()
        try:
            _append_jsonl(path, payload)
        finally:
            _AUDIT_MIRROR_QUEUE.task_done()


def _ensure_audit_mirror_worker() -> None:
    global _AUDIT_MIRROR_THREAD
    with _AUDIT_MIRROR_LOCK:
        if _AUDIT_MIRROR_THREAD is not None and _AUDIT_MIRROR_THREAD.is_alive():
            return
        _AUDIT_MIRROR_THREAD = threading.Thread(
            target=_audit_mirror_worker,
            name="RemCardAuditMirror",
            daemon=True,
        )
        _AUDIT_MIRROR_THREAD.start()


def _enqueue_audit_mirror(path: str, payload: dict[str, Any]) -> bool:
    _ensure_audit_mirror_worker()
    try:
        _AUDIT_MIRROR_QUEUE.put_nowait((path, dict(payload)))
        return True
    except queue.Full:
        return False


def flush_audit_mirror(timeout_sec: float = 1.0) -> bool:
    """Wait briefly for tests or controlled shutdown without blocking on a bad share."""
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    while _AUDIT_MIRROR_QUEUE.unfinished_tasks:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def write_audit_event(
    event: str,
    *,
    baza_dir: Optional[str] = None,
    role: Optional[str] = None,
    details: Optional[dict[str, Any]] = None,
):
    payload: dict[str, Any] = {
        "ts": _now_iso(),
        "event": str(event),
        "host": socket.gethostname(),
        "windows_user": getpass.getuser(),
        "pid": os.getpid(),
        "role": role,
        "app_version": APP_VERSION,
    }
    if details:
        payload.update(details)

    log_name = f"audit_{datetime.now().strftime('%Y%m%d')}.jsonl"
    try:
        local_path = os.path.join(get_writable_runtime_logs_dir(), log_name)
        _append_jsonl(local_path, payload)
    except Exception:
        pass
    if baza_dir:
        shared_path = os.path.join(baza_dir, "logs", log_name)
        _enqueue_audit_mirror(shared_path, payload)

