"""Small, local startup spans; never include patient data or exception text."""
from contextlib import contextmanager
from contextvars import ContextVar
import threading
import time
import uuid

_context = ContextVar("startup_diagnostic_context", default={})


def startup_context():
    return dict(_context.get())


@contextmanager
def startup_attempt(session_id, role):
    token = _context.set({"session_id": session_id, "role": role})
    try:
        yield
    finally:
        _context.reset(token)


def startup_event(stage, **fields):
    from rem_card.app.local_metrics import record_metric
    try:
        record_metric("startup_diagnostic", 1, stage=stage,
                      thread_id=threading.get_native_id(), **_context.get(), **fields)
    except Exception:
        pass


@contextmanager
def startup_span(stage, **fields):
    started = time.perf_counter()
    span_id = uuid.uuid4().hex
    startup_event(stage, phase="begin", span_id=span_id, **fields)
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        startup_event(stage, phase="end", span_id=span_id, outcome=outcome,
                      elapsed_ms=round((time.perf_counter() - started) * 1000, 3), **fields)
