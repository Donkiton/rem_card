"""Bounded observations of a replica copy; never changes its IO or decisions.

Step intervals run from the previous progress callback's exit (or backup start)
to the next callback's entry. They include SQLite IO/locks and the previous
BUSY/LOCKED retry sleep, but exclude the previous callback's work. An increase
in remaining pages is only an indicator, not an exact restart count.
"""
from __future__ import annotations

from contextvars import ContextVar
import functools
import hashlib
import re
import time
import uuid


_CURRENT = ContextVar("replica_copy_diagnostics", default=None)
_PHASES = frozenset({
    "source_open", "source_state", "source_stamp", "local_open", "local_state",
    "full_backup", "checkpoint", "quick_check", "fsync", "writer_probe",
    "lease_acquire", "gate_acquire", "gate_release", "lease_release",
})
_COUNTERS = ("process_cpu_ms", "read_ops", "write_ops", "other_ops",
             "read_bytes", "write_bytes", "other_bytes")


def _quiet(method):
    @functools.wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except Exception:
            return None  # Observations must never replace the operation result.
    return wrapped


def _sample():
    try:
        from rem_card.app.startup_diagnostics import _sample as sample_process
        return sample_process()
    except Exception:
        return {}


def _context(message):
    from rem_card.app.startup_diagnostics import startup_context
    from rem_card.app.db_wait_diagnostics import current_context
    startup = startup_context()
    operation = current_context()
    result = {}
    role = startup.get("role") or operation.get("role")
    if role in {"doctor", "nurse", "settings"}:
        result["role"] = role
    for name in ("session_id", "operation_id"):
        value = startup.get(name) or operation.get(name)
        if isinstance(value, str) and re.fullmatch(r"[a-fA-F0-9-]{1,64}", value):
            result[name] = value
    result["database_resource"] = hashlib.sha256(
        str(message.get("central_db_path") or "").encode("utf-8")).hexdigest()[:16]
    return result, bool(startup.get("role") or startup.get("session_id"))


