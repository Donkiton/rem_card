"""Bounded observation only: no SQL, lock takeover, retries or UI notifications."""
from __future__ import annotations

import functools
import hashlib
import logging
import os
import re
import socket
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import import_module

THRESHOLD_SEC = 5.0
SNAPSHOT_INTERVAL_SEC = 60.0
MAX_ACTIVE = 128
_lock = threading.RLock()
_active = {}
_thread = None
_last_snapshot = float('-inf')
_SESSION_ID = uuid.uuid4().hex


def identity():
    return dict(pid=os.getpid(), host=socket.gethostname(),
                role=_source(os.environ.get('REMCARD_UI_ROLE')), session_id=_SESSION_ID)


def _resource(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:16] if value else ''


def _emit(event, payload):
    try:
        import json
        if not logging.getLogger('RemCard.DBWait').hasHandlers():
            # Spawned DB workers also need persistent local logs. Configure
            # the existing logger lazily, only when there is an event to save.
            import_module('rem_card.app.logger')
        logging.getLogger('RemCard.DBWait').warning(
            '%s %s', event, json.dumps(payload, ensure_ascii=True, separators=(',', ':'))
        )
    except Exception:
        pass  # Diagnostics must not alter a clinical operation.


def _source(value):
    # Caller descriptions can include patient IDs or text. Keep only an
    # operation family, never SQL, parameters, exception text or frame locals.
    family = str(value or 'unknown').split(':', 1)[0]
    return family if re.fullmatch(r'[a-zA-Z_]{1,64}', family) else 'other'


class Operation:
    def __init__(self, source, resource='', stage='running', connection=None, database='', mutex=None):
        self.id = uuid.uuid4().hex
        self.started = time.monotonic()
        self.payload = dict(
            operation_id=self.id, thread_id=threading.get_ident(),
            source=_source(source), stage=stage, retries=0,
            resource=_resource(resource), database_resource=_resource(database or resource),
            started_at=datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
            stage_history=[], **identity(),
        )
        self.stage_started = self.started
        if mutex is not None:
            self.payload['mutex_id'] = f'{_SESSION_ID}:{id(mutex):x}'
        if connection is not None:
            self.bind_connection(connection)

    def stage(self, value):
        with _lock:
            now = time.monotonic()
            history = self.payload['stage_history']
            history.append(dict(stage=self.payload['stage'],
                                elapsed_ms=round((now-self.stage_started)*1000)))
            del history[:-16]
            self.payload['stage'] = value
            if value in ('connection_guard_held', 'central_io_held') and 'mutex_id' in self.payload:
                self.payload['held_mutex_stage'] = value
            self.stage_started = now

    def bind_connection(self, connection):
        # Identity only: never execute SQL or inspect a connection from the watcher.
        with _lock:
            self.payload['connection_id'] = f'{_SESSION_ID}:{id(connection):x}'

    def worker(self, pid):
        with _lock:
            self.payload['worker_pid'] = pid

    def retry(self):
        with _lock:
            self.payload['retries'] += 1


def mark_stage(stage):
    with _lock:
        current = [op for op in _active.values() if op.payload['thread_id'] == threading.get_ident()]
        if current:
            current[-1].stage(stage)


def mark_retry():
    with _lock:
        current = [op for op in _active.values() if op.payload['thread_id'] == threading.get_ident()]
        if current:
            current[-1].retry()


def bind_connection(connection):
    with _lock:
        current = [op for op in _active.values() if op.payload['thread_id'] == threading.get_ident()]
        if current:
            current[-1].bind_connection(connection)


def current_context():
    """Small safe signature for existing lock metadata and metric events."""
    with _lock:
        current = [op for op in _active.values() if op.payload['thread_id'] == threading.get_ident()]
        result = identity()
        for op in current:
            for key in ('operation_id', 'connection_id', 'started_at'):
                if key in op.payload:
                    result[key] = op.payload[key]
        return result


def current_operation():
    with _lock:
        current = [op for op in _active.values() if op.payload['thread_id'] == threading.get_ident()]
        return current[-1] if current else None


