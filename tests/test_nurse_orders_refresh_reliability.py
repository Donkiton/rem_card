from __future__ import annotations

import os
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QEventLoop, QTimer

from rem_card.data.dto.remcard_dto import AdministrationDTO, OrderDTO
from rem_card.services.shift_service import ShiftService
from rem_card.services.read_coordinator import OrdersContext
from rem_card.ui.nurse_view.components.nurse_orders_widget import NurseOrdersWidget


class _Coordinator:
    def __init__(self):
        self.invalidations = []

    def make_orders_context(self, **kwargs):
        return OrdersContext(**kwargs)

    def invalidate_tab(self, context, *, reason):
        self.invalidations.append((context.cache_key(), reason))

    @staticmethod
    def get_cached_tab(_context):
        return None


def _make_widget():
    app = QApplication.instance() or QApplication([])
    del app
    coordinator = _Coordinator()
    service = SimpleNamespace(
        read_coordinator=coordinator,
        read_mode="live",
    )
    shift = datetime(2026, 7, 27, 16, 37, 29)
    widget = NurseOrdersWidget(
        service=service,
        admission_id=164,
        shift_date=shift,
        defer_ui=True,
    )
    return widget, coordinator, shift


def test_orders_context_uses_stable_shift_start():
    first = OrdersContext(
        source_db="live",
        admission_id=164,
        shift_date=datetime(2026, 7, 27, 16, 37, 29),
        role="nurse",
        mode="live",
        variant="committed",
    )
    second = OrdersContext(
        source_db="live",
        admission_id=164,
        shift_date=datetime(2026, 7, 27, 16, 48, 12),
        role="nurse",
        mode="live",
        variant="committed",
    )

    assert first.shift_date == datetime(2026, 7, 27, 8, 0)
    assert first.cache_key() == second.cache_key()


def test_hidden_tab_change_is_reloaded_when_tab_is_shown():
    widget, coordinator, shift = _make_widget()
    requests = []
    try:
        widget.handle_data_changes(
            {
                "forced": True,
                "changed_entities": ["orders"],
            },
            tab_active=False,
        )
        widget._flush_change_batch()

        assert widget._snapshot_stale
        assert widget._deferred_change_reload
        assert coordinator.invalidations

        widget.model = SimpleNamespace(
            admission_id=164,
            shift_date=shift,
            orders=[],
        )
        widget._request_snapshot = lambda **kwargs: requests.append(kwargs)
        widget.ensure_ready_for_show()

        assert requests == [
            {
                "force": True,
                "source": "user",
                "priority": "HIGH",
                "invalidate_reason": "deferred_hidden_tab_change",
            }
        ]
    finally:
        widget.shutdown()


def test_pending_nurse_mark_does_not_block_whole_snapshot():
    widget, _coordinator, _shift = _make_widget()
    try:
        widget._pending_admin_write_count = 1
        widget._pending_admin_ids.add(77)

        handled = widget._try_apply_admin_only_snapshot(
            snapshot={"source": "refresh", "change_id": 10},
            admission_id=164,
            known_change_id=9,
            snapshot_change_id=10,
            current_context_key=widget._current_context_key(),
            snapshot_signature=("snapshot", 10),
        )

        assert handled is False
    finally:
        widget.shutdown()


def _wait_for_orders(predicate):
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: loop.quit() if predicate() else None)
    timer.start()
    QTimer.singleShot(3000, loop.quit)
    if not predicate():
        loop.exec()
    timer.stop()
    assert predicate()


@pytest.fixture
def loading_orders():
    app = QApplication.instance() or QApplication([])

    class Coordinator(_Coordinator):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()
            self.error = False
            self.orders = []

        def load_orders_tab(self, context, **_kwargs):
            self.entered.set()
            assert self.release.wait(3)
            if self.error:
                raise RuntimeError("synthetic orders load failure")
            return {
                "admission_id": context.admission_id, "shift_date": context.shift_date,
                "orders": self.orders, "admin_rows": [], "change_id": 1,
                "has_any_orders": bool(self.orders), "has_any_administrations": False,
            }

    coordinator = Coordinator()
    service = SimpleNamespace(
        read_coordinator=coordinator, read_mode="live", get_day_period=ShiftService.get_day_period,
    )
    widget = NurseOrdersWidget(service, 164, datetime(2026, 7, 27, 8))
    widget.resize(800, 260)
    widget.show()
    app.processEvents()
    try:
        yield widget, coordinator
    finally:
        coordinator.release.set()
        worker = widget._snapshot_worker
        if worker is not None:
            assert worker.wait(2000)
        app.processEvents()
        widget.shutdown()
        widget.hide()


@pytest.mark.parametrize("result", ["ready", "empty", "error"])
def test_first_orders_load_shows_loading_then_explicit_result(loading_orders, result):
    widget, coordinator = loading_orders
    coordinator.error = result == "error"
    if result == "ready":
        coordinator.orders = [OrderDTO(id=1, admission_id=164, latin="Synthetic", is_committed=1)]
    widget.ensure_ready_for_show()
    _wait_for_orders(coordinator.entered.is_set)
    assert widget.orders_status_label.isVisible()
    assert widget.orders_status_label.text() == "Загрузка назначений…"
    assert not widget.table_view.isVisible()
    ticks = []
    QTimer.singleShot(0, lambda: ticks.append(True))
    _wait_for_orders(lambda: bool(ticks))
    coordinator.release.set()
    _wait_for_orders(lambda: widget._snapshot_worker is None)
    assert widget._orders_load_state == result
    assert widget.table_view.isVisible() == (result == "ready")
    assert widget.orders_status_label.isVisible() == (result != "ready")
    if result == "error":
        coordinator.error = False
        widget.ensure_ready_for_show()
        _wait_for_orders(lambda: widget._snapshot_worker is None)
        assert widget._orders_load_state == "empty"


def test_orders_refresh_preserves_pending_mark_and_patient_switch_hides_previous_rows(loading_orders):
    widget, coordinator = loading_orders
    order = OrderDTO(id=1, admission_id=164, latin="Synthetic", is_committed=1)
    coordinator.orders = [order]
    coordinator.release.set()
    widget.ensure_ready_for_show()
    _wait_for_orders(lambda: widget._snapshot_worker is None)
    slot = (1, widget.shift_date.isoformat())
    widget.model.admin_map[slot] = AdministrationDTO(id=77, order_id=1, status="executed", is_committed=1)
    widget._pending_admin_ids.add(77)
    widget._pending_admin_write_count = 1
    coordinator.release.clear()
    coordinator.entered.clear()
    widget.request_refresh(force=True)
    _wait_for_orders(coordinator.entered.is_set)
    assert widget.table_view.isVisible()
    assert widget.model.admin_map[slot].status == "executed"
    coordinator.release.set()
    _wait_for_orders(lambda: widget._snapshot_worker is None)
    assert widget.model.admin_map[slot].id == 77
    assert widget.model.admin_map[slot].status == "executed"

    coordinator.release.clear()
    coordinator.entered.clear()
    widget.request_refresh(force=True)
    _wait_for_orders(coordinator.entered.is_set)
    previous = widget._snapshot_worker
    widget.set_context(admission_id=165, shift_date=widget.shift_date)
    assert widget.model.orders == [] and not widget.table_view.isVisible()
    coordinator.release.set()
    assert previous.wait(2000)
    QApplication.processEvents()
    assert widget.model.admission_id == 165 and widget.model.orders == []
    assert widget._orders_load_state == "idle"
