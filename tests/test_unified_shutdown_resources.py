from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rem_card.app import bootstrap as bootstrap_module
from rem_card.data.dao import db_manager as dbm


class _ConnectionGuard:
    @contextmanager
    def connection_guard(self, _connection):
        yield


class _ControlledThread:
    def __init__(self, *, alive: bool = True):
        self.alive = alive
        self.join_calls = 0

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout=None) -> None:
        self.join_calls += 1


class _Replica:
    def __init__(self, thread, *, stop_result=True):
        self._thread = thread
        self._stop_evt = threading.Event()
        self._fast_sync_evt = threading.Event()
        self.stop_calls = 0
        self.stop_result = stop_result

    def set_failure_callback(self, _callback) -> None:
        pass

    def stop(self, *, timeout_sec=2.0) -> bool:
        self.stop_calls += 1
        self._stop_evt.set()
        self._fast_sync_evt.set()
        return self.stop_result and not self._thread.is_alive()


class _NetworkWorker:
    def __init__(self):
        self._mutex = threading.Lock()
        self.close_calls = 0

    def close(self, *, timeout_sec=0.5) -> bool:
        self.close_calls += 1
        return True


class _ReadConnection:
    def __init__(self):
        self.fail = True
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        if self.fail:
            raise sqlite3.ProgrammingError("created in another thread")


def _manager(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "record_metric", lambda *args, **kwargs: None)
    manager = dbm.DatabaseManager.__new__(dbm.DatabaseManager)
    manager.db_path = str(tmp_path / "medical.db")
    manager._remcard_conn = sqlite3.connect(manager.db_path, check_same_thread=False)
    manager._remcard_conn.execute("CREATE TABLE IF NOT EXISTS probe (value TEXT)")
    manager._journal_conn = manager._remcard_conn
    manager._central_read_conns = {}
    manager._central_io_lock = threading.RLock()
    manager._close_state_lock = threading.Lock()
    manager._closing = False
    manager._closed = False
    manager._startup_quickcheck_stop_evt = threading.Event()
    manager._startup_quickcheck_thread = None
    manager._integrity_stop_evt = threading.Event()
    manager._integrity_thread = None
    manager._outbox_stop_evt = threading.Event()
    manager._outbox_wakeup_evt = threading.Event()
    manager._outbox_thread = None
    manager._outbox = None
    manager._local_replica = None
    manager._network_write_worker = None
    manager.write_controller = _ConnectionGuard()
    manager._cancel_periodic_backup = lambda: None
    manager._create_shutdown_backup = lambda: None
    return manager


