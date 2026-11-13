"""Owned SQLite cleanup uses disposable local files, never a workstation DB."""
from contextlib import closing, contextmanager
from collections import OrderedDict
import gc
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from rem_card.app import sqlite_shared as shared
from rem_card.app import sqlite_connection_diagnostics as lifecycle
from rem_card.data.dao import db_manager as dbm
from rem_card.services.data_service import DataService
from rem_card.services.patient_bed_management.service import PatientBedManagementService


class TrackingCursor(sqlite3.Cursor):
    closed = False

    def close(self):
        self.closed = True
        return super().close()


class TrackingConnection(sqlite3.Connection):
    closed = False

    def cursor(self):
        cursor = super().cursor(factory=TrackingCursor)
        self.retained_cursors = getattr(self, "retained_cursors", [])
        self.retained_cursors.append(cursor)
        return cursor

    def close(self):
        self.closed = True
        return super().close()


class BrokenRollback:
    """Keep a real RESERVED lock until native close; optionally fail close once."""
    def __init__(self, connection, *, fail_close=False):
        self.connection = connection
        self.fail_close = fail_close
        self.close_calls = 0
        self.original = OSError("simulated commit transport failure")

    @property
    def in_transaction(self):
        return self.connection.in_transaction

    def cursor(self):
        return self.connection.cursor()

    def execute(self, sql, *args):
        if sql == "COMMIT":
            raise self.original
        if sql == "ROLLBACK":
            raise sqlite3.OperationalError("simulated rollback failure")
        return self.connection.execute(sql, *args)

    def close(self):
        self.close_calls += 1
        if self.fail_close:
            self.fail_close = False
            raise OSError("simulated close failure")
        self.connection.close()


@pytest.fixture
def manager(tmp_path):
    path = tmp_path / "medical.db"
    conn = sqlite3.connect(path, isolation_level=None, factory=TrackingConnection, check_same_thread=False)
    conn.executescript("CREATE TABLE entries(value INTEGER); INSERT INTO entries VALUES(1),(2),(3);")
    manager = object.__new__(dbm.DatabaseManager)
    manager.db_path = str(path)
    manager.runtime_context = SimpleNamespace(mode="local")
    manager._thread_state = threading.local()
    manager._central_io_lock = threading.RLock()
    manager._closed = manager._closing = False
    manager._remcard_conn = manager._journal_conn = conn
    manager._quarantined_write_conn = None
    manager._quarantined_write_closed = False
    manager._request_periodic_backup = lambda **_kwargs: None
    manager._after_write_committed = lambda: None
    manager.write_controller = shared.SQLiteWriteController(
        str(path), str(tmp_path / "db.lock"), "test", retry_delay_ms=10,
        on_connection_quarantined=manager._on_write_connection_quarantined,
    )
    yield manager
    current = manager._remcard_conn
    if current is not None:
        lifecycle.close_connection(current)
    lifecycle.close_connection(conn)


def _break_write(manager, *, fail_close=False):
    proxy = BrokenRollback(manager._remcard_conn, fail_close=fail_close)
    manager._remcard_conn = manager._journal_conn = proxy
    with pytest.raises(shared.SQLiteTransactionCleanupError) as caught:
        manager.run_write_operation(lambda cursor: cursor.execute("INSERT INTO entries VALUES(9)"))
    assert caught.value.outcome_unknown is True
    assert caught.value.__cause__ is proxy.original
    assert not isinstance(caught.value, sqlite3.OperationalError)
    return proxy


