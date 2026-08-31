"""Bounded, in-memory reduction of explicitly routine technical metrics.

No filesystem, DB or Qt work is performed here. Unknown events fail open to the
existing detailed writer; aggregation never controls the underlying operation.
"""
from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


WINDOW_SEC = 60.0
MAX_GROUPS = 128
MAX_DETAIL_SEC = 1800.0
LATENCY_BOUNDS_MS = (10, 50, 100, 500, 1000)
_LABELS = ("source", "operation", "role", "catalog_key", "status", "result", "reason")
_LOCK_SOURCES = {"local_replica_snapshot", "local_replica_snapshot_gate"}
_DURATION_METRICS = {"read_duration_ms", "orders_load_time_ms", "local_replica_sync_duration_ms"}
_SUPPORTED = _DURATION_METRICS | {
    "sqlite_write_lock_acquired", "sqlite_write_lock_released",
    "settings_cache_hit", "emergency_standby_refresh_skipped",
    "local_replica_sync_failed",
}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, OverflowError):
        return None


def _present(value: Any) -> bool:
    return value not in (None, "", False, 0, "0", "none", "None")


def _problem(payload: dict[str, Any]) -> bool:
    return (
        payload.get("metric") == "local_replica_sync_failed"
        or payload.get("status") in {"error", "failed", "timeout", "cancelled", "conflict", "unknown", "invalid"}
        or payload.get("committed") is False
        or any(_present(payload.get(key)) for key in (
            "error", "error_class", "failure_reason", "delta_failed", "fallback_used",
            "fallback_after_delta", "fallback", "cancelled", "conflict",
        ))
    )


def _duration(payload: dict[str, Any]) -> float | None:
    name = payload["metric"]
    if name in _DURATION_METRICS:
        return _number(payload.get("value"))
    key = {"sqlite_write_lock_acquired": "total_wait_ms",
           "sqlite_write_lock_released": "held_ms"}.get(name, "duration_ms")
    return _number(payload.get(key))


def _routine(payload: dict[str, Any], duration: float | None) -> bool:
    name = payload["metric"]
    if name in _DURATION_METRICS:
        if duration is None or duration >= 500:
            return False
        if name == "read_duration_ms":
            return payload.get("status") == "ok"
        if name == "local_replica_sync_duration_ms":
            return payload.get("result") == "unchanged"
        return (payload.get("source") != "fallback"
                and _number(payload.get("delta_time_ms", 0)) is not None
                and float(payload.get("delta_time_ms", 0)) < 500)
    if name in {"sqlite_write_lock_acquired", "sqlite_write_lock_released"}:
        duration_key = "total_wait_ms" if name.endswith("acquired") else "held_ms"
        return (
            payload.get("source") in _LOCK_SOURCES and payload.get("value") == 1
            and (duration_key not in payload or (duration is not None and duration < 100))
            and (_number(payload.get("attempts", 1)) == 1)
        )
    if name == "settings_cache_hit":
        return payload.get("value") == 1
    if name == "emergency_standby_refresh_skipped":
        return (payload.get("status") == "current"
                and payload.get("detail") == "standby is already current")
    return False