def test_alive_integrity_thread_is_retained_and_close_can_be_retried(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    thread = _ControlledThread()
    manager._integrity_thread = thread
    connection = manager._remcard_conn

    assert manager.close(timeout_sec=0.1) is False
    assert manager._integrity_thread is thread
    assert thread.join_calls == 1
    assert connection.execute("SELECT 1").fetchone() == (1,)

    thread.alive = False
    assert manager.close(timeout_sec=0.1) is True
    assert manager._integrity_thread is None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_closed_flag_does_not_hide_living_background_resource(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    manager._remcard_conn.close()
    manager._remcard_conn = None
    manager._journal_conn = None
    manager._closed = True
    thread = _ControlledThread()
    manager._startup_quickcheck_thread = thread

    assert manager.close(timeout_sec=0.1) is False
    assert manager._startup_quickcheck_thread is thread
    thread.alive = False
    assert manager.close(timeout_sec=0.1) is True


def test_replica_is_not_force_closed_while_its_thread_is_alive(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    thread = _ControlledThread()
    replica = _Replica(thread)
    manager._local_replica = replica

    assert manager.close(timeout_sec=0.1) is False
    assert manager._local_replica is replica
    assert replica._stop_evt.is_set() and replica._fast_sync_evt.is_set()
    assert replica.stop_calls == 1

    thread.alive = False
    assert manager.close(timeout_sec=0.1) is True
    assert replica.stop_calls == 2
    assert manager._local_replica is None


def test_busy_network_writer_is_retained_instead_of_terminated(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    worker = _NetworkWorker()
    worker._mutex.acquire()
    manager._network_write_worker = worker

    assert manager.close(timeout_sec=0.1) is False
    assert manager._network_write_worker is worker
    assert worker.close_calls == 0

    worker._mutex.release()
    assert manager.close(timeout_sec=0.1) is True
    assert worker.close_calls == 1
    assert manager._network_write_worker is None


def test_replica_process_stop_failure_retains_owner_after_thread_exits(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    replica = _Replica(_ControlledThread(alive=False), stop_result=False)
    manager._local_replica = replica

    assert manager.close(timeout_sec=0.1) is False
    assert replica.stop_calls == 1
    assert manager._local_replica is replica
    assert manager._remcard_conn.execute("SELECT 1").fetchone() == (1,)

    replica.stop_result = True
    assert manager.close(timeout_sec=0.1) is True
    assert replica.stop_calls == 2
    assert manager._local_replica is None


@pytest.mark.parametrize("operation,lifecycle", [
    ("rotate_database_manually", "rotate_database_now"),
    ("cancel_manual_rotation", "cancel_manual_rotation"),
])
def test_rotation_retains_unfinished_replica_and_only_restarts_after_successful_stop(
    tmp_path, monkeypatch, operation, lifecycle,
) -> None:
    manager = _manager(tmp_path, monkeypatch)
    manager.runtime_context = SimpleNamespace(mode="network")
    manager.active_rotation_role_locks = lambda _context=None: []
    manager.active_rotation_emergency_sessions = lambda: []
    manager._rotation_blocking_role_lock_paths = lambda: {}
    manager._rotation_blocking_emergency_roots = lambda: []
    manager._rotation_ignored_lock_nonces = lambda _context=None: {}
    manager._init_connections = Mock()
    manager._close_central_read_connection = Mock()
    manager._start_local_replica_sync = Mock()
    events = []
    manager.set_rotation_quiesce_hooks(
        lambda: events.append("paused") or {"ok": True},
        lambda _token: events.append("resumed"),
    )
    replica = _Replica(_ControlledThread(alive=False), stop_result=False)
    manager._local_replica = replica
    connection = manager._remcard_conn
    rotation = Mock(return_value={"status": "db_lock_busy"})
    monkeypatch.setattr(dbm, lifecycle, rotation)

    result = getattr(manager, operation)()

    assert result == {"status": "rotation_quiesce_failed", "reason": "local_replica_stop_failed"}
    assert manager._local_replica is replica
    assert connection.execute("SELECT 1").fetchone() == (1,)
    rotation.assert_not_called()
    manager._start_local_replica_sync.assert_not_called()
    manager._close_central_read_connection.assert_not_called()
    assert events == ["paused", "resumed"]

    replica.stop_result = True
    assert getattr(manager, operation)() == {"status": "db_lock_busy"}
    rotation.assert_called_once()
    manager._start_local_replica_sync.assert_called_once()
    assert replica.stop_calls == 2
    assert manager._local_replica is None
    assert events == ["paused", "resumed", "paused", "resumed"]


def test_start_never_replaces_retained_replica_owner(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    replica = object()
    manager._local_replica = replica
    constructor = Mock(side_effect=AssertionError("must retain existing owner"))
    monkeypatch.setattr(dbm, "LocalReplicaSync", constructor)
    manager._start_local_replica_sync()
    assert manager._local_replica is replica
    constructor.assert_not_called()
    manager._remcard_conn.close()


def test_outbox_and_failed_read_connection_remain_visible_until_closed(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    outbox_thread = _ControlledThread()
    manager._outbox_thread = outbox_thread
    manager._outbox = object()
    read_connection = _ReadConnection()
    owner = threading.Thread(name="former-reader")
    manager._central_read_conns[owner] = read_connection

    assert manager.close(timeout_sec=0.1) is False
    assert manager._outbox_thread is outbox_thread
    assert manager._outbox is not None
    assert read_connection.close_calls == 0

    outbox_thread.alive = False
    assert manager.close(timeout_sec=0.1) is False
    assert manager._outbox_thread is None
    assert manager._outbox is None
    assert manager._central_read_conns[owner] is read_connection

    read_connection.fail = False
    assert manager.close(timeout_sec=0.1) is True
    assert manager._central_read_conns == {}


class _BootstrapManager:
    def __init__(self, *_args, close_result=False, **_kwargs):
        self.close_result = close_result
        self.close_calls = 0
        self.runtime_context = SimpleNamespace(mode="network", source_label="test")

    def close(self) -> bool:
        self.close_calls += 1
        return self.close_result


class _SettingsFailure:
    def ensure_ready(self):
        raise RuntimeError("settings failed")


def _runtime_context(tmp_path):
    return SimpleNamespace(
        mode="network",
        medical_db_path=str(tmp_path / "medical.db"),
        settings_db_path=str(tmp_path / "settings.db"),
        settings_readonly=False,
        baza_dir=str(tmp_path),
    )


def test_bootstrap_exception_retains_manager_when_cleanup_is_incomplete(tmp_path, monkeypatch) -> None:
    manager = _BootstrapManager(close_result=False)
    monkeypatch.setattr(dbm, "DatabaseManager", lambda *args, **kwargs: manager)
    from rem_card.services.settings import settings_service

    monkeypatch.setattr(settings_service, "configure_settings_service", lambda **kwargs: _SettingsFailure())

    with pytest.raises(RuntimeError, match="settings failed") as caught:
        bootstrap_module._bootstrap_impl(role="doctor", runtime_context=_runtime_context(tmp_path))

    assert caught.value.cleanup_failed is True
    assert caught.value.runtime_container.db_manager is manager
    assert caught.value.cleanup_result == {"ok": False, "data_service": True, "db_manager": False}
    assert manager.close_calls == 1


def test_bootstrap_exception_reports_completed_cleanup(tmp_path, monkeypatch) -> None:
    manager = _BootstrapManager(close_result=True)
    monkeypatch.setattr(dbm, "DatabaseManager", lambda *args, **kwargs: manager)
    from rem_card.services.settings import settings_service

    monkeypatch.setattr(settings_service, "configure_settings_service", lambda **kwargs: _SettingsFailure())

    with pytest.raises(RuntimeError, match="settings failed") as caught:
        bootstrap_module._bootstrap_impl(role="doctor", runtime_context=_runtime_context(tmp_path))

    assert caught.value.cleanup_failed is False
    assert caught.value.runtime_container is None
    assert caught.value.cleanup_result == {"ok": True, "data_service": True, "db_manager": True}


def test_partial_container_failure_retains_data_service_and_defers_db_close(tmp_path, monkeypatch) -> None:
    manager = _BootstrapManager(close_result=True)
    monkeypatch.setattr(dbm, "DatabaseManager", lambda *args, **kwargs: manager)
    from rem_card.services.settings import settings_service

    ready_settings = SimpleNamespace(ensure_ready=lambda: {"settings_db_path": str(tmp_path / "settings.db")})
    monkeypatch.setattr(settings_service, "configure_settings_service", lambda **kwargs: ready_settings)
    data_service = SimpleNamespace(shutdown=lambda: False)

    def fail_container(owner, *_args, **_kwargs):
        owner.data_service = data_service
        raise RuntimeError("container failed")

    monkeypatch.setattr(bootstrap_module.Container, "__init__", fail_container)
    with pytest.raises(RuntimeError, match="container failed") as caught:
        bootstrap_module._bootstrap_impl(role="doctor", runtime_context=_runtime_context(tmp_path))

    assert caught.value.cleanup_failed is True
    assert caught.value.runtime_container.data_service is data_service
    assert caught.value.cleanup_result == {"ok": False, "data_service": False, "db_manager": False}
    assert manager.close_calls == 0
