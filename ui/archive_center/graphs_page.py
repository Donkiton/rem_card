from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from rem_card.app.paths import REPORT_DIR
from rem_card.services.analytics.graphs_service import build_graphs_html, build_graphs_pdf
from rem_card.ui.analytics.chart_renderer import fit_chart_images_to_width
from rem_card.ui.analytics.graphs_catalog import GRAPH_GROUPS, TOP_GRAPHS
from rem_card.ui.shared.analytics_integration import (
    get_analytics_base_manager,
    resolve_readonly_analytics_manager,
)
from rem_card.ui.shared.archive_date_edit import ArchiveDateEdit
from rem_card.ui.shared.async_call import AsyncCallThread
from rem_card.ui.shared.persistent_file_dialog import PersistentSaveFileDialog
from rem_card.ui.styles.theme import ANALYTICS_CHART_COLORS


class _GraphOption(QWidget):
    def __init__(self, caption: str, parent=None):
        super().__init__(parent)
        self.setObjectName("ArchiveStatisticsOptionRow")
        row = QHBoxLayout(self)
        row.setContentsMargins(1, 2, 1, 2)
        row.setSpacing(7)
        self.checkbox = QCheckBox(self)
        self.checkbox.setObjectName("ArchiveStatisticsCheck")
        row.addWidget(self.checkbox, 0, Qt.AlignTop)
        label = QLabel(caption, self)
        label.setObjectName("ArchiveStatisticsOptionLabel")
        label.setWordWrap(True)
        label.setCursor(Qt.PointingHandCursor)
        label.mousePressEvent = lambda event: self._toggle(event)
        row.addWidget(label, 1)

    def _toggle(self, event):
        self.checkbox.toggle()
        event.accept()


