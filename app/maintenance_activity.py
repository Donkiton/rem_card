import threading
import time
from contextlib import contextmanager
from itertools import count
from typing import Any

from rem_card.app.local_metrics import record_metric


_LOCK = threading.Lock()
_ACTIVE_TASKS: dict[str, dict[str, Any]] = {}
_TOKEN_COUNTER = count(1)


def _normalize_task_type(task_type: str) -> str:
    return str(task_type or "maintenance").strip().lower() or "maintenance"


def _normalize_source(source: str) -> str:
    return str(source or "background").strip().lower() or "background"


@contextmanager
def maintenance_task(task_type: str, *, source: str = "background", **fields: Any):
    normalized_type = _normalize_task_type(task_type)
    normalized_source = _normalize_source(source)
    started = time.monotonic()
    token = f"{normalized_type}:{threading.get_ident()}:{next(_TOKEN_COUNTER)}"
    payload = {
        "task_id": token,
        "task_type": normalized_type,
        "source": normalized_source,
        "monotonic": started,
        **fields,
    }
    with _LOCK:
        _ACTIVE_TASKS[token] = dict(payload)
    record_metric(
        "maintenance_task_started",
        1,
        task_id=token,
        task_type=normalized_type,
        source=normalized_source,
        **fields,
    )
    status = "ok"
    try:
        yield token
    except Exception:
        status = "error"
        raise
    finally:
        elapsed_ms = (time.monotonic() - started) * 1000.0
        with _LOCK:
            _ACTIVE_TASKS.pop(token, None)
        record_metric(
            "maintenance_task_finished",
            round(elapsed_ms, 3),
            task_id=token,
            task_type=normalized_type,
            source=normalized_source,
            status=status,
            force_flush=status != "ok",
            **fields,
        )


def active_maintenance_snapshot(*, limit: int = 8) -> dict[str, object]:
    now = time.monotonic()
    max_items = max(1, int(limit or 1))
    with _LOCK:
        tasks = [dict(payload) for payload in _ACTIVE_TASKS.values()]
    tasks.sort(key=lambda payload: float(payload.get("monotonic") or 0.0), reverse=True)
    compact = [
        {
            "task_id": str(payload.get("task_id") or ""),
            "task_type": str(payload.get("task_type") or ""),
            "source": str(payload.get("source") or ""),
            "age_sec": round(max(0.0, now - float(payload.get("monotonic") or now)), 3),
        }
        for payload in tasks[:max_items]
    ]
    return {
        "active_count": len(tasks),
        "active": compact,
    }
