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
    begin_wait = threading.Event()
    release_begin = threading.Event()
    failures = []
    calls = []
    real_mark_stage = sqlite_shared.mark_stage

    def mark_stage_with_handshake(stage):
        real_mark_stage(stage)
        if stage == 'sqlite_begin_wait' and not begin_wait.is_set():
            begin_wait.set()
            if not release_begin.wait(3):
                raise AssertionError('sqlite_begin_wait handshake was not released')

    monkeypatch.setattr(sqlite_shared, 'mark_stage', mark_stage_with_handshake)

    def operation():
        calls.append(1)
        with controller.transaction(conn, source='save_vital') as cursor:
            cursor.execute('INSERT INTO sample VALUES (1)')
    try:
        queue.submit(operation, 'save_vital:SECRET_PATIENT', on_error=failures.append, retries_left=1)
        queue.submit(done.set, 'following_action', retryable=False)
        assert begin_wait.wait(1)
        assert not done.is_set()
        monkeypatch.setattr(diagnostics, 'THRESHOLD_SEC', 0)
        monkeypatch.setattr(diagnostics, '_last_snapshot', float('-inf'))
        diagnostics._snapshot()
        snapshots = [p for event, p in events if event == 'DB_WAIT_SNAPSHOT']
        assert snapshots
        assert any(op['stage'] == 'sqlite_begin_wait' for op in snapshots[-1]['operations'])
        native_wait = next(op for op in snapshots[-1]['operations'] if op['stage'] == 'sqlite_begin_wait')
        assert native_wait['holder_kind'] == 'sqlite_native'
        assert native_wait['holder_confidence'] == 'unknown'
        release_begin.set()
        assert done.wait(3)
        assert len(calls) == 2
        assert attempts.count('sqlite_locked_count') == 4
        assert len(failures) == 1
        assert 'SECRET_PATIENT' not in str(events)
        holder.execute('ROLLBACK')
        operation()
        assert holder.execute('SELECT count(*) FROM sample').fetchone()[0] == 1
    finally:
        release_begin.set()
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
            # Holding the mutex survives stage changes during a transaction.
            diagnostics.mark_stage('transaction_body')
            thread.start()
            assert not acquired.wait(.04)
            diagnostics._snapshot()
            snapshots = [p for event, p in events if event == 'DB_WAIT_SNAPSHOT']
            phases = {op['stage'] for op in snapshots[0]['operations']}
            assert {'connection_guard_wait', 'transaction_body'} <= phases
            waiting = next(op for op in snapshots[0]['operations'] if op['stage'] == 'connection_guard_wait')
            assert waiting['holder_confidence'] == 'observed'
            local_holder = waiting['local_holders'][0]
            assert local_holder['connection_id'] == waiting['connection_id']
            assert local_holder['thread_id'] == threading.get_ident()
            assert local_holder['pid'] == waiting['pid']
            assert local_holder['host'] == waiting['host']
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
    conn = sqlite3.connect(':memory:', isolation_level=None)
    controller = sqlite_shared.SQLiteWriteController(str(tmp_path/'x'), str(tmp_path/'lock'), 'test')
    try:
        conn.execute('CREATE TABLE sample(value INTEGER)')
        with controller.transaction(conn) as cursor:
            cursor.execute('INSERT INTO sample VALUES (1)')
        assert conn.execute('SELECT value FROM sample').fetchone() == (1,)
        assert not (tmp_path/'lock').exists()
    finally:
        conn.close()


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
        waiter = next(op for op in snapshot['operations'] if op['stage'] == 'central_io_wait')
        assert waiter['holder_confidence'] == 'observed'
        assert waiter['local_holders'][0]['source'] == 'test_writer'
    thread.join(2)
    assert entered.is_set()


def test_service_lock_signature_is_readable_by_other_clients(tmp_path, events, monkeypatch):
    monkeypatch.setenv('REMCARD_UI_ROLE', 'nurse')
    conn = sqlite3.connect(':memory:')
    lock = sqlite_shared.FileWriteLock(str(tmp_path / 'db.lock'))
    try:
        with diagnostics.observe('save_order:PRIVATE', connection=conn) as operation:
            assert lock.acquire('synthetic-node', 'save_order')
            holder = sqlite_shared.describe_sqlite_lock_holder(lock.lock_path)
            fields = sqlite_shared._lock_holder_metric_fields(holder)
            assert holder['holder_role'] == 'nurse'
            assert holder['holder_operation_id'] == operation.id
            assert holder['holder_connection_id'] == operation.payload['connection_id']
            assert fields['lock_holder_confidence'] == 'reported'
            assert fields['lock_holder_kind'] == 'file_lock'
            assert holder['holder_pid'] == operation.payload['pid']
            assert holder['holder_host'] == operation.payload['host']
    finally:
        lock.release()
        conn.close()
    missing = sqlite_shared._lock_holder_metric_fields(sqlite_shared.describe_sqlite_lock_holder(lock.lock_path))
    assert missing['lock_holder_confidence'] == 'unknown'
    assert missing['lock_holder_read_reason'] == 'missing'
    assert 'PRIVATE' not in str(events)


