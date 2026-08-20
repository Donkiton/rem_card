from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from rem_card.app.local_metrics import record_metric


ENV_ENABLED = "REMCARD_OPERBLOCK_STARTUP_METRICS"

_LOCK = threading.RLock()
_STARTED_AT = time.perf_counter()
_RUN_ID = ""
_PROCESS_START_TS = datetime.now().astimezone().isoformat(timespec="milliseconds")
_DURATIONS: dict[str, float] = {}
_COUNTS: dict[str, int] = {}
_VALUES: dict[str, Any] = {}
_CONTEXT: dict[str, Any] = {}
_MAX_EVENT_LOOP_PAUSE_MS = 0.0


def enabled() -> bool:
    return str(os.environ.get(ENV_ENABLED, "")).strip().lower() in {"1", "true", "yes", "on"}


def reset(*, started_at: float | None = None, run_id: str | None = None, **context: Any) -> None:
    if not enabled():
        return
    global _STARTED_AT, _RUN_ID, _PROCESS_START_TS, _MAX_EVENT_LOOP_PAUSE_MS
    with _LOCK:
        _STARTED_AT = float(started_at if started_at is not None else time.perf_counter())
        _RUN_ID = str(run_id or os.environ.get("REMCARD_OPERBLOCK_STARTUP_RUN_ID", "") or "")
        _PROCESS_START_TS = datetime.now().astimezone().isoformat(timespec="milliseconds")
        _DURATIONS.clear()
        _COUNTS.clear()
        _VALUES.clear()
        _CONTEXT.clear()
        _CONTEXT.update({key: value for key, value in context.items() if value is not None})
        _MAX_EVENT_LOOP_PAUSE_MS = 0.0
        _VALUES["opblock_process_start_ts"] = _PROCESS_START_TS
    _record_local_metric("opblock_process_start_ts", _PROCESS_START_TS)


def set_context(**context: Any) -> None:
    if not enabled():
        return
    with _LOCK:
        _CONTEXT.update({key: value for key, value in context.items() if value is not None})


def timer_start() -> float:
    return time.perf_counter() if enabled() else 0.0


def record_since(name: str, started_at: float, **fields: Any) -> None:
    if not enabled() or not started_at:
        return
    record_duration(name, (time.perf_counter() - float(started_at)) * 1000.0, **fields)


@contextmanager
def measure(name: str, **fields: Any) -> Iterator[None]:
    if not enabled():
        yield
        return
    started_at = time.perf_counter()
    try:
        yield
    finally:
        record_duration(name, (time.perf_counter() - started_at) * 1000.0, **fields)


def record_duration(name: str, elapsed_ms: float, **fields: Any) -> None:
    if not enabled():
        return
    value = round(max(0.0, float(elapsed_ms or 0.0)), 3)
    with _LOCK:
        _DURATIONS[name] = round(float(_DURATIONS.get(name, 0.0)) + value, 3)
        _COUNTS[name] = int(_COUNTS.get(name, 0)) + 1
    _record_local_metric(name, value, **fields)


def record_value(name: str, value: Any, **fields: Any) -> None:
    if not enabled():
        return
    with _LOCK:
        _VALUES[name] = value
    _record_local_metric(name, value, **fields)


def record_elapsed(name: str, **fields: Any) -> None:
    if not enabled():
        return
    elapsed_ms = round(max(0.0, (time.perf_counter() - _STARTED_AT) * 1000.0), 3)
    record_value(name, elapsed_ms, **fields)


def record_event_loop_pause(pause_ms: float, **fields: Any) -> None:
    if not enabled():
        return
    value = round(max(0.0, float(pause_ms or 0.0)), 3)
    global _MAX_EVENT_LOOP_PAUSE_MS
    with _LOCK:
        if value > _MAX_EVENT_LOOP_PAUSE_MS:
            _MAX_EVENT_LOOP_PAUSE_MS = value
            _VALUES["max_event_loop_pause_ms"] = value
    _record_local_metric("max_event_loop_pause_ms", value, **fields)


def snapshot() -> dict[str, Any]:
    with _LOCK:
        values = dict(_VALUES)
        values.setdefault("max_event_loop_pause_ms", round(_MAX_EVENT_LOOP_PAUSE_MS, 3))
        return {
            "run_id": _RUN_ID,
            "context": dict(_CONTEXT),
            "durations": dict(_DURATIONS),
            "counts": dict(_COUNTS),
            "values": values,
            "metrics": {**_DURATIONS, **values},
        }


def _record_local_metric(name: str, value: Any, **fields: Any) -> None:
    payload = {
        "role": "operblock",
        "run_id": _RUN_ID,
        **_CONTEXT,
        **fields,
    }
    record_metric(str(name), value, **payload)
