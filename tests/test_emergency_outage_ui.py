from __future__ import annotations

import builtins
import os
import sqlite3
import sys
import threading
import time
import types
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.mark.parametrize("role", ("doctor", "nurse"))
def test_source_emergency_restart_falls_back_to_unified_entrypoint(monkeypatch, tmp_path, role):
    from rem_card.app import runtime_outage

    marker = tmp_path / "emergency-startup-request.json"
    missing_script = tmp_path / "missing-entrypoint.py"
    monkeypatch.setattr(runtime_outage.sys, "frozen", False, raising=False)
    monkeypatch.setattr(runtime_outage.sys, "argv", [str(missing_script)])
    monkeypatch.setattr(runtime_outage.sys, "executable", str(tmp_path / "python.exe"))

    command = runtime_outage._runtime_restart_args(str(marker), role=role)

    assert command == [
        str(tmp_path / "python.exe"),
        str(Path(runtime_outage.__file__).resolve().parents[1] / "run_remcard.py"),
        "--role",
        role,
        "--emergency-startup-request",
        str(marker.resolve()),
    ]


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


def test_runtime_outage_quiesces_monitor_and_repeated_maintenance_without_waiting():
    from rem_card.services.data_service import DataService

    harness = _pause_harness(runtime_mode="network")
    harness._network_outage_detected = False
    harness._network_outage_info = {}
    scheduler_stop_timeouts = []
    harness._emergency_standby_scheduler = SimpleNamespace(
        stop=lambda *, timeout: scheduler_stop_timeouts.append(timeout) or True,
    )
    harness._emergency_restore_probe_scheduler = None
    maintenance_calls = []
    harness._poll_maintenance_tasks.append(lambda: maintenance_calls.append(True))
    harness.block_new_writes_for_runtime_outage = MethodType(
        DataService.block_new_writes_for_runtime_outage,
        harness,
    )
    harness.quiesce_runtime_outage_background_work = MethodType(
        DataService.quiesce_runtime_outage_background_work,
        harness,
    )
    harness.set_change_monitor_enabled = MethodType(
        DataService.set_change_monitor_enabled,
        harness,
    )

    started = time.monotonic()
    harness.block_new_writes_for_runtime_outage({"category": "network_unavailable"})
    elapsed = time.monotonic() - started

    for _ in range(5):
        harness.run_poll_maintenance_tasks()
    harness.set_change_monitor_enabled(True)

    assert elapsed < 0.1
    assert harness._monitor.enabled is False
    assert scheduler_stop_timeouts == [0.0]
    assert maintenance_calls == []
    assert harness._network_outage_info == {"category": "network_unavailable"}


