from datetime import datetime, timedelta
from html import escape
from pathlib import Path

from rem_card.app.paths import get_icon_dir
from rem_card.services.order_domain_service import (
    NURSE_MARK_EXECUTED,
    NURSE_MARK_NOT_EXECUTED,
)

from .table_layout import (
    cell_attrs,
    cell_content,
    colspan_cell_attrs,
    hourly_widths,
    render_hourly_colgroup,
    table_width_attrs,
)

PRESCRIPTION_NAME_WIDTH_PT = 120.0


def _icon_uri(file_name: str) -> str:
    path = Path(get_icon_dir()) / file_name
    if not path.exists():
        return ""
    return path.resolve().as_uri()


_MARK_ICONS = {
    NURSE_MARK_EXECUTED: (_icon_uri("done.png"), "Выполнено"),
    NURSE_MARK_NOT_EXECUTED: (_icon_uri("notdone.png"), "Не выполнено"),
}
_PLANNED_MARK_HTML = '<span class="prescription-planned-mark" title="Запланировано">&#9675;</span>'


def _is_due(planned_time) -> bool:
    if planned_time is None:
        return True
    if isinstance(planned_time, datetime):
        planned_dt = planned_time
    else:
        try:
            planned_dt = datetime.fromisoformat(str(planned_time).replace(" ", "T"))
        except ValueError:
            return True
    return planned_dt + timedelta(hours=1) <= datetime.now()


def _mark_icon_html(
    nurse_mark: str,
    planned_time,
    *,
    fallback_to_x: bool,
    auto_not_done: bool = True,
    css_class: str = "prescription-mark-icon",
    size: int = 10,
) -> str:
    effective_mark = nurse_mark
    if auto_not_done and not effective_mark and not fallback_to_x and _is_due(planned_time):
        effective_mark = NURSE_MARK_NOT_EXECUTED

    icon_src, alt = _MARK_ICONS.get(effective_mark, ("", ""))
    if icon_src:
        return (
            f'<img class="{escape(css_class, quote=True)}" '
            f'width="{int(size)}" height="{int(size)}" '
            f'src="{escape(icon_src, quote=True)}" '
            f'alt="{escape(alt, quote=True)}" />'
        )
    return "X" if fallback_to_x else ""


def _render_mark(mark) -> str:
    fallback_to_x = not isinstance(mark, dict)
    if isinstance(mark, dict):
        role = mark.get("role")
        nurse_mark = mark.get("nurse_mark") or mark.get("comment") or ""
        planned_time = mark.get("planned_time")
    else:
        role = mark
        nurse_mark = ""
        planned_time = None

    icon = _mark_icon_html(
        str(nurse_mark or ""),
        planned_time,
        fallback_to_x=fallback_to_x,
    )
    body_icon = _mark_icon_html(
        str(nurse_mark or ""),
        planned_time,
        fallback_to_x=False,
        auto_not_done=False,
        css_class="prescription-mark-icon-mini",
        size=7,
    )
    arrow = '<span class="prescription-arrow">→</span>'
    planned_mark = "" if fallback_to_x else _PLANNED_MARK_HTML

    if role == 'single':
        return icon or planned_mark
    if role == 'start':
        return f"{icon or planned_mark} {arrow}".strip()
    if role == 'body':
        if body_icon:
            return f'<span class="prescription-body-mark">{arrow}&nbsp;{body_icon}</span>'
        return arrow
    if role == 'end':
        return f"{arrow} {icon or planned_mark}".strip()
    return ""


def _chain_key(mark):
    if not isinstance(mark, dict):
        return None
    return mark.get("chain_key") or mark.get("big_chain_id") or mark.get("chain_id")


def _is_explicit_not_done(mark) -> bool:
    if not isinstance(mark, dict):
        return False
    return (mark.get("nurse_mark") or mark.get("comment") or "") == NURSE_MARK_NOT_EXECUTED


def render_prescriptions(data, hours, table_width_pt):
    col_widths = hourly_widths(table_width_pt, PRESCRIPTION_NAME_WIDTH_PT)
    name_width = col_widths[0]
    matrix_widths = col_widths[1:]

    html = f'<div class="section prescriptions-section"><table class="report-table data-table prescriptions-table" {table_width_attrs(table_width_pt)}>'
    html += render_hourly_colgroup(table_width_pt, PRESCRIPTION_NAME_WIDTH_PT)
    html += '<thead>'
    html += f'<tr class="table-title-row"><th colspan="25" {colspan_cell_attrs()}>ЛИСТ НАЗНАЧЕНИЙ</th></tr>'
    html += f'<tr><th class="name-cell" {cell_attrs(name_width, "text-align: left;")}>Препарат / Дозировка</th>'
    html += "".join(f'<th class="matrix-cell" {cell_attrs(matrix_widths[i])}>{h}</th>' for i, h in enumerate(hours))
    html += '</tr>'
    html += '</thead><tbody>'
    p_matrix = data.get("prescriptions_matrix", [])
    if not p_matrix:
        html += '<tr><td colspan="25" style="border: 1px solid #999; padding:20px; text-align: center;">Нет назначений за период</td></tr>'
    else:
        for item in p_matrix:
            name_data = item["name"]
            if isinstance(name_data, list):
                name_display = "<br/>".join(name_data)
            else:
                name_display = str(name_data)
                
            row = f'<tr><td class="name-cell" {cell_attrs(name_width, "text-align: left;")}>{cell_content(name_display)}</td>'
            stopped_chains = set()
            for i, mark in enumerate(item["marks"]):
                chain_key = _chain_key(mark)
                if chain_key and chain_key in stopped_chains:
                    display = ""
                else:
                    display = _render_mark(mark)
                    if chain_key and _is_explicit_not_done(mark):
                        stopped_chains.add(chain_key)
                row += f'<td class="matrix-cell" {cell_attrs(matrix_widths[i])}>{cell_content(display)}</td>'
            html += row + '</tr>'
    html += '</tbody></table></div>'
    return html
