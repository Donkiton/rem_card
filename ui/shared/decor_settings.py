from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
import re
import shutil
import time
from typing import Any
from zoneinfo import ZoneInfo

from rem_card.app.paths import get_icon_dir
from rem_card.app.runtime_paths import is_compiled
from rem_card.app.settings_db_paths import get_settings_backgrounds_dir, get_settings_decor_elements_dir


DECOR_SETTINGS_VERSION = 1
DECOR_SETTINGS_KEY = "decor_settings"
DECOR_SETTINGS_SCOPE = "shared"
DECOR_TIMEZONE = "Asia/Vladivostok"
DECOR_RANGE_YEAR = 2000
DECOR_YEAR_MINUTES = 366 * 24 * 60
DEFAULT_SNOWFLAKE_FILES = (
    "decor_snowflake_1.svg",
    "decor_snowflake_2.svg",
    "decor_snowflake_3.svg",
)
SUPPORTED_DECOR_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".svg"}
DECOR_ZONES = {
    "all": "Вся РЕМ карта и оперблок",
    "remcard": "РЕМ карта",
    "operblock": "Оперблок",
    "w1": "W1 пациенты",
}


@dataclass(frozen=True)
class DecorScheduleConflict:
    first_name: str
    second_name: str
    first_period: str
    second_period: str

    def message(self) -> str:
        return (
            "События декора пересекаются по времени.\n\n"
            f"1. {self.first_name}: {self.first_period}\n"
            f"2. {self.second_name}: {self.second_period}\n\n"
            "Одновременно может быть активно только одно событие."
        )


class DecorSettingsValidationError(ValueError):
    def __init__(self, conflict: DecorScheduleConflict):
        self.conflict = conflict
        super().__init__(conflict.message())


def default_decor_settings_payload() -> dict[str, Any]:
    return {
        "version": DECOR_SETTINGS_VERSION,
        "timezone": DECOR_TIMEZONE,
        "events": [
            {
                "id": "new_year_snow",
                "name": "Новогодний снег",
                "enabled": True,
                "zone": "all",
                "start": "12-31",
                "start_time": "00:00",
                "end": "01-01",
                "end_time": "23:59",
                "timezone": DECOR_TIMEZONE,
                "intensity": 34,
                "wind_strength": 52,
                "snowdrifts": {
                    "enabled": True,
                    "max_height": 42,
                    "accumulation": 38,
                    "surface_intensity": 35,
                },
                "particles": [
                    {
                        "id": "snowflake_1",
                        "name": "Снежинка 1",
                        "file": DEFAULT_SNOWFLAKE_FILES[0],
                        "size": 22,
                        "weight": 0.65,
                    },
                    {
                        "id": "snowflake_2",
                        "name": "Снежинка 2",
                        "file": DEFAULT_SNOWFLAKE_FILES[1],
                        "size": 28,
                        "weight": 0.85,
                    },
                    {
                        "id": "snowflake_3",
                        "name": "Снежинка 3",
                        "file": DEFAULT_SNOWFLAKE_FILES[2],
                        "size": 18,
                        "weight": 0.45,
                    },
                ],
            }
        ],
    }


def now_vladivostok() -> datetime:
    try:
        return datetime.now(ZoneInfo(DECOR_TIMEZONE))
    except Exception:
        return datetime.now(timezone(timedelta(hours=10)))


def normalize_decor_settings_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        payload = default_decor_settings_payload()

    raw_events = payload.get("events")
    if raw_events is None:
        raw_events = default_decor_settings_payload()["events"]
    if not isinstance(raw_events, list):
        raw_events = []

    events: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for index, raw_event in enumerate(raw_events, start=1):
        event = normalize_decor_event(raw_event, index)
        base_id = event["id"]
        event_id = base_id
        suffix = 2
        while event_id in used_ids:
            event_id = f"{base_id}_{suffix}"
            suffix += 1
        event["id"] = event_id
        used_ids.add(event_id)
        events.append(event)

    return {
        "version": DECOR_SETTINGS_VERSION,
        "timezone": DECOR_TIMEZONE,
        "events": events,
    }


