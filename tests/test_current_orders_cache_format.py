from types import SimpleNamespace

from rem_card.services import persistent_snapshot_cache
from rem_card.ui.shared.components.current_orders_widget import (
    CURRENT_ORDERS_CACHE_FORMAT_VERSION,
    CurrentNurseOrdersWidget,
)


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