@pytest.mark.parametrize("fail_close", [False, True])
def test_owned_failed_rollback_quarantines_and_next_operation_recovers_once(manager, fail_close):
    proxy = _break_write(manager, fail_close=fail_close)
    assert manager._quarantined_write_conn is proxy
    assert proxy.close_calls == 1
    if fail_close:
        assert proxy.connection.in_transaction
        with pytest.raises(shared.SQLiteTransactionCleanupError):
            with manager.write_controller.transaction(proxy):
                pytest.fail("A poisoned transaction cannot be borrowed as nested")
    else:
        assert proxy.connection.closed
    with closing(sqlite3.connect(manager.db_path, isolation_level=None, timeout=0)) as observer:
        if not fail_close:
            observer.execute("BEGIN IMMEDIATE")
            observer.execute("ROLLBACK")
    replacement_counts = []
    original = manager._replace_quarantined_write_connection
    def replace(deadline):
        replacement_counts.append(deadline)
        return original(deadline)
    manager._replace_quarantined_write_connection = replace
    for value in (4, 5):
        manager.run_write_operation(lambda cursor: cursor.execute("INSERT INTO entries VALUES(?)", (value,)))
    assert replacement_counts == [None]
    assert proxy.connection.closed
    assert proxy.close_calls == (2 if fail_close else 1)
    assert manager._quarantined_write_conn is None
    assert manager._remcard_conn is manager._journal_conn
    assert [tuple(row) for row in manager._remcard_conn.execute("SELECT value FROM entries").fetchall()] == [(1,), (2,), (3,), (4,), (5,)]
    assert manager._remcard_conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert manager._remcard_conn.execute("PRAGMA synchronous").fetchone()[0] == 3
    assert manager._remcard_conn.execute("PRAGMA busy_timeout").fetchone()[0] == shared.SQLITE_BUSY_TIMEOUT_MS


def test_replacement_configuration_failure_closes_handle_and_does_not_replay(manager, monkeypatch):
    _break_write(manager)
    opened = []
    original = dbm.configure_connection
    def broken(conn, **kwargs):
        opened.append(conn)
        assert kwargs["preserve_journal_mode"] is True
        raise ValueError("configuration failed")
    monkeypatch.setattr(dbm, "configure_connection", broken)
    with pytest.raises(ValueError):
        manager.run_write_operation(lambda _: pytest.fail("Must not run before recovery is complete"))
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")
    assert manager._quarantined_write_conn is not None
    monkeypatch.setattr(dbm, "configure_connection", original)
    manager.run_write_operation(lambda cursor: cursor.execute("INSERT INTO entries VALUES(4)"))
    assert manager._remcard_conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 4


def test_recovery_cannot_reset_expired_budget_or_reopen_after_close(manager, monkeypatch):
    _break_write(manager)
    monkeypatch.setattr(dbm.sqlite3, "connect", lambda *_a, **_kw: pytest.fail("Must not open"))
    with manager.write_metadata_context({"write_deadline_monotonic": time.perf_counter() - 1}):
        with pytest.raises(shared.QueuedWriteDeadlineExceeded):
            manager.run_write_operation(lambda _: pytest.fail("Must not run"))
    manager._closed = True
    with pytest.raises(dbm.DatabaseClosedError):
        manager.run_write_operation(lambda _: pytest.fail("Must not run"))


def test_nested_failure_does_not_close_or_rollback_callers_transaction(manager):
    conn = manager._remcard_conn
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(ValueError):
        with manager.write_controller.transaction(conn) as cursor:
            cursor.execute("INSERT INTO entries VALUES(4)")
            raise ValueError("nested body failed")
    assert conn.in_transaction and not conn.closed
    assert manager._quarantined_write_conn is None
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 3


def test_base_exception_in_owned_body_cleans_transaction(manager):
    with pytest.raises(KeyboardInterrupt):
        with manager.write_controller.transaction(manager._remcard_conn) as cursor:
            cursor.execute("INSERT INTO entries VALUES(9)")
            raise KeyboardInterrupt()
    assert not manager._remcard_conn.in_transaction
    assert manager._remcard_conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 3


def test_borrowed_snapshot_rollback_failure_closes_real_lock_and_restores_context(manager):
    reader = sqlite3.connect(manager.db_path, isolation_level=None, factory=TrackingConnection)
    proxy = BrokenRollback(reader)
    with manager.existing_central_read_scope(proxy):
        with pytest.raises(sqlite3.OperationalError, match="rollback failure"):
            with manager.central_read_snapshot_scope():
                manager._fetch_one_central("SELECT * FROM entries")
        assert manager._scoped_central_read_connection() is proxy
        assert manager._thread_state.central_read_scope_depth == 1
    assert reader.closed and proxy.close_calls == 1
    assert manager._scoped_central_read_connection() is None
    manager._remcard_conn.execute("BEGIN IMMEDIATE")
    manager._remcard_conn.execute("INSERT INTO entries VALUES(4)")
    manager._remcard_conn.execute("COMMIT")


