from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re

from PySide6.QtGui import (
    QColor,
)
from PySide6.QtWidgets import (
    QLineEdit,
)

from rem_card.services.operblock_service import (
    OPERBLOCK_TABLES,
)
from rem_card.services.operblock_timeline import (
    format_operblock_medication_display_label,
)
from rem_card.services.operblock_route_settings import (
    normalize_operblock_route_code,
    operblock_comment_with_route,
    operblock_route_from_comment,
    operblock_route_label,
)

from rem_card.ui.operblock_view.operblock_visual_primitives import (
    OPERBLOCK_INFUSION_RATE_UNIT,
    OPERBLOCK_DEFAULT_INFUSION_RATES,
    OPERBLOCK_ORDERS_TEXT,
    OPERBLOCK_ORDER_ROUTE_DEFAULT,
)

def _line_edit() -> QLineEdit:
    edit = QLineEdit()
    edit.setFixedHeight(34)
    return edit


def _format_dt(value) -> str:
    text = str(value or "").replace("T", " ")
    if "." in text:
        text = text.split(".", 1)[0]
    return text[:16] if text else "-"


def _parse_datetime_value(value) -> datetime | None:
    text = str(value or "").strip().replace("T", " ")
    if not text:
        return None
    if "." in text:
        text = text.split(".", 1)[0]
    candidates = (text[:19], text[:16], text)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        for candidate in candidates:
            try:
                return datetime.strptime(candidate, fmt)
            except Exception:
                continue
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _minute_floor_dt(value: datetime | None) -> datetime | None:
    return value.replace(second=0, microsecond=0) if isinstance(value, datetime) else None


def _operblock_time_minutes_from_text(value: str) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    colon_match = re.fullmatch(r"\s*(\d{1,2})\s*:\s*(\d{0,2})\s*", raw)
    if colon_match:
        hour = int(colon_match.group(1))
        minute = int(colon_match.group(2) or "0")
    else:
        digits = re.sub(r"\D", "", raw)
        if not digits:
            return None
        if len(digits) <= 2:
            hour = int(digits)
            minute = 0
        elif len(digits) == 3:
            hour = int(digits[:1])
            minute = int(digits[1:])
        else:
            hour = int(digits[:2])
            minute = int(digits[2:4])
    if hour == 24 and minute == 0:
        return 0
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour * 60 + minute
    return None


def _operblock_format_time_edit_text(value: str) -> str:
    raw = str(value or "")
    digits = re.sub(r"\D", "", raw)[:4]
    if not digits:
        return ""
    if ":" in raw:
        parts = raw.split(":", 1)
        hour_digits = re.sub(r"\D", "", parts[0])[:2]
        minute_digits = re.sub(r"\D", "", parts[1])[:2]
        return f"{hour_digits}:{minute_digits}" if minute_digits else f"{hour_digits}:"
    if len(digits) == 1:
        if int(digits) > 2:
            return f"0{digits}:"
        return digits
    if len(digits) == 2:
        if int(digits) <= 23:
            return f"{digits}:"
        return f"0{digits[0]}:{digits[1]}"
    if len(digits) == 3:
        if int(digits[:2]) <= 23:
            return f"{digits[:2]}:{digits[2]}"
        return f"0{digits[0]}:{digits[1:]}"
    return f"{digits[:2]}:{digits[2:4]}"


def _operblock_time_text_from_minutes(minutes: int) -> str:
    normalized = int(minutes) % (24 * 60)
    return f"{normalized // 60:02d}:{normalized % 60:02d}"


def _format_protocol_started_at(value) -> str:
    parsed = _parse_datetime_value(value)
    return parsed.strftime("%d.%m.%Y %H:%M") if parsed else "-"


def _format_main_remcard_status_text(started_at, *, active: bool = True) -> tuple[str, str]:
    parsed = _parse_datetime_value(started_at)
    time_str = parsed.strftime("%H:%M") if parsed else "--:--"
    if active:
        return f"🔴 Опер. {time_str}", "#e74c3c"
    return f"⚫ Закрыт {time_str}", "#968c8c"


def normalize_operblock_birth_date_text(value: str, *, final: bool = True) -> str:
    text = str(value or "").strip().replace(",", ".").replace("/", ".").replace("\\", ".")
    if not text:
        return ""

    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return ""
    day = digits[:2]
    rest = digits[2:]
    if len(digits) <= 2:
        return day
    if not rest:
        return day

    month_len = 1
    if len(rest) >= 2:
        two_digit_month = int(rest[:2])
        month_len = 2 if 1 <= two_digit_month <= 12 else 1
    month = rest[:month_len]
    year = rest[month_len:]

    if final and len(month) == 1:
        month = month.zfill(2)
    if final and len(year) in (1, 2):
        year_num = int(year)
        pivot = datetime.now().year % 100
        century = 1900 if year_num > pivot else 2000
        year = f"{century + year_num:04d}"

    result = f"{day}.{month}"
    if year:
        result = f"{result}.{year[:4]}"
    return result[:10]


def _safe_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None


