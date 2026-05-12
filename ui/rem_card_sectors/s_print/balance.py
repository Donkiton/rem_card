from .table_layout import (
    cell_attrs,
    cell_content,
    colspan_cell_attrs,
    hourly_widths,
    render_hourly_colgroup,
    table_width_attrs,
)

BALANCE_NAME_WIDTH_PT = 120.0


def format_hourly_balance_value(value):
    if value in (None, ""):
        return ""
    try:
        rounded = int(round(float(value)))
    except (TypeError, ValueError):
        return str(value)
    if rounded == 0:
        return ""
    return str(rounded)


def _format_hourly_value(value):
    value = format_hourly_balance_value(value)
    if value:
        return value
    return ""


def _format_ml_value(value):
    return int(round(float(value or 0)))


def get_current_balance_totals(data):
    bf = data.get("balance_final")
    if not isinstance(bf, dict):
        return None

    cur_bal = bf.get("current", {})
    cur_bal_data = cur_bal.get("daily", cur_bal) if isinstance(cur_bal, dict) else {}
    in_cur = bf.get("in_cur", {})
    out_cur = bf.get("out_cur", {})

    total_in = _format_ml_value(in_cur.get("total", cur_bal_data.get("total", 0)))
    total_out = _format_ml_value(sum((out_cur or {}).values()))
    net_balance = total_in - total_out
    return {
        "total_in": total_in,
        "total_out": total_out,
        "net_balance": net_balance,
    }


def format_signed_ml(value):
    value = int(value or 0)
    if value > 0:
        return f"+{value}"
    return str(value)


def _render_hourly_balance_table(title, rows, hourly_data, hours, table_width_pt, name_width, matrix_widths):
    html = f'<table class="report-table data-table" {table_width_attrs(table_width_pt)}>'
    html += render_hourly_colgroup(table_width_pt, BALANCE_NAME_WIDTH_PT)
    html += '<thead>'
    html += f'<tr class="table-title-row"><th colspan="25" {colspan_cell_attrs()}>{title}</th></tr>'
    html += f'<tr><th class="name-cell" {cell_attrs(name_width, "text-align: left;")}>Тип</th>'
    html += "".join(f'<th class="matrix-cell" {cell_attrs(matrix_widths[i])}>{h}</th>' for i, h in enumerate(hours))
    html += '</tr>'
    html += '</thead><tbody>'
    for label, key in rows:
        row = f'<tr><td class="name-cell" {cell_attrs(name_width, "text-align: left;")}>{label}</td>'
        for i in range(24):
            val = _format_hourly_value(hourly_data.get(i, {}).get(key, ""))
            row += f'<td class="matrix-cell" {cell_attrs(matrix_widths[i])}>{cell_content(val)}</td>'
        html += row + '</tr>'
    html += '</tbody></table>'
    return html


def render_balance(data, hours, table_width_pt):
    col_widths = hourly_widths(table_width_pt, BALANCE_NAME_WIDTH_PT)
    name_width = col_widths[0]
    matrix_widths = col_widths[1:]

    bf = data.get("balance_final", {})
    out_hourly = bf.get("out_hourly", {})
    in_hourly = bf.get("in_hourly", {})
    totals = get_current_balance_totals(data) or {"total_in": 0, "total_out": 0}

    out_rows = [("Диурез", "urine"), ("Дренажи", "drain"), ("ЖКТ (зонд)", "ng"), ("Рвота", "stool"), ("Другое", "other")]
    in_rows = [("Инфузия", "infusion"), ("Препараты", "preparats"), ("Кровь", "blood"), ("Плазма", "plasma"), ("Перорально", "oral")]

    html = '<div class="section section-avoid balance-section">'
    html += _render_hourly_balance_table(
        f'ПОЧАСОВОЕ ВВЕДЕНИЕ. {totals["total_in"]} мл.',
        in_rows,
        in_hourly,
        hours,
        table_width_pt,
        name_width,
        matrix_widths,
    )
    html += _render_hourly_balance_table(
        f'ПОЧАСОВОЕ ВЫВЕДЕНИЕ. {totals["total_out"]} мл.',
        out_rows,
        out_hourly,
        hours,
        table_width_pt,
        name_width,
        matrix_widths,
    )
    html += '</div>'

    return html
