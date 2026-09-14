import time
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from rem_card.app.unified_runtime import SessionShutdown
from rem_card.ui.unified_window import UnifiedWindow


@pytest.fixture
def shell(tmp_path):
    app = QApplication.instance() or QApplication([])
    old_unified = app.property('unified_entry')
    window = UnifiedWindow()
    window.store = SimpleNamespace(control_dir=tmp_path / 'control')
    yield window
    window.container = None
    window.role_window = None
    window._busy = window._leaving = False
    window._workers.clear()
    window._exit_update_checked = True
    window._candidate = None
    window.close()
    window.deleteLater()
    app.processEvents()
    app.setProperty('unified_entry', old_unified)


def state(mode, generation, operation=None):
    return dict(state=mode, generation=generation, operation_id=operation, owner_token='token')


def test_confirmed_application_exit_does_not_ask_to_return_to_roles(shell, monkeypatch):
    from rem_card.ui.shared.custom_message_box import CustomMessageBox
    monkeypatch.setattr(CustomMessageBox, 'question', lambda *a, **k: pytest.fail('second confirmation'))
    shell.container = SimpleNamespace()
    calls = []
    monkeypatch.setattr(shell, 'request_role_exit', lambda force=False: calls.append(force))
    shell.request_application_exit(confirmed=True)
    assert shell._pending_exit
    assert calls == [True]


def test_application_close_uses_exit_confirmation_not_role_confirmation(shell, monkeypatch):
    from rem_card.ui.shared.custom_message_box import CustomMessageBox
    from PySide6.QtGui import QCloseEvent
    messages = []
    def confirm(parent, title, message):
        messages.append(message)
        return QMessageBox.Yes
    monkeypatch.setattr(CustomMessageBox, 'question', confirm)
    shell.container = SimpleNamespace()
    calls = []
    monkeypatch.setattr(shell, 'request_role_exit', lambda force=False: calls.append(force))
    event = QCloseEvent()
    shell.closeEvent(event)
    assert not event.isAccepted()
    assert messages == ['Выйти из программы?']
    assert calls == [True]


def test_resize_cursor_clears_when_pointer_enters_child(shell):
    from PySide6.QtCore import QEvent, Qt
    shell.entry_chrome.setCursor(Qt.SizeFDiagCursor)
    QApplication.sendEvent(shell.welcome, QEvent(QEvent.Enter))
    assert shell.entry_chrome.cursor().shape() == Qt.ArrowCursor


def test_inner_content_is_clipped_and_role_mode_restores_entry_header(shell):
    from PySide6.QtCore import QPoint
    shell.showNormal()
    QApplication.processEvents()
    chrome = shell.entry_chrome
    assert not chrome.content.mask().contains(QPoint(0, 0))
    assert chrome.content.mask().contains(chrome.content.rect().center())
    chrome.set_role_mode(True)
    assert chrome.title_bar.isHidden()
    chrome.set_role_mode(False)
    assert not chrome.title_bar.isHidden()
    assert not chrome.content.mask().contains(QPoint(0, 0))


def test_transition_passes_intermediate_rectangles_and_returns_to_saved_size(shell, monkeypatch):
    from PySide6.QtCore import QRect, QEventLoop, QTimer
    display = SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 1920, 1040))
    monkeypatch.setattr(shell, 'screen', lambda: display)
    monkeypatch.setattr(QApplication, 'screenAt', lambda point: display)
    shell.showNormal()
    shell.setGeometry(100, 100, 1000, 650)
    QApplication.processEvents()
    original = QRect(shell.geometry())
    shell._save_geometry('animation_origin')
    screen = shell.screen().availableGeometry()
    target = QRect(screen.left(), screen.top(), min(1250, screen.width()), min(800, screen.height()))
    shell.settings.setValue('animation_destination/normal_rect', target)
    shell.settings.setValue('animation_destination/maximized', False)
    observed = []
    loop = QEventLoop()
    sampler = QTimer()
    sampler.setInterval(15)
    sampler.timeout.connect(lambda: observed.append(QRect(shell.geometry())))
    sampler.start()
    shell._animate_page(shell.loading, 'animation_destination', False, loop.quit)
    QTimer.singleShot(3000, loop.quit)
    loop.exec()
    sampler.stop()
    assert not shell._transition.running
    assert shell.geometry() == target
    assert any(rect != original and rect != target for rect in observed)
    shell._animate_page(shell.welcome, 'animation_origin', False, loop.quit)
    QTimer.singleShot(3000, loop.quit)
    loop.exec()
    assert shell.geometry() == original
    assert shell._transition.cover is None


