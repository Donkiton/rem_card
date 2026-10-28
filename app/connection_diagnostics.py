"""Local, bounded timings for connection setup; never query the database here."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import logging
import re
import threading
import time

from rem_card.app.local_metrics import record_metric

SLOW_CONNECTION_MS = 1000.0
REPEAT_INTERVAL_SEC = 30.0
MAX_STREAMS = 128
MAX_PHASES = 16
_current = ContextVar("connection_diagnostic", default=None)
_lock = threading.Lock()
_streams = OrderedDict()
_first = set()


def _label(value):
    text = str(value or "")
    return text if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", text) else "other"


def _error_fields(exc):
    result = {"error_class": _label(type(exc).__name__)}
    for key in ("sqlite_errorcode", "winerror", "errno"):
        value = getattr(exc, key, None)
        if isinstance(value, int):
            result[key] = value
    name = getattr(exc, "sqlite_errorname", None)
    if name:
        result["sqlite_errorname"] = _label(name)
    return result


def _permit(payload, now):
    """One baseline per resource/role and bounded repeated slow/error details."""
    resource_key = (payload["database_resource"], payload.get("role", "unknown"), payload["source"])
    with _lock:
        first = resource_key not in _first and len(_first) < MAX_STREAMS
        if first:
            _first.add(resource_key)
        if payload["outcome"] == "ok" and payload["elapsed_ms"] < SLOW_CONNECTION_MS:
            return first
        key = (*resource_key, payload["outcome"], payload.get("failure_phase", ""))
        previous = _streams.get(key)
        if previous is not None and now - previous["emitted_at"] < REPEAT_INTERVAL_SEC:
            previous["suppressed"] += 1
            previous["max_ms"] = max(previous["max_ms"], payload["elapsed_ms"])
            return False
        if previous is not None:
            payload["suppressed_before"] = previous["suppressed"]
            payload["window_max_ms"] = max(previous["max_ms"], payload["elapsed_ms"])
        _streams[key] = {"emitted_at": now, "suppressed": 0, "max_ms": payload["elapsed_ms"]}
        _streams.move_to_end(key)
        while len(_streams) > MAX_STREAMS:
            _streams.popitem(last=False)
        return True


def _emit(payload):
    try:
        record_metric("central_connection_summary", 1, **payload)
    except Exception:
        pass
    try:
        import json
        logging.getLogger("RemCard.Connection").info(
            "CENTRAL_CONNECTION_SUMMARY %s",
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
        )
    except Exception:
        pass


class _Observation:
    def __init__(self, resource, source):
        from rem_card.app.db_wait_diagnostics import current_context
        self.started = time.perf_counter()
        self.cpu_started = time.thread_time()
        self.phases = {}
        self.failure_phase = ""
        self.errors = {}
        self.context = current_context()
        try:
            from rem_card.app.startup_diagnostics import startup_context
            startup = startup_context()
            session = str(startup.get("session_id") or "")
            if session:
                self.context["startup_session_id"] = (
                    session if re.fullmatch(r"[a-fA-F0-9]{32}", session)
                    else hashlib.sha256(session.encode("utf-8")).hexdigest()[:32]
                )
            if startup.get("role") in {"doctor", "nurse"}:
                self.context["role"] = startup["role"]
        except Exception:
            pass
        self.resource = hashlib.sha256(str(resource).encode("utf-8")).hexdigest()[:16]
        self.source = _label(source)

    def finish(self, exc):
        payload = {
            **self.context,
            "schema_version": 1,
            "source": self.source,
            "database_resource": self.resource,
            "elapsed_ms": round((time.perf_counter() - self.started) * 1000, 3),
            "thread_cpu_ms": round((time.thread_time() - self.cpu_started) * 1000, 3),
            "phases": self.phases,
            "outcome": "ok" if exc is None else _label(type(exc).__name__),
            "failure_phase": self.failure_phase,
            **self.errors,
        }
        if exc is not None:
            payload.update(_error_fields(exc))
        if _permit(payload, time.monotonic()):
            _emit(payload)


class _Phase:
    def __init__(self, observation, name):
        self.observation = observation
        self.name = _label(name)
        self.started = time.perf_counter() if observation is not None else None
        self.finished = False

    def finish(self, exc=None):
        try:
            self._finish(exc)
        except Exception:
            pass

    def _finish(self, exc):
        if self.finished or self.observation is None:
            return
        self.finished = True
        observation = self.observation
        if self.name in observation.phases or len(observation.phases) < MAX_PHASES:
            phase = observation.phases.setdefault(self.name, {"count": 0, "total_ms": 0.0, "max_ms": 0.0})
            elapsed = max(0.0, (time.perf_counter() - self.started) * 1000)
            phase["count"] += 1
            phase["total_ms"] = round(phase["total_ms"] + elapsed, 3)
            phase["max_ms"] = round(max(phase["max_ms"], elapsed), 3)
        if exc is not None and not observation.failure_phase:
            observation.failure_phase = self.name
            observation.errors = _error_fields(exc)


@contextmanager
def connection_observation(resource, *, source="central_readonly"):
    observation = None
    token = None
    try:
        observation = _Observation(resource, source)
        token = _current.set(observation)
    except Exception:
        pass
    failure = None
    try:
        yield
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            if token is not None:
                _current.reset(token)
            if observation is not None:
                observation.finish(failure)
        except Exception:
            pass


@contextmanager
def connection_phase(name):
    phase = _Phase(None, "unobserved")
    try:
        observation = _current.get()
        if observation is not None:
            phase = _Phase(observation, name)
            from rem_card.app.db_wait_diagnostics import mark_stage
            mark_stage(name)
    except Exception:
        pass
    try:
        yield phase
    except BaseException as exc:
        try:
            phase.finish(exc)
        except Exception:
            pass
        raise
    finally:
        try:
            phase.finish()
        except Exception:
            pass
