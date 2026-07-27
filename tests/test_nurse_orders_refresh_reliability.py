from __future__ import annotations

import os
from datetime import datetime
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

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
