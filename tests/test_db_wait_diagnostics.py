import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from rem_card.app import db_wait_diagnostics as diagnostics
from rem_card.app import sqlite_shared


@pytest.fixture
def events(monkeypatch):
    captured = []
    monkeypatch.setattr(diagnostics, '_emit', lambda event, payload: captured.append((event, payload)))
    monkeypatch.setattr(diagnostics, 'THRESHOLD_SEC', .02)
    monkeypatch.setattr(diagnostics, '_last_snapshot', float('-inf'))
    monkeypatch.setattr(sqlite_shared, 'record_metric', lambda *args, **kwargs: None)
    return captured


def test_nested_retries_delay_next_task_without_identifying_original_holder(tmp_path, events, monkeypatch):
    db = tmp_path / 'synthetic.db'
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute('CREATE TABLE sample(value INTEGER)')
    holder.execute('BEGIN IMMEDIATE')
    conn = sqlite3.connect(db, isolation_level=None, timeout=.05, check_same_thread=False)
    controller = sqlite_shared.SQLiteWriteController(str(db), str(tmp_path/'db.lock'), 'test', max_retries=2, retry_delay_ms=1)
    attempts = []
    monkeypatch.setattr(sqlite_shared, 'record_metric', lambda name, value, **kw: attempts.append(name))
    queue = sqlite_shared.LocalWriteQueue()
    done = threading.Event()
    failures = []
    calls = []
    def operation():
        calls.append(1)
        with controller.transaction(conn, source='save_vital') as cursor:
            cursor.execute('INSERT INTO sample VALUES (1)')
    try:
        queue.submit(operation, 'save_vital:SECRET_PATIENT', on_error=failures.append, retries_left=1)
        queue.submit(done.set, 'following_action', retryable=False)
        assert not done.wait(.03)
        diagnostics._snapshot()
        assert done.wait(3)
        assert len(calls) == 2
        assert attempts.count('sqlite_locked_count') == 4
        assert len(failures) == 1
        snapshots = [p for event, p in events if event == 'DB_WAIT_SNAPSHOT']
        assert snapshots
        assert any(op['stage'] == 'sqlite_begin_wait' for op in snapshots[0]['operations'])
        assert 'SECRET_PATIENT' not in str(events)
        holder.execute('ROLLBACK')
        operation()
        assert holder.execute('SELECT count(*) FROM sample').fetchone()[0] == 1
    finally:
        queue.shutdown(3)
        if holder.in_transaction:
            holder.rollback()
        conn.close()
        holder.close()


def test_connection_guard_snapshot_shows_holder_and_waiter(tmp_path, events):
    controller = sqlite_shared.SQLiteWriteController(str(tmp_path/'x'), str(tmp_path/'lock'), 'test')
    conn = sqlite3.connect(':memory:', check_same_thread=False)
    acquired = threading.Event()
    def waiter():
        with controller.connection_guard(conn):
            acquired.set()
    thread = threading.Thread(target=waiter)
    try:
        with controller.connection_guard(conn):
            thread.start()
            assert not acquired.wait(.04)
            diagnostics._snapshot()
            snapshots = [p for event, p in events if event == 'DB_WAIT_SNAPSHOT']
            phases = {op['stage'] for op in snapshots[0]['operations']}
            assert {'connection_guard_wait', 'connection_guard_held'} <= phases
        thread.join(2)
        assert acquired.is_set()
    finally:
        thread.join(2)
        conn.close()


def test_rollback_and_commit_are_observed_without_changing_data(tmp_path, events):
    conn = sqlite3.connect(':memory:', isolation_level=None)
    conn.execute('CREATE TABLE sample(value INTEGER)')
    controller = sqlite_shared.SQLiteWriteController(str(tmp_path/'x'), str(tmp_path/'lock'), 'test')
    try:
        with pytest.raises(ValueError):
            with controller.transaction(conn, source='test_write') as cursor:
                cursor.execute('INSERT INTO sample VALUES (1)')
                time.sleep(.03)
                raise ValueError('private data must not be logged')
        assert conn.execute('SELECT count(*) FROM sample').fetchone()[0] == 0
        with controller.transaction(conn, source='test_write') as cursor:
            cursor.execute('INSERT INTO sample VALUES (2)')
            time.sleep(.03)
        assert conn.execute('SELECT value FROM sample').fetchall() == [(2,)]
        finished = [p for e, p in events if e == 'DB_WAIT_FINISHED']
        assert any(p['stage'] == 'rolled_back' and p['outcome'] == 'ValueError' for p in finished)
        assert any(p['stage'] == 'committed' for p in finished)
        assert 'private data' not in str(events)
    finally:
        conn.close()


def test_snapshot_is_rate_limited_and_fast_operations_are_quiet(events, monkeypatch):
    with diagnostics.observe('quick'):
        pass
    assert not events
    with diagnostics.observe('slow') as op:
        op.started -= 1
        diagnostics._snapshot()
        diagnostics._snapshot()
    assert sum(e == 'DB_WAIT_SNAPSHOT' for e, _ in events) == 1
    with diagnostics._lock:
        assert not diagnostics._active


def test_logging_failure_does_not_change_transaction(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('log unavailable')
    monkeypatch.setattr(diagnostics.logging.getLogger('RemCard.DBWait'), 'warning', fail)
    monkeypatch.setattr(diagnostics, 'THRESHOLD_SEC', 0)
    with diagnostics.observe('operation'):
        pass


def test_central_io_scope_exposes_waiter_and_holder_without_database_initialization(events):
    from types import SimpleNamespace
    from rem_card.data.dao.db_manager import DatabaseManager
    manager = SimpleNamespace(_central_io_lock=threading.RLock(), db_path='synthetic-only')
    entered = threading.Event()
    def waiter():
        with DatabaseManager._central_io_lock_scope(manager, 'test_reader'):
            entered.set()
    thread = threading.Thread(target=waiter)
    with DatabaseManager._central_io_lock_scope(manager, 'test_writer'):
        thread.start()
        assert not entered.wait(.04)
        diagnostics._snapshot()
        snapshot = next(payload for event, payload in events if event == 'DB_WAIT_SNAPSHOT')
        assert {'central_io_wait', 'central_io_held'} <= {op['stage'] for op in snapshot['operations']}
    thread.join(2)
    assert entered.is_set()
