import json
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget

from rem_card.ui.shared import emergency_participants as peers


@pytest.fixture
def window():
    app = QApplication.instance() or QApplication([])
    target = QMainWindow()
    target._is_closing = False
    target._runtime_outage_handling = False
    target.stack = QWidget(target)
    target._doctor_orders_widget_for_close = lambda: None
    target._emergency_workflow = SimpleNamespace(busy=False, waiting=None, pause_token=None)
    target.container = SimpleNamespace(data_service=SimpleNamespace(
        pause_emergency_work=lambda **kw: {"ok": True}, resume_emergency_work=lambda token: {"ok": True}))
    target.close_calls = 0
    def close():
        target.close_calls += 1
        return True
    target.close = close
    yield target
    app.setProperty("remcard_restart_requested", False)
    target.deleteLater()
    app.processEvents()


def _participant(**kwargs):
    return SimpleNamespace(finish_lock=None, another_window_is_finishing=lambda: True, **kwargs)


def test_peer_draft_requires_resolution_before_closing(window):
    window._doctor_orders_widget_for_close = lambda: SimpleNamespace(has_drafts=lambda: True)
    monitor = peers.EmergencyPeerMonitor(window, _participant())
    monitor.timer.stop()
    monitor.poll()
    assert window.close_calls == 0
    assert not monitor.draining
    assert window.stack.isEnabled()
    assert "черновик" in window.statusBar().currentMessage()


def test_peer_closes_only_after_successful_local_drain(window, monkeypatch):
    class Worker(QObject):
        succeeded = Signal(object)
        failed = Signal(object)
        def __init__(self, *args, **kwargs):
            super().__init__()
        def start(self):
            pass
    monkeypatch.setattr(peers, "AsyncCallThread", Worker)
    monitor = peers.EmergencyPeerMonitor(window, _participant())
    monitor.timer.stop()
    monitor.poll()
    assert not window.stack.isEnabled()
    assert window.close_calls == 0
    monitor.worker.succeeded.emit({"ok": False})
    assert window.close_calls == 0
    assert not monitor.draining
    monitor.poll()
    monitor.worker.succeeded.emit({"ok": True})
    assert window.close_calls == 1


def test_already_paused_peer_closes_its_waiting_dialog(window):
    dismissed = []
    window._emergency_workflow.pause_token = object()
    window._emergency_workflow.waiting = SimpleNamespace(finish_with_code=dismissed.append)
    monitor = peers.EmergencyPeerMonitor(window, _participant())
    monitor.timer.stop()
    monitor.poll()
    assert dismissed == [0]
    assert window.close_calls == 1


def test_owner_refreshes_once_when_last_peer_finishes(window):
    probes = []
    participant = _participant(peers_ready=lambda: False)
    participant.finish_lock = object()
    window._emergency_workflow.waiting = object()
    window._emergency_workflow.check_now = lambda: probes.append(True)
    monitor = peers.EmergencyPeerMonitor(window, participant)
    monitor.timer.stop()
    monitor.poll()
    participant.peers_ready = lambda: True
    monitor.poll()
    monitor.poll()
    assert probes == [True]


@pytest.mark.parametrize("result,close_accepted", [({"ok": False}, True), ({"ok": True}, False)])
def test_cancelled_common_finish_restores_peer_after_failed_drain_or_close(window, result, close_accepted):
    participant = _participant()
    resumed = []
    window.container.data_service.resume_emergency_work = lambda token: resumed.append(token) or {"ok": True}
    window.close = lambda: close_accepted
    monitor = peers.EmergencyPeerMonitor(window, participant)
    monitor.timer.stop()
    monitor.draining = True
    window.stack.setEnabled(False)
    monitor._drained(result)
    participant.another_window_is_finishing = lambda: False
    monitor.poll()
    assert not monitor.draining
    assert window.stack.isEnabled()
    assert resumed == ([result] if result["ok"] else [])


def test_cancelled_finish_during_drain_resumes_without_closing_peer(window):
    participant = _participant()
    monitor = peers.EmergencyPeerMonitor(window, participant)
    monitor.timer.stop()
    monitor.draining = True
    window.stack.setEnabled(False)
    participant.another_window_is_finishing = lambda: False
    monitor._drained({"ok": True})
    assert window.close_calls == 0
    assert not monitor.draining
    assert window.stack.isEnabled()


@pytest.mark.parametrize("status,same_database,redirect", [
    ("active", True, True), ("waiting", True, True),
    ("merge_pending", True, True), ("merged", True, False),
    ("active", False, False),
])
def test_online_window_joins_only_its_pending_local_session(window, monkeypatch, tmp_path,
                                                            status, same_database, redirect):
    from rem_card.app import emergency_paths
    monkeypatch.setattr(emergency_paths, "resolve_emergency_root", lambda *args: str(tmp_path))
    metadata = tmp_path / "active" / "session" / "emergency_session.json"
    metadata.parent.mkdir(parents=True)
    remote = str(tmp_path / "remote.db")
    metadata.write_text(json.dumps({"status": status, "base_remote_db_path": remote}), encoding="utf-8")
    window.container.runtime_context = SimpleNamespace(
        mode="network", medical_db_path=remote if same_database else str(tmp_path / "other.db"))
    monitor = peers.NetworkEmergencyMonitor(window)
    monitor.timer.stop()
    monitor.poll()
    monitor.poll()
    assert window.close_calls == int(redirect)
    assert window.stack.isEnabled() is not redirect
    if redirect:
        assert QApplication.instance().property("remcard_restart_requested") is True


@pytest.mark.parametrize("draft", [False, True])
def test_network_redirect_does_not_freeze_a_window_that_cannot_close(window, monkeypatch, tmp_path, draft):
    from rem_card.app import emergency_paths
    monkeypatch.setattr(emergency_paths, "resolve_emergency_root", lambda *args: str(tmp_path))
    metadata = tmp_path / "active" / "session" / "emergency_session.json"
    metadata.parent.mkdir(parents=True)
    remote = str(tmp_path / "remote.db")
    metadata.write_text(json.dumps({"status": "active", "base_remote_db_path": remote}), encoding="utf-8")
    window.container.runtime_context = SimpleNamespace(mode="network", medical_db_path=remote)
    window._doctor_orders_widget_for_close = lambda: SimpleNamespace(has_drafts=lambda: draft)
    window.close = lambda: False
    monitor = peers.NetworkEmergencyMonitor(window)
    monitor.timer.stop()
    monitor.poll()
    assert not monitor.redirecting
    assert window.stack.isEnabled()
    assert QApplication.instance().property("remcard_restart_requested") is not True
