from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QGridLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from rem_card.ui.styles.theme import TEXT_PRIMARY


class GameSelectPage(QWidget):
    snake_requested = Signal()
    leaderboard_requested = Signal()
    arcade_requested = Signal(str)
    arcade_leaderboard_requested = Signal(str)
    back_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def set_user_name(self, full_name: str) -> None:
        pass

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)

        games = [
            ("1. Змейка", lambda checked=False: self.snake_requested.emit(), lambda checked=False: self.leaderboard_requested.emit()),
            (
                "2. Dodge",
                lambda checked=False: self.arcade_requested.emit("dodge"),
                lambda checked=False: self.arcade_leaderboard_requested.emit("dodge"),
            ),
            (
                "3. Тетрис",
                lambda checked=False: self.arcade_requested.emit("tetris"),
                lambda checked=False: self.arcade_leaderboard_requested.emit("tetris"),
            ),
            (
                "4. Сапёр",
                lambda checked=False: self.arcade_requested.emit("minesweeper"),
                lambda checked=False: self.arcade_leaderboard_requested.emit("minesweeper"),
            ),
            (
                "5. 2048",
                lambda checked=False: self.arcade_requested.emit("game_2048"),
                lambda checked=False: self.arcade_leaderboard_requested.emit("game_2048"),
            ),
        ]
        for row, (title, load_handler, leaderboard_handler) in enumerate(games):
            name = QLabel(title)
            name.setStyleSheet(f"font-weight: bold; color: {TEXT_PRIMARY};")
            load_btn = QPushButton("Загрузить")
            leaders_btn = QPushButton("Лидеры")
            load_btn.clicked.connect(load_handler)
            leaders_btn.clicked.connect(leaderboard_handler)
            grid.addWidget(name, row, 0)
            grid.addWidget(load_btn, row, 1)
            grid.addWidget(leaders_btn, row, 2)

        back_btn = QPushButton("Назад")
        back_btn.clicked.connect(self.back_requested.emit)

        layout.addLayout(grid)
        layout.addStretch()
        layout.addWidget(back_btn)
