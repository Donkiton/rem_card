import time
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from rem_card.app.unified_runtime import SessionShutdown
from rem_card.ui.unified_window import UnifiedWindow


@pytest.fixture
def shell(tmp_path, monkeypatch):
    from rem_card.app import local_administrator
    monkeypatch.setattr(local_administrator, "is_local_administrator", lambda: False)
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
    window._restart = window._pending_exit = False
    window.close()
    window.deleteLater()
    app.processEvents()
    app.setProperty('unified_entry', old_unified)


def state(mode, generation, operation=None):
    return dict(state=mode, generation=generation, operation_id=operation, owner_token='token')


def test_initial_loading_precedes_role_chooser(shell, monkeypatch, tmp_path):
    from rem_card.app import runtime_paths
    monkeypatch.setattr(runtime_paths, 'is_compiled', lambda: False)
    monkeypatch.setattr(runtime_paths, 'resolve_baza_dir', lambda: str(tmp_path))
    monkeypatch.setattr(shell, '_configure_store', lambda: None)
    monkeypatch.setattr(shell, '_async', lambda *a: None)
    monkeypatch.setattr(shell, 'refresh_access', lambda: None)
    monkeypatch.setattr(shell, '_check_updates', lambda: None)
    shell.stack.setCurrentWidget(shell.welcome)
    shell.initialize()
    assert shell.stack.currentWidget() is shell.loading
    shell._ready()
    assert shell.stack.currentWidget() is shell.welcome


def test_role_preparation_stays_on_chooser_and_error_restores_controls(shell, monkeypatch, tmp_path):
    shell.root = str(tmp_path)
    calls = []
    monkeypatch.setattr(shell, '_async', lambda *a: calls.append(a))
    shell.stack.setCurrentWidget(shell.welcome)
    shell.enter_role('doctor')
    assert shell.stack.currentWidget() is shell.welcome
    assert shell.welcome.role_buttons['doctor'].property('preparing')
    assert not shell.welcome.theme_switch.isEnabled()
    assert not any(button.isEnabled() for button in shell.welcome.role_buttons.values())
    shell.enter_role('nurse')
    assert shell.role == 'doctor' and len(calls) == 1
    shell._error(RuntimeError('Нет доступа'))
    assert not shell.welcome.role_buttons['doctor'].property('preparing')
    assert shell.welcome.theme_switch.isEnabled() == shell.welcome.theme_switch.runtime_enabled


def test_entry_theme_updates_chrome_loading_and_survives_resize(shell):
    for mode in ('light', 'dark', 'light'):
        shell.welcome.set_theme(mode)
        assert shell.entry_chrome.property('entry_theme') == mode
        assert shell.entry_chrome.title_bar.property('entry_theme') == mode
        assert shell.loading._theme_mode == mode
        shell.loading.set_stage(1)
        assert all(row._theme_mode == mode for row in shell.loading.stage_rows)
        shell.welcome.resize(1194, 744)
        QApplication.processEvents()
        assert shell.welcome._theme_mode == mode


@pytest.mark.parametrize('mode', ['light', 'dark'])
def test_about_button_opens_dialog_in_current_theme_and_accepts_click(shell, mode):
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtWidgets import QDialogButtonBox
    from PySide6.QtTest import QTest
    from rem_card.ui.shared.unified_settings_dialogs import EntryInformationDialog
    shell.welcome.theme_switch._on_theme_changed(mode)
    observed = []

    def accept_dialog():
        dialog = QApplication.activeModalWidget()
        if isinstance(dialog, EntryInformationDialog):
            observed.append((dialog.chrome.property('entry_theme'), dialog.styleSheet()))
            box = dialog.findChild(QDialogButtonBox)
            QTest.mouseClick(box.button(QDialogButtonBox.Ok), Qt.LeftButton)
        elif dialog is not None:
            dialog.reject()

    QTimer.singleShot(0, accept_dialog)
    shell.welcome.about_button.click()
    assert len(observed) == 1
    assert observed[0][0] == mode
    assert ('#fffdf9' in observed[0][1]) == (mode == 'light')


