from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rem_card.data.dto.remcard_dto import OrderDTO
from rem_card.services.burn_monitoring import load_burn_infused_volume
from rem_card.services.order_service import OrderService
from rem_card.services.shift_service import ShiftService
from rem_card.ui.shared.patient_calculator_context import build_burn_context, burn_recent_diuresis


START = datetime(2026, 9, 4, 8)
NOW = START + timedelta(hours=5, minutes=30)


@pytest.fixture
def service():
    # Реальный SQL выбора последних версий отметок; только временная БД в памяти.
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE orders (id INTEGER PRIMARY KEY, admission_id INTEGER, status TEXT);
        CREATE TABLE administrations (
            id INTEGER PRIMARY KEY, order_id INTEGER, planned_time TEXT,
            big_chain_id TEXT, cell_role TEXT DEFAULT 'single',
            status TEXT DEFAULT 'planned', comment TEXT DEFAULT '',
            is_committed INTEGER DEFAULT 1
        );
        INSERT INTO orders VALUES (1, 7, 'active'), (2, 8, 'active');
    """)
    def fetch_all(query, params, **kwargs):
        return db.execute(query, params).fetchall()

    orders_service = OrderService(SimpleNamespace(db=SimpleNamespace(fetch_all_remcard=fetch_all)))
    order = OrderDTO(id=1, admission_id=7, comment="250 ml", is_committed=1, created_at=START)
    fluids = []
    state = SimpleNamespace(db=db, orders=[order], fluids=fluids)

    def get_orders(admission_id, shift, *, only_committed):
        assert only_committed
        lower, upper = ShiftService.get_day_period(shift)
        return [item for item in state.orders if item.admission_id == admission_id
                and item.is_committed and lower <= item.created_at < upper]

    def get_fluids(admission_id, lower, upper):
        assert admission_id == 7
        return [item for item in fluids if lower <= item.timestamp < upper]

    state.get_orders = get_orders
    state.get_latest_administrations_for_order_ids = orders_service.get_latest_admin_rows_for_order_ids
    state.fluid_service = SimpleNamespace(get_fluids_in_bounds=get_fluids)
    yield state
    db.close()


def add_mark(service, hour, *, mark="nurse_executed", status="planned", committed=1, order_id=1,
             role="single", chain=None):
    service.db.execute(
        """INSERT INTO administrations
        (order_id, planned_time, comment, status, is_committed, cell_role, big_chain_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (order_id, (START + timedelta(hours=hour)).isoformat(), mark, status, committed, role, chain),
    )


def test_only_250_of_750_planned_is_prefilled(service):
    add_mark(service, 1)
    add_mark(service, 3, mark="")
    add_mark(service, 7, mark="")
    context = build_burn_context(service, 7, {}, now=NOW)
    assert context["infused_ml"] == 250
    assert "04.09 08:00–04.09 13:30" in context["infused_source"]
    assert service.orders[0].administrations == []  # Не меняем объект карты.
    add_mark(service, 3)  # Новая сохранённая версия выполнения.
    assert build_burn_context(service, 7, {}, now=NOW)["infused_ml"] == 500


@pytest.mark.parametrize("status,mark", [
    ("deleted", "nurse_executed"), ("cancelled", "nurse_executed"),
    ("planned", "nurse_not_executed"), ("planned", ""),
])
def test_latest_cancelled_deleted_or_cleared_mark_is_not_counted(service, status, mark):
    add_mark(service, 1)
    add_mark(service, 1, status=status, mark=mark)
    assert load_burn_infused_volume(service, 7, START, NOW) == 0


def test_other_patient_future_previous_day_and_drafts_are_excluded(service):
    add_mark(service, 1, order_id=2)
    add_mark(service, -1)
    add_mark(service, 8)
    add_mark(service, 2, committed=0)
    assert load_burn_infused_volume(service, 7, START, NOW) == 0


def test_doctor_draft_does_not_erase_confirmed_execution(service):
    add_mark(service, 1)
    add_mark(service, 1, status="deleted", committed=0)
    assert load_burn_infused_volume(service, 7, START, NOW) == 250


def test_continuous_infusion_uses_completed_fraction_not_entire_plan(service):
    service.orders[0].comment = "750 ml"
    service.orders[0].duration_min = 180
    add_mark(service, 1, role="start", chain="a")
    add_mark(service, 2, mark="", role="body", chain="a")
    add_mark(service, 3, mark="", role="end", chain="a")
    assert load_burn_infused_volume(service, 7, START, START + timedelta(hours=2)) == 250


def test_bounds_of_selected_card_and_0800_rollover(service):
    add_mark(service, 1)
    assert build_burn_context(service, 7, {}, now=START - timedelta(minutes=1))["infused_ml"] == 0
    assert build_burn_context(service, 7, {}, now=START + timedelta(days=1))["infused_ml"] == 0
    assert build_burn_context(service, 7, {}, shift_date=START,
                              now=START + timedelta(days=1))["infused_ml"] == 250


@pytest.mark.parametrize("values,expected", [
    ([None, 500, 100], 200), ([200, 500, 100], 266.7),
    ([None, None, 100], 33.3), ([None, None, None], 0), ([0, 0, 0], 0),
])
def test_missing_urine_hours_are_zero_not_missing_average(service, values, expected):
    for index, value in enumerate(values):
        if value is not None:
            service.fluids.append(SimpleNamespace(
                timestamp=NOW - timedelta(hours=2.5-index), urine=value
            ))
    result = burn_recent_diuresis(service, 7, now=NOW)
    assert result["urine_average_3h_ml"] == expected
    assert result["urine_last_hour_ml"] == (values[-1] or 0)


def test_diuresis_read_failure_does_not_hide_infused_volume(service):
    add_mark(service, 1)
    def fail(*args, **kwargs):
        raise RuntimeError("read failed")
    service.fluid_service.get_fluids_in_bounds = fail
    result = build_burn_context(service, 7, {}, now=NOW)
    assert result["infused_ml"] == 250
    assert "urine_average_3h_ml" not in result


def test_urine_outside_last_three_hours_is_not_counted(service):
    for offset, value in ((-3.5, 900), (-1.5, 500), (-0.5, 100), (0.5, 700)):
        service.fluids.append(SimpleNamespace(timestamp=NOW + timedelta(hours=offset), urine=value))
    assert burn_recent_diuresis(service, 7, now=NOW) == {
        "urine_last_hour_ml": 100, "urine_average_3h_ml": 200,
    }


def test_order_read_failure_is_not_reported_as_zero(service):
    def fail(*args, **kwargs):
        raise RuntimeError("read failed")
    service.get_orders = fail
    result = build_burn_context(service, 7, {}, now=NOW)
    assert result["infused_load_failed"]
    assert "infused_ml" not in result
    assert "вручную" in result["infused_source"]
