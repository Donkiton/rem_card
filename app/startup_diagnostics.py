"""Read-only, bounded startup telemetry. Never records SQL or patient data."""
import ctypes
import functools
import os
import threading
import time
import uuid
from contextlib import contextmanager

_started = time.perf_counter()
_run_id = uuid.uuid4().hex
_attempt = {}
_attempt_number = 0


def _sample():
    result = {"process_cpu_ms": time.process_time() * 1000}
    if os.name == "nt":
        try:
            class IO(ctypes.Structure):
                _fields_ = [(name, ctypes.c_ulonglong) for name in
                            ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetCurrentProcess.restype = ctypes.c_void_p
            kernel.GetProcessIoCounters.argtypes = [ctypes.c_void_p, ctypes.POINTER(IO)]
            kernel.GetTickCount64.restype = ctypes.c_ulonglong
            counters = IO()
            if kernel.GetProcessIoCounters(kernel.GetCurrentProcess(), ctypes.byref(counters)):
                result.update({name: getattr(counters, name) for name, _ in IO._fields_})
            result["boot_uptime_ms"] = kernel.GetTickCount64()
        except Exception:
            pass
    return result


def event(stage, **fields):
    try:
        from rem_card.app.local_metrics import record_metric
        from rem_card.app.version import APP_VERSION
        record_metric("startup_diagnostic", 1, run_id=_run_id, version=APP_VERSION,
                      stage=stage, since_start_ms=round((time.perf_counter()-_started)*1000, 3),
                      **fields)
    except Exception:
        pass


def start():
    event("application_start", **_sample())


def role_requested(role, session_id):
    global _attempt, _attempt_number
    _attempt_number += 1
    _attempt = dict(role=role, session_id=session_id, attempt=_attempt_number,
                    first_role=_attempt_number == 1, requested_at=time.perf_counter())
    event("role_requested", **{k: v for k, v in _attempt.items() if k != "requested_at"})


def role_ready():
    context = dict(_attempt)
    began = context.pop("requested_at", None)
    event("role_ready", **context,
          request_to_ready_ms=round((time.perf_counter()-began)*1000, 3) if began else None)


@contextmanager
def span(stage):
    started = time.perf_counter()
    before = _sample()
    context = {k: v for k, v in _attempt.items() if k != "requested_at"}
    span_id = uuid.uuid4().hex
    event(stage, phase="begin", span_id=span_id, **context)
    outcome = "ok"
    try:
        yield
    except BaseException as exc:
        outcome = type(exc).__name__
        raise
    finally:
        after = _sample()
        delta = {k + "_delta": round(after[k]-v, 3) for k, v in before.items()
                 if k in after and k != "boot_uptime_ms"}
        event(stage, phase="end", span_id=span_id, outcome=outcome,
              elapsed_ms=round((time.perf_counter()-started)*1000, 3),
              thread_id=threading.get_ident(), **context, **delta)


def measured(stage):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            with span(stage):
                return fn(*args, **kwargs)
        return wrapped
    return decorate
