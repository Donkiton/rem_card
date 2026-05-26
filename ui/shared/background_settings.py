from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import json
import os
import re
import shutil
import sys
import time
from typing import Any

from rem_card.app.paths import get_icon_dir
from rem_card.ui.styles.theme_storage import get_style_settings_path


BACKGROUND_SETTINGS_ENV = "REMCARD_BACKGROUND_SETTINGS_PATH"
BACKGROUND_SETTINGS_VERSION = 1
BACKGROUND_SETTINGS_RELATIVE_PATH = os.path.join("settings", "display_settings", "background_settings.json")
DEFAULT_BACKGROUND_ID = "default"
DEFAULT_BACKGROUND_FILE = "fon.png"
SUPPORTED_BACKGROUND_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}
BACKGROUND_RANGE_YEAR = 2000

_ACTIVE_BACKGROUND_CACHE: dict[str, Any] = {}


def get_background_settings_path() -> str:
    override = os.environ.get(BACKGROUND_SETTINGS_ENV)
    if override:
        return os.path.abspath(os.path.normpath(override))

    style_path = Path(os.path.abspath(get_style_settings_path()))
    if style_path.parent.name == "color_scheme":
        settings_root = style_path.parent.parent
    else:
        settings_root = style_path.parent
    return str(settings_root / "display_settings" / "background_settings.json")


def invalidate_background_settings_cache() -> None:
    _ACTIVE_BACKGROUND_CACHE.clear()


def _default_background_entry(file_name: str = DEFAULT_BACKGROUND_FILE) -> dict[str, Any]:
    return {
        "id": DEFAULT_BACKGROUND_ID,
        "name": "Стандартный фон",
        "file": _safe_file_name(file_name) or DEFAULT_BACKGROUND_FILE,
        "start": "01-01",
        "end": "12-31",
        "locked": True,
    }


def default_background_settings_payload() -> dict[str, Any]:
    return {
        "version": BACKGROUND_SETTINGS_VERSION,
        "backgrounds": [_default_background_entry()],
    }


def _safe_file_name(value: Any) -> str:
    text = os.path.basename(str(value or "").strip().replace("\\", os.sep).replace("/", os.sep))
    return text


def _month_day_tuple(value: str) -> tuple[int, int]:
    month, day = value.split("-", 1)
    return int(month), int(day)


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
            if first > 12 and 1 <= second <= 12:
                parsed = (second, first)
            else:
                parsed = (first, second)

    if parsed is None:
        parsed = _month_day_tuple(fallback)

    month, day = parsed
    try:
        date(BACKGROUND_RANGE_YEAR, month, day)
    except ValueError:
        month, day = _month_day_tuple(fallback)
    return f"{month:02d}-{day:02d}"


def month_day_to_label(value: str) -> str:
    month, day = _month_day_tuple(normalize_month_day(value, "01-01"))
    return f"{day:02d}.{month:02d}"


def _month_day_number(value: str) -> int:
    month, day = _month_day_tuple(normalize_month_day(value, "01-01"))
    return month * 100 + day


def _month_day_of_year(value: str) -> int:
    month, day = _month_day_tuple(normalize_month_day(value, "01-01"))
    return date(BACKGROUND_RANGE_YEAR, month, day).timetuple().tm_yday


def background_range_days(start: str, end: str) -> set[int]:
    start_day = _month_day_of_year(start)
    end_day = _month_day_of_year(end)
    if start_day <= end_day:
        return set(range(start_day, end_day + 1))
    return set(range(start_day, 367)) | set(range(1, end_day + 1))


def background_ranges_overlap(first: dict[str, Any], second: dict[str, Any]) -> bool:
    first_days = background_range_days(str(first.get("start") or "01-01"), str(first.get("end") or "12-31"))
    second_days = background_range_days(str(second.get("start") or "01-01"), str(second.get("end") or "12-31"))
    return bool(first_days.intersection(second_days))


def date_in_background_range(current_date: date, start: str, end: str) -> bool:
    current_value = current_date.month * 100 + current_date.day
    start_value = _month_day_number(start)
    end_value = _month_day_number(end)
    if start_value <= end_value:
        return start_value <= current_value <= end_value
    return current_value >= start_value or current_value <= end_value


