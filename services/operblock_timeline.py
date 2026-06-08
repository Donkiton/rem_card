from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any, Mapping, Optional

from rem_card.services.operblock_route_settings import operblock_route_from_comment


OPERBLOCK_TIMELINE_EVENT_TYPES = (
    "bolus",
    "infusion_start",
    "infusion_change",
    "infusion_stop",
    "clinical_event",
    "note",
)

OPERBLOCK_STAGE_KIND_LABELS = {
    "anesthesia_start": "Начало пособия",
    "anesthesia_end": "Конец пособия",
    "surgery_start": "Начало операции",
    "surgery_end": "Конец операции",
}


_DOSE_COMPONENT_RE = re.compile(r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>[A-Za-zА-Яа-яЁёµ%./]+)")
def _legacy_order_route(comment: Any) -> Optional[str]:
    return operblock_route_from_comment(comment)


@dataclass(frozen=True)
class OperBlockMedicationEvent:
    id: str
    source: str
    source_id: int
    admission_id: int
    operation_case_id: Optional[int]
    table_code: Optional[str]
    event_time: datetime
    end_time: Optional[datetime]
    event_type: str
    drug_label: str
    display_label: str
    raw_text: str
    dose_value: Optional[str]
    dose_unit: Optional[str]
    volume_ml: Optional[str]
    concentration_text: Optional[str]
    rate_value: Optional[str]
    rate_unit: Optional[str]
    route: Optional[str]
    status: str
    revision: int
    created_at: Optional[str]
    updated_at: Optional[str]
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["event_time"] = self.event_time.isoformat(timespec="seconds")
        data["end_time"] = self.end_time.isoformat(timespec="seconds") if self.end_time else None
        data["payload"] = dict(self.payload or {})
        return data


@dataclass(frozen=True)
class OperBlockInfusionInterval:
    interval_id: str
    admission_id: int
    operation_case_id: int
    drug_label: str
    display_label: str
    start_time: datetime
    end_time: Optional[datetime]
    status: str
    volume_ml: Optional[str]
    current_rate_value: Optional[str]
    current_rate_unit: Optional[str]
    rate_history: list[dict[str, Any]] = field(default_factory=list)
    dose_history: list[dict[str, Any]] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["start_time"] = self.start_time.isoformat(timespec="seconds")
        data["end_time"] = self.end_time.isoformat(timespec="seconds") if self.end_time else None
        data["payload"] = dict(self.payload or {})
        return data


@dataclass(frozen=True)
class OperBlockTimelineSnapshot:
    admission_id: int
    operation_case_id: Optional[int]
    generated_at: str
    bolus_events: list[OperBlockMedicationEvent] = field(default_factory=list)
    infusion_intervals: list[OperBlockInfusionInterval] = field(default_factory=list)
    operation_events: list[OperBlockMedicationEvent] = field(default_factory=list)
    content_hash: str = ""
    source_versions: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "admission_id": self.admission_id,
            "operation_case_id": self.operation_case_id,
            "generated_at": self.generated_at,
            "bolus_events": [event.to_dict() for event in self.bolus_events],
            "infusion_intervals": [interval.to_dict() for interval in self.infusion_intervals],
            "operation_events": [event.to_dict() for event in self.operation_events],
            "content_hash": self.content_hash,
            "source_versions": dict(self.source_versions or {}),
        }


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(second=0, microsecond=0)
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T")).replace(second=0, microsecond=0)
    except Exception:
        return None


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


def _normalize_dose_unit(unit: str) -> tuple[str, str]:
    raw = str(unit or "").strip().lower().replace("ё", "е").replace("µ", "мк")
    raw = raw.replace(".", "")
    aliases = {
        "ml": "мл",
        "мл": "мл",
        "mg": "мг",
        "мг": "мг",
        "mkg": "мкг",
        "mcg": "мкг",
        "мкг": "мкг",
        "мкгр": "мкг",
        "ug": "мкг",
    }
    unit_key = aliases.get(raw, raw)
    unit_label = {"мл": "мл", "мг": "мг", "мкг": "мкг"}.get(unit_key, str(unit or "").strip())
    return unit_key, unit_label


