from __future__ import annotations

from copy import copy
from datetime import datetime, timedelta

from rem_card.data.dto.remcard_dto import AdministrationDTO
from rem_card.services.balance_calculator import BalanceCalculator
from rem_card.services.shift_service import ShiftService


def load_burn_infused_volume(service, admission_id: int, start: datetime, end: datetime) -> float:
    """Фактический объём из сохранённых назначений за [start, end).

    Используем тот же расчёт выполненных введений/цепочек, что и баланс карты.
    Не суммируем плановые объёмы и не читаем черновые изменения врача.
    Все операции с сервисом — только чтение.
    """
    if end <= start:
        return 0.0
    shift_start, shift_end = ShiftService.get_day_period(start)
    total = 0.0
    while shift_start < end:
        orders = [copy(order) for order in service.get_orders(
            admission_id, shift_start, only_committed=True
        )]
        order_map = {order.id: order for order in orders if order.id is not None}
        for order in orders:
            order.administrations = []
        if order_map:
            rows = service.get_latest_administrations_for_order_ids(
                order_ids=list(order_map),
                start_dt=shift_start,
                end_dt=shift_end,
                only_committed=True,
                include_deleted=False,
                include_cancelled=False,
            )
            for row in rows:
                data = dict(row)
                order = order_map.get(data["order_id"])
                if order is None:
                    continue
                planned = data["planned_time"]
                if not isinstance(planned, datetime):
                    planned = datetime.fromisoformat(str(planned))
                order.administrations.append(AdministrationDTO(
                    id=data["id"],
                    order_id=data["order_id"],
                    big_chain_id=data.get("big_chain_id"),
                    cell_role=data.get("cell_role", "single"),
                    planned_time=planned,
                    status=data.get("status", "planned"),
                    comment=data.get("comment", ""),
                    is_committed=data.get("is_committed", 1),
                    volume_ml=data.get("volume_ml", 0.0),
                ))
        # Полная цепочка нужна для определения её скорости; отсекаем период
        # внутри расчёта, а не удаляем её начальные отметки из выборки.
        interval_start = max(start, shift_start)
        interval_end = min(end, shift_end)
        hourly = BalanceCalculator.calculate_hourly_actual_input(
            orders,
            start_time=interval_start,
            current_time=interval_end - timedelta(microseconds=1),
            end_of_card=shift_end,
        )
        total += sum(sum(bucket.values()) for bucket in hourly.values())
        shift_start, shift_end = shift_end, shift_end + timedelta(days=1)
    return round(total, 1)
