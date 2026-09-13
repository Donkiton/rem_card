from __future__ import annotations

from rem_card.ui.styles.theme_tokens import token


def analytics_chart_colors(tokens: dict[str, str]) -> list[str]:
    return [token(tokens, f"chart.palette.{idx}") for idx in range(1, 11)]


def vital_colors(tokens: dict[str, str]) -> dict[str, str]:
    return {
        "ad": token(tokens, "medical.vital.bp.line"),
        "ad_fill": token(tokens, "medical.vital.bp.bg"),
        "pulse": token(tokens, "medical.vital.pulse.line"),
        "pulse_fill": token(tokens, "medical.vital.pulse.bg"),
        "spo2": token(tokens, "medical.vital.spo2.line"),
        "spo2_fill": token(tokens, "medical.vital.spo2.bg"),
        "temp": token(tokens, "medical.vital.temp.line"),
        "temp_fill": token(tokens, "medical.vital.temp.bg"),
        "rr": token(tokens, "medical.vital.resp.line"),
        "rr_fill": token(tokens, "medical.vital.resp.bg"),
        "cvp": token(tokens, "medical.vital.cvp.line"),
        "cvp_fill": token(tokens, "medical.vital.cvp.bg"),
    }


def chart_paint_colors(tokens: dict[str, str]) -> dict[str, str]:
    """Colors used by pyqtgraph and QPainter surfaces.

    QSS cannot recolor these surfaces, therefore every value is derived from
    semantic tokens and can be reapplied to existing plot items at runtime.
    """
    return {
        "header_bg": token(tokens, "surface.panel", "#e9ecef"),
        "body_bg": token(tokens, "surface.window", "#f8f9fa"),
        "plot_bg": token(tokens, "chart.bg", "#ffffff"),
        "border": token(tokens, "border.default", "#bdc3c7"),
        "grid": token(tokens, "chart.grid", "#e0e0e0"),
        "axis": token(tokens, "chart.axis", "#495057"),
        "text": token(tokens, "chart.text", "#2c3e50"),
        "tooltip_bg": token(tokens, "surface.card", "#ffffff"),
        "tooltip_border": token(tokens, "border.default", "#544d4d"),
        "hover": token(tokens, "surface.hover", "#d8dde2"),
        "slice": token(tokens, "border.default", "#888888"),
    }


def chart_widget_style(tokens: dict[str, str]) -> str:
    colors = chart_paint_colors(tokens)
    return f"""
        QWidget#chart_header {{
            background-color: {colors['header_bg']} !important;
            border-top: 1.5px solid {colors['border']} !important;
            border-right: 1.5px solid {colors['border']} !important;
            border-bottom: 0.5px solid {colors['border']} !important;
            border-top-right-radius: 5px !important;
            border-top-left-radius: 0px !important;
            border-left: none !important;
        }}
        QWidget#chart_body {{
            background-color: {colors['body_bg']} !important;
            border-right: 1.5px solid {colors['border']} !important;
            border-bottom: 1.5px solid {colors['border']} !important;
            border-bottom-right-radius: 5px !important;
            border-left: none !important;
            border-top: none !important;
        }}
    """
