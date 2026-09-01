from copy import deepcopy
from datetime import datetime, timedelta
from types import MethodType, SimpleNamespace

import pytest

from rem_card.data.dto.remcard_dto import AdministrationDTO, OrderDTO
from rem_card.services.balance_calculator import BalanceCalculator
from rem_card.ui.doctor_view.doctor_remcard_widget import DoctorRemCardWidget
from rem_card.ui.doctor_view.orders_widget import OrdersWidget
from rem_card.ui.nurse_view.nurse_main_widget import NurseMainWidget
from rem_card.ui.nurse_view.components.nurse_orders_widget import NurseOrdersWidget
from rem_card.ui.shared.orders_balance_adapter import (
    apply_orders_widget_mark_overrides, project_balance_orders,
)


START = datetime(2026, 7, 13, 8)
END = START + timedelta(days=1)


def order(order_id=1, hours=(1, 2, 3), volume=100):
    return OrderDTO(
        id=order_id, admission_id=7, latin="Test fluid", comment=f"{volume} ml", is_committed=1,
        administrations=[AdministrationDTO(
            id=order_id * 10 + hour, order_id=order_id,
            planned_time=START + timedelta(hours=hour), is_committed=1,
        ) for hour in hours],
    )


def widget(orders, role="doctor"):
    model = SimpleNamespace(
        admission_id=7, shift_date=START, orders=deepcopy(orders),
        admin_map={(o.id, a.planned_time.isoformat()): deepcopy(a) for o in orders for a in o.administrations},
    )
    result = SimpleNamespace(
        model=model, has_drafts=lambda: False, _balance_mark_overrides={},
        _balance_mark_override_seq=0, _pending_admin_write_count=0,
    )
    cls = OrdersWidget if role == "doctor" else NurseOrdersWidget
    for name in ("balance_mark_overrides", "balance_mark_override_sequence", "acknowledge_balance_mark_overrides"):
        setattr(result, name, MethodType(getattr(cls, name), result))
    return result


def owner(role, orders):
    values = []
    layout = SimpleNamespace(
        orders_widget=widget(orders, role), current_admission_id=7,
        sector_3a=SimpleNamespace(update_values=lambda **kwargs: values.append(kwargs)),
    )
    result = SimpleNamespace(
        admission_id=7, _current_date=START, layout_manager=layout,
        _balance_runtime_cache={"start_dt": START, "end_dt": END, "orders": deepcopy(orders)},
        _balance_runtime_provisional=False,
        _balance_calculator_cls=BalanceCalculator,
        _local_oral_state_for_balance=lambda: None,
        _ensure_card_widgets_initialized=lambda: None,
        _sync_plan_card_ui_state=lambda: False,
        _bind_balance_widgets_if_ready=lambda: None,
        _is_orders_tab_active=lambda: True,
        _card_snapshot_cache={"patient": "unchanged", "version": 1}, _last_change_id=0,
        service=SimpleNamespace(get_day_period=lambda _date: (START, END)),
        _balance_snapshot_sync=SimpleNamespace(schedule=lambda *_args: None),
    )
    cls = DoctorRemCardWidget if role == "doctor" else NurseMainWidget
    name = "update_balance_data" if role == "doctor" else "_update_balance_calculations"
    calculate = MethodType(getattr(cls, name), result)
    setattr(result, name, calculate)
    result.apply_snapshot = MethodType(cls._apply_authoritative_balance_snapshot, result)
    return result, values, calculate


def _enable_local_draft(orders_widget, orders):
    orders_widget.model.orders = deepcopy(list(orders))
    orders_widget.model.admin_map = {
        (item.id, admin.planned_time.isoformat()): deepcopy(admin)
        for item in orders
        for admin in item.administrations
    }
    orders_widget._local_draft_dirty_order_ids = {item.id for item in orders}
    orders_widget._local_draft_dirty_admin_keys = set()
    orders_widget._local_deleted_orders = {}
    orders_widget.has_drafts = lambda: bool(orders_widget._local_draft_dirty_order_ids)


