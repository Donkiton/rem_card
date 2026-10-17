from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import shutil
import socket
import sqlite3
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from rem_card.app import daily_backup_scheduler as daily
from rem_card.app.sqlite_shared import FileWriteLock, validate_sqlite_file
from rem_card.data.dao import db_manager as dbm


def _make_database(path: Path, value: str = "saved") -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE records (value TEXT NOT NULL)")
        connection.execute("INSERT INTO records VALUES (?)", (value,))


def _config(tmp_path: Path, *, backup_date: str, job_token: str | None = None) -> dict:
    return {
        "database_path": str(tmp_path / "medical.db"),
        "backup_dir": str(tmp_path / "backups" / "valid"),
        "health_dir": str(tmp_path / "backup_health"),
        "invalid_dir": str(tmp_path / "backup_health" / "invalid"),
        "database_lock_path": str(tmp_path / "db.lock"),
        "backup_date": backup_date,
        "role": "doctor",
        "job_token": job_token or uuid.uuid4().hex,
        "retention_days": 35,
        "max_count": 35,
        "database_lock_wait_sec": 5.0,
    }


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _CaptureConnection:
    def __init__(self) -> None:
        self.value = None

    def send(self, value) -> None:
        self.value = value

    def close(self) -> None:
        pass


def test_real_sqlite_backup_is_valid_and_can_be_restored(tmp_path: Path) -> None:
    source = tmp_path / "medical.db"
    _make_database(source, "clinical row")

    result = daily._perform_daily_backup(_config(tmp_path, backup_date="2026-10-17"))

    assert result["status"] == "success" and result["reason"] == "created"
    backup = Path(result["backup_path"])
    assert validate_sqlite_file(str(backup)) == (True, "ok")
    restored = tmp_path / "restored.db"
    shutil.copy2(backup, restored)
    assert validate_sqlite_file(str(restored)) == (True, "ok")
    with sqlite3.connect(restored) as connection:
        assert connection.execute("SELECT value FROM records").fetchall() == [("clinical row",)]


def test_database_lock_is_released_before_validation_hash_and_pruning(tmp_path: Path, monkeypatch) -> None:
    from rem_card.app import sqlite_shared

    _make_database(tmp_path / "medical.db")
    db_lock = tmp_path / "db.lock"
    original_validate = sqlite_shared.validate_sqlite_file
    original_sha = daily._sha256

    def checked_validate(path: str):
        assert not db_lock.exists(), "central DB lock leaked into backup validation"
        return original_validate(path)

    def checked_sha(path: str):
        assert not db_lock.exists(), "central DB lock leaked into backup hashing"
        return original_sha(path)

    monkeypatch.setattr(sqlite_shared, "validate_sqlite_file", checked_validate)
    monkeypatch.setattr(daily, "_sha256", checked_sha)
    result = daily._perform_daily_backup(_config(tmp_path, backup_date="2026-10-17"))
    assert result["status"] == "success"


def test_live_database_writer_defers_backup_without_deleting_its_lock(tmp_path: Path) -> None:
    _make_database(tmp_path / "medical.db")
    path = str(tmp_path / "db.lock")
    writer = FileWriteLock(path)
    assert writer.acquire("clinical", "clinical_write")
    token = Path(path).read_bytes()
    config = _config(tmp_path, backup_date="2026-10-17")
    config["database_lock_wait_sec"] = 0.1
    capture = _CaptureConnection()
    try:
        daily._daily_backup_worker(config, capture)
        assert capture.value == {"status": "deferred", "reason": "central_database_lock_busy"}
        assert Path(path).read_bytes() == token
    finally:
        writer.release()


def test_two_spawned_clients_reserve_once_and_deduplicate_by_validated_metadata(tmp_path: Path) -> None:
    _make_database(tmp_path / "medical.db")
    context = multiprocessing.get_context("spawn")
    processes = []
    receivers = []
    for _index in range(2):
        receiving, sending = context.Pipe(duplex=False)
        process = context.Process(
            target=daily._daily_backup_worker,
            args=(_config(tmp_path, backup_date="2026-10-17"), sending),
        )
        process.start()
        sending.close()
        processes.append(process)
        receivers.append(receiving)

    results = []
    for process, receiving in zip(processes, receivers):
        assert receiving.poll(15.0)
        results.append(receiving.recv())
        receiving.close()
        process.join(timeout=5.0)
        assert process.exitcode == 0
        process.close()

    final = daily._perform_daily_backup(_config(tmp_path, backup_date="2026-10-17"))
    assert final["status"] == "success" and final["reason"] == "already_done"
    assert any(item["status"] == "success" for item in results)
    backups = list((tmp_path / "backups" / "valid").glob("daily_*.db"))
    assert len(backups) == 1
    valid, reason, metadata = daily._validated_daily_backup(
        str(backups[0]), backup_date="2026-10-17", source_path=str(tmp_path / "medical.db")
    )
    assert valid, reason
    assert metadata["backup_kind"] == daily.DAILY_BACKUP_KIND


