from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
from typing import Any, Mapping, Optional
import uuid

from rem_card.app import operblock_startup_metrics
from rem_card.app.db_cycle_registry import discover_db_cycle_paths
from rem_card.app.logger import logger
from rem_card.app.patient_age import (
    format_patient_age,
    format_patient_age_from_birth_date,
    parse_date_value,
    storage_age_from_birth_date,
)
from rem_card.app.paths import REPORT_DIR
from rem_card.app.sqlite_shared import configure_connection
from rem_card.data.dto.remcard_dto import PatientStatus, VitalDTO
from rem_card.services.concurrency import DATA_CONFLICT_MESSAGE, DataConflictError, assert_revision_matches
from rem_card.services.operblock_medication_presets import (
    load_operblock_medication_presets,
    operblock_medication_preset_display_name,
    operblock_medication_preset_requires_narcotic_sheet,
)
from rem_card.services.operblock_route_settings import (
    normalize_operblock_route_code,
    operblock_comment_with_route,
    strip_operblock_route_tag,
)
from rem_card.services.operblock_anesthesia_types import normalize_operblock_anesthesia_type_label
from rem_card.services.operblock_timeline import (
    OPERBLOCK_STAGE_KIND_LABELS,
    OperBlockTimelineSnapshot,
    build_timeline_snapshot_from_legacy_orders,
    operation_stage_kind_from_payload,
    with_timeline_content_hash,
)
from rem_card.services.patient_departments import normalize_profile_department
from rem_card.services.patient_departments import PROFILE_DEPARTMENTS
from rem_card.services.patient_bed_management.recovery_beds import RECOVERY_BED_TRANSFER_ORDER


OPERBLOCK_ROLE = "operblock"
OPERBLOCK_TABLES = (
    {"code": "emergency", "display_name": "Экстренная операционная", "sort_order": 1},
    {"code": "planned", "display_name": "Плановая операционная", "sort_order": 2},
)
OPERBLOCK_BLOOD_GROUP_OPTIONS = (
    "O(I) первая",
    "A(II) вторая",
    "B(III) третья",
    "AB(IV) четвертая",
)
OPERBLOCK_BLOOD_RH_OPTIONS = (
    "Rh(+) положительный",
    "Rh(-) отрицательный",
)
OPERBLOCK_TRANSFER_DEPARTMENT_OPTIONS = ("РАО",) + PROFILE_DEPARTMENTS
OPERBLOCK_REPORT_RETENTION_DAYS = 7
OPERBLOCK_MKB_CODE_RE = re.compile(r"^[A-Z]\d{2}(?:\.\d{1,2})?$")
_RU_TO_EN_KEYBOARD = {
    "й": "q",
    "ц": "w",
    "у": "e",
    "к": "r",
    "е": "t",
    "н": "y",
    "г": "u",
    "ш": "i",
    "щ": "o",
    "з": "p",
    "х": "[",
    "ъ": "]",
    "ф": "a",
    "ы": "s",
    "в": "d",
    "а": "f",
    "п": "g",
    "р": "h",
    "о": "j",
    "л": "k",
    "д": "l",
    "ж": ";",
    "э": "'",
    "я": "z",
    "ч": "x",
    "с": "c",
    "м": "v",
    "и": "b",
    "т": "n",
    "ь": "m",
    "б": ",",
    "ю": ".",
}


class OperBlockConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class OperBlockPatientInput:
    table_code: str
    history_number: str
    full_name: str
    gender: str
    birth_date: date
    diagnosis_code: Optional[str]
    diagnosis_text: str
    started_at: Optional[datetime] = None
    department_profile: str = ""
    operation_name: str = ""
    anesthesia_assistance_type: str = ""
    surgeons: tuple[str, ...] = ()
    operating_nurse: str = ""
    anesthesiologist: str = ""
    anesthetist: str = ""
    height_cm: Optional[int] = None
    weight_kg: Optional[float] = None
    allergies: str = ""
    blood_group: str = ""
    blood_rh: str = ""
    preop_sys: Optional[int] = None
    preop_dia: Optional[int] = None
    preop_pulse: Optional[int] = None
    preop_spo2: Optional[int] = None


