from __future__ import annotations

from datetime import datetime

from rem_card.services.remcard_facade import RemCardService


class _FakeOrderService:
    def __init__(self):
        self.calls = []

    def commit_local_draft(self, admission_id, shift_date, **kwargs):
        self.calls.append((admission_id, shift_date, kwargs))
        return {-1: 42}


def _make_service(build_snapshot):
    service = RemCardService.__new__(RemCardService)
    service._orders = _FakeOrderService()
    service.build_orders_snapshot = build_snapshot
    return service


def test_commit_local_order_draft_returns_authoritative_snapshot():
    shift_date = datetime(2026, 8, 5, 8, 0)
    build_calls = []

    def build_snapshot(admission_id, date, **kwargs):
        build_calls.append((admission_id, date, kwargs))
        return {
            "admission_id": admission_id,
            "shift_date": date,
            "only_committed": True,
            "orders": [],
            "admin_rows": [],
            "has_any_draft": False,
            "has_any_orders": False,
            "has_any_administrations": False,
            "change_id": 17,
        }

    service = _make_service(build_snapshot)

    result = service.commit_local_order_draft(
        7,
        shift_date,
        orders=[],
        admin_map={},
        dirty_admin_keys=set(),
        baseline_admin_map={},
    )

    assert result["order_id_map"] == {-1: 42}
    assert build_calls == [
        (7, shift_date, {"only_committed": True, "include_change_cursor": True})
    ]
    assert result["snapshot"]["change_id"] == 17
    assert result["snapshot"]["version"] == 17
    assert result["snapshot"]["source"] == "post_finalize"
    assert result["snapshot"]["load_strategy"] == "commit_snapshot"


def test_commit_local_order_draft_keeps_success_when_snapshot_build_fails():
    shift_date = datetime(2026, 8, 5, 8, 0)

    def build_snapshot(*_args, **_kwargs):
        raise RuntimeError("snapshot unavailable")

    service = _make_service(build_snapshot)

    result = service.commit_local_order_draft(
        7,
        shift_date,
        orders=[],
        admin_map={},
        dirty_admin_keys=set(),
        baseline_admin_map={},
    )

    assert result == {"order_id_map": {-1: 42}, "snapshot": None}
    assert len(service._orders.calls) == 1