@pytest.mark.parametrize("reason", ["write_queue_not_idle", "foreground_activity:patient_card"])
def test_busy_foreground_or_writer_defers_without_starting_worker(tmp_path: Path, monkeypatch, reason: str) -> None:
    _make_database(tmp_path / "medical.db")
    scheduler = daily.DailyBackupScheduler(
        database_path=str(tmp_path / "medical.db"),
        backup_dir=str(tmp_path / "backups"),
        health_dir=str(tmp_path / "health"),
        invalid_dir=str(tmp_path / "invalid"),
        database_lock_path=str(tmp_path / "db.lock"),
        role="doctor",
        idle_probe=lambda: (False, reason),
        startup_delay_sec=0.01,
        deferred_retry_sec=5.0,
    )
    worker = pytest.fail
    monkeypatch.setattr(scheduler, "_run_worker", lambda _date: worker("worker must not start while busy"))
    monkeypatch.setattr(daily, "record_metric", lambda *args, **kwargs: None)
    assert scheduler.start()
    deadline = time.time() + 2.0
    while scheduler.get_probe().get("status") != "deferred" and time.time() < deadline:
        time.sleep(0.01)
    probe = scheduler.get_probe()
    assert probe["status"] == "deferred" and probe["reason"] == reason
    assert scheduler.stop()


def test_corrupt_source_failure_keeps_last_healthy_backup(tmp_path: Path) -> None:
    source = tmp_path / "medical.db"
    _make_database(source)
    previous = daily._perform_daily_backup(_config(tmp_path, backup_date="2026-10-16"))
    previous_path = Path(previous["backup_path"])
    previous_sha = _sha(previous_path)
    source.write_bytes(b"not a sqlite database")

    capture = _CaptureConnection()
    daily._daily_backup_worker(_config(tmp_path, backup_date="2026-10-17"), capture)

    assert capture.value["status"] == "failure"
    assert previous_path.exists() and _sha(previous_path) == previous_sha
    assert validate_sqlite_file(str(previous_path)) == (True, "ok")


def test_same_size_corrupt_daily_copy_is_not_deduplicated_and_can_be_repaired(tmp_path: Path, monkeypatch) -> None:
    _make_database(tmp_path / "medical.db")
    config = _config(tmp_path, backup_date="2026-10-17")
    original = daily._perform_daily_backup(config)
    backup = Path(original["backup_path"])
    raw = bytearray(backup.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    backup.write_bytes(raw)
    assert backup.stat().st_size == int(json.loads(Path(f"{backup}.meta.json").read_text(encoding="utf-8"))["size_bytes"])

    original_copy = daily._bounded_sqlite_copy
    monkeypatch.setattr(daily, "_bounded_sqlite_copy", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("copy failed")))
    capture = _CaptureConnection()
    daily._daily_backup_worker(_config(tmp_path, backup_date="2026-10-17"), capture)
    assert capture.value["status"] == "failure", "same-size SHA corruption must not report already_done"

    monkeypatch.setattr(daily, "_bounded_sqlite_copy", original_copy)
    repaired = daily._perform_daily_backup(_config(tmp_path, backup_date="2026-10-17"))
    assert repaired["status"] == "success" and repaired["reason"] == "created"
    assert validate_sqlite_file(str(backup)) == (True, "ok")