def test_borrowed_existing_transaction_remains_owned_by_caller(manager):
    with closing(sqlite3.connect(manager.db_path, isolation_level=None)) as reader:
        reader.execute("BEGIN")
        with manager.existing_central_read_scope(reader):
            with pytest.raises(ValueError):
                with manager.central_read_snapshot_scope():
                    manager._fetch_all_central("SELECT * FROM entries")
                    raise ValueError("body failed")
        assert reader.in_transaction
        reader.rollback()


def test_explicit_cursor_cleanup_releases_implicit_shared_lock_without_gc(manager):
    reader = sqlite3.connect(manager.db_path, isolation_level=None, factory=TrackingConnection)
    writer = manager._remcard_conn
    writer.execute("PRAGMA busy_timeout=0")
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        raw = reader.cursor()
        raw.execute("SELECT * FROM entries")
        raw.fetchone()
        assert not reader.in_transaction  # Native implicit read is still active.
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO entries VALUES(4)")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            writer.execute("COMMIT")
        raw.close()
        writer.execute("COMMIT")
        # Keep the cursor object alive on purpose. Successful helper cleanup
        # releases SHARED now, not during some later GUI garbage collection.
        with manager.existing_central_read_scope(reader):
            assert manager._fetch_one_central("SELECT * FROM entries")[0] == 1
        assert reader.retained_cursors[-1].closed
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO entries VALUES(5)")
        writer.execute("COMMIT")
    finally:
        if writer.in_transaction:
            writer.rollback()
        reader.close()
        if was_enabled:
            gc.enable()


@pytest.mark.parametrize("kind", ["one", "all"])
def test_borrowed_read_failure_retained_traceback_has_closed_cursor(manager, kind):
    conn = sqlite3.connect(manager.db_path, isolation_level=None, factory=TrackingConnection)
    errors = []
    with manager.existing_central_read_scope(conn):
        for _ in range(3):
            try:
                getattr(manager, "_fetch_" + kind + "_central")("SELECT * FROM nonexistent")
            except sqlite3.OperationalError as exc:
                errors.append(exc)  # Retain every traceback while asserting cleanup.
    assert len(errors) == 3
    assert all(cursor.closed for cursor in conn.retained_cursors)
    assert not conn.closed  # The external owner remains responsible for its handle.
    conn.close()


def test_readonly_configuration_failure_closes_owned_handle_immediately(manager, monkeypatch):
    opened = []
    def broken(conn, **_kwargs):
        opened.append(conn)
        raise ValueError("read configuration failed")
    monkeypatch.setattr(dbm, "configure_connection", broken)
    with pytest.raises(ValueError):
        manager._open_readonly_central_connection()
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")


