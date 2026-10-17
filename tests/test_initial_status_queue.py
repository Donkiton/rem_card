from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from rem_card.data.dao.patient_status_dao import InitialStatusOutcome, PatientStatusDAO
from rem_card.services.patient_status_service import PatientStatusService
from rem_card.services.remcard_facade import RemCardService


class _StatusDb:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
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
    assert call["write_metadata"] == {
        "queue_deadline_sec": service.INITIAL_STATUS_QUEUE_DEADLINE_SEC,
        "write_budget_ms": service.INITIAL_STATUS_WRITE_BUDGET_MS,
        "queue_retries_left": service.INITIAL_STATUS_RETRIES,
    }

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
