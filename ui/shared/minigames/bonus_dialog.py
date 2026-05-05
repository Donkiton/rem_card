from __future__ import annotations

from typing import Dict, Optional

from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QStackedWidget, QVBoxLayout, QWidget

from rem_card.services.minigames.minigame_score_store import ARCADE_GAME_LABELS, MinigameScoreStore
from rem_card.services.minigames.minigame_user_store import MinigameUserStore, normalize_role
from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.shared.minigames.game_select_page import GameSelectPage
from rem_card.ui.shared.minigames.user_select_page import UserSelectPage
from rem_card.ui.styles.theme import COLOR_PRIMARY_DARK, TEXT_PRIMARY


class BonusDialog(BaseStyledDialog):
    SETTINGS_POS_KEY = "bonus_dialog/last_pos"

    def __init__(self, role: str, data_root_provider, parent=None):
        super().__init__("Бонус", parent)
        self.role = normalize_role(role)
        self.user_store = MinigameUserStore(data_root_provider)
        self.score_store = MinigameScoreStore(data_root_provider, user_store=self.user_store)
        self.current_user: Optional[Dict] = None
        self.snake_widget = None
        self.snake_page = None
        self.arcade_widget = None
        self.arcade_page = None

        self.resize(520, 330)
        self._build_ui()
        self._refresh_users()
        self._restore_last_position()

    def closeEvent(self, event) -> None:
        self._save_last_position()
        self._stop_active_game()
        super().closeEvent(event)

    def reject(self) -> None:
        self._save_last_position()
        self._stop_active_game()
        super().reject()

    def _build_ui(self) -> None:
        self.stack = QStackedWidget()

        self.user_page = UserSelectPage(self.role)
        self.user_page.add_requested.connect(self._add_user)
        self.user_page.load_requested.connect(self._load_user)

        self.game_page = GameSelectPage()
        self.game_page.snake_requested.connect(self._open_snake)
        self.game_page.leaderboard_requested.connect(self._open_leaderboard)
        self.game_page.arcade_requested.connect(self._open_arcade_game)
        self.game_page.arcade_leaderboard_requested.connect(self._open_arcade_leaderboard)
        self.game_page.back_requested.connect(self._show_user_select)

        self.stack.addWidget(self.user_page)
        self.stack.addWidget(self.game_page)
        self.content_layout.addWidget(self.stack)

    def _refresh_users(self, select_user_id: str = "") -> None:
        users = self.user_store.list_users(role=self.role)
        self.user_page.set_users(users)
        if select_user_id:
            self.user_page.select_user(select_user_id)

    def _add_user(self, full_name: str, role: str) -> None:
        try:
            user = self.user_store.create_user(full_name, role)
        except Exception as exc:
            CustomMessageBox.warning(self, "Бонус", str(exc))
            return

        self.user_page.name_edit.clear()
        select_id = user["user_id"] if user.get("role") == self.role else ""
        self._refresh_users(select_id)

    def _load_user(self, user_id: str) -> None:
        try:
            user = self.user_store.get_user(user_id)
        except Exception as exc:
            CustomMessageBox.warning(self, "Бонус", f"Не удалось загрузить пользователя:\n{exc}")
            self._refresh_users()
            return
        if user.get("role") != self.role:
            CustomMessageBox.warning(self, "Бонус", "Пользователь относится к другой роли.")
            self._refresh_users()
            return
        self.current_user = user
        self.resize(520, 300)
        self.stack.setCurrentWidget(self.game_page)

    def _show_user_select(self) -> None:
        self._stop_active_game()
        self.resize(520, 330)
        self.stack.setCurrentWidget(self.user_page)
        self._refresh_users(str((self.current_user or {}).get("user_id") or ""))

    def _open_leaderboard(self) -> None:
        from rem_card.ui.shared.minigames.leaderboard_dialog import LeaderboardDialog

        dialog = LeaderboardDialog(self.score_store, self)
        dialog.exec()

    def _open_arcade_leaderboard(self, game_key: str) -> None:
        from rem_card.ui.shared.minigames.arcade_leaderboard_dialog import ArcadeLeaderboardDialog

        dialog = ArcadeLeaderboardDialog(self.score_store, game_key, self)
        dialog.exec()

    def _open_snake(self) -> None:
        if not self.current_user:
            CustomMessageBox.warning(self, "Бонус", "Сначала выберите пользователя.")
            return

        from rem_card.ui.shared.minigames.snake_widget import SnakeWidget

        self._stop_active_game()
        snake_page = QWidget()
        page_layout = QVBoxLayout(snake_page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(8)

        top_row = QHBoxLayout()
        user_label = QLabel(str(self.current_user.get("full_name") or ""))
        user_label.setStyleSheet(f"font-weight: bold; color: {TEXT_PRIMARY};")
        back_btn = QPushButton("К минииграм")
        back_btn.clicked.connect(self._show_games)
        top_row.addWidget(user_label)
        top_row.addStretch()
        top_row.addWidget(back_btn)

        self.snake_widget = SnakeWidget()
        self.snake_widget.game_finished.connect(self._save_snake_result)
        self.snake_page = snake_page

        page_layout.addLayout(top_row)
        page_layout.addWidget(self.snake_widget, 0, Qt.AlignCenter)

        self.stack.addWidget(snake_page)
        self.stack.setCurrentWidget(snake_page)
        self.resize(700, 760)
        self.snake_widget.setFocus(Qt.OtherFocusReason)

    def _open_arcade_game(self, game_key: str) -> None:
        if not self.current_user:
            CustomMessageBox.warning(self, "Бонус", "Сначала выберите пользователя.")
            return

        from rem_card.ui.shared.minigames.arcade_widgets import arcade_game_size, create_arcade_game_widget

        clean_key = str(game_key or "").strip().lower()
        self._stop_active_game()
        arcade_page = QWidget()
        page_layout = QVBoxLayout(arcade_page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(8)

        top_row = QHBoxLayout()
        user_label = QLabel(str(self.current_user.get("full_name") or ""))
        user_label.setStyleSheet(f"font-weight: bold; color: {TEXT_PRIMARY};")
        title_label = QLabel(ARCADE_GAME_LABELS.get(clean_key, clean_key))
        title_label.setStyleSheet(f"font-weight: bold; color: {COLOR_PRIMARY_DARK};")
        back_btn = QPushButton("К минииграм")
        back_btn.clicked.connect(self._show_games)
        top_row.addWidget(user_label)
        top_row.addSpacing(12)
        top_row.addWidget(title_label)
        top_row.addStretch()
        top_row.addWidget(back_btn)

        self.arcade_widget = create_arcade_game_widget(
            clean_key,
            parent=arcade_page,
            on_finished=lambda score, result, duration, extra=None, key=clean_key: self._save_arcade_result(
                key, score, result, duration, extra
            ),
        )
        self.arcade_page = arcade_page

        page_layout.addLayout(top_row)
        page_layout.addWidget(self.arcade_widget, 0, Qt.AlignCenter)

        self.stack.addWidget(arcade_page)
        self.stack.setCurrentWidget(arcade_page)
        width, height = arcade_game_size(clean_key)
        self.resize(width, height)
        start_new_game = getattr(self.arcade_widget, "start_new_game", None)
        if callable(start_new_game):
            start_new_game()
        self.arcade_widget.setFocus(Qt.OtherFocusReason)

    def _show_games(self) -> None:
        self._stop_active_game()
        self.resize(520, 300)
        self.stack.setCurrentWidget(self.game_page)

    def _save_snake_result(self, result: Dict) -> None:
        if not self.current_user:
            return
        try:
            user = self.score_store.save_snake_result(
                str(self.current_user["user_id"]),
                score=int(result.get("score") or 0),
                length=int(result.get("length") or 1),
                duration_sec=int(result.get("duration_sec") or 0),
                won=bool(result.get("won")),
            )
        except Exception as exc:
            if self.snake_widget:
                self.snake_widget.set_save_status("Не удалось сохранить")
            CustomMessageBox.warning(self, "Бонус", f"Не удалось сохранить результат:\n{exc}")
            return
        self.current_user = user
        if self.snake_widget:
            self.snake_widget.set_save_status("Результат сохранён")

    def _save_arcade_result(
        self,
        game_key: str,
        score: int,
        result: str,
        duration: float,
        extra: Optional[Dict] = None,
    ) -> None:
        if not self.current_user:
            return
        try:
            user = self.score_store.save_arcade_result(
                str(self.current_user["user_id"]),
                game_key,
                score=int(score or 0),
                result=str(result or "game_over"),
                duration_sec=float(duration or 0),
                extra=extra if isinstance(extra, dict) else {},
            )
        except Exception as exc:
            CustomMessageBox.warning(self, "Бонус", f"Не удалось сохранить результат:\n{exc}")
            return
        self.current_user = user

    def _stop_active_game(self) -> None:
        self._stop_snake()
        self._stop_arcade()

    def _stop_snake(self) -> None:
        if self.snake_widget is not None:
            try:
                self.snake_widget.stop_game()
            except Exception:
                pass
            self.snake_widget = None
        if self.snake_page is not None:
            try:
                self.stack.removeWidget(self.snake_page)
                self.snake_page.deleteLater()
            except Exception:
                pass
            self.snake_page = None

    def _stop_arcade(self) -> None:
        if self.arcade_widget is not None:
            try:
                stop_game = getattr(self.arcade_widget, "stop_game", None)
                if callable(stop_game):
                    stop_game()
            except Exception:
                pass
            self.arcade_widget = None
        if self.arcade_page is not None:
            try:
                self.stack.removeWidget(self.arcade_page)
                self.arcade_page.deleteLater()
            except Exception:
                pass
            self.arcade_page = None

    def _settings(self) -> QSettings:
        return QSettings("MyHospital", "RemCard")

    def _restore_last_position(self) -> None:
        value = self._settings().value(self.SETTINGS_POS_KEY)
        if not isinstance(value, QPoint):
            return
        screen = QApplication.screenAt(value) or QApplication.primaryScreen()
        if screen is not None and not screen.availableGeometry().contains(value):
            return
        self.move(value)

    def _save_last_position(self) -> None:
        settings = self._settings()
        settings.setValue(self.SETTINGS_POS_KEY, self.pos())
        settings.sync()