def _format_decimal_ru(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return str(normalized.quantize(Decimal("1")))
    return format(normalized, "f").rstrip("0").rstrip(".").replace(".", ",")


def _order_dose_components(text: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    clean = str(text or "")
    parenthesized_ranges: list[tuple[int, int]] = []
    for match in re.finditer(r"\(([^)]*)\)", clean):
        parenthesized_ranges.append((match.start(), match.end()))

    for match in _DOSE_COMPONENT_RE.finditer(clean):
        raw_value = match.group("value").replace(",", ".")
        try:
            value = Decimal(raw_value)
        except InvalidOperation:
            continue
        unit_key, unit_label = _normalize_dose_unit(match.group("unit"))
        result.append(
            {
                "value": value,
                "unit_key": unit_key,
                "unit_label": unit_label,
                "parenthesized": any(start <= match.start() < end for start, end in parenthesized_ranges),
            }
        )
    return result


def _clean_operblock_drug_name(drug_name: str) -> str:
    clean = re.sub(r"\s+", " ", str(drug_name or "").strip())
    clean = re.sub(r"\s+\d+(?:[.,]\d+)?\s*%$", "", clean).strip()
    return clean or "Без названия"


def _extract_concentration_text(drug_name: str) -> Optional[str]:
    match = re.search(r"(\d+(?:[.,]\d+)?\s*%)\s*$", str(drug_name or "").strip())
    return match.group(1).replace(" ", "") if match else None


def _preferred_dose_component(components: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    priorities = (
        lambda item: bool(item.get("parenthesized")) and item.get("unit_key") in {"мг", "мкг"},
        lambda item: item.get("unit_key") in {"мг", "мкг"},
        lambda item: item.get("unit_key") == "мл",
    )
    for predicate in priorities:
        for component in components:
            if predicate(component):
                return component
    return None


def _preferred_operblock_marker_dose(dose_text: str) -> str:
    component = _preferred_dose_component(_order_dose_components(dose_text))
    if component:
        return f"{_format_decimal_ru(component['value'])} {component['unit_label']}"
    return str(dose_text or "").strip()


def _format_operblock_medication_display_label_parts(drug_name: str, dose_text: str) -> str:
    drug_label = _clean_operblock_drug_name(drug_name)
    dose_label = _preferred_operblock_marker_dose(dose_text)
    return f"{drug_label} {dose_label}".strip() if dose_label else drug_label


def format_operblock_medication_display_label(text: str) -> str:
    drug_name, dose_text = _split_order_drug_and_dose(text)
    return _format_operblock_medication_display_label_parts(drug_name, dose_text)


def parse_operblock_medication_text(text: str) -> dict[str, Any]:
    drug_name, dose_text = _split_order_drug_and_dose(text)
    components = _order_dose_components(dose_text)
    preferred = _preferred_dose_component(components)
    volume = next((item for item in components if item.get("unit_key") == "мл"), None)
    return {
        "drug_label": _clean_operblock_drug_name(drug_name),
        "display_label": format_operblock_medication_display_label(text),
        "dose_value": _format_decimal_ru(preferred["value"]) if preferred else None,
        "dose_unit": preferred["unit_label"] if preferred else None,
        "volume_ml": _format_decimal_ru(volume["value"]) if volume else None,
        "concentration_text": _extract_concentration_text(drug_name),
    }


def legacy_order_row_to_medication_event(
    row: Mapping[str, Any],
    *,
    admission_id: int,
    operation_case_id: Optional[int],
    table_code: Optional[str],
) -> Optional[OperBlockMedicationEvent]:
    event_time = _parse_dt(row.get("datetime"))
    source_id = int(row.get("id") or 0)
    if event_time is None or source_id <= 0:
        return None
    raw_text = str(row.get("text") or "").strip()
    parsed = parse_operblock_medication_text(raw_text)
    display_drug_name = _clean_operblock_drug_name(row.get("drug_display_name") or "")
    if display_drug_name != "Без названия":
        _raw_drug_name, dose_text = _split_order_drug_and_dose(raw_text)
        parsed["drug_label"] = display_drug_name
        parsed["display_label"] = _format_operblock_medication_display_label_parts(display_drug_name, dose_text)
        parsed["concentration_text"] = _extract_concentration_text(display_drug_name) or parsed.get("concentration_text")
    revision = int(row.get("revision") or 0)
    route = _legacy_order_route(row.get("comment"))
    payload = {"is_committed": int(row.get("is_committed") or 0)}
    drug_key = str(row.get("drug_key") or "").strip()
    if drug_key:
        payload["preset_id"] = drug_key
    if route:
        payload["route"] = route
    return OperBlockMedicationEvent(
        id=f"legacy_order:{source_id}",
        source="legacy_order",
        source_id=source_id,
        admission_id=int(admission_id),
        operation_case_id=int(operation_case_id) if operation_case_id else None,
        table_code=str(table_code or "") or None,
        event_time=event_time,
        end_time=None,
        event_type="bolus",
        drug_label=str(parsed["drug_label"]),
        display_label=str(parsed["display_label"]),
        raw_text=raw_text,
        dose_value=parsed.get("dose_value"),
        dose_unit=parsed.get("dose_unit"),
        volume_ml=parsed.get("volume_ml"),
        concentration_text=parsed.get("concentration_text"),
        rate_value=None,
        rate_unit=None,
        route=route,
        status=str(row.get("status") or "active"),
        revision=revision,
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        payload=payload,
    )


def _parse_payload_json(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not value:
        return {}
    try:
        payload = json.loads(str(value))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def operation_stage_kind_from_payload(payload: Mapping[str, Any] | None) -> str:
    if not isinstance(payload, Mapping):
        return ""
    kind = str(payload.get("stage_kind") or payload.get("operation_stage") or "").strip()
    return kind if kind in OPERBLOCK_STAGE_KIND_LABELS else ""


def timeline_event_row_to_medication_event(row: Mapping[str, Any]) -> Optional[OperBlockMedicationEvent]:
    event_time = _parse_dt(row.get("event_time"))
    source_id = int(row.get("id") or 0)
    if event_time is None or source_id <= 0:
        return None
    event_type = str(row.get("event_type") or "").strip()
    if event_type not in OPERBLOCK_TIMELINE_EVENT_TYPES:
        return None
    raw_text = str(row.get("raw_text") or row.get("display_label") or row.get("drug_label") or "").strip()
    drug_label = str(row.get("drug_label") or "").strip()
    display_label = str(row.get("display_label") or "").strip()
    payload = _parse_payload_json(row.get("payload_json"))
    stage_kind = operation_stage_kind_from_payload(payload)
    if stage_kind:
        label = OPERBLOCK_STAGE_KIND_LABELS[stage_kind]
        raw_text = raw_text or label
        drug_label = drug_label or label
        display_label = display_label or label
    payload_display_name = _clean_operblock_drug_name(payload.get("display_name") or payload.get("label") or "")
    if payload_display_name == "Без названия":
        payload_display_name = ""
    if not drug_label:
        drug_label = str(payload.get("label") or payload.get("display_name") or "").strip()
    if not display_label:
        display_label = str(payload.get("display_name") or payload.get("label") or "").strip()
    if payload_display_name:
        drug_label = payload_display_name
        rate_value = str(row.get("rate_value") or "").strip()
        rate_unit = str(row.get("rate_unit") or "").strip()
        if event_type == "infusion_stop":
            display_label = f"{payload_display_name} стоп".strip()
        elif rate_value and rate_unit:
            display_label = f"{payload_display_name} {rate_value} {rate_unit}".strip()
        elif row.get("volume_ml"):
            display_label = f"{payload_display_name} {row.get('volume_ml')} мл".strip()
        else:
            display_label = payload_display_name
    if not drug_label or not display_label:
        parsed = parse_operblock_medication_text(raw_text or drug_label or display_label)
        drug_label = drug_label or str(parsed["drug_label"])
        display_label = display_label or str(parsed["display_label"])
    parent_event_id = row.get("parent_event_id")
    if parent_event_id is not None:
        payload["parent_event_id"] = int(parent_event_id)
    return OperBlockMedicationEvent(
        id=f"timeline_event:{source_id}",
        source="timeline_event",
        source_id=source_id,
        admission_id=int(row.get("admission_id") or 0),
        operation_case_id=int(row.get("operation_case_id") or 0) or None,
        table_code=str(row.get("table_code") or "") or None,
        event_time=event_time,
        end_time=_parse_dt(row.get("end_time")),
        event_type=event_type,
        drug_label=drug_label or "Без названия",
        display_label=display_label or drug_label or "Без названия",
        raw_text=raw_text,
        dose_value=row.get("dose_value"),
        dose_unit=row.get("dose_unit"),
        volume_ml=row.get("volume_ml"),
        concentration_text=row.get("concentration_text"),
        rate_value=row.get("rate_value"),
        rate_unit=row.get("rate_unit"),
        route=row.get("route"),
        status=str(row.get("status") or "active"),
        revision=int(row.get("revision") or 0),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        payload=payload,
    )


def build_infusion_intervals_from_timeline_events(
    events: list[OperBlockMedicationEvent],
) -> list[OperBlockInfusionInterval]:
    starts = [
        event
        for event in events
        if event.event_type == "infusion_start" and event.status in {"active", "stopped"}
    ]
    by_parent: dict[int, list[OperBlockMedicationEvent]] = {}
    for event in events:
        parent_id = event.payload.get("parent_event_id") if isinstance(event.payload, Mapping) else None
        if parent_id is None:
            continue
        by_parent.setdefault(int(parent_id), []).append(event)

    intervals: list[OperBlockInfusionInterval] = []
    for start in sorted(starts, key=lambda item: (item.event_time, item.source_id)):
        children = sorted(by_parent.get(start.source_id, []), key=lambda item: (item.event_time, item.source_id))
        changes = [event for event in children if event.event_type == "infusion_change" and event.status == "active"]
        stops = [event for event in children if event.event_type == "infusion_stop" and event.status == "active"]
        stop = stops[-1] if stops else None
        rate_events = [start] + changes
        current = rate_events[-1]
        start_payload = dict(start.payload or {})
        is_gas = str(start_payload.get("kind") or "").strip().casefold() == "gas"
        rate_history = [] if is_gas else [
            {
                "event_id": event.id,
                "event_time": event.event_time.isoformat(timespec="seconds"),
                "rate_value": event.rate_value,
                "rate_unit": event.rate_unit,
                "revision": event.revision,
            }
            for event in rate_events
            if event.rate_value and event.rate_unit
        ]
        dose_history: list[dict[str, Any]] = []
        if is_gas:
            for event in rate_events:
                event_payload = dict(event.payload or {})
                dose_text = str(
                    event_payload.get("display_dose_text")
                    or event_payload.get("dose_text")
                    or ""
                ).strip()
                if not dose_text:
                    continue
                dose_history.append(
                    {
                        "event_id": event.id,
                        "event_time": event.event_time.isoformat(timespec="seconds"),
                        "dose_text": dose_text,
                        "revision": event.revision,
                    }
                )
        event_ids = [start.id] + [event.id for event in children]
        payload = dict(start_payload)
        if is_gas:
            payload["kind"] = "gas"
            if dose_history:
                latest_dose = str(dose_history[-1].get("dose_text") or "").strip()
                if latest_dose:
                    payload["dose_text"] = latest_dose
                    payload["display_dose_text"] = latest_dose
        payload["start_event_id"] = start.source_id
        payload["start_revision"] = start.revision
        intervals.append(
            OperBlockInfusionInterval(
                interval_id=f"infusion:{start.source_id}",
                admission_id=start.admission_id,
                operation_case_id=int(start.operation_case_id or 0),
                drug_label=start.drug_label,
                display_label=start.display_label,
                start_time=start.event_time,
                end_time=stop.event_time if stop else None,
                status="stopped" if stop or start.status == "stopped" else "active",
                volume_ml=start.volume_ml,
                current_rate_value=None if is_gas else current.rate_value,
                current_rate_unit=None if is_gas else current.rate_unit,
                rate_history=rate_history,
                dose_history=dose_history,
                event_ids=event_ids,
                payload=payload,
            )
        )
    return intervals


def _timeline_hash_payload(snapshot: OperBlockTimelineSnapshot) -> dict[str, Any]:
    return {
        "admission_id": snapshot.admission_id,
        "operation_case_id": snapshot.operation_case_id,
        "bolus_events": [
            event.to_dict()
            for event in sorted(snapshot.bolus_events, key=lambda item: (item.event_time, item.source, item.source_id))
        ],
        "infusion_intervals": [
            interval.to_dict()
            for interval in sorted(snapshot.infusion_intervals, key=lambda item: (item.start_time, item.interval_id))
        ],
        "operation_events": [
            event.to_dict()
            for event in sorted(snapshot.operation_events, key=lambda item: (item.event_time, item.source, item.source_id))
        ],
        "source_versions": dict(snapshot.source_versions or {}),
    }


def with_timeline_content_hash(snapshot: OperBlockTimelineSnapshot) -> OperBlockTimelineSnapshot:
    raw = json.dumps(_timeline_hash_payload(snapshot), ensure_ascii=False, sort_keys=True, default=str)
    content_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return OperBlockTimelineSnapshot(
        admission_id=snapshot.admission_id,
        operation_case_id=snapshot.operation_case_id,
        generated_at=snapshot.generated_at,
        bolus_events=snapshot.bolus_events,
        infusion_intervals=snapshot.infusion_intervals,
        operation_events=snapshot.operation_events,
        content_hash=content_hash,
        source_versions=snapshot.source_versions,
    )


def build_timeline_snapshot_from_legacy_orders(
    *,
    admission_id: int,
    operation_case_id: Optional[int],
    table_code: Optional[str],
    order_rows: list[Mapping[str, Any]],
    generated_at: str,
    timeline_rows: Optional[list[Mapping[str, Any]]] = None,
) -> OperBlockTimelineSnapshot:
    bolus_events = [
        event
        for event in (
            legacy_order_row_to_medication_event(
                row,
                admission_id=admission_id,
                operation_case_id=operation_case_id,
                table_code=table_code,
            )
            for row in order_rows
        )
        if event is not None
    ]
    bolus_events.sort(key=lambda item: (item.event_time, item.source_id))
    timeline_events = [
        event
        for event in (timeline_event_row_to_medication_event(row) for row in (timeline_rows or []))
        if event is not None
    ]
    operation_events = [
        event
        for event in timeline_events
        if event.event_type == "clinical_event" and operation_stage_kind_from_payload(event.payload)
    ]
    operation_event_ids = {event.id for event in operation_events}
    medication_timeline_events = [
        event
        for event in timeline_events
        if event.id not in operation_event_ids
    ]
    infusion_intervals = build_infusion_intervals_from_timeline_events(medication_timeline_events)
    source_versions = {
        "legacy_orders": {
            "count": len(bolus_events),
            "max_revision": max((event.revision for event in bolus_events), default=0),
            "ids": [event.source_id for event in bolus_events],
        },
        "timeline_events": {
            "count": len(timeline_events),
            "max_revision": max((event.revision for event in timeline_events), default=0),
            "ids": [event.source_id for event in timeline_events],
        },
    }
    snapshot = OperBlockTimelineSnapshot(
        admission_id=int(admission_id),
        operation_case_id=int(operation_case_id) if operation_case_id else None,
        generated_at=generated_at,
        bolus_events=bolus_events,
        infusion_intervals=infusion_intervals,
        operation_events=operation_events,
        source_versions=source_versions,
    )
    return with_timeline_content_hash(snapshot)

