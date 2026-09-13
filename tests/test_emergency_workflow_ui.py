from __future__ import annotations

# Environment isolation must happen before importing Qt and project UI modules.
# ruff: noqa: E402
import os
import threading
from pathlib import Path
from types import SimpleNamespace

_RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "tmp" / "ui-emergency-tests"
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["REMCARD_BAZA_DIR"] = str(_RUNTIME_ROOT / "baza")
os.environ["REMCARD_CI_SETTINGS_DIR"] = str(_RUNTIME_ROOT / "settings")
os.environ["REMCARD_STYLE_SETTINGS_FILE"] = str(_RUNTIME_ROOT / "settings" / "style.ini")
os.environ["LOCALAPPDATA"] = str(_RUNTIME_ROOT / "local")
os.environ["REMCARD_EMERGENCY_DB_ROOT"] = str(_RUNTIME_ROOT / "emergency")
os.environ["REMCARD_LOCAL_LOGS_DIR"] = str(_RUNTIME_ROOT / "logs")

import pytest
from PySide6.QtCore import QElapsedTimer, QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QHBoxLayout, QWidget

from rem_card.ui.shared.emergency_mode_button import create_emergency_mode_button, sync_emergency_mode_button
from rem_card.ui.shared.emergency_workflow_controller import EmergencyWorkflowController


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


class Store:
    def __init__(self, root, status="active", recovery=False):
        self.root = str(root)
        self.metadata = SimpleNamespace(
            status=status,
            emergency_session_id="s1",
            merge_recovery_required=recovery,
        )
        self.transitions = []

    def resolve_root(self):
        return self.root

    def read_active_session(self, session_id):
        assert session_id == "s1"
        return self.metadata

    def mark_session_status(self, session_id, status):
        self.metadata.status = status
        self.transitions.append(status)
        return self.metadata


class Scheduler:
    def __init__(self, status=None):
        self.status = dict(status or {
            "status": "network_baza_unavailable",
            "network_stable": False,
            "last_probe_ts": 1.0,
            "running": False,
            "pending": False,
        })
        self.requests = []

    def request_probe(self, **kwargs):
        self.requests.append(dict(kwargs))
        return True

    def get_status(self):
        return dict(self.status)


class Window(QWidget):
    def __init__(self, store, scheduler=None):
        super().__init__()
        self.store = store
        self.stack = QWidget(self)
        self._is_closing = False
        self.closed = False
        self.resume_tokens = []
        self.shared_finish_requests = 0
        self.shared_finish_cancellations = 0
        self.peers_ready = True
        self.scheduler = scheduler or Scheduler()
        self.container = SimpleNamespace(
            runtime_context=SimpleNamespace(
                mode="emergency",
                emergency_session_id="s1",
                settings_db_path=str(Path(store.root) / "settings.db"),
            ),
            data_service=SimpleNamespace(
                pause_emergency_work=lambda **kw: {"ok": True, "token": "pause-1"},
                resume_emergency_work=lambda token: (self.resume_tokens.append(token) or {"ok": True}),
            ),
            emergency_restore_probe_scheduler=self.scheduler,
        )

    def _emergency_store_for_runtime(self):
        return self.store

    def _doctor_orders_widget_for_close(self):
        return None

    def _request_shared_emergency_finish(self):
        self.shared_finish_requests += 1
        return True

    def _cancel_shared_emergency_finish(self):
        self.shared_finish_cancellations += 1

    def _shared_emergency_peers_ready(self):
        return self.peers_ready

    def close(self):
        self.closed = True
        return super().close()


def _settle(app, predicate, timeout_ms=3000):
    timer = QElapsedTimer()
    timer.start()
    while not predicate() and timer.elapsed() < timeout_ms:
        loop = QEventLoop()
        QTimer.singleShot(10, loop.quit)
        loop.exec()
        app.processEvents()
    assert predicate()


def _ready(ts):
    return {
        "status": "merge_ready_mode_a",
        "network_stable": True,
        "merge_ready": True,
        "last_probe_ts": float(ts),
        "running": False,
        "pending": False,
        "remote_db_path": "X:/baza/medical.db",
    }


