import threading
import time
from contextlib import contextmanager
from typing import Iterable

from rem_card.app.local_metrics import record_metric


_LOCK = threading.Lock()
_ACTIVE_COUNTS: dict[str, int] = {}
_LAST_ACTIVITY: dict[str, dict[str, object]] = {}


def _normalize_name(name: str) -> str:
    return str(name or "foreground").strip().lower() or "foreground"


def _normalize_source(source: str) -> str:
    value = str(source or "refresh").strip().lower() or "refresh"
    if value == "user":
        return "click"
    if value not in {"click", "cache", "refresh"}:
        return "refresh"
    return value


def mark_foreground_activity(
    name: str,
    *,
    admission_id=None,
    source: str = "refresh",
    ttl_sec: float = 2.0,
    **fields,
) -> None:
    normalized_name = _normalize_name(name)
    normalized_source = _normalize_source(source)
    now = time.monotonic()
    ttl = max(0.0, float(ttl_sec or 0.0))
    payload = {
        "name": normalized_name,
        "admission_id": admission_id,
        "source": normalized_source,
        "monotonic": now,
        "active_until": now + ttl,
    }
    payload.update(fields)
    with _LOCK:
        _LAST_ACTIVITY[normalized_name] = payload
    record_metric(
        "foreground_activity_marked",
        1,
        activity_name=normalized_name,
        admission_id=admission_id,
        source=normalized_source,
    )


@contextmanager
def foreground_read(
    name: str,
    *,
    admission_id=None,
    source: str = "refresh",
    **fields,
):
    normalized_name = _normalize_name(name)
    normalized_source = _normalize_source(source)
    started = time.monotonic()
    with _LOCK:
        _ACTIVE_COUNTS[normalized_name] = int(_ACTIVE_COUNTS.get(normalized_name, 0)) + 1
        _LAST_ACTIVITY[normalized_name] = {
            "name": normalized_name,
            "admission_id": admission_id,
            "source": normalized_source,
            "monotonic": started,
            "active_until": started,
            **fields,
        }
        active_count = int(_ACTIVE_COUNTS.get(normalized_name, 0))
    try:
        record_metric(
            "foreground_read_start",
            1,
            activity_name=normalized_name,
            admission_id=admission_id,
            source=normalized_source,
            active_count=active_count,
            **fields,
        )
        yield
    finally:
        elapsed_ms = (time.monotonic() - started) * 1000.0
        with _LOCK:
            current_count = max(0, int(_ACTIVE_COUNTS.get(normalized_name, 0)) - 1)
            if current_count:
                _ACTIVE_COUNTS[normalized_name] = current_count
            else:
                _ACTIVE_COUNTS.pop(normalized_name, None)
            ended = time.monotonic()
            _LAST_ACTIVITY[normalized_name] = {
                "name": normalized_name,
                "admission_id": admission_id,
                "source": normalized_source,
                "monotonic": ended,
                "active_until": ended,
                **fields,
            }
        record_metric(
            "foreground_read_end",
            round(elapsed_ms, 3),
            activity_name=normalized_name,
            admission_id=admission_id,
            source=normalized_source,
            active_count=current_count,
            **fields,
        )


def should_defer_background_io(
    *,
    idle_window_sec: float,
    names: Iterable[str] | None = None,
) -> tuple[bool, str, float | None]:
    now = time.monotonic()
    idle_window = max(0.0, float(idle_window_sec or 0.0))
    requested_names = {_normalize_name(name) for name in names} if names is not None else None
    with _LOCK:
        active_items = {
            name: count
            for name, count in _ACTIVE_COUNTS.items()
            if count > 0 and (requested_names is None or name in requested_names)
        }
        if active_items:
            active_name = sorted(active_items)[0]
            return True, f"active:{active_name}", 0.0

        candidates = [
            (name, payload)
            for name, payload in _LAST_ACTIVITY.items()
            if requested_names is None or name in requested_names
        ]

    if not candidates:
        return False, "idle", None

    latest_name, latest_payload = max(candidates, key=lambda item: float(item[1].get("monotonic") or 0.0))
    latest_ts = float(latest_payload.get("monotonic") or 0.0)
    active_until = float(latest_payload.get("active_until") or latest_ts)
    age_sec = max(0.0, now - latest_ts)
    if now <= active_until:
        return True, f"recent:{latest_name}", age_sec
    if age_sec < idle_window:
        return True, f"recent:{latest_name}", age_sec
    return False, "idle", age_sec


def _reset_foreground_activity_for_tests() -> None:
    with _LOCK:
        _ACTIVE_COUNTS.clear()
        _LAST_ACTIVITY.clear()
