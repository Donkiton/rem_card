from __future__ import annotations

import threading
import time
import unittest
from contextlib import nullcontext
from types import MethodType, SimpleNamespace
from unittest.mock import patch

from rem_card.app.network_write_worker import NetworkWriteWorkerTimeout
from rem_card.app.sqlite_shared import LocalWriteQueue
from rem_card.services.data_service import DataService
from rem_card.services.patient_service import PatientService


class _Signal:
    def __init__(self, *, dispatch_callbacks: bool = False):
        self.values = []
        self.dispatch_callbacks = dispatch_callbacks

    def emit(self, *values):
        self.values.append(values)
        if self.dispatch_callbacks and values and callable(values[0]):
            values[0](*values[1:])


class _Database:
    @staticmethod
    def run_write_operation(operation, *, source: str):
        return operation(SimpleNamespace(source=source))


_BOUND_METHODS = (
    "set_runtime_session",
    "_ensure_write_registry",
    "_begin_write_submission",
    "_finish_write_submission",
    "_register_accepted_write",
    "_record_write_outcome_metric",
    "_set_write_outcome",
    "write_outcomes",
    "unsettled_writes",
    "has_unsettled_writes",
    "_wait_for_write_submissions",
    "_emergency_write_submission_scope",
    "enqueue_write",
    "run_write",
    "set_shutting_down",
    "shutdown",
)


def _service_harness() -> SimpleNamespace:
    service = SimpleNamespace(
        db=_Database(),
        _queue=LocalWriteQueue(),
        _monitor=None,
        _monitor_enabled=False,
        _shutting_down=False,
        _network_outage_detected=False,
        _runtime_role="doctor",
        _runtime_session_id="session-doctor-7",
        _emergency_pause_lock=threading.RLock(),
        _emergency_pause_state=None,
        _emergency_write_submissions=0,
        _unknown_active_write=False,
        _unconfirmed_write_count=0,
        _direct_central_failure_unsubscribe=None,
        write_failed=_Signal(),
        write_finished=_Signal(),
        _success_callback_requested=_Signal(dispatch_callbacks=True),
        _error_callback_requested=_Signal(dispatch_callbacks=True),
    )
    for name in _BOUND_METHODS:
        setattr(service, name, MethodType(getattr(DataService, name), service))
    service._terminal_write_state = DataService._terminal_write_state
    service._uses_direct_central_runtime = lambda: False
    service._reject_write_if_emergency_paused = lambda _description: False
    service._reject_write_if_outage = lambda _description: False
    service._record_operblock_write_intent = lambda _description: None
    service._opblock_interactive_write_metadata = (
        lambda _description, metadata=None: dict(metadata or {})
    )
    service._write_metadata_context = lambda _metadata: nullcontext()
    service._mark_operblock_write_remote_committed = lambda *_args, **_kwargs: True
    service._mark_operblock_write_outcome = lambda *_args, **_kwargs: None
    service._handle_database_access_failure = lambda *_args, **_kwargs: "unknown"
    service._mirror_operblock_write_after_commit = lambda *_args, **_kwargs: None
    service.request_immediate_refresh = lambda **_kwargs: None
    service._stop_emergency_schedulers = lambda *, timeout=5.0: True
    service.set_runtime_session("session-doctor-7", "doctor")
    return service


def _wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


