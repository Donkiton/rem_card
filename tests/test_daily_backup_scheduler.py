from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import shutil
import socket
import sqlite3
import threading
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
    config = _config(tmp_path, backup_date="2026-10-17")
    snapshot_lease = Path(daily._daily_paths(config)["snapshot_lease"])
    original_validate = sqlite_shared.validate_sqlite_file
    original_sha = daily._sha256
    original_copy = daily._bounded_sqlite_copy

    def checked_copy(*args, **kwargs):
        assert snapshot_lease.exists(), "rotation reader lease must cover the live SQLite read"
        return original_copy(*args, **kwargs)

    def checked_validate(path: str):
        assert not db_lock.exists(), "central DB lock leaked into backup validation"
        assert not snapshot_lease.exists(), "rotation reader lease leaked into local validation"
        return original_validate(path)

    def checked_sha(path: str):
        assert not db_lock.exists(), "central DB lock leaked into backup hashing"
        return original_sha(path)

    monkeypatch.setattr(daily, "_bounded_sqlite_copy", checked_copy)
    monkeypatch.setattr(sqlite_shared, "validate_sqlite_file", checked_validate)
    monkeypatch.setattr(daily, "_sha256", checked_sha)
    result = daily._perform_daily_backup(config)
    assert result["status"] == "success"


def test_local_snapshot_and_network_publication_use_separate_staging_paths(
    tmp_path: Path, monkeypatch
) -> None:
    _make_database(tmp_path / "medical.db")
    config = _config(tmp_path, backup_date="2026-10-17")
    config["local_staging_dir"] = str(tmp_path / "local-worker-staging")
    observed: dict[str, Path] = {}
    original_copy = daily._bounded_sqlite_copy
    original_upload = daily._upload_local_staging

    def capture_copy(copy_config, staging_path, **kwargs):
        observed["local"] = Path(staging_path)
        return original_copy(copy_config, staging_path, **kwargs)

    def capture_upload(local_path, publication_path, **kwargs):
        observed["upload_source"] = Path(local_path)
        observed["publication"] = Path(publication_path)
        return original_upload(local_path, publication_path, **kwargs)

    monkeypatch.setattr(daily, "_bounded_sqlite_copy", capture_copy)
    monkeypatch.setattr(daily, "_upload_local_staging", capture_upload)

    result = daily._perform_daily_backup(config)

    assert result["status"] == "success"
    assert observed["local"].parent == Path(config["local_staging_dir"])
    assert observed["upload_source"] == observed["local"]
    assert observed["publication"].parent == Path(config["backup_dir"])
    assert observed["local"].parent != observed["publication"].parent
    assert not observed["local"].exists()
    assert not observed["publication"].exists()


def test_progress_distinguishes_read_copy_upload_and_verify_without_record_data(tmp_path: Path) -> None:
    _make_database(tmp_path / "medical.db", "sensitive row is never telemetry")
    progress: list[dict] = []
    config = _config(tmp_path, backup_date="2026-10-17")

    result = daily._perform_daily_backup(
        config,
        progress_callback=progress.append,
    )

    assert result["status"] == "success"
    started = [item["stage"] for item in progress if item["state"] == "started"]
    assert started == ["read_wait", "copy", "verify_local", "upload", "verify_upload", "publish",
                       "done_marker", "prune", "worker_finalize"]
    assert all(event["attempt_id"] == config["job_token"] for event in progress)
    assert all("sensitive row" not in repr(item) for item in progress)
    assert all("path" not in key for item in progress for key in item)