def test_confirmed_application_exit_does_not_ask_to_return_to_roles(shell, monkeypatch):
    from rem_card.ui.shared.custom_message_box import CustomMessageBox
    monkeypatch.setattr(CustomMessageBox, 'question', lambda *a, **k: pytest.fail('second confirmation'))
    shell.container = SimpleNamespace()
    calls = []
    monkeypatch.setattr(shell, 'request_role_exit', lambda force=False: calls.append(force))
    shell.request_application_exit(confirmed=True)
    assert shell._pending_exit
    assert calls == [True]


def test_application_exit_hides_window_but_keeps_runtime_until_drain(shell, monkeypatch):
    from PySide6.QtWidgets import QWidget
    data = SimpleNamespace(set_shutting_down=lambda: None)
    container = SimpleNamespace(data_service=data)
    role = QWidget()
    role.doctor_main = role.nurse_main = role.operblock_main = None
    role.iter_runtime_containers = lambda: [container]
    shell.stack.addWidget(role)
    shell.stack.setCurrentWidget(role)
    shell.container, shell.role_window, shell.role = container, role, 'doctor'
    shell.show()
    pages = []
    shell.stack.currentChanged.connect(lambda _: pages.append(shell.stack.currentWidget()))
    waits = []
    monkeypatch.setattr(shell, '_wait_before_drain', lambda: waits.append(True))
    shell.request_application_exit(confirmed=True)
    assert shell.isHidden()
    assert shell.stack.currentWidget() is role
    assert shell.loading not in pages
    assert shell.container is container and shell._shutdown is not None
    assert shell._leaving and waits == [True]


@pytest.mark.parametrize('index,result', [(0, QMessageBox.Yes), (1, QMessageBox.No)])
def test_role_confirmation_buttons_visibly_press_and_return_result(shell, index, result):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QPushButton
    from rem_card.ui.shared.custom_message_box import CustomMessageBox
    dialog = CustomMessageBox('Выход из роли', 'Вернуться к выбору ролей?', 'warning', parent=shell,
                              action_buttons=[('Вернуться к ролям', QMessageBox.Yes), ('Остаться', QMessageBox.No)])
    dialog.show()
    QApplication.processEvents()
    button = dialog.findChildren(QPushButton, 'DialogOkBtn')[index]
    QTest.mouseMove(button, button.rect().center())
    QApplication.processEvents()
    before = button.grab().toImage()
    geometry = button.geometry()
    QTest.mousePress(button, Qt.LeftButton)
    QApplication.processEvents()
    assert button.isDown()
    assert button.grab().toImage() != before
    assert button.geometry() == geometry
    QTest.mouseRelease(button, Qt.LeftButton)
    assert dialog.result() == result and not dialog.isVisible()
    dialog.deleteLater()


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
    target = QRect(screen.left(), screen.top(), min(1250, screen.width()), min(max(800, shell.minimumHeight()), screen.height()))
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
    assert not shell._transition.running


def test_drain_waits_for_live_animation_without_releasing_runtime(shell, monkeypatch):
    from PySide6.QtCore import QRect, QTimer
    container = object()
    shell.container = container
    shell._transition.start(QRect(shell.geometry()), lambda: None, lambda: None)
    queued = []
    monkeypatch.setattr(QTimer, 'singleShot', lambda delay, callback: queued.append(callback))
    monkeypatch.setattr(shell, '_async', lambda *args: pytest.fail('Drain during animation'))
    shell._wait_before_drain()
    assert shell.container is container and queued == [shell._wait_before_drain]
    shell._transition.cancel()


def test_return_animation_resizes_chooser_instead_of_clinical_page(shell):
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QWidget
    role_page = QWidget()
    shell.stack.addWidget(role_page)
    shell.stack.setCurrentWidget(role_page)
    shell.entry_chrome.set_role_mode(True)
    shell.showNormal()
    QApplication.processEvents()
    prepared = []
    def prepare():
        prepared.append(True)
        shell.stack.setCurrentWidget(shell.welcome)
        shell.entry_chrome.set_role_mode(False)
    shell._transition.start(QRect(shell.geometry()), prepare, lambda: None,
                            prepare_before_resize=True)
    assert shell._transition.running and prepared == [True]
    assert role_page.isHidden() and shell.welcome.isVisible()
    shell._transition.cancel()


