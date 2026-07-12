from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from pathlib import Path

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

    def open_persistent_readonly_connection(self, *, source: str):
        assert source == "data_update_monitor"
        self.open_count += 1
        conn = _Connection()
        self.connections.append(conn)
        return conn

    @contextmanager
    def existing_central_read_scope(self, conn, *, force_central: bool):
        assert force_central is True
        self.scope_connections.append(conn)
        yield self


class _DataService:
    def __init__(self, db):
        self.db = db
        self._shutting_down = False
        self._runtime_role = "doctor"
        self.maintenance_calls = 0

    def run_poll_maintenance_tasks(self):
        self.maintenance_calls += 1

    def get_latest_change_id(self):
        return 0


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

    assert set(blockers) == {"doctor", "nurse", "nurse_emergency"}
    assert Path(blockers["doctor"]) == tmp_path / "session_locks" / "doctor.lock"


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
