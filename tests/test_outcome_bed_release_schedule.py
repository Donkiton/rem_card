from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from rem_card.data.dao.patient_dao import PatientDAO
from rem_card.data.dao.db_manager import DatabaseManager
from rem_card.services.patient_service import PatientService
from rem_card.services.remcard_facade import RemCardService
from rem_card.services.sync_coordinator import SyncCoordinator
from test_unified_write_outcomes import _service_harness, _wait_until
from test_late_card_outcome import _MemoryDb


class _Clock(datetime):
    current = datetime(2026, 10, 3, 8)

    @classmethod
    def now(cls, tz=None):
        return cls.current


@pytest.fixture
def clock(monkeypatch):
    from rem_card.services import patient_service
    from rem_card.data.dao import patient_dao

    state = SimpleNamespace(mono=1000.0)
    _Clock.current = datetime(2026, 10, 3, 8)
    monkeypatch.setattr(patient_service, "datetime", _Clock)
    monkeypatch.setattr(patient_dao, "datetime", _Clock)
    # Patch the module reference rather than the process-wide time module.
    monkeypatch.setattr(patient_service, "time", SimpleNamespace(
        monotonic=lambda: state.mono, perf_counter=lambda: state.mono,
    ))

    def advance(seconds):
        state.mono += seconds
        _Clock.current += timedelta(seconds=seconds)

    state.advance = advance
    return state


class _ScheduleDao:
    def __init__(self):
        self.deadline = None
        self.reads = 0
        self.releases = 0
        self.before_read = None

    def get_next_outcome_bed_release_at(self, *, delay_minutes):
        self.reads += 1
        if self.before_read:
            self.before_read()
        return self.deadline

    def release_due_outcome_beds(self, *, delay_minutes):
        self.releases += 1
        self.deadline = None
        return 1


def test_idle_hour_has_sixty_checks_and_no_mutations(clock):
    dao = _ScheduleDao()
    service = PatientService(dao)
    for _ in range(1800):
        service.maybe_release_due_outcome_beds()
        clock.advance(2)
    assert dao.reads == 60
    assert dao.releases == 0


def test_deadline_is_checked_between_control_reconciliations(clock):
    dao = _ScheduleDao()
    dao.deadline = _Clock.current + timedelta(seconds=31)
    service = PatientService(dao)
    assert service.maybe_release_due_outcome_beds() == 0
    clock.advance(30)
    assert service.maybe_release_due_outcome_beds() == 0
    assert dao.reads == 1
    clock.advance(2)
    assert service.maybe_release_due_outcome_beds() == 1
    assert dao.reads == 2
    assert dao.releases == 1
    clock.advance(16)
    assert service.maybe_release_due_outcome_beds() == 0
    assert service._outcome_release_schedule_known


@pytest.mark.parametrize("entity", ["beds", "admissions", "patient_status_events"])
def test_other_client_changes_invalidate_schedule_without_sql(clock, entity):
    dao = _ScheduleDao()
    patients = PatientService(dao)
    patients.maybe_release_due_outcome_beds()
    facade = RemCardService.__new__(RemCardService)
    facade._patients = patients
    payload = SyncCoordinator.classify({"changes": [{"entity_name": entity}]})
    facade._handle_data_changes_for_cache(payload)
    assert dao.reads == 1
    assert not patients._outcome_release_schedule_known
    dao.deadline = _Clock.current - timedelta(seconds=1)
    clock.advance(16)
    assert patients.maybe_release_due_outcome_beds() == 1


def test_unrelated_clinical_changes_do_not_schedule_empty_checks(clock):
    dao = _ScheduleDao()
    patients = PatientService(dao)
    patients.maybe_release_due_outcome_beds()
    facade = RemCardService.__new__(RemCardService)
    facade._patients = patients
    for entity in ("vitals", "fluids", "lab_orders"):
        facade._handle_data_changes_for_cache(SyncCoordinator.classify({"changed_entities": [entity]}))
    clock.advance(16)
    patients.maybe_release_due_outcome_beds()
    assert dao.reads == 1


