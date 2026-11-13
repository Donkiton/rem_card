from __future__ import annotations

import threading
import time
import sqlite3
from collections import OrderedDict, deque
from contextlib import contextmanager
from pathlib import Path
import pytest

from rem_card.app import sqlite_connection_diagnostics as registry
from rem_card.data.dao.db_manager import DatabaseManager
from rem_card.services.data_update_monitor import DataUpdateMonitor


class _Connection:
    def __init__(self):
        self.open_thread_id = threading.get_ident()
        self.close_thread_id = None
        self.closed = False

    def close(self):
        self.close_thread_id = threading.get_ident()
        self.closed = True


class _Db:
    def __init__(self):
        self.open_count = 0
        self.connections: list[_Connection] = []
        self.scope_connections: list[_Connection] = []

    def open_persistent_readonly_connection(self, *, source: str, cancel_check=None, defer_to_writer=False):
        assert source == "data_update_monitor"
        assert defer_to_writer is True
        self.open_count += 1
        conn = _Connection()
        self.connections.append(conn)
        return conn

    @contextmanager
    def existing_central_read_scope(self, conn, *, force_central: bool, cancel_check=None, defer_to_writer=None):
        assert force_central is True
        assert defer_to_writer is True
        self.scope_connections.append(conn)
        yield self


class _DataService:
    def __init__(self, db):
        self.db = db
        self._shutting_down = False
        self._runtime_role = "doctor"
        self.maintenance_calls = 0
        self.latest_change_id = 0
        self.change_rows = []

    def run_poll_maintenance_tasks(self):
        self.maintenance_calls += 1

    def get_latest_change_id(self):
        return self.latest_change_id

    def fetch_changes_since(self, _last_change_id):
        return list(self.change_rows)


def test_monitor_reuses_one_connection_across_poll_scopes():
    db = _Db()
    monitor = DataUpdateMonitor(_DataService(db), enabled=True)

    with monitor._persistent_read_scope():
        monitor._poll_once(force_emit=False, force_sources=[], run_maintenance=False)
    with monitor._persistent_read_scope():
        monitor._poll_once(force_emit=False, force_sources=[], run_maintenance=False)

    assert db.open_count == 1
    assert db.scope_connections == [db.connections[0], db.connections[0]]
    monitor._close_persistent_read_connection()
    assert db.connections[0].closed is True


def test_monitor_forces_full_refresh_for_partial_change_log_gap():
    db = _Db()
    service = _DataService(db)
    service.latest_change_id = 20
    service.change_rows = [
        {
            "id": 15,
            "entity_name": "vitals",
            "entity_id": 1,
            "admission_id": 7,
            "action": "update",
            "changed_at": "2026-08-28 00:00:00",
            "changed_by": "doctor",
            "version": None,
        }
    ]
    monitor = DataUpdateMonitor(service, enabled=True)
    monitor._last_seen_id = 10
    monitor._last_seen_settings_id = 0
    emitted = []
    monitor._emit_payload = lambda **payload: emitted.append(payload)

    monitor._poll_once(
        force_emit=False,
        force_sources=[],
        run_maintenance=False,
    )

    assert monitor._last_seen_id == 20
    assert monitor._state_epoch == 1
    assert len(emitted) == 1
    assert emitted[0]["gap_detected"] is True
    assert emitted[0]["forced"] is True
    assert emitted[0]["reason"] == "partial_change_rows"
    assert emitted[0]["changes"] == []


def test_monitor_reopens_after_connection_is_discarded():
    db = _Db()
    monitor = DataUpdateMonitor(_DataService(db), enabled=True)

    with monitor._persistent_read_scope():
        pass
    first = db.connections[0]
    monitor._close_persistent_read_connection()
    with monitor._persistent_read_scope():
        pass

    assert first.closed is True
    assert db.open_count == 2
    assert db.connections[1] is not first
    monitor._close_persistent_read_connection()


