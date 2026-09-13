from __future__ import annotations

import builtins
import os
import sqlite3
import sys
import threading
import types
from types import MethodType, SimpleNamespace

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def qt_app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def isolated_database_failure_reporting(monkeypatch):
    from rem_card.app import db_availability

    crash_reports = types.ModuleType("rem_card.services.crash_reports")
    crash_reports.capture_database_failure = lambda *_args, **_kwargs: None
    crash_reports.flush_local_crash_outbox = lambda: None
    monkeypatch.setitem(sys.modules, "rem_card.services.crash_reports", crash_reports)
    db_availability.notify_direct_central_success()
    db_availability.set_database_unavailable_warnings_suppressed(False)
    yield db_availability
    db_availability.notify_direct_central_success()
    db_availability.set_database_unavailable_warnings_suppressed(False)


def test_emergency_dialog_construction_has_no_theme_or_settings_dependency(qt_app, monkeypatch):
    from PySide6.QtWidgets import QDialog
    from rem_card.ui.shared.emergency_dialogs import EmergencyActionDialog

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in {
            "rem_card.ui.styles.theme_manager",
            "rem_card.services.settings.settings_service",
        }:
            raise AssertionError(f"startup dialog imported runtime settings dependency: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    dialog = EmergencyActionDialog("Аварийный режим", "Сеть недоступна", [("Понятно", 1)])
    try:
        dialog.show()
        qt_app.processEvents()
        assert dialog.isVisible()
        assert "#2563eb" in dialog.styleSheet()
    finally:
        dialog.finish_with_code(QDialog.Rejected)
        dialog.deleteLater()
        qt_app.processEvents()


def test_password_verifier_exception_is_visible_and_does_not_close_dialog(qt_app):
    from PySide6.QtWidgets import QDialog
    from rem_card.ui.shared.emergency_dialogs import EmergencyPasswordDialog

    def failed_verifier(_value):
        raise OSError("settings snapshot unavailable")

    dialog = EmergencyPasswordDialog("Аварийный пароль", "Введите пароль", failed_verifier)
    try:
        dialog.show()
        dialog.password_edit.setText("123456")
        dialog.submit_password()
        qt_app.processEvents()
        assert dialog.isVisible()
        assert dialog.result() == 0
        assert dialog.error_label.isVisible()
        assert "Не удалось проверить" in dialog.error_label.text()
    finally:
        dialog.finish_with_code(QDialog.Rejected)
        dialog.deleteLater()
        qt_app.processEvents()


def test_database_warning_is_presented_once_until_explicit_success(
    isolated_database_failure_reporting,
    monkeypatch,
):
    db_availability = isolated_database_failure_reporting
    shown = []

    class _Signal:
        def emit(self, message):
            shown.append(message)
            assert db_availability._begin_warning_display()
            db_availability._finish_warning_display()

    monkeypatch.setattr(
        db_availability,
        "_get_qt_notifier",
        lambda: SimpleNamespace(show_requested=_Signal()),
    )
    logger = SimpleNamespace(error=lambda *_args, **_kwargs: None)

    for _ in range(3):
        db_availability.notify_database_unavailable(
            sqlite3.OperationalError("unable to open database file"),
            context="remcard_read_all",
            logger=logger,
        )
    assert len(shown) == 1

    db_availability.notify_direct_central_success()
    db_availability.notify_database_unavailable(
        sqlite3.OperationalError("unable to open database file"),
        context="remcard_read_all",
        logger=logger,
    )
    assert len(shown) == 2


def test_subscriber_claims_direct_failure_and_suppresses_low_level_dialog(
    isolated_database_failure_reporting,
    monkeypatch,
):
    db_availability = isolated_database_failure_reporting
    events = []
    fallback = []
    unsubscribe = db_availability.subscribe_direct_central_failure(
        lambda event: events.append(event) or True
    )
    monkeypatch.setattr(db_availability, "_show_warning_throttled", lambda: fallback.append(True))
    logger = SimpleNamespace(error=lambda *_args, **_kwargs: None)
    try:
        wrapped = db_availability.notify_database_unavailable(
            OSError("network path not found"),
            context="remcard_read_all",
            logger=logger,
        )
    finally:
        unsubscribe()

    assert isinstance(wrapped, db_availability.DatabaseUnavailableError)
    assert len(events) == 1
    assert events[0].context == "remcard_read_all"
    assert events[0].thread_id == threading.get_ident()
    assert fallback == []


def test_swallowed_patient_maintenance_failure_reaches_network_data_service(
    isolated_database_failure_reporting,
    monkeypatch,
):
    from rem_card.services.data_service import DataService
    from rem_card.services.patient_service import PatientService

    db_availability = isolated_database_failure_reporting
    handled = []
    data_service = SimpleNamespace(
        db=SimpleNamespace(runtime_context=SimpleNamespace(mode="network")),
        _shutting_down=False,
        _network_outage_detected=False,
        _handle_database_access_failure=lambda exc, *, source: handled.append((exc, source)),
    )
    data_service._uses_direct_central_runtime = MethodType(
        DataService._uses_direct_central_runtime,
        data_service,
    )
    data_service._handle_direct_central_failure = MethodType(
        DataService._handle_direct_central_failure,
        data_service,
    )
    unsubscribe = db_availability.subscribe_direct_central_failure(
        data_service._handle_direct_central_failure
    )
    monkeypatch.setattr(db_availability, "_show_warning_throttled", lambda: None)
    low_level_logger = SimpleNamespace(error=lambda *_args, **_kwargs: None)

    class _Dao:
        @staticmethod
        def release_due_outcome_beds(*, delay_minutes):
            _ = delay_minutes
            raise db_availability.notify_database_unavailable(
                OSError("network path not found"),
                context="remcard_read_all",
                logger=low_level_logger,
            )

    try:
        patient_service = PatientService(_Dao())
        assert patient_service._release_due_outcome_beds_impl() == 0
    finally:
        unsubscribe()

    assert len(handled) == 1
    assert isinstance(handled[0][0], db_availability.DatabaseUnavailableError)
    assert handled[0][1] == "direct_central:remcard_read_all"


def test_direct_failure_callback_ignores_non_network_runtime(isolated_database_failure_reporting):
    from rem_card.services.data_service import DataService

    event = isolated_database_failure_reporting.DirectCentralFailureEvent(
        cause=OSError("network path not found"),
        wrapped=isolated_database_failure_reporting.DatabaseUnavailableError("unavailable"),
        context="remcard_read_all",
        detected_monotonic=1.0,
        thread_id=threading.get_ident(),
    )
    harness = SimpleNamespace(
        db=SimpleNamespace(runtime_context=SimpleNamespace(mode="emergency")),
        _shutting_down=False,
        _network_outage_detected=False,
        _handle_database_access_failure=lambda *_args, **_kwargs: pytest.fail(
            "emergency runtime received central network event"
        ),
    )
    harness._uses_direct_central_runtime = MethodType(DataService._uses_direct_central_runtime, harness)
    assert DataService._handle_direct_central_failure(harness, event) is False


def test_direct_failure_of_another_local_database_cannot_trigger_network_outage(isolated_database_failure_reporting):
    from rem_card.services.data_service import DataService

    events = []
    harness = SimpleNamespace(
        db=SimpleNamespace(runtime_context=SimpleNamespace(mode="network"), db_path="C:/test/primary.db"),
        _shutting_down=False, _network_outage_detected=False,
        _handle_database_access_failure=lambda *a, **kw: events.append(kw),
    )
    harness._uses_direct_central_runtime = MethodType(DataService._uses_direct_central_runtime, harness)
    event_type = isolated_database_failure_reporting.DirectCentralFailureEvent
    for path, mode in (("C:/test/local.db", "emergency"), ("C:/test/other.db", "network")):
        event = event_type(OSError("unable to open database file"),
                           isolated_database_failure_reporting.DatabaseUnavailableError("unavailable"),
                           "remcard_read_all", 1.0, 1, path, mode)
        assert DataService._handle_direct_central_failure(harness, event) is False
    assert not events


class _SignalRecorder:
    def __init__(self):
        self.values = []

    def emit(self, *values):
        self.values.append(values)


class _PauseQueue:
    def __init__(self, idle=True):
        self.idle = idle

    def is_idle(self):
        return self.idle

    def pending_count(self):
        return 0 if self.idle else 1

    def active_count(self):
        return 0


class _PauseMonitor:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.pause_calls = []

    def is_enabled(self):
        return self.enabled

    def pause_and_wait(self, timeout_sec):
        self.pause_calls.append(timeout_sec)
        self.enabled = False
        return True

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)


