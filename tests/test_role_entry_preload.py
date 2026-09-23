"""Preloading prepares empty UI without opening a patient/session."""
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget

from rem_card.ui.shared.display_settings_storage import DisplaySettingsStorage
from rem_card.ui.shared.role_entry_preload import RoleEntryPreload
from rem_card.ui.shared.staged_card_prewarm import StagedUiPrewarm


def _process_until(app, predicate, timeout_sec=1.0):
    deadline = time.monotonic() + timeout_sec
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.001)
    assert predicate()


def test_staged_card_prewarm_yields_to_qt_heartbeat_between_steps():
    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    owner._is_closing = False
    events = []
    prewarmer = None

    def heartbeat():
        events.append("heartbeat")
        if prewarmer is not None and not prewarmer.done:
            QTimer.singleShot(1, owner, heartbeat)

    steps = [(f"step_{index}", lambda index=index: events.append(f"step_{index}")) for index in range(4)]
    prewarmer = StagedUiPrewarm(
        owner,
        role="doctor",
        steps=steps,
        stagger_ms=5,
        metric_recorder=lambda *args, **kwargs: None,
    )
    try:
        QTimer.singleShot(0, owner, heartbeat)
        assert prewarmer.start()
        _process_until(app, lambda: prewarmer.done)
        positions = [events.index(f"step_{index}") for index in range(4)]
        assert positions == sorted(positions)
        for left, right in zip(positions, positions[1:]):
            assert "heartbeat" in events[left + 1:right]
    finally:
        owner.deleteLater()
        app.processEvents()


def test_staged_card_prewarm_discards_queued_steps_on_role_shutdown():
    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    owner._is_closing = False
    calls = []
    prewarmer = StagedUiPrewarm(
        owner,
        role="nurse",
        steps=[(f"step_{index}", lambda index=index: calls.append(index)) for index in range(3)],
        stagger_ms=30,
        metric_recorder=lambda *args, **kwargs: None,
    )
    try:
        assert prewarmer.start()
        _process_until(app, lambda: prewarmer.completed_steps == 1)
        owner._is_closing = True
        assert prewarmer.cancel(reason="role_shutdown")
        deadline = time.monotonic() + 0.08
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.001)
        assert calls == [0]
        assert prewarmer.cancelled and not prewarmer.done
    finally:
        owner.deleteLater()
        app.processEvents()


def test_patient_open_finishes_remaining_card_prewarm_once():
    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    owner._is_closing = False
    calls = []
    prewarmer = StagedUiPrewarm(
        owner,
        role="doctor",
        steps=[(f"step_{index}", lambda index=index: calls.append(index)) for index in range(4)],
        stagger_ms=80,
        metric_recorder=lambda *args, **kwargs: None,
    )
    try:
        assert prewarmer.start()
        _process_until(app, lambda: prewarmer.completed_steps == 1)
        assert prewarmer.finish_now(reason="patient_open")
        assert calls == [0, 1, 2, 3]
        assert prewarmer.done
        deadline = time.monotonic() + 0.12
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.001)
        assert calls == [0, 1, 2, 3]
    finally:
        owner.deleteLater()
        app.processEvents()


def test_finish_now_stops_if_a_step_cancels_the_prewarm():
    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    owner._is_closing = False
    calls = []
    prewarmer = None

    def cancel_step():
        calls.append("cancel")
        prewarmer.cancel(reason="context_switch")

    prewarmer = StagedUiPrewarm(
        owner,
        role="doctor",
        steps=[("cancel", cancel_step), ("late", lambda: calls.append("late"))],
        stagger_ms=5,
        metric_recorder=lambda *args, **kwargs: None,
    )
    try:
        assert not prewarmer.finish_now(reason="patient_open")
        assert calls == ["cancel"]
        assert prewarmer.cancelled and not prewarmer.done
    finally:
        owner.deleteLater()
        app.processEvents()


def _build_role_card_widget(role):
    from rem_card.ui.doctor_view.doctor_remcard_widget import DoctorRemCardWidget
    from rem_card.ui.nurse_view.nurse_main_widget import NurseMainWidget

    patient_service = MagicMock()
    remcard_service = MagicMock()
    remcard_service.status_service = MagicMock()
    remcard_service.fluid_service = MagicMock()
    remcard_service.data_service = None
    shift_start = datetime(2026, 9, 23, 8, 0)
    remcard_service.get_day_period.return_value = (shift_start, shift_start + timedelta(days=1))
    remcard_service.normalize_time.side_effect = lambda value, fallback: value or fallback
    remcard_service.is_time_input_valid.return_value = True
    remcard_service.display_hint.side_effect = lambda value, _date: {"label": value, "text": ""}
    return (
        DoctorRemCardWidget(remcard_service, None, patient_service)
        if role == "doctor"
        else NurseMainWidget(patient_service, remcard_service)
    )


