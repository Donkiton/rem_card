"""Контракт очереди и синхронных callbacks у трёх клинических сервисов."""

from types import SimpleNamespace

import pytest

from rem_card.services.fluid_service import FluidService
from rem_card.services.patient_service import PatientService
from rem_card.services.patient_bed_management.service import PatientBedManagementService


@pytest.fixture(params=[PatientService, FluidService, PatientBedManagementService])
def submit(request):
    def call(data_service, operation, on_success=None, on_error=None):
        return request.param.enqueue_write(
            SimpleNamespace(data_service=data_service), "save:test", operation, on_success, on_error,
        )
    return call


@pytest.mark.parametrize("queue_result", [True, False, None])
def test_queue_owns_execution_and_receives_original_callbacks(submit, queue_result):
    received = []
    executed = []
    operation = lambda: executed.append(True)
    success = lambda value: None
    error = lambda exc: None
    queue = SimpleNamespace(enqueue_write=lambda **kwargs: received.append(kwargs) or queue_result)
    assert submit(queue, operation, success, error) is None
    assert executed == []
    assert received == [{
        "description": "save:test", "operation": operation,
        "on_success": success, "on_error": error,
    }]


def test_rejected_queue_does_not_run_synchronous_fallback(submit):
    executed = []
    failure = RuntimeError("queue rejected")
    def reject(**kwargs):
        raise failure
    with pytest.raises(RuntimeError) as caught:
        submit(SimpleNamespace(enqueue_write=reject), lambda: executed.append(True))
    assert caught.value is failure
    assert executed == []


def test_synchronous_success_preserves_result_identity_and_return_contract(submit):
    payload = object()
    received = []
    assert submit(None, lambda: payload, received.append) is None
    assert received == [payload]


@pytest.mark.parametrize("handle", [True, False])
def test_operation_error_is_reported_or_propagated_once(submit, handle):
    failure = ValueError("operation failed")
    errors = []
    successes = []
    def operation():
        raise failure
    if handle:
        assert submit(None, operation, successes.append, errors.append) is None
        assert errors == [failure]
    else:
        with pytest.raises(ValueError) as caught:
            submit(None, operation, successes.append)
        assert caught.value is failure
    assert successes == []


def test_success_callback_error_is_not_reported_as_failed_write(submit):
    committed = []
    errors = []
    failure = RuntimeError("UI callback failed")
    def on_success(value):
        assert value == "committed"
        raise failure
    with pytest.raises(RuntimeError) as caught:
        submit(None, lambda: committed.append(True) or "committed", on_success, errors.append)
    assert caught.value is failure
    assert committed == [True]
    assert errors == []


@pytest.mark.parametrize("description", ["patient_bed_create_admission:1", "patient_bed_update_admission:7"])
def test_patient_form_write_metadata_has_one_queue_and_native_budget(description):
    received = []
    service = PatientBedManagementService(SimpleNamespace(), SimpleNamespace(enqueue_write=lambda **kw: received.append(kw)))
    settled = lambda *_args: None
    service.enqueue_write(description, lambda: None, on_settled=settled)
    assert received[0]["on_settled"] is settled
    assert received[0]["write_metadata"] == {
        "queue_deadline_sec": 15.0, "write_budget_ms": 15000,
        "queue_retryable": False, "queue_retries_left": 0,
        "queue_cancel_before_start": True,
    }


@pytest.mark.parametrize("unknown", [False, True])
def test_synchronous_settled_precedes_error_callback_and_keeps_uncertainty(unknown):
    from rem_card.services.write_dispatch import enqueue_service_write

    class Failure(RuntimeError):
        outcome_unknown = unknown

    failure = Failure("synthetic failure")
    calls = []
    def operation():
        raise failure
    enqueue_service_write(
        None, "test", operation, on_error=lambda exc: calls.append(("error", exc)),
        on_settled=lambda state, value: calls.append((state, value)),
    )
    assert calls == [("unknown" if unknown else "failed", failure), ("error", failure)]


