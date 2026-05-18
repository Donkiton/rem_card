from __future__ import annotations

import random
import time
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from PySide6.QtCore import Qt, QTimer, QSize, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget


GAME_LABELS = {
    "dodge": "Dodge",
    "tetris": "Тетрис",
    "minesweeper": "Сапёр",
    "game_2048": "2048",
}

KEY_LEFT = Qt.Key.Key_Left
KEY_RIGHT = Qt.Key.Key_Right
KEY_UP = Qt.Key.Key_Up
KEY_DOWN = Qt.Key.Key_Down
KEY_SPACE = Qt.Key.Key_Space
KEY_P = Qt.Key.Key_P
STRONG_FOCUS = Qt.FocusPolicy.StrongFocus
ALIGN_CENTER = Qt.AlignmentFlag.AlignCenter
RIGHT_BUTTON = Qt.MouseButton.RightButton
_MINE_ICON: Optional[QIcon] = None


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def create_arcade_game_widget(game_key: str, parent: Optional[QWidget] = None, on_finished: Optional[Callable] = None) -> QWidget:
    clean_key = str(game_key or "").strip().lower()
    if clean_key == "dodge":
        return DodgeGameWidget(parent=parent, on_finished=on_finished)
    if clean_key == "tetris":
        return TetrisGameWidget(parent=parent, on_finished=on_finished)
    if clean_key == "minesweeper":
        return MinesweeperGameWidget(parent=parent, on_finished=on_finished)
    if clean_key == "game_2048":
        return Game2048Widget(parent=parent, on_finished=on_finished)
    raise ValueError("Неизвестная миниигра.")


def arcade_game_size(game_key: str) -> Tuple[int, int]:
    return {
        "dodge": (720, 600),
        "tetris": (560, 690),
        "minesweeper": (430, 470),
        "game_2048": (470, 560),
    }.get(str(game_key or "").strip().lower(), (620, 620))


def _notdone_icon() -> QIcon:
    global _MINE_ICON
    if _MINE_ICON is not None:
        return _MINE_ICON
    from rem_card.app.paths import get_icon_dir

    pixmap = QPixmap(f"{get_icon_dir()}\\notdone.png")
    icon = QIcon()
    if not pixmap.isNull():
        icon.addPixmap(pixmap, QIcon.Normal, QIcon.Off)
        icon.addPixmap(pixmap, QIcon.Disabled, QIcon.Off)
    _MINE_ICON = icon
    return _MINE_ICON


# -----------------------------
# Dodge
# -----------------------------