OPERBLOCK_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _normalize_operblock_card_color(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if OPERBLOCK_HEX_COLOR_RE.fullmatch(text):
        return text.lower()
    return None


def _contrast_text_color(hex_color: str | None, *, default: str = OPERBLOCK_ORDERS_TEXT) -> str:
    color = _normalize_operblock_card_color(hex_color)
    if not color:
        return default
    qcolor = QColor(color)
    return "#000000" if qcolor.lightness() > 150 else "#FFFFFF"


def _operblock_preset_card_color(preset: dict) -> str | None:
    source = preset or {}
    for key in ("card_color", "card_color_hex", "color"):
        if key in source:
            return _normalize_operblock_card_color(source.get(key))
    return None


def _stable_ui_hash(payload) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalize_operblock_table_filter(table_code: str | None) -> str | None:
    code = str(table_code or "").strip().lower()
    if not code:
        return None
    allowed = {str(table["code"]) for table in OPERBLOCK_TABLES}
    if code not in allowed:
        raise ValueError("Неизвестная операционная.")
    return code


def _operblock_table_display_name(table_code: str | None) -> str:
    code = str(table_code or "").strip().lower()
    for table in OPERBLOCK_TABLES:
        if str(table.get("code") or "") == code:
            return str(table.get("display_name") or "")
    return ""


_IMPLICIT_DOSE_UNIT_PATTERN = (
    r"(?:мкгр|мкг|мг|мл|гр|г|ед|ме|ME|IU|mcg|mkg|mg|ml|ug|g|ed)"
)
_IMPLICIT_DOSE_COMPONENT_PATTERN = (
    rf"\d+(?:[.,]\d+)?\s*{_IMPLICIT_DOSE_UNIT_PATTERN}(?![A-Za-zА-Яа-яЁёµ/])"
)
_IMPLICIT_TRAILING_DOSE_RE = re.compile(
    rf"(?P<dose>{_IMPLICIT_DOSE_COMPONENT_PATTERN}"
    rf"(?:\s*(?:[,;+]|и|\s+)\s*{_IMPLICIT_DOSE_COMPONENT_PATTERN})*"
    rf"(?:\s*\([^)]*\))*\s*)$",
    flags=re.IGNORECASE,
)


def _split_order_drug_and_dose(text: str) -> tuple[str, str]:
    clean = re.sub(r"\s+", " ", str(text or "").strip())
    if " - " in clean:
        drug, dose = clean.split(" - ", 1)
        return drug.strip() or "Без названия", dose.strip()

    match = _IMPLICIT_TRAILING_DOSE_RE.search(clean)
    if match:
        drug = clean[: match.start()].strip()
        if drug and not re.search(r"\d+\s*[-–—]\s*$", drug):
            drug = re.sub(r"\s*[:;,–—-]\s*$", "", drug).strip()
            if drug:
                return drug, match.group("dose").strip()
    return clean or "Без названия", ""


def _build_order_text_for_display(drug_name: str, dose_text: str) -> str:
    drug = str(drug_name or "").strip()
    dose = str(dose_text or "").strip()
    return f"{drug} {dose}".strip() if dose else drug


_BOLUS_MASS_DOSE_RE = re.compile(
    r"^(?P<value>\d+(?:[.,]\d+)?)(?:\s*(?P<unit>мкгр|мкг|mcg|mkg|ug|мг|mg|гр|г|g))?$",
    flags=re.IGNORECASE,
)


def _normalize_bolus_dose_text(value: str) -> str:
    clean = re.sub(r"\s+", " ", str(value or "").strip())
    if not clean:
        return ""

    single_match = _BOLUS_MASS_DOSE_RE.fullmatch(clean)
    if single_match:
        try:
            amount = Decimal(single_match.group("value").replace(",", "."))
        except (InvalidOperation, AttributeError):
            return clean
        _unit_key, unit_label = _normalize_dose_unit(single_match.group("unit") or "мг")
        return f"{_format_decimal_ru(amount)} {unit_label}"

    def normalize_component(match: re.Match) -> str:
        try:
            amount = Decimal(match.group("value").replace(",", "."))
        except (InvalidOperation, AttributeError):
            return match.group(0)
        _unit_key, unit_label = _normalize_dose_unit(match.group("unit"))
        return f"{_format_decimal_ru(amount)} {unit_label}"

    normalized = _DOSE_COMPONENT_RE.sub(normalize_component, clean)
    return normalized if _order_dose_components(normalized) else clean


def _normalize_order_route_code(value) -> str:
    return normalize_operblock_route_code(value)


def _order_route_code_from_comment(comment: str) -> str:
    return operblock_route_from_comment(comment) or OPERBLOCK_ORDER_ROUTE_DEFAULT


def _order_comment_with_route(comment: str, route_code: str) -> str:
    return operblock_comment_with_route(comment, route_code)


def _order_route_code(row: dict) -> str:
    route = str((row or {}).get("route") or "").strip()
    if route:
        return _normalize_order_route_code(route)
    payload = (row or {}).get("payload")
    if isinstance(payload, dict) and payload.get("route"):
        return _normalize_order_route_code(payload.get("route"))
    return _order_route_code_from_comment(str((row or {}).get("comment") or ""))


def _stored_order_route_value(route_code: str | None) -> str | None:
    normalized = _normalize_order_route_code(route_code)
    return None if normalized == OPERBLOCK_ORDER_ROUTE_DEFAULT else normalized


def _order_route_suffix(row: dict, *, short: bool = False) -> str:
    route_code = _order_route_code(row)
    if route_code == OPERBLOCK_ORDER_ROUTE_DEFAULT:
        return ""
    label = operblock_route_label(route_code, short=short)
    return f"({label})" if label else ""


def _order_dose_text_with_route(dose_text: str, row: dict, *, short: bool = False) -> str:
    clean = re.sub(r"\s+", " ", str(dose_text or "").strip())
    suffix = _order_route_suffix(row, short=short)
    return f"{clean} {suffix}".strip() if clean and suffix else clean


def _clean_operblock_marker_drug_name(drug_name: str) -> str:
    clean = re.sub(r"\s+", " ", str(drug_name or "").strip())
    clean = re.sub(r"\s+\d+(?:[.,]\d+)?\s*%$", "", clean).strip()
    return clean or "Без названия"


def _preferred_operblock_marker_dose(dose_text: str) -> str:
    components = _order_dose_components(dose_text)
    priorities = (
        lambda item: bool(item.get("parenthesized")) and item.get("unit_key") in {"мг", "мкг"},
        lambda item: item.get("unit_key") in {"мг", "мкг"},
        lambda item: item.get("unit_key") == "мл",
    )
    for predicate in priorities:
        for component in components:
            if predicate(component):
                return f"{_format_decimal_ru(component['value'])} {component['unit_label']}"
    return str(dose_text or "").strip()


def _format_operblock_order_marker_text(text: str) -> str:
    return format_operblock_medication_display_label(text)


def _format_order_day(value) -> str:
    parsed = _parse_datetime_value(value)
    return parsed.strftime("%d.%m.%Y") if parsed else "Без даты"


def _format_order_time(value) -> str:
    parsed = _parse_datetime_value(value)
    return parsed.strftime("%H:%M") if parsed else "--:--"


def _split_infusion_rate_text(rate_text: str) -> tuple[str, str]:
    text = re.sub(r"\s+", " ", str(rate_text or "").strip())
    match = re.match(r"^(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>.*)$", text)
    if not match:
        return "", ""
    value = match.group("value").strip()
    try:
        value = _format_decimal_ru(Decimal(value.replace(",", ".")))
    except (InvalidOperation, ValueError):
        pass
    return value, OPERBLOCK_INFUSION_RATE_UNIT


def _format_infusion_rate(value, unit) -> str:
    value_text = str(value or "").strip()
    if not value_text:
        return ""
    try:
        value_text = _format_decimal_ru(Decimal(value_text.replace(",", ".")))
    except (InvalidOperation, ValueError):
        pass
    unit_text = OPERBLOCK_INFUSION_RATE_UNIT
    return f"{value_text} {unit_text}".strip()


def _compact_infusion_rate_display_text(rate_text: str) -> str:
    value, _unit = _split_infusion_rate_text(rate_text)
    if value:
        return f"{value} мл/ч"
    text = re.sub(r"\s+", " ", str(rate_text or "").strip())
    return text.replace("мл/час", "мл/ч")


def _compact_infusion_rate_texts(rate_texts: list[str]) -> str:
    cleaned = [re.sub(r"\s+", " ", str(text or "").strip()) for text in rate_texts]
    cleaned = [text for text in cleaned if text]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]

    parsed: list[tuple[str, str]] = []

    def normalize_unit(unit_text: str) -> str:
        raw_unit = re.sub(r"\s+", "", str(unit_text or "").strip().casefold())
        if raw_unit in {"мл", "ml"}:
            return "мл"
        if raw_unit in {"мл/час", "мл/ч", "ml/h", "ml/hr", "ml/hour"}:
            return OPERBLOCK_INFUSION_RATE_UNIT
        if raw_unit in {"mac", "мак"}:
            return "MAC"
        return re.sub(r"\s+", " ", str(unit_text or "").strip())

    for text in cleaned:
        match = re.match(r"^(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>.*)$", text)
        if not match:
            return " - ".join(cleaned)
        value = match.group("value").strip()
        try:
            value = _format_decimal_ru(Decimal(value.replace(",", ".")))
        except (InvalidOperation, ValueError):
            pass
        unit = normalize_unit(match.group("unit"))
        if not value or not unit:
            return " - ".join(cleaned)
        parsed.append((value, unit))

    units = {unit for _value, unit in parsed}
    if len(units) != 1:
        return " - ".join(cleaned)

    values: list[str] = []
    for value, _unit in parsed:
        if not values or values[-1] != value:
            values.append(value)
    return f"{'-'.join(values)} {parsed[0][1]}"