def test_maximized_transition_keeps_work_area_and_normal_restore_rectangle(shell, monkeypatch):
    from PySide6.QtCore import QRect, QEventLoop, QTimer
    display = SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 1920, 1040))
    monkeypatch.setattr(shell, 'screen', lambda: display)
    monkeypatch.setattr(QApplication, 'screenAt', lambda point: display)
    shell.showNormal()
    shell.setGeometry(80, 80, 1000, 650)
    normal = QRect(shell.geometry())
    shell.settings.setValue('animation_max/normal_rect', normal)
    shell.settings.setValue('animation_max/maximized', True)
    loop = QEventLoop()
    shell._animate_page(shell.loading, 'animation_max', False, loop.quit)
    QTimer.singleShot(3000, loop.quit)
    loop.exec()
    assert shell.geometry() == shell.screen().availableGeometry()
    assert shell._is_custom_maximized
    assert shell._custom_normal_geometry == normal
    assert not shell._transition.running


def test_local_only_never_schedules_central_access_or_update_probe(shell, monkeypatch):
    calls = []
    shell._local_only = True
    monkeypatch.setattr(shell, '_async', lambda *args: calls.append(args))
    shell.refresh_access()
    shell._check_updates()
    shell.update_application()
    assert calls == []
    assert not shell._update_requested


def test_local_role_restores_central_path_and_sync_environment(shell, monkeypatch):
    import os
    monkeypatch.setenv('REMCARD_BAZA_DIR', 'central-original')
    monkeypatch.delenv('REMCARD_UNIFIED_LOCAL_ONLY', raising=False)
    monkeypatch.setenv('REMCARD_LOCAL_OUTBOX_SYNC', '1')
    shell.role = 'operblock_planned'
    shell._configure_role_environment()
    os.environ['REMCARD_BAZA_DIR'] = 'local-emergency'
    os.environ['REMCARD_UNIFIED_LOCAL_ONLY'] = '1'
    shell._restore_role_environment()
    assert os.environ['REMCARD_BAZA_DIR'] == 'central-original'
    assert 'REMCARD_UNIFIED_LOCAL_ONLY' not in os.environ
    assert os.environ['REMCARD_LOCAL_OUTBOX_SYNC'] == '1'


def test_cancelled_local_admission_restores_welcome_access_checks(shell):
    shell._local_only = True
    shell._admitted_failed(RuntimeError('local startup cancelled'))
    assert not shell._local_only
    assert shell.container is None
    assert shell.lease is None


def test_partial_central_owner_never_starts_local_fallback(shell, monkeypatch):
    calls = []
    failure = OSError('network lost during cleanup')
    failure.cleanup_failed = True
    failure.runtime_container = object()
    monkeypatch.setattr(shell, '_admitted_failed', lambda exc: calls.append(exc))
    shell._admission_failed(failure)
    assert calls == [failure]
    assert not shell._local_only


def test_leaving_local_storage_restarts_before_central_can_be_opened(shell, monkeypatch):
    calls = []
    shell._local_only = True
    shell._leaving = True
    shell._shutdown = SimpleNamespace(containers=[])
    shell.lease = SimpleNamespace(release=lambda: calls.append('release'))
    monkeypatch.setattr(shell, '_role_threads_running', lambda: False)
    monkeypatch.setattr(shell, 'close', lambda: calls.append('close'))
    shell._drained({'ok': True})
    assert calls == ['release', 'close']
    assert shell._requires_fresh_runtime
    assert shell._restart
    assert shell._suppress_exit_update


def test_restart_failure_cannot_reopen_role_with_cached_storage_paths(shell, monkeypatch, tmp_path):
    calls = []
    shell.root = str(tmp_path)
    shell._requires_fresh_runtime = True
    monkeypatch.setattr(shell, 'close', lambda: calls.append('restart'))
    monkeypatch.setattr(shell, '_async', lambda *args: calls.append('unsafe_bootstrap'))
    shell.enter_role('doctor')
    shell.update_application()
    assert calls == ['restart']
    assert not shell._update_requested


