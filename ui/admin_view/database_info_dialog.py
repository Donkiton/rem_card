from __future__ import annotations

import os
from datetime import datetime
from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from rem_card.services.database_info_service import (
    BackupInfo,
    DatabaseInfoCollectionCancelled,
    DatabaseInfoService,
    DatabaseInfoSnapshot,
)
from rem_card.ui.shared.async_call import AsyncCallThread
from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.shared.window_state import SavedFramelessDialogMixin
from rem_card.ui.styles.database_info_styles import apply_database_info_dialog_style


class SortableTableItem(QTableWidgetItem):
    def __lt__(self, other: QTableWidgetItem) -> bool:
        left = self.data(Qt.UserRole)
        right = other.data(Qt.UserRole)
        if left is not None and right is not None and type(left) is type(right):
            return left < right
        return super().__lt__(other)


class DatabaseInfoDialog(SavedFramelessDialogMixin, BaseStyledDialog):
    def __init__(
        self,
        db_manager=None,
        parent=None,
        *,
        snapshot_loader: Callable[[], DatabaseInfoSnapshot] | None = None,
        auto_load: bool = True,
    ) -> None:
        super().__init__("Информация о базах данных", parent)
        self._init_saved_frameless_dialog(
            "admin/database_info_dialog_geometry",
            drag_area_height=32,
        )
        self.db_manager = db_manager
        self._snapshot: DatabaseInfoSnapshot | None = None
        self._worker: AsyncCallThread | None = None
        if snapshot_loader is None:
            runtime_context = getattr(db_manager, "runtime_context", None)
            current_db_path = str(getattr(db_manager, "db_path", "") or "")
            self._service = DatabaseInfoService(
                runtime_context,
                current_db_path=current_db_path,
            )
            snapshot_loader = self._service.collect
        else:
            self._service = None
        self._snapshot_loader = snapshot_loader

        self.resize(1120, 720)
        self.setMinimumSize(920, 600)
        self.setSizeGripEnabled(True)
        self._init_ui()
        apply_database_info_dialog_style(self)
        self._restore_saved_geometry()
        if auto_load:
            QTimer.singleShot(0, self.reload_info)

    def _init_ui(self) -> None:
        root = self.content_layout
        root.setContentsMargins(18, 14, 18, 18)
        root.setSpacing(12)

        header = QFrame()
        header.setObjectName("DatabaseInfoHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(14, 11, 14, 11)
        header_layout.setSpacing(4)
        title = QLabel("Состояние и история хранилища")
        title.setObjectName("DatabaseInfoTitle")
        header_layout.addWidget(title)
        hint = QLabel(
            "Просмотр рабочих баз, архивных циклов ротации и резервных копий. "
            "Окно ничего не изменяет и не запускает восстановление."
        )
        hint.setObjectName("DatabaseInfoHint")
        hint.setWordWrap(True)
        header_layout.addWidget(hint)
        root.addWidget(header)

        cards = QHBoxLayout()
        cards.setSpacing(10)
        self.database_count_value = self._add_summary_card(cards, "Рабочие БД")
        self.cycle_count_value = self._add_summary_card(cards, "Циклы")
        self.backup_count_value = self._add_summary_card(cards, "Бэкапы")
        self.backup_size_value = self._add_summary_card(cards, "Общий объём")
        self.latest_backup_value = self._add_summary_card(cards, "Последний бэкап")
        root.addLayout(cards)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("DatabaseInfoTabs")
        self.database_table = self._create_table(
            ["База", "Назначение", "Создана", "Изменена", "Размер", "Состояние", "Схема"]
        )
        self.cycle_table = self._create_table(
            ["Цикл", "Начало", "Период данных", "Размер", "Госпитализации", "Состояние"]
        )
        self.backup_table = self._create_table(
            ["Хранилище", "Тип", "Создан", "Размер", "Состояние", "Источник", "Файл"]
        )
        self.history_table = self._create_table(
            ["Дата", "Событие", "Описание", "Размер", "Состояние"]
        )
        self.tabs.addTab(self._table_page(self.database_table), "Рабочие БД")
        self.tabs.addTab(self._table_page(self.cycle_table), "Циклы ротации")
        self.tabs.addTab(self._backup_page(), "Бэкапы")
        self.tabs.addTab(self._table_page(self.history_table), "Хронология")
        root.addWidget(self.tabs, 1)

        self.path_label = QLabel("Выберите строку, чтобы увидеть полный путь к файлу.")
        self.path_label.setObjectName("DatabaseInfoPath")
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.path_label)

        footer = QHBoxLayout()
        self.status_label = QLabel("Ожидание загрузки…")
        self.status_label.setObjectName("DatabaseInfoStatus")
        footer.addWidget(self.status_label, 1)
        self.refresh_button = QPushButton("Обновить")
        self.refresh_button.setObjectName("DialogOkBtn")
        self.refresh_button.clicked.connect(self.reload_info)
        footer.addWidget(self.refresh_button)
        close_button = QPushButton("Закрыть")
        close_button.setObjectName("DialogOkBtn")
        close_button.clicked.connect(self.accept)
        footer.addWidget(close_button)
        root.addLayout(footer)

    @staticmethod
    def _add_summary_card(layout: QHBoxLayout, caption: str) -> QLabel:
        card = QFrame()
        card.setObjectName("DatabaseInfoCard")
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 8, 12, 8)
        card_layout.setSpacing(2)
        caption_label = QLabel(caption)
        caption_label.setObjectName("DatabaseInfoCardCaption")
        value_label = QLabel("—")
        value_label.setObjectName("DatabaseInfoCardValue")
        value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        card_layout.addWidget(caption_label)
        card_layout.addWidget(value_label)
        layout.addWidget(card)
        return value_label

    def _create_table(self, headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setObjectName("DatabaseInfoTable")
        table.setHorizontalHeaderLabels(headers)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSortingEnabled(True)
        table.verticalHeader().hide()
        table.horizontalHeader().setStretchLastSection(True)
        for column in range(len(headers)):
            table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(len(headers) - 1, QHeaderView.Stretch)
        table.currentCellChanged.connect(self._on_table_selection_changed)
        return table

    @staticmethod
    def _table_page(table: QTableWidget) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(table)
        return page

    def _backup_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Показать:"))
        self.backup_filter = QComboBox()
        self.backup_filter.setObjectName("DatabaseInfoFilter")
        self.backup_filter.addItem("Все бэкапы", "all")
        self.backup_filter.addItem("Основная БД", "Основная БД")
        self.backup_filter.addItem("Настройки", "Настройки")
        self.backup_filter.addItem("Только невалидные", "invalid")
        self.backup_filter.addItem("Без метаданных", "no_metadata")
        self.backup_filter.currentIndexChanged.connect(self._fill_backup_table)
        toolbar.addWidget(self.backup_filter)
        self.backup_filter_count = QLabel()
        self.backup_filter_count.setObjectName("DatabaseInfoStatus")
        toolbar.addWidget(self.backup_filter_count)
        toolbar.addStretch()
        layout.addLayout(toolbar)
        layout.addWidget(self.backup_table)
        return page

    def reload_info(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        self.refresh_button.setEnabled(False)
        self.status_label.setText("Собираем сведения о базах и бэкапах…")
        worker_ref = {}

        def load_snapshot():
            worker = worker_ref["worker"]
            if self._service is not None:
                return self._service.collect(cancel_check=worker.is_cancel_requested)
            return self._snapshot_loader()

        worker = AsyncCallThread(load_snapshot, parent=self)
        worker_ref["worker"] = worker
        self._worker = worker
        worker.succeeded.connect(self._apply_snapshot, Qt.QueuedConnection)
        worker.failed.connect(self._on_load_failed, Qt.QueuedConnection)
        worker.finished.connect(
            lambda current=worker: self._on_load_finished(current),
            Qt.QueuedConnection,
        )
        worker.start()

    def _apply_snapshot(self, snapshot: DatabaseInfoSnapshot) -> None:
        self._snapshot = snapshot
        available = sum(1 for item in snapshot.databases if item.status == "Доступна")
        self.database_count_value.setText(f"{available} из {len(snapshot.databases)}")
        self.cycle_count_value.setText(str(len(snapshot.cycles)))
        self.backup_count_value.setText(str(len(snapshot.backups)))
        self.backup_size_value.setText(_format_size(snapshot.total_backup_bytes))
        self.latest_backup_value.setText(_format_datetime(snapshot.latest_backup_at, compact=True))
        self._fill_database_table()
        self._fill_cycle_table()
        self._fill_backup_table()
        self._fill_history_table()
        warning_suffix = f" · предупреждений: {len(snapshot.warnings)}" if snapshot.warnings else ""
        self.status_label.setText(
            f"Обновлено: {_format_datetime(snapshot.collected_at)}{warning_suffix}"
        )
        if snapshot.warnings:
            self.status_label.setToolTip("\n".join(snapshot.warnings))
        else:
            self.status_label.setToolTip("")

    def _on_load_failed(self, error: object) -> None:
        if isinstance(error, DatabaseInfoCollectionCancelled):
            self.status_label.setText("Сбор сведений отменён.")
            return
        self.status_label.setText(f"Не удалось собрать сведения: {error}")
        self.status_label.setToolTip(str(error))

    def _on_load_finished(self, worker: AsyncCallThread) -> None:
        if self._worker is worker:
            self._worker = None
            self.refresh_button.setEnabled(True)

    def _fill_database_table(self) -> None:
        if self._snapshot is None:
            return
        table = self.database_table
        table.setSortingEnabled(False)
        table.setRowCount(0)
        for info in self._snapshot.databases:
            values = [
                (info.title, info.title),
                (info.kind, info.kind),
                (_format_datetime(info.created_at), _timestamp(info.created_at)),
                (_format_datetime(info.modified_at), _timestamp(info.modified_at)),
                (_format_size(info.size_bytes), info.size_bytes),
                (info.status, info.status),
                (info.schema_version or "—", info.schema_version or ""),
            ]
            self._append_row(table, values, info.path, info.detail)
        table.setSortingEnabled(True)

    def _fill_cycle_table(self) -> None:
        if self._snapshot is None:
            return
        table = self.cycle_table
        table.setSortingEnabled(False)
        table.setRowCount(0)
        for info in self._snapshot.cycles:
            title = "Текущая БД" if info.is_current else os.path.basename(info.path)
            period = _format_period(info.min_admission_datetime, info.max_admission_datetime)
            status = "Доступен" if info.quick_check_ok else "Ошибка чтения"
            started_at = info.cycle_started_at or info.created_at
            values = [
                (title, title),
                (_format_datetime(started_at), _timestamp(started_at)),
                (period, period),
                (_format_size(info.size_bytes), info.size_bytes),
                (str(info.admission_count), info.admission_count),
                (status, status),
            ]
            detail = (
                f"Пациентов: {info.patient_count}; на койках: {info.active_beds}; "
                f"изменена: {_format_datetime(info.modified_at)}"
            )
            self._append_row(table, values, info.path, detail)
        table.setSortingEnabled(True)

    def _fill_backup_table(self) -> None:
        if self._snapshot is None:
            return
        selected_filter = self.backup_filter.currentData() or "all"
        backups = [item for item in self._snapshot.backups if _backup_matches(item, selected_filter)]
        self.backup_filter_count.setText(f"Найдено: {len(backups)}")
        table = self.backup_table
        table.setSortingEnabled(False)
        table.setRowCount(0)
        for info in backups:
            values = [
                (info.scope, info.scope),
                (info.kind, info.kind),
                (_format_datetime(info.created_at), _timestamp(info.created_at)),
                (_format_size(info.size_bytes), info.size_bytes),
                (info.validation_status, info.validation_status),
                (info.source or "—", info.source or ""),
                (os.path.basename(info.path), os.path.basename(info.path)),
            ]
            detail_parts = []
            if info.sha256:
                detail_parts.append(f"SHA-256: {info.sha256}")
            if info.metadata_error:
                detail_parts.append(f"Ошибка метаданных: {info.metadata_error}")
            self._append_row(table, values, info.path, "; ".join(detail_parts))
        table.setSortingEnabled(True)

    def _fill_history_table(self) -> None:
        if self._snapshot is None:
            return
        table = self.history_table
        table.setSortingEnabled(False)
        table.setRowCount(0)
        for event in self._snapshot.events:
            values = [
                (_format_datetime(event.occurred_at), _timestamp(event.occurred_at)),
                (event.title, event.title),
                (event.description or event.event_type, event.description or event.event_type),
                (_format_size(event.size_bytes), event.size_bytes),
                (event.status, event.status),
            ]
            self._append_row(table, values, event.path, event.event_type)
        table.setSortingEnabled(True)

    @staticmethod
    def _append_row(
        table: QTableWidget,
        values: list[tuple[str, object]],
        path: str,
        detail: str = "",
    ) -> None:
        row = table.rowCount()
        table.insertRow(row)
        tooltip = path + (f"\n\n{detail}" if detail else "")
        for column, (text, sort_value) in enumerate(values):
            item = SortableTableItem(str(text))
            item.setData(Qt.UserRole, sort_value)
            item.setData(Qt.UserRole + 1, path)
            item.setData(Qt.UserRole + 2, detail)
            item.setToolTip(tooltip)
            table.setItem(row, column, item)

    def _on_table_selection_changed(self, *_args) -> None:
        table = self.sender()
        if not isinstance(table, QTableWidget):
            return
        item = table.currentItem()
        if item is None:
            return
        path = str(item.data(Qt.UserRole + 1) or "")
        detail = str(item.data(Qt.UserRole + 2) or "")
        self.path_label.setText(path + (f"\n{detail}" if detail else ""))

    def _cancel_collection(self) -> None:
        worker = self._worker
        if worker is not None and worker.isRunning():
            worker.quit()

    def done(self, result: int) -> None:
        self._cancel_collection()
        super().done(result)

    def closeEvent(self, event) -> None:
        self._cancel_collection()
        super().closeEvent(event)


def _backup_matches(info: BackupInfo, selected_filter: str) -> bool:
    if selected_filter == "all":
        return True
    if selected_filter == "invalid":
        return info.validation_status in {
            "Невалиден",
            "Ошибка метаданных",
            "Изменён после проверки",
        }
    if selected_filter == "no_metadata":
        return info.validation_status in {"Без метаданных", "Ошибка метаданных"}
    return info.scope == selected_filter


def _format_datetime(value: datetime | None, *, compact: bool = False) -> str:
    if value is None:
        return "—"
    return value.strftime("%d.%m.%Y" if compact else "%d.%m.%Y %H:%M")


def _format_period(start: datetime | None, end: datetime | None) -> str:
    if start is None and end is None:
        return "—"
    if start is None:
        return f"до {_format_datetime(end, compact=True)}"
    if end is None:
        return f"с {_format_datetime(start, compact=True)}"
    return f"{_format_datetime(start, compact=True)} — {_format_datetime(end, compact=True)}"


def _format_size(size_bytes: int) -> str:
    value = float(max(0, size_bytes or 0))
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024.0 or unit == "ТБ":
            return f"{int(value)} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} ТБ"


def _timestamp(value: datetime | None) -> float:
    return value.timestamp() if value is not None else 0.0
