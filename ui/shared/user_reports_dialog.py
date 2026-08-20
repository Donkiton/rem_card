from __future__ import annotations

from typing import Any

from PySide6.QtCore import QModelIndex, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from rem_card.services.user_reports import (
    REPORT_TYPE_PROBLEM,
    REPORT_TYPE_SUGGESTION,
    STATUS_CLOSED,
    STATUS_IN_PROGRESS,
    STATUS_NEW,
    STATUS_READ,
    UserReportsService,
    report_status_label,
)
from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.shared.window_state import SavedFramelessDialogMixin


class UserReportDialog(BaseStyledDialog):
    submitted = Signal()

    def __init__(self, *, role: str | None = None, parent=None, service: UserReportsService | None = None):
        super().__init__("Репорт", parent)
        self.role = str(role or "").strip()
        self.service = service or UserReportsService()
        self.resize(640, 460)
        self._setup_ui()

    def _setup_ui(self):
        layout = self.content_layout
        layout.setSpacing(12)

        title = QLabel("Опишите проблему или предложение")
        title.setObjectName("DisplaySettingsSectionTitle")
        layout.addWidget(title)

        self.tabs = QTabWidget(self)
        self.problem_edit = QTextEdit(self.tabs)
        self.problem_edit.setPlaceholderText(
            "Например: при создании карты значение аллергии «нет» попадает как аллергия на препарат «нет»."
        )
        self.suggestion_edit = QTextEdit(self.tabs)
        self.suggestion_edit.setPlaceholderText(
            "Например: предлагаю добавить быстрый фильтр или изменить поведение кнопки."
        )
        self.tabs.addTab(self.problem_edit, "Проблема")
        self.tabs.addTab(self.suggestion_edit, "Предложение")
        layout.addWidget(self.tabs, 1)

        hint = QLabel("Для проблем автоматически прикладываются логи программы за последний час.")
        hint.setWordWrap(True)
        hint.setObjectName("UserReportsHint")
        layout.addWidget(hint)

        footer = QHBoxLayout()
        footer.addStretch()
        cancel_btn = QPushButton("Отмена")
        cancel_btn.setObjectName("DialogOkBtn")
        cancel_btn.clicked.connect(self.reject)
        self.send_btn = QPushButton("Отправить")
        self.send_btn.setObjectName("DialogOkBtn")
        self.send_btn.clicked.connect(self._submit)
        footer.addWidget(cancel_btn)
        footer.addWidget(self.send_btn)
        layout.addLayout(footer)
        self.setStyleSheet(
            self.styleSheet()
            + """
            QLabel#DisplaySettingsSectionTitle {
                font-weight: bold;
                color: #2c3e50;
            }
            QLabel#UserReportsHint {
                color: #607080;
                border: none;
                background: transparent;
            }
            """
        )

    def _current_type_and_text(self) -> tuple[str, str]:
        if self.tabs.currentIndex() == 1:
            return REPORT_TYPE_SUGGESTION, self.suggestion_edit.toPlainText().strip()
        return REPORT_TYPE_PROBLEM, self.problem_edit.toPlainText().strip()

    def _submit(self):
        report_type, text = self._current_type_and_text()
        if not text:
            CustomMessageBox.warning(self, "Репорт", "Заполните текст перед отправкой.")
            return
        if len(text) > 20000:
            CustomMessageBox.warning(self, "Репорт", "Текст слишком длинный. Сократите описание до 20000 символов.")
            return

        type_text = "проблему" if report_type == REPORT_TYPE_PROBLEM else "предложение"
        reply = CustomMessageBox.question(
            self,
            "Подтверждение",
            f"Отправить {type_text} в папку репортов?",
        )
        if reply != CustomMessageBox.Yes:
            return

        self.send_btn.setEnabled(False)
        try:
            self.service.submit_report(report_type=report_type, text=text, role=self.role)
        except Exception as exc:
            self.send_btn.setEnabled(True)
            CustomMessageBox.critical(self, "Ошибка", f"Не удалось отправить репорт:\n{exc}")
            return

        self.submitted.emit()
        self.accept()


