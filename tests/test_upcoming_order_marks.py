from datetime import datetime
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from rem_card.services import persistent_snapshot_cache
from rem_card.services.order_domain_service import NURSE_MARK_EXECUTED, NURSE_MARK_NOT_EXECUTED
from rem_card.ui.rem_card_sectors.sector_1a import Sector1a
from rem_card.ui.rem_card_sectors.sector_w1a import SectorW1a
from rem_card.ui.shared.components.current_orders_widget import CurrentNurseOrdersWidget
from rem_card.ui.shared.custom_message_box import CustomMessageBox


def _row(version=7, mark=""):
    return {
        "id": 17, "version": version,
        "planned_time": datetime.now().isoformat(),
        "comment": mark, "actual_time": datetime.now().isoformat() if mark else None,
        "latin": "Test", "admission_id": 1, "bed_number": 1,
        "patient_name": "Test patient", "order_id": 3,
    }


@pytest.fixture(params=["current", "w1a"])
def panel(request, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(persistent_snapshot_cache, "load_snapshot", lambda *_: None)
    monkeypatch.setattr(persistent_snapshot_cache, "schedule_store_snapshot", lambda *_, **__: None)
    writes = []
    service = SimpleNamespace(enqueue_write=lambda **kwargs: writes.append(kwargs))
    if request.param == "current":
        sector = Sector1a()
        widget = CurrentNurseOrdersWidget(service, sector, None)
        widget.admission_id = 1
        widget.shift_date = datetime.now()
        cards = widget.cards_1a

        def deliver(rows):
            widget._on_refresh_result({
                "request": {
                    "service": service, "admission_id": 1,
                    "shift_date": widget.shift_date, "generation": widget._refresh_generation,
                },
                "data": rows, "version": 10, "error": None,
            })
    else:
        sector = widget = SectorW1a(service, role="nurse", auto_initial_refresh=False, preparing=True)
        cards = widget.cards

        def deliver(rows):
            rows = [dict(row) for row in rows]
            for row in rows:
                row["expected_revision"] = row.pop("version")
            widget._apply_snapshot({"rows": rows, "change_id": 10, "content_hash": repr(rows)})

    now = [100.0]
    widget._mark_state._clock = lambda: now[0]
    refreshes = []
    widget.refresh_data = lambda **kwargs: refreshes.append(kwargs)
    deliver([_row()])
    assert 17 in cards
    try:
        yield SimpleNamespace(widget=widget, deliver=deliver, cards=cards,
                              writes=writes, now=now, refreshes=refreshes, app=app)
    finally:
        widget.shutdown()
        sector.deleteLater()


def _mark_and_confirm(panel):
    panel.widget.handle_status_change(17, NURSE_MARK_EXECUTED)
    assert 17 not in panel.cards
    assert len(panel.writes) == 1
    panel.writes[0]["on_success"](None)


def test_old_reply_cannot_resurrect_marked_order_during_grace_window(panel):
    _mark_and_confirm(panel)
    panel.now[0] += 7.9
    panel.deliver([_row(7)])
    assert 17 not in panel.cards
    assert panel.widget._get_pending_mark(17)["mark"] == NURSE_MARK_EXECUTED
    assert panel.widget._pending_mark_timer.isActive()
    assert panel.widget._mark_state.ttl_sec == 8.0


def test_newer_clear_is_visible_inside_grace_window_and_survives_old_replies(panel):
    _mark_and_confirm(panel)
    panel.now[0] += 1
    panel.deliver([_row(9, NURSE_MARK_NOT_EXECUTED)])
    assert 17 not in panel.cards
    assert panel.widget._get_pending_mark(17) is None
    panel.deliver([_row(10)])
    assert 17 in panel.cards
    for stale in (_row(7), _row(8, NURSE_MARK_EXECUTED), _row(9, NURSE_MARK_NOT_EXECUTED)):
        panel.deliver([stale])
        assert 17 in panel.cards
        row = panel.widget._all_data[0]
        assert row.get("expected_revision", row.get("version")) == 10


def test_newer_clear_arriving_before_success_callback_is_not_overwritten(panel):
    panel.widget.handle_status_change(17, NURSE_MARK_EXECUTED)
    panel.deliver([_row(10)])
    # A read alone cannot acknowledge a write still in flight.
    assert 17 not in panel.cards
    panel.writes[0]["on_success"](None)
    assert 17 in panel.cards
    assert panel.widget._get_pending_mark(17) is None


def test_optimistic_overlay_does_not_contaminate_received_or_persistent_data(panel):
    panel.widget.handle_status_change(17, NURSE_MARK_EXECUTED)
    incoming = _row()
    panel.deliver([incoming])
    assert incoming["comment"] == ""
    assert panel.widget._all_data[0]["comment"] == ""
    if isinstance(panel.widget, CurrentNurseOrdersWidget):
        cached = panel.widget._snapshot_cache[panel.widget._cache_key()]
        assert cached["data"][0]["comment"] == ""
    assert 17 not in panel.cards


def test_full_grace_window_starts_after_slow_write_completes(panel):
    panel.widget.handle_status_change(17, NURSE_MARK_EXECUTED)
    panel.now[0] += 12
    panel.deliver([_row(7)])
    assert 17 not in panel.cards
    panel.writes[0]["on_success"](None)
    panel.now[0] += 7.9
    assert panel.widget._get_pending_mark(17) is not None
    panel.now[0] += 0.2
    panel.widget._on_pending_mark_timeout()
    assert panel.widget._get_pending_mark(17) is None
    assert not panel.widget._pending_mark_timer.isActive()
    assert panel.refreshes[-1] == {"force": True}
    assert 17 not in panel.cards
    panel.deliver([_row(7)])
    assert 17 not in panel.cards
    panel.deliver([_row(10)])
    assert 17 in panel.cards


def test_write_error_restores_order_without_waiting_for_next_snapshot(panel, monkeypatch):
    warnings = []
    monkeypatch.setattr(CustomMessageBox, "warning", lambda *args: warnings.append(args))
    panel.widget.handle_status_change(17, NURSE_MARK_EXECUTED)
    panel.writes[0]["on_error"](RuntimeError("synthetic failure"))
    assert 17 in panel.cards
    assert panel.widget._get_pending_mark(17) is None
    assert not panel.widget._pending_mark_timer.isActive()
    assert panel.refreshes[-1] == {"force": True}
    assert len(warnings) == 1


def test_context_cleanup_stops_grace_timer_and_forgets_revision_watermarks(panel):
    _mark_and_confirm(panel)
    panel.widget._clear_pending_marks()
    assert not panel.widget._pending_mark_timer.isActive()
    assert not panel.widget._pending_marks
    panel.deliver([_row(7)])
    assert 17 in panel.cards


def test_shutdown_ignores_late_write_callback_and_stops_grace_timer(panel):
    panel.widget.handle_status_change(17, NURSE_MARK_EXECUTED)
    panel.widget.shutdown()
    panel.writes[0]["on_success"](None)
    assert not panel.widget._pending_mark_timer.isActive()
    assert not panel.widget._pending_marks


@pytest.mark.parametrize("result", ["on_success", "on_error"])
def test_previous_context_callback_cannot_confirm_or_undo_another_mark(panel, result):
    panel.widget.handle_status_change(17, NURSE_MARK_EXECUTED)
    previous_write = panel.writes[0]
    panel.widget._clear_pending_marks()
    panel.deliver([_row(7)])
    panel.widget.handle_status_change(17, NURSE_MARK_NOT_EXECUTED)
    previous_write[result](RuntimeError("old context") if result == "on_error" else None)
    pending = panel.widget._get_pending_mark(17)
    assert pending["mark"] == NURSE_MARK_NOT_EXECUTED
    assert pending["saved"] is False
    assert panel.widget._all_data[0]["comment"] == ""


def test_old_memory_snapshot_cannot_replace_a_newer_clear(panel):
    _mark_and_confirm(panel)
    panel.deliver([_row(10)])
    if isinstance(panel.widget, CurrentNurseOrdersWidget):
        cached = panel.widget._snapshot_cache[panel.widget._cache_key()]
        cached["data"] = [_row(8, NURSE_MARK_EXECUTED)]
        assert panel.widget._apply_cached_snapshot_if_available()
    else:
        panel.deliver([_row(8, NURSE_MARK_EXECUTED)])
    assert 17 in panel.cards


def test_grace_timer_signal_reloads_after_expiry(panel):
    _mark_and_confirm(panel)
    panel.now[0] += 8.1
    panel.widget._pending_mark_timer.timeout.emit()
    assert not panel.widget._pending_marks
    assert panel.refreshes[-1] == {"force": True}
    assert not panel.widget._pending_mark_timer.isActive()
