from __future__ import annotations

import os
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
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from rem_card.app.paths import REPORT_DIR
from rem_card.services.analytics.detailed_statistics_service import (
    SECTION_GROUPS,
    TOP_SECTIONS,
    build_detailed_statistics_report_html,
)
from rem_card.services.analytics.operblock_statistics_service import (
    OPERBLOCK_SECTION_GROUPS,
    OPERBLOCK_TOP_INDICATORS,
    build_operblock_statistics_report_html,
)
from rem_card.ui.shared.analytics_integration import (
    get_analytics_base_manager,
    resolve_readonly_analytics_manager,
)
from rem_card.ui.shared.archive_date_edit import ArchiveDateEdit
from rem_card.ui.shared.async_call import AsyncCallThread
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.shared.html_pdf_worker import HtmlPdfWorker
from rem_card.ui.shared.persistent_file_dialog import PersistentSaveFileDialog


class _IndicatorOption(QWidget):
    def __init__(self, caption: str, parent=None):
        super().__init__(parent)
        self.setObjectName("ArchiveStatisticsOptionRow")
        row = QHBoxLayout(self)
        row.setContentsMargins(1, 2, 1, 2)
        row.setSpacing(7)
        self.checkbox = QCheckBox(self)
        self.checkbox.setObjectName("ArchiveStatisticsCheck")
        row.addWidget(self.checkbox, 0, Qt.AlignTop)
        self.label = QLabel(caption, self)
        self.label.setObjectName("ArchiveStatisticsOptionLabel")
        self.label.setWordWrap(True)
        self.label.setCursor(Qt.PointingHandCursor)
        self.label.mousePressEvent = self._toggle_from_label
        row.addWidget(self.label, 1)

    def _toggle_from_label(self, event):
        self.checkbox.toggle()
        event.accept()