class UserReportsInboxDialog(SavedFramelessDialogMixin, BaseStyledDialog):
    reports_changed = Signal()
    _GEOMETRY_SETTINGS_KEY = "user_reports/inbox_dialog_geometry"
    _SPLITTER_SETTINGS_KEY = "user_reports/inbox_dialog_splitter_state"
    _TABLE_HEADER_SETTINGS_KEY = "user_reports/inbox_dialog_table_header_state"

    def __init__(self, *, role: str | None = None, parent=None, service: UserReportsService | None = None):
        super().__init__("Репорты", parent)
        self.role = str(role or "").strip()
        self.service = service or UserReportsService()
        self.reports: list[dict[str, Any]] = []
        self._loading = False
        self._selected_directory = ""
        self.resize(940, 640)
        self.setMinimumSize(700, 480)
        self.setSizeGripEnabled(True)
        self._init_saved_frameless_dialog(
            self._GEOMETRY_SETTINGS_KEY,
            drag_area_height=32,
        )
        self._setup_ui()
        self._restore_saved_geometry()
        self._load_reports()

    def _setup_ui(self):
        layout = self.content_layout
        layout.setSpacing(10)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        toolbar.addWidget(QLabel("Фильтр:"))
        self.filter_combo = QComboBox()
        self.filter_combo.addItem("Все", "all")
        self.filter_combo.addItem("Новые", STATUS_NEW)
        self.filter_combo.addItem("Проблемы", REPORT_TYPE_PROBLEM)
        self.filter_combo.addItem("Предложения", REPORT_TYPE_SUGGESTION)
        self.filter_combo.addItem("Прочитанные", STATUS_READ)
        self.filter_combo.addItem("В работе", STATUS_IN_PROGRESS)
        self.filter_combo.addItem("Закрытые", STATUS_CLOSED)
        self.filter_combo.currentIndexChanged.connect(lambda *_args: self._load_reports())
        toolbar.addWidget(self.filter_combo)
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("UserReportsSummary")
        toolbar.addWidget(self.summary_label, 1)
        refresh_btn = QPushButton("Обновить")
        refresh_btn.setObjectName("DialogOkBtn")
        refresh_btn.clicked.connect(lambda *_args: self._load_reports())
        toolbar.addWidget(refresh_btn)
        layout.addLayout(toolbar)

        self.splitter = QSplitter(Qt.Vertical, self)
        self.table = QTableWidget(0, 5, self.splitter)
        self.table.setHorizontalHeaderLabels(["Дата", "Тип", "Статус", "Пользователь", "Текст"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.splitter.addWidget(self.table)

        detail_widget = QWidget(self.splitter)
        detail_layout = QVBoxLayout(detail_widget)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(8)
        self.detail_tabs = QTabWidget(detail_widget)
        self.detail_text = QPlainTextEdit(self.detail_tabs)
        self.detail_text.setReadOnly(True)
        self.logs_text = QPlainTextEdit(self.detail_tabs)
        self.logs_text.setReadOnly(True)
        self.detail_tabs.addTab(self.detail_text, "Обращение")
        self.detail_tabs.addTab(self.logs_text, "Логи")
        detail_layout.addWidget(self.detail_tabs, 1)

        action_row = QHBoxLayout()
        action_row.addStretch()
        self.mark_new_btn = QPushButton("Вернуть в новые")
        self.mark_new_btn.setObjectName("DialogOkBtn")
        self.mark_new_btn.clicked.connect(lambda: self._change_selected_status(STATUS_NEW))
        self.in_progress_btn = QPushButton("В работу")
        self.in_progress_btn.setObjectName("DialogOkBtn")
        self.in_progress_btn.clicked.connect(lambda: self._change_selected_status(STATUS_IN_PROGRESS))
        self.close_btn = QPushButton("Закрыть")
        self.close_btn.setObjectName("DialogOkBtn")
        self.close_btn.clicked.connect(lambda: self._change_selected_status(STATUS_CLOSED))
        action_row.addWidget(self.mark_new_btn)
        action_row.addWidget(self.in_progress_btn)
        action_row.addWidget(self.close_btn)
        detail_layout.addLayout(action_row)
        self.splitter.addWidget(detail_widget)
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 3)
        layout.addWidget(self.splitter, 1)

        footer = QHBoxLayout()
        footer.addStretch()
        close_dialog_btn = QPushButton("Закрыть окно")
        close_dialog_btn.setObjectName("DialogOkBtn")
        close_dialog_btn.clicked.connect(self.accept)
        footer.addWidget(close_dialog_btn)
        layout.addLayout(footer)

        self.setStyleSheet(
            self.styleSheet()
            + """
            QLabel#UserReportsSummary {
                color: #607080;
                border: none;
                background: transparent;
            }
            """
        )
        self._update_action_buttons()

    def _restore_saved_geometry(self) -> None:
        super()._restore_saved_geometry()
        settings = self._settings()
        splitter_state = settings.value(self._SPLITTER_SETTINGS_KEY)
        if splitter_state is not None:
            self.splitter.restoreState(splitter_state)
        header_state = settings.value(self._TABLE_HEADER_SETTINGS_KEY)
        if header_state is not None:
            self.table.horizontalHeader().restoreState(header_state)

    def _save_saved_geometry(self) -> None:
        super()._save_saved_geometry()
        settings = self._settings()
        settings.setValue(self._SPLITTER_SETTINGS_KEY, self.splitter.saveState())
        settings.setValue(self._TABLE_HEADER_SETTINGS_KEY, self.table.horizontalHeader().saveState())
        settings.sync()

    def _load_reports(self, select_directory: str | None = None):
        if self._loading:
            return
        self._loading = True
        selected = select_directory if select_directory is not None else self._selected_directory
        try:
            try:
                self.reports = self.service.list_reports()
            except Exception as exc:
                self.reports = []
                CustomMessageBox.warning(self, "Репорты", f"Не удалось загрузить репорты:\n{exc}")
            selected_restored = self._fill_table(selected)
            self._update_summary()
        finally:
            self._loading = False
        if selected_restored:
            self._on_selection_changed(mark_opened=False)

    def _filtered_reports(self) -> list[dict[str, Any]]:
        filter_key = str(self.filter_combo.currentData() or "all")
        if filter_key == "all":
            return list(self.reports)
        if filter_key in {REPORT_TYPE_PROBLEM, REPORT_TYPE_SUGGESTION}:
            return [item for item in self.reports if item.get("type") == filter_key]
        return [item for item in self.reports if item.get("status") == filter_key]

    def _fill_table(self, selected_directory: str = "") -> bool:
        previous_blocked = self.table.blockSignals(True)
        selected_row = -1
        reports = self._filtered_reports()
        try:
            self.table.setRowCount(0)
            for row, report in enumerate(reports):
                self.table.insertRow(row)
                directory = str(report.get("directory") or "")
                values = [
                    str(report.get("created_at") or ""),
                    str(report.get("type_label") or ""),
                    str(report.get("status_label") or ""),
                    self._author_text(report),
                    self._preview_text(str(report.get("text") or "")),
                ]
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setData(Qt.UserRole, directory)
                    if column == 2 and report.get("status") == STATUS_NEW:
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)
                    self.table.setItem(row, column, item)
                if selected_directory and directory and directory == selected_directory:
                    selected_row = row

            if selected_row >= 0:
                self.table.selectRow(selected_row)
            else:
                self.table.clearSelection()
                self.table.setCurrentIndex(QModelIndex())
                self._selected_directory = ""
                self.detail_text.setPlainText("Выберите репорт в списке." if reports else "Репортов нет.")
                self.logs_text.clear()
                self._update_action_buttons()
        finally:
            self.table.blockSignals(previous_blocked)

        return selected_row >= 0

    def _update_summary(self):
        total = len(self.reports)
        new_count = sum(1 for item in self.reports if item.get("status") == STATUS_NEW)
        filtered = len(self._filtered_reports())
        self.summary_label.setText(f"Всего: {total}. Новых: {new_count}. В текущем фильтре: {filtered}.")

    def _on_selection_changed(self, *, mark_opened: bool = True):
        if self._loading:
            return
        item = self.table.currentItem()
        directory = str(item.data(Qt.UserRole) or "") if item else ""
        self._selected_directory = directory
        report = self._report_by_directory(directory)
        if not report:
            self.detail_text.setPlainText("Репорт не выбран.")
            self.logs_text.clear()
            self._update_action_buttons()
            return

        if mark_opened and report.get("status") == STATUS_NEW:
            try:
                updated = self.service.mark_opened(directory, role=self.role)
                if updated:
                    self._replace_report(updated)
                    report = updated
                    self.reports_changed.emit()
                    self._update_current_row_status(updated)
                    self._update_summary()
            except Exception as exc:
                CustomMessageBox.warning(self, "Репорты", f"Не удалось отметить репорт прочитанным:\n{exc}")

        self._show_report(report)
        self._update_action_buttons()

    def _show_report(self, report: dict[str, Any]):
        context = report.get("created_by") if isinstance(report.get("created_by"), dict) else {}
        lines = [
            f"Тип: {report.get('type_label') or ''}",
            f"Статус: {report.get('status_label') or ''}",
            f"Отправлено: {report.get('created_at') or ''}",
            f"Обновлено: {report.get('updated_at') or ''}",
            f"Пользователь: {context.get('user') or ''}",
            f"Роль: {context.get('role') or ''}",
            f"Компьютер: {context.get('host') or ''}",
            f"Версия: {context.get('app_version') or ''}",
            f"Папка: {report.get('directory') or ''}",
            "",
            "Текст:",
            str(report.get("text") or ""),
        ]
        self.detail_text.setPlainText("\n".join(lines))

        logs = self.service.read_logs(str(report.get("directory") or ""))
        if logs:
            self.logs_text.setPlainText(logs)
            self.detail_tabs.setTabEnabled(1, True)
        else:
            self.logs_text.setPlainText("Для этого обращения логи не прикладывались.")
            self.detail_tabs.setTabEnabled(1, False)

    def _change_selected_status(self, status: str):
        directory = self._selected_directory
        if not directory:
            return
        label = report_status_label(status)
        reply = CustomMessageBox.question(
            self,
            "Подтверждение",
            f"Изменить статус выбранного репорта на «{label}»?",
        )
        if reply != CustomMessageBox.Yes:
            return
        try:
            updated = self.service.update_status(directory, status, role=self.role, note="manual")
        except Exception as exc:
            CustomMessageBox.critical(self, "Ошибка", f"Не удалось изменить статус:\n{exc}")
            return
        self._replace_report(updated)
        self.reports_changed.emit()
        self._load_reports(select_directory=directory)

    def _update_action_buttons(self):
        has_selection = bool(self._selected_directory)
        for button in (self.mark_new_btn, self.in_progress_btn, self.close_btn):
            button.setEnabled(has_selection)

    def _update_current_row_status(self, report: dict[str, Any]):
        row = self.table.currentRow()
        if row < 0:
            return
        status_item = self.table.item(row, 2)
        if status_item is not None:
            status_item.setText(str(report.get("status_label") or ""))
            font = status_item.font()
            font.setBold(report.get("status") == STATUS_NEW)
            status_item.setFont(font)

    def _replace_report(self, updated: dict[str, Any]):
        directory = str(updated.get("directory") or "")
        if not directory:
            return
        for index, report in enumerate(self.reports):
            if str(report.get("directory") or "") == directory:
                self.reports[index] = updated
                return
        self.reports.insert(0, updated)

    def _report_by_directory(self, directory: str) -> dict[str, Any] | None:
        for report in self.reports:
            if str(report.get("directory") or "") == directory:
                return report
        return None

    def _author_text(self, report: dict[str, Any]) -> str:
        context = report.get("created_by") if isinstance(report.get("created_by"), dict) else {}
        user = str(context.get("user") or "")
        role = str(context.get("role") or "")
        host = str(context.get("host") or "")
        left = user or role or "неизвестно"
        return f"{left} @ {host}" if host else left

    def _preview_text(self, text: str) -> str:
        single_line = " ".join(str(text or "").split())
        return single_line[:180] + ("..." if len(single_line) > 180 else "")
