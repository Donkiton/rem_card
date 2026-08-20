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