def test_preflight_never_pauses_before_fresh_probe_and_password(app, tmp_path):
    scheduler = Scheduler(_ready(10))
    window = Window(Store(tmp_path), scheduler)
    pauses = []
    window.container.data_service.pause_emergency_work = lambda **kw: (
        pauses.append(dict(kw)) or {"ok": True, "token": "pause-1"}
    )
    controller = EmergencyWorkflowController(window)

    controller.begin_wait()
    dialog = controller.preflight
    assert dialog is not None
    dialog._verifier = lambda password: password == "correct-password"
    assert window.stack.isEnabled()
    assert not dialog.confirm_button.isEnabled()

    dialog.password_edit.setText("correct-password")
    dialog.submit_password()
    assert pauses == []
    assert controller.waiting is None

    # The exact ready payload that existed before request_probe is stale.
    controller.on_status(_ready(10))
    assert not dialog.confirm_button.isEnabled()
    assert pauses == []

    controller.on_status({
        "status": "session_lock_active",
        "reason": "doctor.lock",
        "last_probe_ts": 11.0,
        "network_stable": False,
        "running": False,
        "pending": False,
    })
    assert not dialog.confirm_button.isEnabled()
    assert "другое окно RemCard" in dialog.message_label.text()

    dialog.retry_button.click()
    controller.on_status({
        "status": "round_success_merge_ready_mode_a",
        "network_stable": False,
        "consecutive_successes": 1,
        "success_rounds_required": 3,
        "last_probe_ts": 12.0,
        "running": False,
        "pending": False,
    })
    assert not dialog.confirm_button.isEnabled()
    assert controller._probe_context == "preflight"
    assert "1 из 3" in dialog.message_label.text()
    controller.on_status(_ready(13))
    assert dialog.confirm_button.isEnabled()
    assert window.stack.isEnabled()
    assert pauses == []

    dialog.password_edit.setText("wrong")
    dialog.submit_password()
    assert pauses == []
    dialog.password_edit.setText("correct-password")
    dialog.submit_password()
    _settle(app, lambda: controller.pause_token is not None)
    assert len(pauses) == 1
    assert not window.stack.isEnabled()
    assert window.shared_finish_requests == 1
    assert window.store.metadata.status == "waiting"


def test_cancelled_preflight_keeps_active_work_enabled(app, tmp_path):
    window = Window(Store(tmp_path), Scheduler(_ready(3)))
    controller = EmergencyWorkflowController(window)
    controller.begin_wait()
    controller.preflight.cancel_button.click()
    app.processEvents()
    assert controller.preflight is None
    assert controller.waiting is None
    assert window.stack.isEnabled()
    assert window.store.metadata.status == "active"
    assert window.shared_finish_requests == 0


def test_probe_timeout_is_actionable_in_preflight_and_waiting(app, tmp_path):
    window = Window(Store(tmp_path))
    controller = EmergencyWorkflowController(window)
    controller.begin_wait()
    controller._probe_timed_out()
    assert controller.preflight.retry_button.isEnabled()
    assert "не приостановлена" in controller.preflight.message_label.text()
    controller.preflight.cancel_button.click()

    controller.begin_wait(password_required=False)
    _settle(app, lambda: controller.pause_token is not None)
    assert controller._probe_context == "waiting"
    controller._probe_timed_out()
    assert "остаётся приостановленной" in controller.waiting.message_label.text()
    assert "не приостановлена" not in controller.waiting.message_label.text()


