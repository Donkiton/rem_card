"""Bounded runtime read failures, without SQL, connection probes or patient data."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import logging
import re
import threading
import time

from rem_card.app.db_wait_diagnostics import current_context
from rem_card.app.local_metrics import record_metric
from rem_card.app.write_operation_diagnostics import write_error_fields


REPEAT_INTERVAL_SEC = 30.0
MAX_STREAMS = 128
MAX_EVENTS_PER_WINDOW = 32
_lock = threading.Lock()
_streams = OrderedDict()
_window_started = float("-inf")
_window_emitted = 0
_window_suppressed = 0


def _label(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", value) else "other"


def _permit(payload):
    global _window_started, _window_emitted, _window_suppressed
    now = time.monotonic()
    key = (payload["database_resource"], payload["source"], payload["phase"],
           payload["connection_scope"], payload.get("error_class"), payload.get("sqlite_errorcode"))
    with _lock:
        previous = _streams.get(key)
        if previous is not None and now - previous["emitted_at"] < REPEAT_INTERVAL_SEC:
            previous["suppressed"] += 1
            return False
        if now - _window_started >= REPEAT_INTERVAL_SEC:
            _window_started, _window_emitted = now, 0
        if _window_emitted >= MAX_EVENTS_PER_WINDOW:
            _window_suppressed += 1
            return False
        _window_emitted += 1
        if previous is not None:
            payload["suppressed_before"] = previous["suppressed"]
        if _window_suppressed:
            payload["global_suppressed_before"] = _window_suppressed
            _window_suppressed = 0
        _streams[key] = {"emitted_at": now, "suppressed": 0}
        _streams.move_to_end(key)
        while len(_streams) > MAX_STREAMS:
            _streams.popitem(last=False)
        return True


def _emit(payload):
    try:
        record_metric("central_read_error", 1, **payload)
    except Exception:
        pass
    try:
        logging.getLogger("RemCard.Read").warning(
            "CENTRAL_READ_ERROR %s", json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
        )
    except Exception:
        pass


class ReadOperationDiagnostics:
    """Only assignments on success; collect context and codes on SQL failure."""

    def __init__(self, resource, source):
        self.resource = resource
        self.source = source
        self.phase = "connection_select"
        self.connection_scope = "unknown"
        self.connection_token = None
        self.uri_mode = "unknown"
        self.query_only_configured = None

    def bind(self, connection, *, scope, known_readonly=False):
        self.connection_token = id(connection)
        self.connection_scope = scope
        # Creation-site knowledge, not a new query of the current connection.
        self.uri_mode = "ro" if known_readonly else "unknown"
        self.query_only_configured = True if known_readonly else None

    def report(self, exc):
        try:
            # Connection failures already have central_connection_summary.
            if self.phase not in {"execute", "fetch_one", "fetch_all"}:
                return
            context = current_context()
            if self.connection_token is not None:
                context["connection_id"] = f"{context['session_id']}:{self.connection_token:x}"
            fields = write_error_fields(exc)
            payload = {
                **context, "schema_version": 1,
                "database_resource": hashlib.sha256(str(self.resource).encode("utf-8")).hexdigest()[:16],
                "source": _label(self.source), "phase": self.phase,
                "connection_scope": _label(self.connection_scope),
                "uri_mode_at_creation": self.uri_mode,
                "query_only_configured": self.query_only_configured,
                "error_class": _label(fields.get("error_class")),
            }
            for key in ("sqlite_errorcode", "sqlite_errorname", "winerror", "errno"):
                if key in fields:
                    payload[key] = fields[key]
            if _permit(payload):
                _emit(payload)
        except Exception:
            pass  # Preserve the original exception, notification and cleanup.
