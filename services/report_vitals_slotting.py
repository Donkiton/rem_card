from __future__ import annotations

from bisect import bisect_left
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Sequence, Tuple


VITAL_REPORT_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("hr", "pulse"),
    ("sys", "sys"),
    ("dia", "dia"),
    ("spo2", "spo2"),
    ("temp", "temp"),
    ("rr", "rr"),
    ("cvp", "cvp"),
)


def select_latest_vitals_by_report_hour(
    vitals: Iterable[Any],
    start_dt: datetime,
    end_dt: datetime,
    *,
    slots: int = 24,
) -> Dict[int, Any]:
    """
    Раскладывает vitals по часовым ячейкам отчета и выбирает в каждой ячейке
    самое свежее значение по timestamp.

    Правила окон (без "дыр"):
    - 1-я ячейка (08:00): [08:00, 08:31)
    - Промежуточные: [H-00:29, H+00:31)
      пример: для 11:00 это [10:31, 11:31)
    - Последняя ячейка (07:00): [06:31, end_dt)
      что покрывает значения до 07:59 включительно (при end_dt = 08:00)
    """
    valid: List[Any] = [
        v for v in vitals
        if getattr(v, "timestamp", None) is not None
    ]
    if not valid:
        return {}

    valid.sort(key=lambda x: x.timestamp)
    timestamps = [v.timestamp for v in valid]

    result: Dict[int, Any] = {}
    for i in range(slots):
        target_time = start_dt + timedelta(hours=i)

        if i == 0:
            slot_start = start_dt
            slot_end = min(end_dt, target_time + timedelta(minutes=31))
        elif i == (slots - 1):
            slot_start = target_time - timedelta(minutes=29)
            slot_end = end_dt
        else:
            slot_start = target_time - timedelta(minutes=29)
            slot_end = min(end_dt, target_time + timedelta(minutes=31))

        if slot_end <= slot_start:
            continue

        left = bisect_left(timestamps, slot_start)
        right = bisect_left(timestamps, slot_end)
        if right > left:
            result[i] = valid[right - 1]

    return result


def build_vitals_report_matrix(
    vitals: Iterable[Any],
    start_dt: datetime,
    end_dt: datetime,
    *,
    slots: int = 24,
    active_intervals: Sequence[Tuple[datetime, datetime]] | None = None,
    fields: Sequence[Tuple[str, str]] = VITAL_REPORT_FIELDS,
) -> Dict[int, Dict[str, Any]]:
    """
    Возвращает матрицу виталов для печати.

    Сначала сохраняются реальные записи, попавшие в часовые окна отчета.
    Затем пустые почасовые ячейки между двумя реальными измерениями
    заполняются линейно рассчитанными значениями на пересечении с часом.
    Значения не экстраполируются до первого/после последнего измерения и не
    протягиваются через разные ACTIVE-интервалы движения пациента.
    """
    valid: List[Any] = [
        v for v in vitals
        if getattr(v, "timestamp", None) is not None
    ]
    valid.sort(key=lambda x: x.timestamp)

    matrix: Dict[int, Dict[str, Any]] = {}
    selected_by_hour = select_latest_vitals_by_report_hour(valid, start_dt, end_dt, slots=slots)
    for i, chosen_v in selected_by_hour.items():
        row: Dict[str, Any] = {}
        for matrix_key, attr in fields:
            val = getattr(chosen_v, attr, None)
            if val is not None:
                row[matrix_key] = val
        if row:
            matrix[i] = row

    if not valid:
        return matrix

    normalized_active_intervals = _normalize_active_intervals(active_intervals)
    for matrix_key, attr in fields:
        field_points = [
            vital for vital in valid
            if getattr(vital, attr, None) is not None
        ]
        if not field_points:
            continue

        point_times = [vital.timestamp for vital in field_points]
        for i in range(slots):
            target_dt = start_dt + timedelta(hours=i)
            if target_dt < start_dt or target_dt >= end_dt:
                continue

            row = matrix.setdefault(i, {})
            if matrix_key in row:
                continue

            value = _interpolated_field_value(
                field_points,
                point_times,
                attr,
                matrix_key,
                target_dt,
                normalized_active_intervals,
            )
            if value is not None:
                row[matrix_key] = value

    return {index: row for index, row in matrix.items() if row}


def _normalize_active_intervals(
    intervals: Sequence[Tuple[datetime, datetime]] | None,
) -> List[Tuple[datetime, datetime]]:
    if not intervals:
        return []

    normalized: List[Tuple[datetime, datetime]] = []
    for start, end in intervals:
        if start is None or end is None:
            continue
        start_minute = start.replace(second=0, microsecond=0)
        end_minute = end.replace(second=0, microsecond=0)
        if end_minute < start_minute:
            continue
        normalized.append((start_minute, end_minute))

    normalized.sort(key=lambda item: item[0])
    merged: List[Tuple[datetime, datetime]] = []
    for start, end in normalized:
        if merged and start <= merged[-1][1] + timedelta(minutes=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _active_interval_index(
    moment: datetime,
    intervals: Sequence[Tuple[datetime, datetime]],
) -> int | None:
    if not intervals:
        return None
    minute = moment.replace(second=0, microsecond=0)
    for index, (start, end) in enumerate(intervals):
        if start <= minute <= end:
            return index
    return -1


def _same_active_interval(
    left: datetime,
    target: datetime,
    right: datetime,
    intervals: Sequence[Tuple[datetime, datetime]],
) -> bool:
    if not intervals:
        return True
    left_index = _active_interval_index(left, intervals)
    target_index = _active_interval_index(target, intervals)
    right_index = _active_interval_index(right, intervals)
    return left_index >= 0 and left_index == target_index == right_index


def _interpolated_field_value(
    field_points: Sequence[Any],
    point_times: Sequence[datetime],
    attr: str,
    matrix_key: str,
    target_dt: datetime,
    active_intervals: Sequence[Tuple[datetime, datetime]],
) -> Any:
    pos = bisect_left(point_times, target_dt)
    if pos < len(field_points) and point_times[pos] == target_dt:
        return getattr(field_points[pos], attr, None)
    if pos <= 0 or pos >= len(field_points):
        return None

    previous_vital = field_points[pos - 1]
    next_vital = field_points[pos]
    previous_dt = previous_vital.timestamp
    next_dt = next_vital.timestamp
    if next_dt <= previous_dt:
        return None
    if not _same_active_interval(previous_dt, target_dt, next_dt, active_intervals):
        return None

    previous_value = getattr(previous_vital, attr, None)
    next_value = getattr(next_vital, attr, None)
    if previous_value is None or next_value is None:
        return None
    if matrix_key == "cvp" and (previous_value == -1 or next_value == -1):
        return None

    try:
        start_value = float(previous_value)
        end_value = float(next_value)
    except (TypeError, ValueError):
        return None

    total_seconds = (next_dt - previous_dt).total_seconds()
    if total_seconds <= 0:
        return None
    ratio = (target_dt - previous_dt).total_seconds() / total_seconds
    value = start_value + (end_value - start_value) * ratio
    return _format_interpolated_value(matrix_key, value)


def _format_interpolated_value(matrix_key: str, value: float) -> Any:
    if matrix_key == "temp":
        return round(value, 1)
    if value >= 0:
        return int(value + 0.5)
    return int(value - 0.5)