def test_recovery_notice_is_single_and_repeats_after_five_minute_timer(app, tmp_path):
    window = Window(Store(tmp_path))
    controller = EmergencyWorkflowController(window)
    controller.on_status(_ready(2))
    _settle(app, lambda: controller.recovery_notice is not None)
    first = controller.recovery_notice
    for ts in range(3, 20):
        controller.on_status(_ready(ts))
    app.processEvents()
    assert controller.recovery_notice is first

    first.stay_button.click()
    app.processEvents()
    assert controller.recovery_notice is None
    assert controller._recovery_reminder_timer.isActive()
    assert controller._recovery_reminder_timer.interval() == 5 * 60 * 1000

    # The timer is injectable/controllable in tests; no wall-clock five-minute wait.
    controller._recovery_reminder_timer.start(1)
    _settle(app, lambda: controller.recovery_notice is not None)
    second = controller.recovery_notice
    assert second is not first
    controller.on_status({
        "status": "network_baza_unavailable",
        "network_stable": False,
        "last_probe_ts": 20.0,
    })
    app.processEvents()
    assert controller.recovery_notice is None
    assert not controller._recovery_reminder_timer.isActive()


def test_go_now_cancelled_password_retries_notice_without_disabling_work(app, tmp_path):
    window = Window(Store(tmp_path))
    controller = EmergencyWorkflowController(window)
    controller.on_status(_ready(2))
    _settle(app, lambda: controller.recovery_notice is not None)
    controller.recovery_notice.go_button.click()
    app.processEvents()
    assert controller.preflight is not None
    assert window.stack.isEnabled()
    controller.preflight.cancel_button.click()
    app.processEvents()
    assert controller.preflight is None
    assert controller._recovery_reminder_timer.isActive()
    assert window.store.metadata.status == "active"
    assert window.stack.isEnabled()


def test_local_activity_does_not_restart_five_minute_network_reminder(app, tmp_path):
    window = Window(Store(tmp_path))
    controller = EmergencyWorkflowController(window)
    controller.on_status(_ready(2))
    _settle(app, lambda: controller.recovery_notice is not None)
    controller.recovery_notice.stay_button.click()
    for index, status in enumerate(("local_write_busy", "round_success_merge_ready_mode_a", "local_maintenance_busy"), 3):
        controller.on_status({"status": status, "network_stable": False, "last_probe_ts": float(index)})
    controller.on_status(_ready(6))
    app.processEvents()
    assert controller.recovery_notice is None
    assert controller._recovery_reminder_timer.isActive()


def test_ignored_window_close_keeps_recovery_notice_timer_alive(app, tmp_path):
    class IgnoreCloseWindow(Window):
        def closeEvent(self, event):
            event.ignore()

    window = IgnoreCloseWindow(Store(tmp_path))
    controller = EmergencyWorkflowController(window)
    controller._network_ready = True
    controller._defer_recovery_notice()
    assert controller._recovery_reminder_timer.isActive()
    assert window.close() is False
    app.processEvents()
    assert window._is_closing is False
    assert controller._shutting_down is False
    assert controller._recovery_reminder_timer.isActive()


def test_wait_resume_preserves_token_status_and_shared_finish_hooks(app, tmp_path):
    store = Store(tmp_path)
    window = Window(store)
    controller = EmergencyWorkflowController(window)
    controller.begin_wait(password_required=False)
    _settle(app, lambda: controller.pause_token is not None)
    assert store.metadata.status == "waiting"
    assert window.shared_finish_requests == 1
    controller.resume()
    assert store.metadata.status == "active"
    assert window.stack.isEnabled()
    assert window.resume_tokens == [{"ok": True, "token": "pause-1"}]
    assert window.shared_finish_cancellations == 1


def test_reopened_waiting_requests_shared_finish_before_pause(app, tmp_path):
    store = Store(tmp_path, status="waiting")
    window = Window(store)
    controller = EmergencyWorkflowController(window)
    assert not window.stack.isEnabled()
    _settle(app, lambda: controller.pause_token is not None)
    assert window.shared_finish_requests == 1
    assert store.metadata.status == "waiting"


