from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import html
import re

import pyqtgraph as pg
from PySide6.QtCore import QEvent, QRectF, Qt, QTimer
from PySide6.QtGui import QFont, QFontMetrics, QPainterPath
from PySide6.QtWidgets import QGraphicsPathItem, QScrollBar

from rem_card.services.operblock_route_settings import (
    OPERBLOCK_DEFAULT_ROUTE_CODE,
    normalize_operblock_route_code,
    operblock_route_from_comment,
    operblock_route_label,
)
from rem_card.services.operblock_timeline import format_operblock_medication_display_label
from rem_card.ui.shared.chart_widget import ChartWidget
from rem_card.ui.operblock_view.operblock_control_styles import (
    operblock_horizontal_scrollbar_style,
    operblock_vertical_scrollbar_style as _operblock_vertical_scrollbar_style,
)

OPERBLOCK_INITIAL_CHART_HOURS = 3

OPERBLOCK_INFUSION_RATE_UNIT = "мл/час"

OPERBLOCK_ORDER_ROUTE_DEFAULT = OPERBLOCK_DEFAULT_ROUTE_CODE

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

def _normalize_order_route_code(value) -> str:
    return normalize_operblock_route_code(value)

def _order_route_code_from_comment(comment: str) -> str:
    return operblock_route_from_comment(comment) or OPERBLOCK_ORDER_ROUTE_DEFAULT

def _order_route_code(row: dict) -> str:
    route = str((row or {}).get("route") or "").strip()
    if route:
        return _normalize_order_route_code(route)
    payload = (row or {}).get("payload")
    if isinstance(payload, dict) and payload.get("route"):
        return _normalize_order_route_code(payload.get("route"))
    return _order_route_code_from_comment(str((row or {}).get("comment") or ""))

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

def _format_operblock_order_marker_text(text: str) -> str:
    return format_operblock_medication_display_label(text)

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

OXYGEN_CHART_FLOW_UNIT = "л/м"
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
    return f"{_format_decimal_ru(flow)} {OXYGEN_CHART_FLOW_UNIT}"

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

class _OperBlockDisplayVital:
    """Прокси витала: timestamp для отрисовки, real_timestamp для подсказки."""

    def __init__(self, source, display_timestamp: datetime):
        self._source = source
        self.real_timestamp = getattr(source, "timestamp", None)
        self.timestamp = display_timestamp

    def __getattr__(self, name):
        return getattr(self._source, name)


class _OperBlockTimelineTransform:
    PREOP_GAP = timedelta(minutes=20)

    def __init__(self, *, baseline_at: datetime | None, operation_start_at: datetime | None):
        self.baseline_at = _minute_floor_dt(baseline_at)
        self.operation_start_at = _minute_floor_dt(operation_start_at)
        self.display_origin_at = self._build_display_origin()

    def _build_display_origin(self) -> datetime | None:
        if self.has_preop_gap:
            return self.operation_start_at - self.display_preop_gap
        return self.operation_start_at or self.baseline_at

    @property
    def has_preop_gap(self) -> bool:
        return (
            isinstance(self.baseline_at, datetime)
            and isinstance(self.operation_start_at, datetime)
            and self.operation_start_at >= self.baseline_at
        )

    @property
    def real_preop_span(self) -> timedelta:
        if not self.has_preop_gap:
            return timedelta(0)
        return self.operation_start_at - self.baseline_at

    @property
    def display_preop_gap(self) -> timedelta:
        real_span = self.real_preop_span
        if self.has_preop_gap and real_span <= timedelta(0):
            return self.PREOP_GAP
        return max(real_span, self.PREOP_GAP)

    def signature(self) -> tuple:
        return (
            self.baseline_at.isoformat() if isinstance(self.baseline_at, datetime) else None,
            self.operation_start_at.isoformat() if isinstance(self.operation_start_at, datetime) else None,
            self.display_origin_at.isoformat() if isinstance(self.display_origin_at, datetime) else None,
            int(self.display_preop_gap.total_seconds()),
        )

    def display_datetime_for(self, value: datetime | None) -> datetime | None:
        event_dt = _minute_floor_dt(value)
        if event_dt is None:
            return None
        if not self.has_preop_gap or self.display_origin_at is None:
            return event_dt
        baseline_at = self.baseline_at
        operation_start_at = self.operation_start_at
        if event_dt < baseline_at:
            return self.display_origin_at
        if self.real_preop_span <= timedelta(0):
            if event_dt <= operation_start_at:
                return operation_start_at
            return event_dt
        if event_dt <= baseline_at:
            return self.display_origin_at
        if event_dt < operation_start_at:
            real_span = max(1.0, self.real_preop_span.total_seconds())
            ratio = (event_dt - baseline_at).total_seconds() / real_span
            return self.display_origin_at + timedelta(seconds=self.display_preop_gap.total_seconds() * ratio)
        return event_dt

    def display_datetime_for_vital(self, value: datetime | None) -> datetime | None:
        event_dt = _minute_floor_dt(value)
        if event_dt is None:
            return None
        if not self.has_preop_gap or self.display_origin_at is None:
            return event_dt
        baseline_at = self.baseline_at
        operation_start_at = self.operation_start_at
        if event_dt <= baseline_at:
            return self.display_origin_at
        if event_dt < operation_start_at:
            real_span = max(1.0, self.real_preop_span.total_seconds())
            ratio = (event_dt - baseline_at).total_seconds() / real_span
            return self.display_origin_at + timedelta(seconds=self.display_preop_gap.total_seconds() * ratio)
        return event_dt

    def display_hours_for(self, value: datetime | None, *, fallback_start: datetime | None = None) -> float | None:
        display_dt = self.display_datetime_for(value)
        origin = self.display_origin_at or fallback_start
        if display_dt is None or origin is None:
            return None
        return (display_dt - origin).total_seconds() / 3600.0

    def display_hours_for_vital(self, value: datetime | None, *, fallback_start: datetime | None = None) -> float | None:
        display_dt = self.display_datetime_for_vital(value)
        origin = self.display_origin_at or fallback_start
        if display_dt is None or origin is None:
            return None
        return (display_dt - origin).total_seconds() / 3600.0

    def real_datetime_for_display_hours(
        self,
        x: float,
        *,
        fallback_start: datetime | None = None,
    ) -> datetime | None:
        origin = self.display_origin_at or fallback_start
        if origin is None:
            return None
        display_dt = origin + timedelta(hours=float(x or 0.0))
        if not self.has_preop_gap or self.display_origin_at is None:
            return display_dt
        if display_dt <= self.display_origin_at:
            return self.baseline_at
        if display_dt < self.operation_start_at:
            display_span = max(1.0, self.display_preop_gap.total_seconds())
            real_span = max(1.0, self.real_preop_span.total_seconds())
            ratio = (display_dt - self.display_origin_at).total_seconds() / display_span
            return self.baseline_at + timedelta(seconds=real_span * ratio)
        return display_dt


