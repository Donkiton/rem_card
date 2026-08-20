from .table_layout import (
    cell_attrs,
    cell_content,
    colspan_cell_attrs,
    hourly_widths,
    render_hourly_colgroup,
    table_width_attrs,
)

VITALS_NAME_WIDTH_PT = 120.0
VITALS_CELL_STYLE = "font-size: 6pt; line-height: 1; padding-left: 0; padding-right: 0;"
BP_CELL_STYLE = "line-height: 0.95;"


def _format_bp_cell(sys, dia):
    has_sys = sys not in (None, "")
    has_dia = dia not in (None, "")
    if not has_sys and not has_dia:
        return ""

    sys_text = str(sys) if has_sys else "&nbsp;"
    dia_text = str(dia) if has_dia else "&nbsp;"
    return f'<span style="{BP_CELL_STYLE}">{sys_text}<br/>{dia_text}</span>'


def render_vitals(data, hours, table_width_pt):
    col_widths = hourly_widths(table_width_pt, VITALS_NAME_WIDTH_PT)
    name_width = col_widths[0]
    matrix_widths = col_widths[1:]

    html = '<div class="section section-avoid vitals-section">'
    html += f'<table class="report-table data-table" {table_width_attrs(table_width_pt)}>'
    html += render_hourly_colgroup(table_width_pt, VITALS_NAME_WIDTH_PT)
    html += '<thead>'
    html += f'<tr class="table-title-row"><th colspan="25" {colspan_cell_attrs()}>ТАБЛИЦА ПОКАЗАТЕЛЕЙ</th></tr>'
    html += f'<tr><th class="name-cell" {cell_attrs(name_width, VITALS_CELL_STYLE + " text-align: left;")}>Час</th>'
    html += "".join(
        f'<th class="matrix-cell" {cell_attrs(matrix_widths[i], VITALS_CELL_STYLE)}>{h}</th>'
        for i, h in enumerate(hours)
    )
    html += '</tr>'
    html += '</thead><tbody>'
    
    v_matrix = data.get("vitals_matrix", {})
    settings = data.get("vital_settings", {'ad': 1, 'pulse': 1, 'temp': 1, 'spo2': 1, 'rr': 0, 'cvp': 0})
    
    # Жесткий порядок согласно настройкам
    indicators = [
        ("АД", "ad", settings.get('ad')),
        ("ЧСС", "hr", settings.get('pulse')),
        ("Темп", "temp", settings.get('temp')),
        ("ЧДД", "rr", settings.get('rr')),
        ("SpO2", "spo2", settings.get('spo2')),
        ("ЦВД", "cvp", settings.get('cvp'))
    ]
    
    for label, key, is_visible in indicators:
        if not is_visible: continue
        
        row = f'<tr><td class="name-cell" {cell_attrs(name_width, VITALS_CELL_STYLE + " text-align: left;")}>{label}</td>'
        for i in range(24):
            val = ""
            if key == "ad":
                sys = v_matrix.get(i, {}).get("sys", "")
                dia = v_matrix.get(i, {}).get("dia", "")
                val = _format_bp_cell(sys, dia)
            elif key == "cvp":
                cvp_val = v_matrix.get(i, {}).get("cvp", "")
                val = "Н/Н" if cvp_val == -1 else cvp_val
            else:
                val = v_matrix.get(i, {}).get(key, "")
            extra_style = VITALS_CELL_STYLE
            row += f'<td class="matrix-cell" {cell_attrs(matrix_widths[i], extra_style)}>{cell_content(val)}</td>'
        html += row + '</tr>'
    html += '</tbody></table></div>'
    return html