def test_worker_failure_preserves_verify_phase_before_finalization(tmp_path: Path, monkeypatch) -> None:
    from rem_card.app import sqlite_shared

    _make_database(tmp_path / "medical.db")
    config = _config(tmp_path, backup_date="2026-10-28")
    config["emit_progress"] = True
    messages: list[dict] = []
    diagnostics: list[dict] = []

    class Connection(_CaptureConnection):
        def send(self, message):
            messages.append(message)

    def failed_validation(*_args, **_kwargs):
        raise OSError("validation unavailable")

    monkeypatch.setattr(sqlite_shared, "validate_sqlite_file", failed_validation)
    monkeypatch.setattr(daily, "record_metric", lambda _name, _value, **fields: diagnostics.append(fields))
    daily._daily_backup_worker(config, Connection())
    result = messages[-1]
    assert (result["status"], result["reason"], result["error_class"]) == ("failure", "worker_exception", "OSError")
    assert result["failure_stage"] == "verify_local" and result["failure_stage_state"] == "started"
    assert result["failure_error_class"] == "OSError"
    assert result["failure_copy_pages_remaining"] == 0 and result["failure_copy_pages_total"] > 0
    assert result["failure_stage_wait_ms"] >= 0 and result["failure_reported_elapsed_ms"] is None
    finalize = [message for message in messages if message.get("stage") == "worker_finalize"]
    assert [message["state"] for message in finalize] == ["started", "completed"]
    assert all(message["failure_stage"] == "verify_local" for message in finalize)
    outcome = next(item for item in diagnostics if item.get("event") == "worker_outcome")
    assert outcome["failure_stage"] == "verify_local" and outcome["error_class"] == "OSError"
    assert not Path(config["database_lock_path"]).exists()
    assert not Path(daily._daily_paths(config)["reservation"]).exists()


