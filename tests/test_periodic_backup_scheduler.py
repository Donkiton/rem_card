from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from unittest.mock import Mock

import pytest

from rem_card.data.dao import db_manager as dbm


class ManualTimer:
    """Продвигает фоновые попытки без ожиданий и зависимости от часов CI."""

    def __init__(self, interval, function, args=()):
        self.function = function
        self.args = args
        self.cancelled = False

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.function(*self.args)


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "RUNTIME_AUTO_BACKUPS_ENABLED", True)
    monkeypatch.setattr(dbm.threading, "Timer", ManualTimer)
    monkeypatch.setattr(dbm, "record_metric", lambda *args, **kwargs: None)
    monkeypatch.setattr(dbm, "should_defer_background_io", lambda **kwargs: (False, "", None))
    obj = dbm.DatabaseManager.__new__(dbm.DatabaseManager)
    obj.db_path = str(tmp_path / "medical.db")
    obj.medical_db_lock_path = str(tmp_path / "db.lock")
    obj.medical_backups_valid_dir = str(tmp_path / "backups")
    obj.medical_invalid_backups_dir = str(tmp_path / "invalid")
    obj._remcard_conn = sqlite3.connect(obj.db_path, isolation_level=None, check_same_thread=False)
    obj._remcard_conn.execute("CREATE TABLE entries (value TEXT)")
    obj._journal_conn = obj._remcard_conn
    obj._central_io_lock = threading.RLock()
    obj._write_activity_lock = threading.Lock()
    obj._active_write_count = 0
    obj._last_write_activity_ts = 0.0
    obj._write_queue_idle_probe = lambda: True
    obj._thread_state = threading.local()
    obj._periodic_backup_lock = threading.Lock()
    obj._periodic_backup_timer = None
    obj._periodic_backup_interval_sec = 600.0
    obj._last_backup_ts = 0.0
    obj._closed = False
    obj._closing = False
    obj._close_state_lock = threading.Lock()
    obj._startup_quickcheck_stop_evt = threading.Event()
    obj._startup_quickcheck_thread = None
    obj._integrity_stop_evt = threading.Event()
    obj._integrity_thread = None
    obj._network_write_worker = None
    obj._stop_outbox_replay = Mock()
    obj._stop_local_replica_sync = Mock()
    obj._close_central_read_connection = Mock()
    obj._create_shutdown_backup = Mock()
    obj._after_write_committed = Mock()
    obj._rotate_backups = Mock()
    obj.write_controller = dbm.SQLiteWriteController(
        db_path=obj.db_path, lock_path=obj.medical_db_lock_path, owner_id="periodic-test", logger=dbm.logger,
    )
    yield obj
    obj.close()


@pytest.mark.parametrize("write_kind", ["transaction", "execute"])
def test_committed_write_is_backed_up_after_queue_becomes_idle(manager, tmp_path, write_kind):
    manager._write_queue_idle_probe = lambda: False
    if write_kind == "transaction":
        with manager.remcard_transaction() as cursor:
            cursor.execute("INSERT INTO entries VALUES ('saved')")
    else:
        manager.execute_remcard("INSERT INTO entries VALUES ('saved')").close()

    assert manager._active_write_count == 0
    timer = manager._periodic_backup_timer
    assert timer is not None
    assert not (tmp_path / "backups").exists()
    # Даже после окончания транзакции очередь ещё может обрабатывать callback.
    manager._last_write_activity_ts = 0.0
    timer.fire()
    retry = manager._periodic_backup_timer
    assert retry is not None and retry is not timer
    assert not (tmp_path / "backups").exists()

    manager._write_queue_idle_probe = lambda: True
    # Запускаем настоящую фоновую нить: новая запись не требуется,
    # а соединение SQLite должно безопасно использоваться из worker.
    worker = threading.Thread(target=retry.fire)
    worker.start()
    worker.join(timeout=5.0)
    assert not worker.is_alive()
    assert manager._periodic_backup_timer is None
    backups = list((tmp_path / "backups").glob("periodic_*.db"))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as backup:
        assert backup.execute("SELECT value FROM entries").fetchall() == [("saved",)]
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert manager._last_backup_ts > 0


def test_rollback_does_not_schedule_backup(manager):
    with pytest.raises(ValueError, match="rollback"):
        with manager.remcard_transaction() as cursor:
            cursor.execute("INSERT INTO entries VALUES ('discarded')")
            raise ValueError("rollback")
    assert manager._periodic_backup_timer is None
    assert manager._remcard_conn.execute("SELECT * FROM entries").fetchall() == []