class DodgeGameWidget(QWidget):
    def __init__(self, parent: Optional[QWidget] = None, on_finished: Optional[Callable] = None):
        super().__init__(parent)
        self.on_finished = on_finished
        self.setFocusPolicy(STRONG_FOCUS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        top = QHBoxLayout()
        self.score_label = QLabel("Счёт: 0", self)
        self.status_label = QLabel("", self)
        self.status_label.setAlignment(ALIGN_CENTER)
        self.new_button = QPushButton("Новая игра", self)
        self.new_button.setObjectName("secondaryButton")
        self.new_button.clicked.connect(self.start_new_game)
        top.addWidget(self.score_label)
        top.addWidget(self.status_label, 1)
        top.addWidget(self.new_button)
        layout.addLayout(top)

        self.board = DodgeBoard(self)
        self.board.state_changed.connect(self._state_changed)
        self.board.finished.connect(self._finished)
        layout.addWidget(self.board, alignment=ALIGN_CENTER)
        self.setFocusProxy(self.board)

    def start_new_game(self) -> None:
        self.status_label.setText("Игра идёт")
        self.board.reset()
        self.board.start()
        self.board.setFocus()

    def stop_game(self) -> None:
        self.board.timer.stop()

    def _state_changed(self, state: Dict) -> None:
        self.score_label.setText(
            f"Счёт: {_safe_int(state.get('score'), 0)} | Время: {_safe_float(state.get('survival_sec'), 0):.1f} c | Увернулся: {_safe_int(state.get('dodged'), 0)}"
        )

    def _finished(self, payload: Dict) -> None:
        self.status_label.setText("Игра окончена")
        if self.on_finished:
            self.on_finished(
                _safe_int(payload.get("score"), 0),
                str(payload.get("result") or "dead"),
                _safe_float(payload.get("duration_sec"), 0.0),
                payload.get("extra") if isinstance(payload.get("extra"), dict) else {},
            )


class DodgeBoard(QWidget):
    state_changed = Signal(dict)
    finished = Signal(dict)

    WIDTH = 640
    HEIGHT = 420
    PLAYER_SIZE = 24
    STEP_MS = 25

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.setFocusPolicy(STRONG_FOCUS)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.reset()

    def reset(self) -> None:
        self.player_x = self.WIDTH / 2 - self.PLAYER_SIZE / 2
        self.player_y = self.HEIGHT - self.PLAYER_SIZE - 18
        self.obstacles: List[Dict[str, float]] = []
        self.pressed_keys: Set[int] = set()
        self.started_at = time.monotonic()
        self.last_spawn_at = self.started_at
        self.spawn_interval = 0.55
        self.dodged = 0
        self.score = 0
        self.is_finished = False
        self.is_paused = False
        self.state_changed.emit(self._state())
        self.update()

    def start(self) -> None:
        self.started_at = time.monotonic()
        self.last_spawn_at = self.started_at
        self.timer.start(self.STEP_MS)

    def sizeHint(self) -> QSize:
        return QSize(self.WIDTH, self.HEIGHT)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key in (KEY_SPACE, KEY_P):
            self._pause_toggle()
            return
        if key in (KEY_LEFT, KEY_RIGHT, KEY_UP, KEY_DOWN):
            self.pressed_keys.add(int(key))
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key in (KEY_LEFT, KEY_RIGHT, KEY_UP, KEY_DOWN):
            self.pressed_keys.discard(int(key))
            return
        super().keyReleaseEvent(event)

    def _pause_toggle(self) -> None:
        if self.is_finished:
            return
        self.is_paused = not self.is_paused
        if self.is_paused:
            self.timer.stop()
        else:
            self.timer.start(self.STEP_MS)
        self.update()

    def _spawn_obstacle(self) -> None:
        size = random.randint(18, 44)
        x = random.randint(0, max(0, self.WIDTH - size))
        survival = time.monotonic() - self.started_at
        speed = random.uniform(3.2, 5.4) + min(4.0, survival / 18.0)
        self.obstacles.append({"x": float(x), "y": float(-size), "w": float(size), "h": float(size), "speed": float(speed)})

    def _tick(self) -> None:
        if self.is_finished or self.is_paused:
            return

        now = time.monotonic()
        survival = now - self.started_at
        self.spawn_interval = max(0.18, 0.55 - survival / 120.0)
        while now - self.last_spawn_at >= self.spawn_interval:
            self._spawn_obstacle()
            self.last_spawn_at += self.spawn_interval

        speed = 7.0
        if int(KEY_LEFT) in self.pressed_keys:
            self.player_x -= speed
        if int(KEY_RIGHT) in self.pressed_keys:
            self.player_x += speed
        if int(KEY_UP) in self.pressed_keys:
            self.player_y -= speed
        if int(KEY_DOWN) in self.pressed_keys:
            self.player_y += speed

        self.player_x = max(0, min(self.WIDTH - self.PLAYER_SIZE, self.player_x))
        self.player_y = max(0, min(self.HEIGHT - self.PLAYER_SIZE, self.player_y))

        active: List[Dict[str, float]] = []
        for obs in self.obstacles:
            obs["y"] += obs["speed"]
            if obs["y"] > self.HEIGHT:
                self.dodged += 1
            else:
                active.append(obs)
        self.obstacles = active

        player = (self.player_x, self.player_y, self.PLAYER_SIZE, self.PLAYER_SIZE)
        for obs in self.obstacles:
            other = (obs["x"], obs["y"], obs["w"], obs["h"])
            if self._intersects(player, other):
                self._finish()
                return

        self.score = int(survival * 10) + self.dodged * 3
        self.state_changed.emit(self._state())
        self.update()

    @staticmethod
    def _intersects(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> bool:
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        return ax < bx + bw and ax + aw > bx and ay < by + bh and ay + ah > by

    def _state(self) -> Dict:
        survival = 0.0 if not hasattr(self, "started_at") else time.monotonic() - self.started_at
        return {"score": self.score, "survival_sec": survival, "dodged": self.dodged}

    def _finish(self) -> None:
        if self.is_finished:
            return
        self.is_finished = True
        self.timer.stop()
        duration = time.monotonic() - self.started_at
        self.score = int(duration * 10) + self.dodged * 3
        self.finished.emit(
            {
                "result": "dead",
                "score": self.score,
                "duration_sec": duration,
                "extra": {"survival_sec": round(duration, 2), "dodged": self.dodged},
            }
        )
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#f7fafb"))

        painter.setPen(QPen(QColor("#e1e8ec"), 1))
        for x in range(0, self.WIDTH + 1, 20):
            painter.drawLine(x, 0, x, self.HEIGHT)
        for y in range(0, self.HEIGHT + 1, 20):
            painter.drawLine(0, y, self.WIDTH, y)

        painter.setPen(QPen(QColor("#153f61"), 1))
        painter.setBrush(QColor("#2f80b7"))
        painter.drawRect(int(self.player_x), int(self.player_y), self.PLAYER_SIZE, self.PLAYER_SIZE)

        painter.setPen(QPen(QColor("#6f1d1d"), 1))
        painter.setBrush(QColor("#b63b3b"))
        for obs in self.obstacles:
            painter.drawRect(int(obs["x"]), int(obs["y"]), int(obs["w"]), int(obs["h"]))

        if self.is_finished:
            self._paint_overlay(painter, "ИГРА ОКОНЧЕНА")
        elif self.is_paused:
            self._paint_overlay(painter, "ПАУЗА")

    def _paint_overlay(self, painter: QPainter, text: str) -> None:
        painter.fillRect(self.rect(), QColor(0, 0, 0, 80))
        painter.setPen(QColor("#ffffff"))
        painter.drawText(self.rect(), ALIGN_CENTER, text)


# -----------------------------
# Тетрис
# -----------------------------

TETRIS_SHAPES: Dict[str, List[List[int]]] = {
    "I": [[1, 1, 1, 1]],
    "O": [[1, 1], [1, 1]],
    "T": [[0, 1, 0], [1, 1, 1]],
    "S": [[0, 1, 1], [1, 1, 0]],
    "Z": [[1, 1, 0], [0, 1, 1]],
    "J": [[1, 0, 0], [1, 1, 1]],
    "L": [[0, 0, 1], [1, 1, 1]],
}
TETRIS_COLORS = {
    "I": "#3aa6c8",
    "O": "#d9b642",
    "T": "#8b5eb8",
    "S": "#4f9d5d",
    "Z": "#c45151",
    "J": "#4a6db5",
    "L": "#d58a36",
}


class TetrisGameWidget(QWidget):
    def __init__(self, parent: Optional[QWidget] = None, on_finished: Optional[Callable] = None):
        super().__init__(parent)
        self.on_finished = on_finished
        self.setFocusPolicy(STRONG_FOCUS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        top = QHBoxLayout()
        self.score_label = QLabel("Счёт: 0", self)
        self.status_label = QLabel("", self)
        self.status_label.setAlignment(ALIGN_CENTER)
        self.new_button = QPushButton("Новая игра", self)
        self.new_button.setObjectName("secondaryButton")
        self.new_button.clicked.connect(self.start_new_game)
        top.addWidget(self.score_label)
        top.addWidget(self.status_label, 1)
        top.addWidget(self.new_button)
        layout.addLayout(top)

        hint = QLabel("←/→ — движение, ↑ — поворот, ↓ — ускорить, пробел — сброс, P — пауза", self)
        hint.setAlignment(ALIGN_CENTER)
        layout.addWidget(hint)

        self.board = TetrisBoard(self)
        self.board.state_changed.connect(self._state_changed)
        self.board.next_piece_changed.connect(self._next_piece_changed)
        self.board.finished.connect(self._finished)
        game_row = QHBoxLayout()
        game_row.setSpacing(12)
        self.next_preview = TetrisNextPreview(self)
        game_row.addWidget(self.board)
        game_row.addWidget(self.next_preview, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(game_row)
        self.setFocusProxy(self.board)

    def start_new_game(self) -> None:
        self.status_label.setText("Игра идёт")
        self.board.reset()
        self.board.start()
        self.board.setFocus()

    def stop_game(self) -> None:
        self.board.timer.stop()

    def _state_changed(self, state: Dict) -> None:
        self.score_label.setText(
            f"Счёт: {_safe_int(state.get('score'), 0)} | Линии: {_safe_int(state.get('lines'), 0)} | Уровень: {_safe_int(state.get('level'), 1)}"
        )

    def _next_piece_changed(self, piece_name: str) -> None:
        self.next_preview.set_piece(piece_name)

    def _finished(self, payload: Dict) -> None:
        self.status_label.setText("Игра окончена")
        if self.on_finished:
            self.on_finished(
                _safe_int(payload.get("score"), 0),
                "game_over",
                _safe_float(payload.get("duration_sec"), 0.0),
                payload.get("extra") if isinstance(payload.get("extra"), dict) else {},
            )


class TetrisBoard(QWidget):
    state_changed = Signal(dict)
    next_piece_changed = Signal(str)
    finished = Signal(dict)

    COLUMNS = 10
    ROWS = 20
    CELL = 24
    WIDTH = COLUMNS * CELL
    HEIGHT = ROWS * CELL

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.setFocusPolicy(STRONG_FOCUS)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._step_down)
        self.reset()

    def reset(self) -> None:
        self.grid: List[List[Optional[str]]] = [[None for _ in range(self.COLUMNS)] for _ in range(self.ROWS)]
        self.current_name = ""
        self.current_shape: List[List[int]] = []
        self.next_name = self._random_piece_name()
        self.current_x = 0
        self.current_y = 0
        self.score = 0
        self.lines = 0
        self.level = 1
        self.is_finished = False
        self.is_paused = False
        self.started_at = time.monotonic()
        self._spawn_piece()
        self.state_changed.emit(self._state())
        self.update()

    def start(self) -> None:
        self.started_at = time.monotonic()
        self.timer.start(self._drop_interval())

    def sizeHint(self) -> QSize:
        return QSize(self.WIDTH, self.HEIGHT)

    def _drop_interval(self) -> int:
        return max(90, 560 - (self.level - 1) * 45)

    def _state(self) -> Dict:
        return {"score": self.score, "lines": self.lines, "level": self.level}

    def _random_piece_name(self) -> str:
        return random.choice(list(TETRIS_SHAPES.keys()))

    def _spawn_piece(self) -> None:
        self.current_name = self.next_name or self._random_piece_name()
        self.current_shape = [row[:] for row in TETRIS_SHAPES[self.current_name]]
        self.next_name = self._random_piece_name()
        self.next_piece_changed.emit(self.next_name)
        self.current_x = self.COLUMNS // 2 - len(self.current_shape[0]) // 2
        self.current_y = 0
        if self._collides(self.current_shape, self.current_x, self.current_y):
            self._finish()

    def _rotated(self, shape: List[List[int]]) -> List[List[int]]:
        return [list(row) for row in zip(*shape[::-1])]

    def _collides(self, shape: List[List[int]], x: int, y: int) -> bool:
        for row_idx, row in enumerate(shape):
            for col_idx, filled in enumerate(row):
                if not filled:
                    continue
                nx = x + col_idx
                ny = y + row_idx
                if nx < 0 or nx >= self.COLUMNS or ny >= self.ROWS:
                    return True
                if ny >= 0 and self.grid[ny][nx] is not None:
                    return True
        return False

    def _merge_current(self) -> None:
        for row_idx, row in enumerate(self.current_shape):
            for col_idx, filled in enumerate(row):
                if not filled:
                    continue
                nx = self.current_x + col_idx
                ny = self.current_y + row_idx
                if 0 <= nx < self.COLUMNS and 0 <= ny < self.ROWS:
                    self.grid[ny][nx] = self.current_name

    def _clear_lines(self) -> int:
        new_grid = [row for row in self.grid if any(cell is None for cell in row)]
        cleared = self.ROWS - len(new_grid)
        for _ in range(cleared):
            new_grid.insert(0, [None for _ in range(self.COLUMNS)])
        self.grid = new_grid
        return cleared

    def _step_down(self) -> None:
        if self.is_finished or self.is_paused:
            return
        if not self._collides(self.current_shape, self.current_x, self.current_y + 1):
            self.current_y += 1
        else:
            self._merge_current()
            cleared = self._clear_lines()
            if cleared:
                scores = {1: 100, 2: 300, 3: 500, 4: 800}
                self.score += scores.get(cleared, cleared * 250) * self.level
                self.lines += cleared
                self.level = self.lines // 10 + 1
                self.timer.setInterval(self._drop_interval())
            self._spawn_piece()
        self.state_changed.emit(self._state())
        self.update()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if self.is_finished:
            return
        key = event.key()
        if key in (KEY_SPACE,):
            self._hard_drop()
            return
        if key == KEY_P:
            self._pause_toggle()
            return
        if self.is_paused:
            return
        if key == KEY_LEFT:
            if not self._collides(self.current_shape, self.current_x - 1, self.current_y):
                self.current_x -= 1
        elif key == KEY_RIGHT:
            if not self._collides(self.current_shape, self.current_x + 1, self.current_y):
                self.current_x += 1
        elif key == KEY_DOWN:
            if not self._collides(self.current_shape, self.current_x, self.current_y + 1):
                self.current_y += 1
                self.score += 1
        elif key == KEY_UP:
            rotated = self._rotated(self.current_shape)
            if not self._collides(rotated, self.current_x, self.current_y):
                self.current_shape = rotated
        else:
            super().keyPressEvent(event)
            return
        self.state_changed.emit(self._state())
        self.update()

    def _hard_drop(self) -> None:
        if self.is_finished or self.is_paused:
            return
        steps = 0
        while not self._collides(self.current_shape, self.current_x, self.current_y + 1):
            self.current_y += 1
            steps += 1
        self.score += steps * 2
        self._step_down()

    def _pause_toggle(self) -> None:
        if self.is_finished:
            return
        self.is_paused = not self.is_paused
        if self.is_paused:
            self.timer.stop()
        else:
            self.timer.start(self._drop_interval())
        self.update()

    def _finish(self) -> None:
        if self.is_finished:
            return
        self.is_finished = True
        self.timer.stop()
        duration = time.monotonic() - self.started_at
        self.finished.emit(
            {
                "score": self.score,
                "duration_sec": duration,
                "extra": {"lines": self.lines, "level": self.level},
            }
        )
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#f7fafb"))

        for y in range(self.ROWS):
            for x in range(self.COLUMNS):
                name = self.grid[y][x]
                if name is not None:
                    self._draw_cell(painter, x, y, TETRIS_COLORS.get(name, "#4f6f8f"))

        if not self.is_finished:
            for row_idx, row in enumerate(self.current_shape):
                for col_idx, filled in enumerate(row):
                    if not filled:
                        continue
                    self._draw_cell(
                        painter,
                        self.current_x + col_idx,
                        self.current_y + row_idx,
                        TETRIS_COLORS.get(self.current_name, "#4f6f8f"),
                    )

        painter.setPen(QPen(QColor("#d8e3e8"), 1))
        for x in range(self.COLUMNS + 1):
            painter.drawLine(x * self.CELL, 0, x * self.CELL, self.HEIGHT)
        for y in range(self.ROWS + 1):
            painter.drawLine(0, y * self.CELL, self.WIDTH, y * self.CELL)

        if self.is_finished:
            self._paint_overlay(painter, "ИГРА ОКОНЧЕНА")
        elif self.is_paused:
            self._paint_overlay(painter, "ПАУЗА")

    def _draw_cell(self, painter: QPainter, x: int, y: int, color: str) -> None:
        painter.setPen(QPen(QColor("#274052"), 1))
        painter.setBrush(QColor(color))
        painter.drawRect(x * self.CELL + 1, y * self.CELL + 1, self.CELL - 2, self.CELL - 2)

    def _paint_overlay(self, painter: QPainter, text: str) -> None:
        painter.fillRect(self.rect(), QColor(0, 0, 0, 80))
        painter.setPen(QColor("#ffffff"))
        painter.drawText(self.rect(), ALIGN_CENTER, text)


class TetrisNextPreview(QWidget):
    CELL = 18
    BOX = 108

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.piece_name = ""
        self.setFixedSize(self.BOX, self.BOX + 34)

    def set_piece(self, piece_name: str) -> None:
        self.piece_name = str(piece_name or "")
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#f7fafb"))
        painter.setPen(QColor("#2c3e50"))
        painter.setFont(QFont("Segoe UI", 9, QFont.Bold))
        painter.drawText(0, 0, self.width(), 24, ALIGN_CENTER, "Следующая")

        box_y = 30
        painter.setPen(QPen(QColor("#d8e3e8"), 1))
        painter.setBrush(QColor("#eef4f7"))
        painter.drawRect(0, box_y, self.BOX - 1, self.BOX - 1)

        shape = TETRIS_SHAPES.get(self.piece_name)
        if not shape:
            return
        color = TETRIS_COLORS.get(self.piece_name, "#4f6f8f")
        rows = len(shape)
        cols = max(len(row) for row in shape)
        offset_x = (self.BOX - cols * self.CELL) // 2
        offset_y = box_y + (self.BOX - rows * self.CELL) // 2
        for row_idx, row in enumerate(shape):
            for col_idx, filled in enumerate(row):
                if not filled:
                    continue
                x = offset_x + col_idx * self.CELL
                y = offset_y + row_idx * self.CELL
                painter.setPen(QPen(QColor("#274052"), 1))
                painter.setBrush(QColor(color))
                painter.drawRect(x + 1, y + 1, self.CELL - 2, self.CELL - 2)


# -----------------------------
# Сапёр
# -----------------------------

class MineCellButton(QPushButton):
    left_clicked = Signal(int, int)
    right_clicked = Signal(int, int)

    def __init__(self, row: int, col: int, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.row = row
        self.col = col
        self.setObjectName("mineCell")

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == RIGHT_BUTTON:
            self.right_clicked.emit(self.row, self.col)
            return
        self.left_clicked.emit(self.row, self.col)


class MinesweeperGameWidget(QWidget):
    ROWS = 9
    COLS = 9
    MINES = 10
    CELL_SIZE = 34
    GRID_SPACING = 2

    def __init__(self, parent: Optional[QWidget] = None, on_finished: Optional[Callable] = None):
        super().__init__(parent)
        self.on_finished = on_finished
        self.buttons: List[List[MineCellButton]] = []
        self.setFocusPolicy(STRONG_FOCUS)
        cell_content_size = self.CELL_SIZE - 2
        self.setStyleSheet(
            f"""
            QPushButton#mineCell {{
                min-width: {cell_content_size}px;
                max-width: {cell_content_size}px;
                min-height: {cell_content_size}px;
                max-height: {cell_content_size}px;
                padding: 0px;
                border: 1px solid #b7c6cf;
                border-radius: 4px;
                font-weight: 800;
            }}
            QPushButton#mineCell:disabled {{
                background-color: #eef4f7;
                color: #2c3e50;
                border-color: #d8e3e8;
            }}
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        top = QHBoxLayout()
        self.status_label = QLabel("", self)
        self.status_label.setAlignment(ALIGN_CENTER)
        self.new_button = QPushButton("Новая игра", self)
        self.new_button.setObjectName("secondaryButton")
        self.new_button.clicked.connect(self.start_new_game)
        top.addWidget(self.status_label, 1)
        top.addWidget(self.new_button)
        layout.addLayout(top)

        hint = QLabel("ЛКМ — открыть клетку, ПКМ — поставить/снять флаг", self)
        hint.setAlignment(ALIGN_CENTER)
        layout.addWidget(hint)

        self.grid_widget = QWidget(self)
        self.grid_layout = QGridLayout(self.grid_widget)
        self.grid_layout.setSpacing(self.GRID_SPACING)
        self.grid_layout.setContentsMargins(0, 0, 0, 0)
        board_size = self.COLS * self.CELL_SIZE + (self.COLS - 1) * self.GRID_SPACING
        self.grid_widget.setFixedSize(board_size, board_size)
        for r in range(self.ROWS):
            row: List[MineCellButton] = []
            for c in range(self.COLS):
                btn = MineCellButton(r, c, self.grid_widget)
                btn.setFixedSize(self.CELL_SIZE, self.CELL_SIZE)
                btn.left_clicked.connect(self._open_from_click)
                btn.right_clicked.connect(self._toggle_flag)
                self.grid_layout.addWidget(btn, r, c)
                row.append(btn)
            self.buttons.append(row)
        layout.addWidget(self.grid_widget, alignment=ALIGN_CENTER)

        self.start_new_game()

    def start_new_game(self) -> None:
        self.first_click = True
        self.is_finished = False
        self.started_at = time.monotonic()
        self.moves = 0
        self.mines: Set[Tuple[int, int]] = set()
        self.opened: Set[Tuple[int, int]] = set()
        self.flags: Set[Tuple[int, int]] = set()
        self.numbers: Dict[Tuple[int, int], int] = {}
        for r in range(self.ROWS):
            for c in range(self.COLS):
                btn = self.buttons[r][c]
                btn.setEnabled(True)
                btn.setText("")
                btn.setIcon(QIcon())
                btn.setStyleSheet("")
        self._update_status("Игра идёт")

    def stop_game(self) -> None:
        pass

    def _update_status(self, prefix: str) -> None:
        flags_left = self.MINES - len(self.flags)
        duration = time.monotonic() - self.started_at if hasattr(self, "started_at") else 0.0
        self.status_label.setText(f"{prefix} | Мины: {self.MINES} | Флаги: {flags_left} | Время: {duration:.0f} c")

    def _neighbors(self, row: int, col: int) -> List[Tuple[int, int]]:
        result = []
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                nr, nc = row + dr, col + dc
                if 0 <= nr < self.ROWS and 0 <= nc < self.COLS:
                    result.append((nr, nc))
        return result

    def _generate_mines(self, safe_row: int, safe_col: int) -> None:
        excluded = {(safe_row, safe_col), *self._neighbors(safe_row, safe_col)}
        candidates = [(r, c) for r in range(self.ROWS) for c in range(self.COLS) if (r, c) not in excluded]
        self.mines = set(random.sample(candidates, self.MINES))
        self.numbers = {}
        for r in range(self.ROWS):
            for c in range(self.COLS):
                if (r, c) in self.mines:
                    continue
                self.numbers[(r, c)] = sum((nr, nc) in self.mines for nr, nc in self._neighbors(r, c))

    def _open_from_click(self, row: int, col: int) -> None:
        if self.is_finished or (row, col) in self.flags or (row, col) in self.opened:
            return
        if self.first_click:
            self._generate_mines(row, col)
            self.first_click = False
            self.started_at = time.monotonic()
        self.moves += 1
        if (row, col) in self.mines:
            self._finish(False)
            return
        self._open_cell(row, col)
        if len(self.opened) >= self.ROWS * self.COLS - self.MINES:
            self._finish(True)
        else:
            self._update_status("Игра идёт")

    def _toggle_flag(self, row: int, col: int) -> None:
        if self.is_finished or (row, col) in self.opened:
            return
        cell = (row, col)
        btn = self.buttons[row][col]
        if cell in self.flags:
            self.flags.remove(cell)
            btn.setText("")
            btn.setIcon(QIcon())
        else:
            btn.setIcon(QIcon())
            self.flags.add(cell)
            btn.setText("F")
        self._update_status("Игра идёт")

    def _open_cell(self, row: int, col: int) -> None:
        stack = [(row, col)]
        while stack:
            r, c = stack.pop()
            cell = (r, c)
            if cell in self.opened or cell in self.flags:
                continue
            self.opened.add(cell)
            btn = self.buttons[r][c]
            btn.setEnabled(False)
            btn.setIcon(QIcon())
            count = self.numbers.get(cell, 0)
            if count > 0:
                btn.setText(str(count))
            else:
                btn.setText("")
                for nr, nc in self._neighbors(r, c):
                    if (nr, nc) not in self.opened and (nr, nc) not in self.mines:
                        stack.append((nr, nc))

    def _reveal_all(self) -> None:
        for r in range(self.ROWS):
            for c in range(self.COLS):
                btn = self.buttons[r][c]
                btn.setEnabled(False)
                if (r, c) in self.mines:
                    btn.setText("")
                    btn.setIcon(_notdone_icon())
                    btn.setIconSize(QSize(18, 18))
                elif (r, c) in self.opened:
                    btn.setIcon(QIcon())
                    count = self.numbers.get((r, c), 0)
                    btn.setText(str(count) if count else "")

    def _finish(self, is_win: bool) -> None:
        if self.is_finished:
            return
        self.is_finished = True
        duration = time.monotonic() - self.started_at
        self._reveal_all()
        if is_win:
            self._update_status("Победа")
            score = max(1, 100000 - int(duration * 100) - self.moves * 5)
            result = "win"
        else:
            self._update_status("Проигрыш")
            score = len(self.opened)
            result = "dead"
        if self.on_finished:
            self.on_finished(
                score,
                result,
                duration,
                {
                    "field": f"{self.COLS}×{self.ROWS}",
                    "mines": self.MINES,
                    "moves": self.moves,
                    "opened": len(self.opened),
                    "flags": len(self.flags),
                    "time_sec": round(duration, 2),
                },
            )


# -----------------------------
# 2048
# -----------------------------

class Game2048Widget(QWidget):
    def __init__(self, parent: Optional[QWidget] = None, on_finished: Optional[Callable] = None):
        super().__init__(parent)
        self.on_finished = on_finished
        self.setFocusPolicy(STRONG_FOCUS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        top = QHBoxLayout()
        self.score_label = QLabel("Счёт: 0", self)
        self.status_label = QLabel("", self)
        self.status_label.setAlignment(ALIGN_CENTER)
        self.new_button = QPushButton("Новая игра", self)
        self.new_button.setObjectName("secondaryButton")
        self.new_button.clicked.connect(self.start_new_game)
        top.addWidget(self.score_label)
        top.addWidget(self.status_label, 1)
        top.addWidget(self.new_button)
        layout.addLayout(top)

        hint = QLabel("Стрелки — сдвиг плиток. Результат сохраняется, когда ходов больше нет.", self)
        hint.setAlignment(ALIGN_CENTER)
        layout.addWidget(hint)

        self.board = Board2048(self)
        self.board.state_changed.connect(self._state_changed)
        self.board.finished.connect(self._finished)
        layout.addWidget(self.board, alignment=ALIGN_CENTER)
        self.setFocusProxy(self.board)

    def start_new_game(self) -> None:
        self.status_label.setText("Игра идёт")
        self.board.reset()
        self.board.setFocus()

    def stop_game(self) -> None:
        pass

    def _state_changed(self, state: Dict) -> None:
        self.score_label.setText(f"Счёт: {_safe_int(state.get('score'), 0)} | Макс. плитка: {_safe_int(state.get('max_tile'), 0)}")
        if _safe_int(state.get("max_tile"), 0) >= 2048 and not self.board.is_finished:
            self.status_label.setText("2048 достигнута")

    def _finished(self, payload: Dict) -> None:
        max_tile = _safe_int(payload.get("max_tile"), 0)
        self.status_label.setText("Игра окончена")
        result = "win" if max_tile >= 2048 else "dead"
        if self.on_finished:
            self.on_finished(
                _safe_int(payload.get("score"), 0),
                result,
                _safe_float(payload.get("duration_sec"), 0.0),
                {"max_tile": max_tile, "moves": _safe_int(payload.get("moves"), 0)},
            )


class Board2048(QWidget):
    state_changed = Signal(dict)
    finished = Signal(dict)

    SIZE = 4
    CELL = 86
    GAP = 8
    BOARD = SIZE * CELL + (SIZE + 1) * GAP

    TILE_COLORS = {
        0: "#d9e4ea",
        2: "#eee4da",
        4: "#ede0c8",
        8: "#f2b179",
        16: "#f59563",
        32: "#f67c5f",
        64: "#f65e3b",
        128: "#edcf72",
        256: "#edcc61",
        512: "#edc850",
        1024: "#edc53f",
        2048: "#edc22e",
    }

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedSize(self.BOARD, self.BOARD)
        self.setFocusPolicy(STRONG_FOCUS)
        self.reset()

    def reset(self) -> None:
        self.grid: List[List[int]] = [[0 for _ in range(self.SIZE)] for _ in range(self.SIZE)]
        self.score = 0
        self.moves = 0
        self.is_finished = False
        self.started_at = time.monotonic()
        self._add_tile()
        self._add_tile()
        self.state_changed.emit(self._state())
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(self.BOARD, self.BOARD)

    def _state(self) -> Dict:
        return {"score": self.score, "max_tile": self._max_tile(), "moves": self.moves}

    def _max_tile(self) -> int:
        return max(max(row) for row in self.grid)

    def _add_tile(self) -> None:
        empty = [(r, c) for r in range(self.SIZE) for c in range(self.SIZE) if self.grid[r][c] == 0]
        if not empty:
            return
        r, c = random.choice(empty)
        self.grid[r][c] = 4 if random.random() < 0.1 else 2

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if self.is_finished:
            return
        key = event.key()
        if key == KEY_LEFT:
            moved, gained = self._move_left()
        elif key == KEY_RIGHT:
            moved, gained = self._move_right()
        elif key == KEY_UP:
            moved, gained = self._move_up()
        elif key == KEY_DOWN:
            moved, gained = self._move_down()
        else:
            super().keyPressEvent(event)
            return

        if moved:
            self.moves += 1
            self.score += gained
            self._add_tile()
            self.state_changed.emit(self._state())
            if not self._can_move():
                self._finish()
            self.update()

    def _compress_line(self, line: Sequence[int]) -> Tuple[List[int], int]:
        values = [v for v in line if v]
        merged: List[int] = []
        gained = 0
        idx = 0
        while idx < len(values):
            if idx + 1 < len(values) and values[idx] == values[idx + 1]:
                value = values[idx] * 2
                merged.append(value)
                gained += value
                idx += 2
            else:
                merged.append(values[idx])
                idx += 1
        merged += [0] * (self.SIZE - len(merged))
        return merged, gained

    def _move_left(self) -> Tuple[bool, int]:
        old = [row[:] for row in self.grid]
        gained = 0
        for r in range(self.SIZE):
            self.grid[r], add = self._compress_line(self.grid[r])
            gained += add
        return old != self.grid, gained

    def _move_right(self) -> Tuple[bool, int]:
        old = [row[:] for row in self.grid]
        gained = 0
        for r in range(self.SIZE):
            line, add = self._compress_line(list(reversed(self.grid[r])))
            self.grid[r] = list(reversed(line))
            gained += add
        return old != self.grid, gained

    def _move_up(self) -> Tuple[bool, int]:
        old = [row[:] for row in self.grid]
        gained = 0
        for c in range(self.SIZE):
            column = [self.grid[r][c] for r in range(self.SIZE)]
            line, add = self._compress_line(column)
            for r in range(self.SIZE):
                self.grid[r][c] = line[r]
            gained += add
        return old != self.grid, gained

    def _move_down(self) -> Tuple[bool, int]:
        old = [row[:] for row in self.grid]
        gained = 0
        for c in range(self.SIZE):
            column = [self.grid[r][c] for r in range(self.SIZE - 1, -1, -1)]
            line, add = self._compress_line(column)
            line = list(reversed(line))
            for r in range(self.SIZE):
                self.grid[r][c] = line[r]
            gained += add
        return old != self.grid, gained

    def _can_move(self) -> bool:
        for r in range(self.SIZE):
            for c in range(self.SIZE):
                if self.grid[r][c] == 0:
                    return True
                if c + 1 < self.SIZE and self.grid[r][c] == self.grid[r][c + 1]:
                    return True
                if r + 1 < self.SIZE and self.grid[r][c] == self.grid[r + 1][c]:
                    return True
        return False

    def _finish(self) -> None:
        if self.is_finished:
            return
        self.is_finished = True
        duration = time.monotonic() - self.started_at
        self.finished.emit({"score": self.score, "duration_sec": duration, "max_tile": self._max_tile(), "moves": self.moves})
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#bbcada"))
        font = QFont()
        font.setBold(True)
        font.setPointSize(16)
        painter.setFont(font)

        for r in range(self.SIZE):
            for c in range(self.SIZE):
                value = self.grid[r][c]
                x = self.GAP + c * (self.CELL + self.GAP)
                y = self.GAP + r * (self.CELL + self.GAP)
                color = self.TILE_COLORS.get(value, "#3c4b58")
                painter.setPen(QPen(QColor("#9eb4c2"), 1))
                painter.setBrush(QColor(color))
                painter.drawRect(x, y, self.CELL, self.CELL)
                if value:
                    painter.setPen(QColor("#1d2930") if value < 128 else QColor("#ffffff"))
                    painter.drawText(x, y, self.CELL, self.CELL, ALIGN_CENTER, str(value))

        if self.is_finished:
            painter.fillRect(self.rect(), QColor(0, 0, 0, 80))
            painter.setPen(QColor("#ffffff"))
            painter.drawText(self.rect(), ALIGN_CENTER, "ХОДОВ НЕТ")
