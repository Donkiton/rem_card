"""A stopped backup must release the writer gate before a real nurse mark commits."""

from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from rem_card.app import daily_backup_scheduler as daily
from rem_card.app.sqlite_shared import SQLiteWriteController, configure_connection
from rem_card.app.unified_db_schema import ensure_unified_schema


def _block_real_backup_at_copy(config, sender):
    """Use production backup/SQLite handles; emulate a stuck network copy callback."""
    def progress(event):
        sender.send({"message_type": "progress", **event})
        if event.get("stage") == "copy" and event.get("state") == "started":
            while not Path(config["database_path"] + ".exit-child").exists():
                time.sleep(0.01)
            # Emulate abrupt OS termination without Python lock-finally code.
            os._exit(0)

    daily._perform_daily_backup(config, progress_callback=progress)


class _DeniedStopProcess:
    def __init__(self, process, *, kill_really):
        self.process = process
        self.kill_really = kill_really
        self.unknown = False
        self.stop_errors = 0

    def __getattr__(self, name):
        return getattr(self.process, name)

    def is_alive(self):
        if self.unknown:
            raise OSError("synthetic process liveness unavailable")
        return self.process.is_alive()

    def terminate(self):
        self.stop_errors += 1
        raise PermissionError("synthetic TerminateProcess race")

    def kill(self):
        self.stop_errors += 1
        if self.kill_really:
            self.process.kill()
        raise PermissionError("synthetic TerminateProcess race")


def _install_copy_worker(monkeypatch, *, kill_really):
    real_context = multiprocessing.get_context("spawn")
    workers = []

    class Context:
        Pipe = staticmethod(real_context.Pipe)

        @staticmethod
        def Process(**kwargs):
            if kwargs.get("name") == "RemCardDailyBackupWorker":
                kwargs["target"] = _block_real_backup_at_copy
                process = _DeniedStopProcess(real_context.Process(**kwargs), kill_really=kill_really)
                workers.append(process)
                return process
            return real_context.Process(**kwargs)

    monkeypatch.setattr(daily.multiprocessing, "get_context", lambda *_args: Context())
    return workers


class _ClinicalConnection:
    """The actual production writer controller and order domain, without UI/background jobs."""

    def __init__(self, path, lock_path):
        from rem_card.services.order_domain_service import OrderDomainService

        self.conn = sqlite3.connect(path, timeout=0.25, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        configure_connection(self.conn, profile="network")
        self.controller = SQLiteWriteController(
            str(path), str(lock_path), "synthetic-nurse", max_retries=1, retry_delay_ms=10,
        )
        self.orders = OrderDomainService(self)
        self.orders._legacy_statuses_sanitized = True

    @contextmanager
    def remcard_transaction(self):
        with self.controller.transaction(self.conn, source="nurse_order_mark") as cursor:
            yield cursor

    def mark(self):
        self.orders.set_nurse_status(1, "nurse_executed", expected_version=0)

    def outcome(self):
        administration = tuple(self.conn.execute(
            "SELECT comment,version FROM administrations WHERE id=1"
        ).fetchone())
        audit_count = self.conn.execute(
            "SELECT COUNT(*) FROM medical_audit_log WHERE table_name='administrations' AND action_type='update'"
        ).fetchone()[0]
        change_count = self.conn.execute(
            "SELECT COUNT(*) FROM change_log WHERE entity_name='administrations' AND action='update'"
        ).fetchone()[0]
        return administration, audit_count, change_count


def _seed_medical_database(path):
    with sqlite3.connect(path) as conn:
        ensure_unified_schema(conn)
        conn.execute("INSERT INTO patients(id,full_name) VALUES(1,'synthetic backup recovery')")
        conn.execute("INSERT INTO admissions(id,patient_id,bed_number,history_number,admission_datetime) "
                     "VALUES(1,1,1,'synthetic','2026-10-24 08:00:00')")
        conn.execute("INSERT INTO orders(id,admission_id,datetime,text,latin,drug_key,type,status,is_committed) "
                     "VALUES(1,1,'2026-10-24 08:00:00','synthetic','Synthetic','synthetic','medication','active',1)")
        conn.execute("INSERT INTO administrations(id,order_id,planned_time,cell_role,status,is_committed,version) "
                     "VALUES(1,1,'2026-10-24 09:00:00','single','planned',1,0)")


@pytest.fixture
def clinical_backup(tmp_path, monkeypatch):
    source = tmp_path / "medical.db"
    lock = tmp_path / "db.lock"
    _seed_medical_database(source)
    connection = _ClinicalConnection(source, lock)
    schedulers = []
    wrappers = []

    def create(*, kill_really):
        workers = _install_copy_worker(monkeypatch, kill_really=kill_really)
        scheduler = daily.DailyBackupScheduler(
            database_path=str(source), database_lock_path=str(lock),
            backup_dir=str(tmp_path / "backups"), health_dir=str(tmp_path / "health"),
            invalid_dir=str(tmp_path / "invalid"), role="nurse", worker_timeout_sec=1.5,
        )
        schedulers.append(scheduler)
        wrappers.append(workers)
        return scheduler, workers, connection, lock

    yield create
    Path(str(source) + ".exit-child").touch()
    for workers in wrappers:
        for worker in workers:
            worker.unknown = False
            try:
                worker.process.join(timeout=2)
                if worker.process.is_alive():
                    worker.process.kill()
                    worker.process.join(timeout=2)
            except ValueError:
                pass  # A successfully reaped production scheduler closed it.
    for scheduler in schedulers:
        assert scheduler.stop(timeout_sec=3)
    connection.conn.close()


def test_permission_error_reap_allows_one_real_nurse_mark_commit(clinical_backup):
    scheduler, workers, clinical, lock = clinical_backup(kill_really=True)

    result = scheduler._run_worker("2026-10-24")

    assert result["status"] == "failure" and result["reason"] == "worker_timeout"
    assert result["stage"] == "copy", "the real backup must own SQLite and its writer lock before termination"
    assert workers[0].stop_errors >= 2
    assert not scheduler._has_owned_resources()
    assert not lock.exists()
    assert clinical.outcome() == ((None, 0), 0, 0)

    clinical.mark()

    assert clinical.outcome() == (("nurse_executed", 1), 1, 1)
    assert not clinical.conn.in_transaction
    assert not lock.exists()


@pytest.mark.parametrize("liveness", ["alive", "unknown"])
def test_unreaped_backup_cannot_report_success_or_admit_a_nurse_write(clinical_backup, liveness):
    scheduler, workers, clinical, lock = clinical_backup(kill_really=False)

    result = scheduler._run_worker("2026-10-24")

    assert result["status"] == "deferred" and result["reason"] == "worker_cleanup_pending"
    assert result["stage"] == "copy"
    owner = workers[0]
    owner.unknown = liveness == "unknown"
    lock_before = lock.read_bytes()
    payload = json.loads(lock_before)
    assert payload["pid"] == owner.pid
    assert payload["source"] == daily.DAILY_DATABASE_LOCK_SOURCE

    again = scheduler._run_worker("2026-10-24")

    assert again["status"] == "deferred" and again["reason"] == "worker_cleanup_pending"
    assert scheduler._process is owner and len(workers) == 1
    assert scheduler.get_probe()["worker_state"] == liveness
    assert lock.read_bytes() == lock_before
    with pytest.raises(sqlite3.OperationalError, match="lock"):
        clinical.mark()
    assert clinical.outcome() == ((None, 0), 0, 0)
    assert not clinical.conn.in_transaction
    assert lock.read_bytes() == lock_before
