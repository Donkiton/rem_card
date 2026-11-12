from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from rem_card.app import sqlite_shared
from rem_card.app.db_availability import DatabaseClosedError
from rem_card.data.dao.db_manager import DatabaseManager
from rem_card.services.data_update_monitor import DataUpdateMonitor
from rem_card.services.remcard_facade import OrdersSnapshotCancelled, orders_snapshot_caller, read_scoped_snapshot


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "priority.db"
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        connection.executescript(
            "CREATE TABLE entries(value INTEGER); INSERT INTO entries VALUES (1);"
            "CREATE TABLE change_log(id INTEGER PRIMARY KEY); INSERT INTO change_log VALUES(1);"
        )
    controller = sqlite_shared.SQLiteWriteController(str(path), str(tmp_path / "db.lock"), "fixture")
    manager = object.__new__(DatabaseManager)
    manager.db_path = str(path)
    manager._thread_state = threading.local()
    manager._closed = False
    manager._remcard_conn = object()
    manager.write_controller = controller
    manager._in_current_thread_remcard_transaction = lambda: False
    manager._should_read_from_local = lambda: False
    manager.runtime_context = SimpleNamespace(mode="")
    return manager, path, controller


def test_delayed_readonly_cache_does_not_hold_native_setup_mutex():
    entered, release, other_finished = threading.Event(), threading.Event(), threading.Event()
    errors = []

    class DelayedConnection(sqlite3.Connection):
        def execute(self, sql, *parameters):
            if sql.startswith("PRAGMA cache_size"):
                entered.set()
                assert release.wait(3)
            return super().execute(sql, *parameters)

    def configure(factory, done):
        try:
            with closing(sqlite3.connect(":memory:", factory=factory)) as connection:
                sqlite_shared.configure_connection(connection, readonly=True)
                assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
                assert connection.execute("PRAGMA cache_size").fetchone()[0] == -8192
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    slow = threading.Thread(target=configure, args=(DelayedConnection, threading.Event()))
    other = threading.Thread(target=configure, args=(sqlite3.Connection, other_finished))
    slow.start()
    try:
        assert entered.wait(3)
        other.start()
        assert other_finished.wait(2)
    finally:
        release.set()
        slow.join(3)
        other.join(3)
    assert not errors and not slow.is_alive() and not other.is_alive()


def test_parallel_real_sqlite_configuration_preserves_private_connection_settings():
    def configure(_index):
        for _ in range(8):
            with closing(sqlite3.connect(":memory:")) as connection:
                sqlite_shared.configure_connection(connection, readonly=True)
                assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
                assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
                assert connection.execute("SELECT CASEFOLD('AbC')").fetchone()[0] == "abc"
                assert connection.execute("PRAGMA mmap_size").fetchone() is None

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(configure, range(12)))


def test_new_read_preparation_waits_for_writer_and_observes_commit(database):
    manager, path, controller = database
    writer_entered, release_writer, read_started, read_finished = [threading.Event() for _ in range(4)]
    opened = []
    result, errors = [], []
    open_connection = manager._open_readonly_central_connection

    def open_observed():
        opened.append(threading.get_ident())
        return open_connection()

    manager._open_readonly_central_connection = open_observed

    def write():
        try:
            with closing(sqlite3.connect(path, isolation_level=None)) as connection:
                with controller.transaction(connection) as cursor:
                    cursor.execute("INSERT INTO entries VALUES(2)")
                    writer_entered.set()
                    assert release_writer.wait(3)
        except BaseException as exc:
            errors.append(exc)

    def read():
        read_started.set()
        try:
            with manager.central_read_cancel_scope(None, defer_to_writer=True):
                result.append(tuple(manager._fetch_one_central("SELECT COUNT(*) FROM entries")))
        except BaseException as exc:
            errors.append(exc)
        finally:
            read_finished.set()

    writer, reader = threading.Thread(target=write), threading.Thread(target=read)
    writer.start()
    try:
        assert writer_entered.wait(3)
        reader.start()
        assert read_started.wait(3)
        assert not read_finished.wait(0.05) and opened == []
    finally:
        release_writer.set()
        writer.join(3)
        reader.join(3)
    assert not errors and result == [(2,)]
    assert len(opened) == 1
    assert controller._read_priority.readers == controller._read_priority.writers == {}


