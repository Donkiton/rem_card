from __future__ import annotations

import os
from datetime import datetime, timedelta

from PySide6.QtCore import QDate, Qt, Signal
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
from rem_card.services.analytics.platform import (
    AnalyticsEngine, AnalyticsPeriod, CohortDefinition, MetricScope, SnapshotCache,
    SavedAnalyticsView, SavedAnalyticsViewStore, StatisticsRepository, default_metric_registry,
    analytics_context_html, materialize_cohort_snapshot,
)
from .analytics_workspace import AnalyticsWorkspace


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

    status_changed = Signal(str)

    def __init__(self, *, source_mode: str, remcard_service=None, operblock_service=None,
                 archive_page=None, show_analytics_workspace: bool = True, parent=None):
        super().__init__(parent)
        self.source_mode = source_mode
        self.remcard_service = remcard_service
        self.operblock_service = operblock_service
        self.archive_page = archive_page
        self.show_analytics_workspace = bool(show_analytics_workspace)
        self.section_groups = OPERBLOCK_SECTION_GROUPS if self.is_operblock else SECTION_GROUPS
        self.top_sections = OPERBLOCK_TOP_INDICATORS if self.is_operblock else TOP_SECTIONS
        self.checkboxes: dict[str, QCheckBox] = {}
        self._worker = None
        self._comparison_worker = None
        self._pdf_worker = None
        self._request_token = 0
        self._comparison_token = 0
        self._loaded = False
        self._latest_report_html = ""
        self._latest_report_signature = None
        self._pending_pdf_path = ""
        self.analytics_registry = default_metric_registry()
        self.analytics_cache = SnapshotCache(maxsize=12)
        self.analytics_workspace = None
        self.analytics_view_store = SavedAnalyticsViewStore()
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

        # Рабочая область не заменяет старый отчёт: она объясняет те же
        # выбранные показатели и оставляет существующий PDF-поток нетронутым.
        self.analytics_workspace = AnalyticsWorkspace(self)
        workspace_scope = MetricScope.OPERBLOCK if self.is_operblock else MetricScope.RAO
        self.analytics_workspace.set_scope(workspace_scope)
        self.analytics_workspace.set_registry_definitions(self.analytics_registry.for_scope(workspace_scope))
        self.analytics_workspace.refresh_requested.connect(self.refresh_analysis)
        self.analytics_workspace.comparison_requested.connect(self._compare_selected_metric)
        self.analytics_workspace.save_view_requested.connect(self._save_analytics_view)
        self.analytics_workspace.load_view_requested.connect(self._load_analytics_view)
        self.analytics_workspace.delete_view_requested.connect(self._delete_analytics_view)
        self._refresh_saved_views()
        if self.show_analytics_workspace:
            layout.addWidget(self.analytics_workspace)

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
            self._set_status_text("Начальная дата позже конечной")
            return None
        return self.date_from.date().toString("yyyy-MM-dd"), self.date_to.date().toString("yyyy-MM-dd")

    def _selected_keys(self) -> list[str]:
        return [key for key, checkbox in self.checkboxes.items() if checkbox.isChecked()]

    def _selection_signature(self):
        period = self._period()
        if period is None:
            return None
        include_recovery = self.chk_include_recovery.isChecked() if not self.is_operblock else False
        cohort = self.analytics_workspace.cohort_definition() if self.analytics_workspace else CohortDefinition()
        selected_metric_id = self.analytics_workspace.metric_selector.currentData() if self.analytics_workspace else None
        comparison_mode = "previous_year"
        comparison = None
        if self.analytics_workspace and self.analytics_workspace.comparison_mode.currentIndex() == 1:
            comparison_mode = "manual"
            comparison = (self.analytics_workspace.period_b_from.date().toString("yyyy-MM-dd"), self.analytics_workspace.period_b_to.date().toString("yyyy-MM-dd"))
        return (period[0], period[1], tuple(self._selected_keys()), include_recovery,
                tuple((item.field, item.operator, str(item.value)) for item in cohort.filters), selected_metric_id,
                comparison_mode, comparison)

    def refresh_report(self):
        selected = self._selected_keys()
        if not selected:
            self._set_status_text("Выберите хотя бы один показатель")
            return
        self._start_request("report", selected)

    def refresh_analysis(self):
        """Refresh structured analytics independently from the legacy report."""
        self._start_request("analysis", [])

    def graph_context(self):
        period = self._period()
        requested = self.analytics_workspace.cohort_definition() if self.analytics_workspace else CohortDefinition()
        return (requested, *(period or ("", "")), self.chk_include_recovery.isChecked() if not self.is_operblock else False)

    def analytics_pdf_context(self) -> tuple[str, tuple[str, str] | None]:
        """Comparison state is shared with the separate RAO graphs page."""
        if self.analytics_workspace and self.analytics_workspace.comparison_mode.currentIndex() == 1:
            return (
                "manual",
                (
                    self.analytics_workspace.period_b_from.date().toString("yyyy-MM-dd"),
                    self.analytics_workspace.period_b_to.date().toString("yyyy-MM-dd"),
                ),
            )
        return "previous_year", None

    def _start_request(self, kind: str, selected: list[str]):
        period = self._period()
        if period is None:
            return
        if self._worker is not None and self._worker.isRunning():
            self._set_status_text("Дождитесь завершения текущего расчёта")
            return
        start_date, end_date = period
        self._request_token += 1
        token = self._request_token
        request_signature = (
            self._selection_signature() if kind == "report" else self._analysis_signature()
        )
        self._set_busy(True, "Расчёт…")
        include_recovery = self.chk_include_recovery.isChecked() if not self.is_operblock else False
        cohort = self.analytics_workspace.cohort_definition() if self.analytics_workspace else CohortDefinition()
        selected_metric_id = self.analytics_workspace.metric_selector.currentData() if self.analytics_workspace else None
        comparison_mode, comparison_period = self.analytics_pdf_context()
        try:
            source = self.operblock_service.db if self.is_operblock and self.operblock_service is not None else get_analytics_base_manager(remcard_service=self.remcard_service)
        except Exception:
            # Fingerprints are optional display provenance.  Their absence
            # must not turn an otherwise asynchronous empty-page state into a
            # synchronous UI exception.
            source = None
        archive_paths = self._archive_db_paths(start_date, end_date)
        def operation():
            html = ""
            if kind == "report":
                source_fingerprints = StatisticsRepository(source, db_paths=archive_paths).clinical_fingerprints()
                html = self._build_report(
                    start_date,
                    end_date,
                    archive_paths,
                    selected,
                    include_recovery,
                    cohort,
                    comparison_mode,
                    comparison_period,
                    selected_metric_id,
                    source_fingerprints,
                )
            try:
                snapshot = self._build_workspace_snapshot(
                    source=source,
                    archive_paths=archive_paths,
                    start_date=start_date,
                    end_date=end_date,
                    scope=MetricScope.OPERBLOCK if self.is_operblock else MetricScope.RAO,
                    cohort=cohort,
                    include_recovery=include_recovery,
                    selected_metric_id=selected_metric_id,
                )
                snapshot_error = ""
            except Exception as error:
                snapshot = None
                snapshot_error = str(error)
            return kind, html, snapshot, snapshot_error
        worker = AsyncCallThread(operation, parent=self)
        self._worker = worker
        worker.succeeded.connect(
            lambda payload, request=token, signature=request_signature:
            self._result_ready(request, payload, signature)
        )
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
        cohort: CohortDefinition,
        comparison_mode: str = "previous_year",
        comparison_period: tuple[str, str] | None = None,
        selected_metric_id: str | None = None,
        source_fingerprints: tuple[str, ...] = (),
    ) -> str:
        scope = MetricScope.OPERBLOCK if self.is_operblock else MetricScope.RAO
        # Единственное определение когорты обязано управлять и снимком builder,
        # и последующей структурированной сводкой.  В частности, признак
        # recovery нельзя оставлять отдельным UI-переключателем после отбора.
        cohort = CohortDefinition(cohort.name, scope, cohort.filters,
                                  include_recovery if scope is MetricScope.RAO else False)
        period = AnalyticsPeriod.from_values(start_date, end_date)
        context_metric_ids = list(selected)
        if selected_metric_id and selected_metric_id not in context_metric_ids:
            context_metric_ids.append(selected_metric_id)
        definitions = [self.analytics_registry.get(key) for key in context_metric_ids if key in self.analytics_registry.ids()]
        context_html = analytics_context_html(
            period=period,
            cohort=cohort,
            definitions=definitions,
            comparison_mode=comparison_mode,
            comparison_period=comparison_period,
            source_fingerprints=source_fingerprints or StatisticsRepository(db_paths=db_paths).clinical_fingerprints(),
        )
        if self.is_operblock:
            if self.operblock_service is None:
                raise RuntimeError("Сервис оперблока недоступен.")
            manager = self.operblock_service.db
            multi_manager = None
            cohort_manager = None
            try:
                if db_paths:
                    from rem_card.services.analytics.multi_db_analytics import create_multi_db_analytics_manager
                    multi_manager = create_multi_db_analytics_manager(db_paths, start_dt=start_date, end_dt=end_date)
                    manager = multi_manager
                cohort_manager, _cases = materialize_cohort_snapshot(manager, MetricScope.OPERBLOCK, period, cohort)
                html = build_operblock_statistics_report_html(
                    cohort_manager, start_date, end_date, selected, db_paths=(),
                )
                return self._normalize_report_html(self._append_analytics_context(html, context_html))
            finally:
                if cohort_manager is not None:
                    cohort_manager.close_connection()
                if multi_manager is not None:
                    multi_manager.close_connection()

        base_manager = get_analytics_base_manager(remcard_service=self.remcard_service)
        manager, cleanup = resolve_readonly_analytics_manager(
            base_manager,
            start_dt=start_date,
            end_dt=end_date,
            db_paths=db_paths,
        )
        try:
            cohort_manager = manager
            # Старые navigation-tests и third-party adapters могут передавать
            # непрозрачный proxy только в renderer. Реальный manager всегда
            # имеет get_connection; только его можно безопасно snapshot-ить.
            if hasattr(manager, "get_connection"):
                # Общие разделы применяют пользовательский recovery-флаг в
                # builder. В main snapshot сохраняем recovery-популяцию, чтобы
                # специальный раздел не становился пустым при выключенном флаге.
                materialized_cohort = CohortDefinition(
                    cohort.name, cohort.scope, cohort.filters, True,
                )
                cohort_manager, _cases = materialize_cohort_snapshot(
                    manager, MetricScope.RAO, period, materialized_cohort,
                )
            html = build_detailed_statistics_report_html(
                cohort_manager,
                start_date,
                end_date,
                selected,
                include_recovery_beds=include_recovery,
            )
            return self._normalize_report_html(self._append_analytics_context(html, context_html))
        finally:
            if 'cohort_manager' in locals() and cohort_manager is not manager:
                cohort_manager.close_connection()
            if cleanup:
                cleanup()

    @staticmethod
    def _append_analytics_context(html: str, context_html: str) -> str:
        source = str(html or "")
        return source.replace("</body>", f"{context_html}</body>", 1) if "</body>" in source else f"{source}{context_html}"

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

    def _result_ready(self, token: int, payload, request_signature=None):
        if token != self._request_token:
            return
        current_signature = self._selection_signature() if payload[0] == "report" else self._analysis_signature()
        if request_signature is not None and request_signature != current_signature:
            self._pending_pdf_path = ""
            target = "отчёт" if payload[0] == "report" else "анализ"
            self._set_busy(False, f"Фильтры изменены — обновите {target}")
            return
        kind, html, snapshot, snapshot_error = payload
        if kind == "report":
            self.report.setHtml(html or self._empty_html())
            self._loaded = True
        if self.analytics_workspace is not None:
            if snapshot is not None:
                self.analytics_workspace.set_snapshot(snapshot)
            elif snapshot_error:
                self.analytics_workspace.kpi.setText(f"Структурированная сводка недоступна: {snapshot_error}")
        if kind == "report":
            self._latest_report_html = str(html or "")
            self._latest_report_signature = request_signature or self._selection_signature()
        self._set_busy(False, "")
        if kind == "report" and self._pending_pdf_path:
            path = self._pending_pdf_path
            self._pending_pdf_path = ""
            self._start_pdf_worker(path)

    def _build_workspace_snapshot(
        self,
        *,
        source,
        archive_paths: list[str],
        start_date: str,
        end_date: str,
        scope: MetricScope,
        cohort: CohortDefinition,
        include_recovery: bool,
        selected_metric_id: str | None,
    ):
        """Build KPI/methodology data off the GUI thread."""
        normalized_cohort = CohortDefinition(
            cohort.name,
            scope,
            cohort.filters,
            include_recovery if scope is MetricScope.RAO else False,
        )
        metric_ids = [definition.id for definition in self.analytics_registry.for_scope(scope) if definition.is_kpi]
        if selected_metric_id in self.analytics_registry.ids() and selected_metric_id not in metric_ids:
            metric_ids.append(selected_metric_id)
        engine = AnalyticsEngine(
            StatisticsRepository(source, db_paths=archive_paths),
            self.analytics_registry,
            self.analytics_cache,
        )
        return engine.snapshot(
            scope,
            AnalyticsPeriod.from_values(start_date, end_date),
            normalized_cohort,
            tuple(metric_ids),
        )

    def _analysis_signature(self):
        period = self._period()
        if period is None:
            return None
        include_recovery = self.chk_include_recovery.isChecked() if not self.is_operblock else False
        cohort = self.analytics_workspace.cohort_definition() if self.analytics_workspace else CohortDefinition()
        selected_metric_id = self.analytics_workspace.metric_selector.currentData() if self.analytics_workspace else None
        comparison_mode, comparison_period = self.analytics_pdf_context()
        return (
            period[0], period[1], include_recovery,
            tuple((item.field, item.operator, str(item.value)) for item in cohort.filters),
            selected_metric_id, comparison_mode, comparison_period,
        )

    def _compare_selected_metric(self):
        if not self.analytics_workspace or not self.analytics_workspace.selected_metric:
            return
        period = self._period()
        if period is None:
            return
        try:
            if self._comparison_worker is not None and self._comparison_worker.isRunning():
                self._set_status_text("Дождитесь завершения сравнения")
                return
            scope = MetricScope.OPERBLOCK if self.is_operblock else MetricScope.RAO
            source = self.operblock_service.db if self.is_operblock and self.operblock_service is not None else get_analytics_base_manager(remcard_service=self.remcard_service)
            requested = self.analytics_workspace.cohort_definition()
            cohort = CohortDefinition(requested.name, scope, requested.filters,
                                      self.chk_include_recovery.isChecked() if scope is MetricScope.RAO else False)
            manual = self.analytics_workspace.comparison_mode.currentIndex() == 1
            comparison_period = None
            if manual:
                comparison_period = (self.analytics_workspace.period_b_from.date().toString("yyyy-MM-dd"), self.analytics_workspace.period_b_to.date().toString("yyyy-MM-dd"))
            current_period = AnalyticsPeriod.from_values(*period)
            previous_period = AnalyticsPeriod.from_values(*comparison_period) if comparison_period else AnalyticsPeriod.previous_calendar_year(current_period)
            metric_id = self.analytics_workspace.selected_metric.definition.id
            current_paths = self._archive_db_paths(*period)
            previous_paths = self._archive_db_paths(
                previous_period.start.date().isoformat(),
                (previous_period.end - timedelta(days=1)).date().isoformat(),
            )
        except Exception as error:
            self._set_status_text(f"Сравнение недоступно: {error}")
            return

        self._comparison_token += 1
        token = self._comparison_token
        request_signature = self._analysis_signature()
        self.analytics_workspace.btn_compare.setEnabled(False)
        self._set_status_text("Сравнение…")
        operation = lambda: self._build_comparison_message(
            source=source,
            current_paths=current_paths,
            previous_paths=previous_paths,
            scope=scope,
            cohort=cohort,
            current_period=current_period,
            previous_period=previous_period,
            metric_id=metric_id,
            manual=manual,
        )
        worker = AsyncCallThread(operation, parent=self)
        self._comparison_worker = worker
        worker.succeeded.connect(
            lambda message, request=token, signature=request_signature:
            self._comparison_ready(request, message, signature)
        )
        worker.failed.connect(lambda error, request=token: self._comparison_failed(request, error))
        worker.finished.connect(lambda: self._comparison_worker_finished(worker))
        worker.start()

    def _build_comparison_message(
        self,
        *,
        source,
        current_paths: list[str],
        previous_paths: list[str],
        scope: MetricScope,
        cohort: CohortDefinition,
        current_period: AnalyticsPeriod,
        previous_period: AnalyticsPeriod,
        metric_id: str,
        manual: bool,
    ) -> str:
        current_engine = AnalyticsEngine(
            StatisticsRepository(source, db_paths=current_paths),
            self.analytics_registry,
            self.analytics_cache,
        )
        current = current_engine.snapshot(scope, current_period, cohort, (metric_id,)).results[metric_id]
        # Rotation is resolved independently for B; a current archive is
        # never reused as a surrogate historical source.
        previous_engine = AnalyticsEngine(
            StatisticsRepository(source, db_paths=previous_paths),
            self.analytics_registry,
            self.analytics_cache,
        )
        previous_snapshot = previous_engine.snapshot(scope, previous_period, cohort, (metric_id,))
        from rem_card.services.analytics.platform import metric_result_has_data

        candidate = previous_snapshot.results[metric_id]
        previous = candidate if metric_result_has_data(candidate) else None
        if previous is None:
            return "Нет данных за ручной период" if manual else "Нет данных за предыдущий год"
        if isinstance(current.value, (int, float)) and isinstance(previous.value, (int, float)):
            return f"Δ: {float(current.value) - float(previous.value):+.2f}"
        return self._structured_comparison_message(
            type("Comparison", (), {"current": current, "previous": previous})()
        )

    def _comparison_ready(self, token: int, message: str, request_signature=None):
        if token != self._comparison_token or self.analytics_workspace is None:
            return
        if request_signature is not None and request_signature != self._analysis_signature():
            self._set_status_text("Фильтры изменены — повторите сравнение")
            self.analytics_workspace.btn_compare.setEnabled(True)
            return
        self.analytics_workspace.kpi.setText(message)
        self._set_status_text("")
        self.analytics_workspace.btn_compare.setEnabled(True)

    def _comparison_failed(self, token: int, error: Exception):
        if token != self._comparison_token:
            return
        self._set_status_text(f"Сравнение недоступно: {error}")
        if self.analytics_workspace is not None:
            self.analytics_workspace.btn_compare.setEnabled(True)

    def _comparison_worker_finished(self, worker):
        if self._comparison_worker is worker:
            self._comparison_worker = None

    def _refresh_saved_views(self):
        if self.analytics_workspace:
            scope = MetricScope.OPERBLOCK if self.is_operblock else MetricScope.RAO
            self.analytics_workspace.set_saved_views([item.name for item in self.analytics_view_store.load() if item.scope is scope])

    def _save_analytics_view(self):
        if not self.analytics_workspace:
            return
        name = self.analytics_workspace.view_name.text().strip()
        if not name:
            self._set_status_text("Укажите название сохранённого вида")
            return
        scope = MetricScope.OPERBLOCK if self.is_operblock else MetricScope.RAO
        views = [item for item in self.analytics_view_store.load() if not (item.name == name and item.scope is scope)]
        requested = self.analytics_workspace.cohort_definition()
        cohort = CohortDefinition(requested.name, scope, requested.filters, self.chk_include_recovery.isChecked() if not self.is_operblock else False)
        manual_period = None
        if self.analytics_workspace.comparison_mode.currentIndex() == 1:
            manual_period = (self.analytics_workspace.period_b_from.date().toString("yyyy-MM-dd"), self.analytics_workspace.period_b_to.date().toString("yyyy-MM-dd"))
        views.append(SavedAnalyticsView(name, scope, cohort, tuple(self._selected_keys()), manual_period, self.analytics_workspace.metric_selector.currentData(), "manual" if manual_period else "previous_year", period_a=self._period()))
        self.analytics_view_store.save(views); self._refresh_saved_views(); self.analytics_workspace.saved_views.setCurrentText(name)

    def _load_analytics_view(self):
        if not self.analytics_workspace:
            return
        name = self.analytics_workspace.saved_views.currentText()
        scope = MetricScope.OPERBLOCK if self.is_operblock else MetricScope.RAO
        view = next((item for item in self.analytics_view_store.load() if item.name == name and item.scope is scope), None)
        if view is None:
            return
        self.analytics_workspace.set_cohort_definition(view.cohort)
        if view.period_a:
            self.date_from.setDate(self.date_from.date().fromString(view.period_a[0], "yyyy-MM-dd"))
            self.date_to.setDate(self.date_to.date().fromString(view.period_a[1], "yyyy-MM-dd"))
        if not self.is_operblock:
            self.chk_include_recovery.setChecked(view.cohort.include_recovery_beds)
        if view.comparison_mode == "manual" and view.comparison_period:
            self.analytics_workspace.comparison_mode.setCurrentIndex(1)
            self.analytics_workspace.period_b_from.setDate(self.analytics_workspace.period_b_from.date().fromString(view.comparison_period[0], "yyyy-MM-dd"))
            self.analytics_workspace.period_b_to.setDate(self.analytics_workspace.period_b_to.date().fromString(view.comparison_period[1], "yyyy-MM-dd"))
        else:
            self.analytics_workspace.comparison_mode.setCurrentIndex(0)
        for key, checkbox in self.checkboxes.items(): checkbox.setChecked(key in view.metric_ids)
        if view.selected_metric_id:
            index = self.analytics_workspace.metric_selector.findData(view.selected_metric_id)
            if index >= 0:
                self.analytics_workspace.metric_selector.setCurrentIndex(index)
        # Единый refresh signal обновляет и отчёт, и контекст отдельной
        # страницы графиков.  Иначе загруженный RAO view оставлял бы графики
        # на предыдущей когорте.
        self.analytics_workspace.refresh_requested.emit()

    @staticmethod
    def _structured_comparison_message(comparison):
        """Показывает A/B artifact вместо ложного сообщения о недоступности."""
        def summary(result):
            artifact = result.artifact or {}
            return str(artifact.get("summary") or result.definition.title)
        return f"A: {summary(comparison.current)} · B: {summary(comparison.previous)}"

    def _delete_analytics_view(self):
        if not self.analytics_workspace:
            return
        name = self.analytics_workspace.saved_views.currentText()
        scope = MetricScope.OPERBLOCK if self.is_operblock else MetricScope.RAO
        self.analytics_view_store.save([item for item in self.analytics_view_store.load() if not (item.name == name and item.scope is scope)]); self._refresh_saved_views()

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
            self._set_status_text("Выберите хотя бы один показатель")
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
        if self.analytics_workspace is not None:
            self.analytics_workspace.btn_update.setEnabled(not busy)
            self.analytics_workspace.metric_selector.setEnabled(not busy)
        self._set_status_text(text)

    def _set_status_text(self, text: str):
        self.status.setText(text)
        self.status_changed.emit(text)

    def _mark_dirty(self, *_):
        if self._loaded:
            self._set_status_text("Фильтры изменены")

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
        self._comparison_token += 1
        if self._worker is not None:
            self._worker.quit()
        if self._comparison_worker is not None:
            self._comparison_worker.quit()
            self._comparison_worker.wait(1000)
        if self._pdf_worker is not None and self._pdf_worker.isRunning():
            self._pdf_worker.requestInterruption()
            self._pdf_worker.quit()
            self._pdf_worker.wait(1000)
