"""Reusable manual maintenance surface; role authorization belongs to its caller."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem,
)

from rem_card.services.storage_maintenance import Inspection, StorageMaintenanceService
from rem_card.ui.shared.async_call import AsyncCallThread
from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.styles.database_info_styles import apply_database_info_dialog_style


class StorageMaintenanceDialog(BaseStyledDialog):
    def __init__(self, root, parent=None):
        super().__init__("Обслуживание служебных файлов", parent)
        self.service = StorageMaintenanceService(root)
        self.inspection = None
        self.worker = None
        self.resize(1000, 640)
        self.setMinimumSize(760, 460)
        self.setSizeGripEnabled(True)
        self.path_label = QLabel(f"Папка базы: {self.service.root}")
        self.path_label.setTextFormat(Qt.PlainText)
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.content_layout.addWidget(self.path_label)
        policy = QLabel(
            "Логи — 30 дней · Метрики — 14 · Аудит и история проверок — 90\n"
            "Обработанная диагностика и карантин блокировок — 30 · Временные файлы — 7\n"
            "Медицинские данные, бэкапы и необработанные отчёты сохраняются."
        )
        policy.setWordWrap(True)
        self.content_layout.addWidget(policy)
        self.status = QLabel("Нажмите «Инспектировать», чтобы получить список старых служебных файлов.")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.content_layout.addWidget(self.status)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Файл", "Категория", "Хранение, дней", "Размер, КБ"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, 4):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.content_layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.inspect_button = QPushButton("Инспектировать")
        self.clean_button = QPushButton("Очистить перечисленные файлы")
        self.clean_button.setEnabled(False)
        self.close_button = QPushButton("Закрыть")
        for button in (self.inspect_button, self.clean_button, self.close_button):
            button.setMinimumHeight(34)
            row.addWidget(button)
        self.content_layout.addLayout(row)
        self.inspect_button.clicked.connect(self.inspect_files)
        self.clean_button.clicked.connect(self.clean_files)
        self.close_button.clicked.connect(self.reject)
        apply_database_info_dialog_style(self)

    def _start(self, callback):
        if self.worker is not None:
            return
        self.inspect_button.setEnabled(False)
        self.clean_button.setEnabled(False)
        self.worker = AsyncCallThread(callback)
        self.worker.succeeded.connect(self._completed)
        self.worker.failed.connect(self._failed)
        self.worker.finished.connect(self._finished)
        self.worker.start()

    def inspect_files(self):
        self.inspection = None
        self.table.setRowCount(0)
        self.status.setText("Инспекция служебных файлов…")
        self._start(self.service.inspect)

    def clean_files(self):
        if self.inspection is None or self.worker is not None:
            return
        preview = self.inspection
        self.inspection = None
        self.status.setText("Повторная проверка и очистка, не более 500 файлов за проход…")
        self._start(lambda: self.service.clean(preview))

    def _completed(self, result):
        if isinstance(result, Inspection):
            self.inspection = result
            self.table.setRowCount(len(result.candidates))
            for row, item in enumerate(result.candidates):
                for column, value in enumerate((item.relative_path, item.category, item.days, f"{item.size / 1024:.1f}")):
                    cell = QTableWidgetItem(str(value))
                    cell.setToolTip(str(value))
                    self.table.setItem(row, column, cell)
            text = f"К удалению: {len(result.candidates)} файлов, {result.bytes / 1024 / 1024:.2f} МБ. Сохранено при проверке: {result.preserved}."
            if result.truncated or result.errors:
                text += " Инспекция неполная; очистка недоступна. " + "\n".join(result.errors[:3])
            self.status.setText(text)
        else:
            self.table.setRowCount(0)
            self.status.setText(
                f"Удалено: {result['removed']}, освобождено {result['bytes'] / 1024 / 1024:.2f} МБ. "
                f"Пропущено: {result['skipped']}. За пределами прохода: {result['remaining']}.\n"
                + ("Результат записан в service_cleanup_last.json. " if result['report_written'] else "Итоговый отчёт не записан. ")
                + "Для следующего прохода повторите инспекцию."
                + ("\n" + "\n".join(result['errors'][:3]) if result['errors'] else "")
            )

    def _failed(self, error):
        self.inspection = None
        self.status.setText(f"Обслуживание не завершено: {error}")

    def _finished(self):
        self.worker = None
        self.inspect_button.setEnabled(True)
        preview = self.inspection
        self.clean_button.setEnabled(bool(preview and preview.candidates and not preview.errors and not preview.truncated))

    def reject(self):
        if self.worker is not None:
            self.status.setText("Дождитесь завершения текущей операции.")
            return
        super().reject()

    def closeEvent(self, event):
        if self.worker is not None:
            event.ignore()
            return
        super().closeEvent(event)