def test_failed_lifecycle_callback_does_not_replay_or_fail_committed_write():
    from rem_card.services.write_dispatch import enqueue_service_write

    writes, results, errors = [], [], []
    def settled(*_args):
        raise RuntimeError("notification failed")
    enqueue_service_write(None, "test", lambda: writes.append(1) or "saved", results.append, errors.append, on_settled=settled)
    assert writes == [1] and results == ["saved"] and errors == []


@pytest.mark.parametrize("unknown", [False, True])
def test_patient_queue_never_replays_raw_busy_or_unknown_failure(unknown):
    import sqlite3
    import threading
    from test_unified_write_outcomes import _service_harness

    class CleanupFailure(sqlite3.DatabaseError):
        outcome_unknown = True

    data = _service_harness()
    service = PatientBedManagementService(SimpleNamespace(), data)
    attempts, terminal, errors = [], [], []
    done = threading.Event()
    failure = CleanupFailure("own rollback failed") if unknown else sqlite3.OperationalError("database is locked")
    def operation():
        attempts.append(1)
        raise failure
    def settled(state, payload):
        terminal.append((state, payload))
        done.set()
    try:
        service.enqueue_write("patient_bed_create_admission:1", operation, on_error=errors.append, on_settled=settled)
        assert done.wait(2)
        assert data._queue.shutdown(2)
        assert attempts == [1]
        assert terminal == [("unknown" if unknown else "failed", failure)]
        assert errors == [failure]
        assert data.write_outcomes()[0]["state"] == terminal[0][0]
    finally:
        data.shutdown(timeout=1, application_exit=True)


def test_patient_queue_expiry_during_shutdown_settles_once_without_running_sql():
    import threading
    from test_unified_write_outcomes import _service_harness

    data = _service_harness()
    service = PatientBedManagementService(SimpleNamespace(), data)
    service.PATIENT_FORM_WRITE_BUDGET_MS = 50
    entered, release, settled = threading.Event(), threading.Event(), threading.Event()
    calls, terminal, ui_errors = [], [], []
    data._error_callback_requested.dispatch_callbacks = False
    def blocker():
        entered.set()
        assert release.wait(2)
    try:
        data._queue.submit(blocker, "legacy blocked task", retryable=False)
        assert entered.wait(1)
        service.enqueue_write(
            "patient_bed_update_admission:7", lambda: calls.append(1), on_error=ui_errors.append,
            on_settled=lambda state, payload: (terminal.append(state), settled.set()),
        )
        data.set_shutting_down()
        assert not data._queue.shutdown(0.01)
        assert settled.wait(1) and terminal == ["failed"] and calls == []
        release.set()
        data._queue._thread.join(2)
        assert not data._queue._thread.is_alive() and data._queue.is_idle()
        assert terminal == ["failed"] and calls == [] and ui_errors == []
        assert data.write_outcomes()[0]["state"] == "failed"
    finally:
        release.set()
        data.shutdown(timeout=1, application_exit=True)


def test_queue_timer_cannot_settle_an_already_running_mutation():
    import threading
    import time
    from rem_card.app.sqlite_shared import LocalWriteQueue

    queue = LocalWriteQueue()
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    results, errors = [], []
    def operation():
        entered.set()
        assert release.wait(2)
        return "committed after deadline"
    try:
        queue.submit(operation, "patient-save", deadline=time.perf_counter() + 0.05, cancel_before_start=True,
                     on_success=lambda result: (results.append(result), done.set()), on_error=errors.append)
        assert entered.wait(1)
        assert not done.wait(0.10) and errors == []
        release.set()
        assert done.wait(1)
        assert results == ["committed after deadline"] and errors == []
    finally:
        release.set()
        assert queue.shutdown(2)