@pytest.mark.parametrize("role", ["doctor", "nurse"])
def test_three_administrations_count_fact_without_reducing_plan_across_tabs(role):
    saved = [order()]
    card, values, calculate = owner(role, saved)
    ow = card.layout_manager.orders_widget
    calculate()
    assert (values[-1]["total"], values[-1]["total_daily"]) == (0, 300)
    for count, admin in enumerate(saved[0].administrations, start=1):
        ow._balance_mark_overrides[admin.id] = {
            "mark": "nurse_executed", "actual_time": admin.planned_time,
            "sequence": count, "pending": True,
        }
        ow._balance_mark_override_seq = count
        for active in (True, False, True):
            card._is_orders_tab_active = lambda: active
            calculate()
            assert (values[-1]["total"], values[-1]["total_daily"]) == (count * 100, 300)
        # DB snapshot replaces the narrow overlay; stale table is not a source.
        admin.comment = "nurse_executed"
        admin.actual_time = admin.planned_time
        ow._balance_mark_overrides[admin.id]["pending"] = False
        card.apply_snapshot(
            {"balance_runtime": {"start_dt": START, "end_dt": END, "orders": deepcopy(saved)}, "change_id": count + 10},
            {"overlay_sequence": count},
        )
        assert not ow._balance_mark_overrides
        assert (values[-1]["total"], values[-1]["total_daily"]) == (count * 100, 300)
        assert card._card_snapshot_cache["patient"] == "unchanged"
        assert card._card_snapshot_cache["version"] == 1


@pytest.mark.parametrize("role", ["doctor", "nurse"])
def test_repeated_mark_and_failed_write_do_not_duplicate_fact(role):
    saved = [order()]
    card, values, calculate = owner(role, saved)
    ow = card.layout_manager.orders_widget
    for sequence in range(1, 4):
        ow._balance_mark_overrides[11] = {"mark": "nurse_executed", "sequence": sequence}
        calculate()
        assert values[-1]["total"] == 100
    ow._balance_mark_overrides.clear()  # failed write rolls back the overlay
    calculate()
    assert (values[-1]["total"], values[-1]["total_daily"]) == (0, 300)
    assert saved[0].administrations[0].comment != "nurse_executed"


@pytest.mark.parametrize("cls", [OrdersWidget, NurseOrdersWidget])
def test_snapshot_acknowledges_only_operations_committed_before_read(cls):
    ow = widget([order()], "doctor" if cls is OrdersWidget else "nurse")
    ow._balance_mark_overrides = {
        11: {"sequence": 1, "pending": False},
        12: {"sequence": 2, "pending": True},
    }
    ow._balance_mark_override_seq = 2
    read_sequence = ow.balance_mark_override_sequence()
    assert read_sequence == 1
    ow._balance_mark_overrides[12]["pending"] = False
    ow.acknowledge_balance_mark_overrides(read_sequence)
    assert set(ow._balance_mark_overrides) == {12}
    # A newer edit of the same administration also survives an old response.
    ow._balance_mark_overrides[11] = {"sequence": 3, "pending": True}
    ow.acknowledge_balance_mark_overrides(2)
    assert set(ow._balance_mark_overrides) == {11}


def test_earlier_doctor_commit_does_not_replace_newer_pending_mark():
    ow = widget([order()])
    notifications = []
    ow.balanceSnapshotRequired = SimpleNamespace(emit=lambda: notifications.append(True))
    ow._balance_mark_overrides[11] = {"sequence": 2, "pending": True, "mark": "nurse_not_executed"}
    # No index/model access is allowed for the superseded completion.
    OrdersWidget._apply_committed_order_mark(ow, None, SimpleNamespace(id=11), "nurse_executed", sequence=1)
    assert ow._balance_mark_overrides[11] == {"sequence": 2, "pending": True, "mark": "nurse_not_executed"}
    assert notifications == [True]