def test_hard_timeout_terminates_blocked_worker_and_cleans_only_its_reservation(tmp_path: Path) -> None:
    _make_database(tmp_path / "medical.db")
    gate = FileWriteLock(str(tmp_path / "db.lock"), logger=daily.logger)
    assert gate.acquire("test-owner", "test_busy_writer")
    scheduler = daily.DailyBackupScheduler(
        database_path=str(tmp_path / "medical.db"),
        backup_dir=str(tmp_path / "backups"),
        health_dir=str(tmp_path / "health"),
        invalid_dir=str(tmp_path / "invalid"),
        database_lock_path=str(tmp_path / "db.lock"),
        role="doctor",
        worker_timeout_sec=0.35,
    )
    try:
        result = scheduler._run_worker("2026-10-17")
    finally:
        gate.release()

    assert result["status"] == "failure" and result["reason"] == "worker_timeout"
    assert not list((tmp_path / "health").glob("daily_central_backup_*.lock"))
    assert scheduler.get_probe()["worker_alive"] is False


def test_stop_cancels_blocked_worker_without_closing_caller_database(tmp_path: Path) -> None:
    source = tmp_path / "medical.db"
    _make_database(source)
    caller_connection = sqlite3.connect(source)
    gate = FileWriteLock(str(tmp_path / "db.lock"), logger=daily.logger)
    assert gate.acquire("test-owner", "test_busy_writer")
    scheduler = daily.DailyBackupScheduler(
        database_path=str(source),
        backup_dir=str(tmp_path / "backups"),
        health_dir=str(tmp_path / "health"),
        invalid_dir=str(tmp_path / "invalid"),
        database_lock_path=str(tmp_path / "db.lock"),
        role="nurse",
        target_hour=0,
        startup_delay_sec=0.01,
        worker_timeout_sec=10.0,
    )
    try:
        assert scheduler.start()
        deadline = time.time() + 5.0
        while not scheduler.get_probe().get("worker_alive") and time.time() < deadline:
            time.sleep(0.02)
        assert scheduler.get_probe()["worker_alive"]
        assert scheduler.stop(timeout_sec=1.0)
        assert caller_connection.execute("SELECT value FROM records").fetchone() == ("saved",)
    finally:
        gate.release()
        caller_connection.close()
    assert not list((tmp_path / "health").glob("daily_central_backup_*.lock"))


def test_dead_worker_cleanup_preserves_foreign_or_replacement_owner(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=time.sleep, args=(0.01,))
    process.start()
    dead_pid = int(process.pid)
    process.join(timeout=5.0)
    assert process.exitcode == 0
    process.close()

    lock_path = tmp_path / "reservation.lock"
    base = {
        "timestamp": time.time(),
        "pid": dead_pid,
        "host": socket.gethostname(),
        "user_id": "daily-backup-job:replacement",
        "source": daily.DAILY_BACKUP_SOURCE,
        "thread_id": 1,
        "lock_token": uuid.uuid4().hex,
    }
    lock_path.write_text(json.dumps(base), encoding="utf-8")
    assert not daily.cleanup_confirmed_dead_worker_reservation(
        str(lock_path), worker_pid=dead_pid, job_token="original"
    )
    assert lock_path.exists()

    foreign = {**base, "host": "another-workstation", "user_id": "daily-backup-job:replacement"}
    lock_path.write_text(json.dumps(foreign), encoding="utf-8")
    assert not daily.cleanup_confirmed_dead_worker_reservation(
        str(lock_path), worker_pid=dead_pid, job_token="replacement"
    )
    assert lock_path.exists()

    lock_path.write_text(json.dumps(base), encoding="utf-8")
    assert daily.cleanup_confirmed_dead_worker_reservation(
        str(lock_path), worker_pid=dead_pid, job_token="replacement"
    )
    assert not lock_path.exists()

    database_lock = {**base, "source": daily.DAILY_DATABASE_LOCK_SOURCE}
    lock_path.write_text(json.dumps(database_lock), encoding="utf-8")
    assert daily.cleanup_confirmed_dead_worker_reservation(
        str(lock_path),
        worker_pid=dead_pid,
        job_token="replacement",
        owner_source=daily.DAILY_DATABASE_LOCK_SOURCE,
    )
    assert not lock_path.exists()


def test_cleanup_refuses_to_remove_staging_while_worker_is_alive(tmp_path: Path) -> None:
    staging = tmp_path / ".daily.staging"
    staging.write_bytes(b"in progress")
    result = daily._cleanup_dead_worker_artifacts(
        reservation_path=str(tmp_path / "reservation.lock"),
        database_lock_path=str(tmp_path / "db.lock"),
        staging_path=str(staging),
        worker_pid=os.getpid(),
        job_token="current",
    )
    assert result == {"reservation": False, "database_lock": False, "staging": False}
    assert staging.exists()