@pytest.mark.parametrize("blocker", ["active_write", "recent_write", "foreground", "cooldown"])
def test_deferred_backup_resumes_without_another_write(manager, monkeypatch, blocker):
    create = Mock(return_value="backup.db")
    manager._create_named_backup = create
    manager._request_periodic_backup()
    if blocker == "active_write":
        manager._active_write_count = 1
    elif blocker == "recent_write":
        manager._last_write_activity_ts = dbm.time.time()
    elif blocker == "foreground":
        monkeypatch.setattr(dbm, "should_defer_background_io", lambda **kwargs: (True, "orders", 0.0))
    else:
        manager._maintenance_io_cooldown_remaining = lambda source: 60.0
    manager._periodic_backup_timer.fire()
    create.assert_not_called()
    assert manager._periodic_backup_timer is not None

    manager._active_write_count = 0
    manager._last_write_activity_ts = 0.0
    monkeypatch.setattr(dbm, "should_defer_background_io", lambda **kwargs: (False, "", None))
    manager._maintenance_io_cooldown_remaining = lambda source: 0.0
    manager._periodic_backup_timer.fire()
    create.assert_called_once()
    assert manager._periodic_backup_timer is None


def test_requests_coalesce_while_pending_and_running(manager):
    manager._request_periodic_backup("first")
    timer = manager._periodic_backup_timer
    for _ in range(10):
        manager._request_periodic_backup("next")
        assert manager._periodic_backup_timer is timer

    def create(**kwargs):
        manager._request_periodic_backup("during_backup")
        assert manager._periodic_backup_timer is timer
        return "backup.db"

    manager._create_named_backup = Mock(side_effect=create)
    timer.fire()
    manager._create_named_backup.assert_called_once_with(prefix="periodic", source="first")
    assert manager._periodic_backup_timer is None


def test_busy_central_connection_retries_without_waiting(manager):
    create = Mock(return_value="backup.db")
    manager._create_named_backup = create
    manager._request_periodic_backup()
    timer = manager._periodic_backup_timer
    with manager._central_io_lock:
        worker = threading.Thread(target=timer.fire)
        worker.start()
        worker.join(timeout=2.0)
        assert not worker.is_alive(), "backup waited behind a clinical operation"
    create.assert_not_called()
    assert manager._periodic_backup_timer is not timer
    manager._periodic_backup_timer.fire()
    create.assert_called_once()


@pytest.mark.parametrize("failure", [None, OSError("backup disk unavailable")])
def test_backup_failure_retries_without_affecting_saved_data(manager, failure):
    manager.execute_remcard("INSERT INTO entries VALUES ('saved')").close()
    manager._last_write_activity_ts = 0.0
    manager._create_named_backup = Mock(side_effect=[failure, "backup.db"])
    manager._periodic_backup_timer.fire()
    assert manager._periodic_backup_timer is not None
    assert manager._remcard_conn.execute("SELECT value FROM entries").fetchall() == [("saved",)]
    manager._periodic_backup_timer.fire()
    assert manager._periodic_backup_timer is None
    assert manager._create_named_backup.call_count == 2


def test_close_cancels_pending_backup_and_late_callback(manager):
    create = Mock(return_value="backup.db")
    manager._create_named_backup = create
    manager._request_periodic_backup()
    timer = manager._periodic_backup_timer
    assert manager.close()
    assert timer.cancelled
    timer.fire()  # Callback мог уже проснуться в момент cancel().
    manager._request_periodic_backup()
    create.assert_not_called()
    assert manager._periodic_backup_timer is None


def test_recent_backup_or_disabled_option_prevents_duplicate_backup(manager, monkeypatch):
    create = Mock(return_value="backup.db")
    manager._create_named_backup = create
    monkeypatch.setattr(dbm, "RUNTIME_AUTO_BACKUPS_ENABLED", False)
    manager._request_periodic_backup()
    assert manager._periodic_backup_timer is None
    monkeypatch.setattr(dbm, "RUNTIME_AUTO_BACKUPS_ENABLED", True)
    manager._request_periodic_backup()
    manager._last_backup_ts = dbm.time.time()  # Пока ждали, создали ручной бэкап.
    manager._periodic_backup_timer.fire()
    create.assert_not_called()
    manager._request_periodic_backup()
    assert manager._periodic_backup_timer is None