@dataclass
class _Bucket:
    labels: dict[str, Any]
    started: float
    first_ts: str
    last_ts: str
    count: int = 0
    raw_count: int = 0
    error_count: int = 0
    duration_count: int = 0
    duration_sum: float = 0.0
    duration_max: float = 0.0
    latency_counts: list[int] = field(default_factory=lambda: [0] * 6)

    def add(self, ts: str, duration: float | None, raw: bool, error: bool) -> None:
        self.last_ts = ts
        self.count += 1
        self.raw_count += int(raw)
        self.error_count += int(error)
        if duration is not None:
            self.duration_count += 1
            self.duration_sum += duration
            self.duration_max = max(self.duration_max, duration)
            index = next((i for i, bound in enumerate(LATENCY_BOUNDS_MS) if duration <= bound), 5)
            self.latency_counts[index] += 1

    def payload(self, now: float) -> dict[str, Any]:
        return {
            "metric": "local_metrics_summary", "value": self.count,
            "summary_version": 1, **self.labels,
            "first_ts": self.first_ts, "last_ts": self.last_ts,
            "window_sec": round(max(0.0, now - self.started), 3),
            "count": self.count, "raw_count": self.raw_count,
            "aggregated_count": self.count - self.raw_count,
            "error_count": self.error_count, "duration_count": self.duration_count,
            "duration_avg_ms": round(self.duration_sum / self.duration_count, 3) if self.duration_count else None,
            "duration_max_ms": self.duration_max if self.duration_count else None,
            "latency_bounds_ms": list(LATENCY_BOUNDS_MS),
            "latency_counts": self.latency_counts,
        }


class MetricAggregator:
    def __init__(self, *, clock=time.monotonic, wall_clock=time.time,
                 max_groups: int = MAX_GROUPS):
        self._clock = clock
        self._wall_clock = wall_clock
        self._max_groups = max(1, min(MAX_GROUPS, max_groups))
        self._lock = threading.Lock()
        self._buckets: dict[tuple[Any, ...], _Bucket] = {}
        self._sync_state: tuple[Any, ...] | None = None
        self._detail_deadline = 0.0
        self._detail_setting: str | None = None

    def detailed(self) -> bool:
        setting = os.environ.get("REMCARD_METRICS_DETAIL_UNTIL", "")
        with self._lock:
            if setting != self._detail_setting:
                self._detail_setting = setting
                try:
                    until = datetime.fromisoformat(setting)
                    remaining = until.timestamp() - self._wall_clock() if until.tzinfo else 0.0
                    remaining = min(MAX_DETAIL_SEC, max(0.0, remaining))
                except (ValueError, OverflowError, OSError):
                    remaining = 0.0
                self._detail_deadline = self._clock() + remaining
            return self._clock() < self._detail_deadline

    def observe(self, payload: dict[str, Any], *, force_raw: bool = False) -> bool:
        """Return True only when this record is represented by a pending summary."""
        name = payload.get("metric")
        if os.environ.get("REMCARD_METRICS_AGGREGATION_ENABLED", "1") == "0" or name not in _SUPPORTED:
            return False
        # Clinical write lock history is deliberately outside this policy.
        if name.startswith("sqlite_write_lock_") and payload.get("source") not in _LOCK_SOURCES:
            return False
        labels = {"source_metric": name}
        for key in _LABELS:
            value = payload.get(key)
            if value is not None:
                if not isinstance(value, (str, int, bool)) or len(str(value)) > 128:
                    return False
                labels[key] = value
        key = tuple(labels.items())
        duration = _duration(payload)
        error = _problem(payload)
        raw = force_raw or error or not _routine(payload, duration)
        now = self._clock()
        with self._lock:
            if name == "local_replica_sync_duration_ms":
                # Keep the first state and every cursor/cycle/result transition.
                state = tuple(str(payload.get(k, "")) for k in ("result", "change_cursor", "db_cycle"))
                if any(len(part) > 256 for part in state):
                    return False
                raw = raw or state != self._sync_state
                self._sync_state = state
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self._max_groups:
                    return False  # No eviction/loss when cardinality is exhausted.
                bucket = _Bucket(labels, now, str(payload["ts"]), str(payload["ts"]))
                self._buckets[key] = bucket
                if name == "emergency_standby_refresh_skipped":
                    raw = True  # Keep the first reason, summarize its repetitions.
            bucket.add(str(payload["ts"]), duration, raw, error)
        return not raw

    def drain(self, *, force: bool = False) -> list[dict[str, Any]]:
        now = self._clock()
        with self._lock:
            keys = [key for key, bucket in self._buckets.items()
                    if force or now - bucket.started >= WINDOW_SEC]
            return [self._buckets.pop(key).payload(now) for key in keys]
