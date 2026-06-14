import os
from datetime import datetime

from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QFrame,
                             QPushButton, QLabel, QDateEdit, QTableWidget, QTableWidgetItem, QHeaderView, QLineEdit,
                             QButtonGroup)
from rem_card.app.logger import logger
from rem_card.ui.shared.async_call import AsyncCallThread
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.styles.theme import (
    STYLE_ARCHIVE_FRAME,
    STYLE_ARCHIVE_PAGE_INFO,
    STYLE_ARCHIVE_TABLE,
    STYLE_ARCHIVE_TITLE,
    STYLE_NEUTRAL_BUTTON,
    STYLE_SMALL_NEUTRAL_BUTTON,
    STYLE_TRANSPARENT_LABEL,
)
from PySide6.QtCore import Qt, QDate, Signal, QTimer
from math import ceil

ARCHIVE_MODE_RAO = "rao"
ARCHIVE_MODE_OPERBLOCK = "operblock"

STYLE_ARCHIVE_MODE_BUTTON = STYLE_SMALL_NEUTRAL_BUTTON + """
    QPushButton:checked {
        background-color: #566573;
        color: white;
        border-color: #34495e;
    }
"""

class ArchiveWidget(QWidget):
    patient_selected = Signal(object) # передает PatientDTO
    operblock_case_selected = Signal(object) # передает dict с operation_case_id
    edit_requested = Signal(object) # передает PatientDTO
    delete_requested = Signal(object) # передает PatientDTO
    back_requested = Signal()

    def __init__(
        self,
        patient_service,
        remcard_service=None,
        parent=None,
        *,
        allow_edit: bool = False,
        operblock_service=None,
    ):
        super().__init__(parent)
        self.patient_service = patient_service
        self.remcard_service = remcard_service
        self.operblock_service = operblock_service
        self.allow_edit = bool(allow_edit)
        self.archive_source_mode = ARCHIVE_MODE_RAO
        self.show_operblock_toggle = bool(self.allow_edit and self.operblock_service is not None)
        self.all_archived_patients = []
        self.filtered_patients = []
        self.page_size = 50
        self.current_page = 1
        self.total_pages = 1
        self._load_worker = None
        self._load_pending = False
        self._load_token = 0
        self._delete_pending = False
        self._period_db_paths = []
        self.init_ui()

    def init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(3, 3, 3, 3)

        self.frame = QFrame()
        self.frame.setObjectName("archiveMainFrame")
        self.frame.setStyleSheet(STYLE_ARCHIVE_FRAME)
        layout = QVBoxLayout(self.frame)
        
        header_layout = QHBoxLayout()
        self.archive_title = QLabel("Архив пациентов РАО")
        self.archive_title.setProperty("heading", "true")
        self.archive_title.setStyleSheet(STYLE_ARCHIVE_TITLE)
        
        header_layout.addWidget(self.archive_title, alignment=Qt.AlignCenter)
        layout.addLayout(header_layout)
        
        # Фильтры
        filter_layout = QGridLayout()
        
        self.date_from = QDateEdit()
        self.date_from.setCalendarPopup(True)
        self.date_from.setDate(QDate(2020, 1, 1))
        self.date_from.dateChanged.connect(self.load_data)
        
        self.date_to = QDateEdit()
        self.date_to.setCalendarPopup(True)
        self.date_to.setDate(QDate.currentDate())
        self.date_to.dateChanged.connect(self.load_data)
        
        self.search_ib = QLineEdit()
        self.search_ib.setPlaceholderText("Номер ИБ")
        self.search_ib.setMinimumWidth(100)
        self.search_ib.setMaximumWidth(120)
        self.search_ib.textChanged.connect(self.filter_data)
        
        self.search_name = QLineEdit()
        self.search_name.setPlaceholderText("ФИО")
        self.search_name.textChanged.connect(self.filter_data)
        
        self.search_diag = QLineEdit()
        self.search_diag.setPlaceholderText("Диагноз")
        self.search_diag.textChanged.connect(self.filter_data)
        
        lbl_from = QLabel("С:")
        lbl_from.setStyleSheet(STYLE_TRANSPARENT_LABEL)
        filter_layout.addWidget(lbl_from, 0, 0)
        filter_layout.addWidget(self.date_from, 0, 1)
        lbl_to = QLabel("По:")
        lbl_to.setStyleSheet(STYLE_TRANSPARENT_LABEL)
        filter_layout.addWidget(lbl_to, 0, 2)
        filter_layout.addWidget(self.date_to, 0, 3)
        
        filter_layout.addWidget(self.search_ib, 1, 0, 1, 1)
        filter_layout.addWidget(self.search_name, 1, 1, 1, 2)
        filter_layout.addWidget(self.search_diag, 1, 3)
        
        # Настраиваем растяжение колонок: колонка ФИО (1) будет в 3 раза шире колонки Диагноза (3)
        filter_layout.setColumnStretch(1, 3)
        filter_layout.setColumnStretch(3, 1)
        
        layout.addLayout(filter_layout)
        
        # Таблица архива
        self.table = QTableWidget()
        self.table.setStyleSheet(STYLE_ARCHIVE_TABLE)
        self._apply_table_headers()
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSortingEnabled(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        
        self.table.itemClicked.connect(self.on_item_clicked)
        self.table.itemDoubleClicked.connect(self.on_item_double_clicked)
        
        layout.addWidget(self.table)

        # Пагинация архива
        self.pagination_bar = QHBoxLayout()
        self.pagination_bar.setContentsMargins(0, 0, 0, 0)
        self.pagination_bar.setSpacing(6)

        self.btn_prev_page = QPushButton("◀")
        self.btn_prev_page.setStyleSheet(STYLE_SMALL_NEUTRAL_BUTTON)
        self.btn_prev_page.clicked.connect(lambda: self._set_page(self.current_page - 1))
        self.pagination_bar.addWidget(self.btn_prev_page)

        self.page_buttons_layout = QHBoxLayout()
        self.page_buttons_layout.setContentsMargins(0, 0, 0, 0)
        self.page_buttons_layout.setSpacing(4)
        self.pagination_bar.addLayout(self.page_buttons_layout)

        self.btn_next_page = QPushButton("▶")
        self.btn_next_page.setStyleSheet(STYLE_SMALL_NEUTRAL_BUTTON)
        self.btn_next_page.clicked.connect(lambda: self._set_page(self.current_page + 1))
        self.pagination_bar.addWidget(self.btn_next_page)

        self.page_info = QLabel("Страница 1 из 1")
        self.page_info.setStyleSheet(STYLE_ARCHIVE_PAGE_INFO)
        self.pagination_bar.addWidget(self.page_info)

        self.pagination_bar.addStretch()

        self.page_jump_input = QLineEdit()
        self.page_jump_input.setPlaceholderText("№")
        self.page_jump_input.setMaximumWidth(52)
        self.page_jump_input.returnPressed.connect(self._jump_to_page_from_input)
        self.pagination_bar.addWidget(self.page_jump_input)

        self.btn_page_jump = QPushButton("Перейти")
        self.btn_page_jump.setStyleSheet(STYLE_SMALL_NEUTRAL_BUTTON)
        self.btn_page_jump.clicked.connect(self._jump_to_page_from_input)
        self.pagination_bar.addWidget(self.btn_page_jump)

        layout.addLayout(self.pagination_bar)
        
        # Кнопки управления
        buttons_layout = QHBoxLayout()
        buttons_layout.addStretch()

        if self.show_operblock_toggle:
            self.archive_mode_group = QButtonGroup(self)
            self.archive_mode_group.setExclusive(True)

            self.btn_mode_operblock = QPushButton("Оперблок")
            self.btn_mode_operblock.setCheckable(True)
            self.btn_mode_operblock.setStyleSheet(STYLE_ARCHIVE_MODE_BUTTON)
            self.btn_mode_operblock.setFixedHeight(35)
            self.btn_mode_operblock.toggled.connect(
                lambda checked: checked and self.set_archive_source_mode(ARCHIVE_MODE_OPERBLOCK)
            )

            self.btn_mode_rao = QPushButton("РАО")
            self.btn_mode_rao.setCheckable(True)
            self.btn_mode_rao.setChecked(True)
            self.btn_mode_rao.setStyleSheet(STYLE_ARCHIVE_MODE_BUTTON)
            self.btn_mode_rao.setFixedHeight(35)
            self.btn_mode_rao.toggled.connect(
                lambda checked: checked and self.set_archive_source_mode(ARCHIVE_MODE_RAO)
            )

            self.archive_mode_group.addButton(self.btn_mode_operblock)
            self.archive_mode_group.addButton(self.btn_mode_rao)
            buttons_layout.addWidget(self.btn_mode_operblock)
            buttons_layout.addWidget(self.btn_mode_rao)

        self.btn_open = QPushButton(" Открыть карту")
        self.btn_open.setStyleSheet(STYLE_NEUTRAL_BUTTON)
        self.btn_open.setFixedHeight(35)
        self.btn_open.setEnabled(False)
        self.btn_open.clicked.connect(self.on_open_clicked)

        self.btn_edit = QPushButton(" Редактировать")
        self.btn_edit.setStyleSheet(STYLE_NEUTRAL_BUTTON)
        self.btn_edit.setFixedHeight(35)
        self.btn_edit.setEnabled(False)
        self.btn_edit.setVisible(self.allow_edit)
        self.btn_edit.clicked.connect(self.on_edit_clicked)

        self.btn_report_stats = QPushButton(" Статистика")
        self.btn_report_stats.setStyleSheet(STYLE_NEUTRAL_BUTTON)
        self.btn_report_stats.setFixedHeight(35)
        self.btn_report_stats.setEnabled(False)
        self.btn_report_stats.clicked.connect(self.on_report_stats_clicked)

        self.btn_graphs = QPushButton(" Сформировать графики")
        self.btn_graphs.setStyleSheet(STYLE_NEUTRAL_BUTTON)
        self.btn_graphs.setFixedHeight(35)
        self.btn_graphs.setEnabled(True)
        self.btn_graphs.clicked.connect(self.on_graphs_clicked)

        self.btn_delete_last = QPushButton(" Удалить последнюю карту")
        self.btn_delete_last.setStyleSheet(STYLE_NEUTRAL_BUTTON)
        self.btn_delete_last.setFixedHeight(35)
        self.btn_delete_last.setEnabled(False)
        self.btn_delete_last.clicked.connect(self.on_delete_last_clicked)

        self.btn_delete = QPushButton(" Удалить все карты")
        self.btn_delete.setStyleSheet(STYLE_NEUTRAL_BUTTON)
        self.btn_delete.setFixedHeight(35)
        self.btn_delete.setEnabled(False)
        self.btn_delete.clicked.connect(self.on_delete_clicked)

        buttons_layout.addWidget(self.btn_report_stats)
        buttons_layout.addWidget(self.btn_graphs)
        buttons_layout.addWidget(self.btn_open)
        buttons_layout.addWidget(self.btn_edit)
        buttons_layout.addWidget(self.btn_delete_last)
        buttons_layout.addWidget(self.btn_delete)
        
        layout.addLayout(buttons_layout)
        main_layout.addWidget(self.frame)

    def load_data(self):
        if self._load_worker and self._load_worker.isRunning():
            self._load_pending = True
            self._load_token += 1
            return

        self._load_pending = False
        start_dt, end_dt = self._get_archive_period_bounds()
        mode = self.archive_source_mode
        self._load_token += 1
        load_token = self._load_token
        if mode == ARCHIVE_MODE_OPERBLOCK:
            loader = lambda: self._load_operblock_archive_cases(start_dt, end_dt)
        else:
            loader = lambda: self.patient_service.get_archived_patients(start_dt=start_dt, end_dt=end_dt)
        worker = AsyncCallThread(
            loader,
            parent=self,
        )
        self._load_worker = worker
        worker.succeeded.connect(lambda rows, token=load_token, source_mode=mode: self._apply_loaded_records(token, source_mode, rows))
        worker.failed.connect(lambda exc, token=load_token, source_mode=mode: self._on_load_failed(exc, token, source_mode))
        worker.finished.connect(lambda: self._on_load_finished(worker))
        worker.start()

    def _apply_loaded_records(self, load_token: int, source_mode: str, records):
        if load_token != self._load_token or source_mode != self.archive_source_mode:
            return
        self.all_archived_patients = list(records or [])
        self._period_db_paths = (
            []
            if source_mode == ARCHIVE_MODE_OPERBLOCK
            else self._collect_db_paths_for_archive_period()
        )
        self.current_page = 1
        self.filter_data()

    def _on_load_failed(self, exc: Exception, load_token: int, source_mode: str):
        if load_token != self._load_token or source_mode != self.archive_source_mode:
            return
        if source_mode == ARCHIVE_MODE_OPERBLOCK:
            logger.warning("Не удалось загрузить архив оперблока: %s", exc, exc_info=True)
            CustomMessageBox.warning(self, "Архив оперблока", f"Не удалось загрузить архив оперблока:\n{exc}")
        else:
            logger.warning("Не удалось загрузить архив пациентов: %s", exc, exc_info=True)

    def _on_load_finished(self, worker):
        if self._load_worker is worker:
            self._load_worker = None
        if self._load_pending:
            QTimer.singleShot(0, self.load_data)

    def filter_data(self):
        # Запоминаем текущий выбранный ключ записи
        selected_key = None
        current_row = self.table.currentRow()
        if current_row >= 0:
            patient = self._patient_from_row(current_row)
            if patient:
                selected_key = self._patient_key(patient)
        
        start_date = self.date_from.date().toPython()
        end_date = self.date_to.date().toPython()
        name_filter = self.search_name.text().lower()
        ib_filter = self.search_ib.text().lower()
        diag_filter = self.search_diag.text().lower()
        
        filtered = []
        for p in self.all_archived_patients:
            # Проверка дат поступления
            adm_dt = self._record_admission_datetime(p)
            if adm_dt:
                adm_date = adm_dt.date()
                if not (start_date <= adm_date <= end_date):
                    continue
            
            # Проверка строк
            if name_filter and name_filter not in self._record_display_name(p).lower():
                continue
            if ib_filter and ib_filter not in self._record_history_number(p).lower():
                continue
            diag = self._record_diagnosis_text(p)
            if diag_filter and diag_filter not in diag.lower():
                continue
                
            filtered.append(p)
        self.filtered_patients = filtered
        self.total_pages = max(1, int(ceil(len(self.filtered_patients) / self.page_size))) if self.filtered_patients else 1

        if selected_key:
            for idx, patient in enumerate(self.filtered_patients):
                if self._patient_key(patient) == selected_key:
                    self.current_page = (idx // self.page_size) + 1
                    break
            else:
                self.current_page = 1
        else:
            self.current_page = 1

        self._set_page(self.current_page, selected_key=selected_key)

    def on_item_clicked(self, item):
        patient = self._patient_from_row(item.row())
        self._apply_action_buttons_state(patient)

    def on_item_double_clicked(self, item):
        if self.archive_source_mode == ARCHIVE_MODE_OPERBLOCK:
            case = self._patient_from_row(item.row())
            if isinstance(case, dict):
                self.operblock_case_selected.emit(dict(case))
            return
        patient = self._patient_from_row(item.row())
        if not patient:
            return
        self.patient_selected.emit(patient)

    def on_open_clicked(self):
        if self.archive_source_mode == ARCHIVE_MODE_OPERBLOCK:
            return
        row = self.table.currentRow()
        if row >= 0:
            patient = self._patient_from_row(row)
            if not patient:
                return
            self.patient_selected.emit(patient)

    def on_edit_clicked(self):
        if self.archive_source_mode == ARCHIVE_MODE_OPERBLOCK:
            return
        if not self.allow_edit:
            return
        row = self.table.currentRow()
        if row >= 0:
            patient = self._patient_from_row(row)
            if not patient:
                return
            if getattr(patient, "is_external_archive", False):
                self._show_external_archive_info(patient)
                return
            self.edit_requested.emit(patient)

    def on_report_stats_clicked(self):
        try:
            if self.archive_source_mode == ARCHIVE_MODE_OPERBLOCK:
                if self.date_from.date() > self.date_to.date():
                    CustomMessageBox.warning(self, "Внимание", "Дата начала периода не может быть позже даты окончания.")
                    return
                if self.operblock_service is None:
                    CustomMessageBox.warning(self, "Статистика", "Сервис оперблока недоступен.")
                    return

                from ..shared.analytics_integration import open_operblock_statistics_dialog

                start_dt, end_dt = self._get_archive_period_bounds()
                open_operblock_statistics_dialog(
                    self,
                    db_manager=self.operblock_service.db,
                    start_dt=start_dt,
                    end_dt=end_dt,
                )
                return
            if self.date_from.date() > self.date_to.date():
                CustomMessageBox.warning(self, "Внимание", "Дата начала периода не может быть позже даты окончания.")
                return

            db_paths = self._collect_db_paths_for_archive_period()
            if not db_paths:
                CustomMessageBox.information(self, "Инфо", "Нет данных в выбранном архивном интервале.")
                return

            from ..shared.analytics_integration import open_analytics_report_dialog

            start_dt, end_dt = self._get_archive_period_bounds()
            open_analytics_report_dialog(
                self,
                remcard_service=self.remcard_service,
                start_dt=start_dt,
                end_dt=end_dt,
                show_graph_button=False,
                db_paths=db_paths,
            )
        except Exception as exc:
            CustomMessageBox.warning(self, "Ошибка", f"Не удалось открыть статистику:\n{exc}")

    def _get_archive_period_bounds(self) -> tuple[str, str]:
        start_dt = self.date_from.date().toString("yyyy-MM-dd 00:00:00")
        end_dt = self.date_to.date().toString("yyyy-MM-dd 23:59:59")
        return start_dt, end_dt

    def on_graphs_clicked(self):
        try:
            if self.archive_source_mode == ARCHIVE_MODE_OPERBLOCK:
                CustomMessageBox.information(self, "Графики", "Графики доступны для архива РАО.")
                return
            if self.date_from.date() > self.date_to.date():
                CustomMessageBox.warning(self, "Внимание", "Дата начала периода не может быть позже даты окончания.")
                return

            db_paths = self._collect_db_paths_for_archive_period()
            if not db_paths:
                CustomMessageBox.information(self, "Инфо", "Нет данных в выбранном архивном интервале.")
                return

            from ..shared.analytics_integration import open_analytics_graphs_dialog

            start_dt, end_dt = self._get_archive_period_bounds()
            open_analytics_graphs_dialog(
                self,
                remcard_service=self.remcard_service,
                start_dt=start_dt,
                end_dt=end_dt,
                db_paths=db_paths,
            )
        except Exception as exc:
            CustomMessageBox.warning(self, "Ошибка", f"Не удалось открыть окно графиков:\n{exc}")

    def on_delete_last_clicked(self):
        if self.archive_source_mode == ARCHIVE_MODE_OPERBLOCK:
            self._delete_selected_operblock_archive_case()
            return
        if self._delete_pending:
            return
        row = self.table.currentRow()
        if row >= 0:
            patient = self._patient_from_row(row)
            if patient:
                if patient.is_external_archive:
                    self._show_external_archive_info(patient)
                    return
                reply = CustomMessageBox.question(
                    self, 
                    "Подтверждение", 
                    f"Вы действительно хотите безвозвратно удалить последнюю сохраненную карту пациента {patient.get_display_name()}?"
                )
                if reply == CustomMessageBox.Yes:
                    if not hasattr(self, 'remcard_service') or not self.remcard_service:
                        CustomMessageBox.warning(self, "Ошибка", "Сервис карт недоступен.")
                        return
                    patient_id = patient.id

                    def operation():
                        return self.remcard_service.delete_last_card(patient_id)

                    def on_success(result):
                        ok, _last_date, msg = result
                        self._finish_delete_pending()
                        if not ok:
                            self.load_data()
                            CustomMessageBox.warning(self, "Внимание", msg)
                            return
                        self.load_data()
                        self.delete_requested.emit(patient)

                    self._enqueue_delete_write(
                        self.remcard_service,
                        f"archive_delete_last_card:{patient_id}",
                        operation,
                        on_success=on_success,
                        error_message="Не удалось удалить карту",
                    )

    def on_delete_clicked(self):
        if self.archive_source_mode == ARCHIVE_MODE_OPERBLOCK:
            self._delete_all_operblock_archive_cases()
            return
        if self._delete_pending:
            return
        row = self.table.currentRow()
        if row >= 0:
            patient = self._patient_from_row(row)
            if patient:
                if patient.is_external_archive:
                    self._show_external_archive_info(patient)
                    return
                reply = CustomMessageBox.question(
                    self, 
                    "Подтверждение", 
                    f"Вы действительно хотите безвозвратно удалить все карты пациента {patient.get_display_name()}?"
                )
                if reply == CustomMessageBox.Yes:
                    patient_id = patient.id

                    def operation():
                        return self.patient_service.delete_admission(patient_id)

                    def on_success(_result):
                        self._finish_delete_pending()
                        self.load_data()
                        self.delete_requested.emit(patient)

                    self._enqueue_delete_write(
                        self.patient_service,
                        f"archive_delete_admission:{patient_id}",
                        operation,
                        on_success=on_success,
                        error_message="Не удалось удалить записи",
                    )

    def _enqueue_delete_write(self, service, description: str, operation, *, on_success, error_message: str):
        def on_error(exc):
            self._finish_delete_pending()
            self.load_data()
            CustomMessageBox.warning(self, "Ошибка", f"{error_message}: {exc}")

        self._begin_delete_pending()
        try:
            if hasattr(service, "enqueue_write"):
                service.enqueue_write(
                    description,
                    operation,
                    on_success=on_success,
                    on_error=on_error,
                )
                return
            result = operation()
        except Exception as exc:
            on_error(exc)
            return
        on_success(result)

    def _begin_delete_pending(self):
        self._delete_pending = True
        self._set_delete_controls_enabled(False)

    def _finish_delete_pending(self):
        self._delete_pending = False
        self._set_delete_controls_enabled(True)

    def _set_delete_controls_enabled(self, enabled: bool):
        self.table.setEnabled(enabled)
        for widget in (
            self.btn_open,
            self.btn_edit,
            self.btn_report_stats,
            self.btn_graphs,
            self.btn_delete_last,
            self.btn_delete,
            self.btn_prev_page,
            self.btn_next_page,
            self.btn_page_jump,
            self.page_jump_input,
            self.search_ib,
            self.search_name,
            self.search_diag,
            self.date_from,
            self.date_to,
        ):
            widget.setEnabled(enabled)
        if hasattr(self, "btn_mode_operblock"):
            self.btn_mode_operblock.setEnabled(enabled)
        if hasattr(self, "btn_mode_rao"):
            self.btn_mode_rao.setEnabled(enabled)
        self._set_layout_widgets_enabled(self.page_buttons_layout, enabled)
        if enabled:
            patient = self._patient_from_row(self.table.currentRow()) if self.table.currentRow() >= 0 else None
            self._apply_action_buttons_state(patient)
            self._refresh_pagination_ui()

    def _set_layout_widgets_enabled(self, layout, enabled: bool):
        for idx in range(layout.count()):
            item = layout.itemAt(idx)
            widget = item.widget()
            if widget is not None:
                widget.setEnabled(enabled)

    def _patient_key(self, patient) -> str:
        if isinstance(patient, dict):
            return f"operblock::{patient.get('operation_case_id') or patient.get('admission_id') or ''}"
        source_db = patient.source_db_path or "current"
        source_admission_id = (
            patient.source_admission_id
            if patient.source_admission_id is not None
            else patient.id
        )
        return f"{source_db}::{source_admission_id}"

    def _patient_from_row(self, row: int):
        item = self.table.item(row, 0)
        if not item:
            return None
        return item.data(Qt.UserRole)

    def _set_page(self, page: int, selected_key: str = None):
        if page < 1:
            page = 1
        if page > self.total_pages:
            page = self.total_pages
        self.current_page = page
        self._render_current_page(selected_key=selected_key)

    def _render_current_page(self, selected_key: str = None):
        self._apply_table_headers()
        self.table.clearSelection()
        self.table.setRowCount(0)

        start = (self.current_page - 1) * self.page_size
        end = start + self.page_size
        page_items = self.filtered_patients[start:end]

        self.table.setRowCount(len(page_items))
        selected_patient = None

        for row, patient in enumerate(page_items):
            if self.archive_source_mode == ARCHIVE_MODE_OPERBLOCK:
                self._render_operblock_case_row(row, patient, selected_key)
                if selected_key and self._patient_key(patient) == selected_key:
                    selected_patient = patient
                continue

            diagnosis = patient.diagnosis_text if patient.diagnosis_text else "без диагноза"
            admitted_at = patient.admission_datetime.strftime("%d.%m.%Y %H:%M") if patient.admission_datetime else "?"
            transferred_at = patient.transfer_datetime.strftime("%d.%m.%Y %H:%M") if patient.transfer_datetime else "не указано"

            item_name = QTableWidgetItem(patient.get_display_name())
            item_name.setData(Qt.UserRole, patient)
            item_ib = QTableWidgetItem(patient.history_number or "")
            item_diag = QTableWidgetItem(diagnosis)
            item_adm = QTableWidgetItem(admitted_at)
            item_trn = QTableWidgetItem(transferred_at)

            self.table.setItem(row, 0, item_name)
            self.table.setItem(row, 1, item_ib)
            self.table.setItem(row, 2, item_diag)
            self.table.setItem(row, 3, item_adm)
            self.table.setItem(row, 4, item_trn)

            if selected_key and self._patient_key(patient) == selected_key:
                self.table.selectRow(row)
                selected_patient = patient

        if selected_patient is None and self.table.currentRow() >= 0:
            selected_patient = self._patient_from_row(self.table.currentRow())
        self._apply_action_buttons_state(selected_patient)
        self._refresh_pagination_ui()

    def _refresh_pagination_ui(self):
        while self.page_buttons_layout.count():
            item = self.page_buttons_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        max_visible = 7
        start_page = max(1, self.current_page - 3)
        end_page = min(self.total_pages, start_page + max_visible - 1)
        start_page = max(1, end_page - max_visible + 1)

        for page in range(start_page, end_page + 1):
            btn = QPushButton(str(page))
            btn.setCheckable(True)
            btn.setChecked(page == self.current_page)
            btn.setFixedHeight(26)
            btn.setMinimumWidth(30)
            btn.clicked.connect(lambda _checked=False, p=page: self._set_page(p))
            self.page_buttons_layout.addWidget(btn)

        self.btn_prev_page.setEnabled(self.current_page > 1)
        self.btn_next_page.setEnabled(self.current_page < self.total_pages)
        self.page_info.setText(f"Страница {self.current_page} из {self.total_pages}")
        if self._delete_pending:
            self._set_delete_controls_enabled(False)

    def _jump_to_page_from_input(self):
        raw = self.page_jump_input.text().strip()
        if not raw:
            return
        if not raw.isdigit():
            CustomMessageBox.warning(self, "Пагинация", "Введите номер страницы цифрами.")
            return
        self._set_page(int(raw))

    def _apply_action_buttons_state(self, patient):
        if self.archive_source_mode == ARCHIVE_MODE_OPERBLOCK:
            has_selected_case = isinstance(patient, dict) and bool(patient.get("operation_case_id"))
            selected_closed = has_selected_case and str(patient.get("status") or "").strip().lower() == "closed"
            self.btn_report_stats.setEnabled(self.operblock_service is not None and not self._delete_pending)
            self.btn_graphs.setEnabled(False)
            self.btn_open.setEnabled(False)
            self.btn_edit.setEnabled(False)
            self.btn_delete_last.setEnabled(selected_closed and not self._delete_pending)
            self.btn_delete.setEnabled(self.operblock_service is not None and not self._delete_pending)
            return

        has_period_data = bool(self._period_db_paths) or bool(self.filtered_patients)
        self.btn_report_stats.setEnabled(has_period_data)
        self.btn_graphs.setEnabled(has_period_data)

        if not patient:
            self.btn_open.setEnabled(False)
            self.btn_edit.setEnabled(False)
            self.btn_delete_last.setEnabled(False)
            self.btn_delete.setEnabled(False)
            return

        if getattr(patient, "is_external_archive", False):
            self.btn_open.setEnabled(True)
            self.btn_edit.setEnabled(False)
            self.btn_delete_last.setEnabled(False)
            self.btn_delete.setEnabled(False)
            return

        self.btn_open.setEnabled(True)
        self.btn_edit.setEnabled(self.allow_edit)
        self.btn_delete_last.setEnabled(True)
        self.btn_delete.setEnabled(True)

    def _collect_db_paths_for_current_filter(self) -> list[str]:
        if self.archive_source_mode == ARCHIVE_MODE_OPERBLOCK:
            return []
        rows = self.filtered_patients
        if not rows:
            return []
        paths = []
        seen = set()
        for patient in rows:
            path = str(getattr(patient, "source_db_path", "") or "").strip()
            if not path:
                continue
            abs_path = os.path.abspath(path)
            if not os.path.isfile(abs_path):
                continue
            key = os.path.normcase(abs_path)
            if key in seen:
                continue
            seen.add(key)
            paths.append(abs_path)
        return paths

    def _collect_db_paths_for_archive_period(self) -> list[str]:
        if self.archive_source_mode == ARCHIVE_MODE_OPERBLOCK:
            return []
        start_dt, end_dt = self._get_archive_period_bounds()
        try:
            if hasattr(self.patient_service, "get_archive_db_paths_for_period"):
                paths = self.patient_service.get_archive_db_paths_for_period(start_dt, end_dt)
                return [os.path.abspath(path) for path in paths if path and os.path.isfile(path)]
        except Exception:
            pass
        return self._collect_db_paths_for_current_filter()

    def _show_external_archive_info(self, patient):
        _ = patient
        CustomMessageBox.information(
            self,
            "Только просмотр",
            "Запись прошлых периодов доступна только для просмотра.",
        )

    def _delete_selected_operblock_archive_case(self):
        if self._delete_pending:
            return
        case = self._patient_from_row(self.table.currentRow()) if self.table.currentRow() >= 0 else None
        if not isinstance(case, dict):
            return
        case_id = self._safe_int(case.get("operation_case_id"))
        if not case_id:
            return
        patient_name = str(case.get("full_name") or "выбранного пациента")
        reply = CustomMessageBox.question(
            self,
            "Удаление из архива оперблока",
            f"Действительно удалить из архива оперблока пациента {patient_name}?",
            CustomMessageBox.Yes | CustomMessageBox.No,
            CustomMessageBox.No,
        )
        if reply != CustomMessageBox.Yes:
            return

        def operation():
            return self.operblock_service.delete_archived_operation_case(case_id)

        def on_success(_result):
            self._finish_delete_pending()
            self.load_data()

        self._enqueue_operblock_archive_write(
            f"doctor_archive_operblock_delete_case:{case_id}",
            operation,
            on_success=on_success,
            error_message="Не удалось удалить запись архива оперблока",
        )

    def _delete_all_operblock_archive_cases(self):
        if self._delete_pending:
            return
        reply = CustomMessageBox.question(
            self,
            "Очистка архива оперблока",
            "Действительно полностью очистить архив оперблока?\n"
            "Будут удалены все архивные записи оперблока, не только текущая выборка.",
            CustomMessageBox.Yes | CustomMessageBox.No,
            CustomMessageBox.No,
        )
        if reply != CustomMessageBox.Yes:
            return

        def operation():
            return self.operblock_service.delete_all_archived_operation_cases()

        def on_success(_result):
            self._finish_delete_pending()
            self.load_data()

        self._enqueue_operblock_archive_write(
            "doctor_archive_operblock_delete_all_cases",
            operation,
            on_success=on_success,
            error_message="Не удалось очистить архив оперблока",
        )

    def _enqueue_operblock_archive_write(self, description: str, operation, *, on_success, error_message: str):
        if self.operblock_service is None:
            CustomMessageBox.warning(self, "Архив оперблока", "Сервис оперблока недоступен.")
            return

        data_service = getattr(self.remcard_service, "data_service", None)

        def on_error(exc):
            self._finish_delete_pending()
            self.load_data()
            CustomMessageBox.warning(self, "Архив оперблока", f"{error_message}:\n{exc}")

        self._begin_delete_pending()
        try:
            if data_service is not None and hasattr(data_service, "enqueue_write"):
                data_service.enqueue_write(
                    description=description,
                    operation=operation,
                    on_success=on_success,
                    on_error=on_error,
                )
                return
            result = operation()
        except Exception as exc:
            on_error(exc)
            return
        on_success(result)

    def set_archive_source_mode(self, mode: str):
        mode = ARCHIVE_MODE_OPERBLOCK if mode == ARCHIVE_MODE_OPERBLOCK else ARCHIVE_MODE_RAO
        if mode == ARCHIVE_MODE_OPERBLOCK and not self.show_operblock_toggle:
            return
        if mode == self.archive_source_mode:
            self._sync_archive_mode_ui()
            return
        self.archive_source_mode = mode
        self.all_archived_patients = []
        self.filtered_patients = []
        self._period_db_paths = []
        self.current_page = 1
        self.total_pages = 1
        self._sync_archive_mode_ui()
        self._render_current_page()
        self.load_data()

    def _sync_archive_mode_ui(self):
        if hasattr(self, "archive_title"):
            title = "Архив пациентов оперблока" if self.archive_source_mode == ARCHIVE_MODE_OPERBLOCK else "Архив пациентов РАО"
            self.archive_title.setText(title)
        if hasattr(self, "btn_mode_operblock"):
            self.btn_mode_operblock.setChecked(self.archive_source_mode == ARCHIVE_MODE_OPERBLOCK)
        if hasattr(self, "btn_mode_rao"):
            self.btn_mode_rao.setChecked(self.archive_source_mode == ARCHIVE_MODE_RAO)
        self._apply_table_headers()

    def _apply_table_headers(self):
        if not hasattr(self, "table"):
            return
        if self.archive_source_mode == ARCHIVE_MODE_OPERBLOCK:
            self.table.setColumnCount(7)
            self.table.setHorizontalHeaderLabels(["Операционная", "ФИО", "ИБ №", "Диагноз", "Начало", "Переведён", "Статус"])
        else:
            self.table.setColumnCount(5)
            self.table.setHorizontalHeaderLabels(["ФИО", "ИБ №", "Диагноз", "Поступил", "Выписан"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

    def _load_operblock_archive_cases(self, start_dt: str, end_dt: str) -> list[dict]:
        if self.operblock_service is None:
            return []
        start = self._parse_datetime_value(start_dt)
        end = self._parse_datetime_value(end_dt)
        cases = self.operblock_service.list_archived_operation_cases()
        result = []
        for case in cases or []:
            item = dict(case or {})
            case_dt = self._parse_datetime_value(item.get("started_at")) or self._parse_datetime_value(item.get("ended_at"))
            if case_dt and start and case_dt < start:
                continue
            if case_dt and end and case_dt > end:
                continue
            result.append(item)
        return result

    def _render_operblock_case_row(self, row: int, case: dict, selected_key: str = None):
        diagnosis_text = self._record_diagnosis_text(case) or "—"
        status = str(case.get("status") or "").strip().lower()
        values = [
            self._operblock_table_short_name(case.get("table_code") or case.get("table_display_name")),
            self._record_display_name(case),
            self._record_history_number(case),
            diagnosis_text,
            self._format_datetime_text(case.get("started_at")),
            "" if status == "active" else self._format_datetime_text(case.get("ended_at")),
            "В операционной" if status == "active" else "В архиве",
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            if column == 0:
                item.setData(Qt.UserRole, dict(case))
            self.table.setItem(row, column, item)
        if selected_key and self._patient_key(case) == selected_key:
            self.table.selectRow(row)

    def _record_display_name(self, record) -> str:
        if isinstance(record, dict):
            return str(record.get("full_name") or "Неизвестно")
        return record.get_display_name()

    def _record_history_number(self, record) -> str:
        if isinstance(record, dict):
            return str(record.get("history_number") or "")
        return str(record.history_number or "")

    def _record_diagnosis_text(self, record) -> str:
        if isinstance(record, dict):
            diagnosis_text = str(record.get("diagnosis_text") or "")
            diagnosis_code = str(record.get("diagnosis_code") or "").strip()
            return f"{diagnosis_code}: {diagnosis_text}" if diagnosis_code else diagnosis_text
        return record.diagnosis_text or ""

    def _record_admission_datetime(self, record):
        if isinstance(record, dict):
            return self._parse_datetime_value(record.get("started_at")) or self._parse_datetime_value(record.get("ended_at"))
        return record.admission_datetime

    @staticmethod
    def _parse_datetime_value(value):
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return value
        text = str(value).strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text)
        except Exception:
            return None

    def _format_datetime_text(self, value) -> str:
        parsed = self._parse_datetime_value(value)
        return parsed.strftime("%d.%m.%Y %H:%M") if parsed else "—"

    @staticmethod
    def _operblock_table_short_name(value) -> str:
        text = str(value or "").strip().lower()
        if text == "emergency" or "экстр" in text:
            return "Экстренная"
        if text == "planned" or "план" in text:
            return "Плановая"
        return str(value or "—")

    @staticmethod
    def _safe_int(value, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