def test_peer_wait_and_session_lock_messages_do_not_soften_backend_blockers(app, tmp_path):
    scheduler = Scheduler()
    window = Window(Store(tmp_path), scheduler)
    window.peers_ready = False
    controller = EmergencyWorkflowController(window)
    reviews = []
    controller._build_review = lambda: {"review": True}
    controller._show_review = lambda value: reviews.append(value)
    controller.begin_wait(password_required=False)
    _settle(app, lambda: controller.pause_token is not None)
    controller.on_status(_ready(2))
    assert reviews == []
    assert "Ожидаем сохранения и закрытия других окон" in controller.waiting.message_label.text()

    controller.on_status({
        "status": "session_lock_active",
        "network_stable": False,
        "reason": "nurse.lock",
        "last_probe_ts": 3.0,
    })
    assert "Закройте обычные окна" in controller.waiting.message_label.text()
    assert reviews == []

    window.peers_ready = True
    controller.check_now()
    controller.on_status(_ready(4))
    _settle(app, lambda: bool(reviews))
    assert reviews == [{"review": True}]


def test_cancelled_review_waits_for_explicit_check_before_reopening(app, tmp_path, monkeypatch):
    window = Window(Store(tmp_path))
    controller = EmergencyWorkflowController(window)
    controller.begin_wait(password_required=False)
    _settle(app, lambda: controller.pause_token is not None)

    class RejectedReviewDialog:
        selected_admission_ids = []

        def __init__(self, *_args, **_kwargs):
            pass

        def exec(self):
            return 0

    monkeypatch.setattr(
        "rem_card.ui.shared.emergency_review_dialog.EmergencyReviewDialog",
        RejectedReviewDialog,
    )
    controller.review_offered = True
    controller._show_review({"patients": []})
    assert controller.review_offered is True
    assert "Проверить связь" in controller.waiting.message_label.text()
    controller._build_review = lambda: (_ for _ in ()).throw(AssertionError("must wait for explicit check"))
    controller.on_status(_ready(3))
    app.processEvents()
    assert controller.review_offered is True


def test_worker_result_is_dispatched_on_gui_thread_without_hang(app, tmp_path):
    controller = EmergencyWorkflowController(Window(Store(tmp_path)))
    gui_ident = threading.get_ident()
    calls = []
    controller._run(
        threading.get_ident,
        lambda worker_ident: calls.append((worker_ident, threading.get_ident())),
    )
    _settle(app, lambda: bool(calls))
    worker_ident, callback_ident = calls[0]
    assert worker_ident != gui_ident
    assert callback_ident == gui_ident


def test_uncertain_commit_disables_resume_and_close_keeps_recovery(app, tmp_path):
    store = Store(tmp_path, status="merge_failed", recovery=True)
    window = Window(store)
    controller = EmergencyWorkflowController(window)
    _settle(app, lambda: controller.pause_token is not None)
    assert not controller.waiting.resume_button.isEnabled()
    controller.close_waiting()
    assert window.closed
    assert store.metadata.status == "merge_failed"
    assert store.metadata.merge_recovery_required


def test_button_visibility_callback_is_cancelled_when_panel_is_deleted(app, tmp_path):
    from PySide6.QtCore import QCoreApplication, QEvent

    window = Window(Store(tmp_path))
    panel = QWidget(window)
    panel.icon_dir = str(tmp_path)
    panel.layout = QHBoxLayout(panel)
    panel.btn_emergency_mode = create_emergency_mode_button(panel)
    panel.layout.addWidget(panel.btn_emergency_mode)
    sync_emergency_mode_button(panel)
    panel.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


def test_sector_button_uses_user_icon_and_only_emergency_runtime(app, tmp_path):
    window = Window(Store(tmp_path))
    panel = QWidget(window)
    panel.icon_dir = str(Path(__file__).resolve().parents[1] / "icon")
    panel.layout = QHBoxLayout(panel)
    panel.btn_emergency_mode = create_emergency_mode_button(panel)
    panel.layout.addWidget(panel.btn_emergency_mode)
    sync_emergency_mode_button(panel)
    app.processEvents()
    assert not panel.btn_emergency_mode.isHidden()
    assert not panel.btn_emergency_mode.icon().isNull()
    window.container.runtime_context.mode = "network"
    sync_emergency_mode_button(panel)
    app.processEvents()
    assert panel.btn_emergency_mode.isHidden()