def test_doctor_draft_keeps_new_nurse_fact_and_other_doctor_order():
    original = [order()]
    ow = widget(original)
    ow.has_drafts = lambda: True
    ow._local_draft_dirty_order_ids = {1}
    ow.model.orders[0].comment = "200 ml"
    ow.model.orders[0].is_committed = 0
    fresh = deepcopy(original) + [order(2, hours=(4,), volume=500)]
    fresh[0].administrations[0].comment = "nurse_executed"
    effective = project_balance_orders(fresh, ow, 7, START)
    result = BalanceCalculator.calculate(effective, current_time=END, end_of_card=END, committed_orders=fresh)
    assert result["current"]["total"] == 100
    assert result["daily"]["total"] == 100 + 2 * 200 + 500
    assert ow.model.orders[0].comment == "200 ml"
    assert ow.model.admin_map[(1, (START + timedelta(hours=1)).isoformat())].comment != "nurse_executed"


def test_local_draft_deletion_keeps_fact_and_does_not_resurrect_order():
    saved = [order()]
    saved[0].administrations[0].comment = "nurse_executed"
    ow = widget(saved)
    ow.has_drafts = lambda: True
    ow._local_draft_dirty_order_ids = {1}
    ow._local_deleted_orders = {1: object()}
    ow.model.orders.clear()
    effective = project_balance_orders(saved, ow, 7, START)
    assert effective == []
    result = BalanceCalculator.calculate(effective, current_time=END, end_of_card=END, committed_orders=saved)
    assert (result["current"]["total"], result["daily"]["total"]) == (100, 100)


def test_overlay_never_changes_dose_structure_or_other_patients():
    saved = [order()]
    ow = widget(saved)
    ow._balance_mark_overrides = {11: {"mark": "nurse_executed"}}
    patched = apply_orders_widget_mark_overrides(saved, ow, 7, START)
    assert patched[0].comment == saved[0].comment
    assert len(patched[0].administrations) == 3
    assert saved[0].administrations[0].comment != "nurse_executed"
    assert apply_orders_widget_mark_overrides(saved, ow, 8, START) is None
    assert apply_orders_widget_mark_overrides(saved, ow, 7, END) is None


@pytest.mark.parametrize("role", ["doctor", "nurse"])
def test_remote_order_add_delete_and_execution_are_visible_with_stale_table(role):
    card, values, calculate = owner(role, [order()])
    fresh = [order(), order(2, hours=(4,), volume=500)]
    fresh[0].administrations[0].comment = "nurse_executed"
    for cursor, saved, expected in ((10, fresh, (100, 800)), (11, fresh[:1], (100, 300))):
        card.apply_snapshot(
            {"balance_runtime": {"start_dt": START, "end_dt": END, "orders": saved}, "change_id": cursor},
            {"overlay_sequence": 0},
        )
        assert (values[-1]["total"], values[-1]["total_daily"]) == expected


def test_post_finalize_committed_baseline_prevents_visual_balance_rollback():
    baseline = [order()]
    committed = [*baseline, order(2, hours=(4,), volume=500)]
    card, values, calculate = owner("doctor", baseline)
    scheduled = []
    card._balance_snapshot_sync = SimpleNamespace(schedule=scheduled.append)
    card._accept_committed_orders_balance_baseline = MethodType(
        DoctorRemCardWidget._accept_committed_orders_balance_baseline,
        card,
    )

    _enable_local_draft(card.layout_manager.orders_widget, committed)
    calculate()
    assert values[-1]["total_daily"] == 800

    card.layout_manager.orders_widget._local_draft_dirty_order_ids.clear()
    card._accept_committed_orders_balance_baseline(
        {
            "admission_id": 7,
            "shift_date": START,
            "change_id": 17,
            "source": "post_finalize",
            "orders": committed,
        }
    )
    calculate()

    assert values[-1]["total_daily"] == 800
    assert scheduled == [17]


