from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
)

from rem_card.app.runtime_paths import (
    DEV_BAZA_DIR_ENV,
    DEV_RUNTIME_BAZA_PIN_ENV,
    add_saved_dev_baza_dir,
    read_dev_database_config,
    remove_saved_dev_baza_dir,
    resolve_baza_dir,
    save_dev_baza_dir,
    validate_dev_baza_dir,
)
from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.shared.async_call import AsyncCallThread
from rem_card.ui.shared.custom_message_box import CustomMessageBox

from .settings_import_dialog import SettingsImportFolderDialog


class DevDatabaseSwitchDialog(BaseStyledDialog):
    """Developer-only selector for the database used on the next process start."""

    def __init__(self, parent=None):
        super().__init__("Смена базы", parent)
        self.selected_path = ""
        self.active_changed = False
        self._validation_worker = None
        self._validation_cancelled = False
        initial_config = read_dev_database_config()
        self._initial_config_load_error = str(initial_config.get("load_error") or "")
        runtime_path_is_pinned = os.environ.get(DEV_RUNTIME_BAZA_PIN_ENV) == str(os.getpid())
        self.environment_override = (
            ""
            if runtime_path_is_pinned
            else str(
                os.environ.get("REMCARD_BAZA_DIR")
                or os.environ.get(DEV_BAZA_DIR_ENV)
                or ""
            ).strip()
        )
        if runtime_path_is_pinned and os.environ.get(DEV_BAZA_DIR_ENV):
            self.environment_override = str(os.environ[DEV_BAZA_DIR_ENV]).strip()
        self.current_path = self._normalize(resolve_baza_dir())
        self.resize(760, 470)
        self._setup_ui()
        self._reload_saved_paths(select_path=self.current_path)

    @staticmethod
    def _normalize(path: str) -> str:
        return os.path.abspath(os.path.normpath(str(path or "").strip().strip('"')))

    @staticmethod
    def _same_path(left: str, right: str) -> bool:
        if not left or not right:
            return False
        return os.path.normcase(DevDatabaseSwitchDialog._normalize(left)) == os.path.normcase(
            DevDatabaseSwitchDialog._normalize(right)
        )

    def _setup_ui(self) -> None:
        root = self.content_layout
        root.setSpacing(12)

        explanation = QLabel(
            "Выберите папку базы RemCard. «Добавить в список» только запоминает путь. "
            "Для переключения нажмите «Сделать активной» — выбор применится после "
            "полного перезапуска dev-версии."
        )
        explanation.setWordWrap(True)
        root.addWidget(explanation)

        current_title = QLabel("Текущая база:")
        current_title.setProperty("heading", "true")
        root.addWidget(current_title)

        self.current_path_label = QLabel(self.current_path)
        self.current_path_label.setObjectName("DevDatabaseCurrentPath")
        self.current_path_label.setWordWrap(True)
        self.current_path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.current_path_label)

        if self.environment_override:
            override_label = QLabel(
                "Внимание: путь сейчас задан переменной окружения. Она имеет приоритет над "
                "сохранённым выбором, пока не будет удалена из конфигурации запуска."
            )
            override_label.setObjectName("DevDatabaseOverrideWarning")
            override_label.setWordWrap(True)
            root.addWidget(override_label)

        path_label = QLabel("Путь к новой базе:")
        root.addWidget(path_label)

        path_row = QHBoxLayout()
        self.path_edit = QLineEdit(self.current_path)
        self.path_edit.setObjectName("DevDatabasePathEdit")
        self.path_edit.setPlaceholderText(r"Например: \\server\share\Baza_rao3_jurnal")
        self.browse_button = QPushButton("Выбрать папку")
        self.browse_button.setObjectName("DialogOkBtn")
        self.browse_button.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(self.browse_button)
        root.addLayout(path_row)

        saved_header = QHBoxLayout()
        saved_header.addWidget(QLabel("Сохранённые пути:"))
        saved_header.addStretch()
        self.save_path_button = QPushButton("Добавить в список")
        self.save_path_button.setObjectName("DialogOkBtn")
        self.save_path_button.clicked.connect(self._save_path)
        self.remove_path_button = QPushButton("Удалить из списка")
        self.remove_path_button.setObjectName("DialogOkBtn")
        self.remove_path_button.clicked.connect(self._remove_selected_path)
        saved_header.addWidget(self.save_path_button)
        saved_header.addWidget(self.remove_path_button)
        root.addLayout(saved_header)

        self.saved_paths_list = QListWidget()
        self.saved_paths_list.setObjectName("DevDatabaseSavedPaths")
        self.saved_paths_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.saved_paths_list.itemSelectionChanged.connect(self._use_selected_path)
        self.saved_paths_list.itemDoubleClicked.connect(lambda _item: self._apply())
        root.addWidget(self.saved_paths_list, 1)

        self.status_label = QLabel()
        self.status_label.setObjectName("DevDatabaseStatus")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        footer = QHBoxLayout()
        footer.addStretch()
        self.cancel_button = QPushButton("Отмена")
        self.cancel_button.setObjectName("DialogOkBtn")
        self.cancel_button.clicked.connect(self.reject)
        self.apply_button = QPushButton("Сделать активной")
        self.apply_button.setObjectName("DialogOkBtn")
        self.apply_button.clicked.connect(self._apply)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.apply_button)
        root.addLayout(footer)

        self.setStyleSheet(
            self.styleSheet()
            + """
            QLabel#DevDatabaseCurrentPath {
                background: #eef4f8;
                border: 1px solid #c7d1da;
                border-radius: 6px;
                color: #2c3e50;
                padding: 8px 10px;
            }
            QLabel#DevDatabaseOverrideWarning {
                background: #fff4d6;
                border: 1px solid #e0bd64;
                border-radius: 6px;
                color: #6b4f00;
                padding: 8px 10px;
            }
            QLineEdit#DevDatabasePathEdit, QListWidget#DevDatabaseSavedPaths {
                background: #ffffff;
                border: 1px solid #c7d1da;
                border-radius: 6px;
                color: #2c3e50;
                padding: 8px 10px;
            }
            QLineEdit#DevDatabasePathEdit:focus, QListWidget#DevDatabaseSavedPaths:focus {
                border-color: #7f9fbd;
            }
            QListWidget#DevDatabaseSavedPaths::item {
                min-height: 28px;
                padding: 4px 6px;
            }
            QListWidget#DevDatabaseSavedPaths::item:selected {
                background: #dceaf7;
                color: #1f2d3d;
            }
            """
        )

    def _candidate_path(self) -> str:
        raw_path = self.path_edit.text().strip().strip('"')
        return self._normalize(raw_path) if raw_path else ""

    def _start_candidate_validation(self, action: str) -> None:
        active_worker = self._validation_worker
        if active_worker is not None and active_worker.isRunning():
            return

        candidate = self._candidate_path()
        if not candidate:
            CustomMessageBox.warning(self, "Смена базы", "Укажите папку базы данных.")
            return

        self._validation_cancelled = False
        self.status_label.setText("Проверяем доступность и структуру базы…")
        self._set_validation_running(True)
        worker = AsyncCallThread(validate_dev_baza_dir, candidate)
        worker._dev_database_candidate = candidate
        worker._dev_database_action = action
        self._validation_worker = worker
        worker.succeeded.connect(self._on_validation_succeeded, Qt.QueuedConnection)
        worker.failed.connect(self._on_validation_failed, Qt.QueuedConnection)
        worker.finished.connect(self._on_validation_finished, Qt.QueuedConnection)
        try:
            worker.start()
        except Exception as exc:
            self._validation_worker = None
            self._set_validation_running(False)
            CustomMessageBox.warning(self, "Смена базы", f"Не удалось проверить базу:\n{exc}")

    def _set_validation_running(self, running: bool) -> None:
        enabled = not running
        for widget in (
            self.path_edit,
            self.browse_button,
            self.saved_paths_list,
            self.save_path_button,
            self.remove_path_button,
            self.apply_button,
        ):
            widget.setEnabled(enabled)

    def _on_validation_succeeded(self, result) -> None:
        worker = self.sender()
        if worker is None or worker is not self._validation_worker or self._validation_cancelled:
            return
        try:
            ok, message = result
        except Exception:
            ok, message = False, "Проверка базы вернула некорректный результат."
        if not ok:
            self.status_label.setText("База не прошла проверку.")
            CustomMessageBox.warning(self, "Смена базы", str(message))
            return

        candidate = str(getattr(worker, "_dev_database_candidate", "") or "")
        action = str(getattr(worker, "_dev_database_action", "") or "")
        if action == "save":
            self._save_validated_path(candidate)
        elif action == "activate":
            self._activate_validated_path(candidate)

    def _on_validation_failed(self, exc: object) -> None:
        if self.sender() is not self._validation_worker or self._validation_cancelled:
            return
        self.status_label.setText("Не удалось проверить базу.")
        CustomMessageBox.warning(self, "Смена базы", f"Не удалось проверить базу:\n{exc}")

    def _on_validation_finished(self) -> None:
        worker = self.sender()
        if worker is not self._validation_worker:
            return
        self._validation_worker = None
        if not self._validation_cancelled:
            self._set_validation_running(False)

    def _browse(self) -> None:
        dialog = SettingsImportFolderDialog(
            self._candidate_path() or self.current_path,
            parent=self,
        )
        if dialog.exec() == QDialog.Accepted and dialog.selected_path:
            self.path_edit.setText(dialog.selected_path)

    def _reload_saved_paths(self, *, select_path: str = "") -> None:
        config = read_dev_database_config()
        paths = list(config.get("saved_baza_dirs") or [])
        if not any(self._same_path(path, self.current_path) for path in paths):
            paths.insert(0, self.current_path)

        self.saved_paths_list.blockSignals(True)
        self.saved_paths_list.clear()
        selected_row = -1
        for path in paths:
            normalized = self._normalize(str(path))
            self.saved_paths_list.addItem(normalized)
            if select_path and self._same_path(normalized, select_path):
                selected_row = self.saved_paths_list.count() - 1
        if selected_row >= 0:
            self.saved_paths_list.setCurrentRow(selected_row)
        self.saved_paths_list.blockSignals(False)

        load_error = str(config.get("load_error") or self._initial_config_load_error or "")
        if load_error:
            self.status_label.setText(
                "Повреждённый файл списка путей отложен в резервную копию. "
                "Можно сохранить список заново."
            )

    def _use_selected_path(self) -> None:
        item = self.saved_paths_list.currentItem()
        if item is not None:
            self.path_edit.setText(item.text())

    def _save_path(self) -> None:
        self._start_candidate_validation("save")

    def _save_validated_path(self, candidate: str) -> None:
        try:
            add_saved_dev_baza_dir(candidate)
        except Exception as exc:
            CustomMessageBox.warning(self, "Смена базы", f"Не удалось сохранить путь:\n{exc}")
            return
        self._reload_saved_paths(select_path=candidate)
        self.path_edit.setText(candidate)
        self.status_label.setText("Путь добавлен в список. Чтобы подключить эту базу, сделайте её активной.")

    def _remove_selected_path(self) -> None:
        item = self.saved_paths_list.currentItem()
        if item is None:
            CustomMessageBox.warning(self, "Смена базы", "Выберите путь для удаления.")
            return
        selected = item.text()
        config = read_dev_database_config()
        configured_active = str(config.get("active_baza_dir") or "")
        if self._same_path(selected, self.current_path) or (
            configured_active and self._same_path(selected, configured_active)
        ):
            CustomMessageBox.warning(
                self,
                "Смена базы",
                "Активный путь нельзя удалить. Сначала сделайте активной другую базу.",
            )
            return
        try:
            remove_saved_dev_baza_dir(selected)
        except Exception as exc:
            CustomMessageBox.warning(self, "Смена базы", f"Не удалось удалить путь:\n{exc}")
            return
        self._reload_saved_paths()
        self.status_label.setText("Путь удалён из списка.")

    def _apply(self) -> None:
        self._start_candidate_validation("activate")

    def _activate_validated_path(self, candidate: str) -> None:
        try:
            save_dev_baza_dir(candidate)
        except Exception as exc:
            CustomMessageBox.warning(self, "Смена базы", f"Не удалось сохранить выбор:\n{exc}")
            return

        self.selected_path = candidate
        self.active_changed = not self._same_path(candidate, self.current_path)
        self.accept()

    def reject(self) -> None:
        self._validation_cancelled = True
        worker = self._validation_worker
        if worker is not None:
            worker.quit()
        super().reject()