@pytest.mark.parametrize("role", ["doctor", "nurse"])
def test_patient_open_during_partial_prewarm_builds_complete_role_card(role):
    app = QApplication.instance() or QApplication([])
    widget = _build_role_card_widget(role)
    try:
        assert not widget.has_full_layout()
        prewarmer = widget._ensure_card_ui_prewarmer()
        assert prewarmer.start()
        _process_until(app, lambda: prewarmer.completed_steps == 2, timeout_sec=2.0)
        assert not widget.has_full_layout()
        assert widget._ensure_full_layout(reason="patient_open")
        assert widget.has_full_layout()
        assert widget._card_ui_prewarmer.done
        assert hasattr(widget, "vitals_input")
        assert getattr(widget.layout_manager, "orders_widget", None) is not None
        assert widget.layout_manager.orders_widget.main_layout is not None
        assert getattr(widget.layout_manager, "nurse_orders_manager", None) is not None
    finally:
        widget.shutdown()
        widget.deleteLater()
        app.processEvents()


def test_real_doctor_card_prewarm_keeps_qt_heartbeat_between_creation_groups():
    app = QApplication.instance() or QApplication([])
    widget = _build_role_card_widget("doctor")
    prewarmer = widget._ensure_card_ui_prewarmer()
    heartbeat_count = [0]
    counts_before_steps = []

    def heartbeat():
        heartbeat_count[0] += 1
        if not prewarmer.done and not prewarmer.cancelled and not prewarmer.failed:
            QTimer.singleShot(1, widget, heartbeat)

    wrapped_steps = []
    for name, callback in prewarmer._steps:
        def run(callback=callback):
            counts_before_steps.append(heartbeat_count[0])
            callback()
        wrapped_steps.append((name, run))
    prewarmer._steps = wrapped_steps
    try:
        QTimer.singleShot(0, widget, heartbeat)
        assert prewarmer.start()
        _process_until(app, lambda: prewarmer.done, timeout_sec=5.0)
        assert len(counts_before_steps) == prewarmer.total_steps
        assert all(after > before for before, after in zip(counts_before_steps, counts_before_steps[1:]))
        assert widget.has_full_layout()
    finally:
        widget.shutdown()
        widget.deleteLater()
        app.processEvents()


def test_role_shutdown_stops_real_partial_card_prewarm():
    app = QApplication.instance() or QApplication([])
    widget = _build_role_card_widget("nurse")
    prewarmer = widget._ensure_card_ui_prewarmer()
    calls = []
    prewarmer._steps = [
        (name, lambda callback=callback, name=name: (calls.append(name), callback())[1])
        for name, callback in prewarmer._steps
    ]
    try:
        assert prewarmer.start()
        _process_until(app, lambda: prewarmer.completed_steps == 1, timeout_sec=2.0)
        widget.shutdown()
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.001)
        assert len(calls) == 1
        assert prewarmer.cancelled and not widget.has_full_layout()
    finally:
        widget.deleteLater()
        app.processEvents()


def test_shared_screen_is_empty_until_consumed_by_a_session(monkeypatch):
    from rem_card.ui.patient_bed_management import management_widget as module
    from shiboken6 import delete
    app = QApplication.instance() or QApplication([])
    owner, destination = QWidget(), QWidget()
    pool = RoleEntryPreload(owner)
    monkeypatch.setattr(app, '_role_entry_preload', pool, raising=False)
    services, refreshes = [], []

    def service(db, data_service=None):
        instance = SimpleNamespace(db=db, data_service=data_service)
        services.append(instance)
        return instance

    monkeypatch.setattr(module, 'PatientBedManagementService', service)
    monkeypatch.setattr(module.PatientBedManagementWidget, 'refresh_bed_statuses',
                        lambda widget: refreshes.append(widget.patient_bed_service))
    try:
        pool.prepare_shared()
        prepared = pool.patient_management
        pool.prepare_shared()
        app.processEvents()
        assert pool.patient_management is prepared
        assert prepared.patient_bed_service is None
        assert not services and not refreshes
        assert prepared._beds_snapshot_by_bed == {}
        assert prepared.isHidden()
        db, data = object(), object()
        adopted = module.PatientBedManagementWidget.create(db, data, destination)
        assert adopted is prepared and adopted.parent() is destination
        assert not adopted.isHidden()
        app.processEvents()
        assert len(services) == 1 and refreshes == services
        assert services[0].db is db and services[0].data_service is data
        # The prepared instance is consumed once; another role gets fresh UI/services.
        other_db = object()
        other = module.PatientBedManagementWidget.create(other_db, parent=destination)
        assert other is not prepared
        app.processEvents()
        assert services[-1].db is other_db and len(refreshes) == 2
    finally:
        delete(owner)
        delete(destination)


