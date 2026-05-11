import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

class ShiftService:
    """Сервис для управления временными интервалами смен (8:00 - 8:00)."""
    SHIFT_START_HOUR = 8
    MINUTES_PER_SHIFT = 24 * 60
    OUTCOME_ROLLOVER_WINDOW_MINUTES = 4 * 60

    @staticmethod
    def _shift_start(date: datetime) -> datetime:
        if date.hour < ShiftService.SHIFT_START_HOUR:
            return date.replace(hour=ShiftService.SHIFT_START_HOUR, minute=0, second=0, microsecond=0) - timedelta(days=1)
        return date.replace(hour=ShiftService.SHIFT_START_HOUR, minute=0, second=0, microsecond=0)

    @staticmethod
    def _parse_time(value: str) -> Optional[Tuple[int, int]]:
        text = str(value or "").strip()
        if not text:
            return None

        if re.fullmatch(r"\d{1,4}", text):
            if len(text) <= 2:
                hour = int(text)
                minute = 0
            elif len(text) == 3:
                hour = int(text[:1])
                minute = int(text[1:])
            else:
                hour = int(text[:2])
                minute = int(text[2:])
        else:
            match = re.fullmatch(r"\s*(\d{1,2})\D+(\d{1,2})\s*", text)
            if not match:
                return None
            hour = int(match.group(1))
            minute = int(match.group(2))

        if hour == 24 and minute == 0:
            return 0, 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
        return None

    @staticmethod
    def is_time_input_valid(value: str) -> bool:
        return ShiftService._parse_time(value) is not None

    @staticmethod
    def normalize_time(value: str, fallback_time: Optional[str] = None) -> str:
        """Нормализует ввод времени в HH:mm без знания смены."""
        parsed = ShiftService._parse_time(value)
        if parsed is None:
            fallback = ShiftService._parse_time(fallback_time) if fallback_time is not None else None
            if fallback is None:
                fallback = (ShiftService.SHIFT_START_HOUR, 0)
            parsed = fallback

        hour, minute = parsed
        return f"{hour:02d}:{minute:02d}"

    @staticmethod
    def resolve_datetime(time: str, shift_date: datetime) -> datetime:
        """Преобразует HH:mm в реальный datetime внутри смены 08:00-08:00."""
        normalized = ShiftService.normalize_time(time)
        hour, minute = map(int, normalized.split(":"))
        shift_start = ShiftService._shift_start(shift_date)
        target_date = shift_start.date()
        if hour < ShiftService.SHIFT_START_HOUR:
            target_date = target_date + timedelta(days=1)
        return datetime.combine(target_date, datetime.min.time()).replace(hour=hour, minute=minute)

    @staticmethod
    def resolve_outcome_datetime(
        time: str,
        shift_date: datetime,
        *,
        reference_dt: Optional[datetime] = None,
        not_before: Optional[datetime] = None,
        latest_activity_dt: Optional[datetime] = None,
    ) -> datetime:
        """
        Преобразует HH:mm для исхода/перевода.

        Обычные записи живут строго внутри выбранной смены 08:00-08:00, но исход
        часто вводят перед 08:00 на ближайшее будущее. В этом случае время
        08:00/08:10 должно попасть в следующую смену, а не в прошлое утро.
        """
        normalized = ShiftService.normalize_time(time)
        hour, _minute = map(int, normalized.split(":"))
        base_dt = ShiftService.resolve_datetime(normalized, shift_date).replace(second=0, microsecond=0)
        ref_dt = (reference_dt or datetime.now()).replace(second=0, microsecond=0)

        lower_bounds = [
            value.replace(second=0, microsecond=0)
            for value in (not_before, latest_activity_dt)
            if value is not None
        ]
        min_allowed = max(lower_bounds) if lower_bounds else None

        candidates = sorted({base_dt + timedelta(days=offset) for offset in (-1, 0, 1)})
        valid_candidates = [
            candidate
            for candidate in candidates
            if min_allowed is None or candidate >= min_allowed
        ]
        if not valid_candidates:
            return base_dt

        if base_dt not in valid_candidates:
            return valid_candidates[0]

        _shift_start, shift_end = ShiftService.get_day_period(shift_date)
        next_candidate = base_dt + timedelta(days=1)
        should_roll_to_near_future = (
            ref_dt < shift_end
            and base_dt < ref_dt <= next_candidate
            and hour >= ShiftService.SHIFT_START_HOUR
            and (next_candidate - ref_dt).total_seconds() <= ShiftService.OUTCOME_ROLLOVER_WINDOW_MINUTES * 60
            and (min_allowed is None or next_candidate >= min_allowed)
        )
        if should_roll_to_near_future:
            return next_candidate

        return base_dt

    @staticmethod
    def apply_offset(time: str, shift_date: datetime, delta_minutes: int) -> str:
        """Сдвигает время внутри смены по кольцу 08:00-08:00."""
        shift_start = ShiftService._shift_start(shift_date)
        current_dt = ShiftService.resolve_datetime(time, shift_start)
        offset = int((current_dt - shift_start).total_seconds() // 60)
        target_offset = (offset + int(delta_minutes)) % ShiftService.MINUTES_PER_SHIFT
        target_dt = shift_start + timedelta(minutes=target_offset)
        return target_dt.strftime("%H:%M")

    @staticmethod
    def now_time(current_dt: datetime, shift_date: datetime) -> str:
        """Возвращает HH:mm для указанного источника времени в рамках выбранной смены."""
        shift_start, shift_end = ShiftService.get_day_period(shift_date)
        if current_dt < shift_start:
            target_dt = shift_start
        elif current_dt >= shift_end:
            target_dt = shift_end - timedelta(minutes=1)
        else:
            target_dt = current_dt
        return target_dt.strftime("%H:%M")

    @staticmethod
    def next_full_hour(time: str, shift_date: datetime) -> str:
        """Округляет время вверх до следующего полного часа внутри смены."""
        normalized = ShiftService.normalize_time(time)
        hour, minute = map(int, normalized.split(":"))
        shift_minutes = ((hour - ShiftService.SHIFT_START_HOUR) % 24) * 60 + minute
        minutes_to_add = 60 if minute == 0 else 60 - minute
        target_offset = (shift_minutes + minutes_to_add) % ShiftService.MINUTES_PER_SHIFT
        target_dt = ShiftService._shift_start(shift_date) + timedelta(minutes=target_offset)
        return target_dt.strftime("%H:%M")

    @staticmethod
    def display_hint(time: str, shift_date: datetime) -> Dict[str, object]:
        normalized = ShiftService.normalize_time(time)
        hour = int(normalized.split(":")[0])
        day_offset = 1 if hour < ShiftService.SHIFT_START_HOUR else 0
        return {
            "label": normalized,
            "day_offset": day_offset,
            "text": "смена +1 день" if day_offset else "сегодня",
        }
    
    @staticmethod
    def get_day_period(date: datetime) -> Tuple[datetime, datetime]:
        """Возвращает начало и конец смены (с 08:00 до 08:00 следующего дня)."""
        start = ShiftService._shift_start(date)
        end = start + timedelta(days=1)
        return start, end

    @staticmethod
    def calculate_icu_day(admission_datetime: Optional[datetime], card_date: datetime) -> Optional[int]:
        """Считает реанимационные сутки по сменам 08:00-08:00."""
        if not admission_datetime or not card_date:
            return None
        admission_start, _ = ShiftService.get_day_period(admission_datetime)
        card_start, _ = ShiftService.get_day_period(card_date)
        days = (card_start.date() - admission_start.date()).days + 1
        return max(1, days)

    @staticmethod
    def get_all_card_dates(raw_dates: List[datetime]) -> List[datetime]:
        """Преобразует список дат в уникальные даты начала смен."""
        card_dates = set()
        for dt in raw_dates:
            start, _ = ShiftService.get_day_period(dt)
            card_dates.add(start)
        return sorted(list(card_dates))
