"""Равномерные по времени непрерывные группы с сохранением порядка сценариев."""

from __future__ import annotations

from functools import lru_cache
import json
import math
from pathlib import Path


@lru_cache(maxsize=1)
def timing_estimates() -> dict[str, float]:
    payload = json.loads(Path(__file__).with_name("timing_estimates.json").read_text(encoding="utf-8"))
    if payload.get("schema") != 1 or not isinstance(payload.get("durations_sec"), dict):
        raise ValueError("Некорректный формат оценок времени safety-проверок")
    estimates = payload["durations_sec"]
    if any(not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 for value in estimates.values()):
        raise ValueError("Время safety-проверки должно быть конечным положительным числом")
    return estimates


def _groups_needed(weights: list[int], limit: int) -> int:
    groups, subtotal = 1, 0
    for weight in weights:
        if subtotal + weight > limit:
            groups += 1
            subtotal = 0
        subtotal += weight
    return groups


def partition_checks(checks: list, count: int, estimates: dict[str, float]) -> list[list]:
    if not checks or not 1 <= count <= len(checks):
        raise ValueError("Число групп должно быть от 1 до числа проверок")
    # Для нового реестра без измерений сохраняем прежнее равное деление.
    if not any(name in estimates for name, _ in checks):
        return [checks[len(checks) * i // count:len(checks) * (i + 1) // count] for i in range(count)]
    weights = [max(1, math.ceil(estimates.get(name, 1.0) * 1000)) for name, _ in checks]
    low, high = max(weights), sum(weights)
    while low < high:
        middle = (low + high) // 2
        if _groups_needed(weights, middle) <= count:
            high = middle
        else:
            low = middle + 1
    # Восстановление оптимального непрерывного разбиения справа налево.
    # Каждой оставшейся группе обязательно оставляем хотя бы один сценарий.
    end = len(checks)
    groups = []
    for remaining in range(count, 0, -1):
        start, subtotal = end, 0
        while start > remaining - 1 and subtotal + weights[start - 1] <= low:
            start -= 1
            subtotal += weights[start]
        groups.append(checks[start:end])
        end = start
    return list(reversed(groups))


def shard_execution_order(checks: list, count: int) -> list[int]:
    estimates = timing_estimates()
    groups = partition_checks(checks, count, estimates)
    return sorted(range(count), key=lambda index: (sum(estimates.get(name, 1.0) for name, _ in groups[index]), index), reverse=True)
