from __future__ import annotations

from types import SimpleNamespace

import pytest

from rem_card.app import daily_backup_scheduler as daily


ATTEMPT = "a" * 32


class _Clock:
    now = 0.0

    def monotonic(self):
        return self.now


class _Process:
    pid = 7123
    alive = True

    def start(self):
        pass

    def is_alive(self):
        return self.alive

    @property
    def exitcode(self):
        return None if self.alive else 0

    def terminate(self):
        self.alive = False

    kill = terminate

    def join(self, timeout=None):
        pass

    def close(self):
        pass


class _Pipe:
    def __init__(self, clock, messages=()):
        self.clock, self.messages = clock, list(messages)
        self.sent = []

    def poll(self, timeout=0):
        self.clock.now += timeout
        return bool(self.messages)

    def recv(self):
        return self.messages.pop(0)

    def send(self, message):
        self.sent.append(message)

    def close(self):
        pass


def _scheduler(tmp_path):
    return daily.DailyBackupScheduler(
        database_path=str(tmp_path / "medical.db"), backup_dir=str(tmp_path / "backups"),
        health_dir=str(tmp_path / "health"), invalid_dir=str(tmp_path / "invalid"),
        database_lock_path=str(tmp_path / "db.lock"), role="doctor", worker_timeout_sec=0.35,
    )


@pytest.fixture
def telemetry(monkeypatch):
    events = []
    monkeypatch.setattr(daily, "record_metric", lambda name, value, **fields: events.append((name, fields)))
    monkeypatch.setattr(daily, "logger", SimpleNamespace(info=lambda *_a: None, warning=lambda *_a: None))
    return events