class CopyDiagnostics:
    def __init__(self, scope, message):
        self.scope = scope
        self.copy_id = uuid.uuid4().hex
        self.started = time.perf_counter()
        self.before = _sample()
        self.context, self.startup = _context(message)
        self.phases = {}
        self.status_counts = {"ok": 0, "done": 0, "busy": 0, "locked": 0, "other": 0}
        self.callback_count = 0
        self.callback_ms = 0.0
        self.callback_writer_probe_ms = 0.0
        self.callback_active = False
        self.previous_callback_exit = None
        self.step_first_ms = None
        self.step_last_ms = None
        self.step_max_ms = 0.0
        self.step_total_ms = 0.0
        self.remaining_first = None
        self.remaining_last = None
        self.remaining_increase_count = 0
        self.page_count_first = None
        self.page_count_last = None
        self.page_count_max = 0
        self.backup_started = False
        self.source_sizes = {}
        self.result_status = "unknown"
        self.sync_mode = "unknown"
        self.failure_phase = "unknown"
        self.last_phase = "unknown"

    @_quiet
    def begin_phase(self, phase):
        if phase not in _PHASES:
            return None
        began = time.perf_counter()
        self.last_phase = phase
        if phase == "full_backup":
            self.backup_started = True
            self.previous_callback_exit = began
        return began

    @_quiet
    def end_phase(self, phase, began, failed=False):
        if began is None:
            return
        elapsed = max(0.0, (time.perf_counter() - began) * 1000)
        aggregate = self.phases.setdefault(phase, {"count": 0, "total_ms": 0.0, "max_ms": 0.0})
        aggregate["count"] += 1
        aggregate["total_ms"] += elapsed
        aggregate["max_ms"] = max(aggregate["max_ms"], elapsed)
        if phase == "writer_probe" and self.callback_active:
            self.callback_writer_probe_ms += elapsed
        if failed:
            self.failure_phase = phase

    def call(self, phase, function, *args, **kwargs):
        began = self.begin_phase(phase)
        failed = True
        try:
            result = function(*args, **kwargs)
            failed = False
            return result
        finally:
            self.end_phase(phase, began, failed)

    @_quiet
    def callback_begin(self, status, remaining, total):
        began = time.perf_counter()
        self.callback_active = True
        self.callback_count += 1
        label = {0: "ok", 101: "done", 5: "busy", 6: "locked"}.get(int(status) & 0xff, "other")
        self.status_counts[label] += 1
        if self.previous_callback_exit is not None:
            elapsed = max(0.0, (began - self.previous_callback_exit) * 1000)
            if self.step_first_ms is None:
                self.step_first_ms = elapsed
            self.step_last_ms = elapsed
            self.step_max_ms = max(self.step_max_ms, elapsed)
            self.step_total_ms += elapsed
        remaining, total = max(0, int(remaining)), max(0, int(total))
        if self.remaining_first is None:
            self.remaining_first = remaining
            self.page_count_first = total
        if self.remaining_last is not None and remaining > self.remaining_last:
            self.remaining_increase_count += 1
        self.remaining_last = remaining
        self.page_count_last = total
        self.page_count_max = max(self.page_count_max, total)
        return began

    @_quiet
    def callback_end(self, began):
        ended = time.perf_counter()
        if began is not None:
            self.callback_ms += max(0.0, (ended - began) * 1000)
        self.previous_callback_exit = ended
        self.callback_active = False

    @_quiet
    def source_stamp(self, which, stamp):
        if which in {"before", "after"} and isinstance(stamp, dict):
            size = stamp.get("size")
            if isinstance(size, int) and size >= 0:
                self.source_sizes["source_size_" + which + "_bytes"] = size

    @_quiet
    def result(self, result):
        if isinstance(result, dict):
            if result.get("status") in {"snapshot_ready", "unchanged", "full_required"}:
                self.result_status = result["status"]
            if result.get("sync_mode") in {"full", "delta", "cache", "unchanged"}:
                self.sync_mode = result["sync_mode"]

    @_quiet
    def note_error(self):
        if self.failure_phase == "unknown":
            self.failure_phase = self.last_phase

    @_quiet
    def finish(self, error):
        # A normal background poll runs every two seconds. Preserve its existing
        # aggregation: raw copy summaries are only for actual full backups or
        # an explicit startup attempt (including reuse and early refusals).
        if not self.backup_started and not self.startup:
            return
        ended = time.perf_counter()
        after = _sample()
        deltas = {key + "_delta": round(max(0, after[key] - self.before[key]), 3)
                  for key in _COUNTERS if key in after and key in self.before}
        phases = {name: {key: round(value, 3) if key != "count" else value
                         for key, value in aggregate.items()}
                  for name, aggregate in self.phases.items()}
        from rem_card.app.local_metrics import record_metric
        record_metric("replica_copy_summary", 1, schema_version=1, copy_id=self.copy_id,
            scope=self.scope, outcome="error" if error is not None else "ok",
            error_class=type(error).__name__ if error is not None else "",
            failure_phase=self.failure_phase if error is not None else "",
            result_status=self.result_status, sync_mode=self.sync_mode,
            total_ms=round(max(0, (ended - self.started) * 1000), 3),
            full_backup_ms=phases.get("full_backup", {}).get("total_ms", 0.0),
            phases=phases, sqlite_status_counts=dict(self.status_counts),
            callback_count=self.callback_count, callback_ms=round(self.callback_ms, 3),
            callback_writer_probe_ms=round(self.callback_writer_probe_ms, 3),
            step_first_ms=round(self.step_first_ms, 3) if self.step_first_ms is not None else None,
            step_last_ms=round(self.step_last_ms, 3) if self.step_last_ms is not None else None,
            step_max_ms=round(self.step_max_ms, 3), step_total_ms=round(self.step_total_ms, 3),
            remaining_first=self.remaining_first, remaining_last=self.remaining_last,
            remaining_increase_count=self.remaining_increase_count,
            page_count_first=self.page_count_first, page_count_last=self.page_count_last,
            page_count_max=self.page_count_max,
            process_io_available=all(key in deltas for key in ("read_ops_delta", "read_bytes_delta")),
            **self.source_sizes, **self.context, **deltas)


class _NoDiagnostics:
    def call(self, phase, function, *args, **kwargs):
        return function(*args, **kwargs)

    def callback_begin(self, *args):
        return None

    def callback_end(self, *args):
        pass

    def source_stamp(self, *args):
        pass

    def note_error(self):
        pass


_NONE = _NoDiagnostics()


def current_copy():
    return _CURRENT.get() or _NONE


def observed_copy(scope):
    def decorate(function):
        @functools.wraps(function)
        def wrapped(message, *args, **kwargs):
            if _CURRENT.get() is not None:
                return function(message, *args, **kwargs)
            try:
                diagnostics = CopyDiagnostics(scope, message)
            except Exception:
                return function(message, *args, **kwargs)
            token = _CURRENT.set(diagnostics)
            error = None
            try:
                result = function(message, *args, **kwargs)
                diagnostics.result(result)
                return result
            except BaseException as exc:
                error = exc
                raise
            finally:
                _CURRENT.reset(token)
                diagnostics.finish(error)
        return wrapped
    return decorate
