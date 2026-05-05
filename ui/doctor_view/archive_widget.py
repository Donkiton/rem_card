import os

from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QFrame,
                             QPushButton, QLabel, QDateEdit, QTableWidget, QTableWidgetItem, QHeaderView, QLineEdit)
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

class ArchiveWidget(QWidget):
    patient_selected = Signal(object) # передает PatientDTO
    delete_requested = Signal(object) # передает PatientDTO
    back_requested = Signal()

    def __init__(self, patient_service, remcard_service=None, parent=None):
        super().__init__(parent)
        self.patient_service = patient_service
        self.remcard_service = remcard_service
        self.all_archived_patients = []
        self.filtered_patients = []
        self.page_size = 50
        self.current_page = 1
        self.total_pages = 1
        self._load_worker = None
        self._load_pending = False
        self._delete_pending = False
        self.init_ui()

    def init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(3, 3, 3, 3)

        self.frame = QFrame()
        self.frame.setObjectName("archiveMainFrame")
        self.frame.setStyleSheet(STYLE_ARCHIVE_FRAME)
        layout = QVBoxLayout(self.frame)
        
        header_layout = QHBoxLayout()
        title = QLabel("Архив пациентов")
        title.setProperty("heading", "true")
        title.setStyleSheet(STYLE_ARCHIVE_TITLE)
        
        header_layout.addWidget(title, alignment=Qt.AlignCenter)
        layout.addLayout(header_layout)
        
        # Фильтры
        filter_layout = QGridLayout()
        
        self.date_from = QDateEdit()
        self.date_from.setCalendarPopup(True)
        self.date_from.setDate(QDate(2020, 1, 1))
        self.date_from.dateChanged.connect(self.filter_data)
        
        self.date_to = QDateEdit()
        self.date_to.setCalendarPopup(True)
        self.date_to.setDate(QDate.currentDate())
        self.date_to.dateChanged.connect(self.filter_data)
        
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
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["ФИО", "ИБ №", "Диагноз", "Поступил", "Выписан"])
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

        self.btn_open = QPushButton(" Открыть карту")
        self.btn_open.setStyleSheet(STYLE_NEUTRAL_BUTTON)
        self.btn_open.setFixedHeight(35)
        self.btn_open.setEnabled(False)
        self.btn_open.clicked.connect(self.on_open_clicked)

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
        buttons_layout.addWidget(self.btn_delete_last)
        buttons_layout.addWidget(self.btn_delete)
        
        layout.addLayout(buttons_layout)
        main_layout.addWidget(self.frame)

    def load_data(self):
        if self._load_worker and self._load_worker.isRunning():
            self._load_pending = True
            return

        self._load_pending = False
        worker = AsyncCallThread(self.patient_service.get_archived_patients, parent=self)
        self._load_worker = worker
        worker.succeeded.connect(self._apply_loaded_patients)
        worker.failed.connect(self._on_load_failed)
        worker.finished.connect(lambda: self._on_load_finished(worker))
        worker.start()

    def _apply_loaded_patients(self, patients):
        self.all_archived_patients = list(patients or [])
        self.current_page = 1
        self.filter_data()

    def _on_load_failed(self, exc: Exception):
        pass

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
            adm_dt = p.admission_datetime
            if adm_dt:
                adm_date = adm_dt.date()
                if not (start_date <= adm_date <= end_date):
                    continue
            
            # Проверка строк
            if name_filter and name_filter not in p.get_display_name().lower():
                continue
            if ib_filter and ib_filter not in (p.history_number or "").lower():
                continue
            diag = p.diagnosis_text or ""
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
        patient = self._patient_from_row(item.row())
        if not patient:
            return
        self.patient_selected.emit(patient)

    def on_open_clicked(self):
        row = self.table.currentRow()
        if row >= 0:
            patient = self._patient_from_row(row)
            if not patient:
                return
            self.patient_selected.emit(patient)

    def on_report_stats_clicked(self):
        try:
            if self.date_from.date() > self.date_to.date():
                CustomMessageBox.warning(self, "Внимание", "Дата начала периода не может быть позже даты окончания.")
                return

            db_paths = self._collect_db_paths_for_current_filter()
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
            if self.date_from.date() > self.date_to.date():
                CustomMessageBox.warning(self, "Внимание", "Дата начала периода не может быть позже даты окончания.")
                return

            db_paths = self._collect_db_paths_for_current_filter()
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
        self.table.clearSelection()
        self.table.setRowCount(0)

        start = (self.current_page - 1) * self.page_size
        end = start + self.page_size
        page_items = self.filtered_patients[start:end]

        self.table.setRowCount(len(page_items))
        selected_patient = None

        for row, patient in enumerate(page_items):
            diagnosis = patient.diagnosis_text if patient.diagnosis_text else "без диагноза"
            admitted_at = patient.admission_datetime.strftime("%d.%m.%Y %H:%M") if patient.admission_datetime else "?"
            transferred_at = patient.transfer_datetime.strftime("%d.%m.%Y %H:%M") if patient.transfer_datetime else "не указано"

            item_name = QTableWidgetItem(patient.get_display_name())
            item_name.setData(Qt.UserRole, patient)
            item_ib = QTableWidgetItem(patient.history_number or "")
            item_diag = QTableWidgetItem(diagnosis)
            item_adm = QTableWidgetItem(admitted_at)
            item_trn = QTableWidgetItem(transferred_at)

            if patient.is_external_archive:
                hint = f"Архивная БД: {patient.source_db_name or 'unknown'}"
                item_name.setToolTip(hint)
                item_ib.setToolTip(hint)
                item_diag.setToolTip(hint)

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
        has_any = bool(self.filtered_patients)
        self.btn_report_stats.setEnabled(has_any)
        self.btn_graphs.setEnabled(has_any)

        if not patient:
            self.btn_open.setEnabled(False)
            self.btn_delete_last.setEnabled(False)
            self.btn_delete.setEnabled(False)
            return

        if getattr(patient, "is_external_archive", False):
            self.btn_open.setEnabled(True)
            self.btn_delete_last.setEnabled(False)
            self.btn_delete.setEnabled(False)
            return

        self.btn_open.setEnabled(True)
        self.btn_delete_last.setEnabled(True)
        self.btn_delete.setEnabled(True)

    def _collect_db_paths_for_current_filter(self) -> list[str]:
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

    def _show_external_archive_info(self, patient):
        db_name = patient.source_db_name or "архивная БД"
        CustomMessageBox.information(
            self,
            "Архивный цикл",
            f"Запись находится в ротационной базе ({db_name}).\n"
            "Открытие карты доступно только в режиме чтения. Удаление из этого окна недоступно.",
        )