def normalize_operblock_history_number(value: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    text = "/".join(part.strip() for part in text.split("/"))
    if not text:
        raise ValueError("Введите номер истории.")
    if text.count("/") > 1:
        raise ValueError("В номере истории может быть не больше одного символа '/'.")
    parts = text.split("/")
    if any(not part for part in parts):
        raise ValueError("В номере истории не может быть пустой части.")
    if not all(ch.isalnum() for part in parts for ch in part):
        raise ValueError("Номер истории может содержать только буквы, цифры и необязательный символ '/'.")
    return text


def normalize_operblock_order_route(value: str | None) -> str:
    return normalize_operblock_route_code(value)


def _strip_operblock_order_route_tag(comment: str) -> str:
    return strip_operblock_route_tag(comment)


def _operblock_order_comment_with_route(comment: str, route: str | None) -> str:
    return operblock_comment_with_route(comment, route)


def normalize_operblock_mkb_code(value: str) -> str:
    translated = "".join(_RU_TO_EN_KEYBOARD.get(ch.lower(), ch) for ch in str(value or ""))
    filtered = [ch.upper() for ch in translated if ch.upper().isalpha() or ch.isdigit() or ch == "."]
    letter = ""
    digits: list[str] = []
    dot_seen = False

    for char in filtered:
        if char == ".":
            dot_seen = True
            continue
        if not letter:
            if "A" <= char <= "Z":
                letter = char
            continue
        if char.isdigit() and len(digits) < 4:
            digits.append(char)

    if not letter:
        return ""

    code = letter + "".join(digits[:2])
    if len(digits) >= 3:
        code += "." + "".join(digits[2:4])
    elif dot_seen and len(digits) >= 2:
        code += "."
    return code


def is_complete_operblock_mkb_code(value: str) -> bool:
    return bool(OPERBLOCK_MKB_CODE_RE.fullmatch(str(value or "").strip().upper()))


def validate_operblock_runtime_path(db_manager: Any | None = None) -> None:
    if db_manager is None:
        return
    db_path = str(getattr(db_manager, "db_path", "") or getattr(db_manager, "remcard_db_path", "") or "")
    if db_path and not os.path.isfile(db_path):
        raise RuntimeError(f"БД оперблока недоступна: {db_path}")


def _now_text() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T"))
    except Exception:
        return None


def _minute_floor(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


def _format_bound_time(value: datetime) -> str:
    return value.strftime("%d.%m.%Y %H:%M")


def _format_protocol_date(value: Any) -> str:
    parsed: date | None = None
    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    else:
        text = str(value or "").strip()
        if text:
            try:
                parsed = datetime.fromisoformat(text.replace(" ", "T")).date()
            except Exception:
                try:
                    parsed = date.fromisoformat(text[:10])
                except Exception:
                    parsed = None
    if parsed is None:
        return ""
    return f"{parsed.day}.{parsed.month:02d}.{parsed.year}г."


def format_operblock_protocol_display(protocol_number: Any, protocol_date: Any) -> str:
    try:
        number = int(protocol_number or 0)
    except (TypeError, ValueError):
        number = 0
    date_text = _format_protocol_date(protocol_date)
    if number <= 0 or not date_text:
        return ""
    return f"{number} от {date_text}"


def normalize_operblock_transfer_department(value: Any) -> str:
    text = normalize_profile_department(value)
    text = re.sub(r"\s+", " ", text).strip()
    if text.casefold().replace("ё", "е") == "рао":
        return "РАО"
    return text


def _is_rao_transfer_department(value: Any) -> bool:
    return normalize_operblock_transfer_department(value) == "РАО"


def _is_upper_abbreviation(text: str) -> bool:
    compact = re.sub(r"[\s.]+", "", str(text or ""))
    return bool(compact) and compact.upper() == compact and any(ch.isalpha() for ch in compact)


def transfer_department_target_text(value: Any) -> str:
    department = normalize_operblock_transfer_department(value)
    if not department:
        return ""
    if _is_upper_abbreviation(department):
        return department

    known = {
        "терапия": "терапию",
        "хирургия": "хирургию",
        "травматология": "травматологию",
        "гинекология": "гинекологию",
        "неврология": "неврологию",
        "кардиология": "кардиологию",
        "инфекционно-педиатрическое": "инфекционно-педиатрическое отделение",
    }
    key = department.casefold().replace("ё", "е")
    if key in known:
        return known[key]

    lower = department[:1].lower() + department[1:]
    if re.search(r"\bотделение$", lower, flags=re.IGNORECASE):
        return lower
    if lower.endswith("ия"):
        return f"{lower[:-2]}ию"
    if lower.endswith("а"):
        return f"{lower[:-1]}у"
    if lower.endswith("я"):
        return f"{lower[:-1]}ю"
    return lower


def operblock_transfer_stage_label(department: Any) -> str:
    target = transfer_department_target_text(department)
    return f"Конец пособия - переведен в {target}" if target else "Конец пособия"


def _normalize_order_datetime_text(value: Any) -> str:
    parsed = _parse_dt(value)
    if parsed is None:
        raise ValueError("Укажите корректное время введения препарата.")
    return _minute_floor(parsed).isoformat(timespec="seconds")


def _hash_payload(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _parse_json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _stage_label(kind: str) -> str:
    return OPERBLOCK_STAGE_KIND_LABELS.get(str(kind or "").strip(), str(kind or "").strip() or "Этап операции")


def _normalize_operation_stage_label(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _row_stage_kind(row: Mapping[str, Any] | dict[str, Any]) -> str:
    payload = _parse_json_dict((row or {}).get("payload_json"))
    return operation_stage_kind_from_payload(payload)


def _is_stage_row(row: Mapping[str, Any] | dict[str, Any]) -> bool:
    return str((row or {}).get("event_type") or "") == "clinical_event" and bool(_row_stage_kind(row))


def _stage_rows_from_timeline_rows(rows: list[Mapping[str, Any]] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw_row in rows or []:
        row = _row_to_dict(raw_row)
        if not _is_stage_row(row):
            continue
        payload = _parse_json_dict(row.get("payload_json"))
        kind = operation_stage_kind_from_payload(payload)
        event_dt = _parse_dt(row.get("event_time"))
        if event_dt is None:
            continue
        row["stage_kind"] = kind
        row["stage_label"] = _normalize_case_text(
            payload.get("label")
            or row.get("display_label")
            or row.get("raw_text")
            or _stage_label(kind)
        )
        row["payload"] = payload
        row["event_dt"] = _minute_floor(event_dt)
        result.append(row)
    result.sort(key=lambda item: (item["event_dt"], int(item.get("id") or 0)))
    return result


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if isinstance(value, datetime) else None


def _stage_row_assistance_type(row: dict[str, Any]) -> str:
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    for key in ("anesthesia_assistance_type", "assistance_type", "anesthesia_type"):
        text = normalize_operblock_anesthesia_type_label((payload or {}).get(key))
        if text:
            return text
    return ""


def _stage_row_text(row: dict[str, Any], *keys: str) -> str:
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    for key in keys:
        text = re.sub(r"\s+", " ", str((payload or {}).get(key) or "").strip())
        if text:
            return text
    return ""


def _normalize_stage_text_list(value: Any, *, split_commas: bool = False) -> list[str]:
    if value is None:
        raw_items: list[Any] = []
    elif isinstance(value, str):
        raw_items = [value]
    elif isinstance(value, Mapping):
        raw_items = []
    else:
        try:
            raw_items = list(value)
        except TypeError:
            raw_items = [value]

    result: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        parts = re.split(r"\s*,\s*", item) if split_commas and isinstance(item, str) else [item]
        for part in parts:
            text = re.sub(r"\s+", " ", str(part or "").strip())
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                result.append(text)
    return result


def _stage_row_text_list(row: dict[str, Any], *keys: str, split_commas: bool = False) -> list[str]:
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    for key in keys:
        values = _normalize_stage_text_list((payload or {}).get(key), split_commas=split_commas)
        if values:
            return values
    return []


def _build_stage_intervals(stage_rows: list[dict[str, Any]]) -> dict[str, Any]:
    anesthesia_start: datetime | None = None
    surgery_start: datetime | None = None
    anesthesia_intervals: list[dict[str, Any]] = []
    surgery_intervals: list[dict[str, Any]] = []
    last_anesthesia_start: datetime | None = None
    last_anesthesia_end: datetime | None = None
    last_surgery_start: datetime | None = None
    last_surgery_end: datetime | None = None
    current_anesthesia_assistance_type = ""
    last_anesthesia_assistance_type = ""
    first_anesthesia_assistance_type = ""
    current_anesthesiologist = ""
    current_anesthetist = ""
    last_anesthesiologist = ""
    last_anesthetist = ""
    first_anesthesiologist = ""
    first_anesthetist = ""
    current_operation_name = ""
    current_surgeons: list[str] = []
    current_surgeon = ""
    current_operating_nurse = ""
    last_operation_name = ""
    last_surgeons: list[str] = []
    last_surgeon = ""
    last_operating_nurse = ""
    first_operation_name = ""
    first_surgeons: list[str] = []
    first_surgeon = ""
    first_operating_nurse = ""

    for row in stage_rows:
        kind = str(row.get("stage_kind") or "")
        event_dt = _minute_floor(row["event_dt"])
        if kind == "anesthesia_start":
            if anesthesia_start is None:
                anesthesia_start = event_dt
                last_anesthesia_start = event_dt
                current_anesthesia_assistance_type = _stage_row_assistance_type(row)
                last_anesthesia_assistance_type = current_anesthesia_assistance_type
                if not first_anesthesia_assistance_type:
                    first_anesthesia_assistance_type = current_anesthesia_assistance_type
                current_anesthesiologist = _stage_row_text(row, "anesthesiologist", "anesthesia_doctor")
                current_anesthetist = _stage_row_text(row, "anesthetist", "anesthesia_nurse")
                last_anesthesiologist = current_anesthesiologist
                last_anesthetist = current_anesthetist
                if current_anesthesiologist and not first_anesthesiologist:
                    first_anesthesiologist = current_anesthesiologist
                if current_anesthetist and not first_anesthetist:
                    first_anesthetist = current_anesthetist
        elif kind == "anesthesia_end":
            if anesthesia_start is not None and event_dt >= anesthesia_start:
                anesthesia_intervals.append(
                    {
                        "start": _iso_or_none(anesthesia_start),
                        "end": _iso_or_none(event_dt),
                        "assistance_type": current_anesthesia_assistance_type,
                        "anesthesiologist": current_anesthesiologist,
                        "anesthetist": current_anesthetist,
                    }
                )
                anesthesia_start = None
                current_anesthesia_assistance_type = ""
                current_anesthesiologist = ""
                current_anesthetist = ""
                last_anesthesia_end = event_dt
        elif kind == "surgery_start":
            if surgery_start is None:
                surgery_start = event_dt
                last_surgery_start = event_dt
                current_operation_name = _stage_row_text(row, "operation_name", "surgery_name")
                current_surgeons = _stage_row_text_list(row, "surgeons", split_commas=True)
                if not current_surgeons:
                    current_surgeons = _stage_row_text_list(row, "surgeon", split_commas=True)
                current_surgeon = ", ".join(current_surgeons)
                current_operating_nurse = _stage_row_text(row, "operating_nurse", "surgery_nurse")
                last_operation_name = current_operation_name
                last_surgeons = list(current_surgeons)
                last_surgeon = current_surgeon
                last_operating_nurse = current_operating_nurse
                if current_operation_name and not first_operation_name:
                    first_operation_name = current_operation_name
                if current_surgeons and not first_surgeons:
                    first_surgeons = list(current_surgeons)
                if current_surgeon and not first_surgeon:
                    first_surgeon = current_surgeon
                if current_operating_nurse and not first_operating_nurse:
                    first_operating_nurse = current_operating_nurse
        elif kind == "surgery_end":
            if surgery_start is not None and event_dt >= surgery_start:
                surgery_intervals.append(
                    {
                        "start": _iso_or_none(surgery_start),
                        "end": _iso_or_none(event_dt),
                        "operation_name": current_operation_name,
                        "surgeons": list(current_surgeons),
                        "surgeon": current_surgeon,
                        "operating_nurse": current_operating_nurse,
                    }
                )
                surgery_start = None
                current_operation_name = ""
                current_surgeons = []
                current_surgeon = ""
                current_operating_nurse = ""
                last_surgery_end = event_dt

    if anesthesia_start is not None:
        anesthesia_intervals.append(
            {
                "start": _iso_or_none(anesthesia_start),
                "end": None,
                "assistance_type": current_anesthesia_assistance_type,
                "anesthesiologist": current_anesthesiologist,
                "anesthetist": current_anesthetist,
            }
        )
    if surgery_start is not None:
        surgery_intervals.append(
            {
                "start": _iso_or_none(surgery_start),
                "end": None,
                "operation_name": current_operation_name,
                "surgeons": list(current_surgeons),
                "surgeon": current_surgeon,
                "operating_nurse": current_operating_nurse,
            }
        )

    return {
        "anesthesia_intervals": anesthesia_intervals,
        "surgery_intervals": surgery_intervals,
        "anesthesia_active": anesthesia_start is not None,
        "surgery_active": surgery_start is not None,
        "current_anesthesia_start": _iso_or_none(anesthesia_start),
        "current_surgery_start": _iso_or_none(surgery_start),
        "last_anesthesia_start": _iso_or_none(last_anesthesia_start),
        "last_anesthesia_end": _iso_or_none(last_anesthesia_end),
        "last_surgery_start": _iso_or_none(last_surgery_start),
        "last_surgery_end": _iso_or_none(last_surgery_end),
        "first_anesthesia_start": anesthesia_intervals[0]["start"] if anesthesia_intervals else None,
        "current_anesthesia_assistance_type": current_anesthesia_assistance_type,
        "last_anesthesia_assistance_type": last_anesthesia_assistance_type,
        "first_anesthesia_assistance_type": first_anesthesia_assistance_type,
        "current_anesthesiologist": current_anesthesiologist,
        "current_anesthetist": current_anesthetist,
        "last_anesthesiologist": last_anesthesiologist,
        "last_anesthetist": last_anesthetist,
        "first_anesthesiologist": first_anesthesiologist,
        "first_anesthetist": first_anesthetist,
        "current_operation_name": current_operation_name,
        "current_surgeons": list(current_surgeons),
        "current_surgeon": current_surgeon,
        "current_operating_nurse": current_operating_nurse,
        "last_operation_name": last_operation_name,
        "last_surgeons": list(last_surgeons),
        "last_surgeon": last_surgeon,
        "last_operating_nurse": last_operating_nurse,
        "first_operation_name": first_operation_name,
        "first_surgeons": list(first_surgeons),
        "first_surgeon": first_surgeon,
        "first_operating_nurse": first_operating_nurse,
    }


def _split_name(full_name: str) -> tuple[str, str, str]:
    parts = [part for part in str(full_name or "").strip().split() if part]
    last = parts[0] if len(parts) >= 1 else ""
    first = parts[1] if len(parts) >= 2 else ""
    middle = " ".join(parts[2:]) if len(parts) >= 3 else ""
    return last, first, middle


def _to_birth_date(value: Any) -> date:
    parsed = parse_date_value(value)
    if parsed is None:
        raise ValueError("Укажите корректную дату рождения.")
    return parsed


def _normalize_case_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _case_option_lookup(options: tuple[str, ...], aliases: Mapping[str, str] | None = None) -> dict[str, str]:
    lookup = {option.casefold(): option for option in options}
    for key, value in (aliases or {}).items():
        lookup[str(key).casefold()] = value
    return lookup


_BLOOD_GROUP_LOOKUP = _case_option_lookup(
    OPERBLOCK_BLOOD_GROUP_OPTIONS,
    {
        "O(I)": "O(I) первая",
        "0(I)": "O(I) первая",
        "I": "O(I) первая",
        "1": "O(I) первая",
        "первая": "O(I) первая",
        "A(II)": "A(II) вторая",
        "II": "A(II) вторая",
        "2": "A(II) вторая",
        "вторая": "A(II) вторая",
        "B(III)": "B(III) третья",
        "III": "B(III) третья",
        "3": "B(III) третья",
        "третья": "B(III) третья",
        "AB(IV)": "AB(IV) четвертая",
        "IV": "AB(IV) четвертая",
        "4": "AB(IV) четвертая",
        "четвертая": "AB(IV) четвертая",
        "четвёртая": "AB(IV) четвертая",
    },
)
_BLOOD_RH_LOOKUP = _case_option_lookup(
    OPERBLOCK_BLOOD_RH_OPTIONS,
    {
        "Rh(+)": "Rh(+) положительный",
        "Rh+": "Rh(+) положительный",
        "+": "Rh(+) положительный",
        "положительный": "Rh(+) положительный",
        "положительная": "Rh(+) положительный",
        "пол.": "Rh(+) положительный",
        "Rh(-)": "Rh(-) отрицательный",
        "Rh-": "Rh(-) отрицательный",
        "-": "Rh(-) отрицательный",
        "отрицательный": "Rh(-) отрицательный",
        "отрицательная": "Rh(-) отрицательный",
        "отр.": "Rh(-) отрицательный",
    },
)


def normalize_operblock_blood_group(value: Any) -> str:
    text = _normalize_case_text(value)
    if not text:
        return ""
    result = _BLOOD_GROUP_LOOKUP.get(text.casefold())
    if result:
        return result
    raise ValueError("Группа крови: выберите значение из списка.")


def normalize_operblock_blood_rh(value: Any) -> str:
    text = _normalize_case_text(value)
    if not text:
        return ""
    result = _BLOOD_RH_LOOKUP.get(text.casefold())
    if result:
        return result
    raise ValueError("Резус: выберите значение из списка.")


def _normalize_optional_int(value: Any, label: str, minimum: int, maximum: int) -> int | None:
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    if not re.fullmatch(r"\d+", text):
        raise ValueError(f"{label}: укажите целое число.")
    number = int(text)
    if number < minimum or number > maximum:
        raise ValueError(f"{label}: допустимый диапазон {minimum}-{maximum}.")
    return number


def _normalize_optional_float(value: Any, label: str, minimum: float, maximum: float) -> float | None:
    text = "" if value is None else str(value).strip().replace(",", ".")
    if not text:
        return None
    try:
        number = float(Decimal(text))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label}: укажите число.") from None
    if number < minimum or number > maximum:
        left = int(minimum) if float(minimum).is_integer() else minimum
        right = int(maximum) if float(maximum).is_integer() else maximum
        raise ValueError(f"{label}: допустимый диапазон {left}-{right}.")
    return number


def _normalize_case_surgeons(value: Any) -> tuple[str, ...]:
    return tuple(_normalize_stage_text_list(value, split_commas=True))


def _surgeons_json(value: Any) -> str | None:
    surgeons = list(_normalize_case_surgeons(value))
    return json.dumps(surgeons, ensure_ascii=False) if surgeons else None


def _surgeons_from_json(value: Any) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except Exception:
        parsed = value
    return list(_normalize_case_surgeons(parsed))


def _case_input_from_payload(data: OperBlockPatientInput | Mapping[str, Any] | dict[str, Any]) -> OperBlockPatientInput:
    if isinstance(data, OperBlockPatientInput):
        return data
    payload = data if isinstance(data, Mapping) else {}
    return OperBlockPatientInput(
        table_code=str(payload.get("table_code") or ""),
        history_number=str(payload.get("history_number") or ""),
        full_name=str(payload.get("full_name") or ""),
        gender=str(payload.get("gender") or ""),
        birth_date=_to_birth_date(payload.get("birth_date")),
        diagnosis_code=payload.get("diagnosis_code"),
        diagnosis_text=str(payload.get("diagnosis_text") or ""),
        started_at=_parse_dt(payload.get("started_at")),
        department_profile=normalize_profile_department(payload.get("department_profile")),
        operation_name=_normalize_case_text(payload.get("operation_name")),
        anesthesia_assistance_type=normalize_operblock_anesthesia_type_label(
            payload.get("anesthesia_assistance_type")
        ),
        surgeons=_normalize_case_surgeons(payload.get("surgeons")),
        operating_nurse=_normalize_case_text(payload.get("operating_nurse")),
        anesthesiologist=_normalize_case_text(payload.get("anesthesiologist")),
        anesthetist=_normalize_case_text(payload.get("anesthetist")),
        height_cm=_normalize_optional_int(payload.get("height_cm"), "Рост", 1, 260),
        weight_kg=_normalize_optional_float(payload.get("weight_kg"), "Вес", 0.5, 500),
        allergies=_normalize_case_text(payload.get("allergies")),
        blood_group=normalize_operblock_blood_group(payload.get("blood_group")),
        blood_rh=normalize_operblock_blood_rh(payload.get("blood_rh")),
        preop_sys=_normalize_optional_int(payload.get("preop_sys"), "АД систолическое", 0, 300),
        preop_dia=_normalize_optional_int(payload.get("preop_dia"), "АД диастолическое", 0, 300),
        preop_pulse=_normalize_optional_int(payload.get("preop_pulse"), "ЧСС", 0, 300),
        preop_spo2=_normalize_optional_int(payload.get("preop_spo2"), "SpO₂", 0, 100),
    )


def _validate_case_vitals(data: OperBlockPatientInput) -> None:
    if (data.preop_sys is None) ^ (data.preop_dia is None):
        raise ValueError("АД: заполните систолическое и диастолическое значения.")
    if data.preop_sys is not None and data.preop_dia is not None and data.preop_dia > data.preop_sys:
        raise ValueError("АД диастолическое не может быть выше систолического.")
    values = (data.preop_sys, data.preop_dia, data.preop_pulse, data.preop_spo2)
    if any(value is not None for value in values) and any(value is None for value in values):
        raise ValueError("Исходные витальные показатели заполните полностью: АД, ЧСС и SpO₂.")


def _has_case_vitals(data: OperBlockPatientInput) -> bool:
    return any(
        value is not None
        for value in (data.preop_sys, data.preop_dia, data.preop_pulse, data.preop_spo2)
    )


def _age_text(row: dict[str, Any], reference: Optional[datetime] = None) -> str:
    birth_date = parse_date_value(row.get("birth_date"))
    if birth_date:
        text = format_patient_age_from_birth_date(birth_date, reference)
        if text:
            return text
    return format_patient_age(row.get("patient_age"), row.get("patient_age_unit"), row.get("patient_months"))


def _row_to_dict(row) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _sqlite_table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if row and row[0]
    }


def _sqlite_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall() if row and row[1]}
    except Exception:
        return set()


class OperBlockService:
    def __init__(self, db_manager):
        self.db = db_manager
        self.client_id = f"{socket.gethostname()}:{os.getpid()}"

    def _runtime_mode(self) -> str:
        return str(getattr(getattr(self.db, "runtime_context", None), "mode", "") or "")

    def _is_opblock_offline_runtime(self) -> bool:
        return self._runtime_mode() == "opblock_offline"

    def _offline_session_id(self) -> str | None:
        runtime_context = getattr(self.db, "runtime_context", None)
        return str(getattr(runtime_context, "emergency_session_id", "") or "") or None

    def _new_case_uuid(self) -> str:
        return f"opblock:{uuid.uuid4()}"

    def _current_db_path(self) -> str:
        raw = str(getattr(self.db, "db_path", "") or getattr(self.db, "remcard_db_path", "") or "")
        return os.path.abspath(raw) if raw else ""

    def _iter_archive_db_paths(self, *, include_current: bool = True) -> list[str]:
        current_db_path = self._current_db_path()
        if not current_db_path:
            return []
        if self._is_opblock_offline_runtime():
            return [current_db_path] if include_current else []
        return discover_db_cycle_paths(
            current_db_path=current_db_path,
            include_current=include_current,
        )

    def get_archive_db_paths_for_period(self, start_dt: str | None, end_dt: str | None) -> list[str]:
        db_paths = self._iter_archive_db_paths(include_current=True)
        if not start_dt or not end_dt:
            return db_paths

        start = _parse_dt(start_dt)
        end = _parse_dt(end_dt)
        if start is None or end is None:
            return db_paths
        if end < start:
            start, end = end, start

        result = []
        for db_path in db_paths:
            if self._db_has_operblock_cases_in_period(db_path, start, end):
                result.append(db_path)
        return result

    @staticmethod
    def _db_has_operblock_cases_in_period(db_path: str, start: datetime, end: datetime) -> bool:
        if not db_path or not os.path.isfile(db_path):
            return False
        conn = None
        try:
            conn = sqlite3.connect(
                f"file:{os.path.abspath(db_path)}?mode=ro",
                uri=True,
                check_same_thread=False,
                isolation_level=None,
                timeout=5.0,
            )
            configure_connection(conn, readonly=True)
            tables = _sqlite_table_names(conn)
            if "operation_cases" not in tables:
                return False
            case_columns = _sqlite_columns(conn, "operation_cases")
            if "started_at" not in case_columns:
                return False
            join_sql = ""
            where_sql = "WHERE DATETIME(oc.started_at) BETWEEN DATETIME(?) AND DATETIME(?)"
            if "admissions" in tables and "admission_id" in case_columns:
                admission_columns = _sqlite_columns(conn, "admissions")
                join_sql = "LEFT JOIN admissions a ON a.id = oc.admission_id"
                if "unit_scope" in admission_columns:
                    where_sql += " AND COALESCE(a.unit_scope, '') = 'operblock'"
            if "status" in case_columns:
                where_sql += " AND COALESCE(oc.status, '') NOT IN ('cancelled', 'deleted')"
            row = conn.execute(
                f"""
                SELECT 1
                FROM operation_cases oc
                {join_sql}
                {where_sql}
                LIMIT 1
                """,
                (
                    start.strftime("%Y-%m-%d %H:%M:%S"),
                    end.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            ).fetchone()
            return bool(row)
        except Exception as exc:
            logger.warning("Skipping operblock DB period check %s: %s", db_path, exc)
            return False
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _build_archive_cases_query(
        *,
        tables: set[str] | None = None,
        admission_columns: set[str] | None = None,
        start_dt: str | None = None,
        end_dt: str | None = None,
    ) -> tuple[str, tuple[Any, ...]]:
        tables = set(tables or {"operating_tables", "admissions", "patients"})
        admission_columns = set(admission_columns or {"unit_scope"})
        table_join = "LEFT JOIN operating_tables t ON t.code = oc.table_code" if "operating_tables" in tables else ""
        table_display_expr = (
            "COALESCE(t.display_name, CASE oc.table_code WHEN 'emergency' THEN 'Экстренная операционная' "
            "WHEN 'planned' THEN 'Плановая операционная' ELSE oc.table_code END)"
            if "operating_tables" in tables
            else "CASE oc.table_code WHEN 'emergency' THEN 'Экстренная операционная' WHEN 'planned' THEN 'Плановая операционная' ELSE oc.table_code END"
        )
        unit_scope_clause = (
            "AND COALESCE(a.unit_scope, '') = 'operblock'"
            if "unit_scope" in admission_columns
            else ""
        )
        params: list[Any] = []
        period_clause = ""
        if start_dt and end_dt:
            period_clause = "AND DATETIME(oc.started_at) BETWEEN DATETIME(?) AND DATETIME(?)"
            params.extend([start_dt, end_dt])

        query = f"""
            SELECT
                oc.id AS operation_case_id,
                oc.patient_id,
                oc.admission_id,
                oc.table_code,
                oc.status AS case_status,
                oc.started_at,
                oc.ended_at,
                {table_display_expr} AS table_display_name,
                p.full_name,
                p.birth_date,
                a.history_number,
                a.patient_gender,
                a.patient_age,
                a.patient_months,
                a.patient_age_unit,
                a.diagnosis_code,
                a.diagnosis_text
            FROM operation_cases oc
            {table_join}
            JOIN admissions a ON a.id = oc.admission_id
            JOIN patients p ON p.id = oc.patient_id
            WHERE oc.status IN ('active', 'closed')
              {unit_scope_clause}
              {period_clause}
            ORDER BY
                CASE WHEN oc.status = 'active' THEN 0 ELSE 1 END,
                datetime(COALESCE(oc.ended_at, oc.started_at)) DESC,
                oc.id DESC
            LIMIT 500
            """
        return query, tuple(params)

    @staticmethod
    def _archive_case_payload(data: Mapping[str, Any] | dict[str, Any], *, db_path: str, is_external: bool) -> dict[str, Any]:
        source_path = os.path.abspath(str(db_path or "")) if db_path else ""
        operation_case_id = int((data or {}).get("operation_case_id") or 0)
        return {
            "operation_case_id": operation_case_id,
            "source_operation_case_id": operation_case_id,
            "admission_id": int((data or {}).get("admission_id") or 0),
            "source_admission_id": int((data or {}).get("admission_id") or 0),
            "patient_id": int((data or {}).get("patient_id") or 0),
            "source_patient_id": int((data or {}).get("patient_id") or 0),
            "table_code": (data or {}).get("table_code"),
            "table_display_name": (data or {}).get("table_display_name"),
            "history_number": (data or {}).get("history_number") or "",
            "full_name": (data or {}).get("full_name") or "Неизвестно",
            "age": _age_text(data),
            "gender": (data or {}).get("patient_gender") or "",
            "diagnosis_code": (data or {}).get("diagnosis_code") or "",
            "diagnosis_text": (data or {}).get("diagnosis_text") or "",
            "started_at": (data or {}).get("started_at"),
            "ended_at": (data or {}).get("ended_at"),
            "status": (data or {}).get("case_status") or "closed",
            "source_db_path": source_path,
            "source_db_name": os.path.basename(source_path) if source_path else "",
            "is_external_archive": bool(is_external),
        }

    @staticmethod
    def _fetch_archive_case_rows_from_db(
        db_path: str,
        *,
        start_dt: str | None = None,
        end_dt: str | None = None,
    ) -> list[dict[str, Any]]:
        if not db_path or not os.path.isfile(db_path):
            return []
        conn = sqlite3.connect(
            f"file:{os.path.abspath(db_path)}?mode=ro",
            uri=True,
            check_same_thread=False,
            isolation_level=None,
            timeout=5.0,
        )
        try:
            configure_connection(conn, readonly=True)
            tables = _sqlite_table_names(conn)
            if not {"operation_cases", "admissions", "patients"}.issubset(tables):
                return []
            admission_columns = _sqlite_columns(conn, "admissions")
            query, params = OperBlockService._build_archive_cases_query(
                tables=tables,
                admission_columns=admission_columns,
                start_dt=start_dt,
                end_dt=end_dt,
            )
            return [dict(row) for row in conn.execute(query, params).fetchall()]
        finally:
            conn.close()

    @staticmethod
    def _board_operation_events_from_timeline(timeline: Mapping[str, Any] | dict[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for event in list((timeline or {}).get("operation_events") or []):
            payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
            kind = operation_stage_kind_from_payload(payload)
            if not kind:
                continue
            label = _normalize_case_text(
                (payload or {}).get("label")
                or event.get("display_label")
                or event.get("raw_text")
                or OPERBLOCK_STAGE_KIND_LABELS.get(kind)
                or ""
            )
            result.append(
                {
                    "id": event.get("id") or "",
                    "source_id": int(event.get("source_id") or 0),
                    "kind": kind,
                    "label": label or OPERBLOCK_STAGE_KIND_LABELS.get(kind, "Этап операции"),
                    "event_time": event.get("event_time"),
                    "revision": int(event.get("revision") or 0),
                }
            )
        result.sort(key=lambda item: (_parse_dt(item.get("event_time")) or datetime.min, int(item.get("source_id") or 0)))
        return result

    @staticmethod
    def _board_medication_history_from_timeline(timeline: Mapping[str, Any] | dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for event in list((timeline or {}).get("bolus_events") or []):
            label = _normalize_case_text(event.get("display_label") or event.get("raw_text") or event.get("drug_label"))
            if not label:
                continue
            items.append(
                {
                    "time": event.get("event_time"),
                    "label": label,
                    "kind": "bolus",
                    "kind_label": "Болюс",
                    "source_id": int(event.get("source_id") or 0),
                }
            )
        for interval in list((timeline or {}).get("infusion_intervals") or []):
            payload = interval.get("payload") if isinstance(interval.get("payload"), Mapping) else {}
            label = _normalize_case_text(
                interval.get("display_label")
                or (payload or {}).get("display_name")
                or (payload or {}).get("label")
                or interval.get("drug_label")
            )
            if not label:
                continue
            is_gas = str((payload or {}).get("kind") or "").strip().casefold() == "gas"
            dose_text = _normalize_case_text((payload or {}).get("display_dose_text") or (payload or {}).get("dose_text"))
            if is_gas and dose_text and dose_text.casefold() not in label.casefold():
                label = f"{label} {dose_text}".strip()
            try:
                source_id = int(str(interval.get("interval_id") or "0").rsplit(":", 1)[-1] or 0)
            except (TypeError, ValueError):
                source_id = 0
            items.append(
                {
                    "time": interval.get("start_time"),
                    "label": label,
                    "kind": "gas" if is_gas else "infusion",
                    "kind_label": "Газ" if is_gas else "Инфузия",
                    "source_id": source_id,
                }
            )
        items.sort(key=lambda item: (_parse_dt(item.get("time")) or datetime.min, int(item.get("source_id") or 0)))
        return items

    def build_operblock_board_snapshot(self) -> dict[str, Any]:
        metric_started = operblock_startup_metrics.timer_start()
        validate_operblock_runtime_path(self.db)
        rows = self.db.fetch_all_remcard(
            """
            SELECT
                t.code AS table_code,
                t.display_name AS table_display_name,
                t.sort_order,
                oc.id AS operation_case_id,
                oc.patient_id,
                oc.admission_id,
                oc.status AS case_status,
                oc.started_at,
                oc.ended_at,
                COALESCE(oc.revision, 0) AS case_revision,
                oc.planned_operation_name,
                oc.planned_surgeons_json,
                oc.planned_operating_nurse,
                oc.planned_anesthesiologist,
                oc.planned_anesthetist,
                oc.height_cm,
                oc.weight_kg,
                oc.allergies,
                oc.blood_group,
                oc.blood_rh,
                oc.preop_sys,
                oc.preop_dia,
                oc.preop_pulse,
                oc.preop_spo2,
                COALESCE(oc.preop_save_initial_vitals, 1) AS preop_save_initial_vitals,
                p.full_name,
                p.birth_date,
                a.history_number,
                a.patient_gender,
                a.patient_age,
                a.patient_months,
                a.patient_age_unit,
                a.diagnosis_code,
                a.diagnosis_text,
                a.department_profile,
                (
                    SELECT v.id FROM vitals v
                    WHERE v.admission_id = oc.admission_id
                      AND STRFTIME('%Y-%m-%d %H:%M', v.datetime) >= STRFTIME('%Y-%m-%d %H:%M', oc.started_at)
                      AND (v.sys IS NOT NULL OR v.dia IS NOT NULL OR v.pulse IS NOT NULL OR v.spo2 IS NOT NULL)
                    ORDER BY CAST(STRFTIME('%s', v.datetime) AS INTEGER) ASC, v.id ASC
                    LIMIT 1
                ) AS first_vitals_id,
                (
                    SELECT v.id FROM vitals v
                    WHERE v.admission_id = oc.admission_id
                      AND STRFTIME('%Y-%m-%d %H:%M', v.datetime) >= STRFTIME('%Y-%m-%d %H:%M', oc.started_at)
                      AND (v.sys IS NOT NULL OR v.dia IS NOT NULL OR v.pulse IS NOT NULL OR v.spo2 IS NOT NULL)
                    ORDER BY CAST(STRFTIME('%s', v.datetime) AS INTEGER) DESC, v.id DESC
                    LIMIT 1
                ) AS latest_vitals_id,
                (
                    SELECT v.sys FROM vitals v
                    WHERE v.admission_id = oc.admission_id
                      AND STRFTIME('%Y-%m-%d %H:%M', v.datetime) >= STRFTIME('%Y-%m-%d %H:%M', oc.started_at)
                      AND (v.sys IS NOT NULL OR v.dia IS NOT NULL OR v.pulse IS NOT NULL OR v.spo2 IS NOT NULL)
                    ORDER BY CAST(STRFTIME('%s', v.datetime) AS INTEGER) DESC, v.id DESC
                    LIMIT 1
                ) AS latest_sys,
                (
                    SELECT v.dia FROM vitals v
                    WHERE v.admission_id = oc.admission_id
                      AND STRFTIME('%Y-%m-%d %H:%M', v.datetime) >= STRFTIME('%Y-%m-%d %H:%M', oc.started_at)
                      AND (v.sys IS NOT NULL OR v.dia IS NOT NULL OR v.pulse IS NOT NULL OR v.spo2 IS NOT NULL)
                    ORDER BY CAST(STRFTIME('%s', v.datetime) AS INTEGER) DESC, v.id DESC
                    LIMIT 1
                ) AS latest_dia,
                (
                    SELECT v.pulse FROM vitals v
                    WHERE v.admission_id = oc.admission_id
                      AND STRFTIME('%Y-%m-%d %H:%M', v.datetime) >= STRFTIME('%Y-%m-%d %H:%M', oc.started_at)
                      AND (v.sys IS NOT NULL OR v.dia IS NOT NULL OR v.pulse IS NOT NULL OR v.spo2 IS NOT NULL)
                    ORDER BY CAST(STRFTIME('%s', v.datetime) AS INTEGER) DESC, v.id DESC
                    LIMIT 1
                ) AS latest_pulse,
                (
                    SELECT v.spo2 FROM vitals v
                    WHERE v.admission_id = oc.admission_id
                      AND STRFTIME('%Y-%m-%d %H:%M', v.datetime) >= STRFTIME('%Y-%m-%d %H:%M', oc.started_at)
                      AND (v.sys IS NOT NULL OR v.dia IS NOT NULL OR v.pulse IS NOT NULL OR v.spo2 IS NOT NULL)
                    ORDER BY CAST(STRFTIME('%s', v.datetime) AS INTEGER) DESC, v.id DESC
                    LIMIT 1
                ) AS latest_spo2,
                (
                    SELECT v.datetime FROM vitals v
                    WHERE v.admission_id = oc.admission_id
                      AND STRFTIME('%Y-%m-%d %H:%M', v.datetime) >= STRFTIME('%Y-%m-%d %H:%M', oc.started_at)
                      AND (v.sys IS NOT NULL OR v.dia IS NOT NULL OR v.pulse IS NOT NULL OR v.spo2 IS NOT NULL)
                    ORDER BY CAST(STRFTIME('%s', v.datetime) AS INTEGER) DESC, v.id DESC
                    LIMIT 1
                ) AS latest_vitals_time
            FROM operating_tables t
            LEFT JOIN operation_cases oc
                ON oc.table_code = t.code
               AND oc.status = 'active'
            LEFT JOIN admissions a ON a.id = oc.admission_id
            LEFT JOIN patients p ON p.id = oc.patient_id
            WHERE t.code IN ('emergency', 'planned')
            ORDER BY t.sort_order
            """
        )

        by_code = {str(row["table_code"]): _row_to_dict(row) for row in rows}
        tables = []
        for table in OPERBLOCK_TABLES:
            row = by_code.get(table["code"], {})
            occupied = row.get("operation_case_id") is not None
            patient = None
            if occupied:
                latest_sys = row.get("latest_sys")
                latest_dia = row.get("latest_dia")
                latest_pulse = row.get("latest_pulse")
                latest_spo2 = row.get("latest_spo2")
                latest_time = row.get("latest_vitals_time")
                latest_source = "current" if latest_time else ""
                if (
                    latest_time
                    and row.get("latest_vitals_id") is not None
                    and row.get("latest_vitals_id") == row.get("first_vitals_id")
                    and any(
                        value is not None
                        for value in (row.get("preop_sys"), row.get("preop_dia"), row.get("preop_pulse"), row.get("preop_spo2"))
                    )
                ):
                    latest_source = "initial"
                if latest_time in (None, "") and any(
                    value is not None
                    for value in (row.get("preop_sys"), row.get("preop_dia"), row.get("preop_pulse"), row.get("preop_spo2"))
                ):
                    latest_sys = row.get("preop_sys")
                    latest_dia = row.get("preop_dia")
                    latest_pulse = row.get("preop_pulse")
                    latest_spo2 = row.get("preop_spo2")
                    latest_source = "initial"
                operation_events: list[dict[str, Any]] = []
                medication_history: list[dict[str, Any]] = []
                try:
                    timeline = self.build_operblock_timeline_snapshot(
                        int(row.get("admission_id") or 0),
                        operation_case_id=int(row.get("operation_case_id") or 0),
                    ).to_dict()
                    operation_events = self._board_operation_events_from_timeline(timeline)
                    medication_history = self._board_medication_history_from_timeline(timeline)
                except Exception as exc:
                    logger.warning(
                        "operblock board timeline summary failed case_id=%s: %s",
                        row.get("operation_case_id"),
                        exc,
                        exc_info=True,
                    )
                patient = {
                    "patient_id": row.get("patient_id"),
                    "admission_id": row.get("admission_id"),
                    "operation_case_id": row.get("operation_case_id"),
                    "full_name": row.get("full_name") or "Неизвестно",
                    "history_number": row.get("history_number") or "",
                    "age": _age_text(row),
                    "gender": row.get("patient_gender") or "",
                    "diagnosis_code": row.get("diagnosis_code") or "",
                    "diagnosis_text": row.get("diagnosis_text") or "",
                    "department_profile": row.get("department_profile") or "",
                    "operation_name": row.get("planned_operation_name") or "",
                    "surgeons": _surgeons_from_json(row.get("planned_surgeons_json")),
                    "operating_nurse": row.get("planned_operating_nurse") or "",
                    "anesthesiologist": row.get("planned_anesthesiologist") or "",
                    "anesthetist": row.get("planned_anesthetist") or "",
                    "height_cm": row.get("height_cm"),
                    "weight_kg": row.get("weight_kg"),
                    "allergies": row.get("allergies") or "",
                    "blood_group": row.get("blood_group") or "",
                    "blood_rh": row.get("blood_rh") or "",
                    "status_text": "В операционной",
                    "started_at": row.get("started_at"),
                    "revision": int(row.get("case_revision") or 0),
                    "latest": {
                        "ad": self._format_ad(latest_sys, latest_dia),
                        "sys": latest_sys,
                        "dia": latest_dia,
                        "pulse": latest_pulse,
                        "spo2": latest_spo2,
                        "datetime": latest_time,
                        "source": latest_source,
                    },
                    "operation_events": operation_events,
                    "medication_history": medication_history,
                }
            tables.append(
                {
                    "code": table["code"],
                    "display_name": row.get("table_display_name") or table["display_name"],
                    "sort_order": table["sort_order"],
                    "occupied": occupied,
                    "patient": patient,
                }
            )

        payload = {
            "role": OPERBLOCK_ROLE,
            "tables": tables,
        }
        payload["content_hash"] = _hash_payload(payload)
        operblock_startup_metrics.record_since(
            "build_operblock_board_snapshot_ms",
            metric_started,
            source="operblock_service",
            table_count=len(tables),
        )
        return payload

    def list_archived_operation_cases(
        self,
        start_dt: str | None = None,
        end_dt: str | None = None,
    ) -> list[dict[str, Any]]:
        validate_operblock_runtime_path(self.db)
        result: list[dict[str, Any]] = []
        current_db_path = self._current_db_path()
        current_key = os.path.normcase(current_db_path) if current_db_path else ""
        db_paths = self.get_archive_db_paths_for_period(start_dt, end_dt) if start_dt and end_dt else self._iter_archive_db_paths(include_current=True)
        if not db_paths and current_db_path:
            db_paths = [current_db_path]

        for db_path in db_paths:
            abs_path = os.path.abspath(str(db_path or ""))
            is_current = bool(current_key) and os.path.normcase(abs_path) == current_key
            try:
                if is_current:
                    query, params = self._build_archive_cases_query(
                        start_dt=start_dt,
                        end_dt=end_dt,
                    )
                    rows = [_row_to_dict(row) for row in self.db.fetch_all_remcard(query, params)]
                else:
                    rows = self._fetch_archive_case_rows_from_db(abs_path, start_dt=start_dt, end_dt=end_dt)
                for row in rows:
                    result.append(
                        self._archive_case_payload(
                            row,
                            db_path=abs_path,
                            is_external=not is_current,
                        )
                    )
            except Exception as exc:
                logger.warning("Skipping operblock archive DB %s due to read error: %s", abs_path, exc)

        result.sort(
            key=lambda item: (
                0 if str(item.get("status") or "").strip().lower() == "active" else 1,
                _parse_dt(item.get("ended_at")) or _parse_dt(item.get("started_at")) or datetime.min,
                int(item.get("source_operation_case_id") or item.get("operation_case_id") or 0),
            ),
            reverse=False,
        )
        active = [item for item in result if str(item.get("status") or "").strip().lower() == "active"]
        archived = [item for item in result if str(item.get("status") or "").strip().lower() != "active"]
        archived.sort(
            key=lambda item: (
                _parse_dt(item.get("ended_at")) or _parse_dt(item.get("started_at")) or datetime.min,
                int(item.get("source_operation_case_id") or item.get("operation_case_id") or 0),
            ),
            reverse=True,
        )
        return active + archived

    def restore_archived_operation_case(self, operation_case_id: int) -> dict[str, int]:
        validate_operblock_runtime_path(self.db)
        now = _now_text()

        def operation(cursor: sqlite3.Cursor):
            case = cursor.execute(
                """
                SELECT id, admission_id, table_code, status
                FROM operation_cases
                WHERE id = ?
                """,
                (int(operation_case_id),),
            ).fetchone()
            if not case:
                raise OperBlockConflictError("Архивный случай не найден.")
            if str(case["status"] or "") != "closed":
                raise OperBlockConflictError("Случай уже активен или не может быть восстановлен.")
            table_code = self._validate_table_code(str(case["table_code"] or ""))
            occupied = cursor.execute(
                """
                SELECT id
                FROM operation_cases
                WHERE table_code = ?
                  AND status = 'active'
                LIMIT 1
                """,
                (table_code,),
            ).fetchone()
            if occupied:
                raise OperBlockConflictError("Нельзя вернуть пациента: операционный стол сейчас занят.")
            cursor.execute(
                """
                UPDATE operation_cases
                SET status = 'active',
                    ended_at = NULL,
                    last_modified_by = 'operblock',
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ?
                  AND status = 'closed'
                """,
                (int(operation_case_id),),
            )
            if cursor.rowcount != 1:
                raise OperBlockConflictError("Случай изменён другим рабочим местом. Обновите архив.")
            cursor.execute(
                """
                INSERT INTO operation_table_assignments (
                    operation_case_id, table_code, assigned_at, status,
                    created_by_role, created_by_client_id, last_modified_by
                ) VALUES (?, ?, ?, 'active', 'operblock', ?, 'operblock')
                """,
                (int(operation_case_id), table_code, now, self.client_id),
            )
            admission_id = int(case["admission_id"])
            cursor.execute(
                """
                UPDATE admissions
                SET is_active = 1,
                    updated_at = ?,
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ?
                """,
                (now, admission_id),
            )
            cursor.execute(
                """
                INSERT INTO patient_status_events (
                    admission_id, status, reason_type, reason_text, start_time,
                    created_by, last_modified_by
                ) VALUES (?, 'OR', 'operblock_restore', 'Возврат в операционную из архива', ?, 'operblock', 'operblock')
                """,
                (admission_id, now),
            )
            return {"operation_case_id": int(operation_case_id), "admission_id": admission_id}

        return dict(self.db.run_write_operation(operation, source="operblock_restore_archived_case"))

    @staticmethod
    def _table_exists_for_delete(cursor: sqlite3.Cursor, table_name: str) -> bool:
        row = cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
            (table_name,),
        ).fetchone()
        return row is not None

    @classmethod
    def _delete_where_in(cls, cursor: sqlite3.Cursor, table_name: str, column_name: str, values: list[int]) -> int:
        clean_values = [int(value) for value in values if value is not None]
        if not clean_values or not cls._table_exists_for_delete(cursor, table_name):
            return 0
        placeholders = ", ".join("?" for _ in clean_values)
        cursor.execute(
            f"DELETE FROM {table_name} WHERE {column_name} IN ({placeholders})",
            tuple(clean_values),
        )
        return int(cursor.rowcount or 0)

    @classmethod
    def _hard_delete_archived_operation_cases(
        cls,
        cursor: sqlite3.Cursor,
        case_rows: list[sqlite3.Row],
    ) -> dict[str, int]:
        case_ids = sorted({int(row["id"]) for row in case_rows if row["id"] is not None})
        admission_ids = sorted({int(row["admission_id"]) for row in case_rows if row["admission_id"] is not None})
        patient_ids = sorted({int(row["patient_id"]) for row in case_rows if row["patient_id"] is not None})
        if not case_ids:
            return {"deleted": 0}

        cls._delete_where_in(cursor, "operblock_timeline_events", "operation_case_id", case_ids)
        cls._delete_where_in(cursor, "operation_table_assignments", "operation_case_id", case_ids)

        deleted_cases = cls._delete_where_in(cursor, "operation_cases", "id", case_ids)

        removable_admission_ids: list[int] = []
        if admission_ids:
            placeholders = ", ".join("?" for _ in admission_ids)
            remaining_rows = cursor.execute(
                f"""
                SELECT DISTINCT admission_id
                FROM operation_cases
                WHERE admission_id IN ({placeholders})
                """,
                tuple(admission_ids),
            ).fetchall()
            remaining_admission_ids = {int(row["admission_id"]) for row in remaining_rows}
            removable_admission_ids = [admission_id for admission_id in admission_ids if admission_id not in remaining_admission_ids]

        if removable_admission_ids:
            if cls._table_exists_for_delete(cursor, "procedures"):
                placeholders = ", ".join("?" for _ in removable_admission_ids)
                procedure_rows = cursor.execute(
                    f"SELECT id FROM procedures WHERE admission_id IN ({placeholders})",
                    tuple(removable_admission_ids),
                ).fetchall()
                procedure_ids = [int(row["id"]) for row in procedure_rows]
                for table_name in (
                    "procedure_consents",
                    "procedure_cvc",
                    "procedure_lumbar_puncture",
                    "procedure_transfusion",
                ):
                    cls._delete_where_in(cursor, table_name, "procedure_id", procedure_ids)
                cls._delete_where_in(cursor, "procedures", "id", procedure_ids)

            order_ids: list[int] = []
            if cls._table_exists_for_delete(cursor, "orders"):
                placeholders = ", ".join("?" for _ in removable_admission_ids)
                order_rows = cursor.execute(
                    f"SELECT id FROM orders WHERE admission_id IN ({placeholders})",
                    tuple(removable_admission_ids),
                ).fetchall()
                order_ids = [int(row["id"]) for row in order_rows]
            cls._delete_where_in(cursor, "administrations", "order_id", order_ids)
            cls._delete_where_in(cursor, "orders", "id", order_ids)

            for table_name in (
                "order_audit_log",
                "change_log",
                "medical_audit_log",
                "patient_status_events",
                "vital_settings",
                "vitals",
                "fluids",
                "operations",
                "ivl_episodes",
                "transfusions",
                "clinical_events",
                "devices",
                "respiratory_support",
                "lab_data",
                "lab_orders",
                "diet_plan",
                "oral_intake_events",
            ):
                cls._delete_where_in(cursor, table_name, "admission_id", removable_admission_ids)

            if cls._table_exists_for_delete(cursor, "beds"):
                placeholders = ", ".join("?" for _ in removable_admission_ids)
                cursor.execute(
                    f"UPDATE beds SET current_admission_id = NULL WHERE current_admission_id IN ({placeholders})",
                    tuple(removable_admission_ids),
                )
            cls._delete_where_in(cursor, "admissions", "id", removable_admission_ids)

        removable_patient_ids: list[int] = []
        if patient_ids:
            placeholders = ", ".join("?" for _ in patient_ids)
            remaining_rows = cursor.execute(
                f"""
                SELECT DISTINCT patient_id
                FROM admissions
                WHERE patient_id IN ({placeholders})
                """,
                tuple(patient_ids),
            ).fetchall()
            remaining_patient_ids = {int(row["patient_id"]) for row in remaining_rows}
            removable_patient_ids = [patient_id for patient_id in patient_ids if patient_id not in remaining_patient_ids]
        cls._delete_where_in(cursor, "patients", "id", removable_patient_ids)

        return {"deleted": deleted_cases}

    def delete_archived_operation_case(self, operation_case_id: int) -> dict[str, int]:
        validate_operblock_runtime_path(self.db)

        def operation(cursor: sqlite3.Cursor):
            case = cursor.execute(
                """
                SELECT oc.id, oc.patient_id, oc.admission_id, oc.status
                FROM operation_cases oc
                JOIN admissions a ON a.id = oc.admission_id
                WHERE oc.id = ?
                  AND COALESCE(a.unit_scope, '') = 'operblock'
                """,
                (int(operation_case_id),),
            ).fetchone()
            if not case:
                raise OperBlockConflictError("Архивный случай не найден.")
            if str(case["status"] or "") not in {"closed", "cancelled"}:
                raise OperBlockConflictError("Удалить можно только пациента из архива.")
            result = self._hard_delete_archived_operation_cases(cursor, [case])
            if int(result.get("deleted") or 0) != 1:
                raise OperBlockConflictError("Случай изменён другим рабочим местом. Обновите архив.")
            return {"operation_case_id": int(operation_case_id), "admission_id": int(case["admission_id"])}

        return dict(self.db.run_write_operation(operation, source="operblock_delete_archived_case"))

    def delete_all_archived_operation_cases(self, table_code: str | None = None) -> dict[str, int]:
        validate_operblock_runtime_path(self.db)
        clean_table_code = self._validate_table_code(table_code) if table_code else None

        def operation(cursor: sqlite3.Cursor):
            table_clause = ""
            params: list[Any] = []
            if clean_table_code:
                table_clause = "AND oc.table_code = ?"
                params.append(clean_table_code)
            rows = cursor.execute(
                f"""
                SELECT oc.id, oc.patient_id, oc.admission_id
                FROM operation_cases oc
                JOIN admissions a ON a.id = oc.admission_id
                WHERE oc.status IN ('closed', 'cancelled')
                  AND COALESCE(a.unit_scope, '') = 'operblock'
                  {table_clause}
                """,
                tuple(params),
            ).fetchall()
            if not rows:
                return {"deleted": 0}
            return self._hard_delete_archived_operation_cases(cursor, list(rows))

        return dict(self.db.run_write_operation(operation, source="operblock_delete_all_archived_cases"))

    def build_operblock_patient_header_snapshot(self, operation_case_id: int) -> dict[str, Any]:
        case = self._get_case_row(operation_case_id)
        latest = self.get_latest_vital_values(int(case["admission_id"]))
        stage_state = self.build_operation_stage_state(operation_case_id)
        payload = {
            "operation_case_id": int(case["operation_case_id"]),
            "patient_id": int(case["patient_id"]),
            "admission_id": int(case["admission_id"]),
            "status": case["case_status"],
            "status_text": "В операционной" if case["case_status"] == "active" else "Случай закрыт",
            "table_code": case["table_code"],
            "table_display_name": case["table_display_name"],
            "history_number": case["history_number"],
            "full_name": case["full_name"],
            "age": _age_text(case),
            "gender": case["patient_gender"],
            "birth_date": case["birth_date"],
            "diagnosis_code": case["diagnosis_code"],
            "diagnosis_text": case["diagnosis_text"],
            "department_profile": case.get("department_profile") or "",
            "started_at": case["started_at"],
            "ended_at": case["ended_at"],
            "protocol_number": case.get("anesthesia_protocol_number"),
            "protocol_date": case.get("anesthesia_protocol_date"),
            "protocol_display": format_operblock_protocol_display(
                case.get("anesthesia_protocol_number"),
                case.get("anesthesia_protocol_date"),
            ),
            "operation_name": case.get("planned_operation_name") or "",
            "anesthesia_assistance_type": normalize_operblock_anesthesia_type_label(
                case.get("planned_anesthesia_assistance_type")
            ),
            "surgeons": _surgeons_from_json(case.get("planned_surgeons_json")),
            "operating_nurse": case.get("planned_operating_nurse") or "",
            "anesthesiologist": case.get("planned_anesthesiologist") or "",
            "anesthetist": case.get("planned_anesthetist") or "",
            "transfer_department": case.get("transfer_department") or "",
            "transfer_department_target": transfer_department_target_text(case.get("transfer_department") or ""),
            "latest": latest,
            "stage_state": stage_state,
        }
        payload["content_hash"] = _hash_payload(payload)
        return payload

    def build_operation_stage_state(self, operation_case_id: int) -> dict[str, Any]:
        case = self._get_case_row(operation_case_id)
        rows = self._fetch_operblock_timeline_event_rows(
            int(case["admission_id"]),
            operation_case_id=int(operation_case_id),
        )
        stage_rows = _stage_rows_from_timeline_rows(rows)
        state = _build_stage_intervals(stage_rows)
        state["events"] = [
            {
                "id": int(row.get("id") or 0),
                "kind": str(row.get("stage_kind") or ""),
                "label": str(row.get("stage_label") or ""),
                "event_time": _iso_or_none(row.get("event_dt")),
                "revision": int(row.get("revision") or 0),
            }
            for row in stage_rows
        ]
        state["case_active"] = str(case.get("case_status") or "") == "active"
        state["case_started_at"] = case.get("started_at")
        state["case_ended_at"] = case.get("ended_at")
        state["planned_anesthesia_assistance_type"] = normalize_operblock_anesthesia_type_label(
            case.get("planned_anesthesia_assistance_type")
        )
        return state

    def build_operblock_vitals_snapshot(
        self,
        admission_id: int,
        *,
        operation_case_id: int | None = None,
    ) -> dict[str, Any]:
        params: list[Any] = [int(admission_id)]
        bounds_clause = ""
        case = self._get_case_row(operation_case_id) if operation_case_id else None
        if case:
            if int(case.get("admission_id") or 0) != int(admission_id):
                raise OperBlockConflictError("Операция не принадлежит выбранной госпитализации.")
            started_at = _parse_dt(case.get("started_at"))
            if started_at is None:
                bounds_clause = "AND 1 = 0"
            else:
                bounds_clause = 'AND datetime("datetime") >= datetime(?)'
                params.append(_minute_floor(started_at).isoformat())
                ended_at = _parse_dt(case.get("ended_at"))
                if ended_at is not None:
                    bounds_clause += ' AND datetime("datetime") <= datetime(?)'
                    params.append(_minute_floor(ended_at).isoformat())
        rows = self.db.fetch_all_remcard(
            f"""
            SELECT id, admission_id, datetime, sys, dia, pulse, spo2, COALESCE(revision, 0) AS revision
            FROM vitals
            WHERE admission_id = ?
              {bounds_clause}
            ORDER BY CAST(STRFTIME('%s', datetime) AS INTEGER) DESC, id DESC
            LIMIT 50
            """,
            tuple(params),
        )
        vitals = []
        for row in rows:
            data = _row_to_dict(row)
            vitals.append(
                {
                    "id": data.get("id"),
                    "datetime": data.get("datetime"),
                    "ad": self._format_ad(data.get("sys"), data.get("dia")),
                    "sys": data.get("sys"),
                    "dia": data.get("dia"),
                    "pulse": data.get("pulse"),
                    "spo2": data.get("spo2"),
                    "revision": int(data.get("revision") or 0),
                }
            )
        payload = {"admission_id": int(admission_id), "vitals": vitals}
        payload["content_hash"] = _hash_payload(payload)
        return payload

    def build_operblock_orders_snapshot(
        self,
        admission_id: int,
        *,
        operation_case_id: int | None = None,
    ) -> dict[str, Any]:
        case = self._get_case_row(operation_case_id) if operation_case_id else None
        if case:
            if int(case.get("admission_id") or 0) != int(admission_id):
                raise OperBlockConflictError("Операция не принадлежит выбранной госпитализации.")
        rows = self.db.fetch_all_remcard(
            """
            SELECT
                id,
                datetime,
                text,
                drug_key,
                status,
                is_committed,
                comment,
                created_at,
                updated_at,
                COALESCE(revision, 0) AS revision
            FROM orders
            WHERE admission_id = ?
              AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
            ORDER BY datetime DESC, id DESC
            LIMIT 500
            """,
            (int(admission_id),),
        )
        orders = self._apply_preset_display_names_to_order_rows([_row_to_dict(row) for row in rows])
        if case:
            timeline_rows = self._fetch_operblock_timeline_event_rows(
                int(admission_id),
                operation_case_id=int(case["operation_case_id"]),
            )
            intervals = self._all_anesthesia_intervals(_stage_rows_from_timeline_rows(timeline_rows))
            if intervals:
                orders = [row for row in orders if self._order_row_in_any_interval(row, intervals)]
            else:
                orders = []
        orders = orders[:100]
        payload = {"admission_id": int(admission_id), "orders": orders}
        payload["content_hash"] = _hash_payload(payload)
        return payload

    @staticmethod
    def _order_row_in_any_interval(row: dict[str, Any], intervals: list[tuple[datetime, datetime | None]]) -> bool:
        order_dt = _parse_dt((row or {}).get("datetime"))
        if order_dt is None:
            return False
        order_minute = _minute_floor(order_dt)
        for start, end in intervals:
            if order_minute < start:
                continue
            if end is not None and order_minute > end:
                continue
            return True
        return False

    @staticmethod
    def _operblock_preset_display_names_by_id() -> dict[str, str]:
        try:
            presets = load_operblock_medication_presets(include_disabled=True)
        except Exception:
            return {}
        result: dict[str, str] = {}
        for preset in presets:
            preset_id = str((preset or {}).get("preset_id") or "").strip()
            display_name = operblock_medication_preset_display_name(preset or {})
            if preset_id and display_name:
                result[preset_id] = display_name
        return result

    def _apply_preset_display_names_to_order_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        display_names = self._operblock_preset_display_names_by_id()
        if not display_names:
            return rows
        for row in rows:
            preset_id = str((row or {}).get("drug_key") or "").strip()
            display_name = display_names.get(preset_id)
            if display_name:
                row["drug_display_name"] = display_name
        return rows

    def build_operblock_timeline_snapshot(
        self,
        admission_id: int,
        operation_case_id: int | None = None,
    ) -> OperBlockTimelineSnapshot:
        case = self._get_case_row(operation_case_id) if operation_case_id else self._get_latest_case_row_for_admission(admission_id)
        if case and int(case.get("admission_id") or 0) != int(admission_id):
            raise OperBlockConflictError("Операция не принадлежит выбранной госпитализации.")
        orders_snapshot = self.build_operblock_orders_snapshot(
            admission_id,
            operation_case_id=int(case["operation_case_id"]) if case else None,
        )
        timeline_rows = self._fetch_operblock_timeline_event_rows(
            admission_id,
            operation_case_id=int(case["operation_case_id"]) if case else None,
        )
        snapshot = build_timeline_snapshot_from_legacy_orders(
            admission_id=int(admission_id),
            operation_case_id=int(case["operation_case_id"]) if case else None,
            table_code=str(case.get("table_code") or "") if case else None,
            order_rows=[_row_to_dict(row) for row in orders_snapshot.get("orders") or []],
            timeline_rows=timeline_rows,
            generated_at=_now_text(),
        )
        return self._apply_operation_end_to_timeline_snapshot(snapshot, _parse_dt(case.get("ended_at")) if case else None)

    @staticmethod
    def _apply_operation_end_to_timeline_snapshot(
        snapshot: OperBlockTimelineSnapshot,
        ended_at: datetime | None,
    ) -> OperBlockTimelineSnapshot:
        if ended_at is None:
            return snapshot
        end_minute = _minute_floor(ended_at)
        intervals = []
        changed = False
        for interval in snapshot.infusion_intervals:
            if interval.status == "active" and interval.end_time is None and interval.start_time <= end_minute:
                intervals.append(replace(interval, end_time=end_minute, status="stopped"))
                changed = True
            else:
                intervals.append(interval)
        if not changed:
            return snapshot
        return with_timeline_content_hash(
            OperBlockTimelineSnapshot(
                admission_id=snapshot.admission_id,
                operation_case_id=snapshot.operation_case_id,
                generated_at=snapshot.generated_at,
                bolus_events=snapshot.bolus_events,
                infusion_intervals=intervals,
                operation_events=snapshot.operation_events,
                source_versions=snapshot.source_versions,
            )
        )

    def build_operblock_protocol_snapshot(self, operation_case_id: int) -> dict[str, Any]:
        header = self.build_operblock_patient_header_snapshot(operation_case_id)
        admission_id = int(header["admission_id"])
        vitals = self.build_operblock_vitals_snapshot(admission_id, operation_case_id=operation_case_id)
        orders = self.build_operblock_orders_snapshot(admission_id, operation_case_id=operation_case_id)
        timeline = self.build_operblock_timeline_snapshot(admission_id, operation_case_id=operation_case_id)
        timeline_payload = timeline.to_dict()
        payload = {"header": header, "vitals": vitals, "orders": orders, "timeline": timeline_payload}
        hash_payload = dict(payload)
        timeline_hash_payload = dict(timeline_payload)
        timeline_hash_payload.pop("generated_at", None)
        hash_payload["timeline"] = timeline_hash_payload
        payload["content_hash"] = _hash_payload(hash_payload)
        return payload

    def build_operation_report_context(self, operation_case_id: int) -> dict[str, Any]:
        snapshot = self.build_operblock_protocol_snapshot(operation_case_id)
        header = dict(snapshot.get("header") or {})
        stage_state = dict(header.get("stage_state") or {})
        timeline = dict(snapshot.get("timeline") or {})
        case = self._get_case_row(operation_case_id)
        vitals = [
            {
                "id": vital.id,
                "datetime": vital.timestamp.isoformat(timespec="seconds"),
                "sys": vital.sys,
                "dia": vital.dia,
                "pulse": vital.pulse,
                "spo2": vital.spo2,
                "temp": vital.temp,
                "rr": vital.rr,
                "cvp": vital.cvp,
            }
            for vital in self.list_operation_vitals(operation_case_id)
        ]
        stage_events = self._operation_report_stage_events(timeline)
        transfer_department = normalize_operblock_transfer_department(
            case.get("transfer_department")
            or header.get("transfer_department")
            or self._transfer_department_from_stage_events(stage_events)
        )
        anesthesia_interval = self._report_first_last_interval(stage_state.get("anesthesia_intervals"))
        surgery_interval = self._report_first_last_interval(stage_state.get("surgery_intervals"))
        controlled_medications = self._operation_report_controlled_medications(timeline)
        report = {
            "generated_at": _now_text(),
            "operation_case_id": int(operation_case_id),
            "patient": {
                "full_name": header.get("full_name") or "",
                "history_number": header.get("history_number") or "",
                "age": header.get("age") or "",
                "birth_date": header.get("birth_date") or "",
                "diagnosis_code": header.get("diagnosis_code") or "",
                "diagnosis_text": header.get("diagnosis_text") or "",
                "department_profile": header.get("department_profile") or "",
            },
            "case": {
                "table_code": header.get("table_code") or "",
                "table_display_name": header.get("table_display_name") or "",
                "admission_started_at": header.get("started_at"),
                "closed_at": header.get("ended_at"),
                "protocol_number": header.get("protocol_number"),
                "protocol_date": header.get("protocol_date"),
                "protocol_display": header.get("protocol_display") or "",
                "operation_name": self._report_operation_name(case, stage_state),
                "surgeons": self._report_surgeons(case, stage_state),
                "anesthesia_type": self._report_anesthesia_type(case, stage_state),
                "anesthesiologist": self._report_anesthesiologist(case, stage_state),
                "anesthetist": self._report_anesthetist(case, stage_state),
                "operating_nurse": self._report_operating_nurse(case, stage_state),
                "surgery_start": surgery_interval.get("start"),
                "surgery_end": surgery_interval.get("end"),
                "surgery_duration_minutes": self._duration_minutes(surgery_interval.get("start"), surgery_interval.get("end")),
                "anesthesia_start": anesthesia_interval.get("start"),
                "anesthesia_end": anesthesia_interval.get("end"),
                "anesthesia_duration_minutes": self._duration_minutes(anesthesia_interval.get("start"), anesthesia_interval.get("end")),
                "transfer_department": transfer_department,
                "transfer_department_target": transfer_department_target_text(transfer_department),
            },
            "stages": stage_events,
            "vitals": vitals,
            "timeline": timeline,
            "medications": self._operation_report_medications(timeline),
            "controlled_medications": controlled_medications,
        }
        report["content_hash"] = _hash_payload(report)
        return report

    @staticmethod
    def _transfer_department_from_stage_events(stage_events: list[dict[str, Any]]) -> str:
        for event in reversed(stage_events or []):
            payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
            department = normalize_operblock_transfer_department((payload or {}).get("transfer_department"))
            if department:
                return department
        return ""

    @staticmethod
    def _report_first_last_interval(intervals: Any) -> dict[str, Any]:
        rows = [dict(row or {}) for row in (intervals or []) if isinstance(row, Mapping)]
        rows = [row for row in rows if row.get("start")]
        rows.sort(key=lambda row: _parse_dt(row.get("start")) or datetime.max)
        if not rows:
            return {}
        first = rows[0]
        last_with_end = next((row for row in reversed(rows) if row.get("end")), rows[-1])
        return {"start": first.get("start"), "end": last_with_end.get("end")}

    @staticmethod
    def _duration_minutes(start_value: Any, end_value: Any) -> int | None:
        start_dt = _parse_dt(start_value)
        end_dt = _parse_dt(end_value)
        if start_dt is None or end_dt is None or end_dt < start_dt:
            return None
        return int(round((end_dt - start_dt).total_seconds() / 60.0))

    @staticmethod
    def _operation_report_stage_events(timeline: Mapping[str, Any] | dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for event in list((timeline or {}).get("operation_events") or []):
            data = dict(event or {})
            payload = data.get("payload") if isinstance(data.get("payload"), Mapping) else {}
            kind = operation_stage_kind_from_payload(payload)
            if not kind:
                continue
            label = _normalize_case_text(
                (payload or {}).get("label")
                or data.get("display_label")
                or data.get("raw_text")
                or OPERBLOCK_STAGE_KIND_LABELS.get(kind)
                or ""
            )
            rows.append(
                {
                    "kind": kind,
                    "label": label or OPERBLOCK_STAGE_KIND_LABELS.get(kind, "Этап операции"),
                    "event_time": data.get("event_time"),
                    "payload": dict(payload or {}),
                    "source_id": int(data.get("source_id") or 0),
                }
            )
        rows.sort(key=lambda item: (_parse_dt(item.get("event_time")) or datetime.min, int(item.get("source_id") or 0)))
        return rows

    @staticmethod
    def _report_operation_name(case: dict[str, Any], stage_state: dict[str, Any]) -> str:
        return _normalize_case_text(
            stage_state.get("last_operation_name")
            or stage_state.get("first_operation_name")
            or case.get("planned_operation_name")
            or ""
        )

    @staticmethod
    def _report_surgeons(case: dict[str, Any], stage_state: dict[str, Any]) -> list[str]:
        values = _normalize_stage_text_list(stage_state.get("last_surgeons") or stage_state.get("first_surgeons"))
        if values:
            return values
        return _surgeons_from_json(case.get("planned_surgeons_json"))

    @staticmethod
    def _report_anesthesia_type(case: dict[str, Any], stage_state: dict[str, Any]) -> str:
        return normalize_operblock_anesthesia_type_label(
            stage_state.get("last_anesthesia_assistance_type")
            or stage_state.get("first_anesthesia_assistance_type")
            or stage_state.get("current_anesthesia_assistance_type")
            or case.get("planned_anesthesia_assistance_type")
            or ""
        )

    @staticmethod
    def _report_anesthesiologist(case: dict[str, Any], stage_state: dict[str, Any]) -> str:
        return _normalize_case_text(
            stage_state.get("last_anesthesiologist")
            or stage_state.get("first_anesthesiologist")
            or case.get("planned_anesthesiologist")
            or ""
        )

    @staticmethod
    def _report_anesthetist(case: dict[str, Any], stage_state: dict[str, Any]) -> str:
        return _normalize_case_text(
            stage_state.get("last_anesthetist")
            or stage_state.get("first_anesthetist")
            or case.get("planned_anesthetist")
            or ""
        )

    @staticmethod
    def _report_operating_nurse(case: dict[str, Any], stage_state: dict[str, Any]) -> str:
        return _normalize_case_text(
            stage_state.get("last_operating_nurse")
            or stage_state.get("first_operating_nurse")
            or case.get("planned_operating_nurse")
            or ""
        )

    @staticmethod
    def _operation_report_medications(timeline: Mapping[str, Any] | dict[str, Any]) -> dict[str, Any]:
        boluses = []
        for event in list((timeline or {}).get("bolus_events") or []):
            payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
            boluses.append(
                {
                    "time": event.get("event_time"),
                    "name": _normalize_case_text(event.get("drug_label") or event.get("display_label") or event.get("raw_text")),
                    "display": _normalize_case_text(event.get("display_label") or event.get("raw_text") or event.get("drug_label")),
                    "dose_value": event.get("dose_value"),
                    "dose_unit": event.get("dose_unit"),
                    "volume_ml": event.get("volume_ml"),
                    "concentration_text": event.get("concentration_text"),
                    "route": event.get("route") or (payload or {}).get("route"),
                }
            )
        infusions = []
        for interval in list((timeline or {}).get("infusion_intervals") or []):
            payload = interval.get("payload") if isinstance(interval.get("payload"), Mapping) else {}
            infusions.append(
                {
                    "start": interval.get("start_time"),
                    "end": interval.get("end_time"),
                    "status": interval.get("status"),
                    "name": _normalize_case_text(
                        (payload or {}).get("display_name")
                        or (payload or {}).get("label")
                        or interval.get("drug_label")
                        or interval.get("display_label")
                    ),
                    "display": _normalize_case_text(interval.get("display_label") or interval.get("drug_label")),
                    "volume_ml": interval.get("volume_ml"),
                    "rate_value": interval.get("current_rate_value"),
                    "rate_unit": interval.get("current_rate_unit"),
                    "payload": dict(payload or {}),
                    "rate_history": list(interval.get("rate_history") or []),
                    "dose_history": list(interval.get("dose_history") or []),
                }
            )
        return {"boluses": boluses, "infusions": infusions}

    @staticmethod
    def _report_medication_identity_key(value: Any) -> str:
        text = _normalize_case_text(value).casefold().replace("ё", "е")
        text = re.sub(r"\s+\d+(?:[.,]\d+)?\s*%$", "", text).strip()
        return text

    @classmethod
    def _controlled_prescription_rules(cls, presets: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if presets is None:
            try:
                presets = load_operblock_medication_presets(include_disabled=True)
            except Exception:
                presets = []
        by_id: dict[str, bool] = {}
        name_keys: set[str] = set()
        for preset in presets or []:
            data = dict(preset or {})
            required = operblock_medication_preset_requires_narcotic_sheet(data)
            preset_id = str(data.get("preset_id") or "").strip()
            if preset_id:
                by_id[preset_id] = required
            if not required:
                continue
            for value in (
                operblock_medication_preset_display_name(data),
                data.get("label"),
                data.get("latin"),
                data.get("drug_name"),
                data.get("drug"),
            ):
                key = cls._report_medication_identity_key(value)
                if key:
                    name_keys.add(key)
        return {"by_id": by_id, "name_keys": name_keys}

    @classmethod
    def _controlled_prescription_required(cls, data: Mapping[str, Any], rules: Mapping[str, Any]) -> bool:
        payload = data.get("payload") if isinstance(data.get("payload"), Mapping) else {}
        preset_id = str((payload or {}).get("preset_id") or data.get("preset_id") or "").strip()
        by_id = rules.get("by_id") if isinstance(rules.get("by_id"), Mapping) else {}
        if preset_id and preset_id in by_id:
            return bool(by_id[preset_id])
        if preset_id and operblock_medication_preset_requires_narcotic_sheet(payload or {}):
            return True

        name_keys = rules.get("name_keys") if isinstance(rules.get("name_keys"), set) else set()
        if not name_keys:
            return False
        for value in (
            data.get("drug_label"),
            data.get("display_label"),
            data.get("name"),
            data.get("display"),
            data.get("raw_text"),
            (payload or {}).get("display_name"),
            (payload or {}).get("label"),
            (payload or {}).get("latin"),
        ):
            key = cls._report_medication_identity_key(value)
            if key and key in name_keys:
                return True
        return False

    @staticmethod
    def _controlled_decimal(value: Any) -> Decimal | None:
        text = str(value or "").strip().replace(",", ".")
        if not text:
            return None
        try:
            return Decimal(text)
        except (InvalidOperation, ValueError):
            return None

    @staticmethod
    def _controlled_format_decimal(value: Decimal) -> str:
        normalized = value.normalize()
        if normalized == normalized.to_integral():
            return str(normalized.quantize(Decimal("1")))
        return format(normalized, "f").rstrip("0").rstrip(".").replace(".", ",")

    @classmethod
    def _controlled_volume_decimal_ml(cls, value: Any) -> Decimal | None:
        text = _normalize_case_text(value)
        if not text:
            return None
        text = re.sub(r"\s*мл\s*$", "", text, flags=re.IGNORECASE).strip()
        return cls._controlled_decimal(text)

    @staticmethod
    def _controlled_dose_unit_key(unit: Any) -> str:
        raw = re.sub(r"\s+", "", str(unit or "").strip().casefold()).replace("ё", "е").replace("µ", "мк")
        raw = raw.rstrip(".")
        aliases = {
            "mg": "мг",
            "мг": "мг",
            "mkg": "мкг",
            "mcg": "мкг",
            "ug": "мкг",
            "мкг": "мкг",
            "мкгр": "мкг",
            "g": "г",
            "гр": "г",
            "г": "г",
            "ml": "мл",
            "мл": "мл",
        }
        return aliases.get(raw, raw)

    @classmethod
    def _controlled_dose_value_unit(
        cls,
        data: Mapping[str, Any],
        *,
        dose_text: Any = None,
        display_text: Any = None,
    ) -> tuple[Decimal | None, str]:
        value = cls._controlled_decimal(data.get("dose_value"))
        unit = _normalize_case_text(data.get("dose_unit"))
        if value is not None and unit:
            return value, unit

        source_text = " ".join(
            _normalize_case_text(value)
            for value in (dose_text, display_text, data.get("display_label"), data.get("raw_text"))
            if _normalize_case_text(value)
        )
        match = re.search(
            r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>мкгр|мкг|мг|мл|гр|г|mcg|mkg|mg|ml|ug|g)\b",
            source_text,
            flags=re.IGNORECASE,
        )
        if not match:
            return None, ""
        return cls._controlled_decimal(match.group("value")), _normalize_case_text(match.group("unit"))

    @classmethod
    def _controlled_concentration_mg_per_ml(cls, data: Mapping[str, Any], display_text: Any) -> Decimal | None:
        payload = data.get("payload") if isinstance(data.get("payload"), Mapping) else {}
        source_text = " ".join(
            _normalize_case_text(value)
            for value in (
                data.get("concentration_text"),
                (payload or {}).get("concentration"),
                (payload or {}).get("concentration_text"),
                display_text,
            )
            if _normalize_case_text(value)
        )
        ratio_match = re.search(
            r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>мкг|мкгр|mcg|mkg|ug|мг|mg|г|гр|g)\s*/\s*(?:мл|ml)\b",
            source_text,
            flags=re.IGNORECASE,
        )
        if ratio_match:
            value = cls._controlled_decimal(ratio_match.group("value"))
            if value is None or value <= 0:
                return None
            unit_key = cls._controlled_dose_unit_key(ratio_match.group("unit"))
            if unit_key == "мг":
                return value
            if unit_key == "г":
                return value * Decimal("1000")
            if unit_key == "мкг":
                return value / Decimal("1000")
            return None

        percent_match = re.search(r"(?P<percent>\d+(?:[.,]\d+)?)\s*%", source_text)
        if not percent_match:
            return None
        percent = cls._controlled_decimal(percent_match.group("percent"))
        if percent is None or percent <= 0:
            return None
        return percent * Decimal("10")

    @classmethod
    def _controlled_calculated_volume_ml(
        cls,
        data: Mapping[str, Any],
        *,
        dose_text: Any = None,
        display_text: Any = None,
    ) -> Decimal | None:
        payload = data.get("payload") if isinstance(data.get("payload"), Mapping) else {}
        for value in (
            data.get("volume_ml"),
            (payload or {}).get("calculated_volume_ml"),
            (payload or {}).get("volume_ml"),
        ):
            explicit = cls._controlled_volume_decimal_ml(value)
            if explicit is not None:
                return explicit

        dose_value, dose_unit = cls._controlled_dose_value_unit(
            data,
            dose_text=dose_text,
            display_text=display_text,
        )
        if dose_value is None:
            return None
        unit_key = cls._controlled_dose_unit_key(dose_unit)
        if unit_key == "мл":
            return dose_value
        dose_mg: Decimal | None = None
        if unit_key == "мг":
            dose_mg = dose_value
        elif unit_key == "г":
            dose_mg = dose_value * Decimal("1000")
        elif unit_key == "мкг":
            dose_mg = dose_value / Decimal("1000")
        if dose_mg is None:
            return None
        concentration = cls._controlled_concentration_mg_per_ml(data, display_text)
        if concentration is None or concentration <= 0:
            return None
        return dose_mg / concentration

    @classmethod
    def _controlled_prescription_name(cls, data: Mapping[str, Any], *, dose_text: Any = None) -> str:
        payload = data.get("payload") if isinstance(data.get("payload"), Mapping) else {}
        display = _normalize_case_text(
            data.get("display_label")
            or data.get("display")
            or data.get("raw_text")
            or data.get("name")
            or data.get("drug_label")
            or (payload or {}).get("display_name")
            or (payload or {}).get("label")
        )
        dose = _normalize_case_text(
            dose_text
            or (payload or {}).get("display_dose_text")
            or (payload or {}).get("dose_text")
        )
        if display and dose and dose.casefold() not in display.casefold():
            result = f"{display} {dose}".strip()
        else:
            result = display or dose or "Препарат"

        dose_value, dose_unit = cls._controlled_dose_value_unit(data, dose_text=dose, display_text=result)
        volume_ml = cls._controlled_calculated_volume_ml(data, dose_text=dose, display_text=result)
        volume_text = f"{cls._controlled_format_decimal(volume_ml)} мл" if volume_ml is not None else ""
        if (
            volume_ml is not None
            and cls._controlled_dose_unit_key(dose_unit) != "мл"
            and not re.search(r"\(\s*\d+(?:[.,]\d+)?\s*мл\s*\)", result, flags=re.IGNORECASE)
            and volume_text.casefold() not in result.casefold()
        ):
            result = f"{result} ({volume_text})"
        return result

    @classmethod
    def _operation_report_controlled_medications(
        cls,
        timeline: Mapping[str, Any] | dict[str, Any],
        *,
        presets: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        rules = cls._controlled_prescription_rules(presets)
        rows: list[dict[str, Any]] = []
        sequence = 0

        def add_row(time_value: Any, data: Mapping[str, Any], *, name: str | None = None) -> None:
            nonlocal sequence
            event_dt = _parse_dt(time_value)
            if event_dt is None:
                return
            sequence += 1
            rows.append(
                {
                    "datetime": _minute_floor(event_dt).isoformat(timespec="seconds"),
                    "name": _normalize_case_text(name) or cls._controlled_prescription_name(data),
                    "source": str(data.get("source") or data.get("id") or ""),
                    "source_id": int(data.get("source_id") or 0),
                    "sequence": sequence,
                }
            )

        for event in list((timeline or {}).get("bolus_events") or []):
            data = dict(event or {})
            if not cls._controlled_prescription_required(data, rules):
                continue
            add_row(data.get("event_time"), data)

        for interval in list((timeline or {}).get("infusion_intervals") or []):
            data = dict(interval or {})
            if not cls._controlled_prescription_required(data, rules):
                continue
            payload = data.get("payload") if isinstance(data.get("payload"), Mapping) else {}
            kind = str((payload or {}).get("kind") or "").strip().casefold()
            if kind == "gas":
                dose_history = [item for item in list(data.get("dose_history") or []) if isinstance(item, Mapping)]
                if dose_history:
                    for item in dose_history:
                        name = cls._controlled_prescription_name(data, dose_text=item.get("dose_text"))
                        add_row(item.get("event_time"), data, name=name)
                    continue
            add_row(data.get("start_time"), data)

        rows.sort(key=lambda item: (_parse_dt(item.get("datetime")) or datetime.max, int(item.get("sequence") or 0)))
        return rows

    def build_operation_report_pdf_path(self, operation_case_id: int) -> Path:
        self.cleanup_operation_report_dir()
        case = self._get_case_row(operation_case_id)
        patient_name = self._safe_report_filename(case.get("full_name") or "patient")
        protocol_display = format_operblock_protocol_display(
            case.get("anesthesia_protocol_number"),
            case.get("anesthesia_protocol_date"),
        )
        protocol_slug = self._safe_report_filename(protocol_display or f"case_{int(operation_case_id)}")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_dir = Path(REPORT_DIR) / "operblock"
        return report_dir / f"{patient_name}_protocol_{protocol_slug}_{stamp}.pdf"

    def build_operation_report_pdf(self, operation_case_id: int, pdf_path) -> Path:
        from rem_card.services.operblock_reportlab_builder import OperBlockReportLabBuilder

        output_path = Path(pdf_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        context = self.build_operation_report_context(operation_case_id)
        OperBlockReportLabBuilder.build_pdf(context, output_path)
        return output_path

    @staticmethod
    def cleanup_operation_report_dir() -> None:
        report_dir = Path(REPORT_DIR) / "operblock"
        report_dir.mkdir(parents=True, exist_ok=True)
        cutoff = datetime.now() - timedelta(days=OPERBLOCK_REPORT_RETENTION_DAYS)
        for path in report_dir.glob("*.pdf"):
            try:
                if not path.is_file():
                    continue
                modified_at = datetime.fromtimestamp(path.stat().st_mtime)
                if modified_at < cutoff:
                    path.unlink()
            except Exception as exc:
                logger.warning("Не удалось удалить старый отчет оперблока path=%s error=%s", path, exc)

    @staticmethod
    def _safe_report_filename(value: Any) -> str:
        text = re.sub(r"\s+", "_", str(value or "").strip())
        text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
        text = re.sub(r"_+", "_", text).strip("._ ")
        return text[:80] or "report"

    def list_operation_vitals(self, operation_case_id: int) -> list[VitalDTO]:
        case = self._get_case_row(operation_case_id)
        admission_id = int(case["admission_id"])
        started_at = _parse_dt(case.get("started_at"))
        ended_at = _parse_dt(case.get("ended_at"))
        if started_at is None:
            return []
        params: list[Any] = [admission_id, _minute_floor(started_at).isoformat()]
        end_clause = ""
        if ended_at is not None:
            end_clause = 'AND datetime("datetime") <= datetime(?)'
            params.append(ended_at.isoformat())
        rows = self.db.fetch_all_remcard(
            f"""
            SELECT id, admission_id, datetime, sys, dia, pulse, temp, spo2, rr, cvp,
                   last_modified_by, updated_at, COALESCE(revision, 0) AS revision
            FROM vitals
            WHERE admission_id = ?
              AND datetime("datetime") >= datetime(?)
              {end_clause}
            ORDER BY datetime("datetime") ASC, id ASC
            """,
            tuple(params),
        )
        result = []
        for row in rows:
            data = _row_to_dict(row)
            timestamp = _parse_dt(data.get("datetime"))
            if timestamp is None:
                continue
            result.append(
                VitalDTO(
                    id=data.get("id"),
                    admission_id=data.get("admission_id"),
                    timestamp=timestamp,
                    sys=data.get("sys"),
                    dia=data.get("dia"),
                    pulse=data.get("pulse"),
                    temp=data.get("temp"),
                    spo2=data.get("spo2"),
                    rr=data.get("rr"),
                    cvp=data.get("cvp"),
                    last_modified_by=data.get("last_modified_by"),
                    updated_at=data.get("updated_at"),
                    revision=data.get("revision"),
                )
            )
        return result

    def _operation_has_vitals_between(
        self,
        admission_id: int,
        started_at: datetime,
        ended_at: datetime | None = None,
        *,
        cursor: sqlite3.Cursor | None = None,
    ) -> bool:
        params: list[Any] = [int(admission_id), _minute_floor(started_at).isoformat()]
        end_clause = ""
        if ended_at is not None:
            end_clause = 'AND datetime("datetime") <= datetime(?)'
            params.append(_minute_floor(ended_at).isoformat())
        query = f"""
            SELECT 1
            FROM vitals
            WHERE admission_id = ?
              AND datetime("datetime") >= datetime(?)
              {end_clause}
            LIMIT 1
        """
        if cursor is not None:
            return bool(cursor.execute(query, tuple(params)).fetchone())
        return bool(self.db.fetch_one_remcard(query, tuple(params)))

    def operation_has_initial_vitals(self, operation_case_id: int) -> bool:
        case = self._get_case_row(operation_case_id)
        started_at = _parse_dt(case.get("started_at"))
        if started_at is None:
            return False
        ended_at = _parse_dt(case.get("ended_at")) if case.get("ended_at") else None
        return self._operation_has_vitals_between(
            int(case["admission_id"]),
            _minute_floor(started_at),
            _minute_floor(ended_at) if ended_at is not None else None,
        )

    def _first_operation_vital_row(
        self,
        cursor: sqlite3.Cursor,
        case: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        started_at = _parse_dt(case.get("started_at"))
        if started_at is None:
            return None
        params: list[Any] = [int(case["admission_id"]), _minute_floor(started_at).isoformat()]
        end_clause = ""
        ended_at = _parse_dt(case.get("ended_at")) if case.get("ended_at") else None
        if ended_at is not None:
            end_clause = 'AND datetime("datetime") <= datetime(?)'
            params.append(_minute_floor(ended_at).isoformat())
        row = cursor.execute(
            f"""
            SELECT id, admission_id, datetime, sys, dia, pulse, spo2, COALESCE(revision, 0) AS revision
            FROM vitals
            WHERE admission_id = ?
              AND datetime("datetime") >= datetime(?)
              {end_clause}
              AND (sys IS NOT NULL OR dia IS NOT NULL OR pulse IS NOT NULL OR spo2 IS NOT NULL)
            ORDER BY datetime("datetime") ASC, id ASC
            LIMIT 1
            """,
            tuple(params),
        ).fetchone()
        return _row_to_dict(row) if row else None

    def _upsert_initial_vitals_for_case(
        self,
        cursor: sqlite3.Cursor,
        case: Mapping[str, Any],
        data: OperBlockPatientInput,
    ) -> int | None:
        if not _has_case_vitals(data):
            return None
        started_at = _parse_dt(case.get("started_at"))
        if started_at is None:
            raise OperBlockConflictError("У операции не задано время начала. Обновите протокол.")
        existing = self._first_operation_vital_row(cursor, case)
        if existing:
            cursor.execute(
                """
                UPDATE vitals
                SET sys = ?,
                    dia = ?,
                    pulse = ?,
                    spo2 = ?,
                    last_modified_by = 'operblock',
                    revision = COALESCE(revision, 0) + 1,
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                """,
                (data.preop_sys, data.preop_dia, data.preop_pulse, data.preop_spo2, int(existing["id"])),
            )
            return int(existing["id"])
        cursor.execute(
            """
            INSERT INTO vitals (
                admission_id, datetime, sys, dia, pulse, spo2, last_modified_by, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'operblock', STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
            """,
            (
                int(case["admission_id"]),
                _minute_floor(started_at).isoformat(timespec="seconds"),
                data.preop_sys,
                data.preop_dia,
                data.preop_pulse,
                data.preop_spo2,
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _same_minute(left: datetime | None, right: datetime | None) -> bool:
        if left is None or right is None:
            return False
        return _minute_floor(left) == _minute_floor(right)

    def _started_at_edit_vital_rows(
        self,
        cursor: sqlite3.Cursor,
        admission_id: int,
    ) -> list[dict[str, Any]]:
        rows = cursor.execute(
            """
            SELECT id, admission_id, datetime, sys, dia, pulse, temp, spo2, rr, cvp,
                   COALESCE(revision, 0) AS revision
            FROM vitals
            WHERE admission_id = ?
            ORDER BY datetime("datetime") ASC, id ASC
            LIMIT 2
            """,
            (int(admission_id),),
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def _editable_initial_vital_row_for_started_at(
        self,
        cursor: sqlite3.Cursor,
        case: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        rows = self._started_at_edit_vital_rows(cursor, int(case["admission_id"]))
        if not rows:
            return None
        if len(rows) != 1:
            return None
        row = rows[0]
        started_at = _parse_dt(case.get("started_at"))
        vital_dt = _parse_dt(row.get("datetime"))
        if not self._same_minute(started_at, vital_dt):
            return None
        if any(row.get(field) is not None for field in ("temp", "rr", "cvp")):
            return None
        return row

    def _operation_started_at_edit_lock_reason(
        self,
        cursor: sqlite3.Cursor,
        case: Mapping[str, Any],
    ) -> str:
        operation_case_id = int(case["operation_case_id"])
        admission_id = int(case["admission_id"])
        event = cursor.execute(
            """
            SELECT id
            FROM operblock_timeline_events
            WHERE operation_case_id = ?
              AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
            LIMIT 1
            """,
            (operation_case_id,),
        ).fetchone()
        if event:
            return "В карте уже есть этапы, пособие, операция или события введения препаратов."

        order = cursor.execute(
            """
            SELECT id
            FROM orders
            WHERE admission_id = ?
              AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
            LIMIT 1
            """,
            (admission_id,),
        ).fetchone()
        if order:
            return "В карте уже есть назначения."

        vital_rows = self._started_at_edit_vital_rows(cursor, admission_id)
        if not vital_rows:
            return ""
        if len(vital_rows) > 1:
            return "В карте уже есть витальные показатели."
        if self._editable_initial_vital_row_for_started_at(cursor, case) is None:
            return "В карте уже есть витальные показатели."
        return ""

    def _assert_started_at_can_be_changed(
        self,
        cursor: sqlite3.Cursor,
        case: Mapping[str, Any],
    ) -> None:
        reason = self._operation_started_at_edit_lock_reason(cursor, case)
        if reason:
            raise ValueError(
                "Время поступления в оперблок можно изменить только до внесения данных в карту. "
                "Отмените внесённые изменения и повторите попытку. "
                f"Причина: {reason}"
            )

    @staticmethod
    def _set_stage_payload_text(payload: dict[str, Any], key: str, value: str) -> None:
        if value:
            payload[key] = value
        else:
            payload.pop(key, None)

    def _sync_case_metadata_to_stage_payloads(
        self,
        cursor: sqlite3.Cursor,
        operation_case_id: int,
        *,
        operation_name: str | None = None,
        anesthesia_assistance_type: str | None = None,
        surgeons: tuple[str, ...] | list[str] | None = None,
        operating_nurse: str | None = None,
        anesthesiologist: str | None = None,
        anesthetist: str | None = None,
    ) -> int:
        rows = [
            _row_to_dict(row)
            for row in cursor.execute(
                """
                SELECT id, payload_json
                FROM operblock_timeline_events
                WHERE operation_case_id = ?
                  AND event_type = 'clinical_event'
                  AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                ORDER BY datetime(event_time) ASC, id ASC
                """,
                (int(operation_case_id),),
            ).fetchall()
        ]

        def latest_stage_row(kind: str) -> dict[str, Any] | None:
            for row in reversed(rows):
                payload = _parse_json_dict(row.get("payload_json"))
                if operation_stage_kind_from_payload(payload) == kind:
                    return row
            return None

        def update_payload(row: dict[str, Any], payload: dict[str, Any]) -> bool:
            payload_json = self._timeline_payload_json(payload)
            if payload_json == row.get("payload_json"):
                return False
            cursor.execute(
                """
                UPDATE operblock_timeline_events
                SET payload_json = ?,
                    revision = COALESCE(revision, 0) + 1,
                    last_modified_by = 'operblock',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                  AND event_type = 'clinical_event'
                  AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                """,
                (payload_json, int(row["id"])),
            )
            if cursor.rowcount != 1:
                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            return True

        updated = 0
        if operation_name is not None or surgeons is not None or operating_nurse is not None:
            surgery_row = latest_stage_row("surgery_start")
            if surgery_row is not None:
                payload = _parse_json_dict(surgery_row.get("payload_json"))
                if operation_name is not None:
                    self._set_stage_payload_text(payload, "operation_name", _normalize_case_text(operation_name))
                if surgeons is not None:
                    clean_surgeons = list(_normalize_case_surgeons(surgeons))
                    if clean_surgeons:
                        payload["surgeons"] = clean_surgeons
                        payload["surgeon"] = ", ".join(clean_surgeons)
                    else:
                        payload.pop("surgeons", None)
                        payload.pop("surgeon", None)
                if operating_nurse is not None:
                    self._set_stage_payload_text(payload, "operating_nurse", _normalize_case_text(operating_nurse))
                if update_payload(surgery_row, payload):
                    updated += 1

        if anesthesia_assistance_type is not None or anesthesiologist is not None or anesthetist is not None:
            anesthesia_row = latest_stage_row("anesthesia_start")
            if anesthesia_row is not None:
                payload = _parse_json_dict(anesthesia_row.get("payload_json"))
                if anesthesia_assistance_type is not None:
                    self._set_stage_payload_text(
                        payload,
                        "anesthesia_assistance_type",
                        normalize_operblock_anesthesia_type_label(anesthesia_assistance_type),
                    )
                if anesthesiologist is not None:
                    self._set_stage_payload_text(payload, "anesthesiologist", _normalize_case_text(anesthesiologist))
                if anesthetist is not None:
                    self._set_stage_payload_text(payload, "anesthetist", _normalize_case_text(anesthetist))
                if update_payload(anesthesia_row, payload):
                    updated += 1
        return updated

    def create_operation_case(self, data: OperBlockPatientInput | dict[str, Any]) -> dict[str, int]:
        validate_operblock_runtime_path(self.db)
        data = _case_input_from_payload(data)
        _validate_case_vitals(data)

        table_code = self._validate_table_code(data.table_code)
        history_number = normalize_operblock_history_number(data.history_number)
        full_name = str(data.full_name or "").strip()
        if not full_name:
            raise ValueError("ФИО пациента не заполнено.")
        diagnosis_code = normalize_operblock_mkb_code(data.diagnosis_code or "")
        if diagnosis_code and not is_complete_operblock_mkb_code(diagnosis_code):
            raise ValueError("Код МКБ-10 должен быть в формате X33, S82.0 или S82.01.")
        diagnosis_text = str(data.diagnosis_text or "").strip()
        if not diagnosis_text:
            raise ValueError("Диагноз не заполнен.")
        department_profile = normalize_profile_department(data.department_profile)

        birth_date = _to_birth_date(data.birth_date)
        now = _now_text()
        started_dt = _minute_floor(data.started_at or datetime.now())
        current_minute = _minute_floor(datetime.now())
        if started_dt > current_minute + timedelta(minutes=1):
            raise ValueError("Время поступления в оперблок не может быть позже текущего времени.")
        started_text = started_dt.isoformat(timespec="seconds")
        age = storage_age_from_birth_date(birth_date, started_dt)
        last_name, first_name, middle_name = _split_name(full_name)
        admission_uid = str(uuid.uuid4())
        bed_number = 0
        offline_case_uuid = self._new_case_uuid()
        offline_session_id = self._offline_session_id() if self._is_opblock_offline_runtime() else None

        def operation(cursor: sqlite3.Cursor):
            existing = cursor.execute(
                "SELECT id FROM operation_cases WHERE table_code = ? AND status = 'active' LIMIT 1",
                (table_code,),
            ).fetchone()
            if existing:
                raise OperBlockConflictError("Операционный стол уже занят другим пользователем.")

            cursor.execute(
                """
                INSERT INTO patients (
                    full_name, admission_uid, birth_date, last_name, first_name, middle_name
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (full_name, admission_uid, birth_date.isoformat(), last_name, first_name, middle_name),
            )
            patient_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO admissions (
                    patient_id, bed_number, history_number, admission_datetime,
                    patient_age, patient_months, patient_age_unit, patient_gender,
                    diagnosis_code, diagnosis_text, department_profile, source_department,
                    created_at, updated_at, unit_scope, admission_type, is_active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    patient_id,
                    bed_number,
                    history_number,
                    started_text,
                    age["patient_age"],
                    age["patient_months"],
                    age["patient_age_unit"],
                    data.gender,
                    diagnosis_code or None,
                    diagnosis_text,
                    department_profile or None,
                    "Оперблок",
                    now,
                    now,
                    OPERBLOCK_ROLE,
                    OPERBLOCK_ROLE,
                ),
            )
            admission_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO operation_cases (
                    patient_id, admission_id, table_code, status, created_at, started_at,
                    created_by_role, created_by_client_id, last_modified_by,
                    planned_operation_name, planned_anesthesia_assistance_type,
                    planned_surgeons_json, planned_operating_nurse,
                    planned_anesthesiologist, planned_anesthetist,
                    height_cm, weight_kg, allergies, blood_group, blood_rh,
                    preop_sys, preop_dia, preop_pulse, preop_spo2, preop_save_initial_vitals,
                    offline_case_uuid, offline_session_id, migration_status
                ) VALUES (?, ?, ?, 'active', ?, ?, 'operblock', ?, 'operblock',
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    patient_id,
                    admission_id,
                    table_code,
                    now,
                    started_text,
                    self.client_id,
                    data.operation_name or None,
                    data.anesthesia_assistance_type or None,
                    _surgeons_json(data.surgeons),
                    data.operating_nurse or None,
                    data.anesthesiologist or None,
                    data.anesthetist or None,
                    data.height_cm,
                    data.weight_kg,
                    data.allergies or None,
                    data.blood_group or None,
                    data.blood_rh or None,
                    data.preop_sys,
                    data.preop_dia,
                    data.preop_pulse,
                    data.preop_spo2,
                    1,
                    offline_case_uuid,
                    offline_session_id,
                    "active" if self._is_opblock_offline_runtime() else None,
                ),
            )
            operation_case_id = int(cursor.lastrowid)
            if self._is_opblock_offline_runtime():
                cursor.execute(
                    """
                    UPDATE operation_cases
                    SET original_local_id = ?,
                        migration_status = 'active'
                    WHERE id = ?
                    """,
                    (operation_case_id, operation_case_id),
                )
            cursor.execute(
                """
                INSERT INTO operation_table_assignments (
                    operation_case_id, table_code, assigned_at, status,
                    created_by_role, created_by_client_id, last_modified_by
                ) VALUES (?, ?, ?, 'active', 'operblock', ?, 'operblock')
                """,
                (operation_case_id, table_code, started_text, self.client_id),
            )
            cursor.execute(
                """
                INSERT INTO patient_status_events (
                    admission_id, status, reason_type, reason_text, start_time,
                    created_by, last_modified_by
                ) VALUES (?, 'OR', 'operblock', 'В операционной', ?, 'operblock', 'operblock')
                """,
                (admission_id, started_text),
            )
            if _has_case_vitals(data):
                self._upsert_initial_vitals_for_case(cursor, {
                    "operation_case_id": operation_case_id,
                    "admission_id": admission_id,
                    "started_at": started_text,
                    "ended_at": None,
                }, data)
            return {
                "patient_id": patient_id,
                "admission_id": admission_id,
                "operation_case_id": operation_case_id,
                "offline_case_uuid": offline_case_uuid,
            }

        try:
            return dict(self.db.run_write_operation(operation, source="operblock_create_operation_case"))
        except sqlite3.IntegrityError as exc:
            raise OperBlockConflictError("Операционный стол уже занят другим пользователем.") from exc

    def get_operation_case_form_data(self, operation_case_id: int) -> dict[str, Any]:
        validate_operblock_runtime_path(self.db)

        def operation(cursor: sqlite3.Cursor):
            case = cursor.execute(
                """
                SELECT
                    oc.id AS operation_case_id,
                    oc.patient_id,
                    oc.admission_id,
                    oc.table_code,
                    oc.status AS case_status,
                    oc.started_at,
                    oc.ended_at,
                    t.display_name AS table_display_name,
                    p.full_name,
                    p.birth_date,
                    a.history_number,
                    a.patient_gender,
                    a.diagnosis_code,
                    a.diagnosis_text,
                    a.department_profile,
                    oc.planned_operation_name,
                    oc.planned_anesthesia_assistance_type,
                    oc.planned_surgeons_json,
                    oc.planned_operating_nurse,
                    oc.planned_anesthesiologist,
                    oc.planned_anesthetist,
                    oc.height_cm,
                    oc.weight_kg,
                    oc.allergies,
                    oc.blood_group,
                    oc.blood_rh,
                    oc.preop_sys,
                    oc.preop_dia,
                    oc.preop_pulse,
                    oc.preop_spo2
                FROM operation_cases oc
                JOIN operating_tables t ON t.code = oc.table_code
                JOIN admissions a ON a.id = oc.admission_id
                JOIN patients p ON p.id = oc.patient_id
                WHERE oc.id = ?
                """,
                (int(operation_case_id),),
            ).fetchone()
            if not case:
                raise OperBlockConflictError("Операция не найдена.")
            data = _row_to_dict(case)
            stage_rows = self._fetch_stage_rows_for_case(cursor, int(operation_case_id))
            stage_state = _build_stage_intervals(stage_rows)
            operation_name = _normalize_case_text(
                stage_state.get("current_operation_name")
                or stage_state.get("last_operation_name")
                or stage_state.get("first_operation_name")
                or data.get("planned_operation_name")
            )
            anesthesia_assistance_type = normalize_operblock_anesthesia_type_label(
                stage_state.get("current_anesthesia_assistance_type")
                or stage_state.get("last_anesthesia_assistance_type")
                or stage_state.get("first_anesthesia_assistance_type")
                or data.get("planned_anesthesia_assistance_type")
            )
            surgeons = _normalize_stage_text_list(
                stage_state.get("current_surgeons")
                or stage_state.get("last_surgeons")
                or stage_state.get("first_surgeons")
                or _surgeons_from_json(data.get("planned_surgeons_json")),
                split_commas=True,
            )
            operating_nurse = _normalize_case_text(
                stage_state.get("current_operating_nurse")
                or stage_state.get("last_operating_nurse")
                or stage_state.get("first_operating_nurse")
                or data.get("planned_operating_nurse")
            )
            anesthesiologist = _normalize_case_text(
                stage_state.get("current_anesthesiologist")
                or stage_state.get("last_anesthesiologist")
                or stage_state.get("first_anesthesiologist")
                or data.get("planned_anesthesiologist")
            )
            anesthetist = _normalize_case_text(
                stage_state.get("current_anesthetist")
                or stage_state.get("last_anesthetist")
                or stage_state.get("first_anesthetist")
                or data.get("planned_anesthetist")
            )
            first_vital = self._first_operation_vital_row(cursor, data)
            if first_vital:
                preop_sys = first_vital.get("sys")
                preop_dia = first_vital.get("dia")
                preop_pulse = first_vital.get("pulse")
                preop_spo2 = first_vital.get("spo2")
                vitals_source = "vitals"
            else:
                preop_sys = data.get("preop_sys")
                preop_dia = data.get("preop_dia")
                preop_pulse = data.get("preop_pulse")
                preop_spo2 = data.get("preop_spo2")
                vitals_source = "case"
            started_at_edit_lock_reason = self._operation_started_at_edit_lock_reason(cursor, data)
            return {
                "operation_case_id": int(data.get("operation_case_id") or 0),
                "table_code": data.get("table_code") or "",
                "table_name": data.get("table_display_name") or "",
                "started_at": data.get("started_at"),
                "can_edit_started_at": not bool(started_at_edit_lock_reason),
                "started_at_edit_lock_reason": started_at_edit_lock_reason,
                "history_number": data.get("history_number") or "",
                "full_name": data.get("full_name") or "",
                "gender": data.get("patient_gender") or "",
                "birth_date": data.get("birth_date"),
                "diagnosis_code": data.get("diagnosis_code") or "",
                "diagnosis_text": data.get("diagnosis_text") or "",
                "department_profile": data.get("department_profile") or "",
                "operation_name": operation_name,
                "anesthesia_assistance_type": anesthesia_assistance_type,
                "surgeons": surgeons,
                "operating_nurse": operating_nurse,
                "anesthesiologist": anesthesiologist,
                "anesthetist": anesthetist,
                "height_cm": data.get("height_cm"),
                "weight_kg": data.get("weight_kg"),
                "allergies": data.get("allergies") or "",
                "blood_group": data.get("blood_group") or "",
                "blood_rh": data.get("blood_rh") or "",
                "preop_sys": preop_sys,
                "preop_dia": preop_dia,
                "preop_pulse": preop_pulse,
                "preop_spo2": preop_spo2,
                "vitals_source": vitals_source,
            }

        return dict(self.db.run_write_operation(operation, source="operblock_get_operation_case_form_data"))

    def update_operation_case_form_data(
        self,
        operation_case_id: int,
        data: OperBlockPatientInput | dict[str, Any],
    ) -> dict[str, int]:
        validate_operblock_runtime_path(self.db)
        data = _case_input_from_payload(data)
        _validate_case_vitals(data)

        history_number = normalize_operblock_history_number(data.history_number)
        full_name = _normalize_case_text(data.full_name)
        if not full_name:
            raise ValueError("ФИО пациента не заполнено.")
        diagnosis_code = normalize_operblock_mkb_code(data.diagnosis_code or "")
        if diagnosis_code and not is_complete_operblock_mkb_code(diagnosis_code):
            raise ValueError("Код МКБ-10 должен быть в формате X33, S82.0 или S82.01.")
        diagnosis_text = _normalize_case_text(data.diagnosis_text)
        if not diagnosis_text:
            raise ValueError("Диагноз не заполнен.")
        department_profile = normalize_profile_department(data.department_profile)
        birth_date = _to_birth_date(data.birth_date)
        last_name, first_name, middle_name = _split_name(full_name)
        now = _now_text()
        requested_started_at = _minute_floor(data.started_at) if data.started_at is not None else None
        if requested_started_at is not None and requested_started_at > _minute_floor(datetime.now()) + timedelta(minutes=1):
            raise ValueError("Время поступления в оперблок не может быть позже текущего времени.")

        def operation(cursor: sqlite3.Cursor):
            case = self._assert_active_operation_case_for_update(cursor, operation_case_id)
            patient_id = int(case["patient_id"])
            admission_id = int(case["admission_id"])
            old_started_at = _parse_dt(case.get("started_at"))
            if old_started_at is None:
                raise OperBlockConflictError("У операции не задано время поступления. Обновите список оперблока.")
            effective_started_at = requested_started_at or _minute_floor(old_started_at)
            started_at_changed = _minute_floor(old_started_at) != effective_started_at
            initial_vital_to_move = None
            if started_at_changed:
                self._assert_started_at_can_be_changed(cursor, case)
                initial_vital_to_move = self._editable_initial_vital_row_for_started_at(cursor, case)
            started_text = effective_started_at.isoformat(timespec="seconds")
            age = storage_age_from_birth_date(birth_date, effective_started_at)
            cursor.execute(
                """
                UPDATE patients
                SET full_name = ?,
                    birth_date = ?,
                    last_name = ?,
                    first_name = ?,
                    middle_name = ?
                WHERE id = ?
                """,
                (full_name, birth_date.isoformat(), last_name, first_name, middle_name, patient_id),
            )
            cursor.execute(
                """
                UPDATE admissions
                SET history_number = ?,
                    admission_datetime = ?,
                    patient_age = ?,
                    patient_months = ?,
                    patient_age_unit = ?,
                    patient_gender = ?,
                    diagnosis_code = ?,
                    diagnosis_text = ?,
                    department_profile = ?,
                    updated_at = ?,
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ?
                """,
                (
                    history_number,
                    started_text,
                    age["patient_age"],
                    age["patient_months"],
                    age["patient_age_unit"],
                    data.gender,
                    diagnosis_code or None,
                    diagnosis_text,
                    department_profile or None,
                    now,
                    admission_id,
                ),
            )
            cursor.execute(
                """
                UPDATE operation_cases
                SET started_at = ?,
                    planned_operation_name = ?,
                    planned_anesthesia_assistance_type = ?,
                    planned_surgeons_json = ?,
                    planned_operating_nurse = ?,
                    planned_anesthesiologist = ?,
                    planned_anesthetist = ?,
                    height_cm = ?,
                    weight_kg = ?,
                    allergies = ?,
                    blood_group = ?,
                    blood_rh = ?,
                    preop_sys = ?,
                    preop_dia = ?,
                    preop_pulse = ?,
                    preop_spo2 = ?,
                    preop_save_initial_vitals = ?,
                    last_modified_by = 'operblock',
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ?
                  AND status = 'active'
                """,
                (
                    started_text,
                    data.operation_name or None,
                    data.anesthesia_assistance_type or None,
                    _surgeons_json(data.surgeons),
                    data.operating_nurse or None,
                    data.anesthesiologist or None,
                    data.anesthetist or None,
                    data.height_cm,
                    data.weight_kg,
                    data.allergies or None,
                    data.blood_group or None,
                    data.blood_rh or None,
                    data.preop_sys,
                    data.preop_dia,
                    data.preop_pulse,
                    data.preop_spo2,
                    1,
                    int(operation_case_id),
                ),
            )
            if cursor.rowcount != 1:
                raise OperBlockConflictError("Случай уже изменён другим рабочим местом. Обновите список оперблока.")
            if started_at_changed:
                cursor.execute(
                    """
                    UPDATE operation_table_assignments
                    SET assigned_at = ?,
                        last_modified_by = 'operblock',
                        revision = COALESCE(revision, 0) + 1,
                        updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                    WHERE operation_case_id = ?
                      AND table_code = ?
                      AND status = 'active'
                      AND released_at IS NULL
                    """,
                    (started_text, int(operation_case_id), case.get("table_code")),
                )
                cursor.execute(
                    """
                    UPDATE patient_status_events
                    SET start_time = ?,
                        last_modified_by = 'operblock',
                        revision = COALESCE(revision, 0) + 1,
                        updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                    WHERE admission_id = ?
                      AND status = 'OR'
                      AND reason_type = 'operblock'
                      AND end_time IS NULL
                    """,
                    (started_text, admission_id),
                )
                if initial_vital_to_move is not None:
                    cursor.execute(
                        """
                        UPDATE vitals
                        SET datetime = ?,
                            last_modified_by = 'operblock',
                            revision = COALESCE(revision, 0) + 1,
                            updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                        WHERE id = ?
                        """,
                        (started_text, int(initial_vital_to_move["id"])),
                    )
            case_for_vitals = dict(case)
            case_for_vitals["started_at"] = started_text
            case_for_vitals["ended_at"] = case.get("ended_at")
            if _has_case_vitals(data):
                self._upsert_initial_vitals_for_case(cursor, case_for_vitals, data)
            synced = self._sync_case_metadata_to_stage_payloads(
                cursor,
                int(operation_case_id),
                operation_name=data.operation_name,
                anesthesia_assistance_type=data.anesthesia_assistance_type,
                surgeons=data.surgeons,
                operating_nurse=data.operating_nurse,
                anesthesiologist=data.anesthesiologist,
                anesthetist=data.anesthetist,
            )
            return {
                "operation_case_id": int(operation_case_id),
                "admission_id": admission_id,
                "patient_id": patient_id,
                "synced_stage_events": int(synced),
            }

        return dict(self.db.run_write_operation(operation, source="operblock_update_operation_case_form_data"))

    def close_operation_case(self, operation_case_id: int) -> dict[str, int]:
        return self.release_operation_table(operation_case_id)

    def release_operation_table(self, operation_case_id: int) -> dict[str, int]:
        validate_operblock_runtime_path(self.db)
        now = _now_text()

        def operation(cursor: sqlite3.Cursor):
            row = cursor.execute(
                """
                SELECT id, admission_id, table_code
                FROM operation_cases
                WHERE id = ? AND status = 'active'
                """,
                (int(operation_case_id),),
            ).fetchone()
            if not row:
                raise OperBlockConflictError("Случай уже закрыт или не найден.")
            admission_id = int(row["admission_id"])
            stage_rows = self._fetch_stage_rows_for_case(cursor, int(operation_case_id))
            if self._active_anesthesia_interval(stage_rows) is not None:
                raise ValueError("Перед освобождением стола завершите анестезиологическое пособие.")
            cursor.execute(
                """
                UPDATE operation_cases
                SET status = 'closed',
                    ended_at = ?,
                    migration_status = CASE
                        WHEN COALESCE(offline_session_id, '') <> '' THEN 'pending'
                        ELSE migration_status
                    END,
                    last_modified_by = 'operblock',
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ? AND status = 'active'
                """,
                (now, int(operation_case_id)),
            )
            if cursor.rowcount != 1:
                raise OperBlockConflictError("Случай уже закрыт другим пользователем.")
            cursor.execute(
                """
                UPDATE operation_table_assignments
                SET status = 'released',
                    released_at = ?,
                    last_modified_by = 'operblock',
                    revision = COALESCE(revision, 0) + 1
                WHERE operation_case_id = ?
                  AND status = 'active'
                  AND released_at IS NULL
                """,
                (now, int(operation_case_id)),
            )
            cursor.execute(
                """
                UPDATE patient_status_events
                SET end_time = ?,
                    last_modified_by = 'operblock',
                    revision = COALESCE(revision, 0) + 1
                WHERE admission_id = ?
                  AND end_time IS NULL
                """,
                (now, admission_id),
            )
            cursor.execute(
                """
                UPDATE admissions
                SET is_active = 0,
                    updated_at = ?,
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ?
                """,
                (now, admission_id),
            )
            return {"operation_case_id": int(operation_case_id), "admission_id": admission_id}

        return dict(self.db.run_write_operation(operation, source="operblock_release_operation_table"))

    def start_anesthesia(
        self,
        operation_case_id: int,
        assistance_type: str | None = None,
        *,
        anesthesiologist: str | None = None,
        anesthetist: str | None = None,
        event_time: Any = None,
    ) -> int:
        return self._add_stage_event(
            operation_case_id,
            "anesthesia_start",
            assistance_type=assistance_type,
            anesthesiologist=anesthesiologist,
            anesthetist=anesthetist,
            event_time=event_time,
        )

    def end_anesthesia(self, operation_case_id: int, *, event_time: Any = None) -> int:
        return self._add_stage_event(operation_case_id, "anesthesia_end", event_time=event_time)

    def end_anesthesia_with_transfer(
        self,
        operation_case_id: int,
        transfer_department: str,
        *,
        event_time: Any = None,
    ) -> int:
        return self._add_stage_event(
            operation_case_id,
            "anesthesia_end",
            transfer_department=transfer_department,
            event_time=event_time,
        )

    def start_surgery(
        self,
        operation_case_id: int,
        *,
        operation_name: str | None = None,
        surgeons: list[str] | None = None,
        surgeon: str | None = None,
        operating_nurse: str | None = None,
        event_time: Any = None,
    ) -> int:
        return self._add_stage_event(
            operation_case_id,
            "surgery_start",
            operation_name=operation_name,
            surgeons=surgeons,
            surgeon=surgeon,
            operating_nurse=operating_nurse,
            event_time=event_time,
        )

    def end_surgery(self, operation_case_id: int, *, event_time: Any = None) -> int:
        return self._add_stage_event(operation_case_id, "surgery_end", event_time=event_time)

    def add_operation_stage(
        self,
        operation_case_id: int,
        label: str,
        *,
        event_time: Any = None,
    ) -> dict[str, Any]:
        validate_operblock_runtime_path(self.db)
        clean_label = _normalize_operation_stage_label(label)
        if not clean_label:
            raise ValueError("Укажите название этапа операции.")
        event_dt = (
            self._normalize_timeline_event_datetime(event_time)
            if event_time is not None
            else _minute_floor(datetime.now())
        )

        def operation(cursor: sqlite3.Cursor):
            case = self._assert_active_operation_case_for_update(cursor, operation_case_id)
            stage_rows = self._fetch_stage_rows_for_case(cursor, int(operation_case_id))
            state = self._stage_state_from_cursor_rows(stage_rows)
            if not bool(state.get("surgery_active")):
                raise ValueError("Этапы операции доступны только после начала операции и до её завершения.")
            surgery_start = _parse_dt(state.get("current_surgery_start"))
            if surgery_start is not None and event_dt < _minute_floor(surgery_start):
                raise ValueError(
                    f"Этап операции не может быть раньше начала операции: "
                    f"{_format_bound_time(_minute_floor(surgery_start))}."
                )
            self._assert_datetime_in_active_anesthesia_bounds(
                cursor,
                event_dt,
                case,
                entity_label="Этап операции",
            )
            payload = {"stage_kind": "custom", "label": clean_label}
            payload_json = self._timeline_payload_json(payload)
            cursor.execute(
                """
                INSERT INTO operblock_timeline_events (
                    operation_case_id, admission_id, table_code, event_type, event_time,
                    drug_label, display_label, raw_text, status, revision, payload_json,
                    created_by_role, created_by_client_id, last_modified_by
                ) VALUES (?, ?, ?, 'clinical_event', ?, ?, ?, ?, 'active', 1, ?,
                          'operblock', ?, 'operblock')
                """,
                (
                    int(case["operation_case_id"]),
                    int(case["admission_id"]),
                    case.get("table_code"),
                    event_dt.isoformat(timespec="seconds"),
                    clean_label,
                    clean_label,
                    clean_label,
                    payload_json,
                    self.client_id,
                ),
            )
            return self._operation_stage_event_result(
                event_id=int(cursor.lastrowid),
                case=case,
                label=clean_label,
                event_dt=event_dt,
                revision=1,
                payload=payload,
            )

        return dict(self.db.run_write_operation(operation, source="operblock_add_operation_stage"))

    def update_operation_stage(
        self,
        event_id: int,
        label: str,
        *,
        expected_revision: Optional[int] = None,
        event_time: Any = None,
    ) -> dict[str, Any]:
        validate_operblock_runtime_path(self.db)
        clean_label = _normalize_operation_stage_label(label)
        if not clean_label:
            raise ValueError("Укажите название этапа операции.")
        new_event_dt = self._normalize_timeline_event_datetime(event_time) if event_time is not None else None

        def operation(cursor: sqlite3.Cursor):
            row = cursor.execute(
                """
                SELECT
                    id, operation_case_id, admission_id, table_code, event_type, event_time,
                    display_label, raw_text, payload_json, COALESCE(revision, 0) AS revision
                FROM operblock_timeline_events
                WHERE id = ?
                  AND event_type = 'clinical_event'
                  AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                """,
                (int(event_id),),
            ).fetchone()
            if not row:
                raise OperBlockConflictError("Этап операции не найден или уже удалён. Обновите протокол.")
            payload = _parse_json_dict(row["payload_json"])
            if operation_stage_kind_from_payload(payload) != "custom":
                raise ValueError("Автоматические этапы операции нельзя редактировать.")
            assert_revision_matches(row["revision"], expected_revision)
            case = self._assert_active_operation_case_for_update(cursor, int(row["operation_case_id"]))
            if int(case["admission_id"]) != int(row["admission_id"]):
                raise OperBlockConflictError("Этап операции не принадлежит текущему случаю. Обновите протокол.")
            stage_rows = self._fetch_stage_rows_for_case(cursor, int(case["operation_case_id"]))
            state = self._stage_state_from_cursor_rows(stage_rows)
            if not bool(state.get("surgery_active")):
                raise ValueError("Этапы операции доступны только после начала операции и до её завершения.")
            event_dt = _parse_dt(row["event_time"])
            if event_dt is None:
                raise OperBlockConflictError("Не удалось определить время этапа. Обновите протокол.")
            effective_event_dt = _minute_floor(new_event_dt or event_dt)
            surgery_start = _parse_dt(state.get("current_surgery_start"))
            if surgery_start is None:
                raise OperBlockConflictError("Не удалось определить начало операции. Обновите протокол.")
            if effective_event_dt < _minute_floor(surgery_start):
                raise ValueError(
                    f"Этап операции не может быть раньше начала операции: "
                    f"{_format_bound_time(_minute_floor(surgery_start))}."
                )
            self._assert_datetime_in_active_anesthesia_bounds(
                cursor,
                effective_event_dt,
                case,
                entity_label="Время этапа операции",
            )
            payload["label"] = clean_label
            payload_json = self._timeline_payload_json(payload)
            if (
                str(row["display_label"] or "").strip() == clean_label
                and str(row["raw_text"] or "").strip() == clean_label
                and str(row["payload_json"] or "") == str(payload_json or "")
                and _minute_floor(event_dt) == effective_event_dt
            ):
                return self._operation_stage_event_result(
                    event_id=int(row["id"]),
                    case=case,
                    label=clean_label,
                    event_dt=effective_event_dt,
                    revision=int(row["revision"] or 0),
                    payload=payload,
                )
            revision_clause = ""
            params: list[Any] = [
                clean_label,
                clean_label,
                clean_label,
                effective_event_dt.isoformat(timespec="seconds"),
                payload_json,
                int(row["id"]),
            ]
            if expected_revision is not None:
                revision_clause = "AND COALESCE(revision, 0) = ?"
                params.append(int(expected_revision))
            cursor.execute(
                f"""
                UPDATE operblock_timeline_events
                SET drug_label = ?,
                    display_label = ?,
                    raw_text = ?,
                    event_time = ?,
                    payload_json = ?,
                    revision = COALESCE(revision, 0) + 1,
                    last_modified_by = 'operblock',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                  AND event_type = 'clinical_event'
                  AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                  {revision_clause}
                """,
                tuple(params),
            )
            if cursor.rowcount != 1:
                assert_revision_matches(None, expected_revision)
                raise OperBlockConflictError("Этап операции уже изменён другим пользователем. Обновите протокол.")
            return self._operation_stage_event_result(
                event_id=int(row["id"]),
                case=case,
                label=clean_label,
                event_dt=effective_event_dt,
                revision=int(row["revision"] or 0) + 1,
                payload=payload,
            )

        return dict(self.db.run_write_operation(operation, source="operblock_update_operation_stage"))

    def update_operation_staff(
        self,
        operation_case_id: int,
        *,
        surgeons: list[str] | None = None,
        operating_nurse: str | None = None,
        anesthesiologist: str | None = None,
        anesthetist: str | None = None,
    ) -> dict[str, Any]:
        validate_operblock_runtime_path(self.db)
        clean_surgeons = _normalize_stage_text_list(surgeons)
        clean_operating_nurse = re.sub(r"\s+", " ", str(operating_nurse or "").strip())
        clean_anesthesiologist = re.sub(r"\s+", " ", str(anesthesiologist or "").strip())
        clean_anesthetist = re.sub(r"\s+", " ", str(anesthetist or "").strip())

        def latest_stage_row(rows: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
            for row in reversed(rows):
                payload = _parse_json_dict(row.get("payload_json"))
                if operation_stage_kind_from_payload(payload) == kind:
                    return row
            return None

        def set_text(payload: dict[str, Any], key: str, value: str) -> None:
            if value:
                payload[key] = value
            else:
                payload.pop(key, None)

        def update_payload(cursor: sqlite3.Cursor, row: dict[str, Any], payload: dict[str, Any]) -> bool:
            payload_json = self._timeline_payload_json(payload)
            if payload_json == row.get("payload_json"):
                return False
            cursor.execute(
                """
                UPDATE operblock_timeline_events
                SET payload_json = ?,
                    revision = COALESCE(revision, 0) + 1,
                    last_modified_by = 'operblock',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                  AND event_type = 'clinical_event'
                  AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                """,
                (payload_json, int(row["id"])),
            )
            if cursor.rowcount != 1:
                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            return True

        def operation(cursor: sqlite3.Cursor):
            case = self._assert_active_operation_case_for_update(cursor, operation_case_id)
            rows = [
                _row_to_dict(row)
                for row in cursor.execute(
                    """
                    SELECT id, operation_case_id, admission_id, table_code, event_type, event_time, end_time,
                           drug_label, display_label, raw_text, status, COALESCE(revision, 0) AS revision,
                           parent_event_id, payload_json, created_at, updated_at
                    FROM operblock_timeline_events
                    WHERE operation_case_id = ?
                      AND event_type = 'clinical_event'
                      AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                    ORDER BY datetime(event_time) ASC, id ASC
                    """,
                    (int(case["operation_case_id"]),),
                ).fetchall()
            ]
            stage_state = _build_stage_intervals(_stage_rows_from_timeline_rows(rows))
            updated = 0

            if surgeons is not None or operating_nurse is not None:
                if not stage_state.get("surgery_active"):
                    raise ValueError("Операция ещё не начата или уже завершена.")
                surgery_row = latest_stage_row(rows, "surgery_start")
                if surgery_row is None:
                    raise OperBlockConflictError("Начало операции не найдено. Обновите протокол.")
                payload = _parse_json_dict(surgery_row.get("payload_json"))
                if surgeons is not None:
                    if clean_surgeons:
                        payload["surgeons"] = clean_surgeons
                        payload["surgeon"] = ", ".join(clean_surgeons)
                    else:
                        payload.pop("surgeons", None)
                        payload.pop("surgeon", None)
                if operating_nurse is not None:
                    set_text(payload, "operating_nurse", clean_operating_nurse)
                if update_payload(cursor, surgery_row, payload):
                    updated += 1
                if surgeons is not None or operating_nurse is not None:
                    case_updates: list[str] = []
                    case_params: list[Any] = []
                    if surgeons is not None:
                        case_updates.append("planned_surgeons_json = ?")
                        case_params.append(_surgeons_json(clean_surgeons))
                    if operating_nurse is not None:
                        case_updates.append("planned_operating_nurse = ?")
                        case_params.append(clean_operating_nurse or None)
                    cursor.execute(
                        f"""
                        UPDATE operation_cases
                        SET {", ".join(case_updates)},
                            last_modified_by = 'operblock',
                            revision = COALESCE(revision, 0) + 1
                        WHERE id = ?
                        """,
                        (*case_params, int(operation_case_id)),
                    )

            if anesthesiologist is not None or anesthetist is not None:
                if not stage_state.get("anesthesia_active"):
                    raise ValueError("Анестезиологическое пособие ещё не начато или уже завершено.")
                anesthesia_row = latest_stage_row(rows, "anesthesia_start")
                if anesthesia_row is None:
                    raise OperBlockConflictError("Начало пособия не найдено. Обновите протокол.")
                payload = _parse_json_dict(anesthesia_row.get("payload_json"))
                if anesthesiologist is not None:
                    set_text(payload, "anesthesiologist", clean_anesthesiologist)
                if anesthetist is not None:
                    set_text(payload, "anesthetist", clean_anesthetist)
                if update_payload(cursor, anesthesia_row, payload):
                    updated += 1
                case_updates: list[str] = []
                case_params: list[Any] = []
                if anesthesiologist is not None:
                    case_updates.append("planned_anesthesiologist = ?")
                    case_params.append(clean_anesthesiologist or None)
                if anesthetist is not None:
                    case_updates.append("planned_anesthetist = ?")
                    case_params.append(clean_anesthetist or None)
                if case_updates:
                    cursor.execute(
                        f"""
                        UPDATE operation_cases
                        SET {", ".join(case_updates)},
                            last_modified_by = 'operblock',
                            revision = COALESCE(revision, 0) + 1
                        WHERE id = ?
                        """,
                        (*case_params, int(operation_case_id)),
                    )

            if updated == 0:
                return {"operation_case_id": int(operation_case_id), "updated": 0}
            return {"operation_case_id": int(operation_case_id), "updated": updated}

        return dict(self.db.run_write_operation(operation, source="operblock_update_operation_staff"))

    def _add_stage_event(
        self,
        operation_case_id: int,
        stage_kind: str,
        *,
        assistance_type: str | None = None,
        anesthesiologist: str | None = None,
        anesthetist: str | None = None,
        operation_name: str | None = None,
        surgeons: list[str] | None = None,
        surgeon: str | None = None,
        operating_nurse: str | None = None,
        transfer_department: str | None = None,
        event_time: Any = None,
    ) -> int:
        validate_operblock_runtime_path(self.db)
        clean_kind = str(stage_kind or "").strip()
        if clean_kind not in OPERBLOCK_STAGE_KIND_LABELS:
            raise ValueError("Неизвестный этап операции.")
        event_dt = (
            self._normalize_timeline_event_datetime(event_time)
            if event_time is not None
            else _minute_floor(datetime.now())
        )
        clean_assistance_type = normalize_operblock_anesthesia_type_label(assistance_type)
        clean_anesthesiologist = re.sub(r"\s+", " ", str(anesthesiologist or "").strip())
        clean_anesthetist = re.sub(r"\s+", " ", str(anesthetist or "").strip())
        clean_operation_name = re.sub(r"\s+", " ", str(operation_name or "").strip())
        clean_surgeons = _normalize_stage_text_list(surgeons)
        clean_surgeon = re.sub(r"\s+", " ", str(surgeon or "").strip())
        if clean_surgeon and clean_surgeon.casefold() not in {item.casefold() for item in clean_surgeons}:
            clean_surgeons.append(clean_surgeon)
        clean_operating_nurse = re.sub(r"\s+", " ", str(operating_nurse or "").strip())
        clean_transfer_department = normalize_operblock_transfer_department(transfer_department)

        def operation(cursor: sqlite3.Cursor):
            nonlocal clean_transfer_department
            case = self._assert_active_operation_case_for_update(cursor, operation_case_id)
            self._assert_datetime_in_operation_bounds(event_dt, case, entity_label=_stage_label(clean_kind))
            stage_rows = self._fetch_stage_rows_for_case(cursor, int(operation_case_id))
            state = self._stage_state_from_cursor_rows(stage_rows)
            anesthesia_active = bool(state.get("anesthesia_active"))
            surgery_active = bool(state.get("surgery_active"))

            if clean_kind == "anesthesia_start":
                if anesthesia_active:
                    raise ValueError("Анестезиологическое пособие уже начато.")
                started_at = _parse_dt(case.get("started_at"))
                if started_at is None:
                    raise OperBlockConflictError("У операции не задано время начала. Обновите протокол.")
                if not self._operation_has_vitals_between(
                    int(case["admission_id"]),
                    _minute_floor(started_at),
                    cursor=cursor,
                ):
                    raise ValueError("Перед началом пособия введите исходные витальные показатели.")
            elif clean_kind == "anesthesia_end":
                if not anesthesia_active:
                    raise ValueError("Анестезиологическое пособие ещё не начато.")
                if surgery_active:
                    raise ValueError("Перед окончанием пособия завершите операцию.")
                active_start = _parse_dt(state.get("current_anesthesia_start"))
                if active_start is None:
                    raise OperBlockConflictError("Не удалось определить начало пособия. Обновите протокол.")
                if event_dt < _minute_floor(active_start):
                    raise ValueError(
                        f"Конец пособия не может быть раньше начала пособия: "
                        f"{_format_bound_time(_minute_floor(active_start))}."
                    )
                last_surgery_end = _parse_dt(state.get("last_surgery_end"))
                if last_surgery_end is not None and event_dt < _minute_floor(last_surgery_end):
                    raise ValueError(
                        f"Конец пособия не может быть раньше окончания операции: "
                        f"{_format_bound_time(_minute_floor(last_surgery_end))}."
                    )
                self._validate_medications_before_anesthesia_end(
                    cursor,
                    case,
                    _minute_floor(active_start),
                    event_dt,
                )
                self._auto_stop_open_infusions(cursor, case, event_dt)
                if not clean_transfer_department:
                    clean_transfer_department = normalize_operblock_transfer_department(
                        case.get("department_profile")
                    )
                if not clean_transfer_department:
                    raise ValueError("Укажите отделение, куда переводится пациент.")
            elif clean_kind == "surgery_start":
                if not anesthesia_active:
                    raise ValueError("Начать операцию можно только после начала пособия.")
                if surgery_active:
                    raise ValueError("Операция уже начата.")
                self._assert_datetime_in_active_anesthesia_bounds(
                    cursor,
                    event_dt,
                    case,
                    entity_label="Начало операции",
                )
            elif clean_kind == "surgery_end":
                if not surgery_active:
                    raise ValueError("Операция ещё не начата.")
                surgery_start = _parse_dt(state.get("current_surgery_start"))
                if surgery_start is not None and event_dt < _minute_floor(surgery_start):
                    raise ValueError(
                        f"Конец операции не может быть раньше начала операции: "
                        f"{_format_bound_time(_minute_floor(surgery_start))}."
                    )
                self._assert_datetime_in_active_anesthesia_bounds(
                    cursor,
                    event_dt,
                    case,
                    entity_label="Конец операции",
                )

            event_id = self._insert_stage_event(
                cursor,
                case,
                clean_kind,
                event_dt,
                assistance_type=clean_assistance_type,
                anesthesiologist=clean_anesthesiologist,
                anesthetist=clean_anesthetist,
                operation_name=clean_operation_name,
                surgeons=clean_surgeons,
                operating_nurse=clean_operating_nurse,
                transfer_department=clean_transfer_department,
            )
            if clean_kind == "anesthesia_end" and _is_rao_transfer_department(clean_transfer_department):
                if self._is_opblock_offline_runtime():
                    logger.info(
                        "operblock_offline_rao_transfer_archival_only case_id=%s",
                        case.get("operation_case_id"),
                    )
                else:
                    self._maybe_create_rao_recovery_admission(cursor, case, event_dt)
            return event_id

        return int(self.db.run_write_operation(operation, source=f"operblock_stage_{clean_kind}"))

    @staticmethod
    def _operation_stage_event_result(
        *,
        event_id: int,
        case: dict[str, Any],
        label: str,
        event_dt: datetime,
        revision: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        clean_label = _normalize_operation_stage_label(label) or _stage_label(str(payload.get("stage_kind") or ""))
        event_time = _minute_floor(event_dt).isoformat(timespec="seconds")
        return {
            "id": f"timeline_event:{int(event_id)}",
            "source": "timeline_event",
            "source_id": int(event_id),
            "admission_id": int(case["admission_id"]),
            "operation_case_id": int(case["operation_case_id"]),
            "table_code": str(case.get("table_code") or "") or None,
            "event_time": event_time,
            "end_time": None,
            "event_type": "clinical_event",
            "drug_label": clean_label,
            "display_label": clean_label,
            "raw_text": clean_label,
            "dose_value": None,
            "dose_unit": None,
            "volume_ml": None,
            "concentration_text": None,
            "rate_value": None,
            "rate_unit": None,
            "route": None,
            "status": "active",
            "revision": int(revision or 0),
            "created_at": None,
            "updated_at": None,
            "payload": dict(payload or {}),
        }

    def _insert_stage_event(
        self,
        cursor: sqlite3.Cursor,
        case: dict[str, Any],
        stage_kind: str,
        event_dt: datetime,
        *,
        assistance_type: str | None = None,
        anesthesiologist: str | None = None,
        anesthetist: str | None = None,
        operation_name: str | None = None,
        surgeons: list[str] | None = None,
        surgeon: str | None = None,
        operating_nurse: str | None = None,
        transfer_department: str | None = None,
    ) -> int:
        clean_transfer_department = normalize_operblock_transfer_department(transfer_department)
        label = operblock_transfer_stage_label(clean_transfer_department) if stage_kind == "anesthesia_end" else _stage_label(stage_kind)
        payload = {"stage_kind": stage_kind, "label": label}
        if stage_kind == "anesthesia_start":
            self._ensure_case_protocol_number(cursor, case, event_dt)
        if stage_kind == "anesthesia_end" and clean_transfer_department:
            payload["transfer_department"] = clean_transfer_department
            payload["transfer_department_target"] = transfer_department_target_text(clean_transfer_department)
        clean_assistance_type = normalize_operblock_anesthesia_type_label(assistance_type)
        if stage_kind == "anesthesia_start" and clean_assistance_type:
            payload["anesthesia_assistance_type"] = clean_assistance_type
        clean_anesthesiologist = re.sub(r"\s+", " ", str(anesthesiologist or "").strip())
        clean_anesthetist = re.sub(r"\s+", " ", str(anesthetist or "").strip())
        if stage_kind == "anesthesia_start" and clean_anesthesiologist:
            payload["anesthesiologist"] = clean_anesthesiologist
        if stage_kind == "anesthesia_start" and clean_anesthetist:
            payload["anesthetist"] = clean_anesthetist
        clean_operation_name = re.sub(r"\s+", " ", str(operation_name or "").strip())
        clean_surgeons = _normalize_stage_text_list(surgeons)
        clean_surgeon = re.sub(r"\s+", " ", str(surgeon or "").strip())
        if clean_surgeon and clean_surgeon.casefold() not in {item.casefold() for item in clean_surgeons}:
            clean_surgeons.append(clean_surgeon)
        clean_operating_nurse = re.sub(r"\s+", " ", str(operating_nurse or "").strip())
        if stage_kind == "surgery_start" and clean_operation_name:
            payload["operation_name"] = clean_operation_name
        if stage_kind == "surgery_start" and clean_surgeons:
            payload["surgeons"] = clean_surgeons
            payload["surgeon"] = ", ".join(clean_surgeons)
        if stage_kind == "surgery_start" and clean_operating_nurse:
            payload["operating_nurse"] = clean_operating_nurse
        payload_json = self._timeline_payload_json(payload)
        cursor.execute(
            """
            INSERT INTO operblock_timeline_events (
                operation_case_id, admission_id, table_code, event_type, event_time,
                drug_label, display_label, raw_text, status, revision, payload_json,
                created_by_role, created_by_client_id, last_modified_by
            ) VALUES (?, ?, ?, 'clinical_event', ?, ?, ?, ?, 'active', 1, ?,
                      'operblock', ?, 'operblock')
            """,
            (
                int(case["operation_case_id"]),
                int(case["admission_id"]),
                case.get("table_code"),
                event_dt.isoformat(timespec="seconds"),
                label,
                label,
                label,
                payload_json,
                self.client_id,
            ),
        )
        event_id = int(cursor.lastrowid)
        if stage_kind == "surgery_start":
            cursor.execute(
                """
                UPDATE operation_cases
                SET planned_operation_name = ?,
                    planned_surgeons_json = ?,
                    planned_operating_nurse = ?,
                    last_modified_by = 'operblock',
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ?
                """,
                (
                    clean_operation_name or None,
                    _surgeons_json(clean_surgeons),
                    clean_operating_nurse or None,
                    int(case["operation_case_id"]),
                ),
            )
        elif stage_kind == "anesthesia_start":
            cursor.execute(
                """
                UPDATE operation_cases
                SET planned_anesthesia_assistance_type = ?,
                    planned_anesthesiologist = ?,
                    planned_anesthetist = ?,
                    last_modified_by = 'operblock',
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ?
                """,
                (
                    clean_assistance_type or None,
                    clean_anesthesiologist or None,
                    clean_anesthetist or None,
                    int(case["operation_case_id"]),
                ),
            )
        elif stage_kind == "anesthesia_end":
            cursor.execute(
                """
                UPDATE operation_cases
                SET transfer_department = ?,
                    last_modified_by = 'operblock',
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ?
                """,
                (clean_transfer_department or None, int(case["operation_case_id"])),
            )
        return event_id

    def _maybe_create_rao_recovery_admission(
        self,
        cursor: sqlite3.Cursor,
        case: dict[str, Any],
        event_dt: datetime,
    ) -> Optional[int]:
        savepoint = "operblock_rao_auto_admission"
        cursor.execute(f"SAVEPOINT {savepoint}")
        try:
            admission_id = self._create_rao_recovery_admission(cursor, case, event_dt)
        except Exception as exc:
            try:
                cursor.execute(f"ROLLBACK TO {savepoint}")
            except Exception:
                logger.error(
                    "operblock_rao_auto_admission_rollback_failed case_id=%s",
                    case.get("operation_case_id"),
                    exc_info=True,
                )
            try:
                cursor.execute(f"RELEASE {savepoint}")
            except Exception:
                logger.error(
                    "operblock_rao_auto_admission_release_after_rollback_failed case_id=%s",
                    case.get("operation_case_id"),
                    exc_info=True,
                )
            logger.error(
                "operblock_rao_auto_admission_failed case_id=%s source_admission_id=%s: %s",
                case.get("operation_case_id"),
                case.get("admission_id"),
                exc,
                exc_info=True,
            )
            return None

        try:
            cursor.execute(f"RELEASE {savepoint}")
        except Exception:
            logger.error(
                "operblock_rao_auto_admission_release_failed case_id=%s rao_admission_id=%s",
                case.get("operation_case_id"),
                admission_id,
                exc_info=True,
            )
            return None
        return admission_id

    def _create_rao_recovery_admission(
        self,
        cursor: sqlite3.Cursor,
        case: dict[str, Any],
        event_dt: datetime,
    ) -> Optional[int]:
        operation_case_id = int(case["operation_case_id"])
        source = self._fetch_rao_transfer_source_data(cursor, operation_case_id)
        if not source:
            raise OperBlockConflictError("Не удалось найти данные операции для автоперевода в РАО.")

        existing_rao_admission_id = source.get("future_rao_admission_id")
        if existing_rao_admission_id:
            logger.info(
                "operblock_rao_auto_admission_skipped_existing case_id=%s rao_admission_id=%s",
                operation_case_id,
                existing_rao_admission_id,
            )
            return None

        admission_dt = _minute_floor(event_dt + timedelta(minutes=10))
        admission_data = self._prepare_rao_recovery_admission_data(
            operation_case_id,
            source,
            event_dt,
            admission_dt,
        )
        if admission_data is None:
            return None

        bed_number = self._select_free_recovery_bed_for_rao(cursor)
        if bed_number is None:
            logger.info(
                "operblock_rao_auto_admission_skipped_no_free_recovery_bed case_id=%s source_admission_id=%s",
                operation_case_id,
                source.get("source_admission_id"),
            )
            return None

        admission_id = self._insert_rao_recovery_patient_admission(cursor, admission_data, bed_number)
        self._occupy_rao_recovery_bed(cursor, admission_id, bed_number)
        self._insert_rao_recovery_status_event(cursor, admission_id, admission_data["admission_dt_text"])
        self._copy_latest_operblock_vitals_to_rao(
            cursor,
            int(source["source_admission_id"]),
            admission_id,
            event_dt,
            admission_dt,
        )
        if "future_rao_admission_id" in _sqlite_columns(cursor.connection, "operation_cases"):
            cursor.execute(
                """
                UPDATE operation_cases
                SET future_rao_admission_id = ?,
                    last_modified_by = 'operblock',
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ?
                """,
                (admission_id, operation_case_id),
            )
        logger.info(
            "operblock_rao_auto_admission_created case_id=%s source_admission_id=%s rao_admission_id=%s bed=%s",
            operation_case_id,
            source.get("source_admission_id"),
            admission_id,
            bed_number,
        )
        return admission_id

    def _prepare_rao_recovery_admission_data(
        self,
        operation_case_id: int,
        source: dict[str, Any],
        event_dt: datetime,
        admission_dt: datetime,
    ) -> Optional[dict[str, Any]]:
        history_number = str(source.get("history_number") or "").strip()
        full_name = _normalize_case_text(source.get("full_name"))
        diagnosis_text = _normalize_case_text(source.get("diagnosis_text"))
        birth_date = parse_date_value(source.get("birth_date"))
        missing_fields: list[str] = []
        if not history_number:
            missing_fields.append("history_number")
        if not full_name:
            missing_fields.append("full_name")
        if birth_date is None:
            missing_fields.append("birth_date")
        elif birth_date > admission_dt.date():
            missing_fields.append("birth_date_after_admission")
        if not diagnosis_text:
            missing_fields.append("diagnosis_text")
        if missing_fields:
            logger.error(
                "operblock_rao_auto_admission_required_field_missing case_id=%s source_admission_id=%s missing=%s",
                operation_case_id,
                source.get("source_admission_id"),
                ",".join(missing_fields),
            )
            return None

        assert birth_date is not None
        operation_name = _normalize_case_text(source.get("operation_name"))
        intake_extra_json = json.dumps(
            {
                "source": "operblock_rao_transfer",
                "operation_case_id": int(operation_case_id),
                "source_patient_id": source.get("source_patient_id"),
                "source_admission_id": source.get("source_admission_id"),
                "table_code": source.get("table_code") or "",
                "operation_finished_at": _minute_floor(event_dt).isoformat(timespec="seconds"),
                "transfer_department": "РАО",
                "operation_name": operation_name,
                "anesthesia_assistance_type": normalize_operblock_anesthesia_type_label(
                    source.get("anesthesia_assistance_type")
                ),
                "surgeons": source.get("surgeons") or [],
                "anesthesiologist": source.get("anesthesiologist") or "",
                "anesthetist": source.get("anesthetist") or "",
                "height_cm": source.get("height_cm"),
                "weight_kg": source.get("weight_kg"),
                "allergies": source.get("allergies") or "",
                "blood_group": source.get("blood_group") or "",
                "blood_rh": source.get("blood_rh") or "",
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        age = storage_age_from_birth_date(birth_date, admission_dt)
        return {
            "admission_dt_text": admission_dt.isoformat(timespec="seconds"),
            "birth_date": birth_date,
            "department_profile": normalize_profile_department(source.get("department_profile")) or None,
            "diagnosis_code": normalize_operblock_mkb_code(source.get("diagnosis_code") or "") or None,
            "diagnosis_text": diagnosis_text,
            "full_name": full_name,
            "history_number": history_number,
            "intake_extra_json": intake_extra_json,
            "operation_name": operation_name,
            "patient_age": age["patient_age"],
            "patient_age_unit": age["patient_age_unit"],
            "patient_gender": source.get("patient_gender") or None,
            "patient_months": age["patient_months"],
        }

    @staticmethod
    def _insert_rao_recovery_patient_admission(
        cursor: sqlite3.Cursor,
        admission_data: dict[str, Any],
        bed_number: int,
    ) -> int:
        full_name = str(admission_data["full_name"])
        birth_date = admission_data["birth_date"]
        last_name, first_name, middle_name = _split_name(full_name)
        admission_uid = str(uuid.uuid4())
        now = _now_text()
        cursor.execute(
            """
            INSERT INTO patients (
                full_name, admission_uid, birth_date, last_name, first_name, middle_name
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (full_name, admission_uid, birth_date.isoformat(), last_name, first_name, middle_name),
        )
        patient_id = int(cursor.lastrowid)
        cursor.execute(
            """
            INSERT INTO admissions (
                patient_id, bed_number, history_number, admission_datetime,
                patient_age, patient_months, patient_age_unit, patient_gender,
                diagnosis_code, diagnosis_text, department_profile, source_department,
                operation_description, intake_extra_json, recovery_bed_stay,
                created_at, updated_at, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 1)
            """,
            (
                patient_id,
                int(bed_number),
                admission_data["history_number"],
                admission_data["admission_dt_text"],
                admission_data["patient_age"],
                admission_data["patient_months"],
                admission_data["patient_age_unit"],
                admission_data["patient_gender"],
                admission_data["diagnosis_code"],
                admission_data["diagnosis_text"],
                admission_data["department_profile"],
                "Профильное отделение",
                admission_data["operation_name"] or None,
                admission_data["intake_extra_json"],
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _occupy_rao_recovery_bed(cursor: sqlite3.Cursor, admission_id: int, bed_number: int) -> None:
        cursor.execute(
            """
            UPDATE beds
            SET status = 'OCCUPIED',
                current_admission_id = ?,
                revision = COALESCE(revision, 0) + 1
            WHERE bed_number = ?
              AND status = 'FREE'
              AND current_admission_id IS NULL
            """,
            (int(admission_id), int(bed_number)),
        )
        if cursor.rowcount != 1:
            raise OperBlockConflictError("Свободная койка пробуждения была занята до автоперевода.")

    @staticmethod
    def _insert_rao_recovery_status_event(
        cursor: sqlite3.Cursor,
        admission_id: int,
        admission_dt_text: str,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO patient_status_events (
                admission_id, status, reason_type, reason_text, start_time,
                created_by, created_at, updated_at, last_modified_by
            ) VALUES (?, ?, 'operblock_rao_transfer', 'Поступил после операции из оперблока',
                      ?, 'operblock', ?, ?, 'operblock')
            """,
            (
                int(admission_id),
                PatientStatus.ACTIVE.value,
                admission_dt_text,
                admission_dt_text,
                admission_dt_text,
            ),
        )

    def _fetch_rao_transfer_source_data(
        self,
        cursor: sqlite3.Cursor,
        operation_case_id: int,
    ) -> dict[str, Any]:
        case_columns = _sqlite_columns(cursor.connection, "operation_cases")
        future_expr = "oc.future_rao_admission_id" if "future_rao_admission_id" in case_columns else "NULL"
        row = cursor.execute(
            f"""
            SELECT
                oc.id AS operation_case_id,
                oc.patient_id AS source_patient_id,
                oc.admission_id AS source_admission_id,
                oc.table_code,
                oc.started_at,
                oc.ended_at,
                {future_expr} AS future_rao_admission_id,
                oc.planned_operation_name,
                oc.planned_anesthesia_assistance_type,
                oc.planned_surgeons_json,
                oc.planned_operating_nurse,
                oc.planned_anesthesiologist,
                oc.planned_anesthetist,
                oc.height_cm,
                oc.weight_kg,
                oc.allergies,
                oc.blood_group,
                oc.blood_rh,
                p.full_name,
                p.birth_date,
                a.history_number,
                a.patient_gender,
                a.diagnosis_code,
                a.diagnosis_text,
                a.department_profile
            FROM operation_cases oc
            JOIN admissions a ON a.id = oc.admission_id
            JOIN patients p ON p.id = oc.patient_id
            WHERE oc.id = ?
            """,
            (int(operation_case_id),),
        ).fetchone()
        if not row:
            return {}

        data = _row_to_dict(row)
        stage_rows = self._fetch_stage_rows_for_case(cursor, int(operation_case_id))
        stage_state = _build_stage_intervals(stage_rows)
        data["operation_name"] = _normalize_case_text(
            stage_state.get("current_operation_name")
            or stage_state.get("last_operation_name")
            or stage_state.get("first_operation_name")
            or data.get("planned_operation_name")
        )
        data["anesthesia_assistance_type"] = normalize_operblock_anesthesia_type_label(
            stage_state.get("current_anesthesia_assistance_type")
            or stage_state.get("last_anesthesia_assistance_type")
            or stage_state.get("first_anesthesia_assistance_type")
            or data.get("planned_anesthesia_assistance_type")
        )
        data["surgeons"] = _normalize_stage_text_list(
            stage_state.get("current_surgeons")
            or stage_state.get("last_surgeons")
            or stage_state.get("first_surgeons")
            or _surgeons_from_json(data.get("planned_surgeons_json")),
            split_commas=True,
        )
        data["operating_nurse"] = _normalize_case_text(
            stage_state.get("current_operating_nurse")
            or stage_state.get("last_operating_nurse")
            or stage_state.get("first_operating_nurse")
            or data.get("planned_operating_nurse")
        )
        data["anesthesiologist"] = _normalize_case_text(
            stage_state.get("current_anesthesiologist")
            or stage_state.get("last_anesthesiologist")
            or stage_state.get("first_anesthesiologist")
            or data.get("planned_anesthesiologist")
        )
        data["anesthetist"] = _normalize_case_text(
            stage_state.get("current_anesthetist")
            or stage_state.get("last_anesthetist")
            or stage_state.get("first_anesthetist")
            or data.get("planned_anesthetist")
        )
        return data

    @staticmethod
    def _select_free_recovery_bed_for_rao(cursor: sqlite3.Cursor) -> Optional[int]:
        transfer_order = tuple(int(bed_number) for bed_number in RECOVERY_BED_TRANSFER_ORDER)
        insert_placeholders = ", ".join("(?, 'FREE', NULL, 0)" for _ in transfer_order)
        cursor.execute(
            f"""
            INSERT OR IGNORE INTO beds (bed_number, status, current_admission_id, revision)
            VALUES {insert_placeholders}
            """,
            transfer_order,
        )

        ordered_values = ", ".join("(?, ?)" for _ in transfer_order)
        ordered_params: list[int] = []
        for sort_order, bed_number in enumerate(transfer_order):
            ordered_params.extend((bed_number, sort_order))
        row = cursor.execute(
            f"""
            WITH desired_beds(bed_number, sort_order) AS (VALUES {ordered_values})
            SELECT bed_number
            FROM desired_beds
            JOIN beds USING (bed_number)
            WHERE beds.status = 'FREE'
              AND beds.current_admission_id IS NULL
            ORDER BY desired_beds.sort_order ASC
            LIMIT 1
            """,
            tuple(ordered_params),
        ).fetchone()
        return int(row["bed_number"]) if row else None

    @staticmethod
    def _copy_latest_operblock_vitals_to_rao(
        cursor: sqlite3.Cursor,
        source_admission_id: int,
        rao_admission_id: int,
        event_dt: datetime,
        admission_dt: datetime,
    ) -> int:
        event_dt_text = _minute_floor(event_dt).isoformat(timespec="seconds")
        row = cursor.execute(
            """
            SELECT sys, dia, pulse, temp, spo2, rr, cvp
            FROM vitals
            WHERE admission_id = ?
              AND CAST(STRFTIME('%s', datetime) AS INTEGER) <= CAST(STRFTIME('%s', ?) AS INTEGER)
              AND (
                  sys IS NOT NULL OR dia IS NOT NULL OR pulse IS NOT NULL OR temp IS NOT NULL
                  OR spo2 IS NOT NULL OR rr IS NOT NULL OR cvp IS NOT NULL
              )
            ORDER BY CAST(STRFTIME('%s', datetime) AS INTEGER) DESC, id DESC
            LIMIT 1
            """,
            (int(source_admission_id), event_dt_text),
        ).fetchone()
        vitals = _row_to_dict(row)
        cursor.execute(
            """
            INSERT INTO vitals (
                admission_id, datetime, sys, dia, pulse, temp, spo2, rr, cvp,
                last_modified_by, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'operblock', STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
            """,
            (
                int(rao_admission_id),
                _minute_floor(admission_dt).isoformat(timespec="seconds"),
                vitals.get("sys"),
                vitals.get("dia"),
                vitals.get("pulse"),
                vitals.get("temp"),
                vitals.get("spo2"),
                vitals.get("rr"),
                vitals.get("cvp"),
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _ensure_case_protocol_number(cursor: sqlite3.Cursor, case: dict[str, Any], event_dt: datetime) -> tuple[int, str]:
        operation_case_id = int(case["operation_case_id"])
        row = cursor.execute(
            """
            SELECT anesthesia_protocol_number, anesthesia_protocol_date, table_code
            FROM operation_cases
            WHERE id = ?
            """,
            (operation_case_id,),
        ).fetchone()
        if not row:
            raise OperBlockConflictError("Операция не найдена. Обновите протокол.")
        existing_display = format_operblock_protocol_display(
            row["anesthesia_protocol_number"],
            row["anesthesia_protocol_date"],
        )
        if existing_display:
            return int(row["anesthesia_protocol_number"]), str(row["anesthesia_protocol_date"])

        table_code = str(row["table_code"] or case.get("table_code") or "")
        protocol_date = _minute_floor(event_dt).date().isoformat()
        max_row = cursor.execute(
            """
            SELECT MAX(anesthesia_protocol_number) AS max_number
            FROM operation_cases
            WHERE table_code = ?
              AND anesthesia_protocol_date = ?
              AND id <> ?
            """,
            (table_code, protocol_date, operation_case_id),
        ).fetchone()
        protocol_number = int((max_row["max_number"] if max_row else 0) or 0) + 1
        cursor.execute(
            """
            UPDATE operation_cases
            SET anesthesia_protocol_number = ?,
                anesthesia_protocol_date = ?,
                last_modified_by = 'operblock',
                revision = COALESCE(revision, 0) + 1
            WHERE id = ?
              AND (anesthesia_protocol_number IS NULL OR anesthesia_protocol_date IS NULL)
            """,
            (protocol_number, protocol_date, operation_case_id),
        )
        if cursor.rowcount != 1:
            check_row = cursor.execute(
                """
                SELECT anesthesia_protocol_number, anesthesia_protocol_date
                FROM operation_cases
                WHERE id = ?
                """,
                (operation_case_id,),
            ).fetchone()
            if check_row and format_operblock_protocol_display(
                check_row["anesthesia_protocol_number"],
                check_row["anesthesia_protocol_date"],
            ):
                return int(check_row["anesthesia_protocol_number"]), str(check_row["anesthesia_protocol_date"])
            raise OperBlockConflictError("Не удалось присвоить номер протокола. Обновите протокол.")
        case["anesthesia_protocol_number"] = protocol_number
        case["anesthesia_protocol_date"] = protocol_date
        return protocol_number, protocol_date

    def _validate_medications_before_anesthesia_end(
        self,
        cursor: sqlite3.Cursor,
        case: dict[str, Any],
        start_dt: datetime,
        end_dt: datetime,
    ) -> None:
        admission_id = int(case["admission_id"])
        order_rows = cursor.execute(
            """
            SELECT id, datetime, text
            FROM orders
            WHERE admission_id = ?
              AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
            ORDER BY datetime("datetime") ASC, id ASC
            """,
            (admission_id,),
        ).fetchall()
        for row in order_rows:
            order_dt = _parse_dt(row["datetime"])
            if order_dt is None:
                continue
            order_minute = _minute_floor(order_dt)
            label = str(row["text"] or "назначение").strip()
            if order_minute < start_dt:
                continue
            if order_minute > end_dt:
                raise ValueError(
                    f"Нельзя завершить пособие в {_format_bound_time(end_dt)}: назначение «{label}» "
                    f"запланировано на {_format_bound_time(order_minute)}, то есть выходит за рамки пособия. "
                    "Исправьте время назначения или завершите пособие позже."
                )

        event_rows = cursor.execute(
            """
            SELECT id, event_type, event_time, end_time, display_label, drug_label, status
            FROM operblock_timeline_events
            WHERE operation_case_id = ?
              AND event_type IN ('infusion_start', 'infusion_change', 'infusion_stop')
              AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
            ORDER BY datetime(event_time) ASC, id ASC
            """,
            (int(case["operation_case_id"]),),
        ).fetchall()
        for row in event_rows:
            event_dt = _parse_dt(row["event_time"])
            if event_dt is None:
                continue
            event_minute = _minute_floor(event_dt)
            label = str(row["display_label"] or row["drug_label"] or "инфузия").strip()
            if event_minute < start_dt:
                continue
            if event_minute > end_dt:
                raise ValueError(
                    f"Нельзя завершить пособие в {_format_bound_time(end_dt)}: событие «{label}» "
                    f"указано на {_format_bound_time(event_minute)}, то есть выходит за рамки пособия. "
                    "Исправьте время события или завершите пособие позже."
                )
            end_value = _parse_dt(row["end_time"])
            if end_value is not None and _minute_floor(end_value) > end_dt:
                raise ValueError(
                    f"Нельзя завершить пособие в {_format_bound_time(end_dt)}: инфузия «{label}» "
                    f"завершена в {_format_bound_time(_minute_floor(end_value))}, позже конца пособия. "
                    "Исправьте время остановки инфузии или завершите пособие позже."
                )

    def _auto_stop_open_infusions(
        self,
        cursor: sqlite3.Cursor,
        case: dict[str, Any],
        end_dt: datetime,
    ) -> None:
        rows = cursor.execute(
            """
            SELECT
                id, operation_case_id, admission_id, table_code, event_time, drug_label,
                volume_ml, concentration_text, rate_value, rate_unit, route, COALESCE(revision, 0) AS revision
            FROM operblock_timeline_events
            WHERE operation_case_id = ?
              AND event_type = 'infusion_start'
              AND status = 'active'
            ORDER BY datetime(event_time) ASC, id ASC
            """,
            (int(case["operation_case_id"]),),
        ).fetchall()
        for row in rows:
            start_dt = _parse_dt(row["event_time"])
            if start_dt is None or _minute_floor(start_dt) > end_dt:
                continue
            display_label = f"{row['drug_label']} стоп".strip()
            payload_json = self._timeline_payload_json({"auto_stopped_by": "anesthesia_end"})
            cursor.execute(
                """
                INSERT INTO operblock_timeline_events (
                    operation_case_id, admission_id, table_code, event_type, event_time,
                    drug_label, display_label, raw_text, volume_ml, concentration_text, rate_value,
                    rate_unit, route, status, revision, parent_event_id, payload_json,
                    created_by_role, created_by_client_id, last_modified_by
                ) VALUES (?, ?, ?, 'infusion_stop', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, ?, ?,
                          'operblock', ?, 'operblock')
                """,
                (
                    int(row["operation_case_id"]),
                    int(row["admission_id"]),
                    row["table_code"],
                    end_dt.isoformat(timespec="seconds"),
                    row["drug_label"],
                    display_label,
                    display_label,
                    row["volume_ml"],
                    row["concentration_text"],
                    row["rate_value"],
                    row["rate_unit"],
                    row["route"],
                    int(row["id"]),
                    payload_json,
                    self.client_id,
                ),
            )
            cursor.execute(
                """
                UPDATE operblock_timeline_events
                SET status = 'stopped',
                    end_time = ?,
                    revision = COALESCE(revision, 0) + 1,
                    last_modified_by = 'operblock',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                  AND event_type = 'infusion_start'
                  AND status = 'active'
                """,
                (end_dt.isoformat(timespec="seconds"), int(row["id"])),
            )

    def add_vitals(
        self,
        admission_id: int,
        *,
        sys: Optional[int],
        dia: Optional[int],
        pulse: Optional[int],
        spo2: Optional[int],
    ) -> int:
        validate_operblock_runtime_path(self.db)
        if sys is None and dia is None and pulse is None and spo2 is None:
            raise ValueError("Введите хотя бы один показатель.")
        now = _now_text()

        def operation(cursor: sqlite3.Cursor):
            case = self._assert_active_operation_for_admission(cursor, admission_id)
            self._assert_datetime_in_operation_bounds(now, case, entity_label="Время витальных функций")
            cursor.execute(
                """
                INSERT INTO vitals (
                    admission_id, datetime, sys, dia, pulse, spo2, last_modified_by, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'operblock', STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
                """,
                (int(admission_id), now, sys, dia, pulse, spo2),
            )
            return int(cursor.lastrowid)

        return int(self.db.run_write_operation(operation, source="operblock_add_vitals"))

    def add_vital_record(self, dto: VitalDTO, *, expected_revision: Optional[int] = None) -> int:
        validate_operblock_runtime_path(self.db)
        timestamp = getattr(dto, "timestamp", None)
        if not isinstance(timestamp, datetime):
            raise ValueError("Укажите корректное время витальных функций.")
        if (
            getattr(dto, "sys", None) is None
            and getattr(dto, "dia", None) is None
            and getattr(dto, "pulse", None) is None
            and getattr(dto, "temp", None) is None
            and getattr(dto, "spo2", None) is None
            and getattr(dto, "rr", None) is None
            and getattr(dto, "cvp", None) is None
        ):
            raise ValueError("Введите хотя бы один показатель.")

        def operation(cursor: sqlite3.Cursor):
            case = self._assert_active_operation_for_admission(cursor, int(dto.admission_id))
            self._assert_datetime_in_operation_bounds(timestamp, case, entity_label="Время витальных функций")
            target_minute = timestamp.strftime("%Y-%m-%d %H:%M")
            row = cursor.execute(
                """
                SELECT id, COALESCE(revision, 0) AS revision
                FROM vitals
                WHERE admission_id = ?
                  AND STRFTIME('%Y-%m-%d %H:%M', datetime) = ?
                """,
                (int(dto.admission_id), target_minute),
            ).fetchone()
            last_modified_by = dto.last_modified_by or "operblock"
            if row:
                old_revision = int(row["revision"] or 0)
                assert_revision_matches(old_revision, expected_revision)
                revision_clause = ""
                params: list[Any] = [
                    dto.sys,
                    dto.dia,
                    dto.pulse,
                    dto.temp,
                    dto.spo2,
                    dto.rr,
                    dto.cvp,
                    last_modified_by,
                    int(row["id"]),
                ]
                if expected_revision is not None:
                    revision_clause = " AND COALESCE(revision, 0) = ?"
                    params.append(int(expected_revision))
                cursor.execute(
                    f"""
                    UPDATE vitals
                    SET sys = COALESCE(?, sys),
                        dia = COALESCE(?, dia),
                        pulse = COALESCE(?, pulse),
                        temp = COALESCE(?, temp),
                        spo2 = COALESCE(?, spo2),
                        rr = COALESCE(?, rr),
                        cvp = COALESCE(?, cvp),
                        last_modified_by = ?,
                        revision = COALESCE(revision, 0) + 1,
                        updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                    WHERE id = ?
                      {revision_clause}
                    """,
                    tuple(params),
                )
                if cursor.rowcount != 1:
                    raise DataConflictError(DATA_CONFLICT_MESSAGE)
                dto.id = int(row["id"])
                dto.revision = old_revision + 1
                return int(row["id"])

            if expected_revision is not None:
                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            cursor.execute(
                """
                INSERT INTO vitals (
                    admission_id, datetime, sys, dia, pulse, temp, spo2, rr, cvp,
                    last_modified_by, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
                """,
                (
                    int(dto.admission_id),
                    timestamp.isoformat(),
                    dto.sys,
                    dto.dia,
                    dto.pulse,
                    dto.temp,
                    dto.spo2,
                    dto.rr,
                    dto.cvp,
                    last_modified_by,
                ),
            )
            dto.id = int(cursor.lastrowid)
            dto.revision = 0
            return int(dto.id)

        return int(self.db.run_write_operation(operation, source="operblock_add_vital_record"))

    def delete_last_vital_record(
        self,
        admission_id: int,
        *,
        expected_revision: Optional[int] = None,
    ) -> Optional[int]:
        validate_operblock_runtime_path(self.db)

        def operation(cursor: sqlite3.Cursor):
            case = self._assert_active_operation_for_admission(cursor, admission_id)
            started_at = _parse_dt(case.get("started_at"))
            ended_at = _parse_dt(case.get("ended_at"))
            if started_at is None:
                raise OperBlockConflictError("У операции не задано время начала. Обновите протокол.")
            params: list[Any] = [int(admission_id), _minute_floor(started_at).isoformat()]
            end_clause = ""
            if ended_at is not None:
                end_clause = 'AND datetime("datetime") <= datetime(?)'
                params.append(_minute_floor(ended_at).isoformat())
            row = cursor.execute(
                f"""
                SELECT id, COALESCE(revision, 0) AS revision
                FROM vitals
                WHERE admission_id = ?
                  AND datetime("datetime") >= datetime(?)
                  {end_clause}
                ORDER BY datetime("datetime") DESC, id DESC
                LIMIT 1
                """,
                tuple(params),
            ).fetchone()
            if not row:
                return None
            assert_revision_matches(row["revision"], expected_revision)
            revision_clause = ""
            delete_params: list[Any] = [int(row["id"])]
            if expected_revision is not None:
                revision_clause = " AND COALESCE(revision, 0) = ?"
                delete_params.append(int(expected_revision))
            cursor.execute(
                f"""
                DELETE FROM vitals
                WHERE id = ?
                  {revision_clause}
                """,
                tuple(delete_params),
            )
            if cursor.rowcount != 1:
                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            return int(row["id"])

        result = self.db.run_write_operation(operation, source="operblock_delete_last_vital_record")
        return int(result) if result is not None else None

    def add_order(
        self,
        admission_id: int,
        text: str,
        *,
        preset_payload: Optional[dict[str, Any]] = None,
        route: str | None = None,
        return_row: bool = False,
    ) -> int | dict[str, Any]:
        validate_operblock_runtime_path(self.db)
        clean_text = str(text or "").strip()
        if not clean_text:
            raise ValueError("Текст назначения не заполнен.")
        preset_id = ""
        if isinstance(preset_payload, dict):
            preset_id = str(preset_payload.get("preset_id") or "").strip()
        comment_text = _operblock_order_comment_with_route("", route) if route is not None else ""
        now = _now_text()

        def operation(cursor: sqlite3.Cursor):
            case = self._assert_active_operation_for_admission(cursor, admission_id)
            self._assert_datetime_in_active_anesthesia_bounds(cursor, now, case, entity_label="Время назначения")
            row = cursor.execute(
                """
                SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_sort_order
                FROM orders
                WHERE admission_id = ?
                """,
                (int(admission_id),),
            ).fetchone()
            sort_order = int(row["next_sort_order"] if row and row["next_sort_order"] is not None else 0)
            cursor.execute(
                """
                INSERT INTO orders (
                    admission_id, datetime, text, drug_key, latin, type, status, dose_value, dose_unit,
                    frequency, specific_times, sort_order, draft_sort_order, is_finalized,
                    is_committed, created_at, comment, last_modified_by, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'observation', 'active', 0, '', 1, '[]', ?, ?, 1, 1, ?, ?, 'operblock',
                          STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
                """,
                (
                    int(admission_id),
                    now,
                    clean_text,
                    preset_id or None,
                    clean_text,
                    sort_order,
                    sort_order,
                    now,
                    comment_text,
                ),
            )
            order_id = int(cursor.lastrowid)
            if not return_row:
                return order_id
            row = cursor.execute(
                """
                SELECT
                    id,
                    datetime,
                    text,
                    drug_key,
                    status,
                    is_committed,
                    comment,
                    created_at,
                    updated_at,
                    COALESCE(revision, 0) AS revision
                FROM orders
                WHERE id = ?
                """,
                (order_id,),
            ).fetchone()
            data = _row_to_dict(row)
            display_name = ""
            if isinstance(preset_payload, Mapping):
                display_name = str(
                    preset_payload.get("display_name")
                    or preset_payload.get("label")
                    or preset_payload.get("latin")
                    or ""
                ).strip()
            if display_name:
                data["drug_display_name"] = display_name
            return data

        result = self.db.run_write_operation(operation, source="operblock_add_order")
        return dict(result or {}) if return_row else int(result)

    def update_order_text(
        self,
        admission_id: int,
        order_id: int,
        text: str,
        datetime_text: str | None = None,
        *,
        expected_revision: Optional[int] = None,
        route: str | None = None,
    ) -> int:
        validate_operblock_runtime_path(self.db)
        clean_text = str(text or "").strip()
        if not clean_text:
            raise ValueError("Текст назначения не заполнен.")
        clean_datetime = _normalize_order_datetime_text(datetime_text) if datetime_text is not None else None

        def operation(cursor: sqlite3.Cursor):
            case = self._assert_active_operation_for_admission(cursor, admission_id)
            row = cursor.execute(
                """
                SELECT id, datetime, text, comment, COALESCE(revision, 0) AS revision
                FROM orders
                WHERE id = ?
                  AND admission_id = ?
                  AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                """,
                (int(order_id), int(admission_id)),
            ).fetchone()
            if not row:
                raise OperBlockConflictError("Назначение не найдено или уже удалено.")
            assert_revision_matches(row["revision"], expected_revision)
            effective_datetime = clean_datetime if clean_datetime is not None else row["datetime"]
            self._assert_datetime_in_active_anesthesia_bounds(
                cursor,
                effective_datetime,
                case,
                entity_label="Время назначения",
            )
            revision_clause = ""
            comment_text = (
                _operblock_order_comment_with_route(row["comment"], route)
                if route is not None
                else str(row["comment"] or "")
            )
            if (
                str(row["text"] or "").strip() == clean_text
                and str(row["datetime"] or "") == str(effective_datetime or "")
                and str(row["comment"] or "") == comment_text
            ):
                return int(order_id)
            params: list[Any] = [clean_text, clean_text, clean_datetime, comment_text, int(order_id), int(admission_id)]
            if expected_revision is not None:
                revision_clause = " AND COALESCE(revision, 0) = ?"
                params.append(int(expected_revision))
            cursor.execute(
                f"""
                UPDATE orders
                SET text = ?,
                    latin = ?,
                    "datetime" = COALESCE(?, "datetime"),
                    comment = ?,
                    last_modified_by = 'operblock',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now'),
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ?
                  AND admission_id = ?
                  {revision_clause}
                """,
                tuple(params),
            )
            if cursor.rowcount != 1:
                assert_revision_matches(None, expected_revision)
                raise OperBlockConflictError("Назначение не найдено или уже изменено другим пользователем.")
            return int(order_id)

        return int(self.db.run_write_operation(operation, source="operblock_update_order_text"))

    def delete_order(self, admission_id: int, order_id: int, *, expected_revision: Optional[int] = None) -> int:
        validate_operblock_runtime_path(self.db)

        def operation(cursor: sqlite3.Cursor):
            case = self._assert_active_operation_for_admission(cursor, admission_id)
            self._require_active_anesthesia_interval(cursor, case, entity_label="Удаление назначения")
            row = cursor.execute(
                """
                SELECT id, COALESCE(revision, 0) AS revision
                FROM orders
                WHERE id = ?
                  AND admission_id = ?
                  AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                """,
                (int(order_id), int(admission_id)),
            ).fetchone()
            if not row:
                raise OperBlockConflictError("Назначение не найдено или уже удалено.")
            assert_revision_matches(row["revision"], expected_revision)
            revision_clause = ""
            params: list[Any] = [int(order_id), int(admission_id)]
            if expected_revision is not None:
                revision_clause = " AND COALESCE(revision, 0) = ?"
                params.append(int(expected_revision))
            cursor.execute(
                f"""
                UPDATE orders
                SET status = 'deleted',
                    last_modified_by = 'operblock',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now'),
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ?
                  AND admission_id = ?
                  {revision_clause}
                """,
                tuple(params),
            )
            if cursor.rowcount != 1:
                assert_revision_matches(None, expected_revision)
                raise OperBlockConflictError("Назначение не найдено или уже изменено другим пользователем.")
            return int(order_id)

        return int(self.db.run_write_operation(operation, source="operblock_delete_order"))

    def start_infusion(
        self,
        admission_id: int,
        operation_case_id: int,
        drug_label: str,
        rate_value: Any = None,
        rate_unit: str = "",
        event_time: Any = None,
        *,
        concentration_text: str | None = None,
        volume_ml: Any = None,
        route: str | None = None,
        payload: Optional[dict[str, Any]] = None,
        return_event: bool = False,
    ) -> int | dict[str, Any]:
        validate_operblock_runtime_path(self.db)
        clean_drug = str(drug_label or "").strip()
        if not clean_drug:
            raise ValueError("Укажите препарат для инфузии.")
        clean_rate_value = str(rate_value or "").strip()
        clean_rate_unit = str(rate_unit or "").strip()
        clean_volume_ml = re.sub(r"\s*мл\s*$", "", str(volume_ml or "").strip(), flags=re.IGNORECASE).strip()
        payload_data = dict(payload or {}) if isinstance(payload, Mapping) else {}
        payload_dose_text = re.sub(
            r"\s+",
            " ",
            str(payload_data.get("display_dose_text") or payload_data.get("dose_text") or "").strip(),
        )
        is_gas_infusion = self._payload_is_gas(payload_data)
        if is_gas_infusion:
            payload_data["kind"] = "gas"
        if (clean_rate_value and not clean_rate_unit) or (clean_rate_unit and not clean_rate_value):
            raise ValueError("Укажите скорость инфузии полностью.")
        if clean_volume_ml and not re.fullmatch(r"\d+(?:[,.]\d+)?", clean_volume_ml):
            raise ValueError("Укажите объем инфузии в мл.")
        if not clean_rate_value and not clean_volume_ml and not payload_dose_text:
            raise ValueError("Укажите скорость или объем инфузии.")
        event_dt = self._normalize_timeline_event_datetime(event_time)
        payload_json = self._timeline_payload_json(payload_data)

        def operation(cursor: sqlite3.Cursor):
            case = self._assert_active_operation_for_case_admission(cursor, admission_id, operation_case_id)
            self._assert_datetime_in_active_anesthesia_bounds(cursor, event_dt, case, entity_label="Время инфузии")
            if is_gas_infusion:
                is_oxygen = self._payload_is_oxygen(payload_data, clean_drug)
                if self._active_gas_start_for_case(
                    cursor,
                    admission_id,
                    operation_case_id,
                    oxygen=is_oxygen,
                ):
                    if is_oxygen:
                        raise OperBlockConflictError(
                            "Кислород уже идет. Измените поток активного кислорода, второй кислород запустить нельзя."
                        )
                    raise OperBlockConflictError(
                        "Газ уже идет. Измените дозу активного газа, второй ингаляционный газ запустить нельзя."
                    )
            rate_tail = f"{clean_rate_value} {clean_rate_unit}".strip()
            volume_tail = f"{clean_volume_ml} мл".strip() if clean_volume_ml else ""
            tail = rate_tail or volume_tail or payload_dose_text
            if tail and tail.casefold() in clean_drug.casefold():
                tail = ""
            display_label = f"{clean_drug} {tail}".strip()
            cursor.execute(
                """
                INSERT INTO operblock_timeline_events (
                    operation_case_id, admission_id, table_code, event_type, event_time,
                    drug_label, display_label, raw_text, volume_ml, concentration_text, rate_value,
                    rate_unit, route, status, revision, payload_json,
                    created_by_role, created_by_client_id, last_modified_by
                ) VALUES (?, ?, ?, 'infusion_start', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, ?,
                          'operblock', ?, 'operblock')
                """,
                (
                    int(operation_case_id),
                    int(admission_id),
                    case.get("table_code"),
                    event_dt.isoformat(timespec="seconds"),
                    clean_drug,
                    display_label,
                    display_label,
                    clean_volume_ml or None,
                    str(concentration_text or "").strip() or None,
                    clean_rate_value or None,
                    clean_rate_unit or None,
                    str(route or "").strip() or None,
                    payload_json,
                    self.client_id,
                ),
            )
            event_id = int(cursor.lastrowid)
            if not return_event:
                return event_id
            row = cursor.execute(
                """
                SELECT
                    id,
                    operation_case_id,
                    admission_id,
                    table_code,
                    event_type,
                    event_time,
                    end_time,
                    drug_label,
                    display_label,
                    raw_text,
                    dose_value,
                    dose_unit,
                    volume_ml,
                    concentration_text,
                    rate_value,
                    rate_unit,
                    route,
                    status,
                    COALESCE(revision, 0) AS revision,
                    created_at,
                    updated_at,
                    payload_json,
                    parent_event_id
                FROM operblock_timeline_events
                WHERE id = ?
                """,
                (event_id,),
            ).fetchone()
            return _row_to_dict(row)

        result = self.db.run_write_operation(operation, source="operblock_start_infusion")
        return dict(result or {}) if return_event else int(result)

    def change_infusion_rate(
        self,
        start_event_id: int,
        *,
        expected_revision: Optional[int],
        new_rate_value: Any,
        new_rate_unit: str,
        event_time: Any,
        start_event_time: Any = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> int:
        validate_operblock_runtime_path(self.db)
        if expected_revision is None:
            raise ValueError("Не удалось проверить актуальность инфузии. Обновите протокол.")
        clean_rate_value = str(new_rate_value or "").strip()
        clean_rate_unit = str(new_rate_unit or "").strip()
        if not clean_rate_value or not clean_rate_unit:
            raise ValueError("Укажите новую скорость инфузии.")
        event_dt = self._normalize_timeline_event_datetime(event_time)
        start_event_dt = self._normalize_timeline_event_datetime(start_event_time) if start_event_time is not None else None
        payload_json = self._timeline_payload_json(payload)

        def operation(cursor: sqlite3.Cursor):
            start = self._get_active_infusion_start_for_update(cursor, start_event_id)
            assert_revision_matches(start["revision"], expected_revision)
            if self._payload_is_gas(_parse_json_dict(start["payload_json"])):
                raise ValueError("Для газа измените дозу или поток. Скорость в мл/час для газа не применяется.")
            case = self._assert_active_operation_for_case_admission(
                cursor,
                int(start["admission_id"]),
                int(start["operation_case_id"]),
            )
            self._assert_datetime_in_active_anesthesia_bounds(
                cursor,
                event_dt,
                case,
                entity_label="Время изменения инфузии",
            )
            if start_event_dt is not None:
                self._assert_datetime_in_active_anesthesia_bounds(
                    cursor,
                    start_event_dt,
                    case,
                    entity_label="Время начала инфузии",
                )
                first_child = cursor.execute(
                    """
                    SELECT event_time
                    FROM operblock_timeline_events
                    WHERE parent_event_id = ?
                      AND event_type IN ('infusion_change', 'infusion_stop')
                      AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                    ORDER BY datetime(event_time) ASC, id ASC
                    LIMIT 1
                    """,
                    (int(start_event_id),),
                ).fetchone()
                first_child_dt = _parse_dt(first_child["event_time"]) if first_child else None
                if first_child_dt is not None and _minute_floor(start_event_dt) > _minute_floor(first_child_dt):
                    raise ValueError(
                        "Время начала инфузии не может быть позже первого события инфузии: "
                        f"{_minute_floor(first_child_dt).strftime('%d.%m.%Y %H:%M')}."
                    )
                if _minute_floor(event_dt) < _minute_floor(start_event_dt):
                    raise ValueError(
                        f"Событие инфузии не может быть раньше начала инфузии: "
                        f"{_format_bound_time(_minute_floor(start_event_dt))}."
                    )
            else:
                self._assert_infusion_event_not_before_start(event_dt, start)
            display_label = f"{start['drug_label']} {clean_rate_value} {clean_rate_unit}".strip()
            cursor.execute(
                """
                INSERT INTO operblock_timeline_events (
                    operation_case_id, admission_id, table_code, event_type, event_time,
                    drug_label, display_label, raw_text, concentration_text, rate_value,
                    rate_unit, route, status, revision, parent_event_id, payload_json,
                    created_by_role, created_by_client_id, last_modified_by
                ) VALUES (?, ?, ?, 'infusion_change', ?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, ?, ?,
                          'operblock', ?, 'operblock')
                """,
                (
                    int(start["operation_case_id"]),
                    int(start["admission_id"]),
                    start["table_code"],
                    event_dt.isoformat(timespec="seconds"),
                    start["drug_label"],
                    display_label,
                    display_label,
                    start["concentration_text"],
                    clean_rate_value,
                    clean_rate_unit,
                    start["route"],
                    int(start_event_id),
                    payload_json,
                    self.client_id,
                ),
            )
            event_id = int(cursor.lastrowid)
            cursor.execute(
                """
                UPDATE operblock_timeline_events
                SET event_time = COALESCE(?, event_time),
                    revision = COALESCE(revision, 0) + 1,
                    last_modified_by = 'operblock',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                  AND event_type = 'infusion_start'
                  AND status = 'active'
                  AND COALESCE(revision, 0) = ?
                """,
                (
                    start_event_dt.isoformat(timespec="seconds") if start_event_dt is not None else None,
                    int(start_event_id),
                    int(expected_revision),
                ),
            )
            if cursor.rowcount != 1:
                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            return event_id

        return int(self.db.run_write_operation(operation, source="operblock_change_infusion_rate"))

    def change_gas_dose(
        self,
        start_event_id: int,
        *,
        expected_revision: Optional[int],
        dose_text: str,
        event_time: Any,
        start_event_time: Any = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> int:
        validate_operblock_runtime_path(self.db)
        if expected_revision is None:
            raise ValueError("Не удалось проверить актуальность газа. Обновите протокол.")
        clean_dose = re.sub(r"\s+", " ", str(dose_text or "").strip())
        if not clean_dose:
            raise ValueError("Укажите дозу газа или поток кислорода.")
        event_dt = self._normalize_timeline_event_datetime(event_time)
        start_event_dt = self._normalize_timeline_event_datetime(start_event_time) if start_event_time is not None else None

        effective_payload = dict(payload or {}) if isinstance(payload, Mapping) else {}
        effective_payload["kind"] = "gas"
        effective_payload["dose_text"] = clean_dose
        effective_payload["display_dose_text"] = clean_dose
        payload_json = self._timeline_payload_json(effective_payload)

        def operation(cursor: sqlite3.Cursor):
            start = self._get_active_infusion_start_for_update(cursor, start_event_id)
            assert_revision_matches(start["revision"], expected_revision)
            if not self._payload_is_gas(_parse_json_dict(start["payload_json"])):
                raise ValueError("Для дозатора изменяется скорость, для газа - доза или поток.")
            case = self._assert_active_operation_for_case_admission(
                cursor,
                int(start["admission_id"]),
                int(start["operation_case_id"]),
            )
            self._assert_datetime_in_active_anesthesia_bounds(
                cursor,
                event_dt,
                case,
                entity_label="Время изменения газа",
            )
            if start_event_dt is not None:
                self._assert_datetime_in_active_anesthesia_bounds(
                    cursor,
                    start_event_dt,
                    case,
                    entity_label="Время начала газа",
                )
                first_child = cursor.execute(
                    """
                    SELECT event_time
                    FROM operblock_timeline_events
                    WHERE parent_event_id = ?
                      AND event_type IN ('infusion_change', 'infusion_stop')
                      AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                    ORDER BY datetime(event_time) ASC, id ASC
                    LIMIT 1
                    """,
                    (int(start_event_id),),
                ).fetchone()
                first_child_dt = _parse_dt(first_child["event_time"]) if first_child else None
                if first_child_dt is not None and _minute_floor(start_event_dt) > _minute_floor(first_child_dt):
                    raise ValueError(
                        "Время начала газа не может быть позже первого события газа: "
                        f"{_minute_floor(first_child_dt).strftime('%d.%m.%Y %H:%M')}."
                    )
                if _minute_floor(event_dt) < _minute_floor(start_event_dt):
                    raise ValueError(
                        f"Изменение газа не может быть раньше начала газа: "
                        f"{_format_bound_time(_minute_floor(start_event_dt))}."
                    )
            else:
                self._assert_infusion_event_not_before_start(event_dt, start)

            display_label = f"{start['drug_label']} {clean_dose}".strip()
            cursor.execute(
                """
                INSERT INTO operblock_timeline_events (
                    operation_case_id, admission_id, table_code, event_type, event_time,
                    drug_label, display_label, raw_text, concentration_text, route, status,
                    revision, parent_event_id, payload_json,
                    created_by_role, created_by_client_id, last_modified_by
                ) VALUES (?, ?, ?, 'infusion_change', ?, ?, ?, ?, ?, ?, 'active',
                          1, ?, ?, 'operblock', ?, 'operblock')
                """,
                (
                    int(start["operation_case_id"]),
                    int(start["admission_id"]),
                    start["table_code"],
                    event_dt.isoformat(timespec="seconds"),
                    start["drug_label"],
                    display_label,
                    display_label,
                    start["concentration_text"],
                    start["route"],
                    int(start_event_id),
                    payload_json,
                    self.client_id,
                ),
            )
            event_id = int(cursor.lastrowid)
            cursor.execute(
                """
                UPDATE operblock_timeline_events
                SET event_time = COALESCE(?, event_time),
                    rate_value = NULL,
                    rate_unit = NULL,
                    revision = COALESCE(revision, 0) + 1,
                    last_modified_by = 'operblock',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                  AND event_type = 'infusion_start'
                  AND status = 'active'
                  AND COALESCE(revision, 0) = ?
                """,
                (
                    start_event_dt.isoformat(timespec="seconds") if start_event_dt is not None else None,
                    int(start_event_id),
                    int(expected_revision),
                ),
            )
            if cursor.rowcount != 1:
                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            return event_id

        return int(self.db.run_write_operation(operation, source="operblock_change_gas_dose"))

    def update_infusion_start_time(
        self,
        start_event_id: int,
        *,
        expected_revision: Optional[int],
        event_time: Any,
    ) -> int:
        validate_operblock_runtime_path(self.db)
        if expected_revision is None:
            raise ValueError("Не удалось проверить актуальность инфузии. Обновите протокол.")
        event_dt = self._normalize_timeline_event_datetime(event_time)

        def operation(cursor: sqlite3.Cursor):
            start = self._get_infusion_start_for_update(
                cursor,
                start_event_id,
                allowed_statuses={"active", "stopped"},
            )
            assert_revision_matches(start["revision"], expected_revision)
            case = self._assert_active_operation_for_case_admission(
                cursor,
                int(start["admission_id"]),
                int(start["operation_case_id"]),
            )
            self._assert_datetime_in_active_anesthesia_bounds(
                cursor,
                event_dt,
                case,
                entity_label="Время начала инфузии",
            )
            first_child = cursor.execute(
                """
                SELECT event_time
                FROM operblock_timeline_events
                WHERE parent_event_id = ?
                  AND event_type IN ('infusion_change', 'infusion_stop')
                  AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                ORDER BY datetime(event_time) ASC, id ASC
                LIMIT 1
                """,
                (int(start_event_id),),
            ).fetchone()
            first_child_dt = _parse_dt(first_child["event_time"]) if first_child else None
            if first_child_dt is not None and _minute_floor(event_dt) > _minute_floor(first_child_dt):
                raise ValueError(
                    "Время начала инфузии не может быть позже первого события инфузии: "
                    f"{_minute_floor(first_child_dt).strftime('%d.%m.%Y %H:%M')}."
                )
            cursor.execute(
                """
                UPDATE operblock_timeline_events
                SET event_time = ?,
                    revision = COALESCE(revision, 0) + 1,
                    last_modified_by = 'operblock',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                  AND event_type = 'infusion_start'
                  AND status IN ('active', 'stopped')
                  AND COALESCE(revision, 0) = ?
                """,
                (event_dt.isoformat(timespec="seconds"), int(start_event_id), int(expected_revision)),
            )
            if cursor.rowcount != 1:
                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            return int(start_event_id)

        return int(self.db.run_write_operation(operation, source="operblock_update_infusion_start_time"))

    def update_infusion_volume(
        self,
        start_event_id: int,
        *,
        expected_revision: Optional[int],
        volume_ml: Any,
        event_time: Any = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> int:
        validate_operblock_runtime_path(self.db)
        if expected_revision is None:
            raise ValueError("Не удалось проверить актуальность инфузии. Обновите протокол.")
        clean_volume_ml = re.sub(r"\s*мл\s*$", "", str(volume_ml or "").strip(), flags=re.IGNORECASE).strip()
        if not clean_volume_ml or not re.fullmatch(r"\d+(?:[,.]\d+)?", clean_volume_ml):
            raise ValueError("Укажите объем инфузии в мл.")
        try:
            if float(clean_volume_ml.replace(",", ".")) <= 0:
                raise ValueError
        except Exception as exc:
            raise ValueError("Укажите объем инфузии в мл.") from exc
        event_dt = self._normalize_timeline_event_datetime(event_time) if event_time is not None else None

        def operation(cursor: sqlite3.Cursor):
            start = self._get_infusion_start_for_update(
                cursor,
                start_event_id,
                allowed_statuses={"active", "stopped"},
            )
            assert_revision_matches(start["revision"], expected_revision)
            effective_payload = _parse_json_dict(start["payload_json"])
            if isinstance(payload, dict):
                effective_payload.update({key: value for key, value in payload.items() if value not in (None, "", [])})
            effective_payload["volume_ml"] = clean_volume_ml
            effective_payload["declared_total_volume_ml"] = clean_volume_ml
            payload_json = self._timeline_payload_json(effective_payload)
            display_label = f"{start['drug_label']} {clean_volume_ml} мл".strip()
            if event_dt is not None:
                case = self._assert_active_operation_for_case_admission(
                    cursor,
                    int(start["admission_id"]),
                    int(start["operation_case_id"]),
                )
                self._assert_datetime_in_active_anesthesia_bounds(
                    cursor,
                    event_dt,
                    case,
                    entity_label="Время начала инфузии",
                )
                first_child = cursor.execute(
                    """
                    SELECT event_time
                    FROM operblock_timeline_events
                    WHERE parent_event_id = ?
                      AND event_type IN ('infusion_change', 'infusion_stop')
                      AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                    ORDER BY datetime(event_time) ASC, id ASC
                    LIMIT 1
                    """,
                    (int(start_event_id),),
                ).fetchone()
                first_child_dt = _parse_dt(first_child["event_time"]) if first_child else None
                if first_child_dt is not None and _minute_floor(event_dt) > _minute_floor(first_child_dt):
                    raise ValueError(
                        "Время начала инфузии не может быть позже первого события инфузии: "
                        f"{_minute_floor(first_child_dt).strftime('%d.%m.%Y %H:%M')}."
                    )
            cursor.execute(
                """
                UPDATE operblock_timeline_events
                SET display_label = ?,
                    raw_text = ?,
                    event_time = COALESCE(?, event_time),
                    volume_ml = ?,
                    payload_json = ?,
                    revision = COALESCE(revision, 0) + 1,
                    last_modified_by = 'operblock',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                  AND event_type = 'infusion_start'
                  AND status IN ('active', 'stopped')
                  AND COALESCE(revision, 0) = ?
                """,
                (
                    display_label,
                    display_label,
                    event_dt.isoformat(timespec="seconds") if event_dt is not None else None,
                    clean_volume_ml,
                    payload_json,
                    int(start_event_id),
                    int(expected_revision),
                ),
            )
            if cursor.rowcount != 1:
                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            return int(start_event_id)

        return int(self.db.run_write_operation(operation, source="operblock_update_infusion_volume"))

    def update_infusion_dose_text(
        self,
        start_event_id: int,
        *,
        expected_revision: Optional[int],
        dose_text: str,
        event_time: Any = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> int:
        validate_operblock_runtime_path(self.db)
        if expected_revision is None:
            raise ValueError("Не удалось проверить актуальность инфузии. Обновите протокол.")
        clean_dose = re.sub(r"\s+", " ", str(dose_text or "").strip())
        if not clean_dose:
            raise ValueError("Укажите дозу назначения.")
        event_dt = self._normalize_timeline_event_datetime(event_time) if event_time is not None else None

        def operation(cursor: sqlite3.Cursor):
            start = self._get_infusion_start_for_update(
                cursor,
                start_event_id,
                allowed_statuses={"active", "stopped"},
            )
            assert_revision_matches(start["revision"], expected_revision)
            effective_payload = _parse_json_dict(start["payload_json"])
            if isinstance(payload, dict):
                effective_payload.update({key: value for key, value in payload.items() if value not in (None, "", [])})
            effective_payload["dose_text"] = clean_dose
            effective_payload["display_dose_text"] = clean_dose
            is_gas_infusion = self._payload_is_gas(effective_payload)
            if is_gas_infusion:
                effective_payload["kind"] = "gas"
            payload_json = self._timeline_payload_json(effective_payload)
            display_label = f"{start['drug_label']} {clean_dose}".strip()
            if event_dt is not None:
                case = self._assert_active_operation_for_case_admission(
                    cursor,
                    int(start["admission_id"]),
                    int(start["operation_case_id"]),
                )
                self._assert_datetime_in_active_anesthesia_bounds(
                    cursor,
                    event_dt,
                    case,
                    entity_label="Время начала назначения",
                )
                first_child = cursor.execute(
                    """
                    SELECT event_time
                    FROM operblock_timeline_events
                    WHERE parent_event_id = ?
                      AND event_type IN ('infusion_change', 'infusion_stop')
                      AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                    ORDER BY datetime(event_time) ASC, id ASC
                    LIMIT 1
                    """,
                    (int(start_event_id),),
                ).fetchone()
                first_child_dt = _parse_dt(first_child["event_time"]) if first_child else None
                if first_child_dt is not None and _minute_floor(event_dt) > _minute_floor(first_child_dt):
                    raise ValueError(
                        "Время начала назначения не может быть позже первого события назначения: "
                        f"{_minute_floor(first_child_dt).strftime('%d.%m.%Y %H:%M')}."
                    )
            cursor.execute(
                """
                UPDATE operblock_timeline_events
                SET display_label = ?,
                    raw_text = ?,
                    event_time = COALESCE(?, event_time),
                    rate_value = CASE WHEN ? THEN NULL ELSE rate_value END,
                    rate_unit = CASE WHEN ? THEN NULL ELSE rate_unit END,
                    payload_json = ?,
                    revision = COALESCE(revision, 0) + 1,
                    last_modified_by = 'operblock',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                  AND event_type = 'infusion_start'
                  AND status IN ('active', 'stopped')
                  AND COALESCE(revision, 0) = ?
                """,
                (
                    display_label,
                    display_label,
                    event_dt.isoformat(timespec="seconds") if event_dt is not None else None,
                    1 if is_gas_infusion else 0,
                    1 if is_gas_infusion else 0,
                    payload_json,
                    int(start_event_id),
                    int(expected_revision),
                ),
            )
            if cursor.rowcount != 1:
                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            if is_gas_infusion:
                cursor.execute(
                    """
                    UPDATE operblock_timeline_events
                    SET status = 'cancelled',
                        revision = COALESCE(revision, 0) + 1,
                        last_modified_by = 'operblock',
                        updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                    WHERE parent_event_id = ?
                      AND event_type = 'infusion_change'
                      AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                    """,
                    (int(start_event_id),),
                )
            return int(start_event_id)

        return int(self.db.run_write_operation(operation, source="operblock_update_infusion_dose_text"))

    def stop_infusion(
        self,
        start_event_id: int,
        *,
        expected_revision: Optional[int],
        event_time: Any,
        payload: Optional[dict[str, Any]] = None,
    ) -> int:
        validate_operblock_runtime_path(self.db)
        if expected_revision is None:
            raise ValueError("Не удалось проверить актуальность инфузии. Обновите протокол.")
        event_dt = self._normalize_timeline_event_datetime(event_time)
        payload_json = self._timeline_payload_json(payload)

        def operation(cursor: sqlite3.Cursor):
            start = self._get_active_infusion_start_for_update(cursor, start_event_id)
            assert_revision_matches(start["revision"], expected_revision)
            case = self._assert_active_operation_for_case_admission(
                cursor,
                int(start["admission_id"]),
                int(start["operation_case_id"]),
            )
            self._assert_datetime_in_active_anesthesia_bounds(
                cursor,
                event_dt,
                case,
                entity_label="Время остановки инфузии",
            )
            self._assert_infusion_event_not_before_start(event_dt, start)
            self._assert_infusion_stop_not_before_latest_change(cursor, int(start_event_id), event_dt)
            display_label = f"{start['drug_label']} стоп".strip()
            cursor.execute(
                """
                INSERT INTO operblock_timeline_events (
                    operation_case_id, admission_id, table_code, event_type, event_time,
                    drug_label, display_label, raw_text, volume_ml, concentration_text, rate_value,
                    rate_unit, route, status, revision, parent_event_id, payload_json,
                    created_by_role, created_by_client_id, last_modified_by
                ) VALUES (?, ?, ?, 'infusion_stop', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, ?, ?,
                          'operblock', ?, 'operblock')
                """,
                (
                    int(start["operation_case_id"]),
                    int(start["admission_id"]),
                    start["table_code"],
                    event_dt.isoformat(timespec="seconds"),
                    start["drug_label"],
                    display_label,
                    display_label,
                    start["volume_ml"],
                    start["concentration_text"],
                    start["rate_value"],
                    start["rate_unit"],
                    start["route"],
                    int(start_event_id),
                    payload_json,
                    self.client_id,
                ),
            )
            event_id = int(cursor.lastrowid)
            cursor.execute(
                """
                UPDATE operblock_timeline_events
                SET status = 'stopped',
                    end_time = ?,
                    revision = COALESCE(revision, 0) + 1,
                    last_modified_by = 'operblock',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                  AND event_type = 'infusion_start'
                  AND status = 'active'
                  AND COALESCE(revision, 0) = ?
                """,
                (event_dt.isoformat(timespec="seconds"), int(start_event_id), int(expected_revision)),
            )
            if cursor.rowcount != 1:
                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            return event_id

        return int(self.db.run_write_operation(operation, source="operblock_stop_infusion"))

    def delete_infusion(self, start_event_id: int, *, expected_revision: Optional[int]) -> int:
        validate_operblock_runtime_path(self.db)
        if expected_revision is None:
            raise ValueError("Не удалось проверить актуальность инфузии. Обновите протокол.")

        def operation(cursor: sqlite3.Cursor):
            start = self._get_infusion_start_for_update(
                cursor,
                start_event_id,
                allowed_statuses={"active", "stopped"},
            )
            assert_revision_matches(start["revision"], expected_revision)
            case = self._assert_active_operation_for_case_admission(
                cursor,
                int(start["admission_id"]),
                int(start["operation_case_id"]),
            )
            self._require_active_anesthesia_interval(cursor, case, entity_label="Удаление инфузии")
            cursor.execute(
                """
                UPDATE operblock_timeline_events
                SET status = 'deleted',
                    revision = COALESCE(revision, 0) + 1,
                    last_modified_by = 'operblock',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                  AND event_type = 'infusion_start'
                  AND status IN ('active', 'stopped')
                  AND COALESCE(revision, 0) = ?
                """,
                (int(start_event_id), int(expected_revision)),
            )
            if cursor.rowcount != 1:
                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            cursor.execute(
                """
                UPDATE operblock_timeline_events
                SET status = 'deleted',
                    revision = COALESCE(revision, 0) + 1,
                    last_modified_by = 'operblock',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE parent_event_id = ?
                  AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                """,
                (int(start_event_id),),
            )
            return int(start_event_id)

        return int(self.db.run_write_operation(operation, source="operblock_delete_infusion"))

    def undo_last_action(self, operation_case_id: int) -> dict[str, Any]:
        validate_operblock_runtime_path(self.db)

        def operation(cursor: sqlite3.Cursor):
            case = self._assert_active_operation_case_for_update(cursor, operation_case_id)
            admission_id = int(case["admission_id"])
            candidates: list[dict[str, Any]] = []

            vital = cursor.execute(
                """
                SELECT id, datetime, updated_at, COALESCE(revision, 0) AS revision
                FROM vitals
                WHERE admission_id = ?
                ORDER BY datetime(COALESCE(updated_at, datetime)) DESC, id DESC
                LIMIT 1
                """,
                (admission_id,),
            ).fetchone()
            if vital:
                action_dt = _parse_dt(vital["updated_at"]) or _parse_dt(vital["datetime"]) or datetime.min
                candidates.append({"kind": "vital", "row": vital, "action_dt": action_dt, "id": int(vital["id"] or 0)})

            order = cursor.execute(
                """
                SELECT id, datetime, updated_at, text, COALESCE(revision, 0) AS revision
                FROM orders
                WHERE admission_id = ?
                  AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                ORDER BY datetime(COALESCE(updated_at, datetime)) DESC, id DESC
                LIMIT 1
                """,
                (admission_id,),
            ).fetchone()
            if order:
                action_dt = _parse_dt(order["updated_at"]) or _parse_dt(order["datetime"]) or datetime.min
                candidates.append({"kind": "order", "row": order, "action_dt": action_dt, "id": int(order["id"] or 0)})

            event = cursor.execute(
                """
                SELECT id, operation_case_id, event_type, event_time, created_at, updated_at,
                       display_label, drug_label, parent_event_id, payload_json, COALESCE(revision, 0) AS revision
                FROM operblock_timeline_events
                WHERE operation_case_id = ?
                  AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                  AND NOT (
                    event_type = 'infusion_stop'
                    AND COALESCE(payload_json, '') LIKE '%auto_stopped_by%'
                  )
                ORDER BY datetime(COALESCE(created_at, updated_at, event_time)) DESC, id DESC
                LIMIT 1
                """,
                (int(operation_case_id),),
            ).fetchone()
            if event:
                action_dt = _parse_dt(event["created_at"]) or _parse_dt(event["updated_at"]) or _parse_dt(event["event_time"]) or datetime.min
                candidates.append({"kind": "timeline_event", "row": event, "action_dt": action_dt, "id": int(event["id"] or 0)})

            if not candidates:
                return {"kind": "none", "message": "Нет действий для отмены."}

            latest = max(candidates, key=lambda item: (item["action_dt"], item["id"]))
            kind = latest["kind"]
            row = latest["row"]
            if kind == "vital":
                cursor.execute("DELETE FROM vitals WHERE id = ?", (int(row["id"]),))
                return {"kind": "vital", "message": "Последние витальные показатели отменены."}
            if kind == "order":
                cursor.execute(
                    """
                    UPDATE orders
                    SET status = 'deleted',
                        last_modified_by = 'operblock',
                        updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now'),
                        revision = COALESCE(revision, 0) + 1
                    WHERE id = ?
                    """,
                    (int(row["id"]),),
                )
                return {"kind": "order", "message": "Последнее назначение отменено."}

            return self._undo_timeline_event(cursor, row)

        return dict(self.db.run_write_operation(operation, source="operblock_undo_last_action"))

    def _undo_timeline_event(self, cursor: sqlite3.Cursor, row: Any) -> dict[str, Any]:
        event_id = int(row["id"])
        event_type = str(row["event_type"] or "")
        label = str(row["display_label"] or row["drug_label"] or "событие").strip()
        payload = _parse_json_dict(row["payload_json"])
        stage_kind = operation_stage_kind_from_payload(payload)

        if event_type == "infusion_start":
            child = cursor.execute(
                """
                SELECT id
                FROM operblock_timeline_events
                WHERE parent_event_id = ?
                  AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                LIMIT 1
                """,
                (event_id,),
            ).fetchone()
            if child:
                raise ValueError("Нельзя отменить старт инфузии, пока у неё есть изменения или остановка. Сначала отмените последнее событие инфузии.")

        if event_type == "infusion_stop":
            parent_id = int(row["parent_event_id"] or 0)
            cursor.execute(
                """
                UPDATE operblock_timeline_events
                SET status = 'deleted',
                    revision = COALESCE(revision, 0) + 1,
                    last_modified_by = 'operblock',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                """,
                (event_id,),
            )
            if parent_id:
                cursor.execute(
                    """
                    UPDATE operblock_timeline_events
                    SET status = 'active',
                        end_time = NULL,
                        revision = COALESCE(revision, 0) + 1,
                        last_modified_by = 'operblock',
                        updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                    WHERE id = ?
                      AND event_type = 'infusion_start'
                    """,
                    (parent_id,),
                )
            return {"kind": "timeline_event", "message": f"Последнее событие «{label}» отменено."}

        if event_type == "clinical_event" and stage_kind == "anesthesia_end":
            event_time = _parse_dt(row["event_time"])
            if event_time is not None:
                auto_stops = cursor.execute(
                    """
                    SELECT id, parent_event_id
                    FROM operblock_timeline_events
                    WHERE event_type = 'infusion_stop'
                      AND event_time = ?
                      AND operation_case_id = ?
                      AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                      AND payload_json LIKE '%auto_stopped_by%'
                    """,
                    (_minute_floor(event_time).isoformat(timespec="seconds"), int(row["operation_case_id"] or 0)),
                ).fetchall()
                for stop in auto_stops:
                    parent_id = int(stop["parent_event_id"] or 0)
                    cursor.execute(
                        """
                        UPDATE operblock_timeline_events
                        SET status = 'deleted',
                            revision = COALESCE(revision, 0) + 1,
                            last_modified_by = 'operblock',
                            updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                        WHERE id = ?
                        """,
                        (int(stop["id"]),),
                    )
                    if parent_id:
                        cursor.execute(
                            """
                            UPDATE operblock_timeline_events
                            SET status = 'active',
                                end_time = NULL,
                                revision = COALESCE(revision, 0) + 1,
                                last_modified_by = 'operblock',
                                updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                            WHERE id = ?
                              AND event_type = 'infusion_start'
                            """,
                            (parent_id,),
                        )

        cursor.execute(
            """
            UPDATE operblock_timeline_events
            SET status = 'deleted',
                revision = COALESCE(revision, 0) + 1,
                last_modified_by = 'operblock',
                updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
            WHERE id = ?
            """,
            (event_id,),
        )
        return {"kind": "timeline_event", "message": f"Последнее событие «{label}» отменено."}

    def assert_vital_write_allowed(self, admission_id: int, timestamp: datetime) -> None:
        validate_operblock_runtime_path(self.db)

        def operation(cursor: sqlite3.Cursor):
            case = self._assert_active_operation_for_admission(cursor, admission_id)
            self._assert_datetime_in_operation_bounds(timestamp, case, entity_label="Время витальных функций")
            return None

        self.db.run_write_operation(operation, source="operblock_assert_vital_write_allowed")

    def _fetch_stage_rows_for_case(self, cursor: sqlite3.Cursor, operation_case_id: int) -> list[dict[str, Any]]:
        rows = cursor.execute(
            """
            SELECT
                id, operation_case_id, admission_id, table_code, event_type, event_time, end_time,
                drug_label, display_label, raw_text, status, COALESCE(revision, 0) AS revision,
                parent_event_id, payload_json, created_at, updated_at
            FROM operblock_timeline_events
            WHERE operation_case_id = ?
              AND event_type = 'clinical_event'
              AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
            ORDER BY datetime(event_time) ASC, id ASC
            """,
            (int(operation_case_id),),
        ).fetchall()
        return _stage_rows_from_timeline_rows([_row_to_dict(row) for row in rows])

    @staticmethod
    def _active_anesthesia_interval(stage_rows: list[dict[str, Any]]) -> tuple[datetime, datetime | None] | None:
        intervals = _build_stage_intervals(stage_rows).get("anesthesia_intervals") or []
        for interval in reversed(intervals):
            start = _parse_dt((interval or {}).get("start"))
            end = _parse_dt((interval or {}).get("end"))
            if start is not None and end is None:
                return _minute_floor(start), None
        return None

    @staticmethod
    def _all_anesthesia_intervals(stage_rows: list[dict[str, Any]]) -> list[tuple[datetime, datetime | None]]:
        result: list[tuple[datetime, datetime | None]] = []
        for interval in _build_stage_intervals(stage_rows).get("anesthesia_intervals") or []:
            start = _parse_dt((interval or {}).get("start"))
            if start is None:
                continue
            result.append((_minute_floor(start), _minute_floor(_parse_dt((interval or {}).get("end"))) if (interval or {}).get("end") else None))
        return result

    def _require_active_anesthesia_interval(
        self,
        cursor: sqlite3.Cursor,
        case: dict[str, Any],
        *,
        entity_label: str,
    ) -> tuple[datetime, datetime | None]:
        stage_rows = self._fetch_stage_rows_for_case(cursor, int(case["operation_case_id"]))
        interval = self._active_anesthesia_interval(stage_rows)
        if interval is None:
            raise ValueError(
                f"{entity_label}: сначала нажмите «Начать пособие». "
                "До начала анестезиологического пособия назначения недоступны."
            )
        return interval

    def _assert_datetime_in_active_anesthesia_bounds(
        self,
        cursor: sqlite3.Cursor,
        value: Any,
        case: dict[str, Any],
        *,
        entity_label: str,
    ) -> None:
        timestamp = _parse_dt(value)
        if timestamp is None:
            raise ValueError(f"{entity_label}: укажите корректное время.")
        self._assert_datetime_in_operation_bounds(timestamp, case, entity_label=entity_label)
        start, end = self._require_active_anesthesia_interval(cursor, case, entity_label=entity_label)
        timestamp_minute = _minute_floor(timestamp)
        if timestamp_minute < start:
            raise ValueError(
                f"{entity_label} не может быть раньше начала пособия: {_format_bound_time(start)}."
            )
        if end is not None and timestamp_minute > end:
            raise ValueError(
                f"{entity_label} не может быть позже окончания пособия: {_format_bound_time(end)}."
            )

    @staticmethod
    def _stage_state_from_cursor_rows(stage_rows: list[dict[str, Any]]) -> dict[str, Any]:
        return _build_stage_intervals(stage_rows)

    def _fetch_operblock_timeline_event_rows(
        self,
        admission_id: int,
        *,
        operation_case_id: int | None = None,
    ) -> list[dict[str, Any]]:
        if not self._operblock_timeline_events_table_exists():
            return []
        params: list[Any] = [int(admission_id)]
        case_clause = ""
        if operation_case_id:
            case_clause = "AND operation_case_id = ?"
            params.append(int(operation_case_id))
        rows = self.db.fetch_all_remcard(
            f"""
            SELECT
                id, operation_case_id, admission_id, table_code, event_type, event_time, end_time,
                drug_label, display_label, raw_text, dose_value, dose_unit, volume_ml,
                concentration_text, rate_value, rate_unit, route, status, COALESCE(revision, 0) AS revision,
                source_order_id, parent_event_id, payload_json, created_at, updated_at
            FROM operblock_timeline_events
            WHERE admission_id = ?
              {case_clause}
              AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
            ORDER BY datetime(event_time) ASC, id ASC
            """,
            tuple(params),
        )
        return [_row_to_dict(row) for row in rows]

    def _operblock_timeline_events_table_exists(self) -> bool:
        row = self.db.fetch_one_remcard(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operblock_timeline_events'"
        )
        return bool(row)

    @staticmethod
    def _normalize_timeline_event_datetime(value: Any) -> datetime:
        parsed = _parse_dt(value)
        if parsed is None:
            raise ValueError("Укажите корректное время события.")
        return _minute_floor(parsed)

    @staticmethod
    def _timeline_payload_json(payload: Optional[dict[str, Any]]) -> str | None:
        if not payload:
            return None
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)

    @staticmethod
    def _payload_is_gas(payload: Mapping[str, Any] | dict[str, Any] | None) -> bool:
        if not isinstance(payload, Mapping):
            return False
        return str(payload.get("kind") or "").strip().casefold() == "gas"

    @staticmethod
    def _text_is_oxygen(value: Any) -> bool:
        text = str(value or "").strip().casefold().replace("ё", "е")
        if not text:
            return False
        if "кислород" in text or re.search(r"(?<![0-9a-zа-я])oxygen(?![0-9a-zа-я])", text):
            return True
        return bool(re.search(r"(?<![0-9a-zа-я])(?:o|о)\s*2(?![0-9a-zа-я])", text))

    @classmethod
    def _payload_is_oxygen(cls, payload: Mapping[str, Any] | dict[str, Any] | None, *texts: Any) -> bool:
        data = payload if isinstance(payload, Mapping) else {}
        subtype = str(data.get("gas_subtype") or data.get("gas_kind") or data.get("subtype") or "").strip()
        if subtype and cls._text_is_oxygen(subtype):
            return True
        if isinstance(data.get("is_oxygen"), bool) and data.get("is_oxygen"):
            return True
        for key in (
            "preset_id",
            "source_drug_id",
            "label",
            "display_name",
            "latin",
            "drug_label",
            "display_label",
            "raw_text",
        ):
            if cls._text_is_oxygen(data.get(key)):
                return True
        return any(cls._text_is_oxygen(text) for text in texts)

    def _active_gas_start_for_case(
        self,
        cursor: sqlite3.Cursor,
        admission_id: int,
        operation_case_id: int,
        *,
        oxygen: bool | None = None,
    ):
        rows = cursor.execute(
            """
            SELECT
                id, operation_case_id, admission_id, table_code, event_time, drug_label,
                display_label, volume_ml, concentration_text, rate_value, rate_unit, route, status,
                payload_json, COALESCE(revision, 0) AS revision
            FROM operblock_timeline_events
            WHERE admission_id = ?
              AND operation_case_id = ?
              AND event_type = 'infusion_start'
              AND status = 'active'
            ORDER BY datetime(event_time) ASC, id ASC
            """,
            (int(admission_id), int(operation_case_id)),
        ).fetchall()
        for row in rows:
            payload = _parse_json_dict(row["payload_json"])
            if not self._payload_is_gas(payload):
                continue
            if oxygen is not None and self._payload_is_oxygen(payload, row["drug_label"], row["display_label"]) != bool(oxygen):
                continue
            return row
        return None

    def _assert_active_operation_for_case_admission(
        self,
        cursor: sqlite3.Cursor,
        admission_id: int,
        operation_case_id: int,
    ) -> dict[str, Any]:
        case = self._assert_active_operation_for_admission(cursor, admission_id)
        if int(case.get("operation_case_id") or 0) != int(operation_case_id):
            raise OperBlockConflictError("Операция изменена другим рабочим местом. Обновите протокол.")
        return case

    def _assert_active_operation_case_for_update(
        self,
        cursor: sqlite3.Cursor,
        operation_case_id: int,
    ) -> dict[str, Any]:
        row = cursor.execute(
            """
            SELECT
                oc.id AS operation_case_id,
                oc.patient_id,
                oc.admission_id,
                oc.table_code,
                oc.status AS case_status,
                oc.started_at,
                oc.ended_at,
                oc.anesthesia_protocol_number,
                oc.anesthesia_protocol_date,
                oc.transfer_department,
                a.department_profile,
                COALESCE(oc.revision, 0) AS revision
            FROM operation_cases oc
            JOIN admissions a ON a.id = oc.admission_id
            WHERE oc.id = ?
              AND oc.status = 'active'
            """,
            (int(operation_case_id),),
        ).fetchone()
        if not row:
            raise OperBlockConflictError("Активный случай в операционной не найден. Обновите список оперблока.")
        case = _row_to_dict(row)
        table_code = self._validate_table_code(str(case.get("table_code") or ""))
        assignment = cursor.execute(
            """
            SELECT id
            FROM operation_table_assignments
            WHERE operation_case_id = ?
              AND table_code = ?
              AND status = 'active'
              AND released_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(case["operation_case_id"]), table_code),
        ).fetchone()
        if not assignment:
            raise OperBlockConflictError("Операционный стол уже освобождён другим рабочим местом. Обновите протокол.")
        case["table_code"] = table_code
        return case

    @staticmethod
    def _assert_infusion_event_not_before_start(event_dt: datetime, start_row: Any) -> None:
        start_dt = _parse_dt(start_row["event_time"])
        if start_dt is None:
            raise OperBlockConflictError("Не удалось определить начало инфузии. Обновите протокол.")
        if _minute_floor(event_dt) < _minute_floor(start_dt):
            raise ValueError("Время события инфузии не может быть раньше старта инфузии.")

    @staticmethod
    def _assert_infusion_stop_not_before_latest_change(
        cursor: sqlite3.Cursor,
        start_event_id: int,
        event_dt: datetime,
    ) -> None:
        latest_change = cursor.execute(
            """
            SELECT event_time
            FROM operblock_timeline_events
            WHERE parent_event_id = ?
              AND event_type = 'infusion_change'
              AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
            ORDER BY datetime(event_time) DESC, id DESC
            LIMIT 1
            """,
            (int(start_event_id),),
        ).fetchone()
        latest_dt = _parse_dt(latest_change["event_time"]) if latest_change else None
        if latest_dt is not None and _minute_floor(event_dt) < _minute_floor(latest_dt):
            raise ValueError(
                "Время остановки инфузии не может быть раньше последнего изменения: "
                f"{_format_bound_time(_minute_floor(latest_dt))}."
            )

    def _get_infusion_start_for_update(
        self,
        cursor: sqlite3.Cursor,
        start_event_id: int,
        *,
        allowed_statuses: set[str],
    ):
        statuses = tuple(sorted(str(status) for status in allowed_statuses if str(status)))
        if not statuses:
            raise ValueError("Не указан допустимый статус инфузии.")
        placeholders = ", ".join("?" for _ in statuses)
        row = cursor.execute(
            f"""
            SELECT
                id, operation_case_id, admission_id, table_code, event_time, drug_label,
                volume_ml, concentration_text, rate_value, rate_unit, route, status,
                payload_json, COALESCE(revision, 0) AS revision
            FROM operblock_timeline_events
            WHERE id = ?
              AND event_type = 'infusion_start'
              AND status IN ({placeholders})
            """,
            (int(start_event_id), *statuses),
        ).fetchone()
        if not row:
            raise OperBlockConflictError("Инфузия не найдена или уже удалена. Обновите протокол.")
        return row

    def _get_active_infusion_start_for_update(self, cursor: sqlite3.Cursor, start_event_id: int):
        try:
            return self._get_infusion_start_for_update(cursor, start_event_id, allowed_statuses={"active"})
        except OperBlockConflictError as exc:
            raise OperBlockConflictError("Активная инфузия не найдена или уже остановлена. Обновите протокол.") from exc

    @staticmethod
    def _bump_infusion_start_revision(
        cursor: sqlite3.Cursor,
        start_event_id: int,
        expected_revision: int,
    ) -> None:
        cursor.execute(
            """
            UPDATE operblock_timeline_events
            SET revision = COALESCE(revision, 0) + 1,
                last_modified_by = 'operblock',
                updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
            WHERE id = ?
              AND event_type = 'infusion_start'
              AND status = 'active'
              AND COALESCE(revision, 0) = ?
            """,
            (int(start_event_id), int(expected_revision)),
        )
        if cursor.rowcount != 1:
            raise DataConflictError(DATA_CONFLICT_MESSAGE)

    def _operation_case_column_expr(self, column_name: str, *, alias: str | None = None) -> str:
        clean_column = re.sub(r"[^0-9A-Za-z_]+", "", str(column_name or ""))
        clean_alias = re.sub(r"[^0-9A-Za-z_]+", "", str(alias or clean_column))
        if not clean_column or not clean_alias:
            return "NULL"
        conn = getattr(self.db, "_remcard_conn", None)
        try:
            if conn is not None and clean_column in _sqlite_columns(conn, "operation_cases"):
                return f"oc.{clean_column}"
        except Exception:
            pass
        return f"NULL AS {clean_alias}"

    def _get_case_row(self, operation_case_id: int) -> dict[str, Any]:
        validate_operblock_runtime_path(self.db)
        planned_assistance_expr = self._operation_case_column_expr("planned_anesthesia_assistance_type")
        row = self.db.fetch_one_remcard(
            f"""
            SELECT
                oc.id AS operation_case_id,
                oc.patient_id,
                oc.admission_id,
                oc.table_code,
                oc.status AS case_status,
                oc.started_at,
                oc.ended_at,
                t.display_name AS table_display_name,
                p.full_name,
                p.birth_date,
                a.history_number,
                a.patient_gender,
                a.patient_age,
                a.patient_months,
                a.patient_age_unit,
                a.diagnosis_code,
                a.diagnosis_text,
                oc.planned_operation_name,
                {planned_assistance_expr},
                oc.planned_surgeons_json,
                oc.planned_operating_nurse,
                oc.planned_anesthesiologist,
                oc.planned_anesthetist,
                oc.height_cm,
                oc.weight_kg,
                oc.allergies,
                oc.blood_group,
                oc.blood_rh,
                oc.preop_sys,
                oc.preop_dia,
                oc.preop_pulse,
                oc.preop_spo2,
                COALESCE(oc.preop_save_initial_vitals, 1) AS preop_save_initial_vitals,
                oc.anesthesia_protocol_number,
                oc.anesthesia_protocol_date,
                oc.transfer_department,
                a.department_profile
            FROM operation_cases oc
            JOIN operating_tables t ON t.code = oc.table_code
            JOIN admissions a ON a.id = oc.admission_id
            JOIN patients p ON p.id = oc.patient_id
            WHERE oc.id = ?
            """,
            (int(operation_case_id),),
        )
        if not row:
            raise OperBlockConflictError("Операция не найдена.")
        return _row_to_dict(row)

    def get_latest_vital_values(self, admission_id: int) -> dict[str, Any]:
        row = self.db.fetch_one_remcard(
            """
            SELECT sys, dia, pulse, spo2, datetime
            FROM vitals
            WHERE admission_id = ?
              AND (sys IS NOT NULL OR dia IS NOT NULL OR pulse IS NOT NULL OR spo2 IS NOT NULL)
            ORDER BY CAST(STRFTIME('%s', datetime) AS INTEGER) DESC, id DESC
            LIMIT 1
            """,
            (int(admission_id),),
        )
        data = _row_to_dict(row)
        return {
            "ad": self._format_ad(data.get("sys"), data.get("dia")),
            "pulse": data.get("pulse"),
            "spo2": data.get("spo2"),
            "datetime": data.get("datetime"),
        }

    def _get_latest_case_row_for_admission(self, admission_id: int) -> dict[str, Any] | None:
        validate_operblock_runtime_path(self.db)
        row = self.db.fetch_one_remcard(
            """
            SELECT
                oc.id AS operation_case_id,
                oc.patient_id,
                oc.admission_id,
                oc.table_code,
                oc.status AS case_status,
                oc.started_at,
                oc.ended_at,
                t.display_name AS table_display_name
            FROM operation_cases oc
            JOIN operating_tables t ON t.code = oc.table_code
            WHERE oc.admission_id = ?
            ORDER BY CASE WHEN oc.status = 'active' THEN 0 ELSE 1 END, oc.id DESC
            LIMIT 1
            """,
            (int(admission_id),),
        )
        return _row_to_dict(row) if row else None

    @staticmethod
    def _format_ad(sys_value: Any, dia_value: Any) -> str:
        if sys_value is None and dia_value is None:
            return ""
        left = "" if sys_value is None else str(sys_value)
        right = "" if dia_value is None else str(dia_value)
        return f"{left}/{right}".strip("/")

    @staticmethod
    def _validate_table_code(table_code: str) -> str:
        code = str(table_code or "").strip().lower()
        allowed = {table["code"] for table in OPERBLOCK_TABLES}
        if code not in allowed:
            raise ValueError("Неизвестный операционный стол.")
        return code

    def _assert_active_operation_for_admission(
        self,
        cursor: sqlite3.Cursor,
        admission_id: int,
    ) -> dict[str, Any]:
        row = cursor.execute(
            """
            SELECT
                id AS operation_case_id,
                admission_id,
                table_code,
                status AS case_status,
                started_at,
                ended_at,
                COALESCE(revision, 0) AS revision
            FROM operation_cases
            WHERE admission_id = ?
              AND status = 'active'
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(admission_id),),
        ).fetchone()
        if not row:
            latest = cursor.execute(
                """
                SELECT status, ended_at
                FROM operation_cases
                WHERE admission_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(admission_id),),
            ).fetchone()
            if latest and str(latest["status"] or "") == "closed":
                raise OperBlockConflictError("Случай в операционной уже закрыт другим рабочим местом. Обновите протокол.")
            raise OperBlockConflictError("Активная операция для пациента не найдена. Обновите список оперблока.")

        case = _row_to_dict(row)
        table_code = self._validate_table_code(str(case.get("table_code") or ""))
        assignment = cursor.execute(
            """
            SELECT id
            FROM operation_table_assignments
            WHERE operation_case_id = ?
              AND table_code = ?
              AND status = 'active'
              AND released_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(case["operation_case_id"]), table_code),
        ).fetchone()
        if not assignment:
            raise OperBlockConflictError("Операционный стол уже освобождён другим рабочим местом. Обновите протокол.")
        case["table_code"] = table_code
        return case

    def _assert_datetime_in_operation_bounds(
        self,
        value: Any,
        case: dict[str, Any],
        *,
        entity_label: str,
    ) -> None:
        timestamp = _parse_dt(value)
        if timestamp is None:
            raise ValueError(f"{entity_label}: укажите корректное время.")
        started_at = _parse_dt(case.get("started_at"))
        ended_at = _parse_dt(case.get("ended_at"))
        if started_at is None:
            raise OperBlockConflictError("У операции не задано время начала. Обновите протокол.")
        timestamp_minute = _minute_floor(timestamp)
        start_minute = _minute_floor(started_at)
        if timestamp_minute < start_minute:
            raise ValueError(
                f"{entity_label} не может быть раньше поступления пациента в операционную: {_format_bound_time(start_minute)}."
            )
        if ended_at is not None:
            end_minute = _minute_floor(ended_at)
            if timestamp_minute > end_minute:
                raise ValueError(
                    f"{entity_label} не может быть позже закрытия случая в операционной: {_format_bound_time(end_minute)}."
                )