@pytest.mark.parametrize('role', ['doctor', 'nurse'])
def test_empty_preload_and_one_time_adoption(monkeypatch, role):
    app = QApplication.instance() or QApplication([])
    assert app is not None
    owner, destination = QWidget(), QWidget()
    pool = RoleEntryPreload(owner)
    def forbidden(*args, **kwargs):
        pytest.fail('Preloading must not read shared display settings')
    monkeypatch.setattr(DisplaySettingsStorage, 'load', forbidden)
    try:
        pool.prepare(role)
        shell, panel = pool.items[role]
        assert shell.patient_service is None
        assert shell.remcard_service is None
        assert shell.sector_w1a.service is None
        assert not any(timer.isActive() for timer in shell.findChildren(QTimer))
        assert panel._reports_count_worker is None
        assert shell.isHidden()
        workspace = pool.workspaces[role]
        assert workspace.count() == 0
        assert pool.take_workspace(role) is workspace
        assert pool.take_workspace(role) is None
        pool.prepare(role)
        assert pool.items[role][0] is shell
        pool.prepare('operblock_planned')
        assert set(pool.items) == {role}
        monkeypatch.setattr(DisplaySettingsStorage, 'load', lambda self: {})
        monkeypatch.setattr(panel, 'refresh_user_reports_count', lambda: None)
        patient, card, oper = object(), SimpleNamespace(status_service=object()), object()
        kwargs = dict(patient_service=patient, remcard_service=card,
                      operblock_service=oper, parent=destination)
        assert pool.take(role, **kwargs) == (shell, panel)
        assert shell.parent() is destination
        assert shell.beds_selection_widget.patient_service is patient
        assert shell.sector_w1a.service is card
        assert panel._reports_count_timer.isActive()
        assert pool.take(role, **kwargs) is None
    finally:
        owner.deleteLater()
        destination.deleteLater()


def test_entry_waits_for_data_and_discards_old_session(monkeypatch):
    from rem_card.ui import unified_window as module
    from rem_card.app import main
    callbacks, events = [], []
    ready = {'ready': False}
    monkeypatch.setattr(main, '_initial_w1_state', lambda page: ready)
    monkeypatch.setattr(module, 'QTimer', SimpleNamespace(singleShot=lambda delay, owner, callback: callbacks.append(callback)))
    page = SimpleNamespace(wake_initial_role_monitor=lambda: None)
    owner = SimpleNamespace(
        _closing=False, _leaving=False, role_window=page, session_id='current', role='doctor',
        _entry_cancel=None,
        welcome=SimpleNamespace(set_preparing=lambda *args: events.append('loading')),
        entry_chrome=SimpleNamespace(set_role_mode=lambda value: events.append('chrome')),
        stack=SimpleNamespace(indexOf=lambda page: -1, addWidget=lambda page: events.append('add'),
                              setCurrentWidget=lambda page: events.append('show')),
        _role_transition_ready=lambda: events.append('ready'),
        _admitted_failed=lambda error: events.append('failed'))
    owner._finish_role_entry = lambda *args: module.UnifiedWindow._finish_role_entry(owner, *args)
    owner._finish_role_entry(page, 'current', module.time.monotonic() + 10)
    assert events == ['loading'] and len(callbacks) == 1
    owner.session_id = 'other'
    callbacks.pop()()
    assert events == ['loading']  # late result cannot reopen the old role
    owner.session_id = 'current'
    ready['ready'] = True
    owner._finish_role_entry(page, 'current', module.time.monotonic() + 10)
    assert events[-4:] == ['chrome', 'add', 'show', 'ready']
    ready['ready'] = False
    owner._finish_role_entry(page, 'current', module.time.monotonic() - 1)
    assert events[-1] == 'failed'
def test_failed_layout_transfer_disposes_prepared_widgets():
    import pytest
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
    from shiboken6 import isValid
    from rem_card.ui.shared.staged_card_prewarm import StagedCardSectors
    app = QApplication.instance() or QApplication([])
    preparation = StagedCardSectors("doctor")
    preparation.steps()[0][1]()
    widgets = list(preparation._created)
    assert widgets
    def fail(sectors):
        assert sectors
        raise RuntimeError("synthetic layout failure")
    with pytest.raises(RuntimeError, match="synthetic layout failure"):
        preparation.build_layout(fail)
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
    assert all(not isValid(widget) for widget in widgets)
