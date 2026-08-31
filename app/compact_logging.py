"""Bounded production compaction for human-readable technical logs.

The policy never receives database objects and stores no formatted arguments in
breadcrumbs. Unknown INFO records fail open. Metrics have independent controls.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


MAX_DETAIL_SEC = 1800.0
INCIDENT_QUIET_SEC = 60.0
MAX_INCIDENTS = 128
MAX_BREADCRUMBS = 64
_CHECKPOINTS = {10, 100, 1000}
_IMPORTANT_TOKENS = (
    "error", "failed", "failure", "fallback", "timeout", "conflict",
    "cancel", "rejected", "stale", "late_result", "corrupt", "unavailable",
)
_ROUTINE_PREFIXES = (
    "[ordersclick]", "[tabperf]", "[ordersshow]",
    "patient_form_refresh_", "patient_beds_refresh_",
)
_ROUTINE_READ_TOKENS = (
    " cache_hit=1", " cache_current=1", " persistent_cache_hit=1",
    "orders_cache_lookup hit=1", "orders_ui_event=", "load_start ",
    "load_finish ", "orders_snapshot_step_start", "orders_snapshot_step_end",
    "orders_snapshot_sql_step_ms=", "build_orders_snapshot_time_ms=",
    "version_unchanged ", "evicted ", "invalidated ",
)


def compaction_enabled() -> bool:
    return os.environ.get("REMCARD_TEXT_LOG_COMPACTION_ENABLED", "1") != "0"


def enable_detailed_text_logs(seconds: float = MAX_DETAIL_SEC) -> str:
    duration = float(seconds)
    if not math.isfinite(duration) or not 0 <= duration <= MAX_DETAIL_SEC:
        raise ValueError(f"Text log detail duration must be between 0 and {MAX_DETAIL_SEC} seconds")
    until = (datetime.now(timezone.utc) + timedelta(seconds=duration)).isoformat()
    os.environ["REMCARD_TEXT_LOG_DETAIL_UNTIL"] = until
    return until


def _message_template(record: logging.LogRecord) -> str:
    return str(record.msg or "")[:1000]


def _is_routine_info(record: logging.LogRecord) -> bool:
    if record.levelno != logging.INFO:
        return False
    rendered = record.getMessage().casefold()
    if any(token in rendered for token in _IMPORTANT_TOKENS):
        return False
    if rendered.startswith(_ROUTINE_PREFIXES):
        return True
    if rendered.startswith("[readcoordinator]"):
        return any(token in rendered for token in _ROUTINE_READ_TOKENS)
    if rendered.startswith("[remcardservice]"):
        return any(token in rendered for token in (
            "orders_snapshot_step_start", "orders_snapshot_step_end",
            "orders_snapshot_sql_step_ms=",
        ))
    if rendered.startswith(("[orderswidget]", "[nurseorderswidget]")):
        return any(token in rendered for token in ("snapshot", "refresh", "loaded", "apply"))
    return False


def _bounded_text(value: Any, limit: int = 120) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    return text[:limit]


def _synthetic_record(message: str, *, level: int = logging.INFO) -> logging.LogRecord:
    record = logging.LogRecord(
        "RemCard.Diagnostics", level, __file__, 0, message, (), None,
        func="compact_logging",
    )
    record._remcard_compact_internal = True  # type: ignore[attr-defined]
    return record


@dataclass
class _Incident:
    fingerprint: str
    logger: str
    module: str
    function: str
    line: int
    first_wall: str
    first_mono: float
    last_wall: str
    last_mono: float
    count: int = 1
    reported_count: int = 1


class CompactLogPolicy:
    def __init__(self, *, clock=time.monotonic, wall_clock=time.time):
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.RLock()
        self._salt = secrets.token_bytes(16)
        self._detail_setting: str | None = None
        self._detail_deadline = 0.0
        self._incidents: dict[str, _Incident] = {}
        self._breadcrumbs: deque[dict[str, Any]] = deque(maxlen=MAX_BREADCRUMBS)

    def detailed(self) -> bool:
        setting = os.environ.get("REMCARD_TEXT_LOG_DETAIL_UNTIL", "")
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

    def _fingerprint(self, record: logging.LogRecord) -> str:
        exc_type = ""
        frames: list[tuple[str, int, str]] = []
        if record.exc_info:
            exc_type = getattr(record.exc_info[0], "__name__", "")
            tb = record.exc_info[2]
            while tb is not None:
                frame = tb.tb_frame
                frames.append((Path(frame.f_code.co_filename).name, tb.tb_lineno, frame.f_code.co_name))
                tb = tb.tb_next
        payload = repr((
            record.name, record.levelno, Path(record.pathname).name, record.lineno,
            _message_template(record), exc_type, frames[-8:], record.args,
        )).encode("utf-8", errors="replace")
        return hashlib.blake2b(payload, key=self._salt, digest_size=12).hexdigest()

    def _remember(self, record: logging.LogRecord, now: float) -> None:
        if getattr(record, "_remcard_compact_internal", False):
            return
        template = _message_template(record)
        event_key = hashlib.blake2b(
            repr((record.name, record.levelno, Path(record.pathname).name, record.lineno, template))
            .encode("utf-8", errors="replace"), key=self._salt, digest_size=8,
        ).hexdigest()
        item = {
            "age_anchor": now,
            "level": record.levelname,
            "logger": _bounded_text(record.name, 80),
            "module": _bounded_text(record.module, 80),
            "function": _bounded_text(record.funcName, 100),
            "line": max(0, int(record.lineno or 0)),
            "event_key": event_key,
            "count": 1,
        }
        if self._breadcrumbs:
            previous = self._breadcrumbs[-1]
            if all(previous.get(key) == item.get(key) for key in (
                "level", "logger", "module", "function", "line", "event_key",
            )):
                previous["age_anchor"] = now
                previous["count"] = int(previous.get("count") or 1) + 1
                return
        self._breadcrumbs.append(item)

    def _breadcrumb_record(self, now: float, fingerprint: str) -> logging.LogRecord:
        entries = []
        for source in list(self._breadcrumbs)[-20:]:
            item = {key: value for key, value in source.items() if key != "age_anchor"}
            item["age_ms"] = round(max(0.0, now - float(source["age_anchor"])) * 1000.0, 3)
            entries.append(item)
        payload = {"schema_version": 1, "incident": fingerprint, "events": entries}
        return _synthetic_record(
            "TECHNICAL_BREADCRUMBS " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            level=logging.WARNING,
        )

    def _summary_record(self, incident: _Incident, *, status: str) -> logging.LogRecord:
        payload = {
            "schema_version": 1,
            "fingerprint": incident.fingerprint,
            "logger": incident.logger,
            "module": incident.module,
            "function": incident.function,
            "line": incident.line,
            "first_ts": incident.first_wall,
            "last_ts": incident.last_wall,
            "duration_ms": round(max(0.0, incident.last_mono - incident.first_mono) * 1000.0, 3),
            "count": incident.count,
            "suppressed_count": max(0, incident.count - 1),
            "status": status,
        }
        incident.reported_count = incident.count
        return _synthetic_record(
            "LOG_INCIDENT_SUMMARY " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            level=logging.WARNING,
        )

    def _expired(self, now: float) -> list[logging.LogRecord]:
        result = []
        for key, incident in list(self._incidents.items()):
            if now - incident.last_mono < INCIDENT_QUIET_SEC:
                continue
            if incident.count > 1:
                result.append(self._summary_record(incident, status="quiet"))
            del self._incidents[key]
        return result

    def process(self, record: logging.LogRecord) -> list[logging.LogRecord]:
        if not compaction_enabled() or self.detailed() or getattr(record, "_remcard_compact_internal", False):
            return [record]
        now = self._clock()
        wall = datetime.now().astimezone().isoformat(timespec="milliseconds")
        with self._lock:
            output = self._expired(now)
            if record.levelno < logging.ERROR:
                self._remember(record, now)
                if _is_routine_info(record):
                    return output
                output.append(record)
                return output

            fingerprint = self._fingerprint(record)
            incident = self._incidents.get(fingerprint)
            if incident is None:
                if len(self._incidents) >= MAX_INCIDENTS:
                    output.append(record)  # Capacity exhaustion never hides an error.
                    return output
                incident = _Incident(
                    fingerprint=fingerprint,
                    logger=_bounded_text(record.name, 80),
                    module=_bounded_text(record.module, 80),
                    function=_bounded_text(record.funcName, 100),
                    line=max(0, int(record.lineno or 0)),
                    first_wall=wall, first_mono=now, last_wall=wall, last_mono=now,
                )
                self._incidents[fingerprint] = incident
                output.extend((self._breadcrumb_record(now, fingerprint), record))
                return output

            incident.count += 1
            incident.last_wall = wall
            incident.last_mono = now
            if incident.count in _CHECKPOINTS or (incident.count > 1000 and incident.count % 1000 == 0):
                output.append(self._summary_record(incident, status="active"))
            return output

    def close(self) -> list[logging.LogRecord]:
        with self._lock:
            result = [
                self._summary_record(item, status="shutdown")
                for item in self._incidents.values() if item.count > 1
            ]
            self._incidents.clear()
            self._breadcrumbs.clear()
            return result


class CompactLogHandler(logging.Handler):
    """Wrap one file handler without changing console or application behavior."""
    def __init__(self, target: logging.Handler, policy: CompactLogPolicy | None = None):
        super().__init__()
        self.target = target
        self.policy = policy or CompactLogPolicy()
        self._closed_once = False

    def emit(self, record: logging.LogRecord) -> None:
        try:
            records = self.policy.process(record)
        except Exception:
            records = [record]  # A diagnostics failure must never hide the source event.
        try:
            for item in records:
                self.target.handle(item)
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        if self._closed_once:
            return
        self._closed_once = True
        try:
            for item in self.policy.close():
                self.target.handle(item)
        finally:
            self.target.close()
            super().close()
