from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QLabel, QPushButton, QTableWidget, QTableWidgetItem

from rem_card.services.minigames.minigame_score_store import ARCADE_GAME_LABELS
from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.shared.minigames.user_select_page import role_label
from rem_card.ui.styles.theme import TEXT_SECONDARY


class ArcadeLeaderboardDialog(BaseStyledDialog):
    def __init__(self, score_store, game_key: str, parent=None):
        self.game_key = str(game_key or "").strip().lower()
        self.score_store = score_store
        title = ARCADE_GAME_LABELS.get(self.game_key, self.game_key)
        super().__init__(f"Лидеры: {title}", parent)
        self.resize(940, 420)
        self._build_ui()
        self._load_rows()

    def _build_ui(self) -> None:
        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            ["Место", "ФИО", "Роль", "Лучший счёт", "Исход", "Время", "Детали", "Игр", "Дата"]
        )
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for col in (0, 2, 3, 4, 5, 6, 7, 8):
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeToContents)

        self.empty_label = QLabel("Результатов по этой игре пока нет.")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setStyleSheet(f"color: {TEXT_SECONDARY};")

        refresh_btn = QPushButton("Обновить")
        refresh_btn.clicked.connect(self._load_rows)

        self.content_layout.addWidget(self.table)
        self.content_layout.addWidget(self.empty_label)
        self.content_layout.addWidget(refresh_btn, 0, Qt.AlignRight)

    def _load_rows(self) -> None:
        entries = self.score_store.get_arcade_leaderboard(self.game_key)
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            self._set_row(row, entry)
        has_rows = bool(entries)
        self.table.setVisible(has_rows)
        self.empty_label.setVisible(not has_rows)

    def _set_row(self, row: int, entry: Dict[str, Any]) -> None:
        values: List[str] = [
            str(row + 1),
            str(entry.get("full_name") or ""),
            role_label(entry.get("role")),
            str(int(entry.get("best_score") or 0)),
            _result_label(entry.get("best_result")),
            _format_duration(int(entry.get("best_duration_sec") or 0)),
            _extra_label(self.game_key, entry.get("best_extra")),
            str(int(entry.get("games_played") or 0)),
            _format_dt(entry.get("best_played_at")),
        ]
        for col, value in enumerate(values):
            item = QTableWidgetItem(value)
            if col in (0, 3, 5, 7, 8):
                item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, col, item)


def _result_label(value: Any) -> str:
    result = str(value or "").strip()
    if result == "win":
        return "победа"
    if result == "dead":
        return "проигрыш"
    if result == "game_over":
        return "игра окончена"
    return result or "-"


def _format_duration(duration_sec: int) -> str:
    clean = max(0, int(duration_sec or 0))
    if clean <= 0:
        return "-"
    minutes, seconds = divmod(clean, 60)
    return f"{minutes:02d}:{seconds:02d}"


def _format_dt(value: Any) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    return parsed.strftime("%d.%m.%Y %H:%M")


def _extra_label(game_key: str, extra: Any) -> str:
    if not isinstance(extra, dict) or not extra:
        return "-"
    if game_key == "dodge":
        return f"увёрнутых блоков: {int(extra.get('dodged') or 0)}"
    if game_key == "tetris":
        return f"линии: {int(extra.get('lines') or 0)}, уровень: {int(extra.get('level') or 1)}"
    if game_key == "minesweeper":
        return (
            f"{extra.get('field') or '9x9'}, мин: {int(extra.get('mines') or 10)}, "
            f"ходов: {int(extra.get('moves') or 0)}"
        )
    if game_key == "game_2048":
        return f"макс. плитка: {int(extra.get('max_tile') or 0)}, ходов: {int(extra.get('moves') or 0)}"
    return ", ".join(f"{key}: {value}" for key, value in extra.items())