@pytest.fixture
def outage_window(qt_app, monkeypatch):
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QMainWindow, QWidget
    from rem_card.app import local_metrics, runtime_outage
    from rem_card.services.data_service import DataService
    from rem_card.services.settings import settings_service
    from rem_card.ui.main_window import MainWindow
    from rem_card.ui.shared.custom_message_box import CustomMessageBox

    events = []
    monitor_release = threading.Event()
    queue_release = threading.Event()

    def wait_for_release(name, release, timeout, result=True):
        events.append((name, timeout, threading.get_ident()))
        assert release.wait(3)
        return result

    class Service(QObject):
        network_outage_detected = Signal(dict)
        prepare_runtime_outage_shutdown = DataService.prepare_runtime_outage_shutdown
        stop_data_update_monitor = DataService.stop_data_update_monitor
        queue_settles = True

        def quiesce_runtime_outage_background_work(self, _payload):
            events.append("quiesce")

        def set_shutting_down(self):
            pass

        def _wait_for_write_submissions(self, _timeout):
            return True

        def _stop_emergency_schedulers(self, *, timeout):
            return True

        def unsettled_writes(self):
            return []

        def get_write_queue_state(self):
            events.append("queue_state")
            return {
                "queue_shutdown_result": self._last_runtime_outage_shutdown_result,
                "queue_settled": self._last_runtime_outage_queue_settled,
                "pending_count": 0 if self.queue_settles else 2,
                "unconfirmed_write_count": 0 if self.queue_settles else 1,
            }

        def get_observed_change_state(self):
            return {"change_id": 19}

    service = Service()
    service._monitor = SimpleNamespace(
        isRunning=lambda: True,
        stop=lambda: None,
        wait=lambda milliseconds: wait_for_release("monitor_wait", monitor_release, milliseconds),
    )
    service._queue = SimpleNamespace(
        shutdown=lambda timeout: wait_for_release("queue_wait", queue_release, timeout, service.queue_settles),
    )
    service._emergency_standby_scheduler = SimpleNamespace(manager=SimpleNamespace(store=SimpleNamespace(
        get_latest_valid_standby=lambda: SimpleNamespace(remote_last_change_id=12),
    )))

    class Window(MainWindow):
        def __init__(self):
            QMainWindow.__init__(self)
            self.container = SimpleNamespace(data_service=service)
            self.stack = QWidget(self)
            self._is_closing = False
            self._runtime_outage_handling = False
            self._runtime_outage_worker = None
            self._runtime_outage_context = None
            self._runtime_outage_close_requested = False
            self._connect_runtime_outage_signal()

        def close(self):
            events.append("close")
            self._is_closing = True

        def _prepare_runtime_outage_emergency_session(self, _marker_payload):
            events.append("prepare_emergency")
            return True

        def _launch_runtime_emergency_restart(self, marker_path):
            events.append(("restart", marker_path))
            return True

    def write_marker(**payload):
        events.append(("marker", payload))
        return "marker.json", payload

    monkeypatch.setattr(local_metrics, "flush_metrics", lambda **kwargs: None)
    monkeypatch.setattr(runtime_outage, "write_runtime_outage_startup_request", write_marker)
    monkeypatch.setattr(settings_service, "reset_settings_service", lambda: events.append("reset_settings"))
    monkeypatch.setattr(CustomMessageBox, "warning", lambda *_args: events.append("warning"))
    monkeypatch.setattr(CustomMessageBox, "warning_with_actions", lambda *_args: events.append("dialog") or 1)
    window = Window()
    try:
        yield window, service, events, monitor_release, queue_release
    finally:
        monitor_release.set()
        queue_release.set()
        if window._runtime_outage_worker is not None:
            assert window._runtime_outage_worker.wait(2000)
        qt_app.processEvents()


def _wait_for_outage_ui(qt_app, predicate):
    deadline = time.monotonic() + 2
    while not predicate() and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.001)
    assert predicate()


@pytest.mark.parametrize("role, queue_settles", [("nurse", True), ("nurse", False), ("doctor", True)])
def test_runtime_outage_drains_off_gui_and_uses_final_queue_state(qt_app, outage_window, role, queue_settles):
    from PySide6.QtCore import QTimer

    window, service, events, monitor_release, queue_release = outage_window
    service.queue_settles = queue_settles
    service.network_outage_detected.emit({"role": role})
    _wait_for_outage_ui(qt_app, lambda: len(events) == 2)
    gui_thread = threading.get_ident()
    assert events[0] == "quiesce"
    assert events[1][:2] == ("monitor_wait", 5000)
    assert events[1][2] != gui_thread
    assert not window.stack.isEnabled()
    retained_worker = window._runtime_outage_worker
    assert retained_worker.isRunning()

    service.network_outage_detected.emit({"role": role})
    QTimer.singleShot(0, lambda: events.append("gui_during_monitor_wait"))
    _wait_for_outage_ui(qt_app, lambda: "gui_during_monitor_wait" in events)
    monitor_release.set()
    _wait_for_outage_ui(qt_app, lambda: len(events) == 4)
    assert events[3] == ("queue_wait", 5.0, events[1][2])
    QTimer.singleShot(0, lambda: events.append("gui_during_queue_wait"))
    _wait_for_outage_ui(qt_app, lambda: "gui_during_queue_wait" in events)
    assert window._runtime_outage_worker is retained_worker

    queue_release.set()
    _wait_for_outage_ui(qt_app, lambda: events[-1] == "close")
    assert window._runtime_outage_worker is None
    if role == "doctor":
        assert events[5:] == ["warning", "close"]
        return

    assert events[5] == "queue_state"
    marker_label, marker_payload = events[6]
    assert marker_label == "marker"
    assert marker_payload == {
        "source_role": "nurse",
        "last_observed_remote_change_id": 19,
        "standby_last_change_id": 12,
        "unconfirmed_writes": not queue_settles,
        "pending_write_count": 0 if queue_settles else 2,
        "unconfirmed_write_count": 0 if queue_settles else 1,
        "unknown_active_write": not queue_settles,
        "queue_shutdown_result": "settled" if queue_settles else "timeout",
        "queue_settled": queue_settles,
    }
    assert events[7:] == ["dialog", "reset_settings", "prepare_emergency", ("restart", "marker.json"), "close"]


