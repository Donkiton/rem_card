from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QHeaderView
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QLineEdit
from PySide6.QtWidgets import QPushButton
from PySide6.QtWidgets import QTableWidget
from PySide6.QtWidgets import QTableWidgetItem
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget
from rem_card.app import operblock_startup_metrics
from rem_card.app.logger import logger
from rem_card.services.operblock_service import OperBlockService
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.styles.theme import BG_MAIN
from rem_card.ui.styles.theme import COLOR_PRIMARY_DARK
from rem_card.ui.styles.theme import STYLE_SECTOR8_BUTTON
from rem_card.ui.styles.theme import TEXT_PRIMARY
from rem_card.ui.styles.theme import TEXT_SECONDARY
from rem_card.ui.styles.theme_runtime import set_widget_style
from rem_card.ui.operblock_view.operblock_helpers import (
    _format_dt,
    _safe_int,
)
from rem_card.ui.operblock_view.operblock_visual_primitives import (
    DANGER_BUTTON_STYLE,
)


class OperBlockArchiveMixin:
    def _filter_archive_cases_by_table(self, cases: list[dict]) -> list[dict]:
        if not self._table_filter_code:
            return [dict(item or {}) for item in cases]
        return [
            dict(item or {})
            for item in cases
            if str((item or {}).get("table_code") or "").strip().lower() == self._table_filter_code
        ]

    def _build_archive_page(self) -> QWidget:
        """Создаёт общий центр архива, используемый во всех рабочих ролях."""
        metric_started = operblock_startup_metrics.timer_start()
        from rem_card.ui.archive_center.archive_main_widget import ArchiveMainWidget

        page = ArchiveMainWidget(
            self.patient_service,
            remcard_service=self.remcard_service,
            parent=self.stack,
            role="operblock",
            allow_edit=not self.is_view_only_mode(),
            allow_rao_edit=False,
            allow_operblock_edit=not self.is_view_only_mode(),
            operblock_service=self.operblock_service,
            initial_destination=1,
        )
        page.operblock_case_selected.connect(self._open_case_from_unified_archive)
        page.operblock_edit_requested.connect(lambda case: self._open_archived_case_edit(case))
        page.patient_selected.connect(self._on_rao_case_selected_in_operblock_archive)
        common_button = QPushButton("Общий архив")
        common_button.setMinimumHeight(34)
        set_widget_style(common_button, STYLE_SECTOR8_BUTTON)
        common_button.clicked.connect(lambda: self._open_common_operblock_archive())
        page.layout().addWidget(common_button)
        operblock_startup_metrics.record_since("build_archive_page_ms", metric_started, source="operblock_widget")
        if getattr(self, "_creating_lazy_archive_page", False):
            operblock_startup_metrics.record_since(
                "archive_page_lazy_build_ms",
                metric_started,
                source="operblock_widget",
            )
        return page

    def _open_common_operblock_archive(self):
        """Open a deliberately narrow central archive surface on user request."""
        from pathlib import Path
        from rem_card.app.operblock_local_destination import get_operblock_destination
        from rem_card.services.operblock.central_archive_facade import ExplicitCentralOperBlockService
        from rem_card.ui.doctor_view.archive_widget import ARCHIVE_MODE_OPERBLOCK, ArchiveWidget

        root = get_operblock_destination()
        if not root:
            CustomMessageBox.warning(self, "Общий архив", "Основная база для оперблока ещё не настроена.")
            return
        db_path = str(Path(root) / "archiv" / "rao_journal.db")
        if not getattr(self, "_common_archive_page", None):
            facade = ExplicitCentralOperBlockService(central_root=root)
            page = QWidget(self.stack)
            layout = QVBoxLayout(page)
            layout.setContentsMargins(8, 8, 8, 8)
            toolbar = QHBoxLayout()
            title = QLabel("Общий архив оперблока")
            set_widget_style(title, f"font-size: 18px; font-weight: 800; color: {COLOR_PRIMARY_DARK};")
            back_button = QPushButton("К локальному архиву")
            back_button.setMinimumHeight(34)
            set_widget_style(back_button, STYLE_SECTOR8_BUTTON)
            back_button.clicked.connect(lambda: self.stack.setCurrentWidget(self.archive_page))
            toolbar.addWidget(title)
            toolbar.addStretch(1)
            toolbar.addWidget(back_button)
            layout.addLayout(toolbar)
            archive = ArchiveWidget(
                self.patient_service,
                remcard_service=self.remcard_service,
                parent=page,
                allow_edit=not self.is_view_only_mode(),
                operblock_service=facade,
                fixed_source_mode=ARCHIVE_MODE_OPERBLOCK,
                embedded=False,
            )
            # This surface must never expose destructive central archive actions.
            for button in (archive.btn_delete_last, archive.btn_delete, archive.btn_report_stats):
                button.setEnabled(False)
                button.hide()
            archive.operblock_case_selected.connect(
                lambda case, path=db_path: self._open_external_archive_case({
                    **dict(case or {}),
                    "source_db_path": str(dict(case or {}).get("source_db_path") or path),
                    "is_external_archive": True,
                })
            )
            archive.operblock_edit_requested.connect(lambda case: self._open_common_archived_case_edit(case))
            layout.addWidget(archive, 1)
            self._common_archive_page = page
            self._common_archive_widget = archive
            self._common_archive_service = facade
            self.stack.addWidget(page)
        self.stack.setCurrentWidget(self._common_archive_page)
        self._common_archive_widget.load_data(reset_page=False)

    def _open_common_archived_case_edit(self, case):
        """Load and update central archive metadata through one gateway per worker.

        The central connection is never retained by the local clinical shell:
        both read and write call the explicit facade from ``AsyncCallThread``.
        """
        if self.is_view_only_mode() or getattr(self, "_is_closing", False):
            return
        case = dict(case or {}) if isinstance(case, dict) else {}
        case_id = _safe_int(case.get("source_operation_case_id") or case.get("operation_case_id"))
        if not case_id:
            CustomMessageBox.warning(self, "Общий архив", "Не удалось определить запись оперблока.")
            return
        service = getattr(self, "_common_archive_service", None)
        if service is None:
            CustomMessageBox.warning(self, "Общий архив", "Подключение к общему архиву не создано.")
            return
        from rem_card.ui.shared.async_call import AsyncCallThread

        worker = AsyncCallThread(
            lambda: service.get_operation_case_form_data(int(case_id)), parent=self,
        )
        self._common_archive_read_worker = worker
        worker.succeeded.connect(
            lambda initial_data, target_id=case_id: self._show_common_archive_edit_dialog(target_id, initial_data)
        )
        worker.failed.connect(
            lambda exc: CustomMessageBox.warning(self, "Общий архив", f"Не удалось открыть карту для редактирования:\n{exc}")
        )
        worker.finished.connect(
            lambda: setattr(self, "_common_archive_read_worker", None)
        )
        worker.start()

    def _show_common_archive_edit_dialog(self, operation_case_id: int, initial_data):
        if self.is_view_only_mode() or getattr(self, "_is_closing", False):
            return
        initial_data = dict(initial_data or {})
        if str(initial_data.get("case_status") or "") != "closed":
            CustomMessageBox.warning(self, "Общий архив", "Редактировать можно только завершённую карту.")
            return
        from rem_card.ui.operblock_view.operblock_admission_dialogs import OccupyTableDialog
        from rem_card.ui.operblock_view.operblock_helpers import _operblock_table_display_name
        import weakref

        table_code = str(initial_data.get("table_code") or "")
        table_name = str(initial_data.get("table_name") or "") or _operblock_table_display_name(table_code)
        dialog = OccupyTableDialog(
            table_code, table_name, self, mode="edit", initial_data=initial_data,
            operation_case_id=int(operation_case_id),
        )
        dialog_ref = weakref.ref(dialog)
        expected_case_revision = initial_data.get("operation_case_revision")
        expected_admission_revision = initial_data.get("admission_revision")

        def save():
            form = dialog_ref()
            if form is None:
                return
            try:
                payload = form.get_data()
            except Exception as exc:
                CustomMessageBox.warning(form, "Ошибка", str(exc))
                return
            form.set_saving(True)
            service = getattr(self, "_common_archive_service", None)
            if service is None:
                form.set_saving(False)
                CustomMessageBox.warning(form, "Общий архив", "Подключение к общему архиву недоступно.")
                return
            from rem_card.ui.shared.async_call import AsyncCallThread
            worker = AsyncCallThread(
                lambda: service.update_archived_operation_case_form_data(
                    int(operation_case_id), payload,
                    expected_operation_case_revision=expected_case_revision,
                    expected_admission_revision=expected_admission_revision,
                ),
                parent=self,
            )
            self._common_archive_write_worker = worker

            def saved(_result):
                current = dialog_ref()
                if current is not None:
                    current.set_saving(False)
                    current.accept()
                archive = getattr(self, "_common_archive_widget", None)
                if archive is not None:
                    archive.load_data(reset_page=False)

            def save_failed(exc):
                current = dialog_ref()
                if current is not None:
                    current.set_saving(False)
                from rem_card.services.concurrency import DataConflictError
                from rem_card.services.operblock_service import OperBlockConflictError
                title = "Конфликт данных" if isinstance(exc, (DataConflictError, OperBlockConflictError)) else "Ошибка сохранения"
                CustomMessageBox.warning(self, title, str(exc))

            worker.succeeded.connect(saved)
            worker.failed.connect(save_failed)
            worker.finished.connect(lambda: setattr(self, "_common_archive_write_worker", None))
            worker.start()

        dialog.save_button.clicked.connect(save)
        dialog.exec()

    def _build_legacy_archive_page(self) -> QWidget:
        metric_started = operblock_startup_metrics.timer_start()
        page = QWidget()
        set_widget_style(page, f"QWidget {{ background-color: {BG_MAIN}; color: {TEXT_PRIMARY}; }}")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        header = QHBoxLayout()
        archive_title = (
            f"Архив пациентов: {self._table_filter_name}"
            if self._table_filter_name
            else "Архив пациентов операционной"
        )
        title = QLabel(archive_title)
        set_widget_style(title, f"font-size: 18px; font-weight: 800; color: {COLOR_PRIMARY_DARK};")
        self.archive_search_input = QLineEdit()
        self.archive_search_input.setPlaceholderText("ФИО, ИБ, диагноз")
        self.archive_search_input.setMinimumHeight(34)
        self.archive_search_input.textChanged.connect(
            lambda _text: self.refresh_operblock_archive(force=True, page=1)
        )
        self.archive_refresh_button = QPushButton("Обновить")
        self.archive_refresh_button.setMinimumHeight(34)
        set_widget_style(self.archive_refresh_button, STYLE_SECTOR8_BUTTON)
        self.archive_refresh_button.clicked.connect(
            lambda: self.refresh_operblock_archive(
                force=True,
                loading_message="Обновление архива оперблока...",
            )
        )
        header.addWidget(title, 0)
        header.addWidget(self.archive_search_input, 1)
        header.addWidget(self.archive_refresh_button, 0)
        layout.addLayout(header)

        self.archive_table = QTableWidget()
        self.archive_table.setColumnCount(7)
        self.archive_table.setHorizontalHeaderLabels(["Стол", "ФИО", "ИБ №", "Диагноз", "Поступил", "Переведён", "Статус"])
        self.archive_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.archive_table.setSelectionMode(QTableWidget.SingleSelection)
        self.archive_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.archive_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.archive_table.itemSelectionChanged.connect(self._update_operblock_archive_buttons)
        self.archive_table.itemDoubleClicked.connect(lambda _item: self._open_selected_archive_case())
        layout.addWidget(self.archive_table, 1)

        pagination = QHBoxLayout()
        pagination.setContentsMargins(0, 0, 0, 0)
        pagination.setSpacing(6)
        self.archive_prev_page_button = QPushButton("◀")
        set_widget_style(self.archive_prev_page_button, STYLE_SECTOR8_BUTTON)
        self.archive_prev_page_button.clicked.connect(lambda: self._set_operblock_archive_page(self._archive_current_page - 1))
        pagination.addWidget(self.archive_prev_page_button)

        self.archive_page_buttons_layout = QHBoxLayout()
        self.archive_page_buttons_layout.setContentsMargins(0, 0, 0, 0)
        self.archive_page_buttons_layout.setSpacing(4)
        pagination.addLayout(self.archive_page_buttons_layout)

        self.archive_next_page_button = QPushButton("▶")
        set_widget_style(self.archive_next_page_button, STYLE_SECTOR8_BUTTON)
        self.archive_next_page_button.clicked.connect(lambda: self._set_operblock_archive_page(self._archive_current_page + 1))
        pagination.addWidget(self.archive_next_page_button)

        self.archive_page_info_label = QLabel("Страница 1 из 1")
        set_widget_style(self.archive_page_info_label, f"border: none; color: {TEXT_SECONDARY}; font-weight: 600;")
        pagination.addWidget(self.archive_page_info_label)
        pagination.addStretch(1)

        self.archive_page_jump_input = QLineEdit()
        self.archive_page_jump_input.setPlaceholderText("№")
        self.archive_page_jump_input.setMaximumWidth(52)
        self.archive_page_jump_input.returnPressed.connect(self._jump_operblock_archive_page_from_input)
        pagination.addWidget(self.archive_page_jump_input)

        self.archive_page_jump_button = QPushButton("Перейти")
        set_widget_style(self.archive_page_jump_button, STYLE_SECTOR8_BUTTON)
        self.archive_page_jump_button.clicked.connect(self._jump_operblock_archive_page_from_input)
        pagination.addWidget(self.archive_page_jump_button)
        layout.addLayout(pagination)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.archive_open_button = QPushButton("Открыть карту")
        self.archive_restore_button = QPushButton("Вернуть на стол")
        self.archive_delete_button = QPushButton("Удалить анест. карту")
        self.archive_delete_all_button = QPushButton("Удалить всех")
        for button in (self.archive_open_button, self.archive_restore_button, self.archive_delete_button, self.archive_delete_all_button):
            button.setMinimumHeight(36)
            set_widget_style(button, STYLE_SECTOR8_BUTTON)
            button.setEnabled(False)
        set_widget_style(self.archive_delete_button, DANGER_BUTTON_STYLE)
        set_widget_style(self.archive_delete_all_button, DANGER_BUTTON_STYLE)
        self.archive_open_button.clicked.connect(self._open_selected_archive_case)
        self.archive_restore_button.clicked.connect(self._restore_selected_archive_case)
        self.archive_delete_button.clicked.connect(self._delete_selected_archive_case)
        self.archive_delete_all_button.clicked.connect(self._delete_all_archive_cases)
        actions.addWidget(self.archive_open_button)
        actions.addWidget(self.archive_restore_button)
        actions.addWidget(self.archive_delete_button)
        actions.addWidget(self.archive_delete_all_button)
        layout.addLayout(actions)
        operblock_startup_metrics.record_since("build_archive_page_ms", metric_started, source="operblock_widget")
        if getattr(self, "_creating_lazy_archive_page", False):
            operblock_startup_metrics.record_since(
                "archive_page_lazy_build_ms",
                metric_started,
                source="operblock_widget",
            )
        return page

    def _show_operblock_archive(self):
        if self._is_closing or self._write_pending:
            return
        action_info = self._start_opblock_action_diagnostics("operblock_open_archive")
        loading_key = self._show_operblock_loading(
            "Открытие архива оперблока...",
            key="open-archive",
            auto_hide_ms=30000,
        )
        try:
            current_widget = self.stack.currentWidget()
            if current_widget == self.protocol_page and self._current_operation_case_id:
                self._archive_return_operation_case_id = int(self._current_operation_case_id)
            elif current_widget != self.archive_page:
                self._archive_return_operation_case_id = None
            first_open = self.archive_page is None
            first_open_started = operblock_startup_metrics.timer_start() if first_open else None
            if not self._ensure_archive_page_created():
                return
            self._set_protocol_chrome(True)
            self.stack.setCurrentWidget(self.archive_page)
            self._current_operation_case_id = None
            self._current_admission_id = None
            self._current_operation_start = None
            self._current_operation_end = None
            self._current_case_active = False
            self._current_operation_has_vitals = False
            self._current_stage_state = {}
            self._current_anesthesia_active = False
            self._current_surgery_active = False
            self._current_anesthesia_assistance_type = ""
            self._current_operation_name = ""
            self._current_protocol_display = ""
            self._update_protocol_title_label()
            self._update_operblock_staff_legend()
            self.refresh_operblock_archive(force=True, page=1)
            if first_open:
                operblock_startup_metrics.record_since(
                    "first_open_archive_ms",
                    first_open_started,
                    source="operblock_widget",
                )
        finally:
            self._finish_opblock_action_diagnostics(action_info, "success")
            self._hide_operblock_loading(loading_key)

    def _open_case_from_unified_archive(self, case):
        case = dict(case or {}) if isinstance(case, dict) else {}
        case_id = _safe_int(case.get("source_operation_case_id") or case.get("operation_case_id"))
        if not case_id:
            CustomMessageBox.warning(self, "Архив оперблока", "Не удалось определить запись оперблока.")
            return
        self._archive_return_operation_case_id = None
        if case.get("is_external_archive"):
            self._open_external_archive_case(case)
            return
        self._protocol_opened_from_archive = True
        self._open_protocol(case_id)

    def _on_rao_case_selected_in_operblock_archive(self, _patient):
        CustomMessageBox.information(
            self,
            "Архив реанимации",
            "Просмотр карт реанимации доступен в рабочих местах врача и медсестры.",
        )

    def _open_archived_case_edit(self, case):
        case = dict(case or {}) if isinstance(case, dict) else {}
        if case.get("is_external_archive"):
            CustomMessageBox.information(self, "Архив оперблока", "Записи прошлых циклов доступны только для просмотра.")
            return
        db = getattr(self.operblock_service, "db", None)
        runtime_mode = str(getattr(getattr(db, "runtime_context", None), "mode", "") or "")
        if runtime_mode == "opblock_offline" and (
            str(case.get("migration_status") or "").strip().lower() == "verified" or case.get("migrated_at")
        ):
            CustomMessageBox.information(self, "Архив оперблока", "Подтверждённая карта редактируется в общем архиве.")
            return
        case_id = _safe_int(case.get("source_operation_case_id") or case.get("operation_case_id"))
        if case_id:
            self._open_edit_patient_dialog(case_id)

    def _filtered_archive_cases(self) -> list[dict]:
        query = str(getattr(self, "archive_search_input", None).text() if hasattr(self, "archive_search_input") else "").strip().casefold()
        cases = list(getattr(self, "_archive_cases", []) or [])
        if not query:
            return cases
        result = []
        for case in cases:
            haystack = " ".join(
                [
                    str(case.get("full_name") or ""),
                    str(case.get("history_number") or ""),
                    str(case.get("diagnosis_code") or ""),
                    str(case.get("diagnosis_text") or ""),
                    str(case.get("table_display_name") or ""),
                ]
            ).casefold()
            if query in haystack:
                result.append(case)
        return result

    def _apply_operblock_archive_cases(self):
        table = getattr(self, "archive_table", None)
        if table is None:
            return
        cases = self._filtered_archive_cases()
        table.setRowCount(0)
        table.setRowCount(len(cases))
        for row, case in enumerate(cases):
            diagnosis_text = str(case.get("diagnosis_text") or "—")
            diagnosis_code = str(case.get("diagnosis_code") or "").strip()
            if diagnosis_code:
                diagnosis_text = f"{diagnosis_code}: {diagnosis_text}"
            status = str(case.get("status") or "").strip().lower()
            values = [
                case.get("table_display_name") or "—",
                case.get("full_name") or "Неизвестно",
                case.get("history_number") or "",
                diagnosis_text,
                _format_dt(case.get("started_at")),
                "" if status == "active" else _format_dt(case.get("ended_at")),
                "В операционной" if status == "active" else "В архиве",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setData(Qt.UserRole, dict(case))
                table.setItem(row, column, item)
        self._update_operblock_archive_buttons()
        self._refresh_operblock_archive_pagination_ui()

    def _set_operblock_archive_page(self, page: int):
        page = max(1, min(int(page or 1), int(getattr(self, "_archive_total_pages", 1) or 1)))
        if page == getattr(self, "_archive_current_page", 1) and getattr(self, "_archive_cases", None):
            self._apply_operblock_archive_cases()
            return
        self.refresh_operblock_archive(force=True, page=page)

    def _jump_operblock_archive_page_from_input(self):
        raw = str(getattr(self, "archive_page_jump_input", None).text() if hasattr(self, "archive_page_jump_input") else "").strip()
        if not raw:
            return
        if not raw.isdigit():
            CustomMessageBox.warning(self, "Пагинация", "Введите номер страницы цифрами.")
            return
        self._set_operblock_archive_page(int(raw))

    def _refresh_operblock_archive_pagination_ui(self):
        layout = getattr(self, "archive_page_buttons_layout", None)
        if layout is None:
            return
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        current_page = max(1, int(getattr(self, "_archive_current_page", 1) or 1))
        total_pages = max(1, int(getattr(self, "_archive_total_pages", 1) or 1))
        max_visible = 7
        start_page = max(1, current_page - 3)
        end_page = min(total_pages, start_page + max_visible - 1)
        start_page = max(1, end_page - max_visible + 1)

        for page in range(start_page, end_page + 1):
            button = QPushButton(str(page))
            button.setCheckable(True)
            button.setChecked(page == current_page)
            button.setMinimumWidth(30)
            button.setFixedHeight(28)
            set_widget_style(button, STYLE_SECTOR8_BUTTON)
            button.clicked.connect(lambda _checked=False, p=page: self._set_operblock_archive_page(p))
            layout.addWidget(button)

        if hasattr(self, "archive_prev_page_button"):
            self.archive_prev_page_button.setEnabled(current_page > 1 and not self._write_pending)
        if hasattr(self, "archive_next_page_button"):
            self.archive_next_page_button.setEnabled(current_page < total_pages and not self._write_pending)
        if hasattr(self, "archive_page_info_label"):
            total_records = int(getattr(self, "_archive_total_records", 0) or 0)
            self.archive_page_info_label.setText(f"Страница {current_page} из {total_pages} · записей: {total_records}")
        if hasattr(self, "archive_page_jump_button"):
            self.archive_page_jump_button.setEnabled(not self._write_pending)
        if hasattr(self, "archive_page_jump_input"):
            self.archive_page_jump_input.setEnabled(not self._write_pending)

    def _archive_case_from_row(self, row: int) -> dict | None:
        table = getattr(self, "archive_table", None)
        if table is None or row < 0:
            return None
        item = table.item(row, 0)
        if item is None:
            return None
        data = item.data(Qt.UserRole)
        return dict(data or {}) if isinstance(data, dict) else None

    def _selected_archive_case(self) -> dict | None:
        table = getattr(self, "archive_table", None)
        if table is None:
            return None
        return self._archive_case_from_row(table.currentRow())

    def _update_operblock_archive_buttons(self):
        selected = self._selected_archive_case()
        enabled = bool(selected) and not self._write_pending
        selected_external = enabled and bool((selected or {}).get("is_external_archive"))
        selected_closed = (
            enabled
            and not selected_external
            and str((selected or {}).get("status") or "").strip().lower() == "closed"
        )
        selected_current_case = enabled and not selected_external
        has_closed_cases = any(
            str((case or {}).get("status") or "").strip().lower() == "closed"
            and not bool((case or {}).get("is_external_archive"))
            for case in (getattr(self, "_archive_cases", None) or [])
        ) and not self._write_pending
        if hasattr(self, "archive_open_button"):
            self.archive_open_button.setEnabled(enabled)
        if hasattr(self, "archive_restore_button"):
            self.archive_restore_button.setEnabled(selected_closed)
        if hasattr(self, "archive_delete_button"):
            self.archive_delete_button.setEnabled(selected_current_case)
        if hasattr(self, "archive_delete_all_button"):
            self.archive_delete_all_button.setEnabled(has_closed_cases)
        self._refresh_operblock_archive_pagination_ui()

    def _open_selected_archive_case(self):
        selected = self._selected_archive_case()
        if selected and selected.get("is_external_archive"):
            self._open_external_archive_case(selected)
            return
        case_id = _safe_int((selected or {}).get("operation_case_id"))
        if case_id:
            self._open_protocol(case_id)

    def _close_external_archive_viewer(self):
        viewer = getattr(self, "_external_archive_viewer", None)
        if viewer is not None:
            try:
                if hasattr(viewer, "shutdown"):
                    viewer.shutdown()
            except Exception as exc:
                logger.warning("Failed to shutdown external operblock archive viewer: %s", exc)
            try:
                if getattr(self, "stack", None) is not None and self.stack.indexOf(viewer) >= 0:
                    self.stack.removeWidget(viewer)
            except Exception:
                pass
            try:
                viewer.deleteLater()
            except Exception:
                pass
        self._external_archive_viewer = None
        if self._external_archive_db_manager is not None:
            try:
                self._external_archive_db_manager.close()
            except Exception as exc:
                logger.warning("Failed to close external operblock archive DB manager: %s", exc)
        self._external_archive_db_manager = None

    def _return_from_external_archive_viewer(self):
        loading_key = self._show_operblock_loading(
            "Возврат в архив оперблока...",
            key="return-external-archive",
            auto_hide_ms=30000,
        )
        try:
            if getattr(self, "archive_page", None) is not None:
                self.stack.setCurrentWidget(self.archive_page)
            self._close_external_archive_viewer()
            self.refresh_operblock_archive(force=True)
        finally:
            self._hide_operblock_loading(loading_key)

    def _open_external_archive_case(self, selected: dict):
        from rem_card.ui.operblock_view.operblock_main_widget import OperBlockMainWidget

        source_db_path = str((selected or {}).get("source_db_path") or "").strip()
        case_id = _safe_int((selected or {}).get("source_operation_case_id") or (selected or {}).get("operation_case_id"))
        if not source_db_path or not case_id:
            CustomMessageBox.warning(self, "Архив оперблока", "Не удалось определить архивную БД или запись оперблока.")
            return
        loading_key = self._show_operblock_loading(
            "Открытие архивной операции...",
            key="open-external-archive",
            auto_hide_ms=30000,
        )
        try:
            self._close_external_archive_viewer()
            ro_db_manager = None
            try:
                from rem_card.services.archive_readonly_service import create_archive_readonly_service

                ro_remcard_service, ro_db_manager = create_archive_readonly_service(source_db_path)
                ro_operblock_service = OperBlockService(ro_db_manager)
                ro_patient_service = getattr(ro_remcard_service, "_patients", self.patient_service)
                viewer = OperBlockMainWidget(
                    ro_patient_service,
                    ro_remcard_service,
                    ro_operblock_service,
                    parent=self.stack,
                    table_code=self._table_filter_code,
                    view_only=True,
                )
            except Exception as exc:
                if ro_db_manager is not None:
                    try:
                        ro_db_manager.close()
                    except Exception:
                        pass
                CustomMessageBox.warning(self, "Архив оперблока", f"Не удалось открыть архивную БД:\n{exc}")
                return
            self._external_archive_viewer = viewer
            self._external_archive_db_manager = ro_db_manager
            viewer.view_back_requested.connect(self._return_from_external_archive_viewer)
            self.stack.addWidget(viewer)
            self.stack.setCurrentWidget(viewer)
            viewer.open_archive_protocol(case_id)
        finally:
            self._hide_operblock_loading(loading_key)

    def _restore_selected_archive_case(self):
        selected = self._selected_archive_case()
        case_id = _safe_int((selected or {}).get("operation_case_id"))
        if not case_id or self._write_pending:
            return
        if (selected or {}).get("is_external_archive"):
            CustomMessageBox.information(self, "Только просмотр", "Запись прошлых периодов доступна только для просмотра.")
            return
        reply = CustomMessageBox.question(
            self,
            "Возврат из архива",
            "Вернуть пациента на операционный стол?",
            CustomMessageBox.Yes | CustomMessageBox.No,
            CustomMessageBox.No,
        )
        if reply != CustomMessageBox.Yes:
            return
        self._write_pending = True
        self._update_operblock_archive_buttons()

        def operation():
            return self.operblock_service.restore_archived_operation_case(case_id)

        self._enqueue_write(
            f"operblock_restore_archive_case:{case_id}",
            operation,
            on_success=lambda result: self._on_restore_archive_case_success(result),
            on_error=lambda exc: self._on_restore_archive_case_error(exc),
        )

    def _on_restore_archive_case_success(self, result):
        self._write_pending = False
        self.refresh_board(force=True)
        self.refresh_operblock_archive(force=True)
        case_id = _safe_int((result or {}).get("operation_case_id"))
        if case_id:
            self._open_protocol(case_id)

    def _on_restore_archive_case_error(self, exc: Exception):
        self._write_pending = False
        CustomMessageBox.warning(self, "Возврат из архива", str(exc))
        self.refresh_operblock_archive(force=True)

    def _delete_selected_archive_case(self):
        selected = self._selected_archive_case()
        case_id = _safe_int((selected or {}).get("operation_case_id"))
        if not case_id or self._write_pending:
            return
        if (selected or {}).get("is_external_archive"):
            CustomMessageBox.information(self, "Только просмотр", "Запись прошлых периодов доступна только для просмотра.")
            return
        patient_name = str((selected or {}).get("full_name") or "выбранного пациента")
        reply = CustomMessageBox.question(
            self,
            "Удаление из архива",
            f"Действительно удалить анестезиологическую карту пациента {patient_name}?\n"
            "Если случай ещё открыт, он будет закрыт, а операционный стол освобождён.",
            CustomMessageBox.Yes | CustomMessageBox.No,
            CustomMessageBox.No,
        )
        if reply != CustomMessageBox.Yes:
            return
        self._write_pending = True
        self._update_operblock_archive_buttons()

        def operation():
            return self.operblock_service.delete_archived_operation_case(case_id)

        self._enqueue_write(
            f"operblock_delete_archive_case:{case_id}",
            operation,
            on_success=lambda _result: self._on_delete_archive_case_success(),
            on_error=lambda exc: self._on_delete_archive_case_error(exc),
        )

    def _delete_all_archive_cases(self):
        if self._write_pending:
            return
        count = len(getattr(self, "_archive_cases", []) or [])
        if count <= 0:
            return
        archive_scope = f"«{self._table_filter_name}»" if self._table_filter_name else "операционной"
        reply = CustomMessageBox.question(
            self,
            "Удаление архива",
            f"Действительно удалить пациентов из архива {archive_scope} в текущей БД? "
            "Записи прошлых циклов после ротации останутся доступными только для просмотра.",
            CustomMessageBox.Yes | CustomMessageBox.No,
            CustomMessageBox.No,
        )
        if reply != CustomMessageBox.Yes:
            return
        self._write_pending = True
        self._update_operblock_archive_buttons()

        self._enqueue_write(
            "operblock_delete_all_archive_cases",
            lambda: self.operblock_service.delete_all_archived_operation_cases(table_code=self._table_filter_code),
            on_success=lambda _result: self._on_delete_archive_case_success(),
            on_error=lambda exc: self._on_delete_archive_case_error(exc),
        )

    def _on_delete_archive_case_success(self):
        self._write_pending = False
        self._archive_cases_hash = ""
        self.refresh_board(force=True)
        self.refresh_operblock_archive(force=True)

    def _on_delete_archive_case_error(self, exc: Exception):
        self._write_pending = False
        CustomMessageBox.warning(self, "Удаление из архива", str(exc))
        self.refresh_operblock_archive(force=True)
