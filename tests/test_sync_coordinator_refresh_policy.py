from types import SimpleNamespace

from rem_card.services.sync_coordinator import SyncCoordinator
from rem_card.ui.doctor_view.doctor_remcard_widget import LOCAL_ORDER_FORCE_PREFIXES
from rem_card.ui.doctor_view.orders_widget import OrdersWidget
from rem_card.ui.main_window import MainWindow


def test_empty_forced_event_does_not_request_full_refresh():
    payload = SyncCoordinator.classify(
        {
            "forced": True,
            "force_sources": [],
            "changes": [],
            "changed_entities": [],
            "reason": "",
        }
    )

    assert payload["sync_actions"]["full_refresh_required"] is False
    assert payload["sync_actions"]["card_snapshot_required"] is False


def test_explicit_manual_refresh_still_requests_full_refresh():
    payload = SyncCoordinator.classify(
        {
            "forced": True,
            "force_sources": ["manual_refresh:doctor"],
            "changes": [],
        }
    )

    assert payload["sync_actions"]["full_refresh_required"] is True


def test_partial_change_rows_requests_full_refresh():
    payload = SyncCoordinator.classify(
        {
            "forced": True,
            "gap_detected": True,
            "reason": "partial_change_rows",
            "changes": [],
        }
    )

    assert payload["sync_actions"]["full_refresh_required"] is True
    assert payload["sync_actions"]["card_snapshot_required"] is True


def test_focus_only_wakes_monitor_without_forced_payload():
    calls = []

    class _DataService:
        def request_immediate_refresh(self, **kwargs):
            calls.append(kwargs)

    window = SimpleNamespace(
        _is_closing=False,
        container=SimpleNamespace(data_service=_DataService()),
    )

    MainWindow._trigger_refresh_on_focus(window)

    assert calls == [{"force_emit": False, "source": "window_focus"}]


def test_orders_finalize_is_silent_only_for_same_client_force_source():
    assert "orders_finalize:" in OrdersWidget._LOCAL_SILENT_FORCE_PREFIXES
    assert "orders_finalize:" in LOCAL_ORDER_FORCE_PREFIXES

    payload = {
        "forced": True,
        "force_sources": ["orders_finalize:5"],
        "changed_entities": ["orders", "administrations"],
        "last_change_id": 42,
    }
    fake_widget = SimpleNamespace(
        _LOCAL_SILENT_FORCE_PREFIXES=OrdersWidget._LOCAL_SILENT_FORCE_PREFIXES,
        _ORDERS_CHANGE_ENTITIES=OrdersWidget._ORDERS_CHANGE_ENTITIES,
        _last_polled_change_id=42,
        _payload_force_sources=lambda value: value["force_sources"],
    )

    assert OrdersWidget._is_local_silent_force_payload(
        fake_widget,
        payload,
        {"orders", "administrations"},
    )
    fake_widget._last_polled_change_id = 41
    assert not OrdersWidget._is_local_silent_force_payload(
        fake_widget,
        payload,
        {"orders", "administrations"},
    )
    fake_widget._last_polled_change_id = 42
    payload["force_sources"] = []
    assert not OrdersWidget._is_local_silent_force_payload(
        fake_widget,
        payload,
        {"orders", "administrations"},
    )
