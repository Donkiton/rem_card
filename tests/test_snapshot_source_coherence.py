from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from rem_card.services.remcard_facade import RemCardService
from rem_card.ui.shared.components.current_orders_widget import (
    CurrentNurseOrdersWidget,
)


class _FakeSnapshotDb:
    def __init__(self, cursor: int = 41):
        self.cursor = int(cursor)
        self.active = False
        self.calls = []

    @contextmanager
    def snapshot_read_scope(self, source: str, *, force_central: bool = False):
        self.calls.append((source, bool(force_central)))
        self.active = True
        try:
            yield self
        finally:
            self.active = False

    def current_snapshot_read_source(self) -> str:
        return "local_replica"

    def get_latest_change_id(self, **_kwargs) -> int:
        assert self.active
        return self.cursor


def _make_service(db: _FakeSnapshotDb) -> RemCardService:
    service = RemCardService.__new__(RemCardService)
    service.orders_dao = SimpleNamespace(db=db)
    service.data_service = None
    return service


def test_current_orders_snapshot_uses_one_source_and_its_cursor():
    db = _FakeSnapshotDb(cursor=17)
    service = _make_service(db)
    service._orders = SimpleNamespace(
        get_nurse_orders_data=lambda admission_id, shift_date: [
            {"id": 1, "admission_id": admission_id, "shift_date": shift_date}
        ]
    )
    service._lab_order_cards_for_admission = lambda *_args: [{"id": -2}]
    shift_date = datetime(2026, 8, 28, 8, 0)

    snapshot = service.build_current_nurse_orders_snapshot(7, shift_date)

    assert [row["id"] for row in snapshot["data"]] == [1, -2]
    assert snapshot["change_id"] == 17
    assert db.calls == [("build_current_nurse_orders_snapshot", False)]


def test_diet_widget_snapshot_uses_one_source_and_its_cursor():
    db = _FakeSnapshotDb(cursor=23)
    service = _make_service(db)
    service.list_diet_templates = lambda: ["template"]
    service.get_diet_plan = lambda *_args: "plan"
    service.get_oral_intake_events = lambda *_args: ["event"]
    shift_date = datetime(2026, 8, 28, 8, 0)

    snapshot = service.build_diet_intake_widget_snapshot(
        9,
        shift_date,
        include_templates=True,
    )

    assert snapshot["templates"] == ["template"]
    assert snapshot["plan"] == "plan"
    assert snapshot["events"] == ["event"]
    assert snapshot["change_id"] == 23
    assert db.calls == [("build_diet_intake_widget_snapshot", False)]


def test_current_orders_cache_keeps_snapshot_cursor_instead_of_later_monitor_cursor():
    widget = CurrentNurseOrdersWidget.__new__(CurrentNurseOrdersWidget)
    widget._snapshot_cache = OrderedDict()
    widget._cache_key = lambda: (7, "2026-08-28T08:00:00")
    widget._current_change_id = lambda: 99

    with patch(
        "rem_card.ui.shared.components.current_orders_widget."
        "persistent_snapshot_cache.schedule_store_snapshot"
    ) as schedule:
        CurrentNurseOrdersWidget._store_snapshot_cache(
            widget,
            [{"id": 1}],
            version=17,
        )

    assert widget._snapshot_cache[widget._cache_key()]["version"] == 17
    assert schedule.call_args.args[2]["version"] == 17