def test_queued_expiry_and_worker_claim_have_one_terminal_owner():
    import threading
    from rem_card.app.sqlite_shared import LocalWriteQueue, QueuedWriteTask

    queue = LocalWriteQueue()
    try:
        for _ in range(30):
            calls, outcomes = [], []
            barrier = threading.Barrier(2)
            task = QueuedWriteTask(lambda: calls.append(1), "race", on_error=lambda _: outcomes.append("expired"))
            task._start_lock = threading.Lock()
            def claim():
                barrier.wait(timeout=2)
                if queue._claim_queued_task(task):
                    task.func()
                    outcomes.append("started")
            def expire():
                barrier.wait(timeout=2)
                queue._expire_queued_task(task)
            threads = [threading.Thread(target=claim), threading.Thread(target=expire)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)
                assert not thread.is_alive()
            queue._expire_queued_task(task)
            assert len(outcomes) == 1
            assert calls == ([1] if outcomes == ["started"] else [])
    finally:
        assert queue.shutdown(2)


def test_expired_task_is_skipped_and_later_task_runs_after_broken_callback():
    import threading
    import time
    from rem_card.app.sqlite_shared import LocalWriteQueue

    queue = LocalWriteQueue()
    entered, release, expired, done = threading.Event(), threading.Event(), threading.Event(), threading.Event()
    calls, success = [], []
    def blocker():
        entered.set()
        assert release.wait(2)
    def broken(_exc):
        expired.set()
        raise RuntimeError("notification failed")
    try:
        queue.submit(blocker, "legacy blocked task", retryable=False)
        assert entered.wait(1)
        queue.submit(lambda: calls.append(1), "expire", deadline=time.perf_counter() + 0.05,
                     cancel_before_start=True, on_error=broken)
        queue.submit(lambda: "next", "next", on_success=lambda value: (success.append(value), done.set()))
        assert expired.wait(1)
        release.set()
        assert done.wait(1)
        assert queue.shutdown(2)
        assert calls == [] and success == ["next"] and queue.is_idle()
    finally:
        release.set()
        queue.shutdown(2)


def test_patient_deadline_timer_start_failure_cannot_publish_a_write(monkeypatch):
    import threading
    from rem_card.app import sqlite_shared
    from test_unified_write_outcomes import _service_harness

    data = _service_harness()
    service = PatientBedManagementService(SimpleNamespace(), data)
    executed, errors, terminal, following = [], [], [], []
    class BrokenTimer:
        def __init__(self, *_args, **_kwargs):
            pass
        def start(self):
            raise RuntimeError("synthetic thread creation failure")
    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(sqlite_shared.threading, "Timer", BrokenTimer)
            service.enqueue_write(
                "patient_bed_create_admission:2", lambda: executed.append(1), on_error=errors.append,
                on_settled=lambda state, payload: terminal.append(state),
            )
        done = threading.Event()
        data._queue.submit(lambda: following.append(1), "next", on_success=lambda _: done.set(), retryable=False)
        assert done.wait(1)
        assert executed == [] and terminal == ["failed"] and len(errors) == 1
        assert following == [1] and data.write_outcomes()[0]["state"] == "failed"
        assert data._queue.shutdown(2) and data._queue.is_idle()
    finally:
        data.shutdown(timeout=1, application_exit=True)


def test_ordinary_queue_saves_do_not_create_deadline_timer(monkeypatch):
    import threading
    import time
    from rem_card.app import sqlite_shared

    queue = sqlite_shared.LocalWriteQueue()
    done, results = threading.Event(), []
    def forbidden_timer(*_args, **_kwargs):
        raise AssertionError("Ordinary saves must not start a deadline thread")
    try:
        monkeypatch.setattr(sqlite_shared.threading, "Timer", forbidden_timer)
        queue.submit(lambda: "saved", "nurse_order_mark", deadline=time.perf_counter() + 2,
                     on_success=lambda value: (results.append(value), done.set()))
        assert done.wait(1) and results == ["saved"]
    finally:
        assert queue.shutdown(2)