@pytest.mark.parametrize("change", ["close", "leaving_role", "replaced_container"])
def test_runtime_outage_lifecycle_ignores_late_transition(qt_app, outage_window, monkeypatch, change):
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QCloseEvent
    from rem_card.ui.main_window import MainWindow
    from rem_card.ui.unified_window import UnifiedWindow

    window, service, events, monitor_release, queue_release = outage_window
    service.network_outage_detected.emit({"role": "nurse"})
    _wait_for_outage_ui(qt_app, lambda: len(events) == 2)
    if change == "close":
        close_event = QCloseEvent()
        MainWindow.closeEvent(window, close_event)
        assert not close_event.isAccepted()
        assert window._runtime_outage_close_requested
        window.show_roles()
    elif change == "leaving_role":
        window._is_closing = True
        shell = SimpleNamespace(
            _shutdown=object(), _transition=SimpleNamespace(running=False), _workers=set(),
            _wait_before_drain=lambda: None,
            _role_threads_running=lambda: pytest.fail("Role drain began while the outage worker was still running"),
        )
        scheduled = []
        with monkeypatch.context() as patch:
            patch.setattr(QTimer, "singleShot", lambda delay, callback: scheduled.append((delay, callback)))
            UnifiedWindow._wait_before_drain(shell)
        assert scheduled == [(100, shell._wait_before_drain)]
    else:
        window.container = SimpleNamespace(data_service=None)
    monitor_release.set()
    queue_release.set()
    _wait_for_outage_ui(qt_app, lambda: window._runtime_outage_worker is None)
    assert events[3] == "queue_state"
    if change == "close":
        assert events[4][0] == "marker"
        assert events[5:] == ["close"]
    else:
        assert events[4:] == []


@pytest.mark.parametrize("exit_app", [True, False])
def test_unified_exit_waits_for_outage_marker_before_teardown(qt_app, outage_window, monkeypatch, exit_app):
    from rem_card.ui.unified_window import UnifiedWindow

    window, service, events, monitor_release, queue_release = outage_window
    old_unified = qt_app.property("unified_entry")
    shell = UnifiedWindow()
    shell.container, shell.role_window, shell.role = window.container, window, "nurse"
    service.queue_settles = False
    monkeypatch.setattr(shell, "_save_geometry", lambda _role: None)
    monkeypatch.setattr(shell, "_wait_before_drain", lambda: events.append("shell_teardown"))
    window.doctor_main = window.nurse_main = window.operblock_main = None
    window.iter_runtime_containers = lambda: [window.container]
    gui_thread = threading.get_ident()

    def record_gui_shutdown():
        if threading.get_ident() == gui_thread:
            events.append("shutdown")

    service.set_shutting_down = record_gui_shutdown
    try:
        service.network_outage_detected.emit({"role": "nurse"})
        _wait_for_outage_ui(qt_app, lambda: len(events) == 2)
        if exit_app:
            shell.request_application_exit(confirmed=True)
        else:
            shell.request_role_exit(force=True)
        assert window._runtime_outage_close_requested
        assert not shell._leaving and not window._is_closing
        assert "shutdown" not in events

        monitor_release.set()
        queue_release.set()
        _wait_for_outage_ui(qt_app, lambda: "shell_teardown" in events)
        marker = next(item[1] for item in events if isinstance(item, tuple) and item[0] == "marker")
        assert marker["queue_shutdown_result"] == "timeout"
        assert marker["unconfirmed_writes"] and marker["last_observed_remote_change_id"] == 19
        assert events[-3:] == [("marker", marker), "shutdown", "shell_teardown"]
        assert shell._pending_exit and shell._central_unavailable
        assert "dialog" not in events and "prepare_emergency" not in events
    finally:
        shell.container = shell.role_window = None
        shell._status_timer.stop()
        shell.deleteLater()
        qt_app.processEvents()
        qt_app.setProperty("unified_entry", old_unified)


def test_runtime_outage_queue_snapshot_failure_closes_without_restart(qt_app, outage_window, monkeypatch):
    window, service, events, monitor_release, queue_release = outage_window

    def failed_snapshot():
        raise RuntimeError("queue snapshot unavailable")

    monkeypatch.setattr(service, "get_write_queue_state", failed_snapshot)
    monitor_release.set()
    queue_release.set()
    service.network_outage_detected.emit({"role": "nurse"})
    _wait_for_outage_ui(qt_app, lambda: bool(events) and events[-1] == "close")
    assert events[3:] == ["warning", "close"]