class UnifiedWriteOutcomeTest(unittest.TestCase):
    def setUp(self):
        for attr, target in (
            ("data_metric", "rem_card.services.data_service.record_metric"),
            ("queue_metric", "rem_card.app.sqlite_shared.record_metric"),
            ("flush_metrics", "rem_card.app.local_metrics.flush_metrics"),
        ):
            patcher = patch(target)
            setattr(self, attr, patcher.start())
            self.addCleanup(patcher.stop)

    def test_late_queued_success_resolves_pending_after_shutdown_timeout(self):
        service = _service_harness()
        started = threading.Event()
        release = threading.Event()

        def operation():
            started.set()
            release.wait(1.0)
            return "saved"

        self.assertTrue(service.enqueue_write("save_card", operation))
        self.assertTrue(started.wait(1.0))
        self.assertFalse(service.shutdown(timeout=0.01))
        self.assertEqual([item["state"] for item in service.unsettled_writes()], ["pending"])
        self.assertFalse(service._unknown_active_write)

        release.set()
        self.assertTrue(
            _wait_until(lambda: service.write_outcomes()[0]["state"] == "committed")
        )
        self.assertEqual(service.unsettled_writes(), [])
        self.assertTrue(service.shutdown(timeout=1.0))

    def test_late_queued_failure_is_terminal_before_error_callback(self):
        service = _service_harness()
        started = threading.Event()
        release = threading.Event()
        callback_state = []
        callback_called = threading.Event()

        def operation():
            started.set()
            release.wait(1.0)
            raise ValueError("invalid form")

        def on_error(_exc):
            callback_state.append(service.write_outcomes()[0]["state"])
            callback_called.set()

        self.assertTrue(service.enqueue_write("save_invalid", operation, on_error=on_error))
        self.assertTrue(started.wait(1.0))
        self.assertFalse(service.shutdown(timeout=0.01))
        release.set()
        self.assertTrue(callback_called.wait(1.0))
        self.assertEqual(callback_state, ["failed"])
        self.assertEqual(service.unsettled_writes(), [])
        self.assertTrue(service.shutdown(timeout=1.0))

    def test_synchronous_inflight_write_blocks_shutdown_and_new_admission(self):
        service = _service_harness()
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        result = []

        def operation(_cursor):
            started.set()
            release.wait(1.0)
            return "committed"

        def writer():
            try:
                result.append(service.run_write("sync_save", operation))
            finally:
                finished.set()

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        self.assertTrue(started.wait(1.0))
        self.assertFalse(service.shutdown(timeout=0.01))
        self.assertEqual(service.unsettled_writes()[0]["state"], "pending")
        with self.assertRaisesRegex(RuntimeError, "shutting down"):
            service.run_write("late_sync_save", lambda _cursor: None)

        release.set()
        self.assertTrue(finished.wait(1.0))
        thread.join(timeout=1.0)
        self.assertEqual(result, ["committed"])
        self.assertEqual(service.write_outcomes()[0]["state"], "committed")
        self.assertTrue(service.shutdown(timeout=1.0))

    def test_success_state_and_session_identity_exist_before_callback(self):
        service = _service_harness()
        callback_snapshot = []
        callback_called = threading.Event()

        def on_success(_result):
            callback_snapshot.extend(service.write_outcomes())
            callback_called.set()

        self.assertTrue(
            service.enqueue_write("save_identity", lambda: 19, on_success=on_success)
        )
        self.assertTrue(callback_called.wait(1.0))
        self.assertTrue(service.shutdown(timeout=1.0))

        self.assertEqual(callback_snapshot[0]["state"], "committed")
        self.assertEqual(callback_snapshot[0]["session_id"], "session-doctor-7")
        self.assertEqual(callback_snapshot[0]["role"], "doctor")
        self.assertEqual(len(callback_snapshot[0]["operation_id"]), 32)
        terminal_metric = [
            call.kwargs
            for call in self.data_metric.call_args_list
            if call.kwargs.get("result") == "committed"
        ][0]
        self.assertEqual(terminal_metric["session_id"], "session-doctor-7")
        self.assertEqual(terminal_metric["role"], "doctor")

    def test_unknown_receipt_is_sticky_and_blocks_shutdown(self):
        service = _service_harness()
        operation_id = service._register_accepted_write("network_save")
        timeout = NetworkWriteWorkerTimeout(
            operation_id=operation_id,
            source="network_save",
            timeout_sec=0.1,
            phase="confirm",
            outcome_unknown=True,
        )

        service._set_write_outcome(operation_id, "unknown", timeout)
        service._set_write_outcome(operation_id, "committed")
        self.assertEqual(service.write_outcomes()[0]["state"], "unknown")
        self.assertTrue(service._unknown_active_write)
        self.assertFalse(service.shutdown(timeout=1.0))

        confirmed_rollback = NetworkWriteWorkerTimeout(
            operation_id="confirmed-rollback",
            source="network_save",
            timeout_sec=0.1,
            phase="execute",
            outcome_unknown=False,
        )
        self.assertEqual(DataService._terminal_write_state(confirmed_rollback), "failed")
        self.assertEqual(
            DataService._terminal_write_state(OSError("network path is unavailable")),
            "unknown",
        )

    def test_patient_auto_release_network_failure_is_registered_unknown(self):
        service = _service_harness()

        class _PatientDao:
            @staticmethod
            def release_due_outcome_beds(*, delay_minutes):
                raise OSError("network path is unavailable")

        patient_service = PatientService(_PatientDao(), data_service=service)
        self.assertEqual(patient_service.maybe_release_due_outcome_beds(force=True), 0)
        self.assertTrue(
            _wait_until(
                lambda: bool(service.write_outcomes())
                and service.write_outcomes()[0]["state"] == "unknown"
            )
        )
        self.assertFalse(patient_service._outcome_release_worker_active)
        self.assertEqual(
            service.write_outcomes()[0]["description"],
            "auto_release_outcome_beds",
        )
        self.assertTrue(service._unknown_active_write)
        self.assertFalse(service.shutdown(timeout=1.0))

    def test_patient_auto_release_pending_write_blocks_shutdown_and_keeps_debounce(self):
        service = _service_harness()
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        class _PatientDao:
            @staticmethod
            def release_due_outcome_beds(*, delay_minutes):
                started.set()
                release.wait(1.0)
                return 1

        patient_service = PatientService(_PatientDao(), data_service=service)
        self.assertTrue(patient_service.maybe_release_due_outcome_beds_async(force=True))
        self.assertTrue(started.wait(1.0))
        self.assertFalse(patient_service.maybe_release_due_outcome_beds_async())
        self.assertFalse(service.shutdown(timeout=0.01))
        self.assertEqual(service.unsettled_writes()[0]["state"], "pending")

        release.set()
        self.assertTrue(
            _wait_until(
                lambda: service.write_outcomes()[0]["state"] == "committed"
            )
        )
        self.assertFalse(patient_service._outcome_release_worker_active)
        self.assertFalse(patient_service.maybe_release_due_outcome_beds_async())
        self.assertTrue(service.shutdown(timeout=1.0))


if __name__ == "__main__":
    unittest.main()
