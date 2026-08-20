from .table_layout import (
    cell_attrs,
    cell_content,
    colspan_cell_attrs,
    render_colgroup,
    table_width_attrs,
    weighted_widths,
)


def render_events(data, table_width_pt):
    col_widths = weighted_widths(table_width_pt, [0.25, 0.20, 0.55])

    html = '<div class="section section-avoid events-section">'
    html += f'<table class="report-table data-table" {table_width_attrs(table_width_pt)}>'
    html += render_colgroup(col_widths)
    html += '<thead>'
    html += f'<tr class="table-title-row"><th colspan="3" {colspan_cell_attrs()}>ДВИЖЕНИЕ</th></tr>'
    html += (
        f'<tr>'
        f'<th class="event-time" {cell_attrs(col_widths[0])}>Дата и Время</th>'
        f'<th class="event-status" {cell_attrs(col_widths[1])}>Где / Статус</th>'
        f'<th class="event-desc" {cell_attrs(col_widths[2], "text-align: left;")}>Комментарий</th>'
        f'</tr>'
    )
    html += '</thead><tbody>'
    
    evs = data.get("events_struct", [])
    if not evs:
        html += '<tr><td colspan="3" style="border: 1px solid #999; padding:20px; text-align: center;">Нет событий за период</td></tr>'
    else:
        for ev in evs:
            html += (
                f'<tr>'
                f'<td class="event-time" {cell_attrs(col_widths[0])}>{cell_content(ev["time"])}</td>'
                f'<td class="event-status" {cell_attrs(col_widths[1])}>{cell_content(ev["status"])}</td>'
                f'<td class="event-desc" {cell_attrs(col_widths[2], "text-align: left;")}>{cell_content(ev["desc"])}</td>'
                f'</tr>'
            )
            
    html += '</tbody></table></div>'
    return html