def test_explicit_snapshot_nested_reads_can_finish_while_writer_is_pending(database):
    manager, path, controller = database
    snapshot_ready, writer_ready, second_read = [threading.Event() for _ in range(3)]
    errors, values = [], []

    def read_snapshot():
        try:
            with manager.central_read_cancel_scope(None, defer_to_writer=True), manager.central_read_snapshot_scope():
                values.append(tuple(manager._fetch_one_central("SELECT COUNT(*) FROM entries")))
                snapshot_ready.set()
                assert writer_ready.wait(3)
                values.append(tuple(manager._fetch_one_central("SELECT COUNT(*) FROM entries")))
            second_read.set()
        except BaseException as exc:
            errors.append(exc)
            second_read.set()

    def write():
        try:
            assert snapshot_ready.wait(3)
            with closing(sqlite3.connect(path, isolation_level=None)) as connection:
                with controller.transaction(connection) as cursor:
                    cursor.execute("INSERT INTO entries VALUES(2)")
                    writer_ready.set()
                    assert second_read.wait(3)
        except BaseException as exc:
            errors.append(exc)

    reader, writer = threading.Thread(target=read_snapshot), threading.Thread(target=write)
    reader.start()
    writer.start()
    reader.join(4)
    writer.join(4)
    assert not reader.is_alive() and not writer.is_alive() and not errors
    assert values == [(1,), (1,)]
    assert tuple(manager._fetch_one_central("SELECT COUNT(*) FROM entries")) == (2,)
    assert controller._read_priority.readers == controller._read_priority.writers == {}


def test_noncooperative_existing_reader_keeps_original_writer_budget(database):
    _manager, path, controller = database
    with closing(sqlite3.connect(path, isolation_level=None)) as reader, closing(
        sqlite3.connect(path, isolation_level=None)
    ) as writer:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM entries").fetchall()
        writer.execute("PRAGMA busy_timeout=10000")
        with pytest.raises(sqlite_shared.QueuedWriteDeadlineExceeded):
            with controller.transaction(writer, write_options={"write_budget_ms": 100}) as cursor:
                cursor.execute("INSERT INTO entries VALUES(2)")
        assert not writer.in_transaction
        assert writer.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
        reader.execute("ROLLBACK")
        assert writer.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 1
    assert controller._read_priority.writers == {}


def test_same_thread_read_to_write_and_writer_reads_do_not_wait_on_own_intent(database):
    manager, path, controller = database
    with controller.read_guard(), closing(sqlite3.connect(path, isolation_level=None)) as connection:
        with controller.transaction(connection) as cursor:
            cursor.execute("INSERT INTO entries VALUES(2)")
            assert tuple(manager._fetch_one_central("SELECT COUNT(*) FROM entries")) == (1,)
    assert tuple(manager._fetch_one_central("SELECT COUNT(*) FROM entries")) == (2,)


def test_idle_autocommit_scope_does_not_keep_read_permission(database):
    manager, _path, controller = database
    with manager.central_read_scope():
        assert controller._read_priority.readers == {}
        assert tuple(manager._fetch_one_central("SELECT COUNT(*) FROM entries")) == (1,)
        assert controller._read_priority.readers == {}


def test_adopted_existing_transaction_finishes_under_pending_writer(database):
    manager, path, controller = database
    snapshot_ready, writer_ready, read_finished = [threading.Event() for _ in range(3)]
    errors, values = [], []

    def read():
        try:
            with closing(sqlite3.connect(path, isolation_level=None)) as connection:
                connection.execute("BEGIN")
                connection.execute("SELECT * FROM entries").fetchall()
                snapshot_ready.set()
                assert writer_ready.wait(3)
                with manager.existing_central_read_scope(connection):
                    assert controller._read_priority.readers[threading.get_ident()] == 1
                    with manager.central_read_cancel_scope(None, defer_to_writer=True):
                        values.append(tuple(manager._fetch_one_central("SELECT COUNT(*) FROM entries")))
                assert connection.in_transaction  # The scope does not own the caller's transaction.
                connection.execute("ROLLBACK")
        except BaseException as exc:
            errors.append(exc)
        finally:
            read_finished.set()

    def write():
        try:
            assert snapshot_ready.wait(3)
            with closing(sqlite3.connect(path, isolation_level=None)) as connection:
                with controller.transaction(connection) as cursor:
                    cursor.execute("INSERT INTO entries VALUES(2)")
                    writer_ready.set()
                    assert read_finished.wait(3)
        except BaseException as exc:
            errors.append(exc)

    reader, writer = threading.Thread(target=read), threading.Thread(target=write)
    reader.start()
    writer.start()
    reader.join(4)
    writer.join(4)
    assert not reader.is_alive() and not writer.is_alive() and not errors
    assert values == [(1,)]
    assert tuple(manager._fetch_one_central("SELECT COUNT(*) FROM entries")) == (2,)
    assert controller._read_priority.readers == controller._read_priority.writers == {}


