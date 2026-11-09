import time
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from rem_card.ui.shared import orders_display_diagnostics as diagnostics


@pytest.fixture
def visible_orders(monkeypatch):
    app = QApplication.instance() or QApplication([])
    widget = QWidget()
    widget.show()
    app.processEvents()
    events = []
    monkeypatch.setattr(diagnostics, "record_metric", lambda name, value, **fields: events.append((name, value, fields)))
    yield widget, events
    widget.close()
    app.processEvents()


def _click(widget, *, role="nurse"):
    tabs = SimpleNamespace(_orders_click_started_at=time.perf_counter() - 0.01)
    diagnostics.begin_orders_display(widget, tabs, role=role)
    assert tabs._orders_click_started_at is None


def test_observe_first_display_and_continue_tracking_visible_changes(visible_orders):
    widget, events = visible_orders
    _click(widget)
    for cursor in (41, 42):
        diagnostics.observe_visible_orders_snapshot(
            widget, {"load_trace_id": "request-1"}, role="nurse", change_cursor=cursor,
        )
    assert [name for name, _, _ in events] == [
        "orders_snapshot_visible", "orders_click_to_data_ms", "orders_snapshot_visible",
    ]
    _, elapsed, details = events[1]
    assert elapsed >= 10
    assert details == {
        "role": "nurse", "status": "ready", "source": "snapshot", "change_cursor": 41,
        "trace_id": "request-1",
    }


def test_hidden_widget_finishes_click_and_ignores_late_snapshot(visible_orders):
    widget, events = visible_orders
    _click(widget)
    widget.hide()
    diagnostics.observe_visible_orders_snapshot(widget, {}, role="nurse", change_cursor=42)
    assert len(events) == 1
    assert events[0][2]["status"] == "cancelled"


def test_empty_but_ready_model_and_timeout_have_distinct_outcomes(visible_orders):
    widget, events = visible_orders
    widget.admission_id, widget.shift_date = 1, "date"
    widget.model = SimpleNamespace(admission_id=1, shift_date="date", orders=[])
    widget._snapshot_stale = False
    widget._last_applied_snapshot_signature = ("same-context",)
    _click(widget, role="doctor")
    diagnostics.finish_if_orders_already_visible(widget)
    _click(widget, role="doctor")
    widget._orders_display_probe.timer.timeout.emit()
    assert [event[2]["status"] for event in events] == ["ready", "timeout"]
