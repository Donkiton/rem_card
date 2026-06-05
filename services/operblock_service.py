from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
import hashlib
import json
import os
import re
import socket
import sqlite3
from typing import Any, Mapping, Optional
import uuid

from rem_card.app import operblock_startup_metrics
from rem_card.app.patient_age import (
    format_patient_age,
    format_patient_age_from_birth_date,
    parse_date_value,
    storage_age_from_birth_date,
)
from rem_card.data.dto.remcard_dto import VitalDTO
from rem_card.services.concurrency import DATA_CONFLICT_MESSAGE, DataConflictError, assert_revision_matches
from rem_card.services.operblock_medication_presets import (
    load_operblock_medication_presets,
    operblock_medication_preset_display_name,
)
from rem_card.services.operblock_anesthesia_types import normalize_operblock_anesthesia_type_label
from rem_card.services.operblock_timeline import (
    OPERBLOCK_STAGE_KIND_LABELS,
    OperBlockTimelineSnapshot,
    build_timeline_snapshot_from_legacy_orders,
    operation_stage_kind_from_payload,
    with_timeline_content_hash,
)


OPERBLOCK_ROLE = "operblock"
OPERBLOCK_TABLES = (
    {"code": "emergency", "display_name": "Экстренная операционная", "sort_order": 1},
    {"code": "planned", "display_name": "Плановая операционная", "sort_order": 2},
)
OPERBLOCK_MKB_CODE_RE = re.compile(r"^[A-Z]\d{2}(?:\.\d{1,2})?$")
OPERBLOCK_ORDER_ROUTE_TAG_RE = re.compile(r"\[OB_ROUTE:(?P<route>iv|im)\]", flags=re.IGNORECASE)
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
    code = str(value or "").strip().casefold()
    if code in {"im", "вм", "в/м", "intramuscular", "внутримышечно", "в/мышечно"}:
        return "im"
    return "iv"


def _strip_operblock_order_route_tag(comment: str) -> str:
    return re.sub(r"\s+", " ", OPERBLOCK_ORDER_ROUTE_TAG_RE.sub("", str(comment or ""))).strip()


def _operblock_order_comment_with_route(comment: str, route: str | None) -> str:
    clean_comment = _strip_operblock_order_route_tag(comment)
    route_code = normalize_operblock_order_route(route)
    if route_code == "im":
        return f"{clean_comment} [OB_ROUTE:im]".strip()
    return clean_comment


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
        row["stage_label"] = _stage_label(kind)
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


def _age_text(row: dict[str, Any], reference: Optional[datetime] = None) -> str:
    birth_date = parse_date_value(row.get("birth_date"))
    if birth_date:
        text = format_patient_age_from_birth_date(birth_date, reference)
        if text:
            return text
    return format_patient_age(row.get("patient_age"), row.get("patient_age_unit"), row.get("patient_months"))


def _row_to_dict(row) -> dict[str, Any]:
    return dict(row) if row is not None else {}