def test_post_finalize_delete_baseline_does_not_resurrect_removed_volume():
    baseline = [order(), order(2, hours=(4,), volume=500)]
    committed = baseline[:1]
    card, values, calculate = owner("doctor", baseline)
    card._accept_committed_orders_balance_baseline = MethodType(
        DoctorRemCardWidget._accept_committed_orders_balance_baseline,
        card,
    )
    orders_widget = card.layout_manager.orders_widget
    orders_widget.model.orders = deepcopy(committed)
    orders_widget.model.admin_map = {
        (item.id, admin.planned_time.isoformat()): deepcopy(admin)
        for item in committed
        for admin in item.administrations
    }
    orders_widget._local_draft_dirty_order_ids = {2}
    orders_widget._local_draft_dirty_admin_keys = set()
    orders_widget._local_deleted_orders = {2: object()}
    orders_widget.has_drafts = lambda: bool(orders_widget._local_draft_dirty_order_ids)
    calculate()
    assert values[-1]["total_daily"] == 300

    orders_widget._local_draft_dirty_order_ids.clear()
    card._accept_committed_orders_balance_baseline(
        {
            "admission_id": 7,
            "shift_date": START,
            "change_id": 18,
            "source": "post_finalize",
            "orders": committed,
        }
    )
    calculate()

    assert values[-1]["total_daily"] == 300


def test_new_card_provisional_runtime_projects_unsaved_orders_immediately():
    card, values, calculate = owner("doctor", [])
    card._balance_runtime_cache = None
    card._initialize_provisional_balance_runtime = MethodType(
        DoctorRemCardWidget._initialize_provisional_balance_runtime,
        card,
    )
    card._initialize_provisional_balance_runtime(START, END)
    _enable_local_draft(
        card.layout_manager.orders_widget,
        [order(2, hours=(4,), volume=500)],
    )

    calculate()

    assert values[-1]["total"] == 0
    assert values[-1]["total_daily"] == 500
    assert card._balance_runtime_provisional is True


@pytest.mark.parametrize("cls", [DoctorRemCardWidget, NurseMainWidget])
def test_local_forced_orders_event_schedules_narrow_read_with_cursor(cls):
    scheduled, refreshes, calculations = [], [], []
    payload = {
        "forced": True, "last_change_id": 123,
        "changed_entities": ["administrations"],
        "sync_actions": {"balance_refresh": True},
    }
    card = SimpleNamespace(
        _is_closing=False, admission_id=7, _selection_mode="card", isVisible=lambda: True,
        _changed_entities_from_payload=lambda p: set(p["changed_entities"]),
        _invalidate_vitals_cache_from_payload=lambda *args: None,
        _payload_is_relevant=lambda p: True,
        _is_local_emergency_notice_payload=lambda *args: False,
        _is_local_orders_force_payload=lambda *args: True,
        _payload_force_sources=lambda p: ["doctor_order_mark:11"],
        _is_orders_tab_active=lambda: True,
        _refresh_current_orders_from_payload=lambda p: None,
        _schedule_balance_update=lambda: calculations.append(True),
        _balance_snapshot_sync=SimpleNamespace(schedule=scheduled.append),
        layout_manager=SimpleNamespace(
            current_admission_id=7,
            selection_stack=SimpleNamespace(currentIndex=lambda: 0),
            orders_widget=SimpleNamespace(handle_data_changes=lambda *a, **kw: refreshes.append(kw)),
        ),
    )
    cls._on_data_changes(card, payload)
    assert scheduled == [123]
    assert refreshes == [{"tab_active": True}]
    assert calculations == [True]


@pytest.mark.parametrize("cls", [DoctorRemCardWidget, NurseMainWidget])
def test_background_loader_uses_captured_service_not_new_patient_service(cls):
    calls = []
    captured = SimpleNamespace(build_balance_snapshot=lambda *a, **kw: calls.append((a, kw)) or {})
    owner = SimpleNamespace(service=None, remcard_service=None)
    cls._load_balance_snapshot_job(owner, {"context": (7, START, "live", "live", captured)})
    assert calls == [((7, START), {"include_change_cursor": True, "balance_only_committed": True})]
