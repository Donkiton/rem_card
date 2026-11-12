from datetime import datetime
import threading
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QLabel

from rem_card.services.read_coordinator import OrdersContext, OrdersRefreshCancelled
from rem_card.services.shift_service import ShiftService
from rem_card.ui.doctor_view.orders_widget import OrdersWidget
from rem_card.ui.doctor_view.order_features import snapshot_application
from rem_card.ui.nurse_view.components.nurse_orders_widget import NurseOrdersWidget
from rem_card.ui.shared.orders_snapshot_recovery import OrdersSnapshotRecovery


def until(predicate, timeout_ms=2000):
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(2)
    timer.timeout.connect(lambda: loop.quit() if predicate() else None)
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    timer.start()
    deadline.start(timeout_ms)
    if not predicate():
        loop.exec()
    timer.stop()
    deadline.stop()
    assert predicate()


def pump(milliseconds=40):
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


@pytest.fixture
def recovery_widget(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(OrdersSnapshotRecovery, "BACKOFF_MS", 10)
    widgets = []

    class Backend:
        def __init__(self):
            self.calls = []
            self.entered = threading.Event()
            self.release = threading.Event()
            self.block_first = False
            self.cancel_count = 0
            self.ignore_cancel = False
            self.active = 0
            self.max_active = 0

        def make_orders_context(self, **kwargs):
            return OrdersContext(**kwargs)

        def invalidate_tab(self, *_args, **_kwargs):
            pass

        def load_orders_tab(self, context, **options):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append((context, options))
            index = len(self.calls)
            try:
                if index == 1 and self.block_first:
                    self.entered.set()
                    assert self.release.wait(3)
                if index <= self.cancel_count or (not self.ignore_cancel and options["cancel_check"]()):
                    raise OrdersRefreshCancelled("synthetic cancelled orders read")
                return {"admission_id": context.admission_id, "shift_date": context.shift_date, "orders": []}
            finally:
                self.active -= 1

    def make(role):
        backend = Backend()
        service = SimpleNamespace(read_coordinator=backend, get_day_period=ShiftService.get_day_period, read_mode="live")
        cls = OrdersWidget if role == "doctor" else NurseOrdersWidget
        widget = cls(service, 17, datetime(2026, 11, 12, 8), defer_ui=True)
        if role == "doctor":
            widget._refresh_status_label = QLabel(widget)
        applied = []
        widget._apply_snapshot_data = lambda **payload: applied.append(payload) or True
        widget.show()
        app.processEvents()
        widgets.append((widget, backend))
        return widget, backend, applied

    yield make
    for widget, backend in widgets:
        backend.release.set()
        if not getattr(widget, "_is_closing", False):
            widget.shutdown(timeout_ms=0)
    until(lambda: all(backend.active == 0 for _, backend in widgets))


@pytest.mark.parametrize("source", ["click", "user", "refresh", "post_finalize"])
def test_watchdog_waits_for_old_worker_and_recovers_without_another_click(recovery_widget, source):
    widget, backend, applied = recovery_widget("doctor")
    backend.block_first = True
    widget._request_snapshot(source=source, priority="HIGH")
    until(backend.entered.is_set)
    old_worker = widget._snapshot_worker
    old_request = widget._active_request_id
    old_generation = widget._active_request_generation
    context = widget._build_orders_context()
    widget._on_post_finalize_snapshot_watchdog()
    assert widget._snapshot_worker is None and old_worker.isRunning()
    pump()
    assert len(backend.calls) == 1 and not applied
    backend.release.set()
    until(lambda: bool(applied))
    assert len(backend.calls) == 2 and backend.max_active == 1
    assert widget._snapshot_seq > old_generation
    assert backend.calls[-1][0].cache_key() == context.cache_key()
    widget._apply_snapshot({
        "seq": old_generation, "request_id": old_request, "generation": old_generation,
        "admission_id": 17, "shift_date": widget.shift_date, "context_key": context.cache_key(), "snapshot": {},
    })
    assert len(applied) == 1


def test_manual_clicks_after_watchdog_coalesce_behind_unfinished_worker(recovery_widget):
    widget, backend, applied = recovery_widget("doctor")
    backend.block_first = True
    widget._request_snapshot(source="click", priority="HIGH")
    until(backend.entered.is_set)
    widget._on_post_finalize_snapshot_watchdog()
    widget.hide()
    widget.show()
    pump()
    assert len(backend.calls) == 1 and not applied
    for _ in range(3):
        widget._request_snapshot(source="user", priority="HIGH")
    pump()
    assert len(backend.calls) == 1 and not applied
    backend.release.set()
    until(lambda: bool(applied))
    assert len(backend.calls) == 2 and backend.max_active == 1


@pytest.mark.parametrize("role", ["doctor", "nurse"])
def test_controlled_cancellation_recovers_with_a_new_generation(recovery_widget, role):
    widget, backend, applied = recovery_widget(role)
    backend.cancel_count = 1
    widget._request_snapshot(source="user", priority="HIGH")
    until(lambda: bool(applied))
    assert len(backend.calls) == 2 and backend.max_active == 1
    assert widget._snapshot_seq == 2


@pytest.mark.parametrize("role", ["doctor", "nurse"])
def test_recovery_limit_and_manual_retry(recovery_widget, role):
    widget, backend, applied = recovery_widget(role)
    backend.cancel_count = 100
    widget._request_snapshot(source="user", priority="HIGH")
    until(lambda: len(backend.calls) == 3 and widget._snapshot_worker is None)
    pump(70)
    assert len(backend.calls) == 3 and not applied
    if role == "doctor":
        assert "автоматически" in widget._refresh_status_label.text()
    else:
        assert widget._orders_load_state == "error"
    backend.cancel_count = 0
    widget._request_snapshot(source="user", priority="HIGH")
    until(lambda: bool(applied))
    assert len(backend.calls) == 4


@pytest.mark.parametrize("role", ["doctor", "nurse"])
@pytest.mark.parametrize("change", ["hide", "patient_roundtrip", "service", "close", "draft", "destroy"])
def test_pending_recovery_respects_owner_context_and_drafts(recovery_widget, monkeypatch, role, change):
    monkeypatch.setattr(OrdersSnapshotRecovery, "BACKOFF_MS", 100)
    widget, backend, applied = recovery_widget(role)
    backend.cancel_count = 1
    widget._request_snapshot(source="user", priority="HIGH")
    until(lambda: getattr(widget, "_orders_snapshot_recovery", None) is not None
          and widget._orders_snapshot_recovery._pending is not None)
    if change == "hide":
        widget.hide()
        widget.show()
    elif change == "patient_roundtrip":
        widget.set_context(admission_id=18, shift_date=widget.shift_date)
        widget.set_context(admission_id=17, shift_date=widget.shift_date)
    elif change == "service":
        widget.service = SimpleNamespace(read_coordinator=backend, read_mode="live")
    elif change == "close":
        widget.shutdown(timeout_ms=0)
    elif change == "destroy":
        widget._is_closing = True
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    else:
        widget._local_cell_draft_guard = True
    pump(160)
    assert len(backend.calls) == 1 and not applied


@pytest.mark.parametrize("change", ["patient_roundtrip", "hide_roundtrip", "close"])
def test_post_finalize_delayed_retry_does_not_survive_a_context_visit(recovery_widget, monkeypatch, change):
    widget, backend, applied = recovery_widget("doctor")
    monkeypatch.setattr(snapshot_application, "ORDERS_POST_FINALIZE_RETRY_BACKOFF_MS", 60)
    widget._post_finalize_retry_count = 1
    widget._schedule_post_finalize_retry()
    assert widget._post_finalize_retry_count == 2
    if change == "patient_roundtrip":
        widget.set_context(admission_id=18, shift_date=widget.shift_date)
        widget.set_context(admission_id=17, shift_date=widget.shift_date)
    elif change == "hide_roundtrip":
        widget.hide()
        widget.show()
    else:
        widget.shutdown(timeout_ms=0)
    pump(110)
    assert not backend.calls and not applied


def test_post_finalize_delayed_retry_runs_for_the_same_visible_visit(recovery_widget, monkeypatch):
    widget, backend, applied = recovery_widget("doctor")
    monkeypatch.setattr(snapshot_application, "ORDERS_POST_FINALIZE_RETRY_BACKOFF_MS", 60)
    widget._post_finalize_retry_count = 1
    widget._schedule_post_finalize_retry()
    until(lambda: bool(applied))
    assert len(backend.calls) == 1
    assert backend.calls[0][1]["source"] == "post_finalize"
    assert backend.calls[0][1]["force_refresh"] is True


@pytest.mark.parametrize("role", ["doctor", "nurse"])
@pytest.mark.parametrize("old_read_returned", [False, True])
def test_same_key_service_change_retires_old_success(recovery_widget, role, old_read_returned):
    widget, backend, applied = recovery_widget(role)
    backend.block_first = True
    backend.ignore_cancel = True
    widget._request_snapshot(source="click", priority="HIGH")
    until(backend.entered.is_set)
    old_worker = widget._snapshot_worker
    old_request = widget._active_request_id
    old_generation = widget._active_request_generation
    old_context = widget._current_context_key()
    old_payloads = []
    old_worker.succeeded.connect(old_payloads.append)
    if old_read_returned:
        backend.release.set()
        assert old_worker.wait(1000)
    new_backend = type(backend)()
    new_service = SimpleNamespace(
        read_coordinator=new_backend, get_day_period=ShiftService.get_day_period, read_mode="live",
    )
    widget.set_context(service=new_service, admission_id=17, shift_date=widget.shift_date)
    assert widget._current_context_key() == old_context
    assert widget._snapshot_worker is None
    assert widget._snapshot_seq > old_generation
    assert widget._retired_snapshot_worker_states[old_request]["state"] == "superseded"
    widget._request_snapshot(source="click", priority="HIGH")
    until(lambda: bool(applied))
    assert len(new_backend.calls) == 1 and len(applied) == 1
    backend.release.set()
    until(lambda: bool(old_payloads))
    widget._apply_snapshot(old_payloads[0])
    pump()
    assert len(applied) == 1 and len(new_backend.calls) == 1