def normalize_decor_event(raw_event: Any, index: int = 1) -> dict[str, Any]:
    data = dict(raw_event) if isinstance(raw_event, dict) else {}
    raw_id = str(data.get("id") or "").strip()
    event_id = re.sub(r"[^A-Za-z0-9_-]+", "_", raw_id).strip("_") or f"decor_event_{index}"
    name = str(data.get("name") or "").strip() or f"Событие декора {index}"
    snowdrifts = data.get("snowdrifts") if isinstance(data.get("snowdrifts"), dict) else {}
    particles = data.get("particles")
    if not isinstance(particles, list):
        particles = []
    normalized_particles = [
        particle
        for particle_index, particle_raw in enumerate(particles, start=1)
        if (particle := normalize_decor_particle(particle_raw, particle_index)) is not None
    ]
    return {
        "id": event_id,
        "name": name,
        "enabled": _bool(data.get("enabled"), True),
        "zone": normalize_decor_zone(data.get("zone")),
        "start": normalize_month_day(data.get("start"), "12-31"),
        "start_time": normalize_time_text(data.get("start_time"), "00:00"),
        "end": normalize_month_day(data.get("end"), "01-01"),
        "end_time": normalize_time_text(data.get("end_time"), "23:59"),
        "timezone": DECOR_TIMEZONE,
        "intensity": _clamp_int(data.get("intensity"), 34, 0, 100),
        "wind_strength": _clamp_int(data.get("wind_strength"), 52, 0, 100),
        "snowdrifts": {
            "enabled": _bool(snowdrifts.get("enabled"), True),
            "max_height": _clamp_int(snowdrifts.get("max_height"), 42, 0, 120),
            "accumulation": _clamp_int(snowdrifts.get("accumulation"), 38, 0, 100),
            "surface_intensity": _clamp_int(snowdrifts.get("surface_intensity"), 35, 0, 100),
        },
        "particles": normalized_particles,
    }


def normalize_decor_particle(raw_particle: Any, index: int = 1) -> dict[str, Any] | None:
    data = dict(raw_particle) if isinstance(raw_particle, dict) else {}
    file_name = _safe_file_name(data.get("file"))
    if not file_name:
        return None
    raw_id = str(data.get("id") or "").strip()
    particle_id = re.sub(r"[^A-Za-z0-9_-]+", "_", raw_id).strip("_") or f"particle_{index}"
    return {
        "id": particle_id,
        "name": str(data.get("name") or "").strip() or os.path.splitext(file_name)[0],
        "file": file_name,
        "size": _clamp_int(data.get("size"), 24, 6, 160),
        "weight": _clamp_float(data.get("weight"), 1.0, 0.1, 5.0),
    }


def normalize_decor_zone(value: Any) -> str:
    zone = str(value or "").strip().lower()
    return zone if zone in DECOR_ZONES else "all"


def normalize_month_day(value: Any, default: str) -> str:
    fallback = default if re.match(r"^\d{2}-\d{2}$", str(default or "")) else "01-01"
    text = str(value or "").strip()
    if not text:
        text = fallback
    parsed: tuple[int, int] | None = None
    match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", text)
    if match:
        parsed = (int(match.group(2)), int(match.group(3)))
    else:
        normalized = text.replace(".", "-").replace("/", "-")
        parts = [part for part in normalized.split("-") if part]
        if len(parts) == 2:
            first = int(parts[0]) if parts[0].isdigit() else 0
            second = int(parts[1]) if parts[1].isdigit() else 0
            parsed = (second, first) if first > 12 and 1 <= second <= 12 else (first, second)
    if parsed is None:
        parsed = _month_day_tuple(fallback)
    month, day = parsed
    try:
        datetime(DECOR_RANGE_YEAR, month, day)
    except ValueError:
        month, day = _month_day_tuple(fallback)
    return f"{month:02d}-{day:02d}"


def normalize_time_text(value: Any, default: str = "00:00") -> str:
    fallback = default if re.match(r"^\d{2}:\d{2}$", str(default or "")) else "00:00"
    text = str(value or "").strip()
    match = re.match(r"^(\d{1,2})[:.](\d{1,2})$", text)
    if not match:
        return fallback
    hour = int(match.group(1))
    minute = int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return fallback
    return f"{hour:02d}:{minute:02d}"