class ArchiveGraphsPage(QWidget):
    """Полноценные графики РАО на базе существующего каталога из 65 показателей."""

    def __init__(self, *, remcard_service=None, archive_page=None, parent=None):
        super().__init__(parent)
        self.remcard_service = remcard_service
        self.archive_page = archive_page
        self.checkboxes: dict[str, QCheckBox] = {}
        self._worker = None
        self._pdf_worker = None
        self._request_token = 0
        self._latest_html = ""
        self._latest_signature = None
        self._pending_pdf_path = ""
        self._temp_graph_paths: set[str] = set()
        self._closing = False
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        toolbar = QFrame(self)
        toolbar.setObjectName("ArchiveStatisticsToolbar")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(12, 9, 12, 9)
        toolbar_layout.setSpacing(8)
        toolbar_layout.addWidget(QLabel("С", toolbar))
        self.date_from = ArchiveDateEdit(QDate(2000, 1, 1), toolbar)
        toolbar_layout.addWidget(self.date_from)
        toolbar_layout.addWidget(QLabel("По", toolbar))
        self.date_to = ArchiveDateEdit(QDate.currentDate(), toolbar)
        toolbar_layout.addWidget(self.date_to)

        self.btn_preview = QPushButton("Показать графики", toolbar)
        self.btn_preview.setObjectName("ArchiveStatisticsRefresh")
        self.btn_preview.clicked.connect(self.build_preview)
        toolbar_layout.addWidget(self.btn_preview)
        self.status = QLabel("", toolbar)
        self.status.setObjectName("ArchiveStatisticsStatus")
        toolbar_layout.addWidget(self.status)
        toolbar_layout.addStretch(1)
        self.btn_save_pdf = QPushButton("Сохранить PDF", toolbar)
        self.btn_save_pdf.setObjectName("ArchiveStatisticsSecondary")
        self.btn_save_pdf.clicked.connect(self.save_pdf)
        toolbar_layout.addWidget(self.btn_save_pdf)
        layout.addWidget(toolbar)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(10)

        selector = QFrame(self)
        selector.setObjectName("ArchiveStatisticsSelector")
        selector.setFixedWidth(330)
        selector_layout = QVBoxLayout(selector)
        selector_layout.setContentsMargins(10, 10, 10, 10)
        selector_layout.setSpacing(8)

        controls = QHBoxLayout()
        controls.setSpacing(5)
        self.btn_all = QPushButton("Все", selector)
        self.btn_top = QPushButton("Основные", selector)
        self.btn_none = QPushButton("Снять", selector)
        for button in (self.btn_all, self.btn_top, self.btn_none):
            button.setObjectName("ArchiveStatisticsOption")
            controls.addWidget(button)
        self.btn_all.clicked.connect(self._select_all)
        self.btn_top.clicked.connect(self._select_top)
        self.btn_none.clicked.connect(self._deselect_all)
        selector_layout.addLayout(controls)

        recovery_option = _GraphOption(
            "Учитывать койки пробуждения в общих графиках",
            selector,
        )
        recovery_option.setObjectName("ArchiveRecoveryOption")
        self.chk_include_recovery = recovery_option.checkbox
        self.chk_include_recovery.setObjectName("ArchiveRecoveryToggle")
        recovery_option.setToolTip(
            "Добавляет пациентов с коек пробуждения в общие расчёты выбранных графиков."
        )
        self.chk_include_recovery.toggled.connect(self._mark_dirty)
        selector_layout.addWidget(recovery_option)

        scroll = QScrollArea(selector)
        scroll.setObjectName("ArchiveStatisticsOptionsScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        options = QWidget(scroll)
        options.setObjectName("ArchiveStatisticsOptions")
        options_layout = QVBoxLayout(options)
        options_layout.setContentsMargins(4, 4, 4, 4)
        options_layout.setSpacing(5)
        for group_name, items in GRAPH_GROUPS.items():
            group_label = QLabel(group_name, options)
            group_label.setObjectName("ArchiveStatisticsGroup")
            group_label.setWordWrap(True)
            options_layout.addWidget(group_label)
            for key, caption in items.items():
                option = _GraphOption(caption, options)
                option.checkbox.toggled.connect(self._mark_dirty)
                options_layout.addWidget(option)
                self.checkboxes[key] = option.checkbox
        options_layout.addStretch(1)
        scroll.setWidget(options)
        selector_layout.addWidget(scroll, 1)
        body.addWidget(selector)

        self.report = QTextBrowser(self)
        self.report.setObjectName("ArchiveStatisticsReport")
        self.report.setOpenExternalLinks(False)
        self.report.setHtml(
            "<div style='padding:28px;color:#6b7785;'>"
            "Выберите графики слева и нажмите «Показать графики»."
            "</div>"
        )
        body.addWidget(self.report, 1)
        layout.addLayout(body, 1)

        self.date_from.dateChanged.connect(self._mark_dirty)
        self.date_to.dateChanged.connect(self._mark_dirty)
        self._select_top()

    def ensure_loaded(self):
        return None

    def _period(self) -> tuple[str, str] | None:
        if self.date_from.date() > self.date_to.date():
            self.status.setText("Начальная дата позже конечной")
            return None
        return self.date_from.date().toString("yyyy-MM-dd"), self.date_to.date().toString("yyyy-MM-dd")

    def _selected_keys(self) -> list[str]:
        return [key for key, checkbox in self.checkboxes.items() if checkbox.isChecked()]

    def _signature(self):
        period = self._period()
        if period is None:
            return None
        return period[0], period[1], tuple(self._selected_keys()), self.chk_include_recovery.isChecked()

    def build_preview(self):
        self._start_graph_build(save_after=False)

    def _start_graph_build(self, *, save_after: bool):
        selected = self._selected_keys()
        if not selected:
            self.status.setText("Выберите хотя бы один график")
            return
        period = self._period()
        if period is None:
            return
        if self._worker is not None and self._worker.isRunning():
            return
        start_date, end_date = period
        include_recovery = self.chk_include_recovery.isChecked()
        self._request_token += 1
        token = self._request_token
        preview_width = self._preview_width()
        busy_text = (
            f"Формирование {len(selected)} графиков (это может занять до минуты)…"
            if len(selected) > 12
            else f"Формирование графиков: {len(selected)}…"
        )
        self._set_busy(True, busy_text)
        worker = AsyncCallThread(
            lambda: self._build_graphs(
                start_date,
                end_date,
                selected,
                include_recovery,
                preview_width,
            ),
            parent=self,
        )
        self._worker = worker
        worker.succeeded.connect(lambda result, request=token: self._graphs_ready(request, result, save_after))
        worker.failed.connect(lambda error, request=token: self._request_failed(request, error))
        worker.finished.connect(lambda: self._worker_finished(worker))
        worker.start()

    def _build_graphs(
        self,
        start_date: str,
        end_date: str,
        selected: list[str],
        include_recovery: bool,
        preview_width: int = 760,
    ):
        base_manager = get_analytics_base_manager(remcard_service=self.remcard_service)
        manager, cleanup = resolve_readonly_analytics_manager(
            base_manager,
            start_dt=start_date,
            end_dt=end_date,
            db_paths=self._archive_db_paths(start_date, end_date),
        )
        try:
            result = build_graphs_html(
                manager,
                start_date,
                end_date,
                selected,
                list(ANALYTICS_CHART_COLORS),
                include_recovery_beds=include_recovery,
            )
            # Масштабирование PNG заметно тяжелее замены HTML. Выполняем его
            # здесь, в том же фоновом worker, чтобы готовность 20–65 графиков
            # не блокировала главный Qt-поток на несколько секунд.
            result.html = fit_chart_images_to_width(
                result.html,
                preview_width,
                resize_images=True,
            )
            preview_paths = re.findall(r"<img\b[^>]*?\bsrc='([^']+)'", result.html)
            result.image_paths = list(dict.fromkeys([*result.image_paths, *preview_paths]))
            return result
        finally:
            if cleanup:
                cleanup()

    def _archive_db_paths(self, start_date: str, end_date: str) -> list[str]:
        if self.archive_page is None or not hasattr(self.archive_page, "get_analytics_db_paths"):
            return []
        return list(
            self.archive_page.get_analytics_db_paths(
                f"{start_date} 00:00:00",
                f"{end_date} 23:59:59",
            )
            or []
        )

    def _graphs_ready(self, token: int, result, save_after: bool):
        image_paths = list(getattr(result, "image_paths", []) or [])
        if token != self._request_token:
            return
        if self._closing:
            self._remember_temp_graph_paths(image_paths)
            self._cleanup_temp_graph_files()
            return
        self._cleanup_temp_graph_files()
        self._remember_temp_graph_paths(image_paths)
        html = str(getattr(result, "html", "") or "")
        self._latest_html = html
        self._latest_signature = self._signature()
        self.report.setHtml(html)
        self._set_busy(False, "")
        if save_after and self._pending_pdf_path:
            path = self._pending_pdf_path
            self._pending_pdf_path = ""
            self._start_pdf_worker(path)

    def _preview_width(self) -> int:
        viewport = self.report.viewport()
        return max(560, (viewport.width() if viewport is not None else 760) - 60)

    def save_pdf(self):
        if not self._selected_keys():
            self.status.setText("Выберите хотя бы один график")
            return
        os.makedirs(REPORT_DIR, exist_ok=True)
        dialog = PersistentSaveFileDialog(
            self,
            title="Сохранить графики",
            directory=REPORT_DIR,
            name_filter="PDF (*.pdf)",
            settings_key="archive/graphs_save_dialog",
            default_suffix="pdf",
        )
        dialog.selectFile(f"graphs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf")
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        paths = dialog.selectedFiles()
        if not paths:
            return
        path = paths[0]
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        if self._latest_html and self._latest_signature == self._signature():
            self._start_pdf_worker(path)
        else:
            self._pending_pdf_path = path
            self._start_graph_build(save_after=True)

    def _start_pdf_worker(self, path: str):
        if self._pdf_worker is not None and self._pdf_worker.isRunning():
            return
        self._set_busy(True, "Сохранение PDF…")
        worker = AsyncCallThread(lambda: build_graphs_pdf(self._latest_html, path), parent=self)
        self._pdf_worker = worker
        worker.succeeded.connect(lambda _path: self._set_busy(False, ""))
        worker.failed.connect(lambda error: self._set_busy(False, f"Ошибка PDF: {error}"))
        worker.finished.connect(lambda: self._pdf_worker_finished(worker))
        worker.start()

    def _request_failed(self, token: int, error: Exception):
        if token != self._request_token:
            return
        self._pending_pdf_path = ""
        self._set_busy(False, f"Ошибка: {error}")

    def _worker_finished(self, worker):
        if self._worker is worker:
            self._worker = None

    def _pdf_worker_finished(self, worker):
        if self._pdf_worker is worker:
            self._pdf_worker = None

    def _set_busy(self, busy: bool, text: str):
        self.btn_preview.setEnabled(not busy)
        self.btn_save_pdf.setEnabled(not busy)
        self.status.setText(text)

    def _mark_dirty(self, *_):
        if self._latest_html:
            self.status.setText("Параметры изменены")

    def _select_all(self):
        for checkbox in self.checkboxes.values():
            checkbox.setChecked(True)

    def _deselect_all(self):
        for checkbox in self.checkboxes.values():
            checkbox.setChecked(False)

    def _select_top(self):
        top = set(TOP_GRAPHS)
        for key, checkbox in self.checkboxes.items():
            checkbox.setChecked(key in top)

    def _remember_temp_graph_paths(self, paths):
        temp_root = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
        for raw_path in paths or ():
            path = os.path.abspath(str(raw_path or "").strip())
            name = os.path.basename(path).lower()
            if name.endswith(".png") and name.startswith(("graph_", "graph_preview_")):
                try:
                    if os.path.commonpath([os.path.normcase(path), temp_root]) == temp_root:
                        self._temp_graph_paths.add(path)
                except ValueError:
                    pass

    def _cleanup_temp_graph_files(self):
        remaining = set()
        for path in self._temp_graph_paths:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                remaining.add(path)
        self._temp_graph_paths = remaining

    def shutdown(self):
        self._closing = True
        self._request_token += 1
        for worker in (self._worker, self._pdf_worker):
            if worker is not None and worker.isRunning():
                worker.requestInterruption()
                worker.quit()
                worker.wait(1000)
        self._cleanup_temp_graph_files()
