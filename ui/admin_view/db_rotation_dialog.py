from __future__ import annotations

import os
from datetime import datetime
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from rem_card.app.db_cycle_registry import DB_CYCLE_MAX_AGE_DAYS, DbCycleInfo, list_db_cycles
from rem_card.ui.shared.custom_message_box import CustomMessageBox


class DbRotationDialog(QDialog):
    def __init__(self, db_manager, parent=None, on_rotated: Callable[[], None] | None = None):
        super().__init__(parent)
        self.db_manager = db_manager
        self.on_rotated = on_rotated
        self._cycles: list[DbCycleInfo] = []
        self.setWindowTitle("Ручная ротация БД")
        self.setMinimumSize(880, 560)
        self._init_ui()
        self.reload_cycles()

    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        title = QLabel("Ручная ротация БД")
        title.setProperty("heading", "true")
        root.addWidget(title)

        body = QHBoxLayout()
        body.setSpacing(12)

        left = QFrame()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)
        left_layout.addWidget(QLabel("Циклы БД"))
        self.list_widget = QListWidget()
        self.list_widget.currentRowChanged.connect(self._on_selection_changed)
        left_layout.addWidget(self.list_widget, 1)
        body.addWidget(left, 1)

        right = QFrame()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)
        right_layout.addWidget(QLabel("Сводка"))
        self.summary = QTextEdit()
        self.summary.setReadOnly(True)
        right_layout.addWidget(self.summary, 1)
        body.addWidget(right, 2)

        root.addLayout(body, 1)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self.refresh_btn = QPushButton("Обновить")
        self.refresh_btn.clicked.connect(self.reload_cycles)
        buttons.addWidget(self.refresh_btn)

        self.rotate_btn = QPushButton("Выполнить ручную ротацию")
        self.rotate_btn.clicked.connect(self._on_rotate_clicked)
        buttons.addWidget(self.rotate_btn)

        self.close_btn = QPushButton("Закрыть")
        self.close_btn.clicked.connect(self.accept)
        buttons.addWidget(self.close_btn)
        root.addLayout(buttons)

    def reload_cycles(self):
        current_path = self._current_db_path()
        self._cycles = list_db_cycles(
            current_db_path=current_path,
            include_current=True,
            validate=False,
            max_age_days=DB_CYCLE_MAX_AGE_DAYS,
        )
        self.list_widget.clear()
        for info in self._cycles:
            item = QListWidgetItem(self._cycle_label(info))
            item.setData(Qt.UserRole, info)
            self.list_widget.addItem(item)
        if self._cycles:
            self.list_widget.setCurrentRow(0)
        else:
            self.summary.setPlainText("БД не найдены.")
        self._update_rotate_button()

    def _on_selection_changed(self, row: int):
        info = self._info_at(row)
        if info is None:
            self.summary.setPlainText("")
        else:
            self.summary.setPlainText(self._format_summary(info))
        self._update_rotate_button()

    def _on_rotate_clicked(self):
        current = self._current_info()
        if current is None:
            CustomMessageBox.warning(self, "Ротация БД", "Текущая БД не найдена.")
            return
        runtime_mode = str(getattr(getattr(self.db_manager, "runtime_context", None), "mode", "") or "")
        if runtime_mode != "network":
            CustomMessageBox.warning(
                self,
                "Ротация БД",
                "Ротация невозможна в аварийном режиме.",
            )
            return
        if current.active_beds > 0:
            CustomMessageBox.warning(
                self,
                "Ротация БД",
                "Ротация невозможна: есть пациенты на койках.",
            )
            return
        active_role_locks = self._active_role_locks()
        if active_role_locks:
            CustomMessageBox.warning(
                self,
                "Ротация БД",
                self._format_role_lock_message(active_role_locks),
            )
            return
        active_emergency_sessions = self._active_emergency_sessions()
        if active_emergency_sessions:
            CustomMessageBox.warning(
                self,
                "Ротация БД",
                self._format_emergency_session_message(active_emergency_sessions),
            )
            return
        reply = CustomMessageBox.question(
            self,
            "Подтверждение",
            "Ротация создаст новую рабочую БД, а текущая будет перенесена в архив.\nПродолжить?",
        )
        if reply != CustomMessageBox.Yes:
            return

        self._set_busy(True)
        try:
            result = self.db_manager.rotate_database_manually()
        except Exception as exc:
            CustomMessageBox.warning(self, "Ошибка", f"Не удалось выполнить ротацию БД:\n{exc}")
            return
        finally:
            self._set_busy(False)

        status = result.get("status")
        if status == "rotated":
            CustomMessageBox.information(self, "Ротация БД", "Ротация БД выполнена.")
            if self.on_rotated:
                self.on_rotated()
            self.reload_cycles()
            return

        message = self._rotation_status_message(result)
        CustomMessageBox.warning(self, "Ротация БД", message)
        self.reload_cycles()

    def _set_busy(self, busy: bool):
        self.refresh_btn.setEnabled(not busy)
        self.close_btn.setEnabled(not busy)
        self.list_widget.setEnabled(not busy)
        if busy:
            self.rotate_btn.setEnabled(False)
        else:
            self._update_rotate_button()

    def _update_rotate_button(self):
        current = self._current_info()
        selected = self._selected_info()
        runtime_mode = str(getattr(getattr(self.db_manager, "runtime_context", None), "mode", "") or "")
        active_role_locks = self._active_role_locks() if current and selected and selected.is_current else []
        active_emergency_sessions = self._active_emergency_sessions() if current and selected and selected.is_current else []
        enabled = bool(
            current
            and selected
            and selected.is_current
            and runtime_mode == "network"
            and current.active_beds == 0
            and not active_role_locks
            and not active_emergency_sessions
        )
        self.rotate_btn.setEnabled(enabled)

    def _selected_info(self) -> DbCycleInfo | None:
        return self._info_at(self.list_widget.currentRow())

    def _current_info(self) -> DbCycleInfo | None:
        for info in self._cycles:
            if info.is_current:
                return info
        return None

    def _info_at(self, row: int) -> DbCycleInfo | None:
        if row < 0 or row >= len(self._cycles):
            return None
        return self._cycles[row]

    def _current_db_path(self) -> str:
        return os.path.abspath(str(getattr(self.db_manager, "db_path", "") or ""))

    def _cycle_label(self, info: DbCycleInfo) -> str:
        if info.is_current:
            return "Текущая БД"
        start = _fmt_date(info.min_admission_datetime) or _fmt_date(info.created_at)
        end = _fmt_date(info.max_admission_datetime)
        if start and end and start != end:
            return f"Архив: {start} - {end}"
        if start:
            return f"Архив: {start}"
        return f"Архив: {os.path.basename(info.path)}"

    def _format_summary(self, info: DbCycleInfo) -> str:
        lines = [
            f"Статус: {'текущая рабочая БД' if info.is_current else 'архивный цикл'}",
            f"Файл: {info.path}",
            f"Размер: {_fmt_size(info.size_bytes)}",
            f"Создана: {_fmt_dt(info.created_at)}",
            f"Изменена: {_fmt_dt(info.modified_at)}",
            f"Начало цикла: {_fmt_dt(info.cycle_started_at)}",
            f"Период данных: {_fmt_dt(info.min_admission_datetime)} - {_fmt_dt(info.max_admission_datetime)}",
            f"Пациентов: {info.patient_count}",
            f"Госпитализаций: {info.admission_count}",
            f"Переведено: {info.transferred_count}",
            f"Умерло: {info.death_count}",
            f"Пациентов на койках: {info.active_beds}",
            f"Проверка доступности: {'доступна' if info.quick_check_ok else 'ошибка'}",
        ]
        if not info.quick_check_ok:
            lines.append(f"Ошибка: {info.validation_message}")
        if info.is_current:
            lines.extend(
                [
                    "",
                    f"Возраст текущего цикла: {_fmt_days(info.age_days)}",
                    f"Осталось до автоматической ротации: {_fmt_days(info.days_until_rotation)}",
                    f"Лимит автоматической ротации: {DB_CYCLE_MAX_AGE_DAYS} дней",
                ]
            )
            runtime_mode = str(getattr(getattr(self.db_manager, "runtime_context", None), "mode", "") or "")
            if runtime_mode != "network":
                lines.append("Ротация: запрещена в аварийном режиме.")
            elif info.active_beds > 0:
                lines.append("Ротация: невозможна, есть пациенты на койках.")
            elif self._active_role_locks():
                lines.append("Ротация: невозможна, роль медсестры запущена на рабочем месте.")
            elif self._active_emergency_sessions():
                lines.append("Ротация: невозможна, есть активная аварийная сессия медсестры.")
            else:
                lines.append("Ротация: можно выполнить вручную.")
        return "\n".join(lines)

    def _active_role_locks(self) -> list[dict[str, str]]:
        checker = getattr(self.db_manager, "active_rotation_role_locks", None)
        if not callable(checker):
            return []
        try:
            return list(checker() or [])
        except Exception:
            return [
                {
                    "role": "nurse",
                    "holder": "не удалось проверить lock роли медсестры",
                }
            ]

    def _active_emergency_sessions(self) -> list[dict[str, str]]:
        checker = getattr(self.db_manager, "active_rotation_emergency_sessions", None)
        if not callable(checker):
            return []
        try:
            return list(checker() or [])
        except Exception:
            return [
                {
                    "role": "nurse",
                    "status": "unknown",
                    "holder": "не удалось проверить аварийные сессии медсестры",
                }
            ]

    @staticmethod
    def _format_role_lock_message(active_role_locks: list[dict[str, str]]) -> str:
        details = []
        for item in active_role_locks:
            role = str(item.get("role") or "")
            if role == "nurse":
                role_text = "медсестра"
            elif role == "nurse_emergency":
                role_text = "аварийная медсестра"
            else:
                role_text = role or "роль"
            holder = str(item.get("holder") or "").strip()
            details.append(f"{role_text}: {holder}" if holder else role_text)
        suffix = "\n\n" + "\n".join(details) if details else ""
        return "Ротация невозможна: роль медсестры запущена на рабочем месте." + suffix

    @staticmethod
    def _format_emergency_session_message(active_sessions: list[dict[str, str]]) -> str:
        details = []
        for item in active_sessions:
            session_id = str(item.get("session_id") or "").strip()
            status = str(item.get("status") or "").strip()
            holder = str(item.get("holder") or "").strip()
            parts = []
            if session_id:
                parts.append(f"сессия {session_id}")
            if status:
                parts.append(f"статус {status}")
            if holder:
                parts.append(holder)
            details.append(", ".join(parts) if parts else "аварийная сессия")
        suffix = "\n\n" + "\n".join(details) if details else ""
        return "Ротация невозможна: есть активная аварийная сессия медсестры." + suffix

    @staticmethod
    def _rotation_status_message(result: dict) -> str:
        status = result.get("status")
        if status == "deferred_active_beds":
            return "Ротация невозможна: есть пациенты на койках."
        if status == "deferred_active_role_lock":
            return DbRotationDialog._format_role_lock_message(list(result.get("blocked_roles") or []))
        if status == "deferred_active_emergency_session":
            return DbRotationDialog._format_emergency_session_message(list(result.get("blocked_emergency_sessions") or []))
        if status == "rotation_forbidden_runtime":
            return "Ротация невозможна в аварийном режиме."
        if status == "pre_rotation_backup_failed":
            return f"Ротация отменена: не удалось создать валидный backup.\n{result.get('error', '')}"
        if status == "source_quick_check_failed":
            return f"Ротация отменена: текущая БД не прошла проверку.\n{result.get('error', '')}"
        if status == "source_changed_after_backup":
            return "Ротация отменена: БД изменилась после создания backup. Повторите операцию."
        if status == "db_lock_busy":
            return "Ротация невозможна: БД сейчас занята другой операцией."
        if status == "rotation_lock_busy":
            return "Ротация уже выполняется на другом рабочем месте."
        if status == "new_db_failed":
            if result.get("current_preserved"):
                return (
                    "Ротация отменена: не удалось подготовить новую рабочую БД.\n"
                    "Текущая БД оставлена на месте."
                )
            return (
                "Старая БД перенесена в архив, но новая рабочая БД не создана.\n"
                "Не закрывайте приложение и проверьте журнал ошибок."
            )
        return f"Ротация не выполнена: {status or 'неизвестная ошибка'}"


def _fmt_dt(value: datetime | None) -> str:
    return value.strftime("%d.%m.%Y %H:%M") if value else "нет данных"


def _fmt_date(value: datetime | None) -> str:
    return value.strftime("%d.%m.%Y") if value else ""


def _fmt_size(size: int) -> str:
    value = float(size or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024.0 or unit == "ГБ":
            return f"{value:.1f} {unit}" if unit != "Б" else f"{int(value)} {unit}"
        value /= 1024.0
    return f"{value:.1f} ГБ"


def _fmt_days(value: float | None) -> str:
    if value is None:
        return "нет данных"
    return f"{value:.1f} дней"