def test_role_page_keeps_ownership_but_does_not_resize_until_reveal(shell):
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QWidget
    role_page = QWidget()
    shell.role_window = role_page
    shell.stack.addWidget(role_page)
    shell.stack.setCurrentWidget(shell.welcome)
    shell.settings.setValue('test_role/normal_rect', QRect(0, 0, 1400, 850))
    shell.settings.setValue('test_role/maximized', False)
    shell._animate_page(role_page, 'test_role', True, lambda: None)
    assert shell.stack.indexOf(role_page) == -1
    assert role_page.parentWidget() is shell.stack
    assert shell.role_window is role_page
    shell._transition.animation.setCurrentTime(shell._transition.animation.duration())
    assert shell.stack.currentWidget() is role_page
    assert shell.stack.indexOf(role_page) >= 0


def test_transition_does_not_reapply_unchanged_native_masks(shell, monkeypatch):
    shell.showNormal()
    QApplication.processEvents()
    shell.entry_chrome._update_masks()
    calls = []
    monkeypatch.setattr(shell, 'setMask', lambda mask: calls.append(mask))
    shell.entry_chrome._update_masks()
    shell.entry_chrome._update_masks()
    assert not calls


def test_transition_holds_entry_typography_and_restores_native_mask_on_cancel(shell):
    from PySide6.QtCore import QRect
    shell.showNormal()
    shell.resize(1200, 780)
    QApplication.processEvents()
    original_font = shell.welcome.heading.font()
    target = QRect(shell.geometry())
    target.setWidth(1800)
    shell._transition.start(target, lambda: None, lambda: None)
    shell._transition.animation.pause()
    shell._transition.animation.setCurrentTime(shell._transition.animation.duration() // 2)
    QApplication.processEvents()
    assert shell.width() > 1300
    assert shell.welcome.heading.font() == original_font
    assert shell.mask().isEmpty()
    assert not shell.entry_chrome.content.mask().isEmpty()
    shell._transition.cancel()
    assert not shell.mask().isEmpty()
    assert not shell.welcome._window_transition_active
    assert not shell.entry_chrome._transition_active


def test_failed_transition_preparation_restores_masks(shell):
    from PySide6.QtCore import QRect
    def fail():
        raise RuntimeError('preparation failed')
    with pytest.raises(RuntimeError, match='preparation failed'):
        shell._transition.start(QRect(shell.geometry()), fail, lambda: None,
                                prepare_before_resize=True)
    assert shell.updatesEnabled()
    assert not shell.entry_chrome._transition_active
    assert not shell._transition.running


def test_entry_status_does_not_move_quote(shell):
    shell.stack.setCurrentWidget(shell.welcome)
    shell.showNormal()
    QApplication.processEvents()
    page = shell.welcome
    original = page.quote_label.geometry()
    for message in ('Подготовка рабочего места…',
                    'Завершение сохранения и освобождение базы…', ''):
        page.set_access_state(message)
        QApplication.processEvents()
        assert page.quote_label.geometry() == original
        assert page.access_label.y() > page.quote_author.geometry().bottom()



def test_transition_keeps_live_text_at_native_resolution(shell, monkeypatch):
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QWidget, QLabel
    page = QWidget()
    label = QLabel('RemCard · Чёткий текст', page)
    label.setGeometry(10, 10, 250, 40)
    label.setStyleSheet('font: 16px "Segoe UI"; color: black; background: white;')
    shell.stack.addWidget(page)
    shell.stack.setCurrentWidget(page)
    shell.showNormal()
    QApplication.processEvents()
    original = label.grab().toImage()
    # A transition must never replace live widgets with a stretched screenshot.
    monkeypatch.setattr(shell.entry_chrome, 'grab', lambda: pytest.fail('Screenshot animation'))
    target = QRect(shell.geometry())
    target.setWidth(target.width() + 200)
    target.setHeight(target.height() + 100)
    shell._transition.start(target, lambda: None, lambda: None)
    shell._transition.animation.pause()
    shell._transition.animation.setCurrentTime(shell._transition.animation.duration() // 2)
    QApplication.processEvents()
    assert label.isVisible() and shell.entry_chrome.content.isVisible()
    assert label.grab().toImage() == original
    shell._transition.cancel()
    assert label.isVisible()


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


def test_leaving_local_storage_keeps_chooser_but_blocks_central_bootstrap(shell, monkeypatch):
    calls = []
    shell._local_only = True
    shell._leaving = True
    shell._shutdown = SimpleNamespace(containers=[])
    shell.lease = SimpleNamespace(release=lambda: calls.append('release'))
    monkeypatch.setattr(shell, '_role_threads_running', lambda: False)
    monkeypatch.setattr(shell, 'close', lambda: calls.append('close'))
    shell._drained({'ok': True})
    assert calls == ['release']
    assert shell._requires_fresh_runtime
    assert not shell._restart
    assert shell.stack.currentWidget() is shell.welcome
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
    shell._local_administrator = True
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
    shell.request_role_exit()
    shell._role_exit_dialog.done(QMessageBox.No)
    assert not shell._pending_exit
    assert not shell._restart
    assert not shell._leaving


def test_unknown_access_forces_role_out(shell, monkeypatch):
    calls = []
    shell.container = object()
    monkeypatch.setattr(shell, 'request_role_exit', lambda force=False: calls.append(force))
    shell._access_received(state('unknown', None))
    assert calls == [True]


@pytest.mark.parametrize('role', ['doctor', 'nurse'])
def test_network_access_loss_routes_to_outage_handler(shell, monkeypatch, role):
    calls = []
    shell.role = role
    shell.container = SimpleNamespace(data_service=SimpleNamespace(
        _handle_database_access_failure=lambda exc, **kwargs: calls.append((exc, kwargs))))
    monkeypatch.setattr(shell, 'request_role_exit', lambda **kw: pytest.fail('bypassed outage dialog'))
    shell._access_received(dict(state='unknown', generation=None, error='root_unavailable'))
    assert shell._central_unavailable
    assert len(calls) == 1 and calls[0][1]['source'] == 'unified_access'
    assert shell.welcome.role_buttons['nurse'].isEnabled()


@pytest.mark.parametrize('role,exit_expected', [('doctor', False), ('nurse', True)])
def test_outage_doctor_returns_to_chooser_nurse_finishes_restart(shell, monkeypatch, role, exit_expected):
    calls = []
    shell.role = role
    monkeypatch.setattr(shell, 'request_role_exit', lambda **kw: calls.append(kw))
    shell._finish_runtime_outage()
    assert shell._pending_exit is exit_expected
    assert shell._central_unavailable and shell._suppress_exit_update
    assert calls == [{'force': True}]


def test_offline_initialization_dispatches_emergency_continuation(shell, monkeypatch):
    from rem_card.app.unified_runtime import CentralUnavailable
    from rem_card.app.unified_preflight import attach_startup_request, build_startup_request
    calls = []
    attach_startup_request(shell, build_startup_request(role='nurse', emergency_startup_request='request.json'))
    monkeypatch.setattr(shell, 'enter_role', calls.append)
    monkeypatch.setattr(shell, '_check_updates', lambda: None)
    shell._initial_incompatible(CentralUnavailable('Нет сети'))
    QApplication.processEvents()
    assert calls == ['nurse']
    shell._dispatch_startup_role()
    QApplication.processEvents()
    assert calls == ['nurse']


def test_clean_process_restart_keeps_selected_role(shell, monkeypatch, tmp_path):
    calls = []
    shell.root = str(tmp_path)
    shell._requires_fresh_runtime = True
    monkeypatch.setattr(shell, '_restart_for_storage_boundary', lambda: calls.append(shell._restart_resume_role))
    shell.enter_role('nurse')
    assert calls == ['nurse']


@pytest.mark.parametrize('status', ['role_not_allowed', 'no_valid_standby', 'local_snapshot_central_mismatch'])
def test_rejected_offline_entry_explains_reason_and_keeps_chooser(shell, monkeypatch, status):
    from rem_card.app.unified_preflight import LocalOnlyStartupError
    messages = []
    shell._central_unavailable = True
    monkeypatch.setattr(QMessageBox, 'warning', lambda parent, title, message: messages.append(message))
    shell._admitted_failed(LocalOnlyStartupError('Копия недоступна: причина', status=status))
    assert messages == ['Копия недоступна: причина']
    assert shell.stack.currentWidget() is shell.welcome
    assert shell.welcome.role_buttons['nurse'].isEnabled()


def test_reconnect_preserves_role_and_drains_before_restart(shell, monkeypatch):
    calls = []
    shell.role = 'doctor'
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.Yes)
    monkeypatch.setattr(shell, 'request_role_exit', lambda **kw: calls.append(kw))
    shell._request_emergency_reconnect()
    assert calls == [{'force': True}]
    assert shell._restart_resume_role == 'doctor'


def test_network_access_loss_does_not_clear_known_incompatibility(shell):
    shell._compatibility_error = 'Требуется обновление RemCard'
    shell._access_received(dict(state='unknown', generation=None, error='root_unavailable'))
    assert not shell.welcome.role_buttons['nurse'].isEnabled()


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


@pytest.mark.parametrize('role', ['doctor', 'nurse', 'operblock_emergency', 'operblock_planned'])
def test_maintenance_acknowledgment_keeps_countdown_and_about_button(shell, monkeypatch, role):
    from PySide6.QtWidgets import QPushButton
    shell.role = role
    shell.container = object()
    calls = []
    monkeypatch.setattr(shell, 'request_role_exit', lambda force=False: calls.append(force))
    shell._access_received(state('draining', 1, 'work'))
    deadline = shell._maintenance_deadline
    dialog = shell._maintenance_warning
    assert dialog is not None
    button = next(b for b in dialog.findChildren(QPushButton) if b.text() == 'ОК')
    button.click()
    assert shell._maintenance_warning is None
    assert shell._maintenance_deadline == deadline and not calls
    assert shell.welcome.about_button.text() == ''
    assert shell.welcome.about_button.accessibleName() == 'О программе'
    shell._access_received(state('draining', 1, 'work'))
    assert shell._maintenance_warning is None
    shell._maintenance_deadline = time.monotonic() - 1
    shell._tick_maintenance()
    assert calls == [True]


def test_local_administrator_is_not_evicted_but_unknown_access_still_closes(shell, monkeypatch):
    shell._local_administrator = True
    shell.container = object()
    shell._access_received(state('draining', 1, 'work'))
    assert shell._maintenance_warning is None
    assert shell._maintenance_deadline is None
    assert all(b.isEnabled() for b in shell.welcome.role_buttons.values())
    calls = []
    monkeypatch.setattr(shell, 'request_role_exit', lambda force=False: calls.append(force))
    shell._access_received(state('unknown', 2))
    assert calls == [True]


def test_maintenance_is_embedded_in_control_center(shell, monkeypatch):
    from PySide6.QtWidgets import QWidget, QStackedWidget, QVBoxLayout
    from rem_card.ui.admin_view.admin_main_widget import AdminMainWidget
    class Center(QWidget):
        _show_maintenance_page = AdminMainWidget._show_maintenance_page
        go_back = AdminMainWidget.go_back
        def __init__(self):
            super().__init__()
            self.stack = QStackedWidget(self)
            QVBoxLayout(self).addWidget(self.stack)
            self.menu_widget = QWidget()
            self.stack.addWidget(self.menu_widget)
            self.settings_content_stack = QStackedWidget(self.menu_widget)
            QVBoxLayout(self.menu_widget).addWidget(self.settings_content_stack)
            category = QWidget()
            self.settings_content_stack.addWidget(category)
            self.settings_categories = [dict(key='maintenance', page=category)]
        def _prepare_settings_surface(self, page):
            pass
        def _select_settings_category(self, index):
            self.settings_content_stack.setCurrentWidget(self.settings_categories[index]['page'])
    center = Center()
    shell.stack.addWidget(center)
    shell.stack.setCurrentWidget(center)
    monkeypatch.setattr(shell, 'refresh_access', lambda: None)
    shell._maintenance_state = state('open', 0)
    shell.open_maintenance(center)
    assert shell.stack.currentWidget() is center
    assert center.stack.currentWidget() is center.menu_widget
    assert center.settings_content_stack.currentWidget() is shell._control_page
    assert not shell._control_page.isWindow()
    assert shell._control_page.property('settingsEmbedded')
    assert not shell._maintenance_button.isEnabled()
    assert center.go_back()
    assert center.settings_content_stack.currentWidget() is center.settings_categories[0]['page']
    shell.open_maintenance(center)
    shell._control_page.btn_back.click()
    assert center.settings_content_stack.currentWidget() is center.settings_categories[0]['page']


def test_local_role_reentry_never_acquires_central_lease(shell, monkeypatch, tmp_path):
    shell.root = str(tmp_path)
    shell._requires_fresh_runtime = True
    shell._local_runtime_roles = frozenset({'doctor', 'nurse'})
    calls = []
    monkeypatch.setattr(shell, '_admission_failed', lambda exc: calls.append(shell.role))
    monkeypatch.setattr(shell, '_async', lambda *a: pytest.fail('central access in pinned process'))
    monkeypatch.setattr(shell, '_restart_for_storage_boundary', lambda: pytest.fail('unnecessary restart'))
    shell.enter_role('nurse')
    assert calls == ['nurse']


def test_role_exit_confirmation_is_single_and_nonblocking(shell, monkeypatch):
    shell.container = object()
    shell.request_role_exit()
    dialog = shell._role_exit_dialog
    shell.request_role_exit()
    assert shell._role_exit_dialog is dialog
    calls = []
    monkeypatch.setattr(shell, 'request_role_exit', lambda force=False: calls.append(force))
    dialog.done(QMessageBox.Yes)
    assert calls == [True]
    assert shell._role_exit_dialog is None


def test_stale_role_exit_confirmation_cannot_close_new_session(shell, monkeypatch):
    shell.container = object()
    shell.session_id = 'old'
    shell.request_role_exit()
    dialog = shell._role_exit_dialog
    shell.session_id = 'new'
    calls = []
    monkeypatch.setattr(shell, 'request_role_exit', lambda force=False: calls.append(force))
    dialog.done(QMessageBox.Yes)
    assert calls == []


def test_emergency_restart_is_requested_before_drain(shell, monkeypatch):
    shell.role = 'nurse'
    calls = []
    monkeypatch.setattr(shell, 'request_role_exit', lambda force=False: calls.append((force, shell._restart, shell._pending_exit)))
    shell._restart_emergency_to_network()
    assert calls == [(True, True, True)]
    assert shell._restart_resume_role == 'nurse'


def test_local_emergency_button_requests_reconnect(shell, monkeypatch):
    shell._local_only = True
    calls = []
    monkeypatch.setattr(shell, '_request_emergency_reconnect', lambda: calls.append('reconnect'))
    shell.request_emergency_mode_action()
    assert calls == ['reconnect']


def test_failed_local_writes_remain_visible_on_chooser(shell, monkeypatch):
    shell._local_only = True
    shell._leaving = True
    data = SimpleNamespace(write_outcomes=lambda: [{'state': 'failed'}])
    shell._shutdown = SimpleNamespace(containers=[SimpleNamespace(data_service=data)])
    monkeypatch.setattr(shell, '_role_threads_running', lambda: False)
    shell._drained({'ok': True})
    assert 'Не выполнено сохранений: 1' in shell.welcome._access_message
    assert 'сессия сохранена' not in shell.welcome._access_message