def _pause_harness(*, runtime_mode="emergency", queue_idle=True):
    from rem_card.services.data_service import DataService

    harness = SimpleNamespace(
        db=SimpleNamespace(runtime_context=SimpleNamespace(mode=runtime_mode)),
        _shutting_down=False,
        _monitor_enabled=True,
        _monitor=_PauseMonitor(),
        _queue=_PauseQueue(idle=queue_idle),
        _poll_maintenance_tasks=[],
        _emergency_pause_lock=threading.RLock(),
        _emergency_pause_state=None,
        _poll_maintenance_active=0,
        _emergency_restore_probe_scheduler=object(),
        write_failed=_SignalRecorder(),
        _error_callback_requested=_SignalRecorder(),
    )
    for name in (
        "_is_emergency_runtime",
        "is_write_queue_idle",
        "pause_emergency_work",
        "resume_emergency_work",
        "_reject_write_if_emergency_paused",
        "run_poll_maintenance_tasks",
        "_emergency_write_submission_scope",
    ):
        setattr(harness, name, MethodType(getattr(DataService, name), harness))
    return harness


def test_emergency_pause_is_reversible_and_preserves_restore_probe():
    harness = _pause_harness()
    restore_probe = harness._emergency_restore_probe_scheduler
    maintenance_calls = []
    harness._poll_maintenance_tasks.append(lambda: maintenance_calls.append(True))

    paused = harness.pause_emergency_work(timeout_sec=0.2)
    assert paused["ok"] is True
    assert paused["queue_idle"] is True
    assert harness._monitor.enabled is False
    assert harness._reject_write_if_emergency_paused("background-clinical-write") is True
    harness.run_poll_maintenance_tasks()
    assert maintenance_calls == []
    assert harness._emergency_restore_probe_scheduler is restore_probe

    # EmergencyWorkflowController retains and passes the complete pause result.
    resumed = harness.resume_emergency_work(paused)
    assert resumed == {
        "ok": True,
        "reason": "resumed",
        "monitor_resumed": True,
        "maintenance_resumed": True,
    }
    assert harness._monitor.enabled is True
    harness.run_poll_maintenance_tasks()
    assert maintenance_calls == [True]