def _normalize_custom_entry(data: Any, index: int) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    raw_id = str(data.get("id") or "").strip()
    if not raw_id or raw_id == DEFAULT_BACKGROUND_ID:
        raw_id = f"background_{index}"
    entry_id = re.sub(r"[^A-Za-z0-9_-]+", "_", raw_id).strip("_") or f"background_{index}"
    file_name = _safe_file_name(data.get("file"))
    name = str(data.get("name") or "").strip() or f"Дополнительный фон {index}"
    return {
        "id": entry_id,
        "name": name,
        "file": file_name,
        "start": normalize_month_day(data.get("start"), "01-01"),
        "end": normalize_month_day(data.get("end"), "12-31"),
        "locked": False,
    }


def normalize_background_settings_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {}

    raw_backgrounds = payload.get("backgrounds")
    if not isinstance(raw_backgrounds, list):
        raw_backgrounds = []

    default_file = DEFAULT_BACKGROUND_FILE
    for raw in raw_backgrounds:
        if isinstance(raw, dict) and str(raw.get("id") or "").strip() == DEFAULT_BACKGROUND_ID:
            default_file = _safe_file_name(raw.get("file")) or DEFAULT_BACKGROUND_FILE
            break

    backgrounds = [_default_background_entry(default_file)]
    used_ids = {DEFAULT_BACKGROUND_ID}
    custom_index = 1
    for raw in raw_backgrounds:
        if isinstance(raw, dict) and str(raw.get("id") or "").strip() == DEFAULT_BACKGROUND_ID:
            continue
        entry = _normalize_custom_entry(raw, custom_index)
        if entry is None:
            continue
        base_id = str(entry["id"])
        entry_id = base_id
        suffix = 2
        while entry_id in used_ids:
            entry_id = f"{base_id}_{suffix}"
            suffix += 1
        entry["id"] = entry_id
        used_ids.add(entry_id)
        backgrounds.append(entry)
        custom_index += 1

    return {
        "version": BACKGROUND_SETTINGS_VERSION,
        "backgrounds": backgrounds,
    }


def background_file_path(file_name: str) -> str:
    return os.path.join(get_icon_dir(), _safe_file_name(file_name))


def background_entry_file_path(entry: dict[str, Any]) -> str:
    return background_file_path(str(entry.get("file") or DEFAULT_BACKGROUND_FILE))


def active_background_entry(payload: dict[str, Any] | None = None, today: date | None = None) -> dict[str, Any]:
    settings = normalize_background_settings_payload(payload or BackgroundSettingsStorage().load())
    current_date = today or date.today()
    backgrounds = list(settings.get("backgrounds") or [])
    default_entry = backgrounds[0] if backgrounds else _default_background_entry()

    for entry in backgrounds[1:]:
        if not str(entry.get("file") or "").strip():
            continue
        if not date_in_background_range(current_date, str(entry.get("start")), str(entry.get("end"))):
            continue
        path = background_entry_file_path(entry)
        if os.path.isfile(path) or _materialize_background_from_db(entry, path):
            return entry

    return default_entry


def _materialize_background_from_db(entry: dict[str, Any], path: str) -> bool:
    try:
        from rem_card.services.settings.settings_service import get_settings_service

        return get_settings_service().materialize_background_image(str(entry.get("id") or ""), path)
    except Exception:
        return False


def get_active_background_path(today: date | None = None) -> str:
    storage = BackgroundSettingsStorage()
    current_date = today or date.today()
    try:
        settings_mtime = os.path.getmtime(storage.path)
    except OSError:
        settings_mtime = None
    cache_key = f"{current_date.isoformat()}|{storage.path}|{settings_mtime}"
    if _ACTIVE_BACKGROUND_CACHE.get("key") == cache_key:
        return str(_ACTIVE_BACKGROUND_CACHE.get("path") or background_file_path(DEFAULT_BACKGROUND_FILE))

    payload = storage.load()
    entry = active_background_entry(payload, current_date)
    path = background_entry_file_path(entry)
    if not os.path.isfile(path):
        path = background_file_path(DEFAULT_BACKGROUND_FILE)
    _ACTIVE_BACKGROUND_CACHE["key"] = cache_key
    _ACTIVE_BACKGROUND_CACHE["path"] = path
    return path


