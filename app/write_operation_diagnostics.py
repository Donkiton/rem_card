"""Bounded write observations; no database, filesystem or patient values."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import re
import time

from rem_card.app.local_metrics import record_metric


SLOW_WRITE_MS = 500.0
_CONTEXT = ContextVar("write_operation_diagnostic_context", default={})
_ID_FIELDS = ("accepted_write_id", "request_id", "operation_id", "session_id", "task_id")


def write_operation_family(source) -> str:
    family = str(source or "unknown").split(":", 1)[0]
    return family if re.fullmatch(r"[A-Za-z_]{1,64}", family) else "other"


def _opaque_id(value) -> str:
    text = str(value or "")
    if not text:
        return ""
    if re.fullmatch(r"[a-fA-F0-9]{32}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", text):
        return text
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def current_write_diagnostic_context() -> dict:
    return dict(_CONTEXT.get())


def write_diagnostic_fields(metadata=None) -> dict:
    values = {**_CONTEXT.get(), **(metadata or {})}
    fields = {key: _opaque_id(values[key]) for key in _ID_FIELDS if values.get(key)}
    role = values.get("role")
    if role in {"doctor", "nurse", "operblock", "settings"}:
        fields["role"] = role
    return fields


@contextmanager
def write_diagnostic_scope(metadata=None):
    token = _CONTEXT.set(write_diagnostic_fields(metadata))
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def _error_fields(exc) -> dict:
    """Numeric codes survive wrapper exceptions; exception text never leaves."""
    fields = {"error_class": type(exc).__name__ if exc is not None else ""}
    seen = set()
    while exc is not None and id(exc) not in seen and len(seen) < 8:
        seen.add(id(exc))
        for attribute in ("sqlite_errorcode", "winerror", "errno"):
            try:
                value = getattr(exc, attribute, None)
            except Exception:
                continue
            if type(value) is int and attribute not in fields:
                fields[attribute] = value
        try:
            name = getattr(exc, "sqlite_errorname", "")
        except Exception:
            name = ""
        if isinstance(name, str) and re.fullmatch(r"SQLITE_[A-Z0-9_]{1,64}", name):
            fields.setdefault("sqlite_errorname", name)
        remote_class = getattr(exc, "remote_error_class", "")
        if isinstance(remote_class, str) and re.fullmatch(r"[A-Za-z_]{1,64}", remote_class):
            fields.setdefault("remote_error_class", remote_class)
        outcome_unknown = getattr(exc, "outcome_unknown", None)
        if type(outcome_unknown) is bool:
            fields.setdefault("outcome_unknown", outcome_unknown)
        exc = exc.__cause__ or exc.__context__
    return fields


def write_error_fields(exc) -> dict:
    try:
        return _error_fields(exc)
    except Exception:
        return {"error_class": type(exc).__name__ if exc is not None else ""}


def _ignore_observation(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except Exception:
        return None


def safe_write_metric(name, value=None, **fields):
    try:
        record_metric(name, value, **fields)
    except Exception:
        pass


class WriteOperationDiagnostics:
    def __init__(self, source, metadata=None, *, clock=None, deadline=None):
        self.clock = clock or time.perf_counter
        self.started = 0.0
        self.started = self._now()
        self.deadline = deadline
        self.context = write_diagnostic_fields(metadata)
        self.family = write_operation_family(source)
        self.phases = {}
        self.stage = "prepare"
        self.failure_stage = ""
        self.error = {}
        self.cleanup_error = {}
        self.attempt_error = {}
        self.recovered_error = {}
        self.database_outcome = "not_started"
        self.nested = False
        self.queue_wait_ms = None
        self.retry_count = 0
        self.confirmation_result = ""

    def _now(self):
        try:
            return self.clock()
        except Exception:
            return self.started

    def note_error(self, exc, *, stage=None):
        fields = _ignore_observation(write_error_fields, exc) or {}
        error_stage = _ignore_observation(getattr, exc, "phase", None)
        failed_stage = stage or (error_stage if isinstance(error_stage, str) and error_stage in self.phases else self.stage)
        if not self.error:
            self.error = fields
            self.failure_stage = failed_stage
        elif self.error != fields:
            if failed_stage in {"rollback", "lock_release", "connection_close", "busy_timeout_restore"}:
                self.cleanup_error = fields
            else:
                if not self.attempt_error:
                    self.attempt_error = {**self.error, "failure_stage": self.failure_stage}
                self.error = fields
                self.failure_stage = failed_stage

    def recover_error(self):
        if self.error:
            self.recovered_error = {**self.error, "failure_stage": self.failure_stage}
            self.error = {}
            self.failure_stage = ""

    @contextmanager
    def phase(self, stage, *, deadline=None):
        started = self._now()
        self.stage = stage
        try:
            yield
        except BaseException as exc:
            _ignore_observation(self.note_error, exc, stage=stage)
            raise
        finally:
            _ignore_observation(self._record_phase, stage, started, deadline)

    def _record_phase(self, stage, started, deadline=None):
        elapsed = max(0.0, (self._now() - started) * 1000.0)
        row = self.phases.setdefault(stage, {"count": 0, "total_ms": 0.0})
        row["count"] += 1
        row["total_ms"] = round(row["total_ms"] + elapsed, 3)
        row["remaining_budget_ms"] = self.remaining_budget_ms(deadline)

    def remaining_budget_ms(self, deadline=None):
        budget_deadline = self.deadline if deadline is None else deadline
        if budget_deadline is None:
            return None
        return round(max(0.0, (budget_deadline - self._now()) * 1000.0), 3)

    def finish(self, metric):
        try:
            elapsed = round(max(0.0, (self._now() - self.started) * 1000.0), 3)
            if not self.error and not self.recovered_error and elapsed < SLOW_WRITE_MS:
                return
            result_fields = {"outcome_unknown": self.database_outcome == "unknown", **self.error}
            safe_write_metric(metric, elapsed, schema_version=1, operation=self.family,
                **self.context, phases=self.phases, failure_stage=self.failure_stage,
                database_outcome=self.database_outcome, nested=self.nested,
                retries=self.retry_count, queue_wait_ms=self.queue_wait_ms,
                remaining_budget_ms=self.remaining_budget_ms(), **result_fields,
                cleanup_error=self.cleanup_error, confirmation_result=self.confirmation_result,
                recovered_error=self.recovered_error, attempt_error=self.attempt_error)
        except Exception:
            pass

    @contextmanager
    def summary(self, metric):
        try:
            yield self
        except BaseException as exc:
            _ignore_observation(self.note_error, exc)
            raise
        finally:
            _ignore_observation(self.finish, metric)