@pytest.mark.parametrize("budget", [None, 40])
def test_terminal_native_begin_busy_retires_handle_without_executing_or_replaying_body(manager, monkeypatch, budget):
    old = manager._remcard_conn
    old.execute("PRAGMA busy_timeout=1")
    manager.write_controller.max_retries = 2
    expected = shared.SQLiteBeginBusyError if budget is None else shared.QueuedWriteDeadlineExceeded
    body_calls = []
    with closing(sqlite3.connect(manager.db_path, isolation_level=None)) as holder:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(expected) as caught:
            manager.run_write_operation(lambda cursor: body_calls.append(cursor),
                                        write_options={} if budget is None else {"write_budget_ms": budget})
        assert old.closed and manager._quarantined_write_conn is old
        assert body_calls == []
        assert DataService._terminal_write_state(caught.value) == "failed"
        holder.rollback()
    done = threading.Event()
    errors, queue_calls = [], []
    queue = shared.LocalWriteQueue()
    # Keep the exception outside Python's except-target lifetime.
    error = caught.value
    def failed(exc):
        errors.append(exc)
        done.set()
    def same_operation():
        queue_calls.append(1)
        raise error
    try:
        queue.submit(same_operation, "finished_native_begin", on_error=failed)
        assert done.wait(2)
    finally:
        assert queue.shutdown(2)
    assert queue_calls == [1] and errors == [error]
    assert manager._quarantined_write_conn is old  # Queue did not invoke recovery.

    statements = []
    original = dbm.configure_connection
    def configured(conn, **kwargs):
        conn.set_trace_callback(statements.append)
        return original(conn, **kwargs)
    monkeypatch.setattr(dbm, "configure_connection", configured)
    monkeypatch.setattr(manager, "_init_connections", lambda: pytest.fail("No startup initializer"))
    assert manager._fetch_one_central("SELECT COUNT(*) FROM entries", use_write_connection=True)[0] == 3
    manager.run_write_operation(lambda cursor: cursor.execute("INSERT INTO entries VALUES(4)"))
    assert manager._remcard_conn is not old
    assert manager._remcard_conn is manager._journal_conn
    assert not any("JOURNAL_MODE" in sql.upper() or "CREATE" in sql.upper() for sql in statements)
    assert manager._remcard_conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 4


def test_text_only_busy_error_does_not_recycle_handle(manager):
    original = manager._remcard_conn
    class FakeBusy(BrokenRollback):
        def execute(self, sql, *args):
            if sql == "BEGIN IMMEDIATE":
                raise sqlite3.OperationalError("database is locked")
            return self.connection.execute(sql, *args)
    proxy = FakeBusy(original)
    manager._remcard_conn = manager._journal_conn = proxy
    manager.write_controller.max_retries = 1
    with pytest.raises(sqlite3.OperationalError):
        manager.run_write_operation(lambda _: pytest.fail("Must not execute"))
    assert proxy.close_calls == 0 and not original.closed
    assert manager._quarantined_write_conn is None


def test_patient_service_uses_replacement_and_cas_after_failed_transaction(manager):
    manager._remcard_conn.executescript("""
        CREATE TABLE patients(id INTEGER PRIMARY KEY, full_name TEXT, admission_uid TEXT, birth_date TEXT);
        CREATE TABLE admissions(id INTEGER PRIMARY KEY, patient_id INTEGER, bed_number INTEGER,
            history_number TEXT, admission_datetime TEXT, patient_age INTEGER, patient_months INTEGER,
            patient_age_unit TEXT, patient_gender TEXT, diagnosis_code TEXT, diagnosis_text TEXT,
            department_profile TEXT, source_department TEXT, recovery_bed_stay INTEGER,
            created_at TEXT, updated_at TEXT);
        CREATE TABLE beds(bed_number INTEGER PRIMARY KEY, status TEXT, current_admission_id INTEGER, revision INTEGER);
    """)
    _break_write(manager)
    service = PatientBedManagementService(manager)
    admission = {"bed_number": 1, "admission_datetime": "2026-11-13 09:00:00"}
    admission_id = service.create_patient_and_admission({"full_name": "Fixture"}, admission)
    assert admission_id == 1
    with pytest.raises(RuntimeError, match="занята"):
        service.create_patient_and_admission({"full_name": "Other fixture"}, admission)
    assert manager._remcard_conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 1
    assert manager._remcard_conn.execute("SELECT COUNT(*) FROM admissions").fetchone()[0] == 1
    assert manager._remcard_conn.execute("SELECT current_admission_id FROM beds").fetchone()[0] == admission_id