def copy_background_to_icon_dir(source_path: str) -> str:
    source = os.path.abspath(os.path.normpath(str(source_path or "").strip().strip('"')))
    if not os.path.isfile(source):
        raise FileNotFoundError("Файл фона не найден.")

    extension = os.path.splitext(source)[1].lower()
    if extension not in SUPPORTED_BACKGROUND_EXTENSIONS:
        raise ValueError("Поддерживаются только изображения PNG, JPG, JPEG, BMP, GIF или WEBP.")

    icon_dir = os.path.abspath(get_icon_dir())
    os.makedirs(icon_dir, exist_ok=True)

    existing = os.path.abspath(os.path.join(icon_dir, os.path.basename(source)))
    if os.path.normcase(source) == os.path.normcase(existing):
        return os.path.basename(source)

    stem = os.path.splitext(os.path.basename(source))[0]
    safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")[:36] or "background"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = f"fon_{stamp}_{safe_stem}"

    target = os.path.join(icon_dir, f"{base_name}{extension}")
    counter = 2
    while os.path.exists(target):
        target = os.path.join(icon_dir, f"{base_name}_{counter}{extension}")
        counter += 1

    shutil.copy2(source, target)
    return os.path.basename(target)


class BackgroundSettingsStorage:
    def __init__(self, path: str | None = None):
        self._file_mode = path is not None or bool(os.environ.get(BACKGROUND_SETTINGS_ENV))
        self.path = os.path.abspath(path or get_background_settings_path())
        self.last_error: str | None = None

    def load(self) -> dict[str, Any]:
        self.last_error = None
        if not self._file_mode:
            try:
                from rem_card.services.settings.settings_service import get_settings_service

                payload = get_settings_service().get_app_setting("shared", "background_settings", default=None)
                if isinstance(payload, dict):
                    return normalize_background_settings_payload(payload)
                return self._default_and_save()
            except Exception as exc:
                self.last_error = str(exc)
                raise
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except FileNotFoundError:
            return self._default_and_save()
        except Exception as exc:
            self.last_error = str(exc)
            self._quarantine_broken_file()
            return self._default_and_save()

        if self._should_replace_uncustomized_file(payload):
            return self._default_and_save()
        return normalize_background_settings_payload(payload)

    def save(self, payload: dict[str, Any]) -> None:
        normalized = normalize_background_settings_payload(payload)
        if not self._file_mode:
            from rem_card.services.settings.settings_service import BACKGROUND_SETTINGS_KEY, get_settings_service

            get_settings_service().set_app_setting(
                "shared",
                "background_settings",
                normalized,
                catalog_key=BACKGROUND_SETTINGS_KEY,
                entity_type="background_settings",
                operation="update",
            )
            invalidate_background_settings_cache()
            return
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(normalized, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, self.path)
        invalidate_background_settings_cache()

    def _default_and_save(self) -> dict[str, Any]:
        payload = self._default_payload()
        try:
            self.save(payload)
        except Exception as exc:
            self.last_error = str(exc)
        return payload

    def _default_payload(self) -> dict[str, Any]:
        bundled = self._read_bundled_payload()
        if bundled:
            return normalize_background_settings_payload(bundled)
        return default_background_settings_payload()

    def _read_bundled_payload(self) -> dict[str, Any] | None:
        path = self._bundled_path()
        if not path or not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            return payload if isinstance(payload, dict) else None
        except Exception as exc:
            self.last_error = str(exc)
            return None

    def _bundled_path(self) -> str | None:
        try:
            from rem_card.app.runtime_paths import is_compiled

            if not is_compiled():
                return None
            if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
                resources_dir = str(sys._MEIPASS)
            else:
                executable_dir = os.path.dirname(os.path.abspath(sys.executable))
                internal_dir = os.path.join(executable_dir, "_internal")
                resources_dir = internal_dir if os.path.isdir(internal_dir) else executable_dir
            return os.path.join(resources_dir, "rem_card", BACKGROUND_SETTINGS_RELATIVE_PATH)
        except Exception:
            return None

    def _should_replace_uncustomized_file(self, payload: Any) -> bool:
        bundled = self._read_bundled_payload()
        if not bundled:
            return False

        schema_default = normalize_background_settings_payload(default_background_settings_payload())
        bundled_normalized = normalize_background_settings_payload(bundled)
        if bundled_normalized == schema_default:
            return False
        current_normalized = normalize_background_settings_payload(payload)
        return current_normalized == schema_default

    def _quarantine_broken_file(self) -> None:
        if not os.path.exists(self.path):
            return
        stamp = time.strftime("%Y%m%d_%H%M%S")
        broken_path = f"{self.path}.{stamp}.broken"
        try:
            os.replace(self.path, broken_path)
        except Exception:
            pass
