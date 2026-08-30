import logging
import sqlite3
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from rem_card.app import schema_migration_guard as guard


class _Clock:
    def __init__(self):
        self.elapsed = 0.0

    def monotonic(self):
        return self.elapsed

    def sleep(self, delay):
        assert delay > 0
        self.elapsed += delay


class _Lock:
    def __init__(self, clock, available_at):
        self.clock = clock
        self.available_at = available_at
        self.attempts = []
        self.releases = 0

    def acquire(self, owner_id, source):
        self.attempts.append((owner_id, source))
        return self.clock.elapsed >= self.available_at

    def release(self):
        self.releases += 1


def _acquire(monkeypatch, clock, lock, mode):
    monkeypatch.setattr(guard, "time", clock)
    if mode == "controller":
        controller = SimpleNamespace(lock=lock, owner_id="client", max_retries=20, retry_delay_sec=0.2)
        guard._acquire_controller_file_lock(controller, "migration_test")
    else:
        monkeypatch.setattr(guard, "FileWriteLock", lambda *args, **kwargs: lock)
        assert guard._acquire_file_lock("db.lock", "client", "migration_test", logging.getLogger(__name__)) is lock


@pytest.mark.parametrize("mode", ["controller", "file"])
def test_migration_waits_beyond_normal_write_retry_budget(monkeypatch, mode):
    clock = _Clock()
    lock = _Lock(clock, available_at=6.0)

    _acquire(monkeypatch, clock, lock, mode)

    assert 6.0 <= clock.elapsed < 6.3
    assert len(lock.attempts) > 20
    assert set(lock.attempts) == {("client", "migration_test")}
    assert lock.releases == 0


@pytest.mark.parametrize("mode", ["controller", "file"])
def test_migration_lock_wait_is_bounded_and_does_not_release_another_owner(monkeypatch, mode):
    clock = _Clock()
    lock = _Lock(clock, available_at=float("inf"))

    with pytest.raises(sqlite3.OperationalError, match="Could not acquire db lock for schema migration"):
        _acquire(monkeypatch, clock, lock, mode)

    assert clock.elapsed == pytest.approx(60.0)
    assert lock.releases == 0


def test_migration_timeout_does_not_start_backup_policy_update_or_ddl(monkeypatch):
    clock = _Clock()
    lock = _Lock(clock, available_at=float("inf"))
    monkeypatch.setattr(guard, "time", clock)
    started = []
    monkeypatch.setattr(guard, "_ensure_after_lock", lambda *args, **kwargs: started.append(True))
    controller = SimpleNamespace(
        lock=lock, owner_id="client", max_retries=20, retry_delay_sec=0.2,
        connection_guard=lambda _conn: nullcontext(),
    )

    with pytest.raises(sqlite3.OperationalError, match="Could not acquire db lock"):
        guard.ensure_unified_schema_with_migration_backup(
            None, db_path="unused.db", backup_dir="unused", controller=controller,
        )

    assert started == []
    assert lock.releases == 0


def test_migration_retry_sleep_is_capped_by_remaining_deadline(monkeypatch):
    clock = _Clock()
    lock = _Lock(clock, available_at=float("inf"))
    monkeypatch.setattr(guard, "time", clock)
    controller = SimpleNamespace(lock=lock, owner_id="client", retry_delay_sec=7.0)

    with pytest.raises(sqlite3.OperationalError, match="Could not acquire db lock"):
        guard._acquire_controller_file_lock(controller, "migration_test")

    assert clock.elapsed == pytest.approx(60.0)