def test_native_blocker_is_not_misidentified_as_released_service_lock(tmp_path, events, monkeypatch):
    db = tmp_path / 'native.db'
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute('CREATE TABLE sample(value INTEGER)')
    holder.execute('BEGIN IMMEDIATE')
    writer = sqlite3.connect(db, isolation_level=None, timeout=.01)
    controller = sqlite_shared.SQLiteWriteController(str(db), str(tmp_path/'db.lock'), 'test', max_retries=2, retry_delay_ms=1)
    metrics = []
    monkeypatch.setattr(sqlite_shared, 'record_metric', lambda name, value, **kw: metrics.append((name, kw)))
    try:
        with pytest.raises(sqlite3.OperationalError):
            with controller.transaction(writer, source='test_write'):
                pytest.fail('native lock must prevent entry into the body')
        retry = next(data for name, data in metrics if name == 'sqlite_write_lock_wait_retry')
        assert retry['phase'] == 'begin_immediate'
        assert retry['sqlite_holder_confidence'] == 'unknown'
        assert retry['lock_holder_read_reason'] == 'missing'
        assert retry['released_file_lock_owner']['pid'] == retry['diagnostic_pid']
        assert retry['released_file_lock_owner']['connection_id'] == retry['diagnostic_connection_id']
        assert not (tmp_path/'db.lock').exists()
        finished = next(data for event, data in events if event == 'DB_WAIT_FINISHED' and data['source'] == 'connection_guard')
        assert finished['retries'] == 1
        assert any(stage['stage'] == 'sqlite_begin_wait' for stage in finished['stage_history'])
    finally:
        holder.rollback()
        holder.close()
        writer.close()


def test_fast_error_records_signature_without_exception_text(events):
    with pytest.raises(ValueError):
        with diagnostics.observe('synthetic'):
            raise ValueError('SECRET_PATIENT')
    event, payload = events[-1]
    assert event == 'DB_WAIT_FINISHED'
    assert payload['outcome'] == 'ValueError'
    assert payload['session_id'] and payload['host'] and payload['pid']
    assert payload['started_at'].endswith('+00:00')
    assert 'SECRET_PATIENT' not in str(events)


def test_same_database_resource_does_not_identify_unrelated_mutex_as_holder(events):
    mutex_a, mutex_b = threading.Lock(), threading.Lock()
    with diagnostics.observe('holder', resource='same-db', mutex=mutex_a) as holder:
        holder.stage('central_io_held')
        holder.payload['thread_id'] = -1
        with diagnostics.observe('waiter', resource='same-db', mutex=mutex_b, stage='central_io_wait') as waiter:
            waiter.started -= 1
            diagnostics._snapshot()
            snapshot = next(data for event, data in events if event == 'DB_WAIT_SNAPSHOT')
            waiting = next(op for op in snapshot['operations'] if op['source'] == 'waiter')
            assert waiting['holder_confidence'] == 'unknown'
            assert waiting['local_holders'] == []


def test_read_snapshot_has_connection_identity_and_releases_transaction(events):
    from types import SimpleNamespace
    from rem_card.data.dao.db_manager import DatabaseManager
    conn = sqlite3.connect(':memory:', isolation_level=None)
    manager = SimpleNamespace(db_path='synthetic-only', _thread_state=threading.local(),
                              _in_current_thread_remcard_transaction=lambda: False,
                              _open_readonly_central_connection=lambda: conn,
                              write_controller=SimpleNamespace(connection_guard=lambda conn: diagnostics.observe('close')))
    try:
        with DatabaseManager.central_read_snapshot_scope(manager):
            assert conn.in_transaction
            diagnostics.current_operation().started -= 1
            diagnostics._snapshot()
            snapshot = next(data for event, data in events if event == 'DB_WAIT_SNAPSHOT')
            operation = next(op for op in snapshot['operations'] if op['stage'] == 'read_snapshot_active')
            assert operation['connection_id']
        assert any(data['stage'] == 'read_snapshot_closed' for event, data in events if event == 'DB_WAIT_FINISHED')
        assert not hasattr(manager._thread_state, 'central_read_scope_conn')
    finally:
        conn.close()
