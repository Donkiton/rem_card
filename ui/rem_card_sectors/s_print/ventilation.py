from .table_layout import (
    cell_attrs,
    cell_content,
    colspan_cell_attrs,
    render_colgroup,
    table_width_attrs,
    weighted_widths,
)


def render_ventilation(data, table_width_pt):
    rows = data.get("ventilation_struct", [])
    if not rows:
        return ""

    col_widths = weighted_widths(table_width_pt, [0.20, 0.16, 0.16, 0.24, 0.24])

    html = '<div class="section section-avoid ventilation-section">'
    html += f'<table class="report-table vent-table" {table_width_attrs(table_width_pt)}>'
    html += render_colgroup(col_widths)
    html += '<thead>'
    html += (
        f'<tr class="table-title-row"><th colspan="5" {colspan_cell_attrs()}>ИСТОРИЯ СОБЫТИЙ ИВЛ</th></tr>'
    )
    html += (
        '<tr>'
        f'<th class="vent-time" {cell_attrs(col_widths[0])}>Время</th>'
        f'<th class="vent-event" {cell_attrs(col_widths[1])}>Событие</th>'
        f'<th class="vent-mode" {cell_attrs(col_widths[2])}>Режим</th>'
        f'<th class="vent-params" {cell_attrs(col_widths[3], "text-align: left;")}>Параметры</th>'
        f'<th class="vent-indications" {cell_attrs(col_widths[4], "text-align: left;")}>Показания</th>'
        '</tr>'
    )
    html += '</thead><tbody>'

    for row in rows:
        html += (
            f'<tr>'
            f'<td class="vent-time" {cell_attrs(col_widths[0])}>{cell_content(row.get("time", "—"))}</td>'
            f'<td class="vent-event" {cell_attrs(col_widths[1])}>{cell_content(row.get("event", "—"))}</td>'
            f'<td class="vent-mode" {cell_attrs(col_widths[2])}>{cell_content(row.get("mode", "—"))}</td>'
            f'<td class="vent-params" {cell_attrs(col_widths[3], "text-align: left;")}>{cell_content(row.get("params", "—"))}</td>'
            f'<td class="vent-indications" {cell_attrs(col_widths[4], "text-align: left;")}>{cell_content(row.get("indications", "—"))}</td>'
            f'</tr>'
        )

    html += '</tbody></table></div>'
    return html
