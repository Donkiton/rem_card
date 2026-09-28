from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
import re
import sqlite3
from typing import Any, Mapping, Optional

from rem_card.app.patient_age import (
    format_patient_age,
    format_patient_age_from_birth_date,
    parse_date_value,
)
from rem_card.services.operblock_route_settings import (
    operblock_comment_with_route,
)
from rem_card.services.operblock_anesthesia_types import normalize_operblock_anesthesia_type_label
from rem_card.services.operblock_timeline import (
    OPERBLOCK_STAGE_KIND_LABELS,
    operation_stage_kind_from_payload,
)
from rem_card.services.patient_departments import normalize_profile_department
from rem_card.services.patient_departments import PROFILE_DEPARTMENTS


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
OPERBLOCK_REPORT_READ_RETRIES = 3
OPERBLOCK_REPORT_READ_RETRY_DELAY_SEC = 0.25
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


class OperBlockSourceMovementChangedError(OperBlockConflictError):
    """The linked RAO movement changed before the operation table was released."""


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
    handoff_id: Optional[int] = None
    source_rao_admission_id: Optional[int] = None


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


def _is_sqlite_locked_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "database is locked" in message
        or "database table is locked" in message
        or "database schema is locked" in message
    )


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


def _operation_stage_window_is_active(state: Mapping[str, Any] | dict[str, Any]) -> bool:
    return bool((state or {}).get("anesthesia_active"))


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
        handoff_id=_normalize_optional_int(payload.get("handoff_id"), "Связь с РАО", 1, 2_147_483_647),
        source_rao_admission_id=_normalize_optional_int(
            payload.get("source_rao_admission_id"),
            "Исходная карта РАО",
            1,
            2_147_483_647,
        ),
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


