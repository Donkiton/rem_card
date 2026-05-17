import json
import os
import queue
import socket
import threading
import time
import atexit
from datetime import datetime
from typing import Any

from rem_card.app.runtime_paths import get_local_logs_dir


_METRICS_LOCK = threading.Lock()
_PATH_LOCK = threading.Lock()
_QUEUE_LOCK = threading.Lock()
_METRICS_QUEUE: "queue.Queue[dict[str, Any]] | None" = None
_METRICS_THREAD: threading.Thread | None = None
_METRICS_STOP = threading.Event()
_DROPPED_LOCK = threading.Lock()
_DROPPED_METRICS = 0
_LATEST_CHANGE_METRIC_LOCK = threading.Lock()
_LATEST_CHANGE_METRIC_STATE: dict[tuple[Any, ...], tuple[Any, float]] = {}
_CACHED_PATH_DAY: str | None = None
_CACHED_PATH: str | None = None
_HOSTNAME = socket.gethostname()
_PID = os.getpid()

_DEFAULT_QUEUE_SIZE = 10000
_DEFAULT_BATCH_SIZE = 250
_DEFAULT_FLUSH_INTERVAL_SEC = 1.0
_DEFAULT_LATEST_CHANGE_MIN_INTERVAL_SEC = 60.0


def _enabled() -> bool:
    return os.environ.get("REMCARD_LOCAL_METRICS_ENABLED", "1") != "0"


def _sync_mode() -> bool:
    return os.environ.get("REMCARD_LOCAL_METRICS_SYNC", "0") == "1"


def _queue_size() -> int:
    try:
        return max(100, int(os.environ.get("REMCARD_LOCAL_METRICS_QUEUE_SIZE", str(_DEFAULT_QUEUE_SIZE))))
    except Exception:
        return _DEFAULT_QUEUE_SIZE


def _batch_size() -> int:
    try:
        return max(1, int(os.environ.get("REMCARD_LOCAL_METRICS_BATCH_SIZE", str(_DEFAULT_BATCH_SIZE))))
    except Exception:
        return _DEFAULT_BATCH_SIZE


def _flush_interval_sec() -> float:
    try:
        return max(0.1, float(os.environ.get("REMCARD_LOCAL_METRICS_FLUSH_SEC", str(_DEFAULT_FLUSH_INTERVAL_SEC))))
    except Exception:
        return _DEFAULT_FLUSH_INTERVAL_SEC


def _latest_change_min_interval_sec() -> float:
    try:
        return max(
            0.0,
            float(
                os.environ.get(
                    "REMCARD_LATEST_CHANGE_METRIC_MIN_INTERVAL_SEC",
                    str(_DEFAULT_LATEST_CHANGE_MIN_INTERVAL_SEC),
                )
            ),
        )
    except Exception:
        return _DEFAULT_LATEST_CHANGE_MIN_INTERVAL_SEC


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _metrics_path() -> str:
    global _CACHED_PATH_DAY, _CACHED_PATH
    day = datetime.now().strftime("%Y%m%d")
    with _PATH_LOCK:
        if _CACHED_PATH_DAY == day and _CACHED_PATH:
            return _CACHED_PATH
        log_dir = get_local_logs_dir()
        os.makedirs(log_dir, exist_ok=True)
        _CACHED_PATH_DAY = day
        _CACHED_PATH = os.path.join(log_dir, f"metrics_{day}.jsonl")
        return _CACHED_PATH


def _write_payloads(payloads: list[dict[str, Any]]) -> None:
    if not payloads:
        return
    try:
        with _METRICS_LOCK:
            with open(_metrics_path(), "a", encoding="utf-8") as fh:
                for payload in payloads:
                    fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _take_dropped_metric_payload() -> dict[str, Any] | None:
    global _DROPPED_METRICS
    with _DROPPED_LOCK:
        if _DROPPED_METRICS <= 0:
            return None
        dropped = int(_DROPPED_METRICS)
        _DROPPED_METRICS = 0
    return {
        "ts": _now_iso(),
        "metric": "local_metrics_dropped",
        "value": dropped,
        "host": _HOSTNAME,
        "pid": _PID,
    }


