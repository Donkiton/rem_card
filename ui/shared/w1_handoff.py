from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class W1LayoutHandoff:
    """Live W1 widget tree transferred from the startup shell to the full card."""

    role: str
    beds_view: Any
    beds_selection_widget: Any
    archive_view: Any
    archive_widget: Any
    admin_view: Any
    admin_widget: Any
    journal_view: Any
    journal_widget: Any
    sector_w1a: Any
    sector_w1b: Any
    sector_w1b_nurse: Any
    sector_w1c: Any
    archive_last_change_id: int
    current_mode: str
    selection_index: int
    sector_1a_current_widget: Any

