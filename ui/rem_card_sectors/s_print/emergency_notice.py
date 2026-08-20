from __future__ import annotations

from datetime import datetime
from typing import Any, Optional


def _as_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T"))
    except Exception:
        return None


def notice_number_for_period(patient: Any, start_dt: datetime, end_dt: datetime) -> str:
    number = str(getattr(patient, "emergency_notice_number", "") or "").strip()
    if not number:
        return ""
    entered_at = _as_datetime(getattr(patient, "emergency_notice_entered_at", None))
    if not entered_at:
        return ""
    if start_dt <= entered_at < end_dt:
        return number
    return ""


def attach_notice_for_period(data: dict, patient: Any, start_dt: datetime, end_dt: datetime) -> None:
    number = notice_number_for_period(patient, start_dt, end_dt)
    if number:
        data["emergency_notice_number"] = number
    else:
        data.pop("emergency_notice_number", None)