class OperBlockService:
    def __init__(self, db_manager):
        self.db = db_manager
        self.client_id = f"{socket.gethostname()}:{os.getpid()}"

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
                p.full_name,
                p.birth_date,
                a.history_number,
                a.patient_gender,
                a.patient_age,
                a.patient_months,
                a.patient_age_unit,
                a.diagnosis_code,
                a.diagnosis_text,
                (
                    SELECT v.sys FROM vitals v
                    WHERE v.admission_id = oc.admission_id
                    ORDER BY v.datetime DESC, v.id DESC
                    LIMIT 1
                ) AS latest_sys,
                (
                    SELECT v.dia FROM vitals v
                    WHERE v.admission_id = oc.admission_id
                    ORDER BY v.datetime DESC, v.id DESC
                    LIMIT 1
                ) AS latest_dia,
                (
                    SELECT v.pulse FROM vitals v
                    WHERE v.admission_id = oc.admission_id
                    ORDER BY v.datetime DESC, v.id DESC
                    LIMIT 1
                ) AS latest_pulse,
                (
                    SELECT v.spo2 FROM vitals v
                    WHERE v.admission_id = oc.admission_id
                    ORDER BY v.datetime DESC, v.id DESC
                    LIMIT 1
                ) AS latest_spo2
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
                    "status_text": "В операционной",
                    "started_at": row.get("started_at"),
                    "revision": int(row.get("case_revision") or 0),
                    "latest": {
                        "ad": self._format_ad(row.get("latest_sys"), row.get("latest_dia")),
                        "pulse": row.get("latest_pulse"),
                        "spo2": row.get("latest_spo2"),
                    },
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

    def list_archived_operation_cases(self) -> list[dict[str, Any]]:
        validate_operblock_runtime_path(self.db)
        rows = self.db.fetch_all_remcard(
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
                a.patient_age,
                a.patient_months,
                a.patient_age_unit,
                a.diagnosis_code,
                a.diagnosis_text
            FROM operation_cases oc
            JOIN operating_tables t ON t.code = oc.table_code
            JOIN admissions a ON a.id = oc.admission_id
            JOIN patients p ON p.id = oc.patient_id
            WHERE oc.status = 'closed'
              AND COALESCE(a.unit_scope, '') = 'operblock'
            ORDER BY datetime(oc.ended_at) DESC, oc.id DESC
            LIMIT 500
            """
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            data = _row_to_dict(row)
            result.append(
                {
                    "operation_case_id": int(data.get("operation_case_id") or 0),
                    "admission_id": int(data.get("admission_id") or 0),
                    "patient_id": int(data.get("patient_id") or 0),
                    "table_code": data.get("table_code"),
                    "table_display_name": data.get("table_display_name"),
                    "history_number": data.get("history_number") or "",
                    "full_name": data.get("full_name") or "Неизвестно",
                    "age": _age_text(data),
                    "gender": data.get("patient_gender") or "",
                    "diagnosis_code": data.get("diagnosis_code") or "",
                    "diagnosis_text": data.get("diagnosis_text") or "",
                    "started_at": data.get("started_at"),
                    "ended_at": data.get("ended_at"),
                    "status": data.get("case_status") or "closed",
                }
            )
        return result

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

    def delete_archived_operation_case(self, operation_case_id: int) -> dict[str, int]:
        validate_operblock_runtime_path(self.db)

        def operation(cursor: sqlite3.Cursor):
            case = cursor.execute(
                """
                SELECT oc.id, oc.admission_id, oc.status
                FROM operation_cases oc
                JOIN admissions a ON a.id = oc.admission_id
                WHERE oc.id = ?
                  AND COALESCE(a.unit_scope, '') = 'operblock'
                """,
                (int(operation_case_id),),
            ).fetchone()
            if not case:
                raise OperBlockConflictError("Архивный случай не найден.")
            if str(case["status"] or "") != "closed":
                raise OperBlockConflictError("Удалить можно только пациента из архива.")
            cursor.execute(
                """
                UPDATE operation_cases
                SET status = 'cancelled',
                    last_modified_by = 'operblock',
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ?
                  AND status = 'closed'
                """,
                (int(operation_case_id),),
            )
            if cursor.rowcount != 1:
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
                SELECT oc.id
                FROM operation_cases oc
                JOIN admissions a ON a.id = oc.admission_id
                WHERE oc.status = 'closed'
                  AND COALESCE(a.unit_scope, '') = 'operblock'
                  {table_clause}
                """,
                tuple(params),
            ).fetchall()
            case_ids = [int(row["id"]) for row in rows]
            if not case_ids:
                return {"deleted": 0}
            placeholders = ", ".join("?" for _ in case_ids)
            cursor.execute(
                f"""
                UPDATE operation_cases
                SET status = 'cancelled',
                    last_modified_by = 'operblock',
                    revision = COALESCE(revision, 0) + 1
                WHERE status = 'closed'
                  AND id IN ({placeholders})
                """,
                tuple(case_ids),
            )
            return {"deleted": int(cursor.rowcount or 0)}

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
            "started_at": case["started_at"],
            "ended_at": case["ended_at"],
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
        return state

    def build_operblock_vitals_snapshot(self, admission_id: int) -> dict[str, Any]:
        rows = self.db.fetch_all_remcard(
            """
            SELECT id, admission_id, datetime, sys, dia, pulse, spo2, COALESCE(revision, 0) AS revision
            FROM vitals
            WHERE admission_id = ?
            ORDER BY datetime DESC, id DESC
            LIMIT 50
            """,
            (int(admission_id),),
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
        vitals = self.build_operblock_vitals_snapshot(admission_id)
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

    def create_operation_case(self, data: OperBlockPatientInput | dict[str, Any]) -> dict[str, int]:
        validate_operblock_runtime_path(self.db)
        if not isinstance(data, OperBlockPatientInput):
            data = OperBlockPatientInput(
                table_code=str(data.get("table_code") or ""),
                history_number=str(data.get("history_number") or ""),
                full_name=str(data.get("full_name") or ""),
                gender=str(data.get("gender") or ""),
                birth_date=_to_birth_date(data.get("birth_date")),
                diagnosis_code=data.get("diagnosis_code"),
                diagnosis_text=str(data.get("diagnosis_text") or ""),
            )

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

        birth_date = _to_birth_date(data.birth_date)
        now = _now_text()
        age = storage_age_from_birth_date(birth_date, datetime.now())
        last_name, first_name, middle_name = _split_name(full_name)
        admission_uid = str(uuid.uuid4())
        bed_number = 0

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
                    now,
                    age["patient_age"],
                    age["patient_months"],
                    age["patient_age_unit"],
                    data.gender,
                    diagnosis_code or None,
                    diagnosis_text,
                    "Оперблок",
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
                    created_by_role, created_by_client_id, last_modified_by
                ) VALUES (?, ?, ?, 'active', ?, ?, 'operblock', ?, 'operblock')
                """,
                (patient_id, admission_id, table_code, now, now, self.client_id),
            )
            operation_case_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO operation_table_assignments (
                    operation_case_id, table_code, assigned_at, status,
                    created_by_role, created_by_client_id, last_modified_by
                ) VALUES (?, ?, ?, 'active', 'operblock', ?, 'operblock')
                """,
                (operation_case_id, table_code, now, self.client_id),
            )
            cursor.execute(
                """
                INSERT INTO patient_status_events (
                    admission_id, status, reason_type, reason_text, start_time,
                    created_by, last_modified_by
                ) VALUES (?, 'OR', 'operblock', 'В операционной', ?, 'operblock', 'operblock')
                """,
                (admission_id, now),
            )
            return {
                "patient_id": patient_id,
                "admission_id": admission_id,
                "operation_case_id": operation_case_id,
            }

        try:
            return dict(self.db.run_write_operation(operation, source="operblock_create_operation_case"))
        except sqlite3.IntegrityError as exc:
            raise OperBlockConflictError("Операционный стол уже занят другим пользователем.") from exc

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
    ) -> int:
        return self._add_stage_event(
            operation_case_id,
            "anesthesia_start",
            assistance_type=assistance_type,
            anesthesiologist=anesthesiologist,
            anesthetist=anesthetist,
        )

    def end_anesthesia(self, operation_case_id: int) -> int:
        return self._add_stage_event(operation_case_id, "anesthesia_end")

    def start_surgery(
        self,
        operation_case_id: int,
        *,
        operation_name: str | None = None,
        surgeons: list[str] | None = None,
        surgeon: str | None = None,
        operating_nurse: str | None = None,
    ) -> int:
        return self._add_stage_event(
            operation_case_id,
            "surgery_start",
            operation_name=operation_name,
            surgeons=surgeons,
            surgeon=surgeon,
            operating_nurse=operating_nurse,
        )

    def end_surgery(self, operation_case_id: int) -> int:
        return self._add_stage_event(operation_case_id, "surgery_end")

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
    ) -> int:
        validate_operblock_runtime_path(self.db)
        clean_kind = str(stage_kind or "").strip()
        if clean_kind not in OPERBLOCK_STAGE_KIND_LABELS:
            raise ValueError("Неизвестный этап операции.")
        event_dt = _minute_floor(datetime.now())
        clean_assistance_type = normalize_operblock_anesthesia_type_label(assistance_type)
        clean_anesthesiologist = re.sub(r"\s+", " ", str(anesthesiologist or "").strip())
        clean_anesthetist = re.sub(r"\s+", " ", str(anesthetist or "").strip())
        clean_operation_name = re.sub(r"\s+", " ", str(operation_name or "").strip())
        clean_surgeons = _normalize_stage_text_list(surgeons)
        clean_surgeon = re.sub(r"\s+", " ", str(surgeon or "").strip())
        if clean_surgeon and clean_surgeon.casefold() not in {item.casefold() for item in clean_surgeons}:
            clean_surgeons.append(clean_surgeon)
        clean_operating_nurse = re.sub(r"\s+", " ", str(operating_nurse or "").strip())

        def operation(cursor: sqlite3.Cursor):
            case = self._assert_active_operation_case_for_update(cursor, operation_case_id)
            self._assert_datetime_in_operation_bounds(event_dt, case, entity_label=_stage_label(clean_kind))
            stage_rows = self._fetch_stage_rows_for_case(cursor, int(operation_case_id))
            state = self._stage_state_from_cursor_rows(stage_rows)
            anesthesia_active = bool(state.get("anesthesia_active"))
            surgery_active = bool(state.get("surgery_active"))

            if clean_kind == "anesthesia_start":
                if anesthesia_active:
                    raise ValueError("Анестезиологическое пособие уже начато.")
            elif clean_kind == "anesthesia_end":
                if not anesthesia_active:
                    raise ValueError("Анестезиологическое пособие ещё не начато.")
                if surgery_active:
                    raise ValueError("Перед окончанием пособия завершите операцию.")
                active_start = _parse_dt(state.get("current_anesthesia_start"))
                if active_start is None:
                    raise OperBlockConflictError("Не удалось определить начало пособия. Обновите протокол.")
                self._validate_medications_before_anesthesia_end(
                    cursor,
                    case,
                    _minute_floor(active_start),
                    event_dt,
                )
                self._auto_stop_open_infusions(cursor, case, event_dt)
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

            return self._insert_stage_event(
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
            )

        return int(self.db.run_write_operation(operation, source=f"operblock_stage_{clean_kind}"))

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
    ) -> int:
        label = _stage_label(stage_kind)
        payload = {"stage_kind": stage_kind, "label": label}
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
        return int(cursor.lastrowid)

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

    def add_order(self, admission_id: int, text: str, *, preset_payload: Optional[dict[str, Any]] = None) -> int:
        validate_operblock_runtime_path(self.db)
        clean_text = str(text or "").strip()
        if not clean_text:
            raise ValueError("Текст назначения не заполнен.")
        preset_id = ""
        if isinstance(preset_payload, dict):
            preset_id = str(preset_payload.get("preset_id") or "").strip()
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
                ) VALUES (?, ?, ?, ?, ?, 'observation', 'active', 0, '', 1, '[]', ?, ?, 1, 1, ?, '', 'operblock',
                          STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
                """,
                (int(admission_id), now, clean_text, preset_id or None, clean_text, sort_order, sort_order, now),
            )
            return int(cursor.lastrowid)

        return int(self.db.run_write_operation(operation, source="operblock_add_order"))

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
    ) -> int:
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
            if is_gas_infusion and self._active_gas_start_for_case(cursor, admission_id, operation_case_id):
                raise OperBlockConflictError("Газ уже идет. Измените дозу активного газа, второй газ запустить нельзя.")
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
            return int(cursor.lastrowid)

        return int(self.db.run_write_operation(operation, source="operblock_start_infusion"))

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
                raise ValueError("Для газа укажите дозу в MAC. Скорость в мл/час для газа не применяется.")
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
            raise ValueError("Укажите дозу газа.")
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
                raise ValueError("Для дозатора изменяется скорость, для газа - доза в MAC.")
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
                SELECT id, operation_case_id, event_type, event_time, updated_at, display_label, drug_label,
                       parent_event_id, payload_json, COALESCE(revision, 0) AS revision
                FROM operblock_timeline_events
                WHERE operation_case_id = ?
                  AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                ORDER BY datetime(COALESCE(updated_at, event_time)) DESC, id DESC
                LIMIT 1
                """,
                (int(operation_case_id),),
            ).fetchone()
            if event:
                action_dt = _parse_dt(event["updated_at"]) or _parse_dt(event["event_time"]) or datetime.min
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

    def _active_gas_start_for_case(
        self,
        cursor: sqlite3.Cursor,
        admission_id: int,
        operation_case_id: int,
    ):
        rows = cursor.execute(
            """
            SELECT
                id, operation_case_id, admission_id, table_code, event_time, drug_label,
                volume_ml, concentration_text, rate_value, rate_unit, route, status,
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
            if self._payload_is_gas(_parse_json_dict(row["payload_json"])):
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
                id AS operation_case_id,
                admission_id,
                table_code,
                status AS case_status,
                started_at,
                ended_at,
                COALESCE(revision, 0) AS revision
            FROM operation_cases
            WHERE id = ?
              AND status = 'active'
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

    def _get_case_row(self, operation_case_id: int) -> dict[str, Any]:
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
                t.display_name AS table_display_name,
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
            ORDER BY datetime DESC, id DESC
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

