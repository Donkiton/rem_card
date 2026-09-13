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

THRESHOLD_SEC = 5.0
SNAPSHOT_INTERVAL_SEC = 60.0
MAX_ACTIVE = 128
_lock = threading.RLock()
_active = {}
_thread = None
_last_snapshot = float('-inf')


def _emit(event, payload):
    try:
        import json
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
    def __init__(self, source, resource='', stage='running'):
        self.id = uuid.uuid4().hex
        self.started = time.monotonic()
        self.payload = dict(
            operation_id=self.id, thread_id=threading.get_ident(),
            source=_source(source), stage=stage, retries=0,
            resource=hashlib.sha256(str(resource).encode()).hexdigest()[:16] if resource else '',
        )

    def stage(self, value):
        with _lock:
            self.payload['stage'] = value

    def retry(self):
        with _lock:
            self.payload['retries'] += 1


def mark_stage(stage):
    with _lock:
        current = [op for op in _active.values() if op.payload['thread_id'] == threading.get_ident()]
        if current:
            current[-1].payload['stage'] = stage


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
        operations = [dict(op.payload, elapsed_ms=round((now-op.started)*1000)) for op in list(_active.values())[:MAX_ACTIVE]]
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
    _emit('DB_WAIT_SNAPSHOT', dict(pid=os.getpid(), host=socket.gethostname(),
          role=_source(os.environ.get('REMCARD_UI_ROLE')), operations=operations, stacks=stacks))


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
def observe(source, *, resource='', stage='running'):
    global _thread
    op = Operation(source, resource, stage)
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
        with _lock:
            if registered:
                _active.pop(op.id, None)
        if elapsed >= THRESHOLD_SEC:
            _emit('DB_WAIT_FINISHED', dict(op.payload, pid=os.getpid(), host=socket.gethostname(),
                  outcome=outcome, elapsed_ms=round(elapsed*1000)))


def observed_scope(stage):
    """Decorate a generator underneath @contextmanager without changing it."""
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(self, *args, **kwargs):
            with observe(fn.__name__, resource=getattr(self, 'db_path', ''), stage=stage):
                yield from fn(self, *args, **kwargs)
        return wrapped
    return decorate
