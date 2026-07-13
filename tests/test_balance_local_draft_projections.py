from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from rem_card.data.dto.remcard_dto import AdministrationDTO, OrderDTO
from rem_card.services.balance_calculator import BalanceCalculator


SHIFT_START = datetime(2026, 7, 13, 8, 0)
NOW = SHIFT_START + timedelta(hours=3)
SHIFT_END = SHIFT_START + timedelta(days=1)


def _fluid_order(
    volume_ml: float,
    administrations: list[AdministrationDTO],
    *,
    order_id: int = 1,
    committed: bool,
) -> OrderDTO:
    return OrderDTO(
        id=order_id,
        admission_id=7,
        latin="Test fluid",
        comment=f"{volume_ml} ml",
        is_committed=int(committed),
        administrations=administrations,
    )


def _admin(admin_id: int, hour: int, *, mark: str = "") -> AdministrationDTO:
    return AdministrationDTO(
        id=admin_id,
        order_id=1,
        planned_time=SHIFT_START + timedelta(hours=hour),
        comment=mark,
        is_committed=1,
    )


def _calculate(effective_orders, committed_orders):
    return BalanceCalculator.calculate(
        effective_orders,
        current_time=NOW,
        end_of_card=SHIFT_END,
        committed_orders=committed_orders,
    )


def test_draft_dose_edit_preserves_committed_fact_and_changes_only_forecast():
    committed = _fluid_order(
        500,
        [
            _admin(11, 1, mark="nurse_executed"),
            _admin(12, 6),
        ],
        committed=True,
    )
    effective = deepcopy(committed)
    effective.comment = "1000 ml"
    effective.is_committed = 0

    result = _calculate([effective], [committed])

    assert result["current"]["total"] == pytest.approx(500.0)
    assert result["daily"]["total"] == pytest.approx(1500.0)


def test_draft_delete_removes_future_plan_but_keeps_administered_volume():
    committed = _fluid_order(
        500,
        [
            _admin(11, 1, mark="nurse_executed"),
            _admin(12, 6),
        ],
        committed=True,
    )

    result = _calculate([], [committed])

    assert result["current"]["total"] == pytest.approx(500.0)
    assert result["daily"]["total"] == pytest.approx(500.0)


def test_new_local_draft_changes_forecast_without_changing_fact():
    draft = _fluid_order(
        750,
        [_admin(21, 6)],
        order_id=-1,
        committed=False,
    )
    draft.administrations[0].order_id = -1
    draft.administrations[0].id = -1
    draft.administrations[0].is_committed = 0

    result = _calculate([draft], [])

    assert result["current"]["total"] == pytest.approx(0.0)
    assert result["daily"]["total"] == pytest.approx(750.0)


def test_continuous_draft_edit_uses_old_rate_for_fact_and_new_rate_for_future():
    administrations = [_admin(30 + hour, hour) for hour in range(4)]
    for index, admin in enumerate(administrations):
        admin.big_chain_id = "chain-1"
        admin.cell_role = "start" if index == 0 else "body"
    administrations[0].comment = "nurse_executed"
    committed = _fluid_order(1200, administrations, committed=True)
    committed.duration_min = 240

    effective = deepcopy(committed)
    effective.comment = "2400 ml"
    effective.is_committed = 0

    result = _calculate([effective], [committed])

    # At 11:00 three of four hours have elapsed: 900 ml is committed fact,
    # while the remaining hour follows the new 600 ml/h draft rate.
    assert result["current"]["total"] == pytest.approx(900.0)
    assert result["daily"]["total"] == pytest.approx(1500.0)


def test_legacy_single_projection_behavior_is_unchanged():
    draft = _fluid_order(
        500,
        [_admin(11, 1, mark="nurse_executed")],
        committed=False,
    )

    result = BalanceCalculator.calculate(
        [draft],
        current_time=NOW,
        end_of_card=SHIFT_END,
    )

    assert result["current"]["total"] == pytest.approx(0.0)
    assert result["daily"]["total"] == pytest.approx(500.0)