def _operation_snapshot(op, now):
    result = dict(op.payload, stage_history=list(op.payload['stage_history']),
                  elapsed_ms=round((now-op.started)*1000),
                  stage_elapsed_ms=round((now-op.stage_started)*1000))
    held_stage = {'connection_guard_wait': 'connection_guard_held',
                  'central_io_wait': 'central_io_held'}.get(result['stage'])
    if held_stage:
        holders = []
        for candidate in _active.values():
            if (result.get('mutex_id') and candidate.payload.get('mutex_id') == result['mutex_id']
                    and candidate.payload['thread_id'] != result['thread_id']
                    and candidate.payload.get('held_mutex_stage') == held_stage):
                holders.append(dict(candidate.payload, stage_history=list(candidate.payload['stage_history'])))
                if len(holders) == 4:
                    break
        result.update(holder_kind='local_mutex', holder_confidence='observed' if holders else 'unknown',
                      local_holders=holders)
    elif result['stage'] in ('sqlite_begin_wait', 'commit', 'sqlite_commit_wait'):
        result.update(holder_kind='sqlite_native', holder_confidence='unknown')
    return result


def _snapshot():
    global _last_snapshot
    now = time.monotonic()
    with _lock:
        if now - _last_snapshot < SNAPSHOT_INTERVAL_SEC:
            return
        overdue = [op for op in _active.values() if now - op.started >= THRESHOLD_SEC]
        if not overdue:
            return
        _last_snapshot = now
        operations = [_operation_snapshot(op, now) for op in list(_active.values())[:MAX_ACTIVE]]
    frames = sys._current_frames()
    stacks = {}
    for tid in {op['thread_id'] for op in operations}:
        frame = frames.get(tid)
        entries = []
        while frame is not None and len(entries) < 16:
            entries.append(f'{os.path.basename(frame.f_code.co_filename)}:{frame.f_lineno}:{frame.f_code.co_name}')
            frame = frame.f_back
        stacks[str(tid)] = entries
    del frames
    _emit('DB_WAIT_SNAPSHOT', dict(**identity(), operations=operations, stacks=stacks))


def _watch():
    global _thread
    while True:
        time.sleep(1)
        with _lock:
            if not _active:
                _thread = None
                return
        try:
            _snapshot()
        except Exception:
            pass


@contextmanager
def observe(source, *, resource='', stage='running', connection=None, database='', mutex=None):
    global _thread
    op = Operation(source, resource, stage, connection, database, mutex)
    registered = False
    try:
        with _lock:
            if len(_active) < MAX_ACTIVE:
                parents = [x for x in _active.values() if x.payload['thread_id'] == op.payload['thread_id']]
                op.payload['parent_id'] = parents[-1].id if parents else ''
                _active[op.id] = op
                registered = True
                if _thread is None:
                    _thread = threading.Thread(target=_watch, name='DBWaitDiagnostics', daemon=True)
                    try:
                        _thread.start()
                    except Exception:
                        _thread = None
    except Exception:
        pass
    outcome = 'ok'
    try:
        yield op
    except BaseException as exc:
        outcome = type(exc).__name__
        raise
    finally:
        elapsed = time.monotonic() - op.started
        should_emit = elapsed >= THRESHOLD_SEC or outcome != 'ok'
        with _lock:
            final_payload = _operation_snapshot(op, time.monotonic()) if should_emit else None
            if registered:
                _active.pop(op.id, None)
        if should_emit:
            _emit('DB_WAIT_FINISHED', dict(final_payload, outcome=outcome))


def observed_scope(stage):
    """Decorate a generator underneath @contextmanager without changing it."""
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(self, *args, **kwargs):
            with observe(fn.__name__, resource=getattr(self, 'db_path', ''), stage=stage):
                yield from fn(self, *args, **kwargs)
        return wrapped
    return decorate


def observed_call(stage):
    """Observe synchronous calls without logging their arguments or result."""
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(self, *args, **kwargs):
            resource = getattr(self, 'db_path', '') or getattr(self, 'central_db_path', '')
            with observe(fn.__name__, resource=resource, stage=stage):
                return fn(self, *args, **kwargs)
        return wrapped
    return decorate
