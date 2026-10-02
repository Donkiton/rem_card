from __future__ import annotations

from datetime import datetime, timedelta
import re


from rem_card.services.operblock_service import (
    OperBlockService,
)

from rem_card.ui.operblock_view.operblock_visual_primitives import (
    OPERBLOCK_INITIAL_CHART_HOURS,
    OPERBLOCK_VITAL_TIME_STEP_MINUTES,
)
from rem_card.ui.operblock_view.operblock_helpers import (
    _minute_floor_dt,
)

class OperBlockVitalsServiceAdapter:
    status_service = None

    def __init__(self, remcard_service, operblock_service: OperBlockService):
        self._remcard_service = remcard_service
        self._operblock_service = operblock_service
        self._operation_case_id: int | None = None
        self._admission_id: int | None = None
        self._started_at: datetime | None = None
        self._ended_at: datetime | None = None

    def capture_vital_writer(self):
        writer = OperBlockVitalsServiceAdapter(self._remcard_service, self._operblock_service)
        writer.set_operation_context(
            operation_case_id=self._operation_case_id, admission_id=self._admission_id,
            started_at=self._started_at, ended_at=self._ended_at,
        )
        return writer

    def set_operation_context(
        self,
        *,
        operation_case_id: int | None,
        admission_id: int | None,
        started_at: datetime | None,
        ended_at: datetime | None,
    ):
        self._operation_case_id = int(operation_case_id) if operation_case_id else None
        self._admission_id = int(admission_id) if admission_id else None
        self._started_at = _minute_floor_dt(started_at)
        self._ended_at = ended_at

    def normalize_time(self, value: str, fallback_time: str | None = None) -> str:
        return self._remcard_service.normalize_time(value, fallback_time)

    def is_time_input_valid(self, value: str) -> bool:
        return self._remcard_service.is_time_input_valid(value)

    def resolve_datetime(self, time: str, shift_date: datetime) -> datetime:
        normalized = self.normalize_time(time)
        hour, minute = map(int, normalized.split(":"))
        start = _minute_floor_dt(self._started_at or shift_date or datetime.now())
        same_day = datetime.combine(start.date(), datetime.min.time()).replace(hour=hour, minute=minute)
        if same_day >= start:
            return same_day
        crosses_midnight = hour < 6 or (start.hour >= 12 and hour < start.hour)
        if crosses_midnight:
            return same_day + timedelta(days=1)
        return same_day

    def get_day_period(self, date: datetime):
        start = self._started_at or date
        end = self._ended_at or (start + timedelta(hours=OPERBLOCK_INITIAL_CHART_HOURS))
        return start, max(end, start)

    def get_effective_bounds(self, admission_id: int, date: datetime):
        return self.get_day_period(date)

    def get_patient(self, admission_id: int):
        return self._remcard_service.get_patient(admission_id)

    def get_vitals(self, admission_id: int, date: datetime):
        if self._operation_case_id:
            return self._operblock_service.list_operation_vitals(self._operation_case_id)
        return self._remcard_service.get_vitals(admission_id, date)

    def get_vitals_extended(self, admission_id: int, date: datetime):
        return self.get_vitals(admission_id, date)

    def suggest_vital_time(self, shift_date: datetime, **_kwargs) -> str:
        vitals = self.get_vitals(self._admission_id or 0, shift_date)
        if vitals:
            return vitals[-1].timestamp.strftime("%H:%M")
        start = self._started_at or shift_date or datetime.now()
        return start.strftime("%H:%M")

    def next_full_hour(self, time: str, shift_date: datetime) -> str:
        current_dt = self.resolve_datetime(time, shift_date)
        return (current_dt + timedelta(minutes=OPERBLOCK_VITAL_TIME_STEP_MINUTES)).strftime("%H:%M")

    def now_time(self, current_dt: datetime, shift_date: datetime) -> str:
        _ = shift_date
        return current_dt.strftime("%H:%M")

    def current_shift_time(self, shift_date: datetime) -> str:
        return self.now_time(datetime.now(), shift_date)

    def apply_offset(self, time: str, shift_date: datetime, delta_minutes: int) -> str:
        current_dt = self.resolve_datetime(time, shift_date)
        target_dt = current_dt + timedelta(minutes=int(delta_minutes))
        start = self._started_at or shift_date
        if target_dt < start:
            target_dt = start
        return target_dt.strftime("%H:%M")

    def display_hint(self, time: str, shift_date: datetime) -> dict:
        resolved = self.resolve_datetime(time, shift_date)
        start = self._started_at or shift_date
        day_offset = max(0, (resolved.date() - start.date()).days)
        return {
            "label": resolved.strftime("%H:%M"),
            "day_offset": day_offset,
            "text": f"операция +{day_offset} день" if day_offset else "операция",
        }

    def add_vital(self, dto, shift_date: datetime | None = None, force: bool = False, expected_revision=None):
        _ = shift_date, force
        timestamp = getattr(dto, "timestamp", None)
        if self._started_at and isinstance(timestamp, datetime):
            start_minute = self._started_at.replace(second=0, microsecond=0)
            vital_minute = timestamp.replace(second=0, microsecond=0)
            if vital_minute < start_minute:
                raise ValueError(
                    f"Пациент поступил в операционную в {start_minute.strftime('%H:%M')}. "
                    "Ввод данных ранее этого времени невозможен."
                )
        if self._ended_at and isinstance(timestamp, datetime):
            end_minute = self._ended_at.replace(second=0, microsecond=0)
            vital_minute = timestamp.replace(second=0, microsecond=0)
            if vital_minute > end_minute:
                raise ValueError(
                    f"Операция завершена в {end_minute.strftime('%H:%M')}. "
                    "Ввод данных позже этого времени невозможен."
                )
        change = self._operblock_service.add_vital_record(dto, expected_revision=expected_revision, return_change=True)
        change["operation_case_id"] = self._operation_case_id
        return change

    def undo_vital_change(self, change):
        return self._operblock_service.undo_vital_change(change)

    def delete_last_vital(self, admission_id: int, date: datetime, expected_revision=None, *, expected_vital_id=None):
        _ = date
        return self._operblock_service.delete_last_vital_record(
            admission_id,
            expected_revision=expected_revision,
            expected_vital_id=expected_vital_id,
        )

    def enqueue_write(self, *args, **kwargs):
        return self._remcard_service.enqueue_write(*args, **kwargs)


OPERBLOCK_STARTED_AT_LOCK_TOOLTIP = (
    "Есть изменения в карте. Отмените их, чтобы изменить время поступления пациента в оперблок."
)


def _sanitize_diagnostic_message(exc: Exception, *, limit: int = 240) -> str:
    return re.sub(r"\s+", " ", str(exc or "")).strip()[:limit]
