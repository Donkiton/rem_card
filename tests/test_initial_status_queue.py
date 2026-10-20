from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from rem_card.data.dao.patient_status_dao import InitialStatusOutcome, PatientStatusDAO
from rem_card.app.network_write_worker import NetworkWriteWorkerTimeout
from rem_card.app.sqlite_shared import QueuedWriteDeadlineExceeded, SQLiteWriteController, configure_connection
from rem_card.services import patient_status_service as patient_status_service_module
from rem_card.services.patient_status_service import PatientStatusService
from rem_card.services.remcard_facade import RemCardService


class _StatusDb:
    def __init__(self, path=":memory:"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE patient_status_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admission_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT,
                created_by TEXT,
                created_at TEXT,
                updated_at TEXT,
                revision INTEGER DEFAULT 0
            )
            """
        )

    @contextmanager
    def remcard_transaction(self):
        try:
            yield self.conn.cursor()
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise


class _Queue:
    def __init__(self):
        self.calls = []

    def enqueue_write(self, **kwargs):
        self.calls.append(kwargs)
        return True


class _RejectingQueue(_Queue):
    def enqueue_write(self, **kwargs):
        self.calls.append(kwargs)
        kwargs["on_settled"]("failed", RuntimeError("rejected"))
        return False


class _RunningQueue(_Queue):
    def enqueue_write(self, **kwargs):
        self.calls.append(kwargs)
        try:
            result = kwargs["operation"]()
        except Exception as exc:
            kwargs["on_settled"]("failed", exc)
            if kwargs["on_error"]:
                kwargs["on_error"](exc)
        else:
            kwargs["on_settled"]("committed", result)
            if kwargs["on_success"]:
                kwargs["on_success"](result)
        return True


def _commit_busy_timeout(message="Write budget exhausted at commit"):
    busy = sqlite3.OperationalError("database is locked")
    try:
        raise busy
    except sqlite3.OperationalError as exc:
        raise QueuedWriteDeadlineExceeded(message) from exc


def test_initial_status_dao_returns_explicit_outcomes_and_propagates_errors():
    db = _StatusDb()
    try:
        dao = PatientStatusDAO(db)
        shift = datetime(2026, 9, 28, 8)
        assert dao.ensure_initial_status(7, shift, shift + timedelta(hours=1)) == InitialStatusOutcome.CREATED
        assert dao.ensure_initial_status(7, shift, shift + timedelta(hours=1)) == InitialStatusOutcome.NO_CHANGE

        db.conn.execute("DROP TABLE patient_status_events")
        with pytest.raises(sqlite3.OperationalError):
            dao.ensure_initial_status(7, shift, shift)
    finally:
        db.conn.close()


def test_initial_status_queue_coalesces_until_terminal_callback_and_sets_budgets():
    queue = _Queue()
    dao = SimpleNamespace(ensure_initial_status=lambda *_args: InitialStatusOutcome.CREATED, db=SimpleNamespace())
    service = PatientStatusService(dao, data_service=queue)
    shift = datetime(2026, 9, 28, 8)

    assert service.enqueue_initial_status(7, shift, shift)
    assert not service.enqueue_initial_status(7, shift, shift)
    assert len(queue.calls) == 1
    call = queue.calls[0]
    assert call["operation"]() == InitialStatusOutcome.CREATED
    metadata = call["write_metadata"]
    assert 0 < metadata["queue_deadline_sec"] <= service.INITIAL_STATUS_QUEUE_DEADLINE_SEC
    assert metadata["write_budget_ms"] == service.INITIAL_STATUS_WRITE_BUDGET_MS
    assert metadata["queue_retryable"] is False
    assert metadata["queue_retries_left"] == 0

    call["on_settled"]("committed", InitialStatusOutcome.CREATED)
    assert not service.enqueue_initial_status(7, shift, shift)
    assert service.enqueue_initial_status(7, shift, shift + timedelta(minutes=1))

    service.invalidate_initial_status_cache(7)
    assert service.enqueue_initial_status(7, shift, shift)


def test_initial_status_rejection_releases_coalescing_key_for_next_refresh():
    queue = _RejectingQueue()
    dao = SimpleNamespace(ensure_initial_status=lambda *_args: InitialStatusOutcome.CREATED, db=SimpleNamespace())
    service = PatientStatusService(dao, data_service=queue)
    shift = datetime(2026, 9, 28, 8)

    assert not service.enqueue_initial_status(7, shift, shift)
    assert not service.enqueue_initial_status(7, shift, shift)
    assert len(queue.calls) == 2


def test_initial_status_retries_confirmed_commit_rollback_and_commits_once():
    queue = _RunningQueue()
    attempts = []
    stored = []
    callbacks = []

    def ensure(*_args):
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            _commit_busy_timeout()
        if not stored:
            stored.append("ACTIVE")
            return InitialStatusOutcome.CREATED
        return InitialStatusOutcome.NO_CHANGE

    dao = SimpleNamespace(ensure_initial_status=ensure, db=SimpleNamespace())
    service = PatientStatusService(dao, data_service=queue)
    shift = datetime(2026, 9, 28, 8)

    assert service.enqueue_initial_status(
        7,
        shift,
        shift,
        on_success=lambda result: callbacks.append(("success", result)),
        on_error=lambda exc: callbacks.append(("error", exc)),
        on_settled=lambda state, payload: callbacks.append(("settled", state, payload)),
    )

    assert attempts == [1, 2]
    assert len(queue.calls) == 1
    assert stored == ["ACTIVE"]
    assert callbacks == [
        ("settled", "committed", InitialStatusOutcome.CREATED),
        ("success", InitialStatusOutcome.CREATED),
    ]
    assert not service.enqueue_initial_status(7, shift, shift)

    service.invalidate_initial_status_cache(7)
    assert service.enqueue_initial_status(7, shift, shift)
    assert attempts == [1, 2, 3]
    assert stored == ["ACTIVE"]


def test_initial_status_real_commit_busy_rolls_back_before_retry(tmp_path, monkeypatch):
    path = tmp_path / "status.db"
    db = _StatusDb(path)
    configure_connection(db.conn, profile="network")
    controller = SQLiteWriteController(str(path), str(tmp_path / "db.lock"), "status-test", retry_delay_ms=10)
    reader = sqlite3.connect(path, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM patient_status_events").fetchall()
    attempts = []
    released = []

    @contextmanager
    def transaction():
        attempts.append(1)
        with controller.transaction(db.conn, write_options={"write_budget_ms": 100}) as cursor:
            yield cursor

    def release_reader_before_retry(_delay):
        # A real blocked COMMIT must have rolled back before retry is allowed.
        assert not db.conn.in_transaction
        assert db.conn.execute("SELECT COUNT(*) FROM patient_status_events").fetchone()[0] == 0
        reader.execute("ROLLBACK")
        released.append(True)

    db.remcard_transaction = transaction
    monkeypatch.setattr(patient_status_service_module, "time", SimpleNamespace(
        perf_counter=time.perf_counter, sleep=release_reader_before_retry,
    ))
    queue = _RunningQueue()
    service = PatientStatusService(PatientStatusDAO(db), data_service=queue)
    shift = datetime(2026, 9, 28, 8)
    results = []
    errors = []
    try:
        assert service.enqueue_initial_status(7, shift, shift, on_success=results.append, on_error=errors.append)
        assert attempts == [1, 1]
        assert released == [True]
        assert errors == []
        assert results == [InitialStatusOutcome.CREATED]
        assert db.conn.execute("SELECT COUNT(*) FROM patient_status_events").fetchone()[0] == 1
        assert not service.enqueue_initial_status(7, shift, shift)
    finally:
        reader.close()
        db.conn.close()


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("permanent failure"),
        QueuedWriteDeadlineExceeded("Write budget exhausted at commit"),
        NetworkWriteWorkerTimeout(
            operation_id="unknown",
            source="ensure_initial_patient_status:7",
            timeout_sec=3.0,
            phase="commit",
            outcome_unknown=True,
        ),
    ],
)
def test_initial_status_does_not_retry_unknown_or_permanent_failure(failure):
    queue = _RunningQueue()
    attempts = []
    errors = []

    def ensure(*_args):
        attempts.append(1)
        raise failure

    service = PatientStatusService(
        SimpleNamespace(ensure_initial_status=ensure, db=SimpleNamespace()),
        data_service=queue,
    )
    shift = datetime(2026, 9, 28, 8)

    assert service.enqueue_initial_status(7, shift, shift, on_error=errors.append)
    assert attempts == [1]
    assert errors == [failure]
    assert service.enqueue_initial_status(7, shift, shift)


@pytest.mark.parametrize("phase", ["begin_immediate_timeout", "file_lock_timeout", "sql_execute_timeout"])
def test_initial_status_does_not_retry_busy_timeout_before_commit(phase):
    queue = _RunningQueue()
    attempts, errors = [], []

    def ensure(*_args):
        attempts.append(1)
        _commit_busy_timeout(f"Write budget exhausted before commit (initial_status, {phase})")

    service = PatientStatusService(
        SimpleNamespace(ensure_initial_status=ensure, db=SimpleNamespace()), data_service=queue,
    )
    shift = datetime(2026, 9, 28, 8)
    assert service.enqueue_initial_status(7, shift, shift, on_error=errors.append)
    assert attempts == [1]
    assert len(errors) == 1 and isinstance(errors[0], QueuedWriteDeadlineExceeded)


def test_initial_status_retry_is_bounded_by_count_and_overall_deadline(monkeypatch):
    queue = _RunningQueue()
    attempts = []

    def always_busy(*_args):
        attempts.append(1)
        _commit_busy_timeout()

    service = PatientStatusService(
        SimpleNamespace(ensure_initial_status=always_busy, db=SimpleNamespace()),
        data_service=queue,
    )
    service.INITIAL_STATUS_RETRIES = 2
    service.INITIAL_STATUS_QUEUE_DEADLINE_SEC = 1.0
    service.INITIAL_STATUS_WRITE_BUDGET_MS = 10
    shift = datetime(2026, 9, 28, 8)

    assert service.enqueue_initial_status(7, shift, shift)
    assert len(attempts) == 3
    assert len(queue.calls) == 1
    deadlines = [call["write_metadata"]["queue_deadline_sec"] for call in queue.calls]
    assert 0 < deadlines[0] <= 1.0

    clock = {"now": 100.0}
    monkeypatch.setattr(
        patient_status_service_module,
        "time",
        SimpleNamespace(
            perf_counter=lambda: clock["now"],
            sleep=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
        ),
    )
    deadline_queue = _RunningQueue()
    deadline_attempts = []

    def expires_deadline(*_args):
        deadline_attempts.append(1)
        clock["now"] += 0.06
        _commit_busy_timeout()

    deadline_service = PatientStatusService(
        SimpleNamespace(ensure_initial_status=expires_deadline, db=SimpleNamespace()),
        data_service=deadline_queue,
    )
    deadline_service.INITIAL_STATUS_QUEUE_DEADLINE_SEC = 0.05

    assert deadline_service.enqueue_initial_status(8, shift, shift)
    assert deadline_attempts == [1]
    assert len(deadline_queue.calls) == 1


def test_vitals_snapshot_enqueues_initial_status_without_waiting_or_inventing_status():
    facade = RemCardService.__new__(RemCardService)
    queued = []
    direct_calls = []
    facade.orders_dao = SimpleNamespace(db=SimpleNamespace())
    facade.status_service = SimpleNamespace(
        enqueue_initial_status=lambda *args, **kwargs: queued.append((args, kwargs)) or True,
        ensure_initial_status=lambda *args: direct_calls.append(args),
        get_current_status=lambda _admission_id: None,
        get_active_intervals=lambda *_args: [],
    )
    shift = datetime(2026, 9, 28, 8)
    patient = SimpleNamespace(admission_datetime=shift + timedelta(hours=1))
    facade.get_day_period = lambda _date: (shift, shift + timedelta(days=1))
    facade.get_patient = lambda _admission_id: patient
    facade._vitals = SimpleNamespace(
        get_effective_bounds_for_patient=lambda *_args, **_kwargs: (shift, shift + timedelta(days=1)),
        get_chart_window_bounds=lambda _date: (shift, shift + timedelta(days=1)),
    )
    facade.vitals_dao = SimpleNamespace(get_vitals=lambda *_args: [])
    facade.get_latest_vital_values = lambda _admission_id: {}
    facade.get_vital_settings_cached = lambda *_args: {}
    facade.has_card = lambda *_args: False
    facade.has_any_cards_bulk = lambda _ids: {7: False}
    facade.build_plan_card_state = lambda *_args, **_kwargs: {}
    invalidations = []
    facade.read_coordinator = SimpleNamespace(
        invalidate_patient_vitals_for_admission=lambda *args, **kwargs: invalidations.append(("vitals", args, kwargs)),
        invalidate_patient_card_for_admission=lambda *args, **kwargs: invalidations.append(("card", args, kwargs)),
    )

    snapshot = facade.build_vitals_snapshot(7, shift, ensure_initial_status=True)

    assert len(queued) == 1
    assert direct_calls == []
    assert snapshot["initial_status_write_queued"] is True
    assert snapshot["status"] is None
    queued[0][1]["on_success"](InitialStatusOutcome.CREATED)
    assert [item[0] for item in invalidations] == ["vitals", "card"]


def test_snapshot_queue_submission_failure_does_not_fail_the_read():
    facade = RemCardService.__new__(RemCardService)
    facade.status_service = SimpleNamespace(
        enqueue_initial_status=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("queue failed"))
    )

    assert not facade._enqueue_initial_status_for_snapshot(
        7,
        datetime(2026, 9, 28, 8),
        datetime(2026, 9, 28, 9),
    )


def test_status_change_payload_invalidates_only_relevant_completed_admission_key():
    facade = RemCardService.__new__(RemCardService)
    invalidated = []
    facade._patients = SimpleNamespace(invalidate_outcome_release_schedule=lambda: None)
    facade.status_service = SimpleNamespace(
        invalidate_initial_status_cache=lambda admission_id=None: invalidated.append(admission_id)
    )

    facade._handle_data_changes_for_cache(
        {
            "changed_entities": ["patient_status_events"],
            "changes": [
                {"entity_name": "patient_status_events", "admission_id": 7},
                {"entity_name": "patient_status_events", "admission_id": 8},
            ],
        }
    )

    assert invalidated == [7, 8]
