"""Preloading prepares empty UI without opening a patient/session."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget

from rem_card.ui.shared.display_settings_storage import DisplaySettingsStorage
from rem_card.ui.shared.role_entry_preload import RoleEntryPreload


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