def test_repeated_actual_busy_reader_reopens_do_not_exhaust_connection_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "_connections", OrderedDict())
    monkeypatch.setattr(registry, "_recent_cleanup", deque(maxlen=registry.MAX_RECENT_CLEANUP))
    monkeypatch.setattr(registry, "_overflow", 0)
    path = str(tmp_path / "synthetic.db")
    seed = sqlite3.connect(path)
    try:
        seed.execute("CREATE TABLE sample(value INTEGER)")
        seed.execute("INSERT INTO sample VALUES (7)")
        seed.commit()
    finally:
        seed.close()

    class ActualDb:
        db_path = path
        _closed = False
        _remcard_conn = object()
        _thread_state = threading.local()
        _readonly_db_uri = DatabaseManager._readonly_db_uri
        _open_readonly_central_connection = DatabaseManager._open_readonly_central_connection
        open_persistent_readonly_connection = DatabaseManager.open_persistent_readonly_connection
        existing_central_read_scope = DatabaseManager.existing_central_read_scope

    monitor = DataUpdateMonitor(_DataService(ActualDb()), enabled=True)
    retained_closed_handles, failures, owner_threads = [], [], []
    cycles = registry.MAX_CONNECTIONS + 17

    def reopen_on_busy():
        blocker = sqlite3.connect(path, isolation_level=None, timeout=0)
        try:
            for _ in range(cycles):
                try:
                    with monitor._persistent_read_scope():
                        connection = monitor._persistent_read_conn
                        retained_closed_handles.append(connection)  # Prevent id reuse from hiding a leak.
                        assert registry.connection_token(connection)
                        connection.execute("PRAGMA busy_timeout = 0")
                        blocker.execute("BEGIN EXCLUSIVE")
                        with pytest.raises(sqlite3.OperationalError) as caught:
                            connection.execute("SELECT value FROM sample").fetchone()
                        assert caught.value.sqlite_errorcode & 255 == sqlite3.SQLITE_BUSY
                finally:
                    # Same owning thread and same close path as run() after a failed poll.
                    monitor._close_persistent_read_connection()
                    owner_threads.append(threading.get_ident())
                    blocker.rollback()
                assert registry.connection_token(connection) is None
                with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                    connection.execute("SELECT 1")
                observed = registry.candidates(registry.canonical_resource(path))
                assert observed["connection_registry_size"] == observed["connection_registry_overflow"] == 0
                assert observed["local_connection_candidates"] == []
                assert observed["native_owner_identified"] is False
        except BaseException as exc:
            failures.append(exc)
        finally:
            monitor._close_persistent_read_connection()
            blocker.close()

    thread = threading.Thread(target=reopen_on_busy)
    thread.start()
    thread.join(15)
    assert not thread.is_alive() and not failures
    assert len(retained_closed_handles) == cycles
    assert len(set(owner_threads)) == 1 and owner_threads[0] != threading.get_ident()
    assert monitor._persistent_read_conn is None


def test_paused_monitor_does_not_open_connection():
    db = _Db()
    monitor = DataUpdateMonitor(_DataService(db), enabled=False)
    monitor.start()
    time.sleep(0.03)
    monitor.stop()
    assert monitor.wait(2000)
    assert db.open_count == 0


def test_admin_monitor_does_not_hold_connection_needed_for_manual_rotation():
    db = _Db()
    service = _DataService(db)
    service._runtime_role = "admin"
    monitor = DataUpdateMonitor(service, enabled=True)

    with monitor._persistent_read_scope():
        monitor._poll_once(force_emit=False, force_sources=[], run_maintenance=False)

    assert db.open_count == 0


def test_runtime_role_change_to_admin_closes_existing_monitor_connection():
    db = _Db()
    service = _DataService(db)
    monitor = DataUpdateMonitor(service, enabled=True)

    with monitor._persistent_read_scope():
        pass
    connection = db.connections[0]
    service._runtime_role = "admin"
    with monitor._persistent_read_scope():
        pass

    assert connection.closed is True
    assert db.open_count == 1


def test_database_rotation_is_blocked_by_doctor_session_lock(tmp_path):
    manager = object.__new__(DatabaseManager)
    manager.baza_dir = str(tmp_path)

    blockers = manager._rotation_blocking_role_lock_paths()

    assert set(blockers) == {
        "doctor",
        "nurse",
        "nurse_emergency",
        "operblock",
        "operblock_emergency",
        "operblock_planned",
    }
    assert Path(blockers["doctor"]) == tmp_path / "session_locks" / "doctor.lock"
    assert Path(blockers["operblock_planned"]) == tmp_path / "session_locks" / "operblock_planned.lock"


def test_monitor_opens_and_closes_connection_in_its_own_thread():
    db = _Db()
    service = _DataService(db)
    monitor = DataUpdateMonitor(service, poll_interval_sec=0.5, enabled=True)
    monitor.start()
    deadline = time.monotonic() + 2.0
    while db.open_count == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    monitor.stop()
    assert monitor.wait(2000)

    assert db.open_count == 1
    conn = db.connections[0]
    assert conn.closed is True
    assert conn.open_thread_id == conn.close_thread_id
    assert conn.open_thread_id != threading.get_ident()


def test_rotation_pause_waits_until_monitor_connection_is_closed():
    db = _Db()
    service = _DataService(db)
    monitor = DataUpdateMonitor(service, poll_interval_sec=0.5, enabled=True)
    monitor.start()
    deadline = time.monotonic() + 2.0
    while db.open_count == 0 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert monitor.pause_and_wait(2.0) is True
    first = db.connections[0]
    assert first.closed is True
    assert first.open_thread_id == first.close_thread_id

    monitor.set_enabled(True)
    deadline = time.monotonic() + 2.0
    while db.open_count < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    monitor.stop()
    assert monitor.wait(2000)

    assert db.open_count == 2
