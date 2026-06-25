from __future__ import annotations

from datetime import datetime
from typing import Any

from rem_card.ui.patient_bed_management.bed_labels import is_recovery_bed


def should_auto_update_recovery_elapsed_time(
    patient: Any,
    reference_date: datetime | None,
    service: Any = None,
    *,
    read_only: bool = False,
    now: datetime | None = None,
) -> bool:
    if read_only or patient is None or reference_date is None:
        return False
    if not is_recovery_bed(getattr(patient, "bed_number", None)):
        return False

    now_dt = now or datetime.now()
    if service is not None and hasattr(service, "get_day_period"):
        try:
            current_start, current_end = service.get_day_period(now_dt)
            return bool(current_start <= reference_date < current_end)
        except Exception:
            pass

    try:
        return reference_date.date() == now_dt.date()
    except Exception:
        return False


def recovery_elapsed_reference_date(
    reference_date: datetime | None,
    *,
    auto_update: bool,
    now: datetime | None = None,
) -> datetime | None:
    if auto_update:
        return now or datetime.now()
    return reference_date
