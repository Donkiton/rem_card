from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QLabel, QTableWidget, QTableWidgetItem

from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.shared.minigames.user_select_page import role_label
from rem_card.ui.styles.shared_styles import apply_minigame_label_style


class LeaderboardDialog(BaseStyledDialog):
    def __init__(self, score_store, parent=None):
        super().__init__("Лидеры: Змейка", parent)
        self.score_store = score_store
        self.resize(760, 420)
        self._build_ui()
        self._load_rows()

    def _build_ui(self) -> None:
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["ФИО", "Роль", "Счёт", "Длина", "Игр", "Время", "Последняя игра"]
        )
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for col in range(1, 7):
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.empty_label = QLabel("Результатов пока нет.")
        self.empty_label.setAlignment(Qt.AlignCenter)
        apply_minigame_label_style(self.empty_label, "secondary")
        self.content_layout.addWidget(self.table)
        self.content_layout.addWidget(self.empty_label)

    def _load_rows(self) -> None:
        entries = self.score_store.get_snake_leaderboard()
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            self._set_row(row, entry)
        has_rows = bool(entries)
        self.table.setVisible(has_rows)
        self.empty_label.setVisible(not has_rows)

    def _set_row(self, row: int, entry: Dict[str, Any]) -> None:
        values: List[str] = [
            str(entry.get("full_name") or ""),
            role_label(entry.get("role")),
            str(entry.get("best_score") or 0),
            str(entry.get("best_length") or 1),
            str(entry.get("games_played") or 0),
            _format_duration(int(entry.get("best_duration_sec") or 0)),
            _format_dt(entry.get("last_played_at")),
        ]
        for col, value in enumerate(values):
            item = QTableWidgetItem(value)
            if col in (2, 3, 4, 5):
                item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, col, item)


def _format_dt(value) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    return parsed.strftime("%d.%m.%Y %H:%M")


def _format_duration(duration_sec: int) -> str:
    if duration_sec <= 0:
        return ""
    minutes, seconds = divmod(duration_sec, 60)
    return f"{minutes:02d}:{seconds:02d}"