def _drain_queue(max_items: int | None = None) -> list[dict[str, Any]]:
    metrics_queue = _METRICS_QUEUE
    if metrics_queue is None:
        return []
    batch: list[dict[str, Any]] = []
    limit = max_items if max_items is not None else _batch_size()
    while len(batch) < limit:
        try:
            batch.append(metrics_queue.get_nowait())
        except queue.Empty:
            break
    dropped_payload = _take_dropped_metric_payload()
    if dropped_payload is not None:
        batch.append(dropped_payload)
    return batch


def _metrics_worker() -> None:
    interval = _flush_interval_sec()
    while not _METRICS_STOP.wait(interval):
        _write_payloads(_drain_queue())
    while True:
        batch = _drain_queue(max_items=_batch_size())
        if not batch:
            break
        _write_payloads(batch)


def _ensure_worker_started() -> queue.Queue:
    global _METRICS_QUEUE, _METRICS_THREAD
    with _QUEUE_LOCK:
        if _METRICS_QUEUE is None:
            _METRICS_QUEUE = queue.Queue(maxsize=_queue_size())
        if _METRICS_THREAD is None or not _METRICS_THREAD.is_alive():
            _METRICS_STOP.clear()
            _METRICS_THREAD = threading.Thread(
                target=_metrics_worker,
                name="RemCardLocalMetricsWriter",
                daemon=True,
            )
            _METRICS_THREAD.start()
        return _METRICS_QUEUE


def flush_metrics(timeout: float = 1.0) -> None:
    if not _enabled():
        return
    deadline = time.monotonic() + max(0.0, float(timeout or 0.0))
    while time.monotonic() <= deadline:
        batch = _drain_queue(max_items=_batch_size())
        if not batch:
            return
        _write_payloads(batch)


def shutdown_metrics(timeout: float = 1.0) -> None:
    _METRICS_STOP.set()
    thread = _METRICS_THREAD
    if thread is not None and thread.is_alive():
        thread.join(timeout=max(0.0, float(timeout or 0.0)))
    flush_metrics(timeout=timeout)


def _should_record_metric(name: str, value: Any, fields: dict[str, Any]) -> bool:
    if str(name) != "latest_change_id":
        return True

    # Fallback cursor reads are rare and diagnostically important: keep all of them.
    if str(fields.get("source") or "") == "fallback":
        return True

    interval = _latest_change_min_interval_sec()
    if interval <= 0:
        return True

    key = (
        str(name),
        fields.get("component"),
        fields.get("admission_id"),
        fields.get("include_global"),
        fields.get("source"),
    )
    now = time.monotonic()
    with _LATEST_CHANGE_METRIC_LOCK:
        previous = _LATEST_CHANGE_METRIC_STATE.get(key)
        if previous is None:
            _LATEST_CHANGE_METRIC_STATE[key] = (value, now)
            return True
        previous_value, previous_ts = previous
        if previous_value != value or (now - previous_ts) >= interval:
            _LATEST_CHANGE_METRIC_STATE[key] = (value, now)
            return True
    return False


def record_metric(name: str, value: Any = None, *, force_flush: bool = False, **fields: Any):
    if not _enabled():
        return
    if not _should_record_metric(str(name), value, fields):
        return
    payload = {
        "ts": _now_iso(),
        "metric": str(name),
        "value": value,
        "host": _HOSTNAME,
        "pid": _PID,
    }
    payload.update(fields)
    if _sync_mode() or force_flush:
        _write_payloads([payload])
        return
    try:
        _ensure_worker_started().put_nowait(payload)
    except queue.Full:
        global _DROPPED_METRICS
        with _DROPPED_LOCK:
            _DROPPED_METRICS += 1
    except Exception:
        pass


atexit.register(shutdown_metrics)
