from types import SimpleNamespace
from datetime import datetime

from PySide6.QtCore import QEvent, Signal
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from rem_card.services import persistent_snapshot_cache
from rem_card.ui.rem_card_sectors.sector_1a import Sector1a
from rem_card.ui.shared.components.current_orders_widget import (
    CURRENT_ORDERS_CACHE_FORMAT_VERSION,
    CurrentNurseOrdersWidget,
)


def _application():
    return QApplication.instance() or QApplication([])


class _TestOrderCard(QWidget):
    statusChanged = Signal(int, str)

    def __init__(self, data, parent=None):
        super().__init__(parent)
        self.data = dict(data)
        self.refresh_count = 0

    def update_data(self, data):
        self.data = dict(data)

    def refresh_time_state(self):
        self.refresh_count += 1


def _visible_order(order_id):
    return {
        "id": order_id,
        "planned_time": datetime.now().isoformat(),
        "latin": "Test",
    }


def test_current_orders_cache_rejects_snapshot_without_format_version():
    assert not CurrentNurseOrdersWidget._is_cache_snapshot_compatible(
        {"version": 10, "data": [{"id": 1}]}
    )


def test_current_orders_cache_accepts_current_format_version():
    assert CurrentNurseOrdersWidget._is_cache_snapshot_compatible(
        {
            "cache_format_version": CURRENT_ORDERS_CACHE_FORMAT_VERSION,
            "version": 10,
            "data": [{"id": 1}],
        }
    )


def test_current_orders_cache_discards_stale_in_memory_snapshot(monkeypatch):
    key = (7, "2026-07-15T08:00:00")
    snapshot_cache = {key: {"version": 10, "data": [{"id": 1}]}}
    deleted = []
    widget = SimpleNamespace(
        _snapshot_cache=snapshot_cache,
        _cache_key=lambda: key,
        _is_cache_snapshot_compatible=CurrentNurseOrdersWidget._is_cache_snapshot_compatible,
    )
    monkeypatch.setattr(
        persistent_snapshot_cache,
        "delete_snapshot",
        lambda namespace, cache_key: deleted.append((namespace, cache_key)) or True,
    )

    applied = CurrentNurseOrdersWidget._apply_cached_snapshot_if_available(widget)

    assert not applied
    assert key not in snapshot_cache
    assert deleted == [("current_orders", key)]


def test_current_orders_recreates_card_if_qt_deleted_cached_child(monkeypatch):
    app = _application()
    monkeypatch.setattr(
        "rem_card.ui.shared.components.current_orders_widget.NurseOrderCard",
        _TestOrderCard,
    )
    sector = Sector1a()
    manager = CurrentNurseOrdersWidget(None, sector, None)
    manager._all_data = [_visible_order(11)]
    manager._render_from_cache()
    deleted_card = manager.cards_1a[11]

    deleted_card.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
    assert not isValid(deleted_card)

    manager._render_from_cache()

    assert 11 in manager.cards_1a
    assert manager.cards_1a[11] is not deleted_card
    assert isValid(manager.cards_1a[11])


def test_current_orders_stops_timer_and_drops_cards_with_sector(monkeypatch):
    app = _application()
    monkeypatch.setattr(
        "rem_card.ui.shared.components.current_orders_widget.NurseOrderCard",
        _TestOrderCard,
    )
    sector = Sector1a()
    manager = CurrentNurseOrdersWidget(None, sector, None)
    manager._all_data = [_visible_order(12)]
    manager._render_from_cache()
    manager._time_timer.start(60_000)
    late_callback = manager._on_mark_write_success

    sector.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()

    assert manager._is_shutting_down is True
    assert not isValid(manager)
    assert manager.cards_1a == {}
    assert manager.sector_1a is None
    manager._render_from_cache()
    late_callback(12)


def test_current_orders_real_cards_survive_patient_context_switch_and_shutdown(monkeypatch):
    app = _application()
    shift_date = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)

    class Service:
        @staticmethod
        def build_current_nurse_orders_snapshot(admission_id, _shift_date):
            return {
                "change_id": int(admission_id),
                "data": [_visible_order(100 + int(admission_id))],
            }

    monkeypatch.setattr(persistent_snapshot_cache, "load_snapshot", lambda *_args: None)
    monkeypatch.setattr(persistent_snapshot_cache, "schedule_store_snapshot", lambda *_args, **_kwargs: None)
    sector = Sector1a()
    manager = CurrentNurseOrdersWidget(Service(), sector, None)

    manager.set_context(1, shift_date)
    first_card = manager.cards_1a[101]
    assert isValid(first_card.lbl_signal)
    assert sector.content_layout.indexOf(first_card) >= 0

    manager.set_context(2, shift_date)
    second_card = manager.cards_1a[102]
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()

    assert not isValid(first_card)
    assert isValid(second_card)
    assert isValid(second_card.lbl_signal)
    assert sector.content_layout.indexOf(second_card) >= 0

    sector.show()
    app.processEvents()
    assert manager.isHidden()
    assert second_card.isVisible()
    assert second_card.lbl_signal.isVisible()

    late_success = manager._on_mark_write_success
    manager.shutdown()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()

    assert manager._is_shutting_down is True
    assert manager.cards_1a == {}
    assert not isValid(second_card)
    late_success(102)
    manager._render_from_cache()
