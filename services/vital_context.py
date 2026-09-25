"""Bounded, per-indicator context shared by charts and printed vitals."""

import os
from datetime import timedelta


VITAL_FIELDS = ("sys", "dia", "pulse", "spo2", "temp", "rr", "cvp")
CHART_LOOKBACK_DAYS = max(0, int(os.environ.get("REMCARD_CHART_LOOKBACK_DAYS", "2")))
CHART_LOOKAHEAD_DAYS = max(0, int(os.environ.get("REMCARD_CHART_LOOKAHEAD_DAYS", "1")))


def vital_context_bounds(start, end):
    return (
        start - timedelta(days=CHART_LOOKBACK_DAYS),
        end + timedelta(days=CHART_LOOKAHEAD_DAYS),
    )


def vital_edge_points(vitals, *, count=2, from_end=False):
    """Keep neighboring measurements of every field, including zero and Н/Н."""
    remaining = {field: count for field in VITAL_FIELDS}
    selected = []
    for vital in reversed(vitals) if from_end else vitals:
        needed = [
            field for field in VITAL_FIELDS
            if remaining[field] > 0 and getattr(vital, field, None) is not None
        ]
        if needed:
            selected.append(vital)
            for field in needed:
                remaining[field] -= 1
        if not any(value > 0 for value in remaining.values()):
            break
    return list(reversed(selected)) if from_end else selected


def select_vitals_with_context(vitals, start, end, *, edge_points=2):
    """Input must be sorted by timestamp; the visible right edge is inclusive."""
    before, inside, after = [], [], []
    for vital in vitals:
        if vital.timestamp < start:
            before.append(vital)
        elif vital.timestamp > end:
            after.append(vital)
        else:
            inside.append(vital)
    return (
        vital_edge_points(before, count=edge_points, from_end=True)
        + inside
        + vital_edge_points(after, count=edge_points)
    )
