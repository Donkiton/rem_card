from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any


@dataclass(frozen=True)
class AnalyticsDatePeriod:
    """Inclusive calendar dates represented as a half-open SQL interval."""

    start_date: date
    end_date: date

    @property
    def start_inclusive(self) -> datetime:
        return datetime.combine(self.start_date, time.min)

    @property
    def end_exclusive(self) -> datetime:
        return datetime.combine(self.end_date + timedelta(days=1), time.min)

    @property
    def inclusive_end(self) -> datetime:
        return self.end_exclusive - timedelta(microseconds=1)

    @property
    def sql_bounds(self) -> tuple[str, str]:
        return self.start_sql, self.end_exclusive_sql

    @property
    def start_sql(self) -> str:
        return self.start_inclusive.strftime("%Y-%m-%d %H:%M:%S")

    @property
    def end_exclusive_sql(self) -> str:
        return self.end_exclusive.strftime("%Y-%m-%d %H:%M:%S")


def normalize_analytics_period(
    start_value: Any,
    end_value: Any,
    *,
    default_start: Any = None,
    default_end: Any = None,
) -> AnalyticsDatePeriod:
    """
    Normalize the analytics API's inclusive *calendar-date* semantics.

    Time components are intentionally ignored: analytics selectors operate on
    whole local calendar days.  SQL consumers use ``>= start`` and
    ``< next-day midnight`` so fractional seconds cannot fall through the end.
    """

    start_date = _coerce_date(start_value) or _coerce_date(default_start)
    end_date = _coerce_date(end_value) or _coerce_date(default_end)
    today = datetime.now().date()
    start_date = start_date or today
    end_date = end_date or today
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    return AnalyticsDatePeriod(start_date=start_date, end_date=end_date)


def parse_analytics_datetime(value: Any) -> datetime | None:
    """Parse the TEXT datetime forms used by current and legacy databases."""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    else:
        text = str(value or "").strip()
        if not text:
            return None
        normalized = text.replace("T", " ")
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            parsed = None
            for fmt in (
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M",
                "%Y-%m-%d",
                "%d.%m.%Y %H:%M:%S",
                "%d.%m.%Y %H:%M",
                "%d.%m.%Y",
            ):
                try:
                    parsed = datetime.strptime(normalized, fmt)
                    break
                except ValueError:
                    continue
            if parsed is None:
                return None
    # Database timestamps represent local wall time.  Keep comparisons stable
    # when a legacy row happens to carry an offset suffix.
    return parsed.replace(tzinfo=None)


def _coerce_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value or "").strip()
    if not text:
        return None
    parsed = parse_analytics_datetime(text)
    return parsed.date() if parsed is not None else None
