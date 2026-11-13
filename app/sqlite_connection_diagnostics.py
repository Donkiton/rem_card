"""Bounded local connection lifetimes; observations are candidates, never lock ownership.

Hooks run on the connection's owning thread and accept no SQL or parameters.
The registry keeps scalar metadata only: no connection/cursor references, SQLite
inspection, filesystem probes, network traffic, logging or worker of its own.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from contextlib import contextmanager
import functools
import hashlib
import itertools
import ntpath
import os
import re
import socket
import threading
import time
import uuid

MAX_CONNECTIONS = 128
MAX_CURSORS = 32
MAX_STATEMENTS = 16
MAX_CANDIDATES = 8
MAX_ALIASES = 32
MAX_RESOURCES = 256
MAX_HISTORY = 8
MAX_RECENT_CLEANUP = 16
MAX_CLEANUP_ERRORS = 4
_lock = threading.RLock()
_connections = OrderedDict()
_aliases = OrderedDict()
_resources = OrderedDict()
_sequence = itertools.count(1)
_session = uuid.uuid4().hex
_host = socket.gethostname()
_overflow = 0
_recent_cleanup = deque(maxlen=MAX_RECENT_CLEANUP)


def _safe(default=None):
    def decorate(function):
        @functools.wraps(function)
        def call(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except Exception:
                return default
        return call
    return decorate


def _family(value):
    text = str(value or "unknown").split(":", 1)[0]
    return text if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", text) else "other"


def _path(value):
    text = os.fsdecode(os.fspath(value)) if value else ""
    if not text:
        return ""
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    if ntpath.isabs(text) or ntpath.splitdrive(text)[0]:
        return ntpath.normcase(ntpath.normpath(text))
    return os.path.normcase(os.path.abspath(text))


def _canonical(key):
    for alias, target in sorted(_aliases.items(), key=lambda item: len(item[0]), reverse=True):
        prefix = alias.rstrip("\\/")
        if key == alias or key == prefix:
            return target
        if key.startswith(prefix + "\\"):
            return target.rstrip("\\/") + key[len(prefix):]
    return key


@_safe("")
def canonical_resource(database, *, canonical_database=None):
    """Pure cached path identity. Aliases must come from already known config.

    A mapped drive cannot be resolved reliably by string manipulation alone.
    Unregistered aliases remain distinct; this API never probes a share/drive.
    """
    key = _path(canonical_database or database)
    if not key:
        return ""
    with _lock:
        resource = _resources.get(key)
        if resource is None:
            resource = hashlib.sha256(_canonical(key).encode("utf-8")).hexdigest()[:16]
            _resources[key] = resource
            while len(_resources) > MAX_RESOURCES:
                _resources.popitem(last=False)
        return resource


@_safe()
def register_database_alias(alias, canonical):
    """Register a cached startup mapping (a database path or mapped drive root)."""
    alias, target = _path(alias), _path(canonical)
    if not alias or not target or alias == target:
        return
    with _lock:
        _aliases[alias] = target
        while len(_aliases) > MAX_ALIASES:
            _aliases.popitem(last=False)
        _resources.clear()
        for row in _connections.values():
            row["database_resource"] = canonical_resource(row["_database_key"])
            row["database_identity_kind"] = "configured_alias" if _canonical(row["_database_key"]) != row["_database_key"] else "normalized_path"


def _touch(row, event):
    row["updated"] = time.monotonic()
    row["history"].append((event, row["updated"]))


def _context():
    # Acquire the operation registry before our lock, never the reverse.
    from rem_card.app.db_wait_diagnostics import current_context
    current = current_context()
    return {"owner_operation_id": current.get("operation_id", ""),
            "operation_session_id": current.get("session_id", "")}


@_safe()
def register_connection(connection, database, *, source="unknown", readonly=None,
                        scope="unknown", canonical_database=None, new=False):
    """Factories use new=True; borrowed/controller hooks use idempotent default."""
    global _overflow
    if connection is None:
        return None
    if not new:
        with _lock:
            existing = _connections.get(id(connection))
            if existing is not None:
                return existing["connection_token"]
    context = _context()
    with _lock:
        existing = _connections.get(id(connection))
        if existing is not None and not new:
            return existing["connection_token"]
        if existing is None and len(_connections) >= MAX_CONNECTIONS:
            _overflow += 1
            return None
        now = time.monotonic()
        token = f"{_session}:{next(_sequence)}"
        _connections[id(connection)] = {
            "connection_token": token, "_database_key": _path(canonical_database or database),
            "database_resource": canonical_resource(database, canonical_database=canonical_database),
            "database_identity_kind": ("configured_canonical" if canonical_database else
                                       "configured_alias" if _canonical(_path(database)) != _path(database) else "normalized_path"),
            "host": _host, "pid": os.getpid(), "session_id": _session,
            "role": _family(os.environ.get("REMCARD_UI_ROLE")),
            "thread_id": threading.get_ident(), "source": _family(source),
            "scope": _family(scope), "readonly_at_creation": readonly if isinstance(readonly, bool) else None,
            "opened": now, "updated": now, "transaction": None, "transaction_started": None,
            "statements": {}, "cursors": {}, "statement_overflow": 0, "cursor_overflow": 0,
            "cleanup_phase": "", "cleanup_error": "", "quarantined": False,
            "quarantine_reason": "",
            "cleanup_errors": deque(maxlen=MAX_CLEANUP_ERRORS), "cleanup_error_count": 0,
            "history": deque([("opened", now)], maxlen=MAX_HISTORY),
            **context,
        }
        return token


@_safe()
def connection_token(connection):
    with _lock:
        return (_connections.get(id(connection)) or {}).get("connection_token")


@_safe()
def connection_token_by_id(identifier):
    with _lock:
        return (_connections.get(identifier) or {}).get("connection_token")


@_safe()
def transaction_started(connection, kind="read", *, source="unknown"):
    context = _context()
    with _lock:
        row = _connections.get(id(connection))
        if row is not None:
            row["transaction"] = kind if kind in {"read", "write"} else "unknown"
            row["transaction_started"] = time.monotonic()
            row["source"] = _family(source)
            row["thread_id"] = threading.get_ident()
            row.update(context)
            _touch(row, "transaction_started")


@_safe()
def transaction_finished(connection, outcome="committed"):
    with _lock:
        row = _connections.get(id(connection))
        if row is not None:
            row["transaction"] = row["transaction_started"] = None
            _touch(row, outcome if outcome in {"committed", "rolled_back"} else "transaction_finished")


@_safe()
def statement_started(connection, *, source="unknown"):
    context = _context()
    with _lock:
        row = _connections.get(id(connection))
        if row is None:
            return None
        row["thread_id"] = threading.get_ident()
        row.update(context)
        token = next(_sequence)
        if len(row["statements"]) >= MAX_STATEMENTS:
            row["statement_overflow"] += 1
            return None
        row["statements"][token] = (_family(source), time.monotonic())
        _touch(row, "statement_started")
        return token


@_safe()
def statement_finished(connection, token):
    with _lock:
        row = _connections.get(id(connection))
        if row is not None and token is not None:
            row["statements"].pop(token, None)
            _touch(row, "statement_finished")


@_safe()
def cursor_opened(connection, cursor, *, source="unknown"):
    with _lock:
        row = _connections.get(id(connection))
        if row is not None and cursor is not None:
            if id(cursor) not in row["cursors"] and len(row["cursors"]) >= MAX_CURSORS:
                row["cursor_overflow"] += 1
                return
            row["cursors"][id(cursor)] = (_family(source), time.monotonic())


@_safe()
def cursor_closed(connection, cursor):
    with _lock:
        row = _connections.get(id(connection))
        if row is not None:
            row["cursors"].pop(id(cursor), None)
            _touch(row, "cursor_closed")


@_safe()
def cleanup_failed(connection, *, phase, error_class):
    with _lock:
        row = _connections.get(id(connection))
        if row is not None:
            row["cleanup_phase"], row["cleanup_error"] = _family(phase), _family(error_class)
            row["cleanup_errors"].append((row["cleanup_phase"], row["cleanup_error"], time.monotonic()))
            row["cleanup_error_count"] += 1
            _touch(row, "cleanup_failed")


@_safe()
def quarantine_connection(connection, *, reason="cleanup_failed"):
    with _lock:
        row = _connections.get(id(connection))
        if row is not None:
            row["quarantined"] = True
            row["quarantine_reason"] = _family(reason)
            _touch(row, "quarantined")


@_safe()
def connection_closed(connection):
    with _lock:
        row = _connections.pop(id(connection), None)
        if row is not None and (row["cleanup_phase"] or row["quarantined"]):
            _touch(row, "closed")
            _recent_cleanup.append((_candidate(row, time.monotonic()), time.monotonic()))


def close_connection(connection):
    """Replace an existing owner-thread close, preserving its exact exception."""
    try:
        connection.close()
    except BaseException as exc:
        cleanup_failed(connection, phase="close", error_class=type(exc).__name__)
        raise
    else:
        connection_closed(connection)


@contextmanager
def closing_connection(connection):
    try:
        yield connection
    finally:
        close_connection(connection)


@contextmanager
def statement_scope(connection, *, source="unknown"):
    token = statement_started(connection, source=source)
    try:
        yield
    finally:
        statement_finished(connection, token)


def _candidate(row, now):
    statements = list(row["statements"].values())
    cursors = list(row["cursors"].values())
    return {
        **{key: row[key] for key in ("connection_token", "database_resource", "host", "pid", "session_id",
                                    "database_identity_kind",
                                    "role", "thread_id", "source", "scope", "readonly_at_creation",
                                    "owner_operation_id", "operation_session_id",
                                    "transaction", "quarantined", "cleanup_phase", "cleanup_error")},
        "quarantine_reason": row["quarantine_reason"],
        "cleanup_errors": [{"phase": phase, "error_class": error, "age_ms": round(max(0, now - when) * 1000)}
                           for phase, error, when in row["cleanup_errors"]],
        "cleanup_errors_truncated": row["cleanup_error_count"] > MAX_CLEANUP_ERRORS,
        "connection_age_ms": round(max(0, now - row["opened"]) * 1000),
        "transaction_age_ms": None if row["transaction_started"] is None else round(max(0, now - row["transaction_started"]) * 1000),
        "active_statements": len(statements), "open_cursors": len(cursors),
        "oldest_statement_ms": round(max((now - item[1] for item in statements), default=0) * 1000),
        "oldest_cursor_ms": round(max((now - item[1] for item in cursors), default=0) * 1000),
        "statement_sources": sorted({item[0] for item in statements}),
        "cursor_sources": sorted({item[0] for item in cursors}),
        "statement_overflow": row["statement_overflow"], "cursor_overflow": row["cursor_overflow"],
        "recent_states": [{"state": event, "age_ms": round(max(0, now - when) * 1000)}
                          for event, when in row["history"]],
        "holder_confidence": "candidate",
    }


@_safe()
def candidates(database_resource, *, exclude_connection_token=None, limit=MAX_CANDIDATES):
    """Snapshots contain no live objects; absence is not proof of no native owner."""
    now = time.monotonic()
    with _lock:
        rows = [row for row in _connections.values()
                if row["database_resource"] == database_resource
                and row["connection_token"] != exclude_connection_token
                and (row["transaction"] or row["statements"] or row["cursors"]
                     or row["cleanup_phase"] or row["quarantined"]
                     or row["statement_overflow"] or row["cursor_overflow"])]
        rows.sort(key=lambda row: (not row["quarantined"], not bool(row["cleanup_phase"]), row["opened"]))
        bounded = rows[:max(1, min(MAX_CANDIDATES, int(limit)))]
        recent = [{**row, "connection_state": "closed", "holder_confidence": "not_active",
                   "closed_ago_ms": round(max(0, now - closed) * 1000)}
                  for row, closed in _recent_cleanup if row["database_resource"] == database_resource]
        return {"local_connection_candidates": [_candidate(row, now) for row in bounded],
                "recent_connection_cleanup": recent[-MAX_CANDIDATES:],
                "recent_connection_cleanup_truncated": len(recent) > MAX_CANDIDATES,
                "connection_candidates_total": len(rows), "connection_candidates_truncated": len(rows) > len(bounded),
                "connection_registry_size": len(_connections), "connection_registry_overflow": _overflow,
                "database_aliases_configured": bool(_aliases),
                "connection_registry_scope": "process_local", "native_owner_identified": False}