def test_termination_failure_retains_worker_and_refuses_cleanup(tmp_path: Path, monkeypatch) -> None:
    class UnstoppableProcess:
        pid = 424242

        def is_alive(self):
            return True

        def terminate(self):
            pass

        def kill(self):
            pass

        def join(self, timeout=None):
            pass

        def close(self):
            pytest.fail("living worker must not be closed")

    scheduler = daily.DailyBackupScheduler(
        database_path=str(tmp_path / "medical.db"),
        backup_dir=str(tmp_path / "backups"),
        health_dir=str(tmp_path / "health"),
        invalid_dir=str(tmp_path / "invalid"),
        database_lock_path=str(tmp_path / "db.lock"),
        role="doctor",
    )
    process = UnstoppableProcess()
    scheduler._process = process
    scheduler._process_job = {
        "reservation_path": str(tmp_path / "reservation.lock"),
        "database_lock_path": str(tmp_path / "db.lock"),
        "staging_path": str(tmp_path / ".staging"),
        "worker_pid": process.pid,
        "job_token": "job",
    }
    monkeypatch.setattr(
        scheduler,
        "_bounded_dead_worker_cleanup",
        lambda *_args, **_kwargs: pytest.fail("living worker must not be cleaned"),
    )

    assert not scheduler._terminate_current_process()
    assert scheduler._process is process and scheduler._process_job is not None


def test_rotation_deletes_only_bounded_metadata_valid_daily_files(tmp_path: Path) -> None:
    source = tmp_path / "medical.db"
    _make_database(source)
    paths = []
    for index, backup_date in enumerate(("2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17")):
        result = daily._perform_daily_backup(_config(tmp_path, backup_date=backup_date))
        path = Path(result["backup_path"])
        stamp = time.time() - (4 - index) * 60
        os.utime(path, (stamp, stamp))
        paths.append(path)

    # Invalid metadata excludes this file from automated deletion.
    Path(f"{paths[0]}.meta.json").write_text("{}", encoding="utf-8")
    config = _config(tmp_path, backup_date="2026-10-17")
    config.update(retention_days=999, max_count=2)
    result = daily._prune_validated_daily_backups(config, str(paths[-1]))

    assert result["deleted"] == 1
    assert paths[0].exists(), "unknown/unvalidated archive must be preserved"
    assert paths[-1].exists(), "newest fully validated archive must be preserved"


def test_environment_disable_is_explicit_and_default_is_enabled(monkeypatch) -> None:
    monkeypatch.delenv(daily.DAILY_BACKUP_ENV, raising=False)
    assert daily.daily_backup_enabled_from_environment()
    monkeypatch.setenv(daily.DAILY_BACKUP_ENV, "0")
    assert not daily.daily_backup_enabled_from_environment()


def test_database_manager_starts_default_scheduler_only_for_network_clinical_role(monkeypatch, tmp_path: Path) -> None:
    created = []

    class FakeScheduler:
        def __init__(self, **kwargs):
            created.append(kwargs)

        def start(self):
            return True

        def get_probe(self):
            return {"enabled": True, "running": True, "status": "scheduled"}

    monkeypatch.delenv(daily.DAILY_BACKUP_ENV, raising=False)
    monkeypatch.setattr(dbm, "DailyBackupScheduler", FakeScheduler)
    manager = dbm.DatabaseManager.__new__(dbm.DatabaseManager)
    manager._runtime_role = "doctor"
    manager.runtime_context = SimpleNamespace(is_network=True, mode="network")
    manager.db_path = str(tmp_path / "medical.db")
    manager.medical_backups_valid_dir = str(tmp_path / "backups")
    manager.medical_backup_health_dir = str(tmp_path / "health")
    manager.medical_invalid_backups_dir = str(tmp_path / "invalid")
    manager.medical_db_lock_path = str(tmp_path / "db.lock")
    manager._daily_backup_scheduler = None

    assert manager._start_daily_backup_scheduler()
    assert created and created[0]["database_path"] == manager.db_path
    assert manager.get_daily_backup_probe()["status"] == "scheduled"

    created.clear()
    manager._runtime_role = "admin"
    manager._daily_backup_scheduler = None
    assert not manager._start_daily_backup_scheduler()
    assert not created
    assert manager.get_daily_backup_probe()["status"] == "disabled_runtime_scope"