@pytest.mark.parametrize("caller,source,background", [
    ("read_coordinator", "monitor", True),
    ("read_coordinator", "refresh", True),
    ("read_coordinator", "click", False),
    ("read_coordinator", "user", False),
    ("read_coordinator", "post_finalize", False),
    ("read_coordinator", "unknown", False),
    ("legacy", "refresh", False),
    (None, None, False),
])
def test_only_known_background_orders_defer_reads_to_reserved_writer(database, caller, source, background):
    manager, path, controller = database
    writer_ready, release_writer, read_finished = [threading.Event() for _ in range(3)]
    errors, values = [], []
    facade = SimpleNamespace(orders_dao=SimpleNamespace(db=manager))

    @read_scoped_snapshot
    def build_orders_snapshot(_service):
        return tuple(manager._fetch_one_central("SELECT COUNT(*) FROM entries"))

    def write():
        try:
            with closing(sqlite3.connect(path, isolation_level=None)) as connection:
                with controller.transaction(connection) as cursor:
                    cursor.execute("INSERT INTO entries VALUES(2)")
                    writer_ready.set()
                    assert release_writer.wait(3)
        except BaseException as exc:
            errors.append(exc)

    def read():
        try:
            if caller is None:
                values.append(tuple(manager._fetch_one_central("SELECT COUNT(*) FROM entries")))
            else:
                with orders_snapshot_caller(caller, request_source=source, step_observer=lambda *_args: None):
                    values.append(build_orders_snapshot(facade))
        except BaseException as exc:
            errors.append(exc)
        finally:
            assert not hasattr(manager._thread_state, "central_read_defer_to_writer")
            read_finished.set()

    writer, reader = threading.Thread(target=write), threading.Thread(target=read)
    writer.start()
    try:
        assert writer_ready.wait(3)
        reader.start()
        if background:
            assert not read_finished.wait(0.05)
        else:
            assert read_finished.wait(2)
    finally:
        release_writer.set()
        writer.join(3)
        reader.join(3)
    assert not writer.is_alive() and not reader.is_alive() and not errors
    assert values == [(2 if background else 1,)]
    assert controller._read_priority.readers == controller._read_priority.writers == {}


def test_source_preparation_can_be_cancelled_before_any_connection_is_opened(database):
    manager, _path, controller = database
    facade = SimpleNamespace(orders_dao=SimpleNamespace(db=manager))
    waiting, cancel, finished = threading.Event(), threading.Event(), threading.Event()
    events, errors = [], []
    original = OrdersSnapshotCancelled("obsolete fixture snapshot")

    @read_scoped_snapshot
    def build_orders_snapshot(_service):
        pytest.fail("cancelled preparation reached the snapshot body")

    def observe(event, step, fields):
        events.append((event, step, fields))
        if event == "poll":
            waiting.set()
            if cancel.is_set():
                raise original

    def read():
        try:
            with orders_snapshot_caller("read_coordinator", step_observer=observe):
                build_orders_snapshot(facade)
        except BaseException as exc:
            errors.append(exc)
        finally:
            assert not hasattr(manager._thread_state, "central_read_cancel_check")
            assert not hasattr(manager._thread_state, "central_read_defer_to_writer")
            finished.set()

    thread = threading.Thread(target=read)
    with controller._read_priority.write():
        thread.start()
        assert waiting.wait(3)
        cancel.set()
        assert finished.wait(2)
    thread.join(3)
    assert errors == [original]
    assert events[0][:2] == ("start", "read_scoped_snapshot")
    assert events[-1][:2] == ("end", "read_scoped_snapshot")
    assert events[-1][2]["status"] == "error"
    assert controller._read_priority.readers == controller._read_priority.writers == {}


def test_read_wait_is_bounded_and_recovers_after_writer(database, monkeypatch):
    manager, _path, controller = database
    monkeypatch.setattr(sqlite_shared, "SQLITE_BUSY_TIMEOUT_MS", 30)
    errors = []

    def read():
        try:
            with manager.central_read_cancel_scope(None, defer_to_writer=True):
                manager._fetch_one_central("SELECT COUNT(*) FROM entries")
        except BaseException as exc:
            errors.append(exc)

    with controller._read_priority.write():
        reader = threading.Thread(target=read)
        reader.start()
        reader.join(2)
        assert not reader.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], sqlite3.OperationalError)
    assert tuple(manager._fetch_one_central("SELECT COUNT(*) FROM entries")) == (1,)


@pytest.mark.parametrize("existing_connection", [False, True])
def test_monitor_stop_cancels_writer_wait_and_closes_on_owner_thread(database, existing_connection):
    manager, _path, controller = database
    monitor = DataUpdateMonitor(SimpleNamespace(db=manager, _runtime_role="nurse", _shutting_down=False))
    ready, begin, stopped = [threading.Event() for _ in range(3)]
    errors = []

    def poll():
        try:
            if existing_connection:
                monitor._persistent_read_conn = manager.open_persistent_readonly_connection()
            ready.set()
            assert begin.wait(3)
            with monitor._persistent_read_scope():
                manager._fetch_one_central("SELECT COUNT(*) FROM entries")
        except BaseException as exc:
            errors.append(exc)
        finally:
            monitor._close_persistent_read_connection()
            stopped.set()

    thread = threading.Thread(target=poll)
    thread.start()
    assert ready.wait(3)
    with controller._read_priority.write():
        begin.set()
        monitor.stop()
        assert stopped.wait(2)
    thread.join(3)
    assert len(errors) == 1 and isinstance(errors[0], DatabaseClosedError)
    assert monitor._persistent_read_conn is None
    assert controller._read_priority.readers == controller._read_priority.writers == {}