@pytest.mark.parametrize("method", ["transaction", "execute"])
@pytest.mark.parametrize("failure", ["rollback", "begin"])
def test_terminal_connection_errors_never_reconnect_or_enter_durable_outbox(manager, monkeypatch, method, failure):
    monkeypatch.setattr(dbm, "DEFERRED_WRITE_FALLBACK_ENABLED", True)
    manager._enqueue_outbox_fallback = lambda *_a, **_kw: pytest.fail("No durable replay")
    manager._reconnect = lambda: pytest.fail("No reconnect/replay in failed call")
    holder = sqlite3.connect(manager.db_path, isolation_level=None)
    if failure == "rollback":
        proxy = BrokenRollback(manager._remcard_conn)
        manager._remcard_conn = manager._journal_conn = proxy
        expected = shared.SQLiteTransactionCleanupError
    else:
        manager._remcard_conn.execute("PRAGMA busy_timeout=1")
        manager.write_controller.max_retries = 1
        holder.execute("BEGIN IMMEDIATE")
        expected = shared.SQLiteBeginBusyError
    try:
        with pytest.raises(expected):
            if method == "execute":
                manager.execute_remcard("INSERT INTO entries VALUES(9)")
            else:
                manager.run_write_operation(lambda cursor: cursor.execute("INSERT INTO entries VALUES(9)"))
    finally:
        holder.close()
    with closing(sqlite3.connect(manager.db_path)) as observer:
        assert observer.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 3


def test_unknown_cleanup_error_is_not_replayed_by_generic_queue(manager):
    proxy = BrokenRollback(manager._remcard_conn)
    manager._remcard_conn = manager._journal_conn = proxy
    calls, failures = [], []
    done = threading.Event()
    queue = shared.LocalWriteQueue()
    def operation():
        calls.append(1)
        manager.run_write_operation(lambda cursor: cursor.execute("INSERT INTO entries VALUES(9)"))
    def failed(exc):
        failures.append(exc)
        done.set()
    try:
        queue.submit(operation, "unknown_write", on_error=failed)
        assert done.wait(2)
    finally:
        assert queue.shutdown(2)
    assert calls == [1] and len(failures) == 1
    assert isinstance(failures[0], shared.SQLiteTransactionCleanupError)
    assert failures[0].__cause__ is proxy.original
    assert DataService._terminal_write_state(failures[0]) == "unknown"
    assert manager._quarantined_write_conn is proxy
    with closing(sqlite3.connect(manager.db_path)) as observer:
        assert observer.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 3


def test_healthy_repeated_operations_do_not_open_or_configure_connections(manager, monkeypatch):
    monkeypatch.setattr(dbm.sqlite3, "connect", lambda *_a, **_kw: pytest.fail("No new healthy connection"))
    monkeypatch.setattr(dbm, "configure_connection", lambda *_a, **_kw: pytest.fail("No healthy reconfiguration"))
    old = manager._remcard_conn
    for _ in range(3):
        manager.run_write_operation(lambda cursor: cursor.execute("UPDATE entries SET value=value+1"))
        assert manager._fetch_one_central("SELECT COUNT(*) FROM entries", use_write_connection=True)[0] == 3
    assert manager._remcard_conn is old and not old.closed