def test_old_open_notification_cannot_cancel_new_countdown(shell):
    shell.container = object()
    shell._access_received(state('draining', 2, 'operation'))
    deadline = shell._maintenance_deadline
    assert 59 < deadline - time.monotonic() <= 60
    shell._access_received(state('open', 1))
    assert shell._maintenance_deadline == deadline
    shell._access_received(state('draining', 2, 'operation'))
    assert shell._maintenance_deadline == deadline
    shell._access_received(state('open', 3))
    assert shell._maintenance_deadline is None


def test_countdown_forces_exit_without_restore(shell, monkeypatch):
    calls = []
    monkeypatch.setattr(shell, 'request_role_exit', lambda force=False: calls.append(force))
    shell._maintenance_deadline = time.monotonic() - 2
    shell._tick_maintenance()
    assert calls == [True]


def test_double_click_submits_only_one_maintenance_mutation(shell, monkeypatch):
    calls = []
    monkeypatch.setattr(shell, '_async', lambda *args: calls.append(args))
    shell._maintenance_state = state('open', 0)
    shell._toggle_maintenance()
    shell._toggle_maintenance()
    assert len(calls) == 1


def test_cancel_close_does_not_leave_pending_exit(shell, monkeypatch):
    shell.container = object()
    shell._pending_exit = True
    shell._restart = True
    from rem_card.ui.shared.custom_message_box import CustomMessageBox
    monkeypatch.setattr(CustomMessageBox, 'exec', lambda *a, **k: QMessageBox.No)
    shell.request_role_exit()
    assert not shell._pending_exit
    assert not shell._restart
    assert not shell._leaving


def test_unknown_access_forces_role_out(shell, monkeypatch):
    calls = []
    shell.container = object()
    monkeypatch.setattr(shell, 'request_role_exit', lambda force=False: calls.append(force))
    shell._access_received(state('unknown', None))
    assert calls == [True]


class _Lease:
    def __init__(self):
        self.release_calls = 0

    def release(self):
        self.release_calls += 1


def test_replaced_network_container_blocks_drain_until_both_owners_close(shell, monkeypatch):
    """A network→local operblock switch must retain the replaced owner as well."""
    import rem_card.app.main as main

    old_network = SimpleNamespace(data_service=SimpleNamespace(write_outcomes=lambda: []), close_ok=False)
    local_replacement = SimpleNamespace(data_service=SimpleNamespace(write_outcomes=lambda: []), close_ok=True)
    calls = []

    def close_owner(window, _logger):
        container = window.iter_runtime_containers()[0]
        calls.append(container)
        return container.close_ok

    monkeypatch.setattr(main, '_shutdown_window_resources', close_owner)
    shell.container = local_replacement
    shell._owned_containers = [old_network, local_replacement]
    shell.lease = _Lease()
    monkeypatch.setattr(shell, 'refresh_access', lambda: None)
    shell._shutdown = SessionShutdown(shell._owned_containers, session_id='test', role='operblock_planned')

    first = shell._shutdown.run()
    assert not first['ok']
    shell._drained(first)
    assert shell.lease.release_calls == 0
    assert shell._owned_containers == [old_network, local_replacement]
    assert calls == [old_network, local_replacement]

    old_network.close_ok = True
    second = shell._shutdown.run()
    assert second['ok']
    shell._drained(second)
    assert calls == [old_network, local_replacement, old_network]
    assert shell.lease is None
    assert shell._owned_containers == []


def test_runtime_outage_suppresses_exit_update_check(shell, tmp_path, monkeypatch):
    async_calls = []

    class _CloseEvent:
        def __init__(self):
            self.accepted = False
            self.ignored = False

        def accept(self):
            self.accepted = True

        def ignore(self):
            self.ignored = True

    shell.root = str(tmp_path)
    shell._suppress_exit_update = True
    monkeypatch.setattr(shell, '_async', lambda *args: async_calls.append(args))
    event = _CloseEvent()

    shell.closeEvent(event)

    assert async_calls == []
    assert event.accepted
    assert not event.ignored
