"""Presentation-only routing of canonical graph artifacts to offline cards.

Only scalar catalog metrics and outcome compositions are eligible. In particular,
a one-point time series or a single histogram observation is not a scalar KPI.
No clinical queries, population selection or metric calculation belongs here.
"""
from __future__ import annotations

import math
from typing import Mapping, Sequence


SCALAR_GRAPH_KEYS = frozenset({
    "g10", "g12", "g16", "g17", "g39", "g40", "g43", "g48", "g54", "g55", "g60",
})
OUTCOME_GRAPH_KEYS = frozenset({"g28", "recovery_flow_outcomes"})
MAX_CARDS = 6


class InfographicReportRenderer:
    """After one failed worker, use the fallback for the rest of this report."""

    def __init__(self):
        self.available = True

    def render(self, items, **kwargs):
        if not self.available:
            return None
        from rem_card.services.analytics.infographic_renderer import render_infographic

        path = render_infographic(items, **kwargs)
        if not path:
            self.available = False
            from rem_card.app.logger import logger
            logger.warning("AntV rendering unavailable; using Matplotlib for remaining report blocks.")
        return path


def _finite_value(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def scalar_card(key: str, artifact: Mapping[str, object]) -> dict | None:
    rows = tuple(artifact.get("series") or ())
    if key not in SCALAR_GRAPH_KEYS or len(rows) != 1:
        return None
    value = _finite_value(rows[0].get("value"))
    if value is None:
        return None
    return {
        "label": str(artifact.get("title") or key),
        "value": value,
        "unit": str(artifact.get("unit") or ""),
    }


def outcome_cards(key: str, artifact: Mapping[str, object]) -> list[dict]:
    rows = tuple(artifact.get("series") or ())
    if key not in OUTCOME_GRAPH_KEYS or not 1 <= len(rows) <= MAX_CARDS:
        return []
    values = [_finite_value(row.get("value")) for row in rows]
    if any(value is None or value < 0 for value in values):
        return []
    total = sum(values)
    if total <= 0:
        return []
    return [
        {
            "label": str(row.get("label") or "Не указан"),
            "value": value,
            "unit": str(artifact.get("unit") or "случаев"),
            "caption": f"{value / total * 100:.1f}% от показанных исходов",
        }
        for row, value in zip(rows, values)
    ]


def render_scalar_overview(selected: Sequence[str], artifacts, colors, image_paths, *, renderer=None) -> tuple[str, set[str]]:
    from rem_card.ui.analytics.chart_renderer import chart_image_html

    renderer = renderer or InfographicReportRenderer()
    cards = [(key, scalar_card(key, artifacts[key])) for key in selected]
    cards = [(key, card) for key, card in cards if card is not None]
    html_parts = []
    rendered_keys = set()
    for start in range(0, len(cards), MAX_CARDS):
        batch = cards[start:start + MAX_CARDS]
        title = "Сводные показатели" if len(cards) <= MAX_CARDS else f"Сводные показатели · {start // MAX_CARDS + 1}"
        path = renderer.render([card for _key, card in batch], title=title, colors=colors)
        if path:
            image_paths.append(path)
            html_parts.append(chart_image_html(title, path))
            rendered_keys.update(key for key, _card in batch)
    return "".join(html_parts), rendered_keys


def render_outcome_infographic(key, artifact, colors, image_paths, *, renderer=None) -> str | None:
    cards = outcome_cards(key, artifact)
    if not cards:
        return None
    from rem_card.ui.analytics.chart_renderer import chart_image_html

    renderer = renderer or InfographicReportRenderer()
    title = str(artifact.get("title") or key)
    path = renderer.render(cards, title=title, colors=colors)
    if not path:
        return None
    image_paths.append(path)
    return chart_image_html(title, path)
