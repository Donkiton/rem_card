import json
from datetime import datetime
from html import escape
from typing import Any, Dict, List, Optional

from rem_card.app.patient_age import parse_date_value

from .table_layout import (
    cell_attrs,
    cell_content,
    colspan_cell_attrs,
    render_colgroup,
    table_width_attrs,
    weighted_widths,
)


DEFAULT_DEATH_PROTOCOL_POSITION = "врач анестезиолог-реаниматолог"
DEFAULT_DEATH_PROTOCOL_WORKPLACE = 'КГБУЗ "Городская больница" им М.И. Шевчук МЗХК'
DEFAULT_DEATH_PROTOCOL_CPR_STOP_REASON = "Неэффективности реанимационных мероприятий в течение 30 минут"
DEATH_OUTCOME_TITLE = "ОСТАНОВКА СЕРДЕЧНОЙ ДЕЯТЕЛЬНОСТИ. ИСХОД: СМЕРТЬ."


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value.replace(second=0, microsecond=0, tzinfo=None)
    if value in (None, ""):
        return None

    raw = str(value).strip()
    if not raw:
        return None
    raw = raw.replace(" ", "T")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed.replace(second=0, microsecond=0, tzinfo=None)


def _decode_payload(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        decoded = json.loads(str(value))
    except Exception:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _normalize_measures(value: Any) -> List[Dict[str, str]]:
    if not isinstance(value, list):
        return []

    result = []
    for item in value:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("label") or "").strip()
            text = str(item.get("value") or item.get("text") or item.get("comment") or "").strip()
        else:
            name = ""
            text = str(item or "").strip()
        if name or text:
            result.append({"name": name, "value": text})
    return result


def _format_dt(value: Optional[datetime]) -> str:
    return value.strftime("%d.%m.%Y %H:%M") if value else "—"