def test_clinical_writer_can_commit_while_completed_snapshot_is_uploaded(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "medical.db"
    _make_database(source, "before")
    upload_started = threading.Event()
    allow_upload = threading.Event()
    original_upload = daily._upload_local_staging

    def paused_upload(local_path, publication_path, **kwargs):
        upload_started.set()
        assert allow_upload.wait(5.0)
        return original_upload(local_path, publication_path, **kwargs)

    monkeypatch.setattr(daily, "_upload_local_staging", paused_upload)
    result_holder: dict[str, dict] = {}
    backup_thread = threading.Thread(
        target=lambda: result_holder.setdefault(
            "result", daily._perform_daily_backup(_config(tmp_path, backup_date="2026-10-17"))
        )
    )
    backup_thread.start()
    assert upload_started.wait(5.0)

    writer_lock = FileWriteLock(str(tmp_path / "db.lock"))
    assert writer_lock.acquire("clinical", "clinical_write")
    try:
        with sqlite3.connect(source) as connection:
            connection.execute("INSERT INTO records VALUES ('during upload')")
    finally:
        writer_lock.release()
        allow_upload.set()
    backup_thread.join(timeout=10.0)

    assert not backup_thread.is_alive()
    assert result_holder["result"]["status"] == "success"
    with sqlite3.connect(result_holder["result"]["backup_path"]) as connection:
        assert connection.execute("SELECT value FROM records").fetchall() == [("before",)]
    with sqlite3.connect(source) as connection:
        assert connection.execute("SELECT value FROM records").fetchall() == [
            ("before",),
            ("during upload",),
        ]


def test_copy_budget_yields_shared_locks_and_defers_without_publishing(tmp_path: Path) -> None:
    source = tmp_path / "medical.db"
    _make_database(source)
    config = _config(tmp_path, backup_date="2026-10-23")
    config.update(copy_budget_sec=0.25, emit_progress=True)
    paths = daily._daily_paths(config)

    class SlowCopy(_CaptureConnection):
        def send(self, message):
            if message.get("stage") == "copy" and message.get("state") == "started":
                time.sleep(0.3)
            super().send(message)

    capture = SlowCopy()
    daily._daily_backup_worker(config, capture)

    assert capture.value["status"] == "deferred" and capture.value["reason"] == "copy_budget_exhausted"
    assert capture.value["failure_stage"] == "copy"
    assert not Path(paths["done"]).exists()
    assert not Path(paths["backup"]).exists()
    assert not Path(config["database_lock_path"]).exists()
    assert not Path(paths["snapshot_lease"]).exists()
    assert not Path(paths["reservation"]).exists()
    assert not Path(paths["staging"]).exists()
    # The next ordinary attempt can publish the same intact source.
    assert daily._perform_daily_backup(config)["status"] == "success"


def test_unrelated_timeout_keeps_failure_classification(tmp_path: Path, monkeypatch) -> None:
    _make_database(tmp_path / "medical.db")

    def unrelated_timeout(*_args, **_kwargs):
        raise TimeoutError("daily SQLite copy exceeded its copy budget")

    monkeypatch.setattr(daily, "_upload_local_staging", unrelated_timeout)
    capture = _CaptureConnection()
    daily._daily_backup_worker(_config(tmp_path, backup_date="2026-10-23"), capture)

    assert capture.value["status"] == "failure"
    assert capture.value["error_class"] == "TimeoutError"
    assert not (tmp_path / "db.lock").exists()


def test_publication_failure_keeps_last_good_backup_and_cleans_owned_staging(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "medical.db"
    _make_database(source, "last good")
    previous = daily._perform_daily_backup(_config(tmp_path, backup_date="2026-10-16"))
    previous_path = Path(previous["backup_path"])
    previous_sha = _sha(previous_path)
    config = _config(tmp_path, backup_date="2026-10-17")
    config["local_staging_dir"] = str(tmp_path / "local-worker-staging")
    paths = daily._daily_paths(config)
    real_replace = os.replace

    def fail_publication(source_path, destination_path):
        if Path(source_path) == Path(paths["publication_staging"]) and Path(destination_path) == Path(paths["backup"]):
            raise OSError("simulated network loss during publication")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(daily.os, "replace", fail_publication)
    capture = _CaptureConnection()
    daily._daily_backup_worker(config, capture)

    assert capture.value["status"] == "failure"
    assert previous_path.exists() and _sha(previous_path) == previous_sha
    assert validate_sqlite_file(str(previous_path)) == (True, "ok")
    assert not Path(paths["backup"]).exists()
    assert not Path(paths["local_staging"]).exists()
    assert not Path(paths["publication_staging"]).exists()


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
        assert capture.value["status"] == "deferred" and capture.value["reason"] == "central_database_lock_busy"
        assert capture.value["failure_stage"] == "read_wait"
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
    assert result["stage"] == "read_wait"
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
    publication = tmp_path / ".daily.publishing"
    staging.write_bytes(b"in progress")
    publication.write_bytes(b"upload in progress")
    result = daily._cleanup_dead_worker_artifacts(
        reservation_path=str(tmp_path / "reservation.lock"),
        database_lock_path=str(tmp_path / "db.lock"),
        staging_path=str(staging),
        publication_staging_path=str(publication),
        worker_pid=os.getpid(),
        job_token="current",
    )
    assert result == {"reservation": False, "database_lock": False, "staging": False}
    assert staging.exists()
    assert publication.exists()

    context = multiprocessing.get_context("spawn")
    worker = context.Process(target=time.sleep, args=(0.01,))
    worker.start()
    dead_pid = int(worker.pid)
    worker.join(timeout=5.0)
    assert worker.exitcode == 0
    worker.close()
    result = daily._cleanup_dead_worker_artifacts(
        reservation_path=str(tmp_path / "reservation.lock"),
        database_lock_path=str(tmp_path / "db.lock"),
        staging_path=str(staging),
        publication_staging_path=str(publication),
        worker_pid=dead_pid,
        job_token="current",
    )
    assert result["staging"] is True
    assert not staging.exists()
    assert not publication.exists()


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


def _hold_daily_copy_lock(config, sender):
    """A real child dies without finally, like an interrupted network copy."""
    paths = daily._daily_paths(config)
    owner = f"daily-backup-job:{config['job_token']}"
    for path, source in (
        (paths["reservation"], daily.DAILY_BACKUP_SOURCE),
        (config["database_lock_path"], daily.DAILY_DATABASE_LOCK_SOURCE),
        (paths["snapshot_lease"], daily.DAILY_SNAPSHOT_LEASE_SOURCE),
    ):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        lock = FileWriteLock(path)
        assert lock.acquire(owner, source)
    sender.send({"message_type": "progress", "stage": "copy", "state": "started"})
    while not Path(config["database_path"] + ".exit-child").exists():
        time.sleep(0.01)
    os._exit(0)


class _FailingStopProcess:
    def __init__(self, process, *, kill_really: bool):
        self.process = process
        self.kill_really = kill_really

    def __getattr__(self, name):
        return getattr(self.process, name)

    def terminate(self):
        raise PermissionError("simulated TerminateProcess exit race")

    def kill(self):
        if self.kill_really:
            self.process.kill()
        raise PermissionError("simulated TerminateProcess exit race")


def _install_copy_stop_failure(monkeypatch, *, kill_really):
    context = multiprocessing.get_context("spawn")
    workers = []

    class Context:
        Pipe = staticmethod(context.Pipe)

        @staticmethod
        def Process(**kwargs):
            if kwargs.get("name") == "RemCardDailyBackupWorker" and not workers:
                kwargs["target"] = _hold_daily_copy_lock
                worker = _FailingStopProcess(context.Process(**kwargs), kill_really=kill_really)
                workers.append(worker)
                return worker
            return context.Process(**kwargs)

    monkeypatch.setattr(daily.multiprocessing, "get_context", lambda *_args: Context())
    return workers


def test_permission_error_after_killing_copy_reaps_real_owned_database_lock(tmp_path, monkeypatch):
    source = tmp_path / "medical.db"
    _make_database(source)
    workers = _install_copy_stop_failure(monkeypatch, kill_really=True)
    scheduler = daily.DailyBackupScheduler(
        database_path=str(source), backup_dir=str(tmp_path / "backups"),
        health_dir=str(tmp_path / "health"), invalid_dir=str(tmp_path / "invalid"),
        database_lock_path=str(tmp_path / "db.lock"), role="doctor", worker_timeout_sec=0.5,
    )
    try:
        result = scheduler._run_worker("2026-10-23")
        assert result["reason"] == "worker_timeout"
        assert not (tmp_path / "db.lock").exists()
        assert not list((tmp_path / "health").glob("*.lock"))
        assert not scheduler._has_owned_resources()
        # Cleanup must restore another workstation's ability to write, without
        # restarting the owning application.
        writer = FileWriteLock(str(tmp_path / "db.lock"))
        assert writer.acquire("second-client", "clinical_write")
        writer.release()
    finally:
        Path(str(source) + ".exit-child").touch()
        assert scheduler.stop()
        assert workers


def test_scheduler_keeps_failed_stop_owner_and_reaps_after_child_later_dies(tmp_path, monkeypatch):
    source = tmp_path / "medical.db"
    _make_database(source)
    workers = _install_copy_stop_failure(monkeypatch, kill_really=False)
    scheduler = daily.DailyBackupScheduler(
        database_path=str(source), backup_dir=str(tmp_path / "backups"),
        health_dir=str(tmp_path / "health"), invalid_dir=str(tmp_path / "invalid"),
        database_lock_path=str(tmp_path / "db.lock"), role="nurse", startup_delay_sec=0,
        worker_timeout_sec=0.5, check_interval_sec=0.05, deferred_retry_sec=0.05,
        failure_retry_sec=0.05,
    )
    try:
        assert scheduler.start()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and scheduler.get_probe().get("reason") != "worker_cleanup_pending":
            time.sleep(0.02)
        assert scheduler._thread.is_alive()
        assert scheduler._process is workers[0]
        assert (tmp_path / "db.lock").exists()
        assert len(workers) == 1
        Path(str(source) + ".exit-child").touch()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not scheduler._completed_date:
            time.sleep(0.02)
        assert scheduler._completed_date
        assert not (tmp_path / "db.lock").exists()
        assert scheduler._thread.is_alive()
    finally:
        Path(str(source) + ".exit-child").touch()
        assert scheduler.stop(timeout_sec=3)


def test_public_stop_deadline_preserves_a_child_that_cannot_be_stopped(tmp_path, monkeypatch):
    source = tmp_path / "medical.db"
    _make_database(source)
    workers = _install_copy_stop_failure(monkeypatch, kill_really=False)
    scheduler = daily.DailyBackupScheduler(
        database_path=str(source), backup_dir=str(tmp_path / "backups"),
        health_dir=str(tmp_path / "health"), invalid_dir=str(tmp_path / "invalid"),
        database_lock_path=str(tmp_path / "db.lock"), role="doctor", worker_timeout_sec=0.5,
    )
    try:
        assert scheduler._run_worker("2026-10-23")["reason"] == "worker_cleanup_pending"
        started = time.monotonic()
        assert scheduler.stop(timeout_sec=0.05) is False
        assert time.monotonic() - started < 0.3
        assert scheduler._process is workers[0]
        assert (tmp_path / "db.lock").exists()
    finally:
        Path(str(source) + ".exit-child").touch()
        assert scheduler.stop(timeout_sec=3)


def _blocked_cleanup_child(arguments, sender):
    while not Path(arguments["staging_path"] + ".allow-cleanup").exists():
        time.sleep(0.01)
    daily._cleanup_dead_worker_entry(arguments, sender)


def test_unstoppable_cleanup_child_is_owned_and_reused_until_it_finishes(tmp_path, monkeypatch):
    context = multiprocessing.get_context("spawn")
    dead = context.Process(target=time.sleep, args=(0,))
    dead.start()
    dead.join(5)
    assert dead.exitcode == 0
    job = {
        "reservation_path": str(tmp_path / "reservation.lock"),
        "database_lock_path": str(tmp_path / "db.lock"),
        "staging_path": str(tmp_path / "owned.staging"),
        "worker_pid": dead.pid, "job_token": uuid.uuid4().hex,
    }
    Path(job["database_lock_path"]).write_text(json.dumps({
        "host": socket.gethostname(), "pid": dead.pid, "source": daily.DAILY_DATABASE_LOCK_SOURCE,
        "user_id": f"daily-backup-job:{job['job_token']}", "lock_token": uuid.uuid4().hex,
    }), encoding="utf-8")
    Path(job["staging_path"]).write_bytes(b"partial snapshot")
    children = []

    class Context:
        Pipe = staticmethod(context.Pipe)

        @staticmethod
        def Process(**kwargs):
            kwargs["target"] = _blocked_cleanup_child
            child = _FailingStopProcess(context.Process(**kwargs), kill_really=False)
            children.append(child)
            return child

    monkeypatch.setattr(daily.multiprocessing, "get_context", lambda *_args: Context())
    scheduler = daily.DailyBackupScheduler(
        database_path=str(tmp_path / "medical.db"), backup_dir=str(tmp_path / "backups"),
        health_dir=str(tmp_path / "health"), invalid_dir=str(tmp_path / "invalid"),
        database_lock_path=job["database_lock_path"], role="doctor",
    )
    scheduler._process, scheduler._process_job = dead, job
    try:
        assert not scheduler._terminate_current_process(timeout_sec=0.3)
        assert scheduler._cleanup_process is children[0]
        assert not scheduler._terminate_current_process(timeout_sec=0.05)
        assert len(children) == 1
        assert scheduler._process_job is job
        assert Path(job["database_lock_path"]).exists()
        Path(job["staging_path"] + ".allow-cleanup").touch()
        children[0].join(5)
        assert scheduler.stop(timeout_sec=3)
        assert not Path(job["database_lock_path"]).exists()
        assert not Path(job["staging_path"]).exists()
        assert len(children) == 1
    finally:
        Path(job["staging_path"] + ".allow-cleanup").touch()
        assert scheduler.stop(timeout_sec=3)


def test_unknown_worker_liveness_never_cleans_or_replaces_owner(tmp_path, monkeypatch):
    class UnknownProcess:
        pid = 424242

        def is_alive(self):
            raise OSError("liveness unavailable")

        def terminate(self):
            raise PermissionError("cannot terminate")

        kill = terminate

        def join(self, timeout=None):
            pass

    scheduler = daily.DailyBackupScheduler(
        database_path=str(tmp_path / "medical.db"), backup_dir=str(tmp_path / "backups"),
        health_dir=str(tmp_path / "health"), invalid_dir=str(tmp_path / "invalid"),
        database_lock_path=str(tmp_path / "db.lock"), role="doctor",
    )
    owner = UnknownProcess()
    scheduler._process = owner
    scheduler._process_job = {"worker_pid": owner.pid, "job_token": "unknown-owner"}
    monkeypatch.setattr(scheduler, "_bounded_dead_worker_cleanup", lambda *_a, **_k: pytest.fail("unknown owner cleaned"))
    result = scheduler._run_worker("2026-10-23")
    assert result["reason"] == "worker_cleanup_pending"
    assert scheduler._process is owner
    assert scheduler.get_probe()["worker_state"] == "unknown"


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