def month_day_to_label(value: str) -> str:
    month, day = _month_day_tuple(normalize_month_day(value, "01-01"))
    return f"{day:02d}.{month:02d}"


def event_period_label(event: dict[str, Any]) -> str:
    return (
        f"{month_day_to_label(str(event.get('start') or '01-01'))} "
        f"{normalize_time_text(event.get('start_time'), '00:00')} - "
        f"{month_day_to_label(str(event.get('end') or '01-01'))} "
        f"{normalize_time_text(event.get('end_time'), '23:59')}"
    )


def validate_decor_settings_payload(payload: dict[str, Any]) -> None:
    conflict = find_decor_schedule_conflict(payload.get("events") or [])
    if conflict is not None:
        raise DecorSettingsValidationError(conflict)


def find_decor_schedule_conflict(events: list[dict[str, Any]]) -> DecorScheduleConflict | None:
    normalized_events = [
        normalize_decor_event(event, index)
        for index, event in enumerate(events, start=1)
        if isinstance(event, dict)
    ]
    for first_index, first_event in enumerate(normalized_events):
        first_intervals = decor_event_intervals(first_event)
        for second_event in normalized_events[first_index + 1 :]:
            second_intervals = decor_event_intervals(second_event)
            if _intervals_overlap(first_intervals, second_intervals):
                return DecorScheduleConflict(
                    first_name=str(first_event.get("name") or "Событие"),
                    second_name=str(second_event.get("name") or "Событие"),
                    first_period=event_period_label(first_event),
                    second_period=event_period_label(second_event),
                )
    return None


def active_decor_event(payload: dict[str, Any] | None = None, now: datetime | None = None) -> dict[str, Any] | None:
    settings = normalize_decor_settings_payload(payload) if payload is not None else DecorSettingsStorage().load()
    current = now or now_vladivostok()
    if current.tzinfo is not None:
        try:
            current = current.astimezone(ZoneInfo(DECOR_TIMEZONE))
        except Exception:
            current = current.astimezone(timezone(timedelta(hours=10)))
    for event in settings.get("events") or []:
        if not _bool(event.get("enabled"), True):
            continue
        if decor_event_is_active(event, current):
            return event
    return None


def decor_event_is_active(event: dict[str, Any], current: datetime) -> bool:
    event = normalize_decor_event(event)
    current_minute = _datetime_to_year_minute(current)
    for start, end in decor_event_intervals(event):
        if start <= current_minute <= end:
            return True
    return False


def decor_event_intervals(event: dict[str, Any]) -> list[tuple[int, int]]:
    start = _event_minute(event.get("start"), event.get("start_time"), "12-31", "00:00")
    end = _event_minute(event.get("end"), event.get("end_time"), "01-01", "23:59")
    if start <= end:
        return [(start, end)]
    return [(start, DECOR_YEAR_MINUTES - 1), (0, end)]


def ensure_decor_asset_dirs() -> str:
    os.makedirs(get_settings_backgrounds_dir(), exist_ok=True)
    decor_dir = os.path.abspath(os.path.normpath(get_settings_decor_elements_dir()))
    os.makedirs(decor_dir, exist_ok=True)
    return decor_dir


def get_decor_assets_write_dir() -> str:
    if is_compiled():
        return ensure_decor_asset_dirs()
    icon_dir = os.path.abspath(os.path.normpath(get_icon_dir()))
    os.makedirs(icon_dir, exist_ok=True)
    return icon_dir


def decor_storage_file_path(file_name: str) -> str:
    return os.path.join(get_settings_decor_elements_dir(), _safe_file_name(file_name))


def decor_icon_file_path(file_name: str) -> str:
    return os.path.join(get_icon_dir(), _safe_file_name(file_name))


def decor_file_path(file_name: str) -> str:
    safe_name = _safe_file_name(file_name)
    if not safe_name:
        return ""
    storage_path = decor_storage_file_path(safe_name)
    if os.path.isfile(storage_path):
        return storage_path
    icon_path = decor_icon_file_path(safe_name)
    if os.path.isfile(icon_path):
        return icon_path
    return storage_path