def test_owned_transaction_cursor_is_closed_with_gc_disabled_and_releases_native_read_lock(manager):
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        returned = manager.run_write_operation(lambda cursor: cursor.execute("SELECT * FROM entries").fetchone())
        assert returned[0] == 1
        cursor = manager._remcard_conn.retained_cursors[-1]
        assert cursor.closed
        with pytest.raises(sqlite3.ProgrammingError):
            cursor.fetchone()
        with closing(sqlite3.connect(manager.db_path, isolation_level=None, timeout=0)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("INSERT INTO entries VALUES(4)")
            writer.execute("COMMIT")
        with pytest.raises(ValueError) as caught:
            with manager.write_controller.transaction(manager._remcard_conn) as cursor:
                cursor.execute("SELECT * FROM entries").fetchone()
                raise ValueError("retain this traceback")
        assert caught.value.__traceback__ is not None
        assert cursor.closed and not manager._remcard_conn.in_transaction
    finally:
        if was_enabled:
            gc.enable()


def test_nested_owned_cursor_closes_without_touching_outer_cursor_or_transaction(manager):
    with manager.write_controller.transaction(manager._remcard_conn) as outer:
        with manager.write_controller.transaction(manager._remcard_conn) as inner:
            inner.execute("SELECT * FROM entries").fetchone()
        assert inner.closed and not outer.closed
        assert manager._remcard_conn.in_transaction
        outer.execute("INSERT INTO entries VALUES(4)")
    assert outer.closed and not manager._remcard_conn.in_transaction


def test_execute_success_retains_legacy_cursor_but_execute_failure_closes_it(manager):
    cursor = manager.write_controller.execute(manager._remcard_conn, "SELECT * FROM entries")
    try:
        assert not cursor.closed
        assert cursor.fetchone()[0] == 1
    finally:
        cursor.close()
    with pytest.raises(sqlite3.OperationalError):
        manager.write_controller.execute(manager._remcard_conn, "SELECT * FROM nonexistent")
    assert manager._remcard_conn.retained_cursors[-1].closed


@pytest.mark.parametrize("kind", ["one", "all"])
def test_owned_read_error_closes_handle_before_retained_exception_can_reach_gc(manager, monkeypatch, kind):
    connections, errors = [], []
    def opened():
        conn = sqlite3.connect(manager.db_path, isolation_level=None, factory=TrackingConnection)
        connections.append(conn)
        return conn
    monkeypatch.setattr(manager, "_open_readonly_central_connection", opened)
    for _ in range(3):
        try:
            getattr(manager, "_fetch_" + kind + "_central")("SELECT * FROM nonexistent")
        except sqlite3.OperationalError as exc:
            errors.append(exc)
    assert len(errors) == len(connections) == 3
    assert all(conn.closed and all(cursor.closed for cursor in conn.retained_cursors) for conn in connections)


def test_recovery_configuration_cannot_extend_absolute_deadline(manager, monkeypatch):
    _break_write(manager)
    opened, timeouts = [], []
    original_connect = dbm.sqlite3.connect
    original_configure = dbm.configure_connection
    clock = SimpleNamespace(now=time.perf_counter())
    deadline = clock.now + 0.05
    def connected(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        return original_connect(*args, **kwargs)
    def configured(conn, **kwargs):
        opened.append(conn)
        original_configure(conn, **kwargs)
        clock.now += 0.1
    monkeypatch.setattr(dbm.sqlite3, "connect", connected)
    monkeypatch.setattr(dbm, "configure_connection", configured)
    monkeypatch.setattr(dbm, "time", SimpleNamespace(perf_counter=lambda: clock.now))
    with manager.write_metadata_context({"write_deadline_monotonic": deadline}):
        with pytest.raises(shared.QueuedWriteDeadlineExceeded):
            manager.run_write_operation(lambda _: pytest.fail("Expired recovered write must not execute"))
    assert 0 < timeouts[0] <= 0.051
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")


def test_waiting_independent_write_rechecks_poison_after_acquiring_manager_mutex(manager, monkeypatch):
    waiting = threading.Event()
    original = manager._central_io_lock_scope
    owner_thread = threading.get_ident()
    results, errors = [], []
    @contextmanager
    def observed(*args, **kwargs):
        if threading.get_ident() != owner_thread:
            waiting.set()
        with original(*args, **kwargs):
            yield
    monkeypatch.setattr(manager, "_central_io_lock_scope", observed)
    def other_write():
        try:
            manager.run_write_operation(lambda cursor: cursor.execute("INSERT INTO entries VALUES(4)"))
            results.append(True)
        except BaseException as exc:
            errors.append(exc)
    with manager._central_io_lock:
        worker = threading.Thread(target=other_write)
        worker.start()
        assert waiting.wait(2)
        old = _break_write(manager)
    worker.join(2)
    assert not worker.is_alive()
    assert results == [True] and errors == []
    assert manager._remcard_conn is not old and manager._quarantined_write_conn is None
    assert manager._remcard_conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 4


def test_read_snapshot_cleanup_preserves_body_failure_in_exception_chain(manager):
    reader = sqlite3.connect(manager.db_path, isolation_level=None, factory=TrackingConnection)
    original = ValueError("read body failure")
    with manager.existing_central_read_scope(BrokenRollback(reader)):
        with pytest.raises(sqlite3.OperationalError) as caught:
            with manager.central_read_snapshot_scope():
                manager._fetch_all_central("SELECT * FROM entries")
                raise original
    assert caught.value.__context__ is original
    assert reader.closed and manager._scoped_central_read_connection() is None


def test_native_busy_retirement_preserves_existing_interactive_exception_contract(manager, monkeypatch):
    monkeypatch.setattr(shared, "_bounded_opblock_interactive_timeout_ms", lambda _value: 30)
    old = manager._remcard_conn
    with closing(sqlite3.connect(manager.db_path, isolation_level=None)) as holder:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(shared.OpBlockInteractiveWriteBusyTimeout) as caught:
            manager.run_write_operation(lambda _: pytest.fail("Must not execute"), write_options={
                "interactive": True, "role": "operblock", "source": "operblock_undo_last_action",
            })
        assert caught.value.phase == "begin_immediate_timeout"
        assert "database is locked" not in str(caught.value).lower()
        assert old.closed and manager._quarantined_write_conn is old
        holder.rollback()
    manager.run_write_operation(lambda cursor: cursor.execute("INSERT INTO entries VALUES(4)"))
    assert manager._remcard_conn is not old


def test_cursor_cleanup_failure_keeps_original_body_error_in_chain(manager):
    cursor = manager._remcard_conn.cursor()
    cleanup_error = RuntimeError("cursor close acknowledgement failed")
    body_error = ValueError("original read body failed")
    class CursorProxy:
        def close(self):
            cursor.close()
            raise cleanup_error
    connection = SimpleNamespace(cursor=lambda: CursorProxy())
    with pytest.raises(RuntimeError) as caught:
        with manager._central_read_cursor(connection, "cleanup_test"):
            raise body_error
    assert caught.value is cleanup_error
    assert caught.value.__context__ is body_error
    assert cursor.closed


def test_quarantined_writer_with_failed_close_does_not_block_independent_readonly_reads(manager, monkeypatch):
    proxy = _break_write(manager, fail_close=True)
    def cannot_close():
        proxy.close_calls += 1
        raise OSError("native close still unavailable")
    monkeypatch.setattr(proxy, "close", cannot_close)
    monkeypatch.setattr(dbm, "DEFERRED_WRITE_FALLBACK_ENABLED", True)
    manager._enqueue_outbox_fallback = lambda *_a, **_kw: pytest.fail("No outbox while cleanup is incomplete")
    manager._reconnect = lambda: pytest.fail("No replay while cleanup is incomplete")
    replacement_calls = []
    manager._replace_quarantined_write_connection = lambda _deadline: replacement_calls.append(1)
    body_calls = []
    try:
        assert proxy.connection.in_transaction  # Own RESERVED transaction remains quarantined.
        assert manager._fetch_one_central("SELECT COUNT(*) FROM entries")[0] == 3
        assert [row[0] for row in manager._fetch_all_central("SELECT value FROM entries")] == [1, 2, 3]
        assert proxy.close_calls == 1 and replacement_calls == []
        with pytest.raises(shared.SQLiteTransactionCleanupError):
            manager.run_write_operation(lambda _: body_calls.append(1))
        assert body_calls == [] and replacement_calls == []
        assert manager._remcard_conn is proxy and manager._quarantined_write_conn is proxy
    finally:
        monkeypatch.undo()
        assert manager.write_controller.close_quarantined_connection(proxy)


@pytest.fixture
def isolated_lifecycle_registry(monkeypatch):
    monkeypatch.setattr(lifecycle, "_connections", OrderedDict())
    monkeypatch.setattr(lifecycle, "_overflow", 0)


def _registered_writer(manager):
    conn = sqlite3.connect(manager.db_path, isolation_level=None, check_same_thread=False)
    lifecycle.register_connection(conn, manager.db_path, source="central_write", readonly=False, scope="manager", new=True)
    manager._remcard_conn = manager._journal_conn = conn
    return conn


def _registry_snapshot(manager):
    return lifecycle.candidates(lifecycle.canonical_resource(manager.db_path))


def test_repeated_manager_reconnect_and_restore_close_do_not_fill_registry(manager, isolated_lifecycle_registry, monkeypatch):
    lifecycle.register_connection(manager._remcard_conn, manager.db_path, source="central_write", new=True)
    manager._central_read_conns = {}
    manager._local_replica = None
    monkeypatch.setattr(manager, "_init_connections", lambda: _registered_writer(manager))
    for _ in range(lifecycle.MAX_CONNECTIONS + 4):
        old = manager._remcard_conn
        manager._reconnect()
        assert lifecycle.connection_token(old) is None
        assert manager._remcard_conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 3
        snapshot = _registry_snapshot(manager)
        assert snapshot["connection_registry_size"] == 1
        assert snapshot["connection_registry_overflow"] == 0
    manager._close_connections_for_restore()
    assert _registry_snapshot(manager)["connection_registry_size"] == 0


@pytest.mark.parametrize("method", ["all", "finished"])
def test_repeated_manager_cached_read_close_removes_registered_handles(manager, isolated_lifecycle_registry, method):
    manager._central_read_conns = {}
    owner = threading.Thread()
    for _ in range(lifecycle.MAX_CONNECTIONS + 4):
        conn = manager._open_readonly_central_connection()
        manager._central_read_conns[owner] = conn
        assert lifecycle.connection_token(conn)
        if method == "all":
            assert manager._close_central_read_connection()
        else:
            manager._close_finished_thread_read_connections_locked()
        assert lifecycle.connection_token(conn) is None
        assert manager._central_read_conns == {}
        snapshot = _registry_snapshot(manager)
        assert snapshot["connection_registry_size"] == 0
        assert snapshot["connection_registry_overflow"] == 0


def test_repeated_manager_shutdown_closes_registered_writer_without_registry_growth(manager, isolated_lifecycle_registry, monkeypatch):
    manager._central_read_conns = {}
    manager._close_state_lock = threading.Lock()
    for name in ("_stop_daily_backup_scheduler", "_stop_shutdown_thread", "_stop_outbox_replay",
                 "_stop_local_replica_sync", "_stop_network_write_worker"):
        monkeypatch.setattr(manager, name, lambda *_a, **_kw: True)
    monkeypatch.setattr(manager, "_cancel_periodic_backup", lambda: None)
    monkeypatch.setattr(manager, "_create_shutdown_backup", lambda: None)
    initial = manager._remcard_conn
    lifecycle.close_connection(initial)
    for _ in range(lifecycle.MAX_CONNECTIONS + 4):
        conn = _registered_writer(manager)
        manager._closed = manager._closing = False
        assert manager.close(timeout_sec=0.1)
        assert lifecycle.connection_token(conn) is None
        snapshot = _registry_snapshot(manager)
        assert snapshot["connection_registry_size"] == 0
        assert snapshot["connection_registry_overflow"] == 0


@pytest.mark.parametrize("method", ["reconnect", "restore", "cached"])
def test_manager_failed_close_keeps_registry_error_until_successful_cleanup(manager, isolated_lifecycle_registry, monkeypatch, method):
    conn = manager._remcard_conn
    original = RuntimeError("native close failed")
    class CloseOnceFails:
        fail = True
        def close(self):
            if self.fail:
                raise original
            conn.close()
    proxy = CloseOnceFails()
    lifecycle.register_connection(proxy, manager.db_path, source="central_close", new=True)
    manager._remcard_conn = manager._journal_conn = proxy
    manager._central_read_conns = {}
    manager._local_replica = None
    monkeypatch.setattr(manager, "_init_connections", lambda: _registered_writer(manager))
    try:
        if method == "reconnect":
            with pytest.raises(RuntimeError) as caught:
                manager._reconnect()
            assert caught.value is original
        elif method == "restore":
            manager._close_connections_for_restore()  # Preserve its existing best-effort return contract.
        else:
            manager._central_read_conns[threading.current_thread()] = proxy
            assert manager._close_central_read_connection() is False
        assert lifecycle.connection_token(proxy)
        row, = _registry_snapshot(manager)["local_connection_candidates"]
        assert row["cleanup_phase"] == "close" and row["cleanup_error"] == "RuntimeError"
        assert row.get("connection_state") != "closed"
    finally:
        proxy.fail = False
        lifecycle.close_connection(proxy)
        manager._central_read_conns.clear()
    assert lifecycle.connection_token(proxy) is None
    assert _registry_snapshot(manager)["connection_registry_size"] == 0