class _ArchiveReportBrowser(QTextBrowser):
    """Показывает полный диагноз для сокращённых строк отчёта."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

    def mouseMoveEvent(self, event):
        cursor = self.cursorForPosition(event.position().toPoint())
        tooltip = cursor.charFormat().toolTip()
        if tooltip:
            QToolTip.showText(event.globalPosition().toPoint(), tooltip, self)
        else:
            QToolTip.hideText()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        QToolTip.hideText()
        super().leaveEvent(event)


class ArchiveStatisticsPage(QWidget):
    """Встроенный отчёт с выбором показателей, экспортом и графиками."""

    def __init__(self, *, source_mode: str, remcard_service=None, operblock_service=None, archive_page=None, parent=None):
        super().__init__(parent)
        self.source_mode = source_mode
        self.remcard_service = remcard_service
        self.operblock_service = operblock_service
        self.archive_page = archive_page
        self.section_groups = OPERBLOCK_SECTION_GROUPS if self.is_operblock else SECTION_GROUPS
        self.top_sections = OPERBLOCK_TOP_INDICATORS if self.is_operblock else TOP_SECTIONS
        self.checkboxes: dict[str, QCheckBox] = {}
        self._worker = None
        self._pdf_worker = None
        self._request_token = 0
        self._loaded = False
        self._latest_report_html = ""
        self._latest_report_signature = None
        self._pending_pdf_path = ""
        self._init_ui()

    @property
    def is_operblock(self) -> bool:
        return self.source_mode == "operblock"

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
        toolbar_layout.addSpacing(6)

        self.btn_refresh = QPushButton("Отсортировать", toolbar)
        self.btn_refresh.setObjectName("ArchiveStatisticsRefresh")
        self.btn_refresh.clicked.connect(self.refresh_report)
        toolbar_layout.addWidget(self.btn_refresh)

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

        recovery_option = _IndicatorOption(
            "Учитывать койки пробуждения в общих показателях",
            selector,
        )
        recovery_option.setObjectName("ArchiveRecoveryOption")
        self.chk_include_recovery = recovery_option.checkbox
        self.chk_include_recovery.setObjectName("ArchiveRecoveryToggle")
        self.chk_include_recovery.setChecked(False)
        recovery_option.setToolTip(
            "Выключено: пациенты пробуждения не входят в общие госпитализации, "
            "койко-дни и производные показатели. Включено: входят."
        )
        self.chk_include_recovery.toggled.connect(self._mark_dirty)
        selector_layout.addWidget(recovery_option)
        recovery_option.setVisible(not self.is_operblock)

        scroll = QScrollArea(selector)
        scroll.setObjectName("ArchiveStatisticsOptionsScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        options = QWidget(scroll)
        options.setObjectName("ArchiveStatisticsOptions")
        options_layout = QVBoxLayout(options)
        options_layout.setContentsMargins(4, 4, 4, 4)
        options_layout.setSpacing(5)
        for group_name, items in self.section_groups.items():
            group_label = QLabel(group_name, options)
            group_label.setObjectName("ArchiveStatisticsGroup")
            group_label.setWordWrap(True)
            options_layout.addWidget(group_label)
            for key, caption in items.items():
                option = _IndicatorOption(caption, options)
                checkbox = option.checkbox
                checkbox.setChecked(True)
                checkbox.toggled.connect(self._mark_dirty)
                options_layout.addWidget(option)
                self.checkboxes[key] = checkbox
        options_layout.addStretch(1)
        scroll.setWidget(options)
        selector_layout.addWidget(scroll, 1)
        body.addWidget(selector)

        self.report = _ArchiveReportBrowser(self)
        self.report.setObjectName("ArchiveStatisticsReport")
        self.report.setOpenExternalLinks(False)
        self.report.setLineWrapMode(QTextBrowser.WidgetWidth)
        self.report.setHtml(self._empty_html())
        body.addWidget(self.report, 1)
        layout.addLayout(body, 1)

        self.date_from.dateChanged.connect(self._mark_dirty)
        self.date_to.dateChanged.connect(self._mark_dirty)

    def ensure_loaded(self):
        """Формирует полный отчёт при первом переходе на страницу."""
        if not self._loaded:
            self.refresh_report()

    @staticmethod
    def _empty_html() -> str:
        return "<div style='padding:28px;color:#6b7785;'>Подготовка статистического отчёта…</div>"

    def _period(self) -> tuple[str, str] | None:
        if self.date_from.date() > self.date_to.date():
            self.status.setText("Начальная дата позже конечной")
            return None
        return self.date_from.date().toString("yyyy-MM-dd"), self.date_to.date().toString("yyyy-MM-dd")

    def _selected_keys(self) -> list[str]:
        return [key for key, checkbox in self.checkboxes.items() if checkbox.isChecked()]

    def _selection_signature(self):
        period = self._period()
        if period is None:
            return None
        include_recovery = self.chk_include_recovery.isChecked() if not self.is_operblock else False
        return period[0], period[1], tuple(self._selected_keys()), include_recovery

    def refresh_report(self):
        selected = self._selected_keys()
        if not selected:
            self.status.setText("Выберите хотя бы один показатель")
            return
        self._start_request("report", selected)

    def _start_request(self, kind: str, selected: list[str]):
        period = self._period()
        if period is None:
            return
        if self._worker is not None and self._worker.isRunning():
            self.status.setText("Дождитесь завершения текущего расчёта")
            return
        start_date, end_date = period
        self._request_token += 1
        token = self._request_token
        self._set_busy(True, "Расчёт…")
        include_recovery = self.chk_include_recovery.isChecked() if not self.is_operblock else False
        operation = lambda: (
            kind,
            self._build_report(
                start_date,
                end_date,
                self._archive_db_paths(start_date, end_date),
                selected,
                include_recovery,
            ),
        )
        worker = AsyncCallThread(operation, parent=self)
        self._worker = worker
        worker.succeeded.connect(lambda payload, request=token: self._result_ready(request, payload))
        worker.failed.connect(lambda error, request=token: self._request_failed(request, error))
        worker.finished.connect(lambda: self._worker_finished(worker))
        worker.start()

    def _archive_db_paths(self, start_date: str, end_date: str) -> list[str]:
        if self.archive_page is None or not hasattr(self.archive_page, "get_analytics_db_paths"):
            return []
        start_bound = start_date if " " in start_date else f"{start_date} 00:00:00"
        end_bound = end_date if " " in end_date else f"{end_date} 23:59:59"
        return list(
            self.archive_page.get_analytics_db_paths(
                start_bound,
                end_bound,
            )
            or []
        )

    def _build_report(
        self,
        start_date: str,
        end_date: str,
        db_paths: list[str],
        selected: list[str],
        include_recovery: bool,
    ) -> str:
        if self.is_operblock:
            if self.operblock_service is None:
                raise RuntimeError("Сервис оперблока недоступен.")
            html = build_operblock_statistics_report_html(
                self.operblock_service.db,
                start_date,
                end_date,
                selected,
                db_paths=db_paths,
            )
            return self._normalize_report_html(html)

        base_manager = get_analytics_base_manager(remcard_service=self.remcard_service)
        manager, cleanup = resolve_readonly_analytics_manager(
            base_manager,
            start_dt=start_date,
            end_dt=end_date,
            db_paths=db_paths,
        )
        try:
            html = build_detailed_statistics_report_html(
                manager,
                start_date,
                end_date,
                selected,
                include_recovery_beds=include_recovery,
            )
            return self._normalize_report_html(html)
        finally:
            if cleanup:
                cleanup()

    @staticmethod
    def _normalize_report_html(html: str) -> str:
        stable_tables = """
        <style>
          table { width: 100% !important; table-layout: fixed; border-collapse: collapse; }
          th, td { box-sizing: border-box; overflow-wrap: anywhere; vertical-align: top; }
          th:nth-child(1), td:nth-child(1) { width: 26%; }
          th:nth-child(2), td:nth-child(2) { width: 43%; }
          th:nth-child(3), td:nth-child(3) { width: 13%; }
          th:nth-child(4), td:nth-child(4) { width: 18%; }
          td.value { text-align: right; white-space: nowrap; }
          td.unit { color: #5d6b78; }
          td.distribution { width: 31% !important; line-height: 1.45; }
        </style>
        """
        source = str(html or "")
        if "</head>" in source:
            return source.replace("</head>", f"{stable_tables}</head>", 1)
        return f"<html><head>{stable_tables}</head><body>{source}</body></html>"

    def _result_ready(self, token: int, payload):
        if token != self._request_token:
            return
        kind, html = payload
        self.report.setHtml(html or self._empty_html())
        self._loaded = True
        if kind == "report":
            self._latest_report_html = str(html or "")
            self._latest_report_signature = self._selection_signature()
        self._set_busy(False, "")
        if kind == "report" and self._pending_pdf_path:
            path = self._pending_pdf_path
            self._pending_pdf_path = ""
            self._start_pdf_worker(path)

    def _request_failed(self, token: int, error: Exception):
        if token != self._request_token:
            return
        self._pending_pdf_path = ""
        self._set_busy(False, f"Ошибка: {error}")

    def _worker_finished(self, worker):
        if self._worker is worker:
            self._worker = None

    def save_pdf(self):
        if not self._selected_keys():
            self.status.setText("Выберите хотя бы один показатель")
            return
        os.makedirs(REPORT_DIR, exist_ok=True)
        prefix = "operblock_statistics" if self.is_operblock else "rao_statistics"
        default_name = f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        dialog = PersistentSaveFileDialog(
            self,
            title="Сохранить статистический отчёт",
            directory=REPORT_DIR,
            name_filter="PDF (*.pdf)",
            settings_key="archive/statistics_save_dialog",
            default_suffix="pdf",
        )
        dialog.selectFile(default_name)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        paths = dialog.selectedFiles()
        if not paths:
            return
        pdf_path = paths[0]
        if not pdf_path.lower().endswith(".pdf"):
            pdf_path += ".pdf"

        if self._latest_report_html and self._latest_report_signature == self._selection_signature():
            self._start_pdf_worker(pdf_path)
        else:
            self._pending_pdf_path = pdf_path
            self.refresh_report()

    def _start_pdf_worker(self, pdf_path: str):
        if self._pdf_worker is not None and self._pdf_worker.isRunning():
            return
        self._set_busy(True, "Сохранение PDF…")
        worker = HtmlPdfWorker(self._latest_report_html, pdf_path, parent=self)
        self._pdf_worker = worker
        worker.completed.connect(self._pdf_ready)
        worker.failed.connect(self._pdf_failed)
        worker.finished.connect(lambda: self._pdf_finished(worker))
        worker.start()

    def _pdf_ready(self, pdf_path: str):
        self._set_busy(False, "PDF сохранён")
        CustomMessageBox.information(self, "Статистика", f"Отчёт сохранён:\n{pdf_path}")

    def _pdf_failed(self, error: str):
        self._set_busy(False, f"Не удалось сохранить PDF: {error}")

    def _pdf_finished(self, worker):
        if self._pdf_worker is worker:
            self._pdf_worker = None

    def _set_busy(self, busy: bool, text: str):
        for button in (self.btn_refresh, self.btn_save_pdf):
            button.setEnabled(not busy)
        self.status.setText(text)

    def _mark_dirty(self, *_):
        if self._loaded:
            self.status.setText("Фильтры изменены")

    def _select_all(self):
        for checkbox in self.checkboxes.values():
            checkbox.setChecked(True)

    def _deselect_all(self):
        for checkbox in self.checkboxes.values():
            checkbox.setChecked(False)

    def _select_top(self):
        top = set(self.top_sections)
        for key, checkbox in self.checkboxes.items():
            checkbox.setChecked(key in top)

    def shutdown(self):
        self._request_token += 1
        if self._worker is not None:
            self._worker.quit()
        if self._pdf_worker is not None and self._pdf_worker.isRunning():
            self._pdf_worker.requestInterruption()
            self._pdf_worker.quit()
            self._pdf_worker.wait(1000)