@pytest.mark.parametrize("reason", ["recovery", "cursor_moved_backwards", "partial_change_rows"])
def test_recovery_and_change_log_gaps_discard_schedule(clock, reason):
    patients = PatientService(_ScheduleDao())
    patients.maybe_release_due_outcome_beds()
    facade = RemCardService.__new__(RemCardService)
    facade._patients = patients
    facade._vitals = SimpleNamespace(invalidate_cache=lambda: None)
    facade._handle_data_changes_for_cache(SyncCoordinator.classify({"reason": reason}))
    assert not patients._outcome_release_schedule_known


def test_invalidation_during_query_cannot_publish_old_schedule(clock):
    dao = _ScheduleDao()
    patients = PatientService(dao)
    dao.before_read = patients.invalidate_outcome_release_schedule
    patients.maybe_release_due_outcome_beds()
    assert not patients._outcome_release_schedule_known
    dao.before_read = None
    clock.advance(16)
    patients.maybe_release_due_outcome_beds()
    assert dao.reads == 2


def test_failed_query_retries_without_trusting_an_empty_schedule(clock):
    dao = _ScheduleDao()
    patients = PatientService(dao)

    def fail():
        raise OSError("network unavailable")

    dao.before_read = fail
    patients.maybe_release_due_outcome_beds()
    assert not patients._outcome_release_schedule_known
    dao.before_read = None
    clock.advance(16)
    patients.maybe_release_due_outcome_beds()
    assert patients._outcome_release_schedule_known


def test_forward_clock_jump_runs_known_deadline(clock):
    dao = _ScheduleDao()
    dao.deadline = _Clock.current + timedelta(minutes=10)
    patients = PatientService(dao)
    patients.maybe_release_due_outcome_beds()
    clock.mono += 16
    _Clock.current += timedelta(minutes=11)
    assert patients.maybe_release_due_outcome_beds() == 1


def test_missed_notification_is_caught_within_a_minute(clock):
    dao = _ScheduleDao()
    patients = PatientService(dao)
    patients.maybe_release_due_outcome_beds()
    dao.deadline = _Clock.current - timedelta(minutes=1)
    clock.advance(59)
    assert patients.maybe_release_due_outcome_beds() == 0
    clock.advance(1)
    assert patients.maybe_release_due_outcome_beds() == 1


def test_queued_schedule_failure_is_registered_and_blocks_new_outage_checks(clock):
    data = _service_harness()
    dao = _ScheduleDao()
    patients = PatientService(dao, data_service=data)

    def fail():
        raise OSError("network path is unavailable")

    dao.before_read = fail
    try:
        assert patients.maybe_release_due_outcome_beds_async(force=True)
        assert _wait_until(lambda: bool(data.write_outcomes()) and data.write_outcomes()[0]["state"] == "unknown")
        assert not patients._outcome_release_schedule_known
        data.block_new_writes_for_runtime_outage({"category": "network_unavailable"})
        assert not patients.maybe_release_due_outcome_beds_async(force=True)
        assert dao.reads == 1
    finally:
        assert data.shutdown(timeout=1, application_exit=True)


def test_idle_schedule_does_not_submit_tasks_to_real_write_queue(clock):
    data = _service_harness()
    dao = _ScheduleDao()
    patients = PatientService(dao, data_service=data)
    try:
        assert patients.maybe_release_due_outcome_beds_async(force=True)
        assert _wait_until(lambda: bool(data.write_outcomes()) and data.write_outcomes()[0]["state"] == "committed")
        clock.advance(16)
        assert not patients.maybe_release_due_outcome_beds_async()
        assert len(data.write_outcomes()) == 1
        # An overdue visible row can force reconciliation even if the cache
        # has not yet received the other client's change notification.
        dao.deadline = _Clock.current - timedelta(seconds=1)
        assert patients.maybe_release_due_outcome_beds_async(force=True)
        assert _wait_until(lambda: len(data.write_outcomes()) == 2 and data.write_outcomes()[1]["state"] == "committed")
        assert dao.releases == 1
    finally:
        assert data.shutdown(timeout=1)