def _install_worker(monkeypatch, scheduler, messages, *, cleanup_completed=True):
    clock, process = _Clock(), _Process()
    receiving, sending = _Pipe(clock, messages), _Pipe(clock)
    context = SimpleNamespace(Pipe=lambda **_k: (receiving, sending), Process=lambda **_k: process)
    monkeypatch.setattr(daily.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(daily.multiprocessing, "get_context", lambda *_a: context)
    monkeypatch.setattr(daily.uuid, "uuid4", lambda: SimpleNamespace(hex=ATTEMPT))
    monkeypatch.setattr(scheduler, "_bounded_dead_worker_cleanup", lambda *_a, **_k: {"completed": cleanup_completed})
    return clock, process


@pytest.mark.parametrize("with_progress", [False, True])
def test_timeout_snapshot_distinguishes_no_progress_from_stalled_copy(tmp_path, monkeypatch, telemetry, with_progress):
    scheduler = _scheduler(tmp_path)
    messages = [{"message_type": "progress", "attempt_id": ATTEMPT, "stage": "copy",
                 "state": "progress", "elapsed_ms": 120.0, "pages_remaining": 300, "pages_total": 400}] if with_progress else []
    clock, _process = _install_worker(monkeypatch, scheduler, messages)
    def cleanup(*_a, **_k):
        clock.now += 0.5
        return {"completed": True}
    monkeypatch.setattr(scheduler, "_bounded_dead_worker_cleanup", cleanup)
    def forbidden(*_a, **_k):
        pytest.fail("parent diagnostics must not inspect files, locks or SQL")
    with monkeypatch.context() as scoped:
        scoped.setattr(daily.os, "stat", forbidden)
        scoped.setattr(daily, "_read_lock_payload", forbidden)
        scoped.setattr(daily.sqlite3, "connect", forbidden)
        result = scheduler._run_worker("2026-10-28")
    assert (result["status"], result["reason"]) == ("failure", "worker_timeout")
    outcome = next(fields for _, fields in telemetry if fields.get("event") == "attempt_outcome")
    assert outcome["attempt_id"] == result["attempt_id"] == ATTEMPT
    assert outcome["attempt_status"] == "failure" and outcome["attempt_reason"] == "worker_timeout"
    assert outcome["outcome_origin"] == "deadline"
    assert outcome["progress_received"] is with_progress
    assert result["last_progress_age_ms"] == outcome["last_progress_age_ms"]
    assert result["attempt_elapsed_ms"] == outcome["attempt_elapsed_ms"] == pytest.approx(400.0)
    assert outcome["stage_wait_ms"] == pytest.approx(300.0 if with_progress else 400.0)
    if with_progress:
        assert outcome["copy_pages_remaining"] == 300 and outcome["copy_pages_total"] == 400
        assert outcome["last_progress_age_ms"] == outcome["copy_progress_age_ms"] == pytest.approx(300.0)
        assert outcome["stage_reported_elapsed_ms"] == 120.0
    else:
        assert outcome["stage"] == "worker_start"
        assert outcome["last_progress_age_ms"] is None and outcome["copy_pages_remaining"] is None
    assert len(telemetry) <= 12


def test_cleanup_pending_preserves_successful_attempt_and_correlation(tmp_path, monkeypatch, telemetry):
    scheduler = _scheduler(tmp_path)
    _install_worker(monkeypatch, scheduler, [
        {"message_type": "progress", "attempt_id": ATTEMPT, "stage": "publish", "state": "completed"},
        {"status": "success", "reason": "created"},
    ], cleanup_completed=False)
    result = scheduler._run_worker("2026-10-28")
    assert (result["status"], result["reason"]) == ("deferred", "worker_cleanup_pending")
    assert (result["attempt_status"], result["attempt_reason"]) == ("success", "created")
    assert result["outcome_origin"] == "worker"
    scheduler._record_result(result, schedule_reason="night_window")
    # A later reap refusal must still identify the same successful attempt.
    scheduler._record_result({"status": "deferred", "reason": "worker_cleanup_pending"}, schedule_reason="night_window")
    results = [fields for name, fields in telemetry if name == "daily_central_backup_result"]
    assert all(fields["attempt_id"] == ATTEMPT and fields["attempt_status"] == "success" for fields in results)
    stop = [fields for _, fields in telemetry if fields.get("event") == "stop_end"]
    assert stop and stop[0]["child_state"] == "stopped" and stop[0]["child_exitcode"] == 0


def test_timeout_after_copy_keeps_page_progress_age_separate_from_current_stage(tmp_path, monkeypatch, telemetry):
    scheduler = _scheduler(tmp_path)
    _install_worker(monkeypatch, scheduler, [
        {"message_type": "progress", "attempt_id": ATTEMPT, "stage": "copy", "state": "progress",
         "elapsed_ms": 100, "pages_remaining": 0, "pages_total": 400},
        {"message_type": "progress", "attempt_id": ATTEMPT, "stage": "verify_local", "state": "started"},
    ])
    scheduler._run_worker("2026-10-28")
    outcome = next(fields for _, fields in telemetry if fields.get("event") == "attempt_outcome")
    assert outcome["stage"] == "verify_local" and outcome["stage_state"] == "started"
    assert outcome["copy_pages_remaining"] == 0
    assert outcome["last_progress_age_ms"] == outcome["stage_wait_ms"] == pytest.approx(200.0)
    assert outcome["copy_progress_age_ms"] == pytest.approx(300.0)


@pytest.mark.parametrize("worker_result", [False, True])
def test_finalize_keeps_original_failure_separate_from_latest_stage(tmp_path, monkeypatch, telemetry, worker_result):
    scheduler = _scheduler(tmp_path)
    failure = {"failure_stage": "verify_local", "failure_stage_state": "started",
               "failure_stage_wait_ms": 25.0, "failure_error_class": "OSError",
               "failure_copy_pages_remaining": 0, "failure_copy_pages_total": 400}
    messages = [{"message_type": "progress", "stage": "worker_finalize", "state": "started", **failure}]
    if worker_result:
        messages.append({"status": "failure", "reason": "worker_exception", "error_class": "OSError", **failure})
    _install_worker(monkeypatch, scheduler, messages)
    result = scheduler._run_worker("2026-10-28")
    scheduler._record_result(result, schedule_reason="night_window")
    assert result["reason"] == ("worker_exception" if worker_result else "worker_timeout")
    assert result["stage"] == "worker_finalize" and result["failure_stage"] == "verify_local"
    assert result["failure_stage_wait_ms"] == 25.0
    for fields in (fields for name, fields in telemetry
                   if name == "daily_central_backup_result" or fields.get("event") == "attempt_outcome"):
        assert fields["failure_stage"] == "verify_local" and fields["failure_error_class"] == "OSError"


def test_cleanup_child_identity_and_result_diagnostics_do_not_add_io(monkeypatch, telemetry):
    connection = _Pipe(_Clock())
    result = {"reservation": True, "database_lock": True, "staging": False, "completed": True}
    monkeypatch.setattr(daily, "_cleanup_dead_worker_artifacts", lambda **_kwargs: result)
    def forbidden(*_a, **_k):
        pytest.fail("cleanup diagnostics must not repeat ownership/file probes")
    with monkeypatch.context() as scoped:
        scoped.setattr(daily.os, "stat", forbidden)
        scoped.setattr(daily, "_read_lock_payload", forbidden)
        scoped.setattr(daily.sqlite3, "connect", forbidden)
        daily._cleanup_dead_worker_entry({"job_token": ATTEMPT, "worker_pid": 7123}, connection)
    assert connection.sent == [result]
    assert [fields["event"] for _, fields in telemetry] == ["cleanup_child_started", "cleanup_child_result"]
    assert all(fields["attempt_id"] == ATTEMPT and fields["owned_worker_pid"] == 7123 for _, fields in telemetry)


def test_child_progress_and_outcome_share_job_id_without_diagnostic_io(monkeypatch, telemetry):
    connection = _Pipe(_Clock())

    def perform(config, *, progress_callback, failure_callback=None):
        daily._emit_progress(progress_callback, "copy", "progress", attempt_id=config["job_token"],
                             backup_date=config["backup_date"], elapsed_ms=10, pages_remaining=2, pages_total=4)
        return {"status": "success", "reason": "created"}

    monkeypatch.setattr(daily, "_perform_daily_backup", perform)
    def forbidden(*_a, **_k):
        pytest.fail("diagnostics must not inspect files, locks or SQL")
    with monkeypatch.context() as scoped:
        scoped.setattr(daily.os, "stat", forbidden)
        scoped.setattr(daily, "_read_lock_payload", forbidden)
        scoped.setattr(daily.sqlite3, "connect", forbidden)
        daily._daily_backup_worker({"job_token": ATTEMPT, "backup_date": "2026-10-28", "emit_progress": True}, connection)
    assert connection.sent[0]["attempt_id"] == ATTEMPT
    assert connection.sent[-1] == {"status": "success", "reason": "created"}
    assert all(fields["attempt_id"] == ATTEMPT for _, fields in telemetry)
    assert [fields["event"] for name, fields in telemetry if name == "daily_backup_diagnostic"] == ["worker_started", "worker_outcome"]


def test_failed_metrics_logger_and_progress_ipc_do_not_change_worker_or_cleanup(tmp_path, monkeypatch):
    def fail(*_a, **_k):
        raise OSError("diagnostic unavailable")
    monkeypatch.setattr(daily, "record_metric", fail)
    monkeypatch.setattr(daily, "logger", SimpleNamespace(info=fail, warning=fail))
    connection = _Pipe(_Clock())
    original_send = connection.send
    connection.send = lambda message: fail() if message.get("message_type") == "progress" else original_send(message)

    def perform(_config, *, progress_callback, failure_callback=None):
        daily._emit_progress(progress_callback, "copy", "started", attempt_id=ATTEMPT)
        return {"status": "success", "reason": "created"}

    monkeypatch.setattr(daily, "_perform_daily_backup", perform)
    daily._daily_backup_worker({"job_token": ATTEMPT, "emit_progress": True}, connection)
    assert connection.sent == [{"status": "success", "reason": "created"}]
    scheduler = _scheduler(tmp_path)
    _install_worker(monkeypatch, scheduler, [])
    result = scheduler._run_worker("2026-10-28")
    scheduler._record_result(result, schedule_reason="night_window")
    assert result["reason"] == "worker_timeout" and not scheduler._has_owned_resources()
    assert scheduler.get_probe()["reason"] == "worker_timeout"


def test_new_outcome_fields_exclude_raw_exception_messages_and_paths(monkeypatch, telemetry):
    secret = "sensitive_patient C:/clinical/private.db"
    def perform(_config, **_kwargs):
        raise OSError(secret)
    monkeypatch.setattr(daily, "_perform_daily_backup", perform)
    connection = _Pipe(_Clock())
    daily._daily_backup_worker({"job_token": ATTEMPT}, connection)
    assert connection.sent[-1]["error"] == secret  # Existing worker result is unchanged.
    assert all(secret not in repr(fields) and "private.db" not in repr(fields) for _, fields in telemetry)
    assert telemetry[-1][1]["error_class"] == "OSError"


def test_parent_diagnostic_builder_failure_preserves_success_and_cleanup(tmp_path, monkeypatch, telemetry):
    scheduler = _scheduler(tmp_path)
    _install_worker(monkeypatch, scheduler, [
        {"message_type": "progress", "stage": "publish", "state": "completed"},
        {"status": "success", "reason": "created"},
    ])
    def fail(*_a, **_k):
        raise OSError("instrumentation failure")
    monkeypatch.setattr(scheduler, "_observe_progress", fail)
    monkeypatch.setattr(scheduler, "_build_attempt_snapshot", fail)
    result = scheduler._run_worker("2026-10-28")
    scheduler._record_result(result, schedule_reason="night_window")
    assert (result["status"], result["reason"]) == ("success", "created")
    assert not scheduler._has_owned_resources()


def test_copy_progress_stays_throttled_per_time_not_per_page(tmp_path, monkeypatch, telemetry):
    from rem_card.app import sqlite_shared

    clock = _Clock()
    monkeypatch.setattr(daily.time, "monotonic", clock.monotonic)
    class Source:
        def backup(self, target, *, progress, **_kwargs):
            for remaining in range(1000, -1, -1):
                progress(0, remaining, 1000)
        def close(self):
            pass
    monkeypatch.setattr(daily.sqlite3, "connect", lambda *_a, **_k: Source())
    monkeypatch.setattr(sqlite_shared, "configure_connection", lambda *_a, **_k: None)
    monkeypatch.setattr(sqlite_shared, "FileWriteLock", lambda *_a, **_k: SimpleNamespace(acquire=lambda *_a: True, release=lambda: None))
    monkeypatch.setattr(daily.os, "makedirs", lambda *_a, **_k: None)
    monkeypatch.setattr(daily.os, "remove", lambda *_a: None)
    monkeypatch.setattr(daily.os.path, "getsize", lambda *_a: 4096)
    daily._bounded_sqlite_copy({"database_path": str(tmp_path / "source.db"), "database_lock_path": "unused",
                                "job_token": ATTEMPT, "backup_date": "2026-10-28"}, str(tmp_path / "staging.db"))
    progress = [fields for name, fields in telemetry if name == "daily_backup_stage"]
    assert len(progress) == 5
    assert sum(fields["state"] == "progress" for fields in progress) == 1
    assert all(fields["attempt_id"] == ATTEMPT for fields in progress)


def test_stop_diagnostic_failure_keeps_exit_race_handling():
    class ExitRace(_Process):
        def terminate(self):
            self.alive = False
            raise PermissionError("already exited")
    state, errors = daily._stop_process_bounded(ExitRace(), diagnostic=lambda *_a, **_k: (_ for _ in ()).throw(OSError()))
    assert state == "stopped" and errors == ("terminate:PermissionError",)
