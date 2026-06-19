import threading
import time
from contextlib import contextmanager
from itertools import count
from typing import Iterable

from rem_card.app.local_metrics import record_metric


_LOCK = threading.Lock()
_ACTIVE_COUNTS: dict[str, int] = {}
_ACTIVE_READS: dict[str, dict[str, dict[str, object]]] = {}
_LAST_ACTIVITY: dict[str, dict[str, object]] = {}
_TOKEN_COUNTER = count(1)


def _normalize_name(name: str) -> str:
    return str(name or "foreground").strip().lower() or "foreground"


def _normalize_source(source: str) -> str:
    value = str(source or "refresh").strip().lower() or "refresh"
    if value == "user":
        return "click"
    if value in {"local_silent_sync", "stale_snapshot"}:
        return "monitor"
    if value not in {"click", "cache", "refresh", "post_finalize", "monitor"}:
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


def _activity_token(name: str, fields: dict[str, object]) -> str:
    request_id = str(fields.get("request_id") or "").strip()
    if request_id:
        return request_id
    return f"{name}:{threading.get_ident()}:{next(_TOKEN_COUNTER)}"


def _set_active_count_locked(name: str) -> int:
    active_reads = _ACTIVE_READS.get(name) or {}
    count_value = len(active_reads)
    if count_value:
        _ACTIVE_COUNTS[name] = count_value
    else:
        _ACTIVE_COUNTS.pop(name, None)
        _ACTIVE_READS.pop(name, None)
    return count_value


def _find_active_token_locked(name: str, request_id: str | None) -> tuple[str | None, dict[str, object] | None]:
    active_reads = _ACTIVE_READS.get(name) or {}
    if request_id:
        key = str(request_id)
        payload = active_reads.get(key)
        if payload is not None:
            return key, payload
        for token, payload in active_reads.items():
            if str(payload.get("request_id") or "") == key:
                return token, payload
    if len(active_reads) == 1:
        token, payload = next(iter(active_reads.items()))
        return token, payload
    return None, None


def mark_foreground_read_stalled(
    name: str,
    *,
    request_id: str | None = None,
    source: str = "refresh",
    **fields,
) -> bool:
    normalized_name = _normalize_name(name)
    normalized_source = _normalize_source(source)
    now = time.monotonic()
    with _LOCK:
        token, payload = _find_active_token_locked(normalized_name, request_id)
        if payload is None:
            return False
        payload["status"] = "stalled"
        payload["stalled_at"] = now
        payload["monotonic"] = now
        payload["source"] = normalized_source
        payload.update(fields)
        _LAST_ACTIVITY[normalized_name] = dict(payload)
    return True


def poison_foreground_read(
    name: str,
    *,
    request_id: str | None = None,
    source: str = "refresh",
    reason: str = "stalled_timeout",
    **fields,
) -> bool:
    normalized_name = _normalize_name(name)
    normalized_source = _normalize_source(source)
    now = time.monotonic()
    removed_payload: dict[str, object] | None = None
    with _LOCK:
        token, payload = _find_active_token_locked(normalized_name, request_id)
        if token is None or payload is None:
            return False
        active_reads = _ACTIVE_READS.get(normalized_name) or {}
        removed_payload = dict(active_reads.pop(token, payload))
        current_count = _set_active_count_locked(normalized_name)
        removed_payload.update(
            {
                "name": normalized_name,
                "source": normalized_source,
                "status": "poisoned",
                "poison_reason": reason,
                "monotonic": now,
                "active_until": now,
                **fields,
            }
        )
        _LAST_ACTIVITY[normalized_name] = removed_payload
    record_metric(
        "foreground_read_poisoned",
        1,
        activity_name=normalized_name,
        admission_id=removed_payload.get("admission_id") if removed_payload else None,
        source=normalized_source,
        active_count=current_count,
        request_id=request_id,
        reason=reason,
        **fields,
    )
    return True


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
    token = _activity_token(normalized_name, fields)
    with _LOCK:
        payload = {
            "name": normalized_name,
            "admission_id": admission_id,
            "source": normalized_source,
            "request_id": token,
            "status": "active",
            "monotonic": started,
            "active_until": started,
            **fields,
        }
        _ACTIVE_READS.setdefault(normalized_name, {})[token] = payload
        active_count = _set_active_count_locked(normalized_name)
        _LAST_ACTIVITY[normalized_name] = dict(payload)
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
            active_reads = _ACTIVE_READS.get(normalized_name) or {}
            was_active = token in active_reads
            if was_active:
                active_reads.pop(token, None)
            current_count = _set_active_count_locked(normalized_name)
            ended = time.monotonic()
            _LAST_ACTIVITY[normalized_name] = {
                "name": normalized_name,
                "admission_id": admission_id,
                "source": normalized_source,
                "request_id": token,
                "status": "finished",
                "monotonic": ended,
                "active_until": ended,
                "was_active": int(bool(was_active)),
                **fields,
            }
        record_metric(
            "foreground_read_end",
            round(elapsed_ms, 3),
            activity_name=normalized_name,
            admission_id=admission_id,
            source=normalized_source,
            active_count=current_count,
            was_active=int(bool(was_active)),
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
    latest_status = str(latest_payload.get("status") or "")
    if latest_status in {"poisoned", "expired", "superseded"}:
        return False, f"{latest_status}:{latest_name}", age_sec
    if now <= active_until:
        return True, f"recent:{latest_name}", age_sec
    if age_sec < idle_window:
        return True, f"recent:{latest_name}", age_sec
    return False, "idle", age_sec


def foreground_activity_snapshot(*, limit: int = 8) -> dict[str, object]:
    now = time.monotonic()
    max_items = max(1, int(limit or 1))
    with _LOCK:
        active_payloads = [
            dict(payload)
            for active_reads in _ACTIVE_READS.values()
            for payload in active_reads.values()
        ]
        recent_payloads = [dict(payload) for payload in _LAST_ACTIVITY.values()]

    def _compact(payload: dict[str, object]) -> dict[str, object]:
        latest_ts = float(payload.get("monotonic") or now)
        active_until = float(payload.get("active_until") or latest_ts)
        return {
            "name": str(payload.get("name") or ""),
            "source": str(payload.get("source") or ""),
            "status": str(payload.get("status") or ""),
            "admission_id": payload.get("admission_id"),
            "request_id": str(payload.get("request_id") or ""),
            "age_sec": round(max(0.0, now - latest_ts), 3),
            "active_for_sec": round(max(0.0, active_until - now), 3),
        }

    active_payloads.sort(key=lambda payload: float(payload.get("monotonic") or 0.0), reverse=True)
    recent_payloads.sort(key=lambda payload: float(payload.get("monotonic") or 0.0), reverse=True)
    return {
        "active_count": len(active_payloads),
        "recent_count": len(recent_payloads),
        "active": [_compact(payload) for payload in active_payloads[:max_items]],
        "recent": [_compact(payload) for payload in recent_payloads[:max_items]],
    }


def _reset_foreground_activity_for_tests() -> None:
    with _LOCK:
        _ACTIVE_COUNTS.clear()
        _ACTIVE_READS.clear()
        _LAST_ACTIVITY.clear()