def test_emergency_pause_timeout_rolls_back_gate():
    harness = _pause_harness(queue_idle=False)
    result = harness.pause_emergency_work(timeout_sec=0)
    assert result["ok"] is False
    assert result["reason"] == "write_queue_not_drained"
    assert harness._emergency_pause_state is None
    assert harness._monitor.enabled is True


def test_emergency_pause_rejects_non_emergency_runtime():
    harness = _pause_harness(runtime_mode="network")
    assert harness.pause_emergency_work()["reason"] == "runtime_not_emergency"


def test_pause_waits_for_a_write_that_passed_the_gate_before_queue_submission():
    from rem_card.services.data_service import DataService

    harness = _pause_harness()
    reached, release = threading.Event(), threading.Event()
    harness._network_outage_detected = False
    harness._record_operblock_write_intent = lambda description: (reached.set(), release.wait(2), None)[-1]
    harness._opblock_interactive_write_metadata = lambda *args: {}
    submitted = []
    harness._queue.submit = lambda **kwargs: submitted.append(kwargs)
    results = []
    thread = threading.Thread(target=lambda: results.append(DataService.enqueue_write(harness, "accepted write", lambda: None)))
    thread.start()
    try:
        assert reached.wait(1)
        paused = harness.pause_emergency_work(timeout_sec=0.02)
        assert paused["ok"] is False
        assert harness._emergency_pause_state is None
        assert not submitted
    finally:
        release.set()
        thread.join(2)
    assert results == [True] and len(submitted) == 1
    assert harness.pause_emergency_work(timeout_sec=0.02)["ok"] is True


def test_overlapping_pause_does_not_claim_an_unfinished_drain_is_ready():
    harness = _pause_harness(queue_idle=False)
    entered = threading.Event()
    harness._monitor.pause_and_wait = lambda timeout: entered.set() or True
    first = threading.Thread(target=lambda: harness.pause_emergency_work(timeout_sec=0.1))
    first.start()
    assert entered.wait(1)
    second = harness.pause_emergency_work(timeout_sec=0.02)
    first.join(1)
    assert second["ok"] is False and second["reason"] == "pause_in_progress"