def _duration_minutes(start_dt: Optional[datetime], end_dt: Optional[datetime]) -> Optional[int]:
    if not start_dt or not end_dt:
        return None
    return max(0, int((end_dt - start_dt).total_seconds() // 60))


def _duration_text(start_dt: Optional[datetime], end_dt: Optional[datetime]) -> str:
    minutes = _duration_minutes(start_dt, end_dt)
    return f"{minutes} мин" if minutes is not None else "—"


def _is_in_period(start_dt: Optional[datetime], end_dt: Optional[datetime], period_start: Any, period_end: Any) -> bool:
    if start_dt is None and end_dt is None:
        return False
    start_bound = _parse_dt(period_start)
    end_bound = _parse_dt(period_end)
    if start_bound is None or end_bound is None:
        return True

    item_start = start_dt or end_dt
    item_end = end_dt or start_dt
    if item_start is None or item_end is None:
        return False
    item_start = item_start.replace(second=0, microsecond=0, tzinfo=None)
    item_end = item_end.replace(second=0, microsecond=0, tzinfo=None)
    if item_end > item_start:
        return item_start < end_bound and item_end > start_bound
    return start_bound <= item_end < end_bound


def _html_text(value: Any, fallback: str = "—") -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    return escape(text).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br/>")


def _plain_text(value: Any) -> str:
    return str(value or "").strip()


def _render_measures(measures: List[Dict[str, str]]) -> str:
    if not measures:
        return "—"

    rows = []
    for item in measures:
        name = _html_text(item.get("name"), "")
        value = _html_text(item.get("value"), "")
        if name and value:
            rows.append(f"{name}: {value}")
        elif name:
            rows.append(name)
        elif value:
            rows.append(value)
    return "<br/>".join(rows) if rows else "—"


def _patient_name_from_context(context: Dict[str, Any]) -> str:
    name = _plain_text(context.get("patient_name"))
    if name:
        return name
    parts = [
        _plain_text(context.get("last_name")),
        _plain_text(context.get("first_name")),
        _plain_text(context.get("middle_name")),
    ]
    return " ".join(part for part in parts if part) or _plain_text(context.get("full_name"))


def _birth_date_text(value: Any) -> str:
    parsed = parse_date_value(value)
    if parsed:
        return parsed.strftime("%d.%m.%Y")
    return _plain_text(value)


def _patient_birth_date_from_context(context: Dict[str, Any]) -> str:
    return _birth_date_text(context.get("birth_date"))


def _normalize_protocol(
    value: Any,
    context: Dict[str, Any],
    biological_dt: Optional[datetime],
) -> Dict[str, str]:
    raw = value if isinstance(value, dict) else {}
    doctor = _plain_text(raw.get("doctor") or raw.get("signature_doctor"))
    date_text = _plain_text(raw.get("biological_death_date"))
    time_text = _plain_text(raw.get("biological_death_time"))
    if biological_dt is not None:
        date_text = date_text or biological_dt.strftime("%d.%m.%Y")
        time_text = time_text or biological_dt.strftime("%H:%M")
    return {
        "doctor": doctor,
        "position": _plain_text(raw.get("position")) or DEFAULT_DEATH_PROTOCOL_POSITION,
        "workplace": _plain_text(raw.get("workplace")) or DEFAULT_DEATH_PROTOCOL_WORKPLACE,
        "patient": _plain_text(raw.get("patient")) or _patient_name_from_context(context),
        "gender": _plain_text(raw.get("gender")) or _plain_text(context.get("patient_gender")),
        "birth_date": _birth_date_text(raw.get("birth_date")) or _patient_birth_date_from_context(context),
        "history_number": _plain_text(raw.get("history_number")) or _plain_text(context.get("history_number")),
        "other": _plain_text(raw.get("other")),
        "cpr_stop_reason": _plain_text(raw.get("cpr_stop_reason")) or DEFAULT_DEATH_PROTOCOL_CPR_STOP_REASON,
        "biological_death_date": date_text,
        "biological_death_time": time_text,
        "signature_doctor": _plain_text(raw.get("signature_doctor")) or doctor,
    }


def _status_value(event: Any) -> str:
    status = getattr(event, "status", "")
    return str(getattr(status, "value", status) or "")


def _is_cpr_event(event: Any) -> bool:
    return _status_value(event) == "CPR" or str(getattr(event, "reason_type", "") or "") == "cpr"


def _recovery_item(
    payload: Dict[str, Any],
    context: Dict[str, Any],
    *,
    event_start: Any = None,
    event_end: Any = None,
    event_reason: Any = None,
    use_context_fallback: bool = False,
) -> Dict[str, Any]:
    clinical_dt = _parse_dt(event_start) or _parse_dt(payload.get("clinical_death_datetime"))
    recovery_dt = _parse_dt(event_end) or _parse_dt(payload.get("recovery_datetime"))
    if use_context_fallback:
        clinical_dt = clinical_dt or _parse_dt(context.get("clinical_death_datetime"))

    cause = payload.get("cardiac_arrest_cause")
    if use_context_fallback:
        cause = cause or context.get("cardiac_arrest_cause")

    raw_comment = str(event_reason or "").strip()
    if raw_comment.startswith("{"):
        raw_comment = ""
    comment = str(payload.get("comment") or raw_comment).strip()

    return {
        "outcome_kind": "recovery",
        "title": "ОСТАНОВКА СЕРДЕЧНОЙ ДЕЯТЕЛЬНОСТИ. ИСХОД: ВОССТАНОВЛЕНИЕ СПОНТАННОГО КРОВООБРАЩЕНИЯ",
        "clinical_time": _format_dt(clinical_dt),
        "recovery_time": _format_dt(recovery_dt),
        "cpr_duration": _duration_text(clinical_dt, recovery_dt),
        "cause": str(cause or "").strip(),
        "comment": comment,
        "measures": _normalize_measures(payload.get("measures")),
        "doctor": str(payload.get("doctor") or "").strip(),
        "protocol": {},
        "_start_dt": clinical_dt,
        "_end_dt": recovery_dt,
        "_sort_dt": clinical_dt or recovery_dt,
    }


def _death_item(payload: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    biological_dt = _parse_dt(context.get("death_datetime") or payload.get("biological_death_datetime"))
    clinical_dt = _parse_dt(context.get("clinical_death_datetime") or payload.get("clinical_death_datetime"))
    cause = context.get("cardiac_arrest_cause") or payload.get("cardiac_arrest_cause")
    protocol = _normalize_protocol(payload.get("death_protocol"), context, biological_dt)
    return {
        "outcome_kind": "death",
        "title": DEATH_OUTCOME_TITLE,
        "clinical_time": _format_dt(clinical_dt),
        "biological_time": _format_dt(biological_dt),
        "cause": str(cause or "").strip(),
        "comment": str(payload.get("comment") or "").strip(),
        "measures": _normalize_measures(payload.get("measures")),
        "doctor": protocol.get("doctor") or protocol.get("signature_doctor"),
        "protocol": protocol,
        "_start_dt": biological_dt,
        "_end_dt": biological_dt,
        "_sort_dt": biological_dt or clinical_dt,
    }


def _public_item(item: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in item.items() if not key.startswith("_")}


def _details_items(details: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_items = details.get("items")
    if isinstance(raw_items, list):
        return [item for item in raw_items if isinstance(item, dict)]
    return [details] if details else []


def build_death_outcome_struct(remcard_service, admission_id, start_dt, end_dt) -> Dict[str, Any]:
    if not admission_id:
        return {}

    status_service = getattr(remcard_service, "status_service", None)
    if not status_service or not hasattr(status_service, "get_admission_outcome_context"):
        return {}

    try:
        context = status_service.get_admission_outcome_context(admission_id) or {}
    except Exception:
        return {}

    payload = _decode_payload(context.get("cardiac_arrest_measures_json"))
    outcome = str(context.get("outcome") or "").strip().lower()
    outcome_type = str(payload.get("outcome_type") or "").strip()
    items: List[Dict[str, Any]] = []
    has_cpr_events = False

    if hasattr(status_service, "get_events"):
        try:
            events = status_service.get_events(admission_id) or []
        except Exception:
            events = []
        for event in events:
            if not _is_cpr_event(event):
                continue
            has_cpr_events = True
            item = _recovery_item(
                _decode_payload(getattr(event, "reason_text", None)),
                context,
                event_start=getattr(event, "start_time", None),
                event_end=getattr(event, "end_time", None),
                event_reason=getattr(event, "reason_text", None),
            )
            if _is_in_period(item.get("_start_dt"), item.get("_end_dt"), start_dt, end_dt):
                items.append(item)

    if not has_cpr_events and outcome_type == "cpr_recovery":
        item = _recovery_item(payload, context, use_context_fallback=True)
        if _is_in_period(item.get("_start_dt"), item.get("_end_dt"), start_dt, end_dt):
            items.append(item)

    biological_dt = _parse_dt(context.get("death_datetime") or payload.get("biological_death_datetime"))
    is_death = outcome in {"умер", "dead", "death"} or biological_dt is not None
    if is_death and outcome_type != "cpr_recovery":
        item = _death_item(payload, context)
        if _is_in_period(item.get("_start_dt"), item.get("_end_dt"), start_dt, end_dt):
            items.append(item)

    if not items:
        return {}

    items.sort(key=lambda item: item.get("_sort_dt") or datetime.min)
    public_items = [_public_item(item) for item in items]
    result = dict(public_items[-1])
    result["items"] = public_items
    return result


def render_death_outcome(data, table_width_pt, include_outcome=True, include_protocol=True):
    details = data.get("death_outcome") or {}
    if not details or not (include_outcome or include_protocol):
        return ""
    items = _details_items(details)
    if include_protocol and not include_outcome and not any(str(item.get("outcome_kind") or "death") != "recovery" for item in items):
        return ""

    col_widths = weighted_widths(table_width_pt, [0.24, 0.76])
    avoid_block_style = "page-break-inside: avoid; break-inside: avoid;"
    label_style = "text-align: left; font-weight: bold; font-size: 8pt; padding: 4px; background-color: #f8f9fa;"
    value_style = "text-align: left; font-size: 8pt; padding: 4px; line-height: 1.25;"

    def row(label: str, value: str, extra_value_style: str = "") -> str:
        merged_value_style = value_style
        if extra_value_style:
            merged_value_style = f"{merged_value_style} {extra_value_style}"
        return (
            "<tr>"
            f'<td class="death-label" {cell_attrs(col_widths[0], label_style)}>{label}</td>'
            f'<td class="death-value" {cell_attrs(col_widths[1], merged_value_style)}>{cell_content(value)}</td>'
            "</tr>"
        )

    def signature_html(doctor: Any = "") -> str:
        doctor_text = _html_text(doctor, "____________________________________________")
        return (
            f"{doctor_text}"
            "&nbsp;&nbsp;&nbsp;&nbsp;"
            "Подпись ______________________________"
        )

    def html_workplace(value: Any) -> str:
        text = _html_text(value)
        return text.replace(
            " им М.И. Шевчук МЗХК",
            "<br/>им&nbsp;М.И.&nbsp;Шевчук&nbsp;МЗХК",
        )

    def html_cpr_stop_reason(value: Any) -> str:
        text = _html_text(value)
        return text.replace(
            "реанимационных мероприятий",
            "реанимационных<br/>мероприятий",
        )

    protocol_col_widths = weighted_widths(table_width_pt, [0.18, 0.32, 0.22, 0.28])
    protocol_label_style = "text-align: left; font-weight: bold; font-size: 8pt; padding: 4px; background-color: #f8f9fa;"
    protocol_value_style = "text-align: left; font-size: 8pt; padding: 4px; line-height: 1.25;"

    def protocol_row(
        left_label: str,
        left_value: str,
        right_label: str = "",
        right_value: str = "",
        *,
        left_value_style: str = "",
        right_value_style: str = "",
    ) -> str:
        left_style = f"{protocol_value_style} {left_value_style}".strip()
        right_style = f"{protocol_value_style} {right_value_style}".strip()
        return (
            "<tr>"
            f'<td class="death-label" {cell_attrs(protocol_col_widths[0], protocol_label_style)}>{left_label}</td>'
            f'<td class="death-value" {cell_attrs(protocol_col_widths[1], left_style)}>{cell_content(left_value)}</td>'
            f'<td class="death-label" {cell_attrs(protocol_col_widths[2], protocol_label_style)}>{cell_content(right_label)}</td>'
            f'<td class="death-value" {cell_attrs(protocol_col_widths[3], right_style)}>{cell_content(right_value)}</td>'
            "</tr>"
        )

    table_attrs = table_width_attrs(table_width_pt).replace(
        'style="',
        'style="page-break-inside: avoid; break-inside: avoid; ',
        1,
    )
    html = '<div class="section section-avoid death-section">'
    has_content = False
    for details_item in items:
        outcome_kind = str(details_item.get("outcome_kind") or "death")
        if include_outcome:
            if has_content:
                html += '<div class="section-gap">&nbsp;</div>'
            html += f'<table class="report-table data-table death-table" {table_attrs}>'
            html += render_colgroup(col_widths)
            html += "<tbody>"
            html += f'<tr class="table-title-row"><th colspan="2" {colspan_cell_attrs()}>{_html_text(details_item.get("title") or DEATH_OUTCOME_TITLE)}</th></tr>'
            html += row("Время клинической смерти", _html_text(details_item.get("clinical_time")))
            html += row("Причина остановки сердца", _html_text(details_item.get("cause")))
            html += row("Мероприятия", _render_measures(details_item.get("measures") or []))
            html += row("Комментарий к причине остановки сердца", _html_text(details_item.get("comment")))
            if outcome_kind == "recovery":
                html += row("Время восстановления кровообращения", _html_text(details_item.get("recovery_time")))
                html += row("Длительность СЛР", _html_text(details_item.get("cpr_duration")))
            else:
                html += row("Время биологической смерти", _html_text(details_item.get("biological_time")))
            html += row("Врач", signature_html(details_item.get("doctor")), "height: 28px; white-space: nowrap;")
            html += "</tbody></table>"
            has_content = True

        protocol = details_item.get("protocol") or {}
        protocol_death_dt = " ".join(
            part
            for part in (
                _plain_text(protocol.get("biological_death_date")),
                _plain_text(protocol.get("biological_death_time")),
            )
            if part
        ) or _plain_text(details_item.get("biological_time"))
        if include_protocol and outcome_kind != "recovery":
            if has_content:
                html += '<div class="section-gap">&nbsp;</div>'
            html += f'<div class="section-avoid death-protocol-section" style="{avoid_block_style}">'
            html += f'<table class="report-table data-table death-table death-protocol-table" {table_attrs}>'
            html += render_colgroup(protocol_col_widths)
            html += "<tbody>"
            html += (
                f'<tr class="table-title-row"><th colspan="4" {colspan_cell_attrs()}>'
                "ПРОТОКОЛ УСТАНОВЛЕНИЯ СМЕРТИ ЧЕЛОВЕКА"
                "</th></tr>"
            )
            html += protocol_row(
                "Основание",
                "Постановление Правительства РФ от 20.09.2012 № 950",
                "Дата рождения",
                _html_text(protocol.get("birth_date")),
            )
            html += protocol_row("Врач", _html_text(protocol.get("doctor")), "Пол", _html_text(protocol.get("gender")))
            html += protocol_row(
                "Должность",
                _html_text(protocol.get("position")),
                "СЛР остановлена по причине",
                html_cpr_stop_reason(protocol.get("cpr_stop_reason")),
            )
            html += protocol_row(
                "Место работы",
                html_workplace(protocol.get("workplace")),
                "Дата и время биологической смерти",
                _html_text(protocol_death_dt),
            )
            html += protocol_row(
                "Пациент",
                _html_text(protocol.get("patient")),
                "ФИО врача",
                _html_text(protocol.get("signature_doctor") or protocol.get("doctor")),
            )
            html += protocol_row(
                "Номер истории",
                _html_text(protocol.get("history_number")),
                "Подпись",
                "______________________________",
                right_value_style="height: 24px;",
            )
            if _plain_text(protocol.get("other")):
                html += protocol_row("Иное", _html_text(protocol.get("other")))
            html += "</tbody></table></div>"
            has_content = True
    html += "</div>"
    return html if has_content else ""