def copy_decor_asset_to_assets_dir(source_path: str) -> str:
    source = os.path.abspath(os.path.normpath(str(source_path or "").strip().strip('"')))
    if not os.path.isfile(source):
        raise FileNotFoundError("Файл декоративного элемента не найден.")
    extension = os.path.splitext(source)[1].lower()
    if extension not in SUPPORTED_DECOR_EXTENSIONS:
        raise ValueError("Поддерживаются только изображения PNG, JPG, JPEG, BMP, GIF, WEBP или SVG.")

    target_dir = get_decor_assets_write_dir()
    existing = os.path.abspath(os.path.join(target_dir, os.path.basename(source)))
    if os.path.normcase(source) == os.path.normcase(existing):
        return os.path.basename(source)

    stem = os.path.splitext(os.path.basename(source))[0]
    safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")[:36] or "decor"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = f"decor_{stamp}_{safe_stem}"
    target = os.path.join(target_dir, f"{base_name}{extension}")
    counter = 2
    while os.path.exists(target):
        target = os.path.join(target_dir, f"{base_name}_{counter}{extension}")
        counter += 1
    _copy_file_atomic(source, target)
    return os.path.basename(target)


class DecorSettingsStorage:
    def load(self) -> dict[str, Any]:
        from rem_card.services.settings.settings_service import get_settings_service

        payload = get_settings_service().get_app_setting(DECOR_SETTINGS_SCOPE, DECOR_SETTINGS_KEY, default=None)
        return normalize_decor_settings_payload(payload)

    def save(self, payload: dict[str, Any]) -> None:
        from rem_card.services.settings.settings_service import DECOR_SETTINGS_APP_KEY, DISPLAY_SETTINGS_KEY, get_settings_service

        normalized = normalize_decor_settings_payload(payload)
        validate_decor_settings_payload(normalized)
        get_settings_service().set_app_setting(
            DECOR_SETTINGS_SCOPE,
            DECOR_SETTINGS_APP_KEY,
            normalized,
            catalog_key=DISPLAY_SETTINGS_KEY,
            entity_type="decor_settings",
            operation="update",
        )


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "да", "on"}:
            return True
        if text in {"0", "false", "no", "n", "нет", "off"}:
            return False
    if value is None:
        return bool(default)
    return bool(value)


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(float(value))
    except Exception:
        number = int(default)
    return max(minimum, min(maximum, number))


def _clamp_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except Exception:
        number = float(default)
    return round(max(minimum, min(maximum, number)), 2)


def _safe_file_name(value: Any) -> str:
    text = os.path.basename(str(value or "").strip().replace("\\", os.sep).replace("/", os.sep))
    return text


def _month_day_tuple(value: str) -> tuple[int, int]:
    month, day = value.split("-", 1)
    return int(month), int(day)


def _month_day_of_year(value: Any, default: str) -> int:
    month, day = _month_day_tuple(normalize_month_day(value, default))
    return datetime(DECOR_RANGE_YEAR, month, day).timetuple().tm_yday


def _event_minute(day_value: Any, time_value: Any, day_default: str, time_default: str) -> int:
    day = _month_day_of_year(day_value, day_default)
    time_text = normalize_time_text(time_value, time_default)
    hour, minute = (int(part) for part in time_text.split(":", 1))
    return (day - 1) * 24 * 60 + hour * 60 + minute


def _datetime_to_year_minute(value: datetime) -> int:
    normalized = value.replace(year=DECOR_RANGE_YEAR)
    day = normalized.timetuple().tm_yday
    return (day - 1) * 24 * 60 + normalized.hour * 60 + normalized.minute


def _intervals_overlap(first: list[tuple[int, int]], second: list[tuple[int, int]]) -> bool:
    for first_start, first_end in first:
        for second_start, second_end in second:
            if first_start <= second_end and second_start <= first_end:
                return True
    return False


def _copy_file_atomic(source_path: str, target_path: str) -> None:
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    tmp_path = f"{target_path}.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        shutil.copy2(source_path, tmp_path)
        os.replace(tmp_path, target_path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