@pytest.fixture
def db(clock):
    database = _MemoryDb(datetime(2026, 10, 2, 8))

    def write(operation, **kwargs):
        with database.remcard_transaction() as cursor:
            return operation(cursor)

    database.run_write_operation = write
    database.conn.execute("DELETE FROM patient_status_events")
    database.conn.commit()
    try:
        yield database
    finally:
        database.close()


def _outcome(db, status="TRANSFERRED", at="2026-10-03T07:30:00"):
    db.conn.execute(
        "INSERT INTO patient_status_events(admission_id, status, start_time) VALUES (1, ?, ?)",
        (status, at),
    )
    db.conn.commit()


@pytest.mark.parametrize("status", ["TRANSFERRED", "DEAD"])
def test_real_sqlite_releases_at_exact_thirty_minute_boundary(db, clock, status):
    _outcome(db, status)
    patients = PatientService(PatientDAO(db))
    _Clock.current = datetime(2026, 10, 3, 7, 59, 59)
    assert patients.maybe_release_due_outcome_beds() == 0
    assert db.conn.execute("SELECT status FROM beds").fetchone()[0] == "OCCUPIED"
    clock.advance(1)
    assert patients.maybe_release_due_outcome_beds(force=True) == 1
    assert db.conn.execute("SELECT status FROM beds").fetchone()[0] == "FREE"


@pytest.mark.parametrize("change", ["cancel", "postpone", "replace_patient"])
def test_transaction_rechecks_racing_outcome_or_bed_change(db, clock, change):
    _outcome(db)
    original_write = db.run_write_operation

    def race(operation, **kwargs):
        if change == "cancel":
            db.conn.execute("UPDATE patient_status_events SET end_time = '2026-10-03T08:00:00'")
        elif change == "postpone":
            db.conn.execute("UPDATE patient_status_events SET start_time = '2026-10-03T07:59:00'")
        else:
            db.conn.execute("UPDATE beds SET current_admission_id = 2")
        db.conn.commit()
        return original_write(operation, **kwargs)

    db.run_write_operation = race
    assert PatientService(PatientDAO(db)).maybe_release_due_outcome_beds() == 0
    assert db.conn.execute("SELECT status FROM beds").fetchone()[0] == "OCCUPIED"


def test_two_clients_cannot_release_twice(db, clock):
    _outcome(db)
    assert PatientService(PatientDAO(db)).maybe_release_due_outcome_beds() == 1
    assert PatientService(PatientDAO(db)).maybe_release_due_outcome_beds() == 0


def test_schedule_preserves_ready_replica_read_policy_without_central_access(db, clock):
    _outcome(db)
    manager = DatabaseManager.__new__(DatabaseManager)
    manager._in_current_thread_remcard_transaction = lambda: False
    manager._should_read_from_local = lambda: True
    manager._local_replica = SimpleNamespace(fetch_all=db.fetch_all_remcard)

    def forbid_central(*args, **kwargs):
        raise AssertionError("central access must not be added for a ready replica")

    manager._fetch_all_central = forbid_central
    assert PatientDAO(manager).get_next_outcome_bed_release_at() == _Clock.current


def test_invalid_time_closed_outcome_and_free_bed_do_not_create_deadlines(db, clock):
    _outcome(db, at="not-a-time")
    dao = PatientDAO(db)
    assert dao.get_next_outcome_bed_release_at() is None
    db.conn.execute("UPDATE patient_status_events SET start_time = '2026-10-03T07:30:00', end_time = '2026-10-03T07:45:00'")
    assert dao.get_next_outcome_bed_release_at() is None
    db.conn.execute("UPDATE patient_status_events SET end_time = NULL")
    db.conn.execute("UPDATE beds SET status = 'FREE'")
    assert dao.get_next_outcome_bed_release_at() is None
