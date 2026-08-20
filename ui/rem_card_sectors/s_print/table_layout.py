EMPTY_CELL = "&nbsp;"
CELL_BORDER = "border: 1px solid #999; text-align: center; vertical-align: middle;"
TITLE_CELL_STYLE = (
    "border: 1px solid #999; font-size: 12px; color: #2c3e50; "
    "padding: 5px; text-align: center; background-color: #f8f9fa;"
)


def cell_content(value):
    return value if value not in (None, "") else EMPTY_CELL


def fmt_pt(value):
    return f"{int(round(float(value)))}pt"


def width_attrs(width_pt, extra_style=""):
    width_int = max(1, int(round(float(width_pt))))
    style = f"width: {fmt_pt(width_pt)};"
    if extra_style:
        style = f"{style} {extra_style.strip()}"
    return f'width="{width_int}" style="{style}"'


def cell_attrs(width_pt, extra_style=""):
    return width_attrs(width_pt, f"{CELL_BORDER} {extra_style}")


def colspan_cell_attrs(extra_style=""):
    style = TITLE_CELL_STYLE
    if extra_style:
        style = f"{style} {extra_style.strip()}"
    return f'style="{style}"'


def table_width_attrs(width_pt):
    width_int = max(1, int(round(float(width_pt))))
    return (
        f'width="{width_int}" border="1" cellspacing="0" cellpadding="0" '
        f'style="width: {fmt_pt(width_pt)}; border-collapse: collapse;"'
    )


def render_colgroup(widths_pt):
    return (
        '<colgroup>'
        + ''.join(f'<col {width_attrs(width_pt)}>' for width_pt in widths_pt)
        + '</colgroup>'
    )


def hourly_widths(table_width_pt, name_width_pt):
    table_width = max(25, int(round(float(table_width_pt))))
    name_width = max(1, min(int(round(float(name_width_pt))), table_width - 24))
    remaining = table_width - name_width
    base = remaining // 24
    extra = remaining % 24
    matrix_widths = [base + (1 if i < extra else 0) for i in range(24)]
    return [name_width] + matrix_widths


def weighted_widths(table_width_pt, weights):
    table_width = max(1, int(round(float(table_width_pt))))
    raw_widths = [float(weight) * table_width for weight in weights]
    widths = [int(width) for width in raw_widths]
    remainder = table_width - sum(widths)
    fractions = sorted(
        enumerate(raw_widths),
        key=lambda item: item[1] - int(item[1]),
        reverse=True,
    )
    for i in range(remainder):
        widths[fractions[i % len(fractions)][0]] += 1
    return widths


def render_hourly_colgroup(table_width_pt, name_width_pt):
    return render_colgroup(hourly_widths(table_width_pt, name_width_pt))