def _decimal_from_ru_number(value) -> Decimal | None:
    text = str(value or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _format_infusion_volume_ml(value: Decimal | None) -> str:
    if value is None:
        return ""
    rounded = max(Decimal("0"), value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if rounded == rounded.to_integral_value():
        text = str(int(rounded))
    else:
        text = format(rounded.normalize(), "f").rstrip("0").rstrip(".").replace(".", ",")
    return f"{text} мл"


def _round_timed_infusion_volume_ml(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    rounded = max(Decimal("0"), value).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return rounded if rounded > 0 else None


def _normalize_volume_ml_text(value) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    text = re.sub(r"\s*мл\s*$", "", text, flags=re.IGNORECASE).strip()
    volume = _decimal_from_ru_number(text)
    if volume is None or volume <= 0:
        return ""
    return _format_infusion_volume_ml(volume).replace(" мл", "")


def _volume_decimal_ml(value) -> Decimal | None:
    return _decimal_from_ru_number(_normalize_volume_ml_text(value))


def _volume_text_without_unit(value: Decimal | None) -> str:
    return _format_infusion_volume_ml(value).replace(" мл", "") if value is not None else ""


def _solution_display_label(value) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return ""
    lowered = text.casefold()
    if lowered.startswith("s. "):
        return text
    if lowered.startswith("sol. "):
        return f"S. {text[5:].strip()}".strip()
    return f"S. {text}"


def _source_has_solvent(source: dict | None) -> bool:
    source = source or {}
    return bool(str(source.get("solvent_id") or "").strip() or str(source.get("solvent_label") or "").strip())


def _source_solvent_volume_ml(source: dict | None) -> Decimal | None:
    if not _source_has_solvent(source):
        return None
    return _volume_decimal_ml((source or {}).get("solvent_volume_ml"))


def _infusion_declared_volume_ml(interval: dict) -> Decimal | None:
    if _is_gas_infusion(interval):
        return None
    payload = interval.get("payload") if isinstance(interval.get("payload"), dict) else {}

    def declared(value: Decimal | None) -> Decimal | None:
        if _infusion_has_rate(interval):
            return value
        return _round_timed_infusion_volume_ml(value)

    declared_total = _volume_decimal_ml(
        (payload or {}).get("declared_total_volume_ml") or (payload or {}).get("total_volume_ml")
    )
    if declared_total is not None:
        return declared(declared_total)

    event_volume = _volume_decimal_ml(interval.get("volume_ml"))
    payload_volume = _volume_decimal_ml((payload or {}).get("volume_ml"))
    base_volume = event_volume if event_volume is not None else payload_volume
    calculated_volume = _volume_decimal_ml((payload or {}).get("calculated_volume_ml"))
    solvent_volume = _source_solvent_volume_ml(payload)
    if solvent_volume is not None:
        if calculated_volume is not None:
            if base_volume is None or abs(base_volume - calculated_volume) <= Decimal("0.01"):
                return declared(calculated_volume + solvent_volume)
            return declared(base_volume)
        return declared(base_volume if base_volume is not None else solvent_volume)
    if base_volume is not None:
        return declared(base_volume)
    return declared(_volume_decimal_ml((payload or {}).get("solvent_volume_ml")))


def _format_infusion_declared_volume(interval: dict) -> str:
    return _format_infusion_volume_ml(_infusion_declared_volume_ml(interval))


def _is_gas_infusion(interval: dict) -> bool:
    payload = interval.get("payload") if isinstance(interval.get("payload"), dict) else {}
    kind = str((payload or {}).get("kind") or "").strip().casefold()
    return kind == "gas"


OXYGEN_ICON_FILE = "oxygen.png"
OXYGEN_FLOW_UNIT = "л/мин"
OXYGEN_FLOW_MIN_LPM = Decimal("0.1")
OXYGEN_FLOW_STEP_LPM = Decimal("0.1")


def _text_is_oxygen(value) -> bool:
    text = str(value or "").strip().casefold().replace("ё", "е")
    if not text:
        return False
    if "кислород" in text or re.search(r"(?<![0-9a-zа-я])oxygen(?![0-9a-zа-я])", text):
        return True
    return bool(re.search(r"(?<![0-9a-zа-я])(?:o|о)\s*2(?![0-9a-zа-я])", text))


def _payload_is_oxygen(payload: dict | None) -> bool:
    data = payload if isinstance(payload, dict) else {}
    subtype = str(data.get("gas_subtype") or data.get("gas_kind") or data.get("subtype") or "").strip()
    if subtype and _text_is_oxygen(subtype):
        return True
    explicit = data.get("is_oxygen")
    if isinstance(explicit, bool) and explicit:
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
        if _text_is_oxygen(data.get(key)):
            return True
    return False


def _payload_or_text_is_oxygen(payload: dict | None, *texts) -> bool:
    if _payload_is_oxygen(payload):
        return True
    return any(_text_is_oxygen(text) for text in texts)


def _strip_oxygen_token_for_number(value: str) -> str:
    return re.sub(
        r"(?<![0-9a-zа-я])(?:o|о)\s*2(?![0-9a-zа-я])",
        " ",
        str(value or ""),
        flags=re.IGNORECASE,
    )


def _oxygen_flow_value_lpm(value) -> Decimal | None:
    text = _strip_oxygen_token_for_number(str(value or ""))
    match = re.search(r"(?P<value>\d+(?:[,.]\d+)?|[,.]\d+)", text)
    if not match:
        return None
    raw_value = match.group("value").replace(",", ".")
    if raw_value.startswith("."):
        raw_value = f"0{raw_value}"
    try:
        flow = Decimal(raw_value)
    except (InvalidOperation, ValueError):
        return None
    if flow < OXYGEN_FLOW_MIN_LPM:
        return None
    return flow.quantize(OXYGEN_FLOW_STEP_LPM, rounding=ROUND_HALF_UP)


def _normalize_oxygen_flow_text(value) -> str:
    flow = _oxygen_flow_value_lpm(value)
    if flow is None:
        return ""
    return f"{_format_decimal_ru(flow)} {OXYGEN_FLOW_UNIT}"


def _oxygen_payload_fields(payload: dict, flow_text: str) -> dict:
    data = dict(payload or {})
    flow = _oxygen_flow_value_lpm(flow_text)
    data["kind"] = "gas"
    data["gas_subtype"] = "oxygen"
    data["is_oxygen"] = True
    data["oxygen_flow_unit"] = OXYGEN_FLOW_UNIT
    if flow is not None:
        data["oxygen_flow_lpm"] = _format_decimal_ru(flow)
    return data


def _format_oxygen_liters(value: Decimal | None) -> str:
    if value is None:
        return ""
    rounded = max(Decimal("0"), value).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if rounded == rounded.to_integral_value():
        text = str(int(rounded))
    else:
        text = format(rounded.normalize(), "f").rstrip("0").rstrip(".").replace(".", ",")
    return f"{text} л"


def _infusion_display_drug_name(interval: dict, fallback: str = "Дозатор") -> str:
    data = interval or {}
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    clean_fallback = str(fallback or "").strip() or ("Газ" if _is_gas_infusion(data) else "Дозатор")
    raw_name = (
        str((payload or {}).get("display_name") or "").strip()
        or str((payload or {}).get("label") or "").strip()
        or str(data.get("drug_label") or "").strip()
        or str(data.get("display_label") or "").strip()
        or clean_fallback
    )
    name = re.sub(r"\s+", " ", raw_name).strip() or clean_fallback
    name = re.sub(
        (
            r"\s+\d+(?:[,.]\d+)?\s*"
            r"(?:мл/час|мл/ч|ml/h|ml/hr|мл|ml|MAC|мак|л/мин|л/м|l/min|lpm|лит/мин|литр(?:ов)?(?:/мин)?)\s*$"
        ),
        "",
        name,
        flags=re.IGNORECASE,
    ).strip() or clean_fallback
    concentration = re.sub(
        r"\s+",
        " ",
        str((payload or {}).get("concentration") or data.get("concentration_text") or "").strip(),
    )
    has_percentage_in_name = bool(re.search(r"\d+(?:[.,]\d+)?\s*%", name))
    if concentration and not has_percentage_in_name and concentration.casefold() not in name.casefold():
        name = f"{name} {concentration}".strip()
    return name


def _is_oxygen_infusion(interval: dict) -> bool:
    if not _is_gas_infusion(interval or {}):
        return False
    data = interval or {}
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    return _payload_or_text_is_oxygen(
        payload,
        data.get("drug_label"),
        data.get("display_label"),
        _infusion_display_drug_name(data, ""),
    )


def _oxygen_consumed_liters(interval: dict, *, now: datetime | None = None) -> Decimal | None:
    if not _is_oxygen_infusion(interval or {}):
        return None
    start_dt = _minute_floor_dt(_parse_datetime_value((interval or {}).get("start_time")))
    if start_dt is None:
        return None
    end_dt = _minute_floor_dt(_parse_datetime_value((interval or {}).get("end_time")))
    if end_dt is None:
        end_dt = _minute_floor_dt(now or datetime.now())
    if end_dt is None:
        return None
    current_dose = _gas_dose_text(interval or {})
    if end_dt <= start_dt:
        return Decimal("0") if _oxygen_flow_value_lpm(current_dose) is not None else None

    events = [
        event
        for event in _gas_dose_events(interval or {})
        if _minute_floor_dt(_parse_datetime_value((event or {}).get("event_time"))) is not None
    ]
    events.sort(key=lambda item: _minute_floor_dt(_parse_datetime_value(item.get("event_time"))) or datetime.min)
    for event in events:
        event_dt = _minute_floor_dt(_parse_datetime_value(event.get("event_time")))
        if event_dt is not None and event_dt <= start_dt:
            current_dose = str(event.get("dose_text") or "").strip()
        elif event_dt is not None:
            break
    if not current_dose and events:
        current_dose = str(events[0].get("dose_text") or "").strip()

    total = Decimal("0")
    has_flow = False
    cursor_dt = start_dt

    def add_segment(flow_text: str, from_dt: datetime, to_dt: datetime) -> None:
        nonlocal total, has_flow
        if to_dt <= from_dt:
            return
        flow = _oxygen_flow_value_lpm(flow_text)
        if flow is None:
            return
        minutes = Decimal(str((to_dt - from_dt).total_seconds())) / Decimal("60")
        total += flow * minutes
        has_flow = True

    for event in events:
        event_dt = _minute_floor_dt(_parse_datetime_value(event.get("event_time")))
        if event_dt is None or event_dt <= start_dt:
            continue
        if event_dt >= end_dt:
            break
        add_segment(current_dose, cursor_dt, event_dt)
        current_dose = str(event.get("dose_text") or "").strip()
        cursor_dt = event_dt
    add_segment(current_dose, cursor_dt, end_dt)
    return total if has_flow else None


def _format_oxygen_consumed_liters(interval: dict, *, now: datetime | None = None) -> str:
    return _format_oxygen_liters(_oxygen_consumed_liters(interval or {}, now=now))


def _normalize_gas_identity_text(value: str) -> str:
    clean = re.sub(r"\s+", " ", str(value or "").strip())
    clean = re.sub(r"^(?:s|sol)\.\s+", "", clean, flags=re.IGNORECASE).strip()
    clean = re.sub(r"\s+\d+(?:[.,]\d+)?\s*%$", "", clean).strip()
    return clean.casefold().replace("ё", "е")


def _gas_display_name_for_payload(drug_name: str, payload: dict | None) -> str:
    gas_payload = dict(payload or {}) if isinstance(payload, dict) else {}
    gas_payload["kind"] = "gas"
    return _infusion_display_drug_name({"drug_label": drug_name, "payload": gas_payload}, "Газ")


def _gas_identity_matches(active_interval: dict, drug_name: str, payload: dict | None) -> bool:
    active_payload = active_interval.get("payload") if isinstance(active_interval.get("payload"), dict) else {}
    requested_payload = payload if isinstance(payload, dict) else {}
    if _is_oxygen_infusion(active_interval) and _payload_or_text_is_oxygen(requested_payload, drug_name):
        return True
    for key in ("preset_id", "source_drug_id"):
        active_value = str((active_payload or {}).get(key) or "").strip()
        requested_value = str((requested_payload or {}).get(key) or "").strip()
        if active_value and requested_value and active_value == requested_value:
            return True
    active_name = _normalize_gas_identity_text(_infusion_display_drug_name(active_interval, "Газ"))
    requested_name = _normalize_gas_identity_text(_gas_display_name_for_payload(drug_name, requested_payload))
    return bool(active_name and requested_name and active_name == requested_name)


def _normalize_gas_dose_text(value: str) -> str:
    clean = re.sub(r"\s+", " ", str(value or "").strip())
    if not clean:
        return ""
    value_only = re.sub(r"(?i)(?:mac|мак)", " ", clean)
    value_only = re.sub(r"\s+", " ", value_only).strip()
    match = re.fullmatch(
        r"(?P<first>\d+(?:[.,]\d+)?)(?:\s*[-–—]\s*(?P<second>\d+(?:[.,]\d+)?))?",
        value_only,
    )
    if match:
        values = []
        for group_name in ("first", "second"):
            raw_value = match.group(group_name)
            if not raw_value:
                continue
            try:
                values.append(_format_decimal_ru(Decimal(raw_value.replace(",", "."))))
            except (InvalidOperation, ValueError):
                values.append(raw_value.replace(".", ","))
        return f"{'-'.join(values)} MAC"
    clean = re.sub(r"(?i)(?:mac|мак)", " MAC ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    clean = re.sub(r"(?i)(?:\s+MAC)+$", " MAC", clean)
    if "mac" not in clean.casefold():
        clean = f"{clean} MAC"
    return clean


def _gas_dose_text(interval: dict) -> str:
    payload = interval.get("payload") if isinstance(interval.get("payload"), dict) else {}
    dose_text = str((payload or {}).get("display_dose_text") or (payload or {}).get("dose_text") or "")
    if _is_oxygen_infusion(interval or {}):
        return _normalize_oxygen_flow_text(dose_text)
    return _normalize_gas_dose_text(dose_text)


def _gas_dose_events(interval: dict) -> list[dict]:
    events: list[dict] = []
    normalize_dose = _normalize_oxygen_flow_text if _is_oxygen_infusion(interval or {}) else _normalize_gas_dose_text
    for item in list((interval or {}).get("dose_history") or []):
        event_dt = _minute_floor_dt(_parse_datetime_value((item or {}).get("event_time")))
        dose_text = normalize_dose(str((item or {}).get("dose_text") or ""))
        if event_dt is None or not dose_text:
            continue
        events.append(
            {
                "event_id": (item or {}).get("event_id"),
                "event_time": event_dt,
                "dose_text": dose_text,
                "revision": (item or {}).get("revision"),
            }
        )
    if not events:
        start_dt = _minute_floor_dt(_parse_datetime_value((interval or {}).get("start_time")))
        dose_text = _gas_dose_text(interval or {})
        if start_dt is not None and dose_text:
            events.append({"event_time": start_dt, "dose_text": dose_text})
    events.sort(key=lambda item: item["event_time"])
    deduped: list[dict] = []
    for event in events:
        if deduped and deduped[-1]["event_time"] == event["event_time"]:
            deduped[-1] = event
        else:
            deduped.append(event)
    return deduped


def _is_volume_only_infusion(interval: dict) -> bool:
    if _is_gas_infusion(interval):
        return False
    return bool(_infusion_declared_volume_ml(interval) is not None and not _infusion_has_rate(interval))


def _counted_infusion_volume_ml(interval: dict, *, now: datetime | None = None) -> Decimal | None:
    if _infusion_has_rate(interval):
        return _infusion_volume_ml(interval, now=now)
    if _infusion_declared_volume_ml(interval) is None:
        return None
    if str(interval.get("status") or "") == "active" and not interval.get("end_time"):
        return None
    return _infusion_declared_volume_ml(interval)


def _infusion_rate_events(interval: dict) -> list[dict]:
    events = []
    for item in list(interval.get("rate_history") or []):
        event_dt = _minute_floor_dt(_parse_datetime_value((item or {}).get("event_time")))
        rate_value = _decimal_from_ru_number((item or {}).get("rate_value"))
        if event_dt is None or rate_value is None:
            continue
        events.append({"event_time": event_dt, "rate_value": rate_value})
    if not events:
        start_dt = _minute_floor_dt(_parse_datetime_value(interval.get("start_time")))
        rate_value = _decimal_from_ru_number(interval.get("current_rate_value"))
        if start_dt is not None and rate_value is not None:
            events.append({"event_time": start_dt, "rate_value": rate_value})
    events.sort(key=lambda item: item["event_time"])
    deduped: list[dict] = []
    for event in events:
        if deduped and deduped[-1]["event_time"] == event["event_time"]:
            deduped[-1] = event
        else:
            deduped.append(event)
    return deduped


def _infusion_volume_ml(interval: dict, *, now: datetime | None = None) -> Decimal | None:
    start_dt = _minute_floor_dt(_parse_datetime_value(interval.get("start_time")))
    if start_dt is None:
        return None
    current_time = _minute_floor_dt(now or datetime.now())
    end_dt = _minute_floor_dt(_parse_datetime_value(interval.get("end_time")))
    if end_dt is None and str(interval.get("status") or "") != "active":
        end_dt = current_time
    effective_end = end_dt or current_time
    if effective_end <= start_dt:
        return Decimal("0")

    events = [event for event in _infusion_rate_events(interval) if event["event_time"] <= effective_end]
    if not events:
        return None
    if events[0]["event_time"] > start_dt:
        events.insert(0, {"event_time": start_dt, "rate_value": events[0]["rate_value"]})

    total = Decimal("0")
    for index, event in enumerate(events):
        segment_start = max(start_dt, event["event_time"])
        next_start = events[index + 1]["event_time"] if index + 1 < len(events) else effective_end
        segment_end = min(effective_end, next_start)
        if segment_end <= segment_start:
            continue
        minutes = Decimal(int((segment_end - segment_start).total_seconds() // 60))
        total += event["rate_value"] * minutes / Decimal("60")
    return total


def _format_infusion_executed_volume(interval: dict, *, now: datetime | None = None) -> str:
    volume = _infusion_volume_ml(interval, now=now)
    return _format_infusion_volume_ml(volume)


def _infusion_has_rate(interval: dict) -> bool:
    if _is_gas_infusion(interval or {}):
        return False
    if _format_infusion_rate(interval.get("current_rate_value"), interval.get("current_rate_unit")):
        return True
    return bool(_infusion_rate_events(interval))


def _normalize_infusion_rate_option(rate_text: str) -> str:
    value, unit = _split_infusion_rate_text(rate_text)
    return _format_infusion_rate(value, unit) if value else ""


def _normalize_infusion_rate_options(rates) -> list[str]:
    result: list[str] = []
    for rate in rates or []:
        text = _normalize_infusion_rate_option(str(rate or ""))
        if text and text not in result:
            result.append(text)
    return result or list(OPERBLOCK_DEFAULT_INFUSION_RATES)


def _dose_option_with_unit(value, unit) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return ""
    if _order_dose_components(text):
        return text
    _unit_key, unit_label = _normalize_dose_unit(str(unit or ""))
    return f"{text} {unit_label}".strip() if unit_label else text


def _timed_infusion_dose_options(preset: dict) -> list[str]:
    result: list[str] = []

    def add_option(value) -> None:
        text = re.sub(r"\s+", " ", str(value or "").strip())
        if text and text not in result:
            result.append(text)

    for dose in preset.get("doses") or []:
        add_option(dose)

    if not result:
        add_option(_dose_option_with_unit(preset.get("default_dose"), preset.get("unit")))

    if not result:
        for value in (preset.get("solvent_volume_ml"), preset.get("volume_ml")):
            volume = _normalize_volume_ml_text(value)
            if volume:
                add_option(f"{volume} мл")
    return result


def _quick_order_title_and_concentration(title: str, concentration_text: str = "") -> tuple[str, str]:
    clean_title = re.sub(r"\s+", " ", str(title or "").strip())
    clean_concentration = re.sub(r"\s+", " ", str(concentration_text or "").strip())
    return clean_title or str(title or "").strip(), clean_concentration


def _quick_order_solvent_text(source: dict) -> str:
    solvent = _solution_display_label((source or {}).get("solvent_label") or (source or {}).get("solvent_id"))
    volume = _normalize_volume_ml_text((source or {}).get("solvent_volume_ml"))
    if solvent and volume:
        return f"{solvent} - {volume} мл"
    return solvent or (f"{volume} мл" if volume else "")


def _split_semicolon_list(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(";") if item.strip()]


def _join_semicolon_list(values) -> str:
    return "; ".join(str(value or "").strip() for value in values or [] if str(value or "").strip())


def _format_infusion_duration(start_time) -> str:
    start_dt = _parse_datetime_value(start_time)
    if start_dt is None:
        return ""
    delta = max(timedelta(0), datetime.now().replace(second=0, microsecond=0) - _minute_floor_dt(start_dt))
    minutes = int(delta.total_seconds() // 60)
    hours, mins = divmod(minutes, 60)
    if hours:
        return f"{hours}ч {mins:02d}мин"
    return f"{mins}мин"


def _format_infusion_interval_duration(start_time, end_time=None) -> str:
    start_dt = _minute_floor_dt(_parse_datetime_value(start_time))
    if start_dt is None:
        return ""
    end_dt = _minute_floor_dt(_parse_datetime_value(end_time)) or datetime.now().replace(second=0, microsecond=0)
    delta = max(timedelta(0), end_dt - start_dt)
    minutes = int(delta.total_seconds() // 60)
    hours, mins = divmod(minutes, 60)
    if hours:
        return f"{hours}ч {mins:02d}мин"
    return f"{mins}мин"


def _order_sort_dt(row: dict) -> datetime:
    return _parse_datetime_value(row.get("datetime")) or datetime.min


_DOSE_COMPONENT_RE = re.compile(r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>[A-Za-zА-Яа-яЁёµ%./]+)")


def _normalize_dose_unit(unit: str) -> tuple[str, str]:
    raw = str(unit or "").strip().lower().replace("ё", "е").replace("µ", "мк")
    raw = raw.replace(".", "")
    aliases = {
        "ml": "мл",
        "мл": "мл",
        "миллилитр": "мл",
        "миллилитра": "мл",
        "миллилитры": "мл",
        "миллилитров": "мл",
        "mg": "мг",
        "мг": "мг",
        "миллиграмм": "мг",
        "миллиграмма": "мг",
        "миллиграммы": "мг",
        "миллиграммов": "мг",
        "миллиграмы": "мг",
        "mcg": "мкг",
        "mkg": "мкг",
        "мкг": "мкг",
        "мкгр": "мкг",
        "микрограмм": "мкг",
        "микрограмма": "мкг",
        "микрограммы": "мкг",
        "микрограммов": "мкг",
        "ug": "мкг",
        "g": "г",
        "гр": "г",
        "г": "г",
        "грам": "г",
        "грамм": "г",
        "грама": "г",
        "грамма": "г",
        "грамы": "г",
        "граммы": "г",
        "граммов": "г",
        "ed": "ед",
        "ед": "ед",
        "me": "МЕ",
        "ме": "МЕ",
        "mac": "MAC",
        "мак": "MAC",
        "%": "%",
    }
    label = aliases.get(raw, raw or unit)
    return label.casefold(), label


def _parse_dose_components(text: str, *, parenthesized: bool) -> list[dict]:
    components: list[dict] = []
    for match in _DOSE_COMPONENT_RE.finditer(str(text or "")):
        try:
            value = Decimal(match.group("value").replace(",", "."))
        except (InvalidOperation, AttributeError):
            continue
        unit_key, unit_label = _normalize_dose_unit(match.group("unit"))
        if not unit_key:
            continue
        components.append(
            {
                "value": value,
                "unit_key": unit_key,
                "unit_label": unit_label,
                "parenthesized": parenthesized,
            }
        )
    return components


def _order_dose_components(dose_text: str) -> list[dict]:
    clean = str(dose_text or "")
    parenthetical_parts = re.findall(r"\(([^)]*)\)", clean)
    main_text = re.sub(r"\([^)]*\)", " ", clean)
    components = _parse_dose_components(main_text, parenthesized=False)
    for part in parenthetical_parts:
        components.extend(_parse_dose_components(part, parenthesized=True))
    return components


def _format_decimal_ru(value: Decimal) -> str:
    if value == value.to_integral_value():
        return str(int(value))
    text = format(value.normalize(), "f").rstrip("0").rstrip(".")
    return text.replace(".", ",")


def _mass_to_micrograms(value: Decimal, unit_key: str) -> Decimal | None:
    if unit_key == "мкг":
        return value
    if unit_key == "мг":
        return value * Decimal("1000")
    if unit_key == "г":
        return value * Decimal("1000000")
    return None


def _parse_concentration_mass_per_ml(concentration_text: str) -> Decimal | None:
    text = re.sub(r"\s+", " ", str(concentration_text or "").strip())
    match = re.search(
        r"(?P<mass>\d+(?:[.,]\d+)?)\s*(?P<mass_unit>мкгр|мкг|mcg|mkg|ug|мг|mg|гр|г|g)"
        r"\s*/\s*(?:(?P<volume>\d+(?:[.,]\d+)?)\s*)?(?P<volume_unit>мл|ml)\b",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    mass_value = _decimal_from_ru_number(match.group("mass"))
    volume_value = _decimal_from_ru_number(match.group("volume") or "1")
    if mass_value is None or volume_value is None or mass_value <= 0 or volume_value <= 0:
        return None
    mass_unit_key, _mass_unit_label = _normalize_dose_unit(match.group("mass_unit"))
    mass_micrograms = _mass_to_micrograms(mass_value, mass_unit_key)
    if mass_micrograms is None:
        return None
    return mass_micrograms / volume_value


def _quick_order_explicit_volume_ml(dose_text: str) -> Decimal | None:
    for component in _order_dose_components(dose_text):
        if component.get("unit_key") == "мл" and component.get("value") > 0:
            return component["value"]
    return None


def _quick_order_mass_dose_component(dose_text: str) -> dict | None:
    return next(
        (
            item
            for item in _order_dose_components(dose_text)
            if not bool(item.get("parenthesized")) and item.get("unit_key") in {"мг", "мкг", "г"}
        ),
        None,
    )


def _quick_order_mass_dose_volume_ml(dose_text: str, concentration_text: str) -> Decimal | None:
    concentration_per_ml = _parse_concentration_mass_per_ml(concentration_text)
    if concentration_per_ml is None or concentration_per_ml <= 0:
        return None
    dose_component = _quick_order_mass_dose_component(dose_text)
    if not dose_component:
        return None
    dose_micrograms = _mass_to_micrograms(dose_component["value"], str(dose_component.get("unit_key") or ""))
    if dose_micrograms is None or dose_micrograms <= 0:
        return None
    return dose_micrograms / concentration_per_ml


def _quick_order_dose_volume_ml(dose_text: str, concentration_text: str) -> Decimal | None:
    explicit_volume = _quick_order_explicit_volume_ml(dose_text)
    if explicit_volume is not None:
        return explicit_volume
    return _quick_order_mass_dose_volume_ml(dose_text, concentration_text)


def _quick_order_dose_volume_ml_text(dose_text: str, concentration_text: str) -> str:
    volume = _quick_order_dose_volume_ml(dose_text, concentration_text)
    return _format_infusion_volume_ml(volume).replace(" мл", "") if volume is not None else ""


def _timed_infusion_total_volume_ml(
    preset: dict,
    dose_text: str,
    concentration_text: str,
) -> Decimal | None:
    dose_volume = _quick_order_dose_volume_ml(dose_text, concentration_text)
    solvent_volume = _source_solvent_volume_ml(preset)
    if solvent_volume is not None:
        return _round_timed_infusion_volume_ml((dose_volume or Decimal("0")) + solvent_volume)
    if dose_volume is not None:
        return _round_timed_infusion_volume_ml(dose_volume)
    return _round_timed_infusion_volume_ml(
        _volume_decimal_ml((preset or {}).get("volume_ml") or (preset or {}).get("solvent_volume_ml"))
    )


def _quick_order_dose_display_text(dose_text: str, concentration_text: str) -> str:
    clean_dose = re.sub(r"\s+", " ", str(dose_text or "").strip())
    if not clean_dose or re.search(r"\([^)]*\b(?:мл|ml)\b[^)]*\)", clean_dose, flags=re.IGNORECASE):
        return clean_dose
    volume_text = _format_infusion_volume_ml(_quick_order_mass_dose_volume_ml(clean_dose, concentration_text))
    return f"{clean_dose} ({volume_text})" if volume_text else clean_dose


def _dose_text_with_computed_volume(dose_text: str, concentration_text: str) -> str:
    clean_dose = re.sub(r"\s+", " ", str(dose_text or "").strip())
    if not clean_dose or _quick_order_explicit_volume_ml(clean_dose) is not None:
        return clean_dose
    volume_text = _format_infusion_volume_ml(_quick_order_mass_dose_volume_ml(clean_dose, concentration_text))
    return f"{clean_dose} ({volume_text})" if volume_text else clean_dose


def _summarize_dose_texts(dose_texts: list[str], *, include_unparsed: bool = True) -> str:
    totals: dict[tuple[bool, str], dict] = {}
    order: list[tuple[bool, str]] = []
    unparsed: list[str] = []
    for dose_text in dose_texts:
        clean_text = re.sub(r"\s+", " ", str(dose_text or "").strip())
        if not clean_text:
            continue
        components = _order_dose_components(clean_text)
        if not components:
            unparsed.append(clean_text)
            continue
        for component in components:
            key = (bool(component["parenthesized"]), str(component["unit_key"]))
            if key not in totals:
                totals[key] = {"value": Decimal("0"), "unit_label": component["unit_label"], "count": 0}
                order.append(key)
            totals[key]["value"] += component["value"]
            totals[key]["count"] += 1

    main_parts: list[str] = []
    parenthetical_parts: list[str] = []
    for key in order:
        total = totals[key]
        if key[1] == "mac":
            value = total["value"] / max(1, int(total.get("count") or 0))
            part = f"среднее {_format_decimal_ru(value)} {total['unit_label']}"
        else:
            part = f"{_format_decimal_ru(total['value'])} {total['unit_label']}"
        if key[0]:
            parenthetical_parts.append(part)
        else:
            main_parts.append(part)

    summary = ", ".join(main_parts) if main_parts else ""
    if parenthetical_parts:
        parens = ", ".join(parenthetical_parts)
        summary = f"{summary} ({parens})" if summary else f"({parens})"

    unique_unparsed = list(dict.fromkeys(unparsed))
    if include_unparsed and unique_unparsed:
        unparsed_text = ", ".join(unique_unparsed)
        summary = f"{summary}, {unparsed_text}" if summary else unparsed_text
    return summary


def _summarize_order_total(rows: list[dict], *, concentration_for_row=None) -> str:
    dose_texts: list[str] = []
    for row in rows:
        dose_text = str((row or {}).get("dose_text") or "")
        concentration_text = ""
        if callable(concentration_for_row):
            try:
                concentration_text = str(concentration_for_row(row or {}) or "")
            except Exception:
                concentration_text = ""
        dose_texts.append(_dose_text_with_computed_volume(dose_text, concentration_text))
    summary = _summarize_dose_texts(
        dose_texts,
        include_unparsed=False,
    )
    if not summary:
        return f"внесено: {len(rows)}"
    return f"Итого: {summary}"
