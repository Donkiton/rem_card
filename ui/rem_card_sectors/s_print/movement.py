from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any, Iterable, Optional


TERMINAL_STATUSES = {"TRANSFERRED", "DEAD"}
NON_MOVEMENT_STATUSES = {"CPR"}
NON_MOVEMENT_REASON_TYPES = {"cpr"}
STATUS_LABELS = {
    "ACTIVE": "В отделении",
    "OUT": "Вне отд.",
    "OR": "Оперблок",
    "CPR": "СЛР",
    "TRANSFERRED": "Переведен",
    "DEAD": "Умер",
}


@dataclass
class MovementInterval:
    status: str
    reason_text: Optional[str]
    start_time: datetime
    end_time: Optional[datetime]
    event_id: int


def movement_comment_text(status_value: Any, reason_text: Any) -> str:
    status_text = str(getattr(status_value, "value", status_value) or "")
    text = str(reason_text or "").strip()
    if status_text == "CPR" or text.startswith("{"):
        try:
            payload = json.loads(text)
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            text = str(payload.get("comment") or "").strip()
    if status_text == "DEAD" and text.startswith("Биологическая смерть:"):
        return ""
    return text


def _status_value(event: Any) -> str:
    status = getattr(event, "status", "")
    return str(getattr(status, "value", status) or "")


def _event_id(event: Any) -> int:
    value = getattr(event, "id", 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def is_non_movement_event(event: Any) -> bool:
    status = _status_value(event)
    reason_type = str(getattr(event, "reason_type", "") or "")
    return status in NON_MOVEMENT_STATUSES or reason_type in NON_MOVEMENT_REASON_TYPES


def _raw_intervals(events: Iterable[Any]) -> list[MovementInterval]:
    intervals: list[MovementInterval] = []
    for event in events or []:
        if is_non_movement_event(event):
            continue
        start_time = getattr(event, "start_time", None)
        if not start_time:
            continue
        intervals.append(
            MovementInterval(
                status=_status_value(event),
                reason_text=getattr(event, "reason_text", None),
                start_time=start_time,
                end_time=getattr(event, "end_time", None),
                event_id=_event_id(event),
            )
        )

    intervals.sort(key=lambda item: (item.start_time, item.event_id))
    return intervals


def _close_overlaps(intervals: list[MovementInterval]) -> list[MovementInterval]:
    normalized: list[MovementInterval] = []
    for index, interval in enumerate(intervals):
        end_time = interval.end_time
        if index + 1 < len(intervals):
            next_start = intervals[index + 1].start_time
            if next_start >= interval.start_time and (end_time is None or end_time > next_start):
                end_time = next_start
        if end_time is not None and end_time <= interval.start_time:
            continue
        normalized.append(
            MovementInterval(
                status=interval.status,
                reason_text=interval.reason_text,
                start_time=interval.start_time,
                end_time=end_time,
                event_id=interval.event_id,
            )
        )
    return normalized


def _intervals_touch(left: MovementInterval, right: MovementInterval) -> bool:
    return left.end_time is None or right.start_time <= left.end_time


def _merge_end(left: Optional[datetime], right: Optional[datetime]) -> Optional[datetime]:
    if left is None or right is None:
        return None
    return max(left, right)


def _merge_same_status(intervals: list[MovementInterval]) -> list[MovementInterval]:
    merged: list[MovementInterval] = []
    for interval in intervals:
        if merged and merged[-1].status == interval.status and _intervals_touch(merged[-1], interval):
            previous = merged[-1]
            previous.end_time = _merge_end(previous.end_time, interval.end_time)
            if not movement_comment_text(previous.status, previous.reason_text):
                previous.reason_text = interval.reason_text
            continue
        merged.append(interval)
    return merged


def normalized_movement_intervals(events: Iterable[Any]) -> list[MovementInterval]:
    return _merge_same_status(_close_overlaps(_raw_intervals(events)))


def _format_interval_time(interval: MovementInterval) -> str:
    start_time = interval.start_time
    end_time = interval.end_time
    time_str = start_time.strftime("%d.%m.%Y %H:%M")
    if end_time:
        time_str += f" - {end_time.strftime('%H:%M')}"
        if end_time.date() != start_time.date():
            time_str = start_time.strftime("%d.%m %H:%M") + " - " + end_time.strftime("%d.%m %H:%M")
    return time_str


def _format_day_interval_time(interval: MovementInterval, period_start: datetime, period_end: datetime) -> str:
    if interval.start_time < period_start:
        start_text = "..."
    else:
        start_text = interval.start_time.strftime("%d.%m.%Y %H:%M")

    if interval.end_time is None or interval.end_time > period_end:
        end_text = "..."
    else:
        end_text = interval.end_time.strftime("%H:%M")

    return f"{start_text} - {end_text}"


def _interval_overlaps_period(interval: MovementInterval, period_start: datetime, period_end: datetime) -> bool:
    interval_end = interval.end_time or period_end
    return interval.start_time < period_end and interval_end > period_start


def _has_period_change(intervals: Iterable[MovementInterval], period_start: datetime, period_end: datetime) -> bool:
    return any(period_start <= interval.start_time < period_end for interval in intervals)


def build_changed_day_movement_struct(
    events: Iterable[Any],
    period_start: datetime,
    period_end: datetime,
) -> list[dict[str, str]]:
    intervals = normalized_movement_intervals(events)
    if not _has_period_change(intervals, period_start, period_end):
        return []

    rows: list[dict[str, str]] = []
    for interval in intervals:
        if not _interval_overlaps_period(interval, period_start, period_end):
            continue
        rows.append(
            {
                "time": _format_day_interval_time(interval, period_start, period_end),
                "status": STATUS_LABELS.get(interval.status, interval.status),
                "desc": movement_comment_text(interval.status, interval.reason_text) or "—",
            }
        )
    return rows


def build_full_movement_struct(events: Iterable[Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for interval in normalized_movement_intervals(events):
        rows.append(
            {
                "time": _format_interval_time(interval),
                "status": STATUS_LABELS.get(interval.status, interval.status),
                "desc": movement_comment_text(interval.status, interval.reason_text) or "—",
            }
        )
        if interval.status in TERMINAL_STATUSES:
            break
    return rows


def first_terminal_movement_time(events: Iterable[Any]) -> Optional[datetime]:
    for interval in normalized_movement_intervals(events):
        if interval.status in TERMINAL_STATUSES:
            return interval.start_time
    return None


def movement_summary_date(periods: Iterable[tuple[Any, datetime, datetime]], events: Iterable[Any]) -> Any:
    period_list = list(periods)
    if not period_list:
        return None

    target_date = period_list[-1][0]
    terminal_time = first_terminal_movement_time(events)
    if terminal_time:
        for period_date, period_start, period_end in period_list:
            if period_start <= terminal_time < period_end:
                return period_date
    return target_date