class OperBlockChartWidget(ChartWidget):
    """Оперблок-слой поверх графика виталов: маркеры введённых назначений."""

    VITAL_AXIS_MIN = 0.0
    VITAL_AXIS_MAX = 260.0
    MEDICATION_BAND_MIN = -170.0
    ORDER_MARKER_Y = -154.0
    ORDER_MARKER_Y_STEP = 5.0
    ORDER_LABEL_Y = -8.0
    ORDER_LABEL_Y_STEP = 15.0
    ORDER_LABEL_LANE_COUNT = 10
    ORDER_LABEL_FONT_SIZE = 10
    ORDER_NAME_COLUMN_MARGIN_PX = 8.0
    ORDER_LABEL_COLUMN_GUARD_PX = 6.0
    ORDER_LABEL_SCROLLBAR_WIDTH_PX = 16
    ORDER_LABEL_BOTTOM_MARGIN_PX = 10.0
    MEDICATION_DETAIL_LIFT_PX = 10.0
    ORDER_DOSE_MARKER_OFFSET_PX = 22.0
    ORDER_DOSE_MARKER_SIZE = 8.0
    ORDER_GUIDELINE_Z = 35
    ORDER_GUIDELINE_MASK_Z = 36
    ORDER_GUIDELINE_GAP_PADDING_PX = 5.0
    ORDER_GUIDELINE_MASK_WIDTH_PX = 5
    INFUSION_BAR_OFFSET_PX = 22.0
    INFUSION_BAR_WIDTH = 7.0
    INFUSION_BAR_MASK_Z = 37
    INFUSION_BAR_Z = 78
    INFUSION_RATE_LABEL_Z = 77
    INFUSION_SEGMENT_GAP_PX = 4.0
    INFUSION_BOTTLE_EDGE_TRIM_PX = 4.0
    INFUSION_BAR_CORNER_RADIUS_PX = 3.0
    INFUSION_RATE_LABEL_CLUSTER_GAP_PX = 6.0
    INFUSION_LANE_Y = -166.0
    INFUSION_LANE_STEP = 5.0
    PREOP_VISUAL_GAP_MINUTES = 20
    TIMELINE_VIEWPORT_HOURS = OPERBLOCK_INITIAL_CHART_HOURS
    TIMELINE_SCROLL_UNITS_PER_HOUR = 60
    TIMELINE_SCROLL_OVERLAY_REFRESH_MS = 40
    ORDER_LABEL_EDGE_MARGIN_PX = 8.0
    ORDER_LABEL_LANE_GAP_PX = 8.0
    OPERATION_START_LABEL = "Начало пособия"
    OPERATION_START_LABEL_FONT_SIZE = 11
    OPERATION_STAGE_LABELS = {
        "anesthesia_start": "Начало пособия",
        "anesthesia_end": "Конец пособия",
        "surgery_start": "Начало операции",
        "surgery_end": "Конец операции",
    }
    OPERATION_STAGE_COLORS = {
        "anesthesia_start": "#506174",
        "anesthesia_end": "#7A3E3E",
        "surgery_start": "#2563EB",
        "surgery_end": "#C2410C",
        "custom": "#0F766E",
    }
    # MVP: protocol snapshot limits orders to 100 rows, so a full overlay rebuild is bounded.
    ORDER_MARKER_RECREATE_LIMIT = 150
    INFUSION_INTERVAL_RECREATE_LIMIT = 50
    ORDER_MARKER_STYLES = (
        {"color": "#8e44ad", "symbol": "d"},
        {"color": "#2980b9", "symbol": "o"},
        {"color": "#16a085", "symbol": "s"},
        {"color": "#d35400", "symbol": "t"},
        {"color": "#c0392b", "symbol": "t1"},
        {"color": "#2c3e50", "symbol": "t2"},
        {"color": "#7f8c8d", "symbol": "t3"},
        {"color": "#27ae60", "symbol": "p"},
        {"color": "#f39c12", "symbol": "h"},
        {"color": "#34495e", "symbol": "+"},
        {"color": "#9b59b6", "symbol": "x"},
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._apply_operblock_y_axis()
        self._order_marker_items: list = []
        self._operation_stage_marker_items_by_key: dict[str, list] = {}
        self._order_marker_groups: list[dict] = []
        self._order_marker_rows: list[dict] = []
        self._infusion_interval_rows: list[dict] = []
        self._operation_stage_rows: list[dict] = []
        self._infusion_interval_groups: list[dict] = []
        self._order_marker_start: datetime | None = None
        self._order_marker_style_map: dict[str, dict] = {}
        self._order_marker_render_signature: tuple | None = None
        self._timeline_snapshot: dict | None = None
        self._timeline_transform: _OperBlockTimelineTransform | None = None
        self._order_label_metrics = QFontMetrics(QFont("Segoe UI", self.ORDER_LABEL_FONT_SIZE, QFont.Bold))
        self._operation_start_label_metrics = QFontMetrics(
            QFont("Segoe UI", self.OPERATION_START_LABEL_FONT_SIZE, QFont.Bold)
        )
        self._order_label_width_cache: dict[str, float] = {}
        self._timeline_total_hours = float(getattr(self, "visible_hours", OPERBLOCK_INITIAL_CHART_HOURS))
        self._timeline_view_left = 0.0
        self._timeline_auto_follow_latest = True
        self._timeline_scrollbar_syncing = False
        self._order_label_total_lanes = 0
        self._order_label_scroll_top_lane = 0
        self._order_label_scrollbar_syncing = False
        self._last_operation_stage_gaps_by_x: dict[float, list[tuple[float, float]]] = {}
        self._timeline_scrollbar = self._create_timeline_scrollbar()
        self._order_label_scrollbar = self._create_order_label_scrollbar()
        self._timeline_scroll_refresh_timer = QTimer(self)
        self._timeline_scroll_refresh_timer.setSingleShot(True)
        self._timeline_scroll_refresh_timer.timeout.connect(self._refresh_timeline_scroll_overlay)
        self._order_label_scroll_refresh_timer = QTimer(self)
        self._order_label_scroll_refresh_timer.setSingleShot(True)
        self._order_label_scroll_refresh_timer.timeout.connect(self._refresh_order_label_scroll_overlay)
        self.chart_layout.addWidget(self._timeline_scrollbar)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_order_label_scrollbar_geometry()
        self._order_marker_render_signature = None
        self._render_order_markers(force=True)

    def eventFilter(self, source, event):
        if source is self.plot_widget.viewport() and event.type() == QEvent.Wheel:
            if self._handle_order_label_wheel_event(event):
                return True
        return super().eventFilter(source, event)

    def set_visible_hours(self, visible_hours: int | float):
        hours = max(1, int(visible_hours or OPERBLOCK_INITIAL_CHART_HOURS))
        old_total = float(getattr(self, "_timeline_total_hours", getattr(self, "visible_hours", hours)))
        was_at_latest = self._timeline_auto_follow_latest or self._timeline_view_is_at_latest(total_hours=old_total)
        total_changed = int(getattr(self, "visible_hours", 0) or 0) != hours
        if total_changed:
            self.visible_hours = hours
            self._timeline_total_hours = float(hours)
            self._ensure_vertical_grid_lines(hours)
            self._last_render_key = None
        else:
            self._timeline_total_hours = float(hours)
        self._apply_operblock_y_axis()
        range_changed = self._apply_timeline_view_window(follow_latest=was_at_latest)
        if total_changed or range_changed:
            self._order_marker_render_signature = None
            self._render_order_markers(force=True)
        self.header_spacer.update()

    def _apply_operblock_y_axis(self):
        ax_left = self.plot_widget.getAxis("left")
        ax_left.setTicks([[(v, str(v)) for v in range(0, int(self.VITAL_AXIS_MAX) + 1, 10)]])
        visible_hours = max(1.0, float(getattr(self, "visible_hours", OPERBLOCK_INITIAL_CHART_HOURS)))
        self.plot_widget.getViewBox().setLimits(
            xMin=0,
            xMax=visible_hours,
            yMin=self.MEDICATION_BAND_MIN,
            yMax=self.VITAL_AXIS_MAX,
        )
        self.plot_widget.setYRange(self.MEDICATION_BAND_MIN, self.VITAL_AXIS_MAX, padding=0)

    def _create_timeline_scrollbar(self) -> QScrollBar:
        scrollbar = QScrollBar(Qt.Horizontal, self)
        scrollbar.setObjectName("OperBlockTimelineScrollBar")
        scrollbar.setFixedHeight(16)
        scrollbar.setSingleStep(1)
        scrollbar.valueChanged.connect(self._on_timeline_scroll_value_changed)
        scrollbar.sliderPressed.connect(self._on_timeline_scroll_user_started)
        scrollbar.sliderReleased.connect(self._refresh_timeline_scroll_overlay)
        scrollbar.setStyleSheet(operblock_horizontal_scrollbar_style("OperBlockTimelineScrollBar"))
        scrollbar.hide()
        return scrollbar

    def _create_order_label_scrollbar(self) -> QScrollBar:
        scrollbar = QScrollBar(Qt.Vertical, self.plot_widget)
        scrollbar.setObjectName("OperBlockOrderLabelScrollBar")
        scrollbar.setFixedWidth(self.ORDER_LABEL_SCROLLBAR_WIDTH_PX)
        scrollbar.setSingleStep(1)
        scrollbar.valueChanged.connect(self._on_order_label_scroll_value_changed)
        scrollbar.sliderReleased.connect(self._refresh_order_label_scroll_overlay)
        scrollbar.setStyleSheet(_operblock_vertical_scrollbar_style("OperBlockOrderLabelScrollBar"))
        scrollbar.hide()
        return scrollbar

    def _timeline_viewport_hours(self, total_hours: float | None = None) -> float:
        total = max(1.0, float(total_hours if total_hours is not None else self._timeline_total_hours))
        return min(total, float(self.TIMELINE_VIEWPORT_HOURS))

    def _timeline_max_view_left(self, total_hours: float | None = None) -> float:
        total = max(1.0, float(total_hours if total_hours is not None else self._timeline_total_hours))
        return max(0.0, total - self._timeline_viewport_hours(total))

    def _timeline_view_is_at_latest(self, total_hours: float | None = None) -> bool:
        return abs(float(getattr(self, "_timeline_view_left", 0.0)) - self._timeline_max_view_left(total_hours)) <= 0.001

    def _current_timeline_view_bounds(self, total_hours: float | None = None) -> tuple[float, float]:
        try:
            left, right = self.plot_widget.getViewBox().viewRange()[0]
            return float(left), float(right)
        except Exception:
            total = max(1.0, float(total_hours if total_hours is not None else self._timeline_total_hours))
            left = min(max(0.0, float(getattr(self, "_timeline_view_left", 0.0))), self._timeline_max_view_left(total))
            return left, left + self._timeline_viewport_hours(total)

    def _apply_timeline_view_window(
        self,
        *,
        left: float | None = None,
        follow_latest: bool = False,
        sync_scrollbar: bool = True,
    ) -> bool:
        total = max(1.0, float(self._timeline_total_hours))
        window = self._timeline_viewport_hours(total)
        max_left = self._timeline_max_view_left(total)
        next_left = max_left if follow_latest else float(self._timeline_view_left if left is None else left)
        next_left = min(max(0.0, next_left), max_left)
        next_right = next_left + window
        view_left, view_right = self._current_timeline_view_bounds(total)
        changed = abs(view_left - next_left) > 0.001 or abs(view_right - next_right) > 0.001
        self._timeline_view_left = next_left
        if changed:
            self.plot_widget.setXRange(next_left, next_right, padding=0)
        if sync_scrollbar:
            self._sync_timeline_scrollbar()
        return changed

    def _sync_timeline_scrollbar(self) -> None:
        scrollbar = getattr(self, "_timeline_scrollbar", None)
        if scrollbar is None:
            return
        total = max(1.0, float(self._timeline_total_hours))
        window = self._timeline_viewport_hours(total)
        max_left = self._timeline_max_view_left(total)
        units = max(1, int(self.TIMELINE_SCROLL_UNITS_PER_HOUR))
        max_value = int(round(max_left * units))
        value = int(round(min(max(0.0, self._timeline_view_left), max_left) * units))
        self._timeline_scrollbar_syncing = True
        try:
            scrollbar.setPageStep(max(1, int(round(window * units))))
            scrollbar.setSingleStep(1)
            scrollbar.setRange(0, max_value)
            scrollbar.setValue(min(value, max_value))
            scrollbar.setVisible(max_value > 0)
        finally:
            self._timeline_scrollbar_syncing = False

    def _sync_order_label_scrollbar_geometry(self) -> None:
        scrollbar = getattr(self, "_order_label_scrollbar", None)
        if scrollbar is None or not getattr(self, "plot_widget", None):
            return
        try:
            vb = self.plot_widget.getViewBox()
            top_point = self.plot_widget.mapFromScene(vb.mapViewToScene(pg.Point(0, self.VITAL_AXIS_MIN)))
            bottom_point = self.plot_widget.mapFromScene(vb.mapViewToScene(pg.Point(0, self.MEDICATION_BAND_MIN)))
            top = int(round(min(top_point.y(), bottom_point.y())))
            bottom = int(round(max(top_point.y(), bottom_point.y())))
        except Exception:
            top = int(round(self.plot_widget.height() * 0.62))
            bottom = self.plot_widget.height() - 4
        width = int(self.ORDER_LABEL_SCROLLBAR_WIDTH_PX)
        height = max(44, bottom - top)
        x = max(0, self.plot_widget.width() - width - 2)
        scrollbar.setGeometry(x, top, width, height)
        scrollbar.raise_()

    def _order_label_text_height_px(self) -> float:
        try:
            return max(10.0, float(self._order_label_metrics.tightBoundingRect("50 мг").height() + 2))
        except Exception:
            return max(10.0, float(self.ORDER_LABEL_FONT_SIZE + 2))

    def _order_label_visible_lane_count(self, total_lanes: int | None = None) -> int:
        total = max(0, int(total_lanes if total_lanes is not None else self._order_label_total_lanes))
        fallback = max(1, int(self.ORDER_LABEL_LANE_COUNT))
        probe_count = max(fallback, total)
        try:
            vb = self.plot_widget.getViewBox()
            bottom_scene_y = float(vb.mapViewToScene(pg.Point(0, self.MEDICATION_BAND_MIN)).y())
            bottom_limit = bottom_scene_y - float(self.ORDER_LABEL_BOTTOM_MARGIN_PX)
            text_height = self._order_label_text_height_px()
            visible = 0
            for lane in range(probe_count):
                label_y = self.order_label_y_for_lane(lane)
                label_top = float(vb.mapViewToScene(pg.Point(0, label_y)).y())
                if label_top + text_height <= bottom_limit + 0.001:
                    visible = lane + 1
                    continue
                break
            if visible <= 0:
                visible = 1
        except Exception:
            visible = fallback
        if total > 0:
            return max(1, min(total, visible))
        return max(1, visible)

    def _sync_order_label_scrollbar(self, total_lanes: int | None = None) -> None:
        scrollbar = getattr(self, "_order_label_scrollbar", None)
        if scrollbar is None:
            return
        total = max(0, int(total_lanes if total_lanes is not None else self._order_label_total_lanes))
        visible_lanes = self._order_label_visible_lane_count(total)
        max_value = max(0, total - visible_lanes)
        current = min(max(0, int(getattr(self, "_order_label_scroll_top_lane", 0))), max_value)
        self._order_label_total_lanes = total
        self._order_label_scroll_top_lane = current
        self._order_label_scrollbar_syncing = True
        try:
            scrollbar.setPageStep(visible_lanes)
            scrollbar.setSingleStep(1)
            scrollbar.setRange(0, max_value)
            scrollbar.setValue(current)
            scrollbar.setVisible(max_value > 0)
            self._sync_order_label_scrollbar_geometry()
        finally:
            self._order_label_scrollbar_syncing = False

    @staticmethod
    def _wheel_lane_delta(event) -> int:
        pixel_delta = event.pixelDelta().y() if hasattr(event, "pixelDelta") else 0
        if pixel_delta:
            return -1 if pixel_delta > 0 else 1
        angle_delta = event.angleDelta().y() if hasattr(event, "angleDelta") else 0
        if not angle_delta:
            return 0
        return -1 * (1 if angle_delta > 0 else -1) * max(1, int(round(abs(angle_delta) / 120)))

    def _handle_order_label_wheel_event(self, event) -> bool:
        try:
            local_pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
            scene_pos = self.plot_widget.mapToScene(local_pos)
            mouse_point = self.plot_widget.getViewBox().mapSceneToView(scene_pos)
        except Exception:
            return False
        if not self.is_medication_tooltip_zone(mouse_point.y()):
            return False
        scrollbar = getattr(self, "_order_label_scrollbar", None)
        if scrollbar is None or scrollbar.maximum() <= scrollbar.minimum():
            event.accept()
            return True
        delta = self._wheel_lane_delta(event)
        if delta:
            next_value = min(max(scrollbar.minimum(), scrollbar.value() + delta), scrollbar.maximum())
            if next_value != scrollbar.value():
                scrollbar.setValue(next_value)
        event.accept()
        return True

    def _on_timeline_scroll_user_started(self) -> None:
        self._timeline_auto_follow_latest = False

    def _on_timeline_scroll_value_changed(self, value: int) -> None:
        if self._timeline_scrollbar_syncing:
            return
        scrollbar = getattr(self, "_timeline_scrollbar", None)
        if scrollbar is not None:
            self._timeline_auto_follow_latest = int(value) >= max(0, scrollbar.maximum() - 1)
        units = max(1, int(self.TIMELINE_SCROLL_UNITS_PER_HOUR))
        if self._apply_timeline_view_window(left=float(value) / units, sync_scrollbar=False):
            self._order_marker_render_signature = None
            self._timeline_scroll_refresh_timer.start(self.TIMELINE_SCROLL_OVERLAY_REFRESH_MS)
            self.header_spacer.update()

    def _refresh_timeline_scroll_overlay(self) -> None:
        self._order_marker_render_signature = None
        self._render_order_markers(force=True)

    def _on_order_label_scroll_value_changed(self, value: int) -> None:
        if self._order_label_scrollbar_syncing:
            return
        next_value = max(0, int(value))
        if next_value == int(getattr(self, "_order_label_scroll_top_lane", 0)):
            return
        self._order_label_scroll_top_lane = next_value
        self._order_marker_render_signature = None
        self._order_label_scroll_refresh_timer.start(self.TIMELINE_SCROLL_OVERLAY_REFRESH_MS)

    def _refresh_order_label_scroll_overlay(self) -> None:
        self._order_marker_render_signature = None
        self._render_order_markers(force=True)

    def _reset_timeline_scroll_state(self) -> None:
        self._timeline_auto_follow_latest = True
        self._timeline_view_left = 0.0
        self._timeline_total_hours = max(1.0, float(getattr(self, "visible_hours", OPERBLOCK_INITIAL_CHART_HOURS)))
        self._apply_timeline_view_window(left=0.0)
        self._order_label_scroll_top_lane = 0
        self._sync_order_label_scrollbar(0)

    def clear_for_context(self, *, admission_id=None, start_time: datetime = None):
        super().clear_for_context(admission_id=admission_id, start_time=start_time)
        self._reset_timeline_scroll_state()
        self.set_operation_timeline_model(None, force=True)
        self.set_timeline_snapshot(None, start_time, force=True)

    def set_operation_timeline_model(
        self,
        model: _OperBlockTimelineTransform | None,
        *,
        force: bool = False,
    ) -> None:
        old_signature = self._timeline_transform.signature() if self._timeline_transform else None
        new_signature = model.signature() if model else None
        self._timeline_transform = model
        if force or old_signature != new_signature:
            self._order_marker_render_signature = None
            self._last_render_key = None

    def update_data(self, vitals, start_time: datetime, active_intervals=None):
        transform = self._timeline_transform
        if not transform or not transform.display_origin_at:
            return super().update_data(vitals, start_time, active_intervals=active_intervals)

        display_vitals = []
        for vital in list(vitals or []):
            timestamp = getattr(vital, "timestamp", None)
            display_timestamp = transform.display_datetime_for_vital(timestamp)
            if display_timestamp is None:
                continue
            display_vitals.append(_OperBlockDisplayVital(vital, display_timestamp))

        display_intervals = self._display_active_intervals(active_intervals, transform)
        super().update_data(display_vitals, transform.display_origin_at, active_intervals=display_intervals)
        self.vitals_data = vitals

    @staticmethod
    def _display_active_intervals(active_intervals, transform: _OperBlockTimelineTransform):
        if not active_intervals:
            return active_intervals
        result = []
        for start_dt, end_dt in active_intervals:
            display_start = transform.display_datetime_for(start_dt)
            display_end = transform.display_datetime_for(end_dt)
            if display_start is not None and display_end is not None:
                result.append((display_start, display_end))
        return result

    def set_operation_orders(self, rows: list[dict], start_time: datetime | None, *, force: bool = False):
        self._timeline_snapshot = None
        self._order_marker_rows = [dict(row or {}) for row in (rows or [])[: self.ORDER_MARKER_RECREATE_LIMIT]]
        self._infusion_interval_rows = []
        self._operation_stage_rows = []
        self._order_marker_start = start_time if isinstance(start_time, datetime) else None
        self._rebuild_order_marker_style_map()
        self._render_order_markers(force=force)

    def set_timeline_snapshot(self, snapshot: dict | None, start_time: datetime | None, *, force: bool = False):
        self._timeline_snapshot = dict(snapshot or {}) if snapshot else None
        self._order_marker_rows = self.marker_rows_from_timeline_snapshot(self._timeline_snapshot)
        self._infusion_interval_rows = self.infusion_rows_from_timeline_snapshot(self._timeline_snapshot)
        self._operation_stage_rows = self.operation_stage_rows_from_timeline_snapshot(self._timeline_snapshot)
        self._order_marker_start = start_time if isinstance(start_time, datetime) else None
        self._rebuild_order_marker_style_map()
        self._render_order_markers(force=force)

    @staticmethod
    def _operation_stage_marker_key(row: dict) -> str:
        source_id = str((row or {}).get("source_id") or "").strip()
        if source_id:
            return f"timeline_event:{source_id}"
        event_id = str((row or {}).get("id") or "").strip()
        if event_id:
            return event_id
        return "|".join(
            (
                str((row or {}).get("stage_kind") or ""),
                str((row or {}).get("event_time") or ""),
                str((row or {}).get("display_label") or ""),
            )
        )

    @staticmethod
    def _operation_stage_display_label(row: dict) -> str:
        stage_kind = str((row or {}).get("stage_kind") or "").strip()
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if stage_kind == "custom":
            return str(
                (payload or {}).get("label")
                or (row or {}).get("display_label")
                or (row or {}).get("raw_text")
                or "Этап операции"
            )
        return str(OperBlockChartWidget.OPERATION_STAGE_LABELS.get(stage_kind) or (row or {}).get("display_label") or "Этап")

    @staticmethod
    def _operation_stage_sort_id(row: dict) -> int:
        for value in ((row or {}).get("source_id"), (row or {}).get("id")):
            text = str(value or "").strip()
            if not text:
                continue
            try:
                return int(text)
            except (TypeError, ValueError):
                match = re.search(r"(\d+)$", text)
                if match:
                    return int(match.group(1))
        return 0

    @classmethod
    def _operation_stage_row_sort_key(cls, row: dict) -> tuple[datetime, int]:
        return (
            _parse_datetime_value((row or {}).get("event_time")) or datetime.min,
            cls._operation_stage_sort_id(row),
        )

    @staticmethod
    def _join_operation_stage_labels(labels: list[str]) -> str:
        cleaned = [re.sub(r"\s+", " ", str(label or "").strip()) for label in labels]
        return ", ".join(label for label in cleaned if label) or "Этап операции"

    def _operation_stage_label_half_width_hours(self) -> float:
        return max(0.0, self._hours_for_plot_pixels(self._operation_start_label_metrics.height() + 10.0) / 2.0)

    def _visible_operation_stage_marker_specs(
        self,
        start: datetime,
        visible_hours: float,
        rows: list[dict] | None = None,
    ) -> list[dict]:
        specs: list[dict] = []
        view_left, view_right = self._current_timeline_view_bounds(visible_hours)
        half_width = self._operation_stage_label_half_width_hours()
        for row in sorted(list(rows if rows is not None else getattr(self, "_operation_stage_rows", []) or []), key=self._operation_stage_row_sort_key):
            stage_kind = str((row or {}).get("stage_kind") or "").strip()
            event_dt = _minute_floor_dt(_parse_datetime_value((row or {}).get("event_time")))
            if event_dt is None:
                continue
            x = self._display_x_for_time(event_dt, start)
            if x is None or x < -0.001 or x > visible_hours + 0.001:
                continue
            if x < view_left - 0.001 or x > view_right + 0.001:
                continue
            label_text = re.sub(r"\s+", " ", self._operation_stage_display_label(row).strip())
            if not label_text:
                continue
            label_x, anchor = self._operation_start_label_position(x, label_text)
            specs.append(
                {
                    "row": dict(row or {}),
                    "event_dt": event_dt,
                    "x": float(x),
                    "label_x": float(label_x),
                    "label_left": float(label_x) - half_width,
                    "label_right": float(label_x) + half_width,
                    "anchor": anchor,
                    "label": label_text,
                    "color": str(self.OPERATION_STAGE_COLORS.get(stage_kind) or "#506174"),
                    "marker_key": self._operation_stage_marker_key(row),
                    "sort_id": self._operation_stage_sort_id(row),
                }
            )
        return specs

    def _operation_stage_marker_clusters(self, specs: list[dict]) -> list[list[dict]]:
        clusters: list[list[dict]] = []
        cluster_right_edges: list[float] = []
        gap = self._hours_for_plot_pixels(2.0)
        for spec in sorted(specs or [], key=lambda item: (float(item.get("label_left") or 0.0), item.get("event_dt") or datetime.min, int(item.get("sort_id") or 0))):
            left = float(spec.get("label_left") or 0.0)
            right = float(spec.get("label_right") or left)
            if clusters and left <= cluster_right_edges[-1] + gap:
                clusters[-1].append(spec)
                cluster_right_edges[-1] = max(cluster_right_edges[-1], right)
                continue
            clusters.append([spec])
            cluster_right_edges.append(right)
        for cluster in clusters:
            cluster.sort(key=lambda item: (item.get("event_dt") or datetime.min, int(item.get("sort_id") or 0)))
        return clusters

    @staticmethod
    def _operation_stage_cluster_marker_keys(cluster: list[dict]) -> set[str]:
        return {str(spec.get("marker_key") or "").strip() for spec in (cluster or []) if str(spec.get("marker_key") or "").strip()}

    def _operation_stage_cluster_keys_for_marker_keys(
        self,
        rows: list[dict],
        start: datetime,
        visible_hours: float,
        marker_keys: set[str],
    ) -> set[str]:
        wanted = {str(key or "").strip() for key in (marker_keys or []) if str(key or "").strip()}
        if not wanted:
            return set()
        affected: set[str] = set()
        for cluster in self._operation_stage_marker_clusters(
            self._visible_operation_stage_marker_specs(start, visible_hours, rows)
        ):
            cluster_keys = self._operation_stage_cluster_marker_keys(cluster)
            if cluster_keys & wanted:
                affected.update(cluster_keys)
        return affected

    def _remove_operation_stage_marker_items(self, marker_key: str) -> None:
        self._remove_operation_stage_marker_items_for_keys({marker_key})

    def _remove_operation_stage_marker_items_for_keys(self, marker_keys: set[str] | list[str] | tuple[str, ...]) -> None:
        keys = {str(key or "").strip() for key in (marker_keys or []) if str(key or "").strip()}
        if not keys:
            return
        marker_items = []
        marker_map = getattr(self, "_operation_stage_marker_items_by_key", {}) or {}
        for key in keys:
            marker_items.extend(list(marker_map.pop(key, []) or []))
        if not marker_items:
            return
        marker_item_ids = {id(item) for item in marker_items}
        for item in marker_items:
            try:
                self.plot_widget.removeItem(item)
            except Exception:
                pass
        for key, items in list(marker_map.items()):
            kept_items = [item for item in list(items or []) if id(item) not in marker_item_ids]
            if kept_items:
                marker_map[key] = kept_items
            else:
                marker_map.pop(key, None)
        self._order_marker_items = [
            item for item in list(getattr(self, "_order_marker_items", []) or []) if id(item) not in marker_item_ids
        ]

    def _append_operation_stage_marker_item(self, marker_key: str, item) -> None:
        self._append_operation_stage_marker_item_for_keys({marker_key}, item)

    def _append_operation_stage_marker_item_for_keys(self, marker_keys: set[str] | list[str] | tuple[str, ...], item) -> None:
        self._order_marker_items.append(item)
        for marker_key in {str(key or "").strip() for key in (marker_keys or []) if str(key or "").strip()}:
            self._operation_stage_marker_items_by_key.setdefault(marker_key, []).append(item)

    def patch_operation_stage_marker(
        self,
        stage_event: dict,
        *,
        snapshot: dict | None = None,
        start_time: datetime | None = None,
    ) -> bool:
        rows = self.operation_stage_rows_from_timeline_snapshot({"operation_events": [dict(stage_event or {})]})
        if not rows:
            return False
        row = rows[0]
        marker_key = self._operation_stage_marker_key(row)
        if not marker_key:
            return False
        if snapshot is not None:
            self._timeline_snapshot = dict(snapshot or {})
        if isinstance(start_time, datetime):
            self._order_marker_start = start_time

        previous_rows = list(getattr(self, "_operation_stage_rows", []) or [])
        patched_rows: list[dict] = []
        replaced = False
        for existing in previous_rows:
            if self._operation_stage_marker_key(existing) == marker_key:
                if not replaced:
                    patched_rows.append(row)
                    replaced = True
                continue
            patched_rows.append(existing)
        if not replaced:
            patched_rows.append(row)
        patched_rows.sort(key=self._operation_stage_row_sort_key)
        self._operation_stage_rows = patched_rows
        start = self._order_marker_start if isinstance(self._order_marker_start, datetime) else None
        if start is None or not getattr(self, "plot_widget", None):
            self._remove_operation_stage_marker_items(marker_key)
            self._order_marker_render_signature = self._current_order_marker_signature()
            return True
        visible_hours = max(1.0, float(getattr(self, "visible_hours", OPERBLOCK_INITIAL_CHART_HOURS)))
        normalized_start = start.replace(second=0, microsecond=0)
        seed_keys = {marker_key}
        affected_keys = set(seed_keys)
        affected_keys.update(
            self._operation_stage_cluster_keys_for_marker_keys(previous_rows, normalized_start, visible_hours, seed_keys)
        )
        affected_keys.update(
            self._operation_stage_cluster_keys_for_marker_keys(patched_rows, normalized_start, visible_hours, seed_keys)
        )
        self._remove_operation_stage_marker_items_for_keys(affected_keys)
        rendered_clusters: set[tuple[str, ...]] = set()
        for cluster in self._operation_stage_marker_clusters(
            self._visible_operation_stage_marker_specs(normalized_start, visible_hours, patched_rows)
        ):
            cluster_keys = self._operation_stage_cluster_marker_keys(cluster)
            if not cluster_keys or not (cluster_keys & affected_keys):
                continue
            cluster_key = tuple(sorted(cluster_keys))
            if cluster_key in rendered_clusters:
                continue
            rendered_clusters.add(cluster_key)
            self._render_operation_stage_marker_group(
                normalized_start,
                visible_hours,
                cluster,
                gaps_by_x=getattr(self, "_last_operation_stage_gaps_by_x", {}) or {},
            )
        self._order_marker_render_signature = self._current_order_marker_signature()
        return True

    @classmethod
    def build_operation_timeline_transform(
        cls,
        operation_started_at: datetime | None,
        vitals,
    ) -> _OperBlockTimelineTransform:
        timestamps = sorted(
            _minute_floor_dt(getattr(vital, "timestamp", None))
            for vital in (vitals or [])
            if _minute_floor_dt(getattr(vital, "timestamp", None)) is not None
        )
        baseline_at = timestamps[0] if timestamps else None
        operation_start_at = _minute_floor_dt(operation_started_at)
        return _OperBlockTimelineTransform(
            baseline_at=baseline_at,
            operation_start_at=operation_start_at,
        )

    @classmethod
    def marker_rows_from_timeline_snapshot(cls, snapshot: dict | None) -> list[dict]:
        events = list((snapshot or {}).get("bolus_events") or [])
        rows: list[dict] = []
        for event in events[: cls.ORDER_MARKER_RECREATE_LIMIT]:
            data = dict(event or {})
            data["datetime"] = data.get("event_time")
            data["text"] = data.get("raw_text") or data.get("display_label") or ""
            rows.append(data)
        return rows

    @classmethod
    def marker_y_for_stack_index(cls, index: int) -> float:
        return cls.ORDER_MARKER_Y + min(max(0, int(index)), 5) * cls.ORDER_MARKER_Y_STEP

    @classmethod
    def order_label_y_for_lane(cls, lane: int) -> float:
        return cls.ORDER_LABEL_Y - max(0, int(lane)) * cls.ORDER_LABEL_Y_STEP

    def _medication_detail_lift_units(self) -> float:
        return self._vital_units_for_plot_pixels(self.MEDICATION_DETAIL_LIFT_PX)

    def _dose_label_y_for_lane(self, lane: int) -> float:
        return self.order_label_y_for_lane(lane) + self._medication_detail_lift_units()

    def _dose_marker_y_for_lane(self, lane: int) -> float:
        return (
            self.order_label_y_for_lane(lane)
            - self._vital_units_for_plot_pixels(self.ORDER_DOSE_MARKER_OFFSET_PX)
            + self._medication_detail_lift_units()
        )

    def _infusion_rate_label_y_for_lane(self, lane: int) -> float:
        return self.order_label_y_for_lane(lane) + self._medication_detail_lift_units()

    def _infusion_bar_y_for_lane(self, lane: int) -> float:
        return (
            self.order_label_y_for_lane(lane)
            - self._vital_units_for_plot_pixels(self.INFUSION_BAR_OFFSET_PX)
            + self._medication_detail_lift_units()
        )

    @classmethod
    def infusion_rows_from_timeline_snapshot(cls, snapshot: dict | None) -> list[dict]:
        intervals = list((snapshot or {}).get("infusion_intervals") or [])
        return [dict(interval or {}) for interval in intervals[: cls.INFUSION_INTERVAL_RECREATE_LIMIT]]

    @classmethod
    def operation_stage_rows_from_timeline_snapshot(cls, snapshot: dict | None) -> list[dict]:
        events = list((snapshot or {}).get("operation_events") or [])
        rows: list[dict] = []
        for event in events:
            data = dict(event or {})
            payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
            stage_kind = str((payload or {}).get("stage_kind") or (payload or {}).get("operation_stage") or "").strip()
            if not stage_kind:
                continue
            data["stage_kind"] = stage_kind
            rows.append(data)
        return rows

    @classmethod
    def infusion_lane_y_for_index(cls, index: int) -> float:
        return cls.INFUSION_LANE_Y + min(max(0, int(index)), 5) * cls.INFUSION_LANE_STEP

    def _display_x_for_time(self, event_dt: datetime | None, fallback_start: datetime | None = None) -> float | None:
        transform = self._timeline_transform
        if transform is not None:
            return transform.display_hours_for(event_dt, fallback_start=fallback_start)
        if event_dt is None or fallback_start is None:
            return None
        return (event_dt - fallback_start).total_seconds() / 3600.0

    def _display_time_for_x(self, x: float, fallback_start: datetime | None = None) -> datetime | None:
        if self._timeline_transform is not None:
            return self._timeline_transform.real_datetime_for_display_hours(x, fallback_start=fallback_start)
        origin = fallback_start
        if origin is None:
            return None
        return origin + timedelta(hours=float(x or 0.0))

    def header_datetime_for_x(self, x: float) -> datetime | None:
        return self._display_time_for_x(x, self.start_time)

    def _label_width_hours(self, label_text: str) -> float:
        visible_range = self.plot_widget.getViewBox().viewRange()[0]
        view_width_hours = max(0.1, float(visible_range[1] - visible_range[0]))
        scene_width = max(1.0, float(self.plot_widget.getViewBox().sceneBoundingRect().width() or 1.0))
        cache_key = str(label_text or "")
        if cache_key not in self._order_label_width_cache:
            self._order_label_width_cache[cache_key] = float(self._order_label_metrics.horizontalAdvance(cache_key) + 8)
        return (self._order_label_width_cache[cache_key] / scene_width) * view_width_hours

    def _hours_for_plot_pixels(self, pixels: float) -> float:
        visible_range = self.plot_widget.getViewBox().viewRange()[0]
        view_width_hours = max(0.1, float(visible_range[1] - visible_range[0]))
        scene_width = max(1.0, float(self.plot_widget.getViewBox().sceneBoundingRect().width() or 1.0))
        return (float(pixels or 0.0) / scene_width) * view_width_hours

    def _vital_units_for_plot_pixels(self, pixels: float) -> float:
        visible_range = self.plot_widget.getViewBox().viewRange()[1]
        view_height = max(0.1, float(visible_range[1] - visible_range[0]))
        scene_height = max(1.0, float(self.plot_widget.getViewBox().sceneBoundingRect().height() or 1.0))
        return (float(pixels or 0.0) / scene_height) * view_height

    @classmethod
    def boundary_aware_label_placement(
        cls,
        x: float,
        *,
        view_left: float,
        view_right: float,
        label_width_hours: float,
        margin_hours: float,
    ) -> tuple[float, tuple[float, float]]:
        left = float(view_left)
        right = max(left + 0.001, float(view_right))
        width = max(0.0, float(label_width_hours or 0.0))
        margin = max(0.0, float(margin_hours or 0.0))
        center_x = float(x or 0.0)
        if center_x - width / 2.0 < left + margin:
            return min(right, center_x + margin), (0.0, 0.0)
        if center_x + width / 2.0 > right - margin:
            return max(left, center_x - margin), (1.0, 0.0)
        return center_x, (0.5, 0.0)

    def _order_label_position(self, x: float, label_text: str) -> tuple[float, tuple[float, float]]:
        view_left, view_right = self.plot_widget.getViewBox().viewRange()[0]
        return self.boundary_aware_label_placement(
            x,
            view_left=float(view_left),
            view_right=float(view_right),
            label_width_hours=self._label_width_hours(label_text),
            margin_hours=self._hours_for_plot_pixels(self.ORDER_LABEL_EDGE_MARGIN_PX),
        )

    @staticmethod
    def _label_bounds(label_x: float, anchor: tuple[float, float], label_width_hours: float) -> tuple[float, float]:
        anchor_x = float(anchor[0] if anchor else 0.5)
        width = max(0.0, float(label_width_hours or 0.0))
        if anchor_x <= 0.01:
            return float(label_x), float(label_x) + width
        if anchor_x >= 0.99:
            return float(label_x) - width, float(label_x)
        return float(label_x) - width / 2.0, float(label_x) + width / 2.0

    @staticmethod
    def _ranges_overlap(left_a: float, right_a: float, left_b: float, right_b: float, margin: float = 0.0) -> bool:
        return float(left_a) < float(right_b) + float(margin) and float(right_a) > float(left_b) - float(margin)

    def _label_overlaps_name_column(
        self,
        *,
        label_x: float,
        label_anchor: tuple[float, float],
        label_text: str,
        name_x: float,
        name_anchor: tuple[float, float],
        drug_text: str,
    ) -> bool:
        label_left, label_right = self._label_bounds(
            label_x,
            label_anchor,
            self._label_width_hours(label_text),
        )
        name_left, name_right = self._label_bounds(
            name_x,
            name_anchor,
            self._label_width_hours(drug_text),
        )
        return self._ranges_overlap(
            label_left,
            label_right,
            name_left,
            name_right,
            self._hours_for_plot_pixels(self.ORDER_LABEL_COLUMN_GUARD_PX),
        )

    def _name_column_bounds(
        self,
        *,
        name_x: float,
        name_anchor: tuple[float, float],
        drug_text: str,
    ) -> tuple[float, float]:
        name_left, name_right = self._label_bounds(
            name_x,
            name_anchor,
            self._label_width_hours(drug_text),
        )
        guard = self._hours_for_plot_pixels(self.ORDER_LABEL_COLUMN_GUARD_PX)
        return name_left - guard, name_right + guard

    def _name_column_overlaps_value(
        self,
        *,
        x_start: float,
        x_end: float,
        name_x: float,
        name_anchor: tuple[float, float],
        drug_text: str,
    ) -> bool:
        name_left, name_right = self._name_column_bounds(
            name_x=name_x,
            name_anchor=name_anchor,
            drug_text=drug_text,
        )
        return self._ranges_overlap(float(x_start), float(x_end), name_left, name_right)

    def _trim_line_segment_to_name_column(
        self,
        *,
        x_start: float,
        x_end: float,
        name_x: float,
        name_anchor: tuple[float, float],
        drug_text: str,
    ) -> tuple[float, float] | None:
        start = float(x_start)
        end = float(x_end)
        if end <= start:
            return None
        name_left, name_right = self._name_column_bounds(
            name_x=name_x,
            name_anchor=name_anchor,
            drug_text=drug_text,
        )
        if not self._ranges_overlap(start, end, name_left, name_right):
            return start, end
        trimmed_start = max(start, name_right)
        if trimmed_start >= end - 0.0001:
            return None
        return trimmed_start, end

    def _bolus_dose_clusters_for_group(self, group: dict) -> list[dict]:
        dose_items: list[dict] = []
        for entry in list(group.get("entries") or []):
            dose_text = re.sub(r"\s+", " ", str(entry.get("dose_text") or "").strip())
            if not dose_text:
                continue
            item_row = entry.get("row") if isinstance(entry.get("row"), dict) else {}
            single_dose_text = _order_dose_text_with_route(dose_text, item_row, short=True)
            marker_x = float(entry.get("x") if entry.get("x") is not None else entry.get("dose_x") or 0.0)
            dose_x = float(entry.get("dose_x") if entry.get("dose_x") is not None else marker_x)
            dose_anchor = entry.get("dose_anchor") or (0.5, 0.0)
            label_left, label_right = self._label_bounds(
                dose_x,
                dose_anchor,
                self._label_width_hours(single_dose_text or dose_text),
            )
            dose_items.append(
                {
                    **entry,
                    "dose_text": dose_text,
                    "single_dose_text": single_dose_text,
                    "marker_x": marker_x,
                    "dose_x": dose_x,
                    "dose_anchor": dose_anchor,
                    "label_left": label_left,
                    "label_right": label_right,
                }
            )
        if not dose_items:
            return []

        dose_items.sort(key=lambda item: (float(item.get("label_left") or 0.0), float(item.get("marker_x") or 0.0)))
        overlap_margin = self._hours_for_plot_pixels(1.0)
        raw_clusters: list[list[dict]] = []
        current: list[dict] = []
        current_right = float("-inf")
        for item in dose_items:
            item_left = float(item.get("label_left") or 0.0)
            item_right = float(item.get("label_right") or item_left)
            if current and item_left > current_right + overlap_margin:
                raw_clusters.append(current)
                current = []
                current_right = float("-inf")
            current.append(item)
            current_right = max(current_right, item_right)
        if current:
            raw_clusters.append(current)

        clusters: list[dict] = []
        for items in raw_clusters:
            dose_texts = [str(item.get("dose_text") or "") for item in items]
            if len(items) == 1:
                summary_text = str(items[0].get("single_dose_text") or dose_texts[0])
            else:
                summary_text = _summarize_dose_texts(dose_texts) or ", ".join(dict.fromkeys(dose_texts))
            marker_values = [float(item.get("marker_x") or 0.0) for item in items]
            marker_x = sum(marker_values) / len(marker_values)
            dose_label_x, dose_anchor = self._order_label_position(marker_x, summary_text)
            label_left, label_right = self._label_bounds(
                dose_label_x,
                dose_anchor,
                self._label_width_hours(summary_text),
            )
            rows = [dict(item.get("row") or {}) for item in items if isinstance(item.get("row"), dict)]
            datetimes = []
            for row in rows:
                parsed_dt = _minute_floor_dt(_parse_datetime_value(row.get("datetime")))
                if parsed_dt is not None:
                    datetimes.append(parsed_dt)
            clusters.append(
                {
                    "entries": items,
                    "rows": rows,
                    "dose_text": summary_text,
                    "marker_x": marker_x,
                    "dose_x": dose_label_x,
                    "dose_anchor": dose_anchor,
                    "label_left": label_left,
                    "label_right": label_right,
                    "source_xs": marker_values,
                    "datetime": min(datetimes) if datetimes else None,
                    "datetime_end": max(datetimes) if datetimes else None,
                    "symbol": items[0].get("symbol") or "o",
                    "style": items[0].get("style"),
                }
            )
        return clusters

    @staticmethod
    def _guideline_gap_key(x: float) -> float:
        return round(float(x or 0.0), 6)

    @staticmethod
    def _merged_vertical_gaps(
        y_min: float,
        y_max: float,
        gaps: list[tuple[float, float]] | tuple[tuple[float, float], ...],
    ) -> list[tuple[float, float]]:
        bottom = min(float(y_min), float(y_max))
        top = max(float(y_min), float(y_max))
        normalized: list[tuple[float, float]] = []
        for raw_low, raw_high in gaps or []:
            low = max(bottom, min(top, float(raw_low)))
            high = max(bottom, min(top, float(raw_high)))
            if high <= low:
                continue
            normalized.append((low, high))
        normalized.sort(key=lambda item: (item[0], item[1]))

        merged: list[tuple[float, float]] = []
        for low, high in normalized:
            if not merged or low > merged[-1][1] + 0.001:
                merged.append((low, high))
            else:
                prev_low, prev_high = merged[-1]
                merged[-1] = (prev_low, max(prev_high, high))
        return merged

    @classmethod
    def vertical_guideline_segments_around_gaps(
        cls,
        y_min: float,
        y_max: float,
        gaps: list[tuple[float, float]] | tuple[tuple[float, float], ...],
    ) -> list[tuple[float, float]]:
        bottom = min(float(y_min), float(y_max))
        top = max(float(y_min), float(y_max))
        segments: list[tuple[float, float]] = []
        current = bottom
        for low, high in cls._merged_vertical_gaps(bottom, top, gaps):
            if low > current + 0.001:
                segments.append((current, low))
            current = max(current, high)
        if current < top - 0.001:
            segments.append((current, top))
        return segments

    def _dose_label_gap_for_y(self, y: float, label_text: str) -> tuple[float, float]:
        padding = self._vital_units_for_plot_pixels(self.ORDER_GUIDELINE_GAP_PADDING_PX)
        text_height = self._vital_units_for_plot_pixels(max(1.0, float(self._order_label_metrics.height() + 4)))
        top = float(y) + padding
        bottom = float(y) - text_height - padding
        return bottom, top

    def _dose_point_gap_for_y(self, y: float) -> tuple[float, float]:
        radius = self._vital_units_for_plot_pixels(
            float(self.ORDER_DOSE_MARKER_SIZE) / 2.0 + self.ORDER_GUIDELINE_GAP_PADDING_PX
        )
        return float(y) - radius, float(y) + radius

    @staticmethod
    def _compact_order_text_item(item: pg.TextItem) -> pg.TextItem:
        text_item = getattr(item, "textItem", None)
        document = text_item.document() if text_item is not None else None
        if document is not None:
            document.setDocumentMargin(0)
        return item

    def _remember_guideline_gap(
        self,
        gaps_by_x: dict[float, list[tuple[float, float]]],
        x: float,
        gap: tuple[float, float],
    ) -> None:
        key = self._guideline_gap_key(x)
        gaps_by_x.setdefault(key, []).append(gap)

    @staticmethod
    def _merge_guideline_gap_maps(*maps: dict[float, list[tuple[float, float]]] | None) -> dict[float, list[tuple[float, float]]]:
        merged: dict[float, list[tuple[float, float]]] = {}
        for gap_map in maps:
            for x_key, gaps in (gap_map or {}).items():
                merged.setdefault(float(x_key), []).extend(list(gaps or []))
        return merged

    def _text_item_view_bounds(self, item: pg.TextItem) -> tuple[float, float, float, float] | None:
        try:
            rect = item.sceneBoundingRect()
            if rect.isEmpty():
                return None
            vb = self.plot_widget.getViewBox()
            center_y = rect.center().y()
            center_x = rect.center().x()
            left_x = vb.mapSceneToView(pg.Point(rect.left(), center_y)).x()
            right_x = vb.mapSceneToView(pg.Point(rect.right(), center_y)).x()
            top_y = vb.mapSceneToView(pg.Point(center_x, rect.top())).y()
            bottom_y = vb.mapSceneToView(pg.Point(center_x, rect.bottom())).y()
            return (
                min(float(left_x), float(right_x)),
                max(float(left_x), float(right_x)),
                min(float(top_y), float(bottom_y)),
                max(float(top_y), float(bottom_y)),
            )
        except Exception:
            return None

    def _remember_text_item_guideline_gaps(
        self,
        gaps_by_x: dict[float, list[tuple[float, float]]],
        item: pg.TextItem,
        x_values,
    ) -> None:
        bounds = self._text_item_view_bounds(item)
        if bounds is None:
            return
        x_left, x_right, y_bottom, y_top = bounds
        x_padding = self._hours_for_plot_pixels(self.ORDER_GUIDELINE_GAP_PADDING_PX)
        y_padding = self._vital_units_for_plot_pixels(self.ORDER_GUIDELINE_GAP_PADDING_PX)
        for raw_x in x_values or []:
            try:
                x = float(raw_x)
            except Exception:
                continue
            if x_left - x_padding <= x <= x_right + x_padding:
                self._remember_guideline_gap(
                    gaps_by_x,
                    x,
                    (y_bottom - y_padding, y_top + y_padding),
                )

    def _order_guideline_gaps_for_groups(
        self,
        groups_with_lanes: list[tuple[dict, int]],
        *,
        lane_map: dict[str, int] | None = None,
    ) -> dict[float, list[tuple[float, float]]]:
        gaps_by_x: dict[float, list[tuple[float, float]]] = {}
        visible_lanes = self._order_label_visible_lane_count()
        scroll_top_lane = max(0, int(getattr(self, "_order_label_scroll_top_lane", 0)))
        for group, allocated_lane in groups_with_lanes:
            if str(group.get("group_kind") or "") != "bolus":
                continue
            absolute_lane = int((lane_map or {}).get(str(group.get("drug_key") or ""), allocated_lane))
            if absolute_lane < scroll_top_lane or absolute_lane >= scroll_top_lane + visible_lanes:
                continue
            lane = absolute_lane - scroll_top_lane
            dose_label_y = self._dose_label_y_for_lane(lane)
            marker_y = self._dose_marker_y_for_lane(lane)
            name_x = float(group.get("name_x") or 0.0)
            name_anchor = group.get("name_anchor") or (0.0, 0.0)
            drug_text = str(group.get("drug_text") or "Назначение")
            for cluster in self._bolus_dose_clusters_for_group(group):
                dose_text = str(cluster.get("dose_text") or "").strip()
                if not dose_text:
                    continue
                marker_x = float(cluster.get("marker_x") if cluster.get("marker_x") is not None else cluster.get("dose_x") or 0.0)
                dose_x = float(cluster.get("dose_x") if cluster.get("dose_x") is not None else marker_x)
                dose_anchor = cluster.get("dose_anchor") or (0.5, 0.0)
                marker_overlaps_name = self._name_column_overlaps_value(
                    x_start=marker_x,
                    x_end=marker_x,
                    name_x=name_x,
                    name_anchor=name_anchor,
                    drug_text=drug_text,
                )
                label_overlaps_name = self._label_overlaps_name_column(
                    label_x=dose_x,
                    label_anchor=dose_anchor,
                    label_text=dose_text,
                    name_x=name_x,
                    name_anchor=name_anchor,
                    drug_text=drug_text,
                )
                if marker_overlaps_name or label_overlaps_name:
                    self._remember_guideline_gap(gaps_by_x, marker_x, (self.MEDICATION_BAND_MIN, self.VITAL_AXIS_MIN))
                    continue
                self._remember_guideline_gap(gaps_by_x, marker_x, self._dose_point_gap_for_y(marker_y))
                self._remember_guideline_gap(gaps_by_x, dose_x, self._dose_label_gap_for_y(dose_label_y, dose_text))
                if abs(dose_x - marker_x) <= 0.0001:
                    self._remember_guideline_gap(gaps_by_x, marker_x, self._dose_label_gap_for_y(dose_label_y, dose_text))
                label_left = float(cluster.get("label_left") or dose_x)
                label_right = float(cluster.get("label_right") or dose_x)
                for source_x in list(cluster.get("source_xs") or []):
                    source_x = float(source_x)
                    if label_left <= source_x <= label_right:
                        self._remember_guideline_gap(gaps_by_x, source_x, self._dose_label_gap_for_y(dose_label_y, dose_text))
        return gaps_by_x

    def _render_order_guidelines(self, guideline_specs: list[dict], gaps_by_x: dict[float, list[tuple[float, float]]]) -> None:
        y_min = float(self.MEDICATION_BAND_MIN)
        y_max = float(self.VITAL_AXIS_MAX)
        for spec in guideline_specs:
            x = float(spec.get("x") or 0.0)
            color = str(spec.get("color") or "#8e44ad")
            gaps = gaps_by_x.get(self._guideline_gap_key(x), [])
            for y_start, y_end in self.vertical_guideline_segments_around_gaps(y_min, y_max, gaps):
                line = pg.PlotDataItem(
                    [x, x],
                    [y_start, y_end],
                    pen=pg.mkPen(color, width=1, style=Qt.DotLine),
                )
                line.setZValue(self.ORDER_GUIDELINE_Z)
                setattr(line, "_operblock_guideline_segment", True)
                self.plot_widget.addItem(line)
                self._order_marker_items.append(line)

        for x_key, gaps in (gaps_by_x or {}).items():
            for y_start, y_end in self._merged_vertical_gaps(y_min, y_max, gaps):
                mask = pg.PlotDataItem(
                    [float(x_key), float(x_key)],
                    [y_start, y_end],
                    pen=pg.mkPen("#ffffff", width=int(self.ORDER_GUIDELINE_MASK_WIDTH_PX)),
                )
                mask.setZValue(self.ORDER_GUIDELINE_MASK_Z)
                setattr(mask, "_operblock_guideline_mask", True)
                self.plot_widget.addItem(mask)
                self._order_marker_items.append(mask)

    def _order_name_column_position(self) -> tuple[float, tuple[float, float]]:
        view_left = float(self.plot_widget.getViewBox().viewRange()[0][0])
        return view_left + self._hours_for_plot_pixels(self.ORDER_NAME_COLUMN_MARGIN_PX), (0.0, 0.0)

    def _operation_start_label_y(self) -> float:
        return self.VITAL_AXIS_MAX - self._vital_units_for_plot_pixels(4.0)

    def _operation_start_label_position(self, x: float, label_text: str) -> tuple[float, tuple[float, float]]:
        _ = label_text
        view_left, view_right = self.plot_widget.getViewBox().viewRange()[0]
        margin = self._hours_for_plot_pixels(self.ORDER_LABEL_EDGE_MARGIN_PX)
        vertical_width = self._hours_for_plot_pixels(self._operation_start_label_metrics.height() + 8)
        current_x = float(x or 0.0)
        if current_x - vertical_width / 2.0 < float(view_left) + margin:
            return min(float(view_right), current_x + margin), (0.0, 1.0)
        if current_x + vertical_width / 2.0 > float(view_right) - margin:
            return max(float(view_left), current_x - margin), (0.0, 0.0)
        return current_x, (0.0, 0.5)

    @classmethod
    def allocate_order_label_lanes(
        cls,
        entries: list[dict],
        *,
        lane_count: int | None = None,
        gap_hours: float = 0.0,
    ) -> list[int]:
        count = max(1, int(lane_count or cls.ORDER_LABEL_LANE_COUNT))
        lane_right_edges = [float("-inf")] * count
        lanes: list[int] = []
        for entry in entries:
            left = float(entry.get("label_left") or 0.0)
            right = float(entry.get("label_right") or left)
            selected = None
            for lane_index, current_right in enumerate(lane_right_edges):
                if left >= current_right + gap_hours:
                    selected = lane_index
                    break
            if selected is None:
                selected = min(range(count), key=lambda index: lane_right_edges[index])
            lane_right_edges[selected] = max(lane_right_edges[selected], right)
            lanes.append(selected)
        return lanes

    @staticmethod
    def order_label_group_key(kind: str, drug_key: str | None = None, drug_text: str | None = None) -> str:
        normalized_kind = str(kind or "bolus").strip().casefold() or "bolus"
        normalized_key = str(drug_key or drug_text or "").strip().casefold()
        if not normalized_key:
            normalized_key = "unknown"
        return f"{normalized_kind}:{normalized_key}"

    @classmethod
    def order_label_groups_for_entries(
        cls,
        entries: list[dict],
        *,
        lane_count: int | None = None,
        gap_hours: float = 0.0,
    ) -> list[tuple[dict, int]]:
        sorted_entries = sorted(entries, key=lambda item: (float(item.get("x") or 0.0), int(item.get("row_index") or 0)))
        groups_by_key: dict[str, dict] = {}
        groups: list[dict] = []
        for entry in sorted_entries:
            kind = str(entry.get("group_kind") or "bolus").strip().casefold() or "bolus"
            key = cls.order_label_group_key(kind, entry.get("drug_key"), entry.get("drug_text"))
            if not key:
                key = f"unknown:{len(groups)}"
            group = groups_by_key.get(key)
            if group is None:
                group = {
                    "drug_key": key,
                    "group_kind": kind,
                    "drug_text": str(entry.get("drug_text") or "Назначение"),
                    "name_x": entry.get("name_x"),
                    "name_anchor": entry.get("name_anchor"),
                    "style": entry.get("style"),
                    "interval": entry.get("interval"),
                    "rate_text": entry.get("rate_text"),
                    "rate_x": entry.get("rate_x"),
                    "rate_anchor": entry.get("rate_anchor"),
                    "x_start": entry.get("x_start"),
                    "x_end": entry.get("x_end"),
                    "visible_hours": entry.get("visible_hours"),
                    "start_time": entry.get("start_time"),
                    "entries": [],
                    "label_left": float(entry.get("label_left") or 0.0),
                    "label_right": float(entry.get("label_right") or 0.0),
                    "first_x": float(entry.get("x") or 0.0),
                }
                groups_by_key[key] = group
                groups.append(group)
            group["entries"].append(entry)
            group["label_left"] = min(float(group.get("label_left") or 0.0), float(entry.get("label_left") or 0.0))
            group["label_right"] = max(float(group.get("label_right") or 0.0), float(entry.get("label_right") or 0.0))
        lanes = cls.allocate_order_label_lanes(groups, lane_count=lane_count, gap_hours=gap_hours)
        return list(zip(groups, lanes))

    def _stable_order_label_lane_map(self, start: datetime, visible_hours: float) -> dict[str, int]:
        groups: dict[str, dict] = {}

        def remember(kind: str, drug_key: str, drug_text: str, x: float, row_index: int) -> None:
            key = self.order_label_group_key(kind, drug_key, drug_text)
            current = groups.get(key)
            first_x = float(x)
            current_order = (
                float(current.get("first_x") or 0.0),
                int(current.get("row_index") or 0),
            ) if current is not None else None
            if current_order is None or (first_x, row_index) < current_order:
                groups[key] = {"first_x": first_x, "row_index": int(row_index)}

        for row_index, row in enumerate(self._order_marker_rows):
            order_dt = _minute_floor_dt(_parse_datetime_value(row.get("datetime")))
            if order_dt is None:
                continue
            x = self._display_x_for_time(order_dt, start)
            if x is None or x < -0.001 or x > visible_hours + 0.001:
                continue
            drug_text, _dose_text = self._order_marker_label_parts(row)
            drug_key = re.sub(r"\s+", " ", drug_text.casefold()).strip() or self._order_marker_drug_key_from_row(row)
            remember("bolus", drug_key, drug_text, x, row_index)

        for index, interval in enumerate(getattr(self, "_infusion_interval_rows", []) or []):
            bounds = self._infusion_bar_display_bounds(interval, start, visible_hours)
            if bounds is None:
                continue
            x_start, _x_end = bounds
            fallback_drug_text = "Газ" if _is_gas_infusion(interval) else "Дозатор"
            drug_text = _infusion_display_drug_name(interval, fallback_drug_text)
            drug_key = self._infusion_order_label_key(interval)
            remember("infusion", drug_key, drug_text, x_start, 10_000 + index)

        stable_lanes: dict[str, int] = {}
        sorted_keys = sorted(
            groups,
            key=lambda item: (
                float(groups[item].get("first_x") or 0.0),
                int(groups[item].get("row_index") or 0),
                item,
            ),
        )
        for index, key in enumerate(sorted_keys):
            stable_lanes[key] = index
        self._sync_order_label_scrollbar(len(sorted_keys))
        return stable_lanes

    @staticmethod
    def infusion_bar_bounds(
        interval: dict,
        start_time: datetime,
        visible_hours: int | float,
        *,
        now: datetime | None = None,
    ) -> tuple[float, float] | None:
        start_dt = _minute_floor_dt(_parse_datetime_value(interval.get("start_time")))
        if start_dt is None:
            return None
        end_dt = _minute_floor_dt(_parse_datetime_value(interval.get("end_time")))
        if end_dt is None and str(interval.get("status") or "") == "active":
            end_dt = _minute_floor_dt(now or datetime.now())
        if end_dt is None or end_dt <= start_dt:
            end_dt = start_dt + timedelta(minutes=1)
        x_start = (start_dt - start_time).total_seconds() / 3600.0
        x_end = (end_dt - start_time).total_seconds() / 3600.0
        right = max(1.0, float(visible_hours or 1))
        if x_end < -0.001 or x_start > right + 0.001:
            return None
        return max(0.0, x_start), min(right, max(x_end, x_start + 1.0 / 60.0))

    def _infusion_bar_display_bounds(
        self,
        interval: dict,
        start_time: datetime,
        visible_hours: int | float,
        *,
        now: datetime | None = None,
    ) -> tuple[float, float] | None:
        transform = self._timeline_transform
        if transform is None:
            return self.infusion_bar_bounds(interval, start_time, visible_hours, now=now)
        start_dt = _minute_floor_dt(_parse_datetime_value(interval.get("start_time")))
        if start_dt is None:
            return None
        end_dt = _minute_floor_dt(_parse_datetime_value(interval.get("end_time")))
        if end_dt is None and str(interval.get("status") or "") == "active":
            end_dt = _minute_floor_dt(now or datetime.now())
        if end_dt is None or end_dt <= start_dt:
            end_dt = start_dt + timedelta(minutes=1)
        x_start = transform.display_hours_for(start_dt, fallback_start=start_time)
        x_end = transform.display_hours_for(end_dt, fallback_start=start_time)
        if x_start is None or x_end is None:
            return None
        right = max(1.0, float(visible_hours or 1))
        if x_end < -0.001 or x_start > right + 0.001:
            return None
        return max(0.0, x_start), min(right, max(x_end, x_start + 1.0 / 60.0))

    @staticmethod
    def _infusion_rate_periods(
        interval: dict,
        *,
        now: datetime | None = None,
    ) -> list[dict]:
        start_dt = _minute_floor_dt(_parse_datetime_value(interval.get("start_time")))
        if start_dt is None:
            return []
        end_dt = _minute_floor_dt(_parse_datetime_value(interval.get("end_time")))
        if end_dt is None and str(interval.get("status") or "") == "active":
            end_dt = _minute_floor_dt(now or datetime.now())
        if end_dt is None or end_dt <= start_dt:
            end_dt = start_dt + timedelta(minutes=1)

        if _is_gas_infusion(interval or {}):
            dose_events = [event for event in _gas_dose_events(interval or {}) if event["event_time"] <= end_dt]
            if not dose_events:
                return []
            if dose_events[0]["event_time"] > start_dt:
                dose_events.insert(0, {"event_time": start_dt, "dose_text": dose_events[0]["dose_text"]})

            periods: list[dict] = []
            for index, event in enumerate(dose_events):
                period_start = max(start_dt, event["event_time"])
                next_dt = dose_events[index + 1]["event_time"] if index + 1 < len(dose_events) else end_dt
                period_end = min(end_dt, next_dt)
                if period_end <= period_start:
                    continue
                periods.append(
                    {
                        "period_start": period_start,
                        "period_end": period_end,
                        "rate_text": str(event.get("dose_text") or ""),
                    }
                )
            return periods

        events = [event for event in _infusion_rate_events(interval) if event["event_time"] <= end_dt]
        if not events:
            return []
        if events[0]["event_time"] > start_dt:
            events.insert(0, {"event_time": start_dt, "rate_value": events[0]["rate_value"]})

        periods: list[dict] = []
        for index, event in enumerate(events):
            period_start = max(start_dt, event["event_time"])
            next_dt = events[index + 1]["event_time"] if index + 1 < len(events) else end_dt
            period_end = min(end_dt, next_dt)
            if period_end <= period_start:
                continue
            periods.append(
                {
                    "period_start": period_start,
                    "period_end": period_end,
                    "rate_text": _format_infusion_rate(event.get("rate_value"), OPERBLOCK_INFUSION_RATE_UNIT),
                }
            )
        return periods

    def _infusion_rate_display_segments(
        self,
        interval: dict,
        start_time: datetime,
        visible_hours: int | float,
    ) -> list[dict]:
        right = max(1.0, float(visible_hours or 1))
        periods = self._infusion_rate_periods(interval)
        if not periods:
            return []
        gap_hours = self._hours_for_plot_pixels(self.INFUSION_SEGMENT_GAP_PX)
        result: list[dict] = []
        for index, period in enumerate(periods):
            x_start = self._display_x_for_time(period.get("period_start"), start_time)
            x_end = self._display_x_for_time(period.get("period_end"), start_time)
            if x_start is None or x_end is None:
                continue
            if x_end < -0.001 or x_start > right + 0.001:
                continue
            visible_x_start = max(0.0, float(x_start))
            visible_x_end = min(right, max(float(x_end), float(x_start) + 1.0 / 60.0))
            draw_x_start = visible_x_start
            draw_x_end = visible_x_end
            segment_width = max(0.0, draw_x_end - draw_x_start)
            if segment_width > gap_hours * 2.5 and len(periods) > 1:
                half_gap = gap_hours / 2.0
                if index > 0:
                    draw_x_start += half_gap
                if index < len(periods) - 1:
                    draw_x_end -= half_gap
            if draw_x_end <= draw_x_start:
                continue
            result.append(
                {
                    "x_start": draw_x_start,
                    "x_end": draw_x_end,
                    "label_x_start": visible_x_start,
                    "label_x_end": visible_x_end,
                    "rate_text": str(period.get("rate_text") or ""),
                    "period_start": period.get("period_start"),
                    "period_end": period.get("period_end"),
                }
            )
        return result

    def _clear_order_markers(self):
        for item in list(getattr(self, "_order_marker_items", [])):
            try:
                self.plot_widget.removeItem(item)
            except Exception:
                pass
        self._order_marker_items = []
        self._operation_stage_marker_items_by_key = {}
        self._order_marker_groups = []
        self._infusion_interval_groups = []

    def _render_order_markers(self, *, force: bool = False):
        signature = self._current_order_marker_signature()
        if not force and signature == self._order_marker_render_signature:
            return
        self._order_marker_render_signature = signature
        self._clear_order_markers()
        if not getattr(self, "plot_widget", None) or not self._order_marker_start:
            self._sync_order_label_scrollbar(0)
            return

        start = self._order_marker_start.replace(second=0, microsecond=0)
        visible_hours = max(1.0, float(getattr(self, "visible_hours", OPERBLOCK_INITIAL_CHART_HOURS)))
        view_left, view_right = self._current_timeline_view_bounds(visible_hours)
        grouped: dict[datetime, list[dict]] = {}
        label_entries: list[dict] = []
        guideline_specs: list[dict] = []
        for row in self._order_marker_rows:
            order_dt = _minute_floor_dt(_parse_datetime_value(row.get("datetime")))
            if order_dt is None:
                continue
            x = self._display_x_for_time(order_dt, start)
            if x is None:
                continue
            if x < -0.001 or x > visible_hours + 0.001:
                continue
            if x < view_left - 0.001 or x > view_right + 0.001:
                continue
            grouped.setdefault(order_dt, []).append(row)

        for order_dt in sorted(grouped):
            rows = sorted(
                grouped[order_dt],
                key=lambda item: (
                    _parse_datetime_value(item.get("datetime")) or datetime.min,
                    self._order_marker_sort_id(item),
                ),
            )
            x = self._display_x_for_time(order_dt, start)
            if x is None:
                continue
            group = {"datetime": order_dt, "x": x, "rows": rows}
            self._order_marker_groups.append(group)
            group_styles = [self._order_marker_style_for_row(row) for row in rows]
            line_color = group_styles[0]["color"] if group_styles else "#8e44ad"
            guideline_specs.append({"x": x, "color": line_color})

            for row_index, row in enumerate(rows):
                drug_text, dose_text = self._order_marker_label_parts(row)
                dose_text_for_bounds = _order_dose_text_with_route(dose_text, row, short=True) if dose_text else ""
                drug_key = re.sub(r"\s+", " ", drug_text.casefold()).strip() or self._order_marker_drug_key_from_row(row)
                name_x, name_anchor = self._order_name_column_position()
                dose_x, dose_anchor = self._order_label_position(x, dose_text_for_bounds or dose_text or drug_text)
                name_width = self._label_width_hours(drug_text)
                name_left, name_right = self._label_bounds(name_x, name_anchor, name_width)
                if dose_text_for_bounds or dose_text:
                    dose_left, dose_right = self._label_bounds(
                        dose_x,
                        dose_anchor,
                        self._label_width_hours(dose_text_for_bounds or dose_text),
                    )
                else:
                    dose_left, dose_right = name_left, name_right
                label_entries.append(
                    {
                        "x": x,
                        "row_index": row_index,
                        "group_kind": "bolus",
                        "drug_key": drug_key,
                        "drug_text": drug_text,
                        "dose_text": dose_text,
                        "symbol": self._order_marker_style_for_row(row).get("symbol"),
                        "name_x": name_x,
                        "dose_x": dose_x,
                        "label_left": min(name_left, dose_left),
                        "label_right": max(name_right, dose_right),
                        "name_anchor": name_anchor,
                        "dose_anchor": dose_anchor,
                        "style": self._order_marker_style_for_row(row),
                        "row": dict(row),
                    }
                )
        label_entries.extend(self._infusion_label_entries(start, visible_hours))
        lane_map = self._stable_order_label_lane_map(start, visible_hours)
        groups_with_lanes = self.order_label_groups_for_entries(
            label_entries,
            lane_count=self._order_label_visible_lane_count(len(lane_map)),
            gap_hours=self._hours_for_plot_pixels(self.ORDER_LABEL_LANE_GAP_PX),
        )
        guideline_gaps = self._order_guideline_gaps_for_groups(groups_with_lanes, lane_map=lane_map)
        visual_gaps = self._render_order_label_entries(
            label_entries,
            lane_map=lane_map,
            groups_with_lanes=groups_with_lanes,
        )
        all_guideline_gaps = self._merge_guideline_gap_maps(guideline_gaps, visual_gaps)
        self._last_operation_stage_gaps_by_x = dict(all_guideline_gaps or {})
        self._render_operation_stage_markers(start, visible_hours, gaps_by_x=all_guideline_gaps)
        self._render_order_guidelines(guideline_specs, all_guideline_gaps)

    def _infusion_label_entries(self, start: datetime, visible_hours: float) -> list[dict]:
        entries: list[dict] = []
        view_left, view_right = self._current_timeline_view_bounds(visible_hours)
        intervals = sorted(
            list(getattr(self, "_infusion_interval_rows", []) or []),
            key=lambda item: (_parse_datetime_value(item.get("start_time")) or datetime.max, str(item.get("interval_id") or "")),
        )
        for index, interval in enumerate(intervals[: self.INFUSION_INTERVAL_RECREATE_LIMIT]):
            bounds = self._infusion_bar_display_bounds(interval, start, visible_hours)
            if bounds is None:
                continue
            x_start, x_end = bounds
            if x_end < view_left - 0.001 or x_start > view_right + 0.001:
                continue
            style = self._order_marker_style_for_key(self._infusion_drug_key(interval))
            drug_text = _infusion_display_drug_name(interval, "Дозатор")
            drug_key = self._infusion_order_label_key(interval)
            name_x, name_anchor = self._order_name_column_position()
            name_left, name_right = self._label_bounds(name_x, name_anchor, self._label_width_hours(drug_text))
            segments = self._infusion_rate_display_segments(interval, start, visible_hours)
            if not segments:
                label_text = _gas_dose_text(interval) if _is_gas_infusion(interval) else _format_infusion_declared_volume(interval)
                segments = [
                    {
                        "x_start": max(x_start, view_left),
                        "x_end": min(x_end, view_right),
                        "rate_text": label_text,
                    }
                ]
            for segment_index, segment in enumerate(segments):
                segment_x_start = float(segment.get("x_start") or 0.0)
                segment_x_end = float(segment.get("x_end") or segment_x_start)
                if segment_x_end < view_left - 0.001 or segment_x_start > view_right + 0.001:
                    continue
                visible_x_start = max(segment_x_start, view_left)
                visible_x_end = min(segment_x_end, view_right)
                rate_text = str(segment.get("rate_text") or "").strip()
                rate_x, rate_anchor = self._order_label_position(
                    (visible_x_start + visible_x_end) / 2.0,
                    rate_text or drug_text,
                )
                rate_left, rate_right = self._label_bounds(
                    rate_x,
                    rate_anchor,
                    self._label_width_hours(rate_text or drug_text),
                )
                entries.append(
                    {
                        "x": segment_x_start,
                        "row_index": 10_000 + index * 100 + segment_index,
                        "group_kind": "infusion",
                        "drug_key": drug_key,
                        "drug_text": drug_text,
                        "rate_text": rate_text,
                        "name_x": name_x,
                        "rate_x": rate_x,
                        "x_start": visible_x_start,
                        "x_end": visible_x_end,
                        "label_left": min(name_left, rate_left, visible_x_start),
                        "label_right": max(name_right, rate_right, visible_x_end),
                        "name_anchor": name_anchor,
                        "rate_anchor": rate_anchor,
                        "style": style,
                        "interval": dict(interval),
                        "visible_hours": visible_hours,
                        "start_time": start,
                    }
                )
        return entries

    def _render_order_label_entries(
        self,
        entries: list[dict],
        *,
        lane_map: dict[str, int] | None = None,
        groups_with_lanes: list[tuple[dict, int]] | None = None,
    ) -> dict[float, list[tuple[float, float]]]:
        visual_gaps: dict[float, list[tuple[float, float]]] = {}
        if not entries:
            return visual_gaps
        if groups_with_lanes is None:
            groups_with_lanes = self.order_label_groups_for_entries(
                entries,
                lane_count=self._order_label_visible_lane_count(),
                gap_hours=self._hours_for_plot_pixels(self.ORDER_LABEL_LANE_GAP_PX),
            )
        visible_lanes = self._order_label_visible_lane_count()
        scroll_top_lane = max(0, int(getattr(self, "_order_label_scroll_top_lane", 0)))
        for group, allocated_lane in groups_with_lanes:
            absolute_lane = int((lane_map or {}).get(str(group.get("drug_key") or ""), allocated_lane))
            if absolute_lane < scroll_top_lane or absolute_lane >= scroll_top_lane + visible_lanes:
                continue
            lane = absolute_lane - scroll_top_lane
            if str(group.get("group_kind") or "") == "infusion":
                self._render_infusion_label_group(group, lane)
                continue
            drug_text = str(group.get("drug_text") or "Назначение")
            style = group.get("style") if isinstance(group.get("style"), dict) else None
            y = self.order_label_y_for_lane(lane)

            drug_label = pg.TextItem(
                html=self._order_marker_label_html(drug_text, style),
                anchor=group.get("name_anchor") or (0.0, 0.0),
            )
            self._compact_order_text_item(drug_label)
            drug_label.setZValue(110 + lane)
            name_x = float(group.get("name_x") or 0.0)
            name_anchor = group.get("name_anchor") or (0.0, 0.0)
            drug_label.setPos(name_x, y)
            self.plot_widget.addItem(drug_label)
            self._order_marker_items.append(drug_label)

            for dose_entry in self._bolus_dose_clusters_for_group(group):
                dose_text = str(dose_entry.get("dose_text") or "").strip()
                if not dose_text:
                    continue
                dose_x = float(dose_entry.get("marker_x") if dose_entry.get("marker_x") is not None else dose_entry.get("dose_x") or 0.0)
                dose_label_x = float(dose_entry.get("dose_x") if dose_entry.get("dose_x") is not None else dose_x)
                dose_anchor = dose_entry.get("dose_anchor") or (0.5, 0.0)
                dose_label_y = self._dose_label_y_for_lane(lane)
                marker_y = self._dose_marker_y_for_lane(lane)
                marker_overlaps_name = self._name_column_overlaps_value(
                    x_start=dose_x,
                    x_end=dose_x,
                    name_x=name_x,
                    name_anchor=name_anchor,
                    drug_text=drug_text,
                )
                label_overlaps_name = self._label_overlaps_name_column(
                    label_x=dose_label_x,
                    label_anchor=dose_anchor,
                    label_text=dose_text,
                    name_x=name_x,
                    name_anchor=name_anchor,
                    drug_text=drug_text,
                )
                if marker_overlaps_name or label_overlaps_name:
                    self._remember_guideline_gap(visual_gaps, dose_x, (self.MEDICATION_BAND_MIN, self.VITAL_AXIS_MIN))
                    continue
                point = pg.ScatterPlotItem(
                    x=[dose_x],
                    y=[marker_y],
                    symbol=dose_entry.get("symbol") or "o",
                    size=self.ORDER_DOSE_MARKER_SIZE,
                    brush=pg.mkBrush(str((style or {}).get("color") or "#8e44ad")),
                    pen=pg.mkPen("#ffffff", width=1),
                )
                point.setZValue(80 + lane)
                self.plot_widget.addItem(point)
                self._order_marker_items.append(point)

                source_xs = [float(value) for value in list(dose_entry.get("source_xs") or [dose_x])]
                self._remember_guideline_gap(visual_gaps, dose_x, self._dose_point_gap_for_y(marker_y))
                self._order_marker_groups.append(
                    {
                        "datetime": dose_entry.get("datetime"),
                        "datetime_end": dose_entry.get("datetime_end"),
                        "x": dose_x,
                        "x_start": min(source_xs + [dose_x]),
                        "x_end": max(source_xs + [dose_x]),
                        "hit_x_start": min(source_xs + [float(dose_entry.get("label_left") or dose_label_x), dose_x]),
                        "hit_x_end": max(source_xs + [float(dose_entry.get("label_right") or dose_label_x), dose_x]),
                        "y": y,
                        "dose_y": dose_label_y,
                        "marker_y": marker_y,
                        "drug_text": drug_text,
                        "summary_text": dose_text if len(dose_entry.get("rows") or []) > 1 else "",
                        "rows": list(dose_entry.get("rows") or []),
                    }
                )

                dose_label = pg.TextItem(
                    html=self._order_marker_label_html(dose_text, style),
                    anchor=dose_anchor,
                    fill=pg.mkBrush("#ffffff"),
                )
                self._compact_order_text_item(dose_label)
                dose_label.setZValue(85 + lane)
                dose_label.setPos(dose_label_x, dose_label_y)
                self.plot_widget.addItem(dose_label)
                self._order_marker_items.append(dose_label)
                self._remember_text_item_guideline_gaps(
                    visual_gaps,
                    dose_label,
                    list(dict.fromkeys([*source_xs, dose_x, dose_label_x])),
                )
        return visual_gaps

    def _render_infusion_label_group(self, group: dict, lane: int) -> None:
        drug_text = str(group.get("drug_text") or "Дозатор")
        style = group.get("style") if isinstance(group.get("style"), dict) else None
        color = str((style or {}).get("color") or "#16a085")
        y = self.order_label_y_for_lane(lane)
        rate_label_y = self._infusion_rate_label_y_for_lane(lane)
        bar_y = self._infusion_bar_y_for_lane(lane)

        drug_label = pg.TextItem(
            html=self._order_marker_label_html(drug_text, style),
            anchor=group.get("name_anchor") or (0.0, 0.0),
        )
        self._compact_order_text_item(drug_label)
        drug_label.setZValue(110 + lane)
        name_x = float(group.get("name_x") or 0.0)
        name_anchor = group.get("name_anchor") or (0.0, 0.0)
        drug_label.setPos(name_x, y)
        self.plot_widget.addItem(drug_label)
        self._order_marker_items.append(drug_label)

        entries = sorted(
            list(group.get("entries") or []),
            key=lambda item: (float(item.get("x_start") or 0.0), int(item.get("row_index") or 0)),
        )
        if not entries:
            entries = [group]
        entries = self._infusion_entries_with_segment_gaps(
            entries,
            edge_trim_hours=self._hours_for_plot_pixels(self.INFUSION_BOTTLE_EDGE_TRIM_PX),
            min_width_hours=self._hours_for_plot_pixels(1.0),
        )
        rate_label_candidates: list[dict] = []
        for entry in entries:
            x_start = float(entry.get("x_start") or 0.0)
            x_end = float(entry.get("x_end") or x_start)
            trimmed_segment = self._trim_line_segment_to_name_column(
                x_start=x_start,
                x_end=x_end,
                name_x=name_x,
                name_anchor=name_anchor,
                drug_text=drug_text,
            )
            if trimmed_segment is None:
                continue
            x_start, x_end = trimmed_segment

            bar_mask_padding_hours = self._hours_for_plot_pixels(self.ORDER_GUIDELINE_GAP_PADDING_PX)
            bar_mask = pg.PlotDataItem(
                [x_start - bar_mask_padding_hours, x_end + bar_mask_padding_hours],
                [bar_y, bar_y],
                pen=pg.mkPen(
                    "#ffffff",
                    width=max(1.0, self.INFUSION_BAR_WIDTH + self.ORDER_GUIDELINE_GAP_PADDING_PX * 2.0),
                ),
            )
            bar_mask.setZValue(self.INFUSION_BAR_MASK_Z)
            self.plot_widget.addItem(bar_mask)
            self._order_marker_items.append(bar_mask)

            bar = self._rounded_infusion_bar_item(x_start, x_end, bar_y, color)
            bar.setZValue(self.INFUSION_BAR_Z)
            self.plot_widget.addItem(bar)
            self._order_marker_items.append(bar)

            rate_text = str(entry.get("rate_text") or "").strip()
            if not rate_text:
                rate_x = x_start
                rate_anchor = (0.5, 0.0)
            else:
                rate_x = float(entry.get("rate_x") if entry.get("rate_x") is not None else x_start)
                rate_anchor = entry.get("rate_anchor") or (0.5, 0.0)
                if rate_x < x_start or rate_x > x_end:
                    rate_x, rate_anchor = self._order_label_position((x_start + x_end) / 2.0, rate_text)
            if rate_text and not self._label_overlaps_name_column(
                label_x=rate_x,
                label_anchor=rate_anchor,
                label_text=rate_text,
                name_x=name_x,
                name_anchor=name_anchor,
                drug_text=drug_text,
            ):
                rate_left, rate_right = self._label_bounds(rate_x, rate_anchor, self._label_width_hours(rate_text))
                rate_label_candidates.append(
                    {
                        "text": rate_text,
                        "x": rate_x,
                        "anchor": rate_anchor,
                        "left": rate_left,
                        "right": rate_right,
                        "x_start": x_start,
                        "x_end": x_end,
                    }
                )

            self._infusion_interval_groups.append(
                {
                    "interval": dict(entry.get("interval") or group.get("interval") or {}),
                    "rate_text": str(entry.get("rate_text") or ""),
                    "x_start": x_start,
                    "x_end": x_end,
                    "y": bar_y,
                    "rate_label_y": rate_label_y,
                    "color": color,
                }
            )
        for label_spec in self._merged_infusion_rate_label_specs(
            rate_label_candidates,
            name_x=name_x,
            name_anchor=name_anchor,
            drug_text=drug_text,
        ):
            label_text = str(label_spec.get("text") or "").strip()
            if not label_text:
                continue
            rate_label = pg.TextItem(
                html=self._order_marker_label_html(label_text, style),
                anchor=label_spec.get("anchor") or (0.5, 0.0),
                fill=pg.mkBrush("#ffffff"),
            )
            self._compact_order_text_item(rate_label)
            rate_label.setZValue(self.INFUSION_RATE_LABEL_Z)
            rate_label.setPos(float(label_spec.get("x") or 0.0), rate_label_y)
            self.plot_widget.addItem(rate_label)
            self._order_marker_items.append(rate_label)

    def _rounded_infusion_bar_item(self, x_start: float, x_end: float, y: float, color: str) -> QGraphicsPathItem:
        height = max(0.0, self._vital_units_for_plot_pixels(self.INFUSION_BAR_WIDTH))
        half_height = height / 2.0
        width = max(0.0, float(x_end) - float(x_start))
        rect = QRectF(float(x_start), float(y) - half_height, width, height)
        radius_x = min(self._hours_for_plot_pixels(self.INFUSION_BAR_CORNER_RADIUS_PX), width / 2.0)
        radius_y = min(self._vital_units_for_plot_pixels(self.INFUSION_BAR_CORNER_RADIUS_PX), height / 2.0)
        path = QPainterPath()
        path.addRoundedRect(rect, radius_x, radius_y)
        item = QGraphicsPathItem(path)
        item.setBrush(pg.mkBrush(color))
        item.setPen(pg.mkPen(None))
        setattr(item, "_operblock_infusion_bar", True)
        setattr(item, "_operblock_infusion_corner_radius_px", float(self.INFUSION_BAR_CORNER_RADIUS_PX))
        return item

    def _merged_infusion_rate_label_specs(
        self,
        candidates: list[dict],
        *,
        name_x: float,
        name_anchor: tuple[float, float],
        drug_text: str,
    ) -> list[dict]:
        if not candidates:
            return []
        sorted_candidates = sorted(
            candidates,
            key=lambda item: (float(item.get("left") or 0.0), float(item.get("x") or 0.0)),
        )
        gap_hours = self._hours_for_plot_pixels(self.INFUSION_RATE_LABEL_CLUSTER_GAP_PX)
        clusters: list[dict] = []
        for candidate in sorted_candidates:
            left = float(candidate.get("left") or 0.0)
            right = float(candidate.get("right") or left)
            if not clusters or left > float(clusters[-1].get("right") or 0.0) + gap_hours:
                cluster = {
                    "items": [candidate],
                    "left": left,
                    "right": right,
                    "x_start": float(candidate.get("x_start") or 0.0),
                    "x_end": float(candidate.get("x_end") or 0.0),
                }
                clusters.append(cluster)
                continue
            cluster = clusters[-1]
            cluster["items"].append(candidate)
            cluster["right"] = max(float(cluster.get("right") or right), right)
            cluster["x_start"] = min(float(cluster.get("x_start") or 0.0), float(candidate.get("x_start") or 0.0))
            cluster["x_end"] = max(float(cluster.get("x_end") or 0.0), float(candidate.get("x_end") or 0.0))

        specs: list[dict] = []
        for cluster in clusters:
            items = list(cluster.get("items") or [])
            if not items:
                continue
            if len(items) == 1:
                item = items[0]
                specs.append({"text": item.get("text"), "x": item.get("x"), "anchor": item.get("anchor")})
                continue
            label_text = _compact_infusion_rate_texts([str(item.get("text") or "") for item in items])
            label_x, label_anchor = self._order_label_position(
                (float(cluster.get("x_start") or 0.0) + float(cluster.get("x_end") or 0.0)) / 2.0,
                label_text,
            )
            if self._label_overlaps_name_column(
                label_x=label_x,
                label_anchor=label_anchor,
                label_text=label_text,
                name_x=name_x,
                name_anchor=name_anchor,
                drug_text=drug_text,
            ):
                continue
            specs.append({"text": label_text, "x": label_x, "anchor": label_anchor})
        return specs

    def _render_operation_stage_markers(
        self,
        start: datetime,
        visible_hours: float,
        *,
        gaps_by_x: dict[float, list[tuple[float, float]]] | None = None,
    ):
        rendered = False
        specs = self._visible_operation_stage_marker_specs(start, visible_hours)
        for cluster in self._operation_stage_marker_clusters(specs):
            if self._render_operation_stage_marker_group(
                start,
                visible_hours,
                cluster,
                gaps_by_x=gaps_by_x,
            ):
                rendered = True
        if rendered:
            return
        self._render_operation_start_marker(start, visible_hours, gaps_by_x=gaps_by_x)

    def _render_operation_stage_marker_group(
        self,
        start: datetime,
        visible_hours: float,
        cluster: list[dict],
        *,
        gaps_by_x: dict[float, list[tuple[float, float]]] | None = None,
    ) -> bool:
        specs = sorted(cluster or [], key=lambda item: (item.get("event_dt") or datetime.min, int(item.get("sort_id") or 0)))
        if not specs:
            return False
        marker_keys = self._operation_stage_cluster_marker_keys(specs)
        drawn_lines: dict[tuple[float, str], set[str]] = {}
        for spec in specs:
            x = float(spec.get("x") or 0.0)
            color = str(spec.get("color") or "#506174")
            spec_key = str(spec.get("marker_key") or "").strip()
            line_key = (self._guideline_gap_key(x), color)
            drawn_lines.setdefault(line_key, set()).add(spec_key)
            if len(drawn_lines[line_key]) > 1:
                continue
            gaps = (gaps_by_x or {}).get(self._guideline_gap_key(x), [])
            for y_start, y_end in self.vertical_guideline_segments_around_gaps(
                self.MEDICATION_BAND_MIN,
                self.VITAL_AXIS_MAX,
                gaps,
            ):
                line = pg.PlotDataItem(
                    [x, x],
                    [y_start, y_end],
                    pen=pg.mkPen(color, width=1, style=Qt.DashLine),
                )
                line.setZValue(32)
                setattr(line, "_operblock_operation_stage_segment", True)
                self.plot_widget.addItem(line)
                self._append_operation_stage_marker_item_for_keys({spec_key}, line)

        label_text = self._join_operation_stage_labels([str(spec.get("label") or "") for spec in specs])
        label_x, anchor = self._operation_start_label_position(float(specs[0].get("x") or 0.0), label_text)
        colors = [str(spec.get("color") or "#506174") for spec in specs]
        label_color = colors[0] if all(color == colors[0] for color in colors) else "#334155"
        label = pg.TextItem(
            html=(
                "<span style='font-family: Segoe UI; "
                f"font-size: {int(self.OPERATION_START_LABEL_FONT_SIZE)}px; font-weight: 700; "
                f"color: {html.escape(label_color)}; padding: 2px 4px;'>{html.escape(label_text)}</span>"
            ),
            anchor=anchor,
            fill=pg.mkBrush("#ffffff"),
            angle=-90,
        )
        label.setZValue(73)
        label.setPos(label_x, self._operation_start_label_y())
        self.plot_widget.addItem(label)
        self._append_operation_stage_marker_item_for_keys(marker_keys, label)
        return True

    def _render_single_operation_stage_marker(
        self,
        start: datetime,
        visible_hours: float,
        event_dt: datetime,
        label_text: str,
        color: str,
        *,
        gaps_by_x: dict[float, list[tuple[float, float]]] | None = None,
        marker_key: str = "",
    ) -> bool:
        x = self._display_x_for_time(event_dt, start)
        if x is None or x < -0.001 or x > visible_hours + 0.001:
            return False
        view_left, view_right = self._current_timeline_view_bounds(visible_hours)
        if x < view_left - 0.001 or x > view_right + 0.001:
            return False
        gaps = (gaps_by_x or {}).get(self._guideline_gap_key(x), [])
        for y_start, y_end in self.vertical_guideline_segments_around_gaps(
            self.MEDICATION_BAND_MIN,
            self.VITAL_AXIS_MAX,
            gaps,
        ):
            line = pg.PlotDataItem(
                [x, x],
                [y_start, y_end],
                pen=pg.mkPen(color, width=1, style=Qt.DashLine),
            )
            line.setZValue(32)
            setattr(line, "_operblock_operation_stage_segment", True)
            self.plot_widget.addItem(line)
            self._append_operation_stage_marker_item(marker_key, line)

        label_x, anchor = self._operation_start_label_position(x, label_text)
        label = pg.TextItem(
            html=(
                "<span style='font-family: Segoe UI; "
                f"font-size: {int(self.OPERATION_START_LABEL_FONT_SIZE)}px; font-weight: 700; "
                f"color: {html.escape(color)}; padding: 2px 4px;'>{html.escape(label_text)}</span>"
            ),
            anchor=anchor,
            fill=pg.mkBrush("#ffffff"),
            angle=-90,
        )
        label.setZValue(73)
        label.setPos(label_x, self._operation_start_label_y())
        self.plot_widget.addItem(label)
        self._append_operation_stage_marker_item(marker_key, label)
        return True

    def _render_operation_start_marker(
        self,
        start: datetime,
        visible_hours: float,
        *,
        gaps_by_x: dict[float, list[tuple[float, float]]] | None = None,
    ):
        transform = self._timeline_transform
        if not transform or not transform.has_preop_gap or not transform.operation_start_at:
            return
        self._render_single_operation_stage_marker(
            start,
            visible_hours,
            transform.operation_start_at,
            self.OPERATION_START_LABEL,
            "#506174",
            gaps_by_x=gaps_by_x,
        )

    def _current_order_marker_signature(self) -> tuple:
        start_key = self._order_marker_start.isoformat() if isinstance(self._order_marker_start, datetime) else None
        visible_hours = round(float(getattr(self, "visible_hours", OPERBLOCK_INITIAL_CHART_HOURS)), 3)
        view_left, view_right = self._current_timeline_view_bounds(visible_hours)
        view_key = (round(view_left, 3), round(view_right, 3))
        rows_key = tuple(
            (
                str(row.get("id") or ""),
                str(row.get("source") or ""),
                int(row.get("source_id") or 0),
                int(row.get("revision") or 0),
                str(row.get("datetime") or ""),
                str(row.get("display_label") or ""),
                str(row.get("text") or row.get("raw_text") or ""),
                str(row.get("route") or ""),
                str(row.get("comment") or ""),
            )
            for row in self._order_marker_rows
        )
        intervals_key = tuple(
            (
                str(row.get("interval_id") or ""),
                str(row.get("status") or ""),
                str(row.get("start_time") or ""),
                str(row.get("end_time") or ""),
                str(row.get("current_rate_value") or ""),
                str(row.get("current_rate_unit") or ""),
                tuple(
                    (
                        str((history or {}).get("event_time") or ""),
                        str((history or {}).get("rate_value") or ""),
                        str((history or {}).get("rate_unit") or ""),
                        str((history or {}).get("revision") or ""),
                    )
                    for history in list(row.get("rate_history") or [])
                ),
                tuple(
                    (
                        str((history or {}).get("event_time") or ""),
                        str((history or {}).get("dose_text") or ""),
                        str((history or {}).get("revision") or ""),
                    )
                    for history in list(row.get("dose_history") or [])
                ),
                str((row.get("payload") or {}).get("start_revision") if isinstance(row.get("payload"), dict) else ""),
                str((row.get("payload") or {}).get("dose_text") if isinstance(row.get("payload"), dict) else ""),
                str((row.get("payload") or {}).get("display_dose_text") if isinstance(row.get("payload"), dict) else ""),
            )
            for row in self._infusion_interval_rows
        )
        stages_key = tuple(
            (
                str(row.get("id") or ""),
                str(row.get("stage_kind") or ""),
                str(row.get("event_time") or ""),
                str(row.get("display_label") or row.get("raw_text") or ""),
                int(row.get("revision") or 0),
            )
            for row in getattr(self, "_operation_stage_rows", []) or []
        )
        active_infusion_minute_key = ""
        if any(str(row.get("status") or "") == "active" for row in self._infusion_interval_rows):
            active_infusion_minute_key = datetime.now().replace(second=0, microsecond=0).isoformat(timespec="minutes")
        timeline_hash = str((self._timeline_snapshot or {}).get("content_hash") or "")
        transform_key = self._timeline_transform.signature() if self._timeline_transform else None
        order_scroll_key = int(getattr(self, "_order_label_scroll_top_lane", 0))
        return (
            start_key,
            visible_hours,
            view_key,
            order_scroll_key,
            timeline_hash,
            transform_key,
            rows_key,
            intervals_key,
            stages_key,
            active_infusion_minute_key,
        )

    def _order_marker_label_text(self, row: dict) -> str:
        label = self._order_marker_display_label(row)
        return re.sub(r"\s+", " ", str(label or "").strip()) or "Назначение"

    def _order_marker_label_parts(self, row: dict) -> tuple[str, str]:
        full_label = self._order_marker_label_text(row)
        drug_label = re.sub(r"\s+", " ", str(row.get("drug_label") or "").strip())
        dose_match = re.search(
            r"(?P<dose>\d+(?:[.,]\d+)?\s*(?:мг|мкг|мл|г|ед|ЕД|ME|IU))$",
            full_label,
            flags=re.IGNORECASE,
        )
        if dose_match:
            dose_text = dose_match.group("dose").strip()
            if drug_label and drug_label.lower().endswith(dose_text.lower()):
                drug_label = drug_label[: -len(dose_text)].strip()
            if not drug_label:
                drug_label = full_label[: dose_match.start()].strip()
            return drug_label or full_label, dose_text
        return drug_label or full_label, ""

    def _order_marker_label_html(self, label: str, style: dict | None = None) -> str:
        color = str((style or {}).get("color") or "#8e44ad")
        font_size = int(getattr(self, "ORDER_LABEL_FONT_SIZE", OperBlockChartWidget.ORDER_LABEL_FONT_SIZE))
        return (
            f"<span style='font-family: Segoe UI; font-size: {font_size}px; font-weight: 700; "
            f"color: {html.escape(color)}; padding: 1px 3px;'>{html.escape(label)}</span>"
        )

    @staticmethod
    def _short_order_label(drug_name: str) -> str:
        clean = re.sub(r"^S\.\s*", "", str(drug_name or "").strip(), flags=re.IGNORECASE)
        clean = re.sub(r"\s+", " ", clean)
        if not clean:
            return "Н"
        first = clean.split(" ", 1)[0]
        return first[:10]

    @staticmethod
    def _order_marker_drug_key(text: str) -> str:
        drug_name, _dose = _split_order_drug_and_dose(str(text or ""))
        key = re.sub(r"\s+", " ", drug_name.strip().casefold())
        return key or "unknown"

    @staticmethod
    def _order_marker_sort_id(row: dict) -> str:
        source_id = row.get("source_id")
        if source_id is not None:
            return f"{row.get('source') or ''}:{source_id}"
        return str(row.get("id") or "")

    @staticmethod
    def _order_marker_raw_text(row: dict) -> str:
        return str(row.get("raw_text") or row.get("text") or row.get("display_label") or "")

    def _order_marker_display_label(self, row: dict) -> str:
        display_label = str(row.get("display_label") or "").strip()
        if display_label:
            return display_label
        return _format_operblock_order_marker_text(self._order_marker_raw_text(row))

    def _order_marker_drug_key_from_row(self, row: dict) -> str:
        drug_label = str(row.get("drug_label") or "").strip()
        if drug_label:
            return re.sub(r"\s+", " ", drug_label.casefold()) or "unknown"
        return self._order_marker_drug_key(self._order_marker_raw_text(row))

    @staticmethod
    def _infusion_drug_key(interval: dict) -> str:
        drug_label = _infusion_display_drug_name(interval, "")
        return re.sub(r"\s+", " ", drug_label.casefold()) or "unknown"

    @staticmethod
    def _infusion_order_label_key(interval: dict) -> str:
        drug_key = OperBlockChartWidget._infusion_drug_key(interval)
        return f"{drug_key}:volume" if _is_volume_only_infusion(interval) else drug_key

    @staticmethod
    def _infusion_entry_interval_key(entry: dict) -> str:
        interval = entry.get("interval") if isinstance(entry.get("interval"), dict) else {}
        payload = interval.get("payload") if isinstance(interval.get("payload"), dict) else {}
        return str(
            interval.get("interval_id")
            or (payload or {}).get("start_event_id")
            or interval.get("start_time")
            or entry.get("row_index")
            or ""
        )

    @classmethod
    def _infusion_entries_with_segment_gaps(
        cls,
        entries: list[dict],
        *,
        edge_trim_hours: float | None = None,
        gap_hours: float | None = None,
        min_width_hours: float = 0.0,
    ) -> list[dict]:
        result = [dict(entry or {}) for entry in (entries or [])]
        if len(result) < 2:
            return result

        if edge_trim_hours is None:
            edge_trim_hours = max(0.0, float(gap_hours or 0.0)) / 2.0
        edge_trim = max(0.0, float(edge_trim_hours or 0.0))
        min_width = max(0.0, float(min_width_hours or 0.0))
        if edge_trim <= 0.0:
            return result

        def bounds(entry: dict) -> tuple[float, float]:
            x_start = float(entry.get("x_start") or 0.0)
            x_end = float(entry.get("x_end") if entry.get("x_end") is not None else x_start)
            return x_start, x_end

        previous = result[0]
        previous_key = cls._infusion_entry_interval_key(previous)
        for current in result[1:]:
            current_key = cls._infusion_entry_interval_key(current)
            if current_key == previous_key:
                previous = current
                previous_key = current_key
                continue

            prev_start, prev_end = bounds(previous)
            curr_start, curr_end = bounds(current)
            prev_width = max(0.0, prev_end - prev_start)
            curr_width = max(0.0, curr_end - curr_start)
            prev_trim = min(edge_trim, max(0.0, prev_width - min_width))
            curr_trim = min(edge_trim, max(0.0, curr_width - min_width))

            if prev_trim > 0.0:
                previous["x_end"] = prev_end - prev_trim
            if curr_trim > 0.0:
                current["x_start"] = curr_start + curr_trim

            previous = current
            previous_key = current_key
        return result

    @staticmethod
    def _infusion_interval_label(interval: dict) -> str:
        fallback_drug_text = "Газ" if _is_gas_infusion(interval) else "Дозатор"
        drug_label = _infusion_display_drug_name(interval, fallback_drug_text)
        short = OperBlockChartWidget._short_order_label(drug_label)
        if _is_gas_infusion(interval):
            return f"{short} {_gas_dose_text(interval)}".strip()
        rate = _format_infusion_rate(interval.get("current_rate_value"), interval.get("current_rate_unit"))
        return f"{short} {rate}".strip()

    @staticmethod
    def _infusion_rate_change_positions(interval: dict, start: datetime, visible_hours: float) -> list[float]:
        result: list[float] = []
        for history in list(interval.get("rate_history") or [])[1:]:
            change_dt = _minute_floor_dt(_parse_datetime_value((history or {}).get("event_time")))
            if change_dt is None:
                continue
            x = (change_dt - start).total_seconds() / 3600.0
            if -0.001 <= x <= visible_hours + 0.001:
                result.append(max(0.0, min(float(visible_hours), x)))
        return result

    def _infusion_rate_change_display_positions(
        self,
        interval: dict,
        start: datetime,
        visible_hours: float,
    ) -> list[float]:
        transform = self._timeline_transform
        if transform is None:
            return self._infusion_rate_change_positions(interval, start, visible_hours)
        result: list[float] = []
        for history in list(interval.get("rate_history") or [])[1:]:
            change_dt = _minute_floor_dt(_parse_datetime_value((history or {}).get("event_time")))
            x = transform.display_hours_for(change_dt, fallback_start=start)
            if x is not None and -0.001 <= x <= visible_hours + 0.001:
                result.append(max(0.0, min(float(visible_hours), x)))
        return result

    def _rebuild_order_marker_style_map(self) -> None:
        current_keys: list[str] = []
        for row in self._order_marker_rows:
            key = self._order_marker_drug_key_from_row(row)
            if key not in current_keys:
                current_keys.append(key)
        for interval in getattr(self, "_infusion_interval_rows", []) or []:
            key = self._infusion_drug_key(interval)
            if key not in current_keys:
                current_keys.append(key)

        previous = dict(getattr(self, "_order_marker_style_map", {}) or {})
        active: dict[str, dict] = {}
        for key in current_keys:
            if key in previous:
                active[key] = previous[key]

        used_styles = list(active.values())
        for key in current_keys:
            if key in active:
                continue
            style = self.ORDER_MARKER_STYLES[len(used_styles) % len(self.ORDER_MARKER_STYLES)]
            for offset in range(len(self.ORDER_MARKER_STYLES)):
                candidate = self.ORDER_MARKER_STYLES[(len(used_styles) + offset) % len(self.ORDER_MARKER_STYLES)]
                if candidate not in used_styles:
                    style = candidate
                    break
            active[key] = style
            used_styles.append(style)
        self._order_marker_style_map = active

    def _order_marker_style(self, text: str) -> dict:
        key = self._order_marker_drug_key(text)
        return self._order_marker_style_for_key(key)

    def _order_marker_style_for_row(self, row: dict) -> dict:
        key = self._order_marker_drug_key_from_row(row)
        return self._order_marker_style_for_key(key)

    def _order_marker_style_for_key(self, key: str) -> dict:
        style = self._order_marker_style_map.get(key)
        if style:
            return style
        checksum = 0
        for index, char in enumerate(key, start=1):
            checksum += index * ord(char)
        return self.ORDER_MARKER_STYLES[checksum % len(self.ORDER_MARKER_STYLES)]

    def _order_marker_slice_rows_for_group(self, group: dict) -> tuple[list[dict], list[datetime]]:
        selected_times: list[datetime] = []
        for row in list(group.get("rows") or []):
            row_dt = _minute_floor_dt(_parse_datetime_value(row.get("datetime")))
            if row_dt is not None and row_dt not in selected_times:
                selected_times.append(row_dt)
        for key in ("datetime", "datetime_end"):
            value = group.get(key)
            row_dt = _minute_floor_dt(value if isinstance(value, datetime) else _parse_datetime_value(value))
            if row_dt is not None and row_dt not in selected_times:
                selected_times.append(row_dt)

        selected_times.sort()
        if not selected_times:
            fallback_rows = [dict(row or {}) for row in list(group.get("rows") or [])]
            fallback_rows.sort(
                key=lambda row: (
                    _parse_datetime_value(row.get("datetime")) or datetime.min,
                    self._order_marker_sort_id(row),
                )
            )
            return fallback_rows, []

        selected_set = set(selected_times)
        rows = [
            dict(row or {})
            for row in self._order_marker_rows
            if _minute_floor_dt(_parse_datetime_value((row or {}).get("datetime"))) in selected_set
        ]
        if not rows:
            rows = [dict(row or {}) for row in list(group.get("rows") or [])]
        rows.sort(
            key=lambda row: (
                _parse_datetime_value(row.get("datetime")) or datetime.min,
                self._order_marker_sort_id(row),
            )
        )
        return rows, selected_times

    def _order_marker_slice_group_for_tooltip(self, group: dict) -> dict:
        rows, selected_times = self._order_marker_slice_rows_for_group(group)
        result = dict(group or {})
        result["rows"] = rows
        result["slice_times"] = selected_times
        if selected_times:
            result["datetime"] = selected_times[0]
            result["datetime_end"] = selected_times[-1]
        result["summary_text"] = ""
        return result

    def _order_marker_tooltip_row_parts(self, row: dict) -> tuple[str, str]:
        drug_text, dose_text = self._order_marker_label_parts(row)
        drug_text = re.sub(r"\s+", " ", str(drug_text or "").strip()) or "Без названия"
        dose_text = re.sub(r"\s+", " ", str(dose_text or "").strip())
        if not dose_text:
            value = str(row.get("dose_value") or "").strip()
            unit = str(row.get("dose_unit") or "").strip()
            dose_text = f"{value} {unit}".strip()
        if not dose_text:
            _raw_drug, raw_dose = _split_order_drug_and_dose(self._order_marker_raw_text(row))
            dose_text = re.sub(r"\s+", " ", str(raw_dose or "").strip())
        if not dose_text:
            display_text = re.sub(
                r"\s+",
                " ",
                str(self._order_marker_display_label(row) or self._order_marker_raw_text(row) or "").strip(),
            )
            if display_text.casefold().startswith(drug_text.casefold()):
                dose_text = display_text[len(drug_text) :].strip(" -:;·")
            elif display_text and display_text.casefold() != drug_text.casefold():
                dose_text = display_text
        dose_text = dose_text or "введено"
        if dose_text != "введено":
            dose_text = _order_dose_text_with_route(dose_text, row, short=True)
        return drug_text, dose_text

    def _order_marker_tooltip_time_text(self, group: dict, rows: list[dict]) -> str:
        selected_times = [dt for dt in list(group.get("slice_times") or []) if isinstance(dt, datetime)]
        if not selected_times:
            for row in rows:
                row_dt = _minute_floor_dt(_parse_datetime_value(row.get("datetime")))
                if row_dt is not None and row_dt not in selected_times:
                    selected_times.append(row_dt)
        selected_times.sort()
        if not selected_times:
            dt = group.get("datetime")
            return dt.strftime("%H:%M") if isinstance(dt, datetime) else "--:--"
        if len(selected_times) == 1:
            return selected_times[0].strftime("%H:%M")
        if len(selected_times) <= 4:
            return ", ".join(dt.strftime("%H:%M") for dt in selected_times)
        return f"{selected_times[0].strftime('%H:%M')} - {selected_times[-1].strftime('%H:%M')}"

    @staticmethod
    def _tooltip_nowrap_text(text: str) -> str:
        return html.escape(str(text or "")).replace(" ", "&nbsp;")

    def _order_marker_tooltip_html(self, group: dict) -> str:
        rows = list(group.get("rows") or [])
        lines = [f"<b>Назначение: {html.escape(self._order_marker_tooltip_time_text(group, rows))}</b>"]
        parsed_row_times = [_parse_datetime_value(row.get("datetime")) for row in rows]
        row_times = {row_dt.strftime("%H:%M") for row_dt in parsed_row_times if row_dt is not None}
        show_row_time = len(row_times) > 1

        drug_groups: dict[str, dict] = {}
        for index, row in enumerate(rows):
            drug_text, dose_text = self._order_marker_tooltip_row_parts(row)
            drug_key = self._order_marker_drug_key_from_row(row)
            if drug_key == "unknown":
                drug_key = re.sub(r"\s+", " ", drug_text.casefold()).strip() or "unknown"
            group_item = drug_groups.setdefault(
                drug_key,
                {
                    "drug_text": drug_text,
                    "style": self._order_marker_style_for_row(row),
                    "items": [],
                    "first_index": index,
                },
            )
            row_dt = parsed_row_times[index] if index < len(parsed_row_times) else None
            group_item["items"].append({"dose_text": dose_text, "row_dt": row_dt})

        for drug_group in sorted(drug_groups.values(), key=lambda item: int(item.get("first_index") or 0)):
            style = drug_group.get("style") if isinstance(drug_group.get("style"), dict) else {}
            color = html.escape(str(style.get("color") or "#4a235a"))
            drug_text = str(drug_group.get("drug_text") or "Без названия")
            items = list(drug_group.get("items") or [])
            if len(items) == 1:
                item = items[0]
                dose_text = str(item.get("dose_text") or "введено")
                row_dt = item.get("row_dt")
                prefix = f"{row_dt.strftime('%H:%M')} · " if show_row_time and isinstance(row_dt, datetime) else ""
                line_text = f"● {prefix}{drug_text} {dose_text}".strip()
                lines.append(
                    f"<span style='color: {color}; font-weight: 700;'>{self._tooltip_nowrap_text(line_text)}</span>"
                )
                continue

            lines.append(
                f"<span style='color: {color}; font-weight: 700;'>{self._tooltip_nowrap_text('● ' + drug_text)}</span>"
            )
            total_text = _summarize_dose_texts([str(item.get("dose_text") or "") for item in items], include_unparsed=False)
            if total_text:
                lines.append(f"<span style='color: {color}; font-weight: 700;'>Итого: {html.escape(total_text)}</span>")
            for item in items:
                dose_text = str(item.get("dose_text") or "введено")
                row_dt = item.get("row_dt")
                prefix = f"{row_dt.strftime('%H:%M')} · " if show_row_time and isinstance(row_dt, datetime) else ""
                line_text = f"{prefix}{drug_text} {dose_text}".strip()
                lines.append(
                    f"<span style='color: {color};'>&nbsp;&nbsp;&nbsp;{html.escape(line_text)}</span>"
                )

        return (
            "<div style='font-family: Segoe UI; font-size: 13px; padding: 5px; min-width: 170px;'>"
            + "<br>".join(lines)
            + "</div>"
        )

    def _infusion_interval_tooltip_html(self, group: dict) -> str:
        interval = dict(group.get("interval") or {})
        start_dt = _parse_datetime_value(interval.get("start_time"))
        end_dt = _parse_datetime_value(interval.get("end_time"))
        is_gas = _is_gas_infusion(interval)
        is_oxygen = _is_oxygen_infusion(interval)
        rate = "" if is_gas else _format_infusion_rate(interval.get("current_rate_value"), interval.get("current_rate_unit"))
        segment_gas_dose = ""
        if is_gas:
            segment_gas_dose = (
                _normalize_oxygen_flow_text(str(group.get("rate_text") or ""))
                if is_oxygen
                else _normalize_gas_dose_text(str(group.get("rate_text") or ""))
            )
        gas_dose = segment_gas_dose or (_gas_dose_text(interval) if is_gas else "")
        status_text = "активна" if str(interval.get("status") or "") == "active" else "остановлена"
        color = html.escape(str(group.get("color") or "#16a085"))
        lines = [
            f"<b style='color: {color};'>{html.escape(_infusion_display_drug_name(interval, 'Газ' if is_gas else 'Дозатор'))}</b>",
            f"Старт: {html.escape(start_dt.strftime('%H:%M') if start_dt else '--:--')}",
            f"Конец: {html.escape(end_dt.strftime('%H:%M') if end_dt else status_text)}",
        ]
        if gas_dose:
            label = "Поток" if is_oxygen else "Доза газа"
            lines.append(f"{label}: {html.escape(gas_dose)}")
        if rate:
            lines.append(f"Текущая скорость: {html.escape(rate)}")
        if rate:
            executed_volume = _format_infusion_executed_volume(interval)
            if executed_volume:
                lines.append(f"Введено: {html.escape(executed_volume)}")
        elif not is_gas:
            declared_volume = _format_infusion_declared_volume(interval)
            if declared_volume:
                lines.append(f"Объем: {html.escape(declared_volume)}")
        if is_gas:
            history = _gas_dose_events(interval)
            if history:
                lines.extend(["", "<b>Изменения потока</b>" if is_oxygen else "<b>Изменения дозы</b>"])
            for item in history[:8]:
                item_dt = _parse_datetime_value((item or {}).get("event_time"))
                item_dose = (
                    _normalize_oxygen_flow_text(str((item or {}).get("dose_text") or ""))
                    if is_oxygen
                    else _normalize_gas_dose_text(str((item or {}).get("dose_text") or ""))
                )
                lines.append(f"{html.escape(item_dt.strftime('%H:%M') if item_dt else '--:--')} · {html.escape(item_dose)}")
        else:
            history = list(interval.get("rate_history") or [])
            if history:
                lines.extend(["", "<b>Изменения скорости</b>"])
            for item in history[:8]:
                item_dt = _parse_datetime_value((item or {}).get("event_time"))
                item_rate = _format_infusion_rate((item or {}).get("rate_value"), (item or {}).get("rate_unit"))
                lines.append(f"{html.escape(item_dt.strftime('%H:%M') if item_dt else '--:--')} · {html.escape(item_rate)}")
        return (
            "<div style='font-family: Segoe UI; font-size: 13px; padding: 5px; background-color: #eef8f4;'>"
            + "<br>".join(lines)
            + "</div>"
        )

    def _nearest_order_marker_group(self, x_click: float, y_click: float | None = None) -> dict | None:
        if not self._order_marker_groups:
            return None
        threshold = max(0.04, min(0.12, self._grid_step_hours() / 2.0))
        closest = None
        closest_key = (float("inf"), float("inf"), 0, 0)
        for group in self._order_marker_groups:
            center_x = float(group.get("x") or 0.0)
            raw_left = group.get("hit_x_start", group.get("x_start", center_x))
            raw_right = group.get("hit_x_end", group.get("x_end", center_x))
            left = min(float(raw_left if raw_left is not None else center_x), float(raw_right if raw_right is not None else center_x))
            right = max(float(raw_left if raw_left is not None else center_x), float(raw_right if raw_right is not None else center_x))
            if left - threshold <= x_click <= right + threshold:
                dist = 0.0 if left <= x_click <= right else min(abs(x_click - left), abs(x_click - right))
            else:
                dist = min(abs(center_x - x_click), abs(left - x_click), abs(right - x_click))
            if dist > threshold:
                continue
            y_dist = 0.0
            if y_click is not None:
                y_candidates = [
                    float(value)
                    for value in (group.get("y"), group.get("dose_y"), group.get("marker_y"))
                    if value is not None
                ]
                y_dist = min((abs(float(y_click) - value) for value in y_candidates), default=0.0)
            summary_score = -1 if str(group.get("summary_text") or "").strip() else 0
            rows_score = -len(group.get("rows") or [])
            candidate_key = (dist, y_dist, summary_score, rows_score)
            if candidate_key < closest_key:
                closest = group
                closest_key = candidate_key
        return closest

    def _nearest_infusion_interval_group(self, x_click: float, y_click: float) -> dict | None:
        closest = None
        closest_dist = float("inf")
        for group in getattr(self, "_infusion_interval_groups", []) or []:
            x_start = float(group.get("x_start") or 0.0)
            x_end = float(group.get("x_end") or 0.0)
            y = float(group.get("y") or 0.0)
            if x_click < x_start - 0.04 or x_click > x_end + 0.04:
                continue
            y_candidates = [
                float(value)
                for value in (group.get("y"), group.get("rate_label_y"))
                if value is not None
            ] or [y]
            dist = min(abs(value - y_click) for value in y_candidates)
            if dist <= 8.0 and dist < closest_dist:
                closest = group
                closest_dist = dist
        return closest

    @classmethod
    def is_vitals_tooltip_zone(cls, y_click: float) -> bool:
        return float(y_click or 0.0) >= cls.VITAL_AXIS_MIN

    @classmethod
    def is_medication_tooltip_zone(cls, y_click: float) -> bool:
        y = float(y_click or 0.0)
        return cls.MEDICATION_BAND_MIN <= y < cls.VITAL_AXIS_MIN

    def on_scene_clicked(self, event):
        if event.button() == Qt.LeftButton:
            pos = event.scenePos()
            vb = self.plot_widget.getViewBox()
            vb_rect = vb.sceneBoundingRect()
            vb_rect.adjust(-1, 0, 1, 0)
            if not vb_rect.contains(pos):
                self._fade_out()
                return

            mouse_point = vb.mapSceneToView(pos)
            visible_hours = max(1, float(getattr(self, "visible_hours", OPERBLOCK_INITIAL_CHART_HOURS)))
            if mouse_point.x() < -0.001 or mouse_point.x() > visible_hours + 0.001:
                self._fade_out()
                return

            if self.is_vitals_tooltip_zone(mouse_point.y()):
                self._show_nearest_vital_tooltip(mouse_point)
                return
            if self.is_medication_tooltip_zone(mouse_point.y()):
                self._show_nearest_medication_tooltip(mouse_point)
                return
            self._fade_out()
            return
        super().on_scene_clicked(event)

    def _show_nearest_medication_tooltip(self, mouse_point) -> None:
        if not (self._order_marker_groups or self._infusion_interval_groups):
            self._fade_out()
            return

        vb = self.plot_widget.getViewBox()
        interval_group = self._nearest_infusion_interval_group(mouse_point.x(), mouse_point.y())
        if interval_group is not None:
            exact_hour = max(
                float(interval_group.get("x_start") or 0.0),
                min(float(interval_group.get("x_end") or 0.0), float(mouse_point.x())),
            )
            html_text = self._infusion_interval_tooltip_html(interval_group)
            scene_p = vb.mapViewToScene(pg.Point(float(mouse_point.x()), float(mouse_point.y())))
            plot_item_p = self.plot_widget.getPlotItem().mapFromScene(scene_p)
            tooltip_pos, anchor = self._slice_tooltip_position(plot_item_p, html_text)
            is_update = self.slice_line.isVisible() and self.tooltip.isVisible()
            self._fade_in_to(exact_hour, tooltip_pos, anchor, html_text, is_update)
            return

        group = self._nearest_order_marker_group(mouse_point.x(), mouse_point.y())
        if group is not None:
            exact_hour = float(group.get("x") or 0.0)
            html_text = self._order_marker_tooltip_html(self._order_marker_slice_group_for_tooltip(group))
            scene_p = vb.mapViewToScene(pg.Point(float(mouse_point.x()), float(mouse_point.y())))
            plot_item_p = self.plot_widget.getPlotItem().mapFromScene(scene_p)
            tooltip_pos, anchor = self._slice_tooltip_position(plot_item_p, html_text)
            is_update = self.slice_line.isVisible() and self.tooltip.isVisible()
            self._fade_in_to(exact_hour, tooltip_pos, anchor, html_text, is_update)
            return

        self._fade_out()

    def _show_nearest_vital_tooltip(self, mouse_point) -> None:
        if not self.start_time or not self.current_vitals:
            return

        x_click = float(mouse_point.x())
        closest_dist = float("inf")
        closest_vital = None
        closest_exact_hour = None
        s_time = self.start_time.replace(microsecond=0)

        for vital in self.current_vitals:
            v_fields = [vital.sys, vital.dia, vital.pulse, vital.temp, vital.spo2]
            if all(value is None for value in v_fields):
                continue
            timestamp = getattr(vital, "timestamp", None)
            if not isinstance(timestamp, datetime):
                continue
            exact_hour = (timestamp.replace(microsecond=0) - s_time).total_seconds() / 3600.0
            dist = abs(exact_hour - x_click)
            if dist < 0.6 and dist < closest_dist:
                closest_dist = dist
                closest_vital = vital
                closest_exact_hour = exact_hour

        if closest_vital is None or closest_exact_hour is None:
            self._fade_out()
            return

        real_timestamp = getattr(closest_vital, "real_timestamp", None) or getattr(closest_vital, "timestamp", None)
        time_str = real_timestamp.strftime("%H:%M") if isinstance(real_timestamp, datetime) else "--:--"
        html_text = (
            "<div style='font-family: Segoe UI; font-size: 13px; padding: 5px; background-color: #ebecef;'>"
            f"<b>Время: {html.escape(time_str)}</b><br>"
        )

        def f_val(value):
            if value is None:
                return "-"
            try:
                number = float(value)
                if number.is_integer():
                    return str(int(number))
                return f"{number:.1f}"
            except Exception:
                return "-"

        if closest_vital.sys is not None or closest_vital.dia is not None:
            html_text += (
                f"<span style='color: {self.colors['ad']};'>"
                f"АД: {f_val(closest_vital.sys)}/{f_val(closest_vital.dia)}</span><br>"
            )
        if closest_vital.pulse is not None:
            html_text += f"<span style='color: {self.colors['pulse']};'>ЧСС: {f_val(closest_vital.pulse)}</span><br>"
        if closest_vital.temp is not None:
            html_text += f"<span style='color: {self.colors['temp']};'>Temp: {f_val(closest_vital.temp)}</span><br>"
        rr_value = getattr(closest_vital, "rr", None)
        if rr_value is not None:
            html_text += f"<span style='color: {self.colors['rr']};'>ЧДД: {f_val(rr_value)}</span><br>"
        cvp_value = getattr(closest_vital, "cvp", None)
        if cvp_value is not None:
            cvp_text = "Ниже нуля" if cvp_value == -1 else f_val(cvp_value)
            html_text += f"<span style='color: {self.colors['cvp']};'>ЦВД: {cvp_text}</span><br>"
        if closest_vital.spo2 is not None:
            html_text += f"<span style='color: {self.colors['spo2']};'>SpO2: {f_val(closest_vital.spo2)}</span>"
        html_text += "</div>"

        vb = self.plot_widget.getViewBox()
        scene_p = vb.mapViewToScene(pg.Point(closest_exact_hour, mouse_point.y()))
        plot_item_p = self.plot_widget.getPlotItem().mapFromScene(scene_p)
        tooltip_pos, anchor = self._slice_tooltip_position(plot_item_p, html_text)
        is_update = self.slice_line.isVisible() and self.tooltip.isVisible()
        self._fade_in_to(closest_exact_hour, tooltip_pos, anchor, html_text, is_update)
