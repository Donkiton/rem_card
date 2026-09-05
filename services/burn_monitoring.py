from __future__ import annotations

from copy import copy
from datetime import datetime, timedelta
from math import isfinite

from rem_card.data.dto.remcard_dto import AdministrationDTO
from rem_card.services.balance_calculator import BalanceCalculator
from rem_card.services.shift_service import ShiftService


def burn_period_bounds(injury: datetime, mode: str, as_of: datetime, card_start: datetime) -> tuple[datetime, datetime]:
    """Период вычитания введённого объёма, не зависящий от смены карты."""
    from rem_card.services.burn_infusion_calculator import MODE_FIRST_24H, MODE_DAY_2_3, MODE_POST_SHOCK

    elapsed = (as_of - injury).total_seconds() / 3600
    if elapsed < 0:
        raise ValueError("Время травмы позже времени данных мониторинга.")
    if mode == MODE_POST_SHOCK:
        return card_start, card_start + timedelta(days=1)
    if mode == MODE_FIRST_24H:
        if elapsed >= 24:
            raise ValueError("Первые 24 часа завершены. Выберите режим 2–3-х суток.")
        return injury, injury + timedelta(days=1)
    if mode == MODE_DAY_2_3:
        if not 24 <= elapsed < 72:
            raise ValueError("Режим 2–3-х суток доступен от 24 до 72 часов после травмы.")
        start = injury + timedelta(days=1 if elapsed < 48 else 2)
        return start, start + timedelta(days=1)
    raise ValueError("Неизвестный режим расчёта.")


def load_burn_oral_volume(service, admission_id: int, start: datetime, end: datetime) -> float:
    """Только фактическое оральное/энтеральное введение, без планов питания."""
    if end <= start:
        return 0.0
    total = 0.0
    shift_start, shift_end = ShiftService.get_day_period(start)
    while shift_start < end:
        for event in service.get_oral_intake_events(admission_id, shift_start):
            if start <= event.event_time < end:
                amount = float(event.amount_ml)
                if not isfinite(amount) or amount < 0:
                    raise ValueError("Некорректный объём энтерального введения.")
                total += amount
        shift_start, shift_end = shift_end, shift_end + timedelta(days=1)
    if not isfinite(total):
        raise ValueError("Суммарный объём энтерального введения слишком велик.")
    return round(total, 1)


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
    if not isfinite(total) or total < 0:
        raise ValueError("Некорректный объём выполненных назначений.")
    return round(total, 1)
