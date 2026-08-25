"""Компактная рабочая область аналитики без отдельного системного диалога."""
from __future__ import annotations

from collections.abc import Mapping
from html import escape
from html.parser import HTMLParser

from PySide6.QtCore import Signal, QDate
from PySide6.QtWidgets import QButtonGroup, QComboBox, QFrame, QGridLayout, QHeaderView, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy, QStackedWidget, QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget

from rem_card.services.analytics.platform import CohortDefinition, CohortFilter, MetricDefinition, MetricResult, MetricScope, StatisticsSnapshot, population_kind_label
from rem_card.ui.shared.archive_date_edit import ArchiveDateEdit


class _DisplayTextExtractor(HTMLParser):
    """Turns legacy display HTML into plain text while preserving line breaks."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppressed = 0

    def handle_starttag(self, tag: str, _attrs):
        tag = tag.casefold()
        if tag in {"script", "style"}:
            self._suppressed += 1
        elif tag == "br" and not self._suppressed:
            self.parts.append("\n")

    def handle_endtag(self, tag: str):
        tag = tag.casefold()
        if tag in {"script", "style"} and self._suppressed:
            self._suppressed -= 1
        elif tag in {"p", "div", "li"} and not self._suppressed:
            self.parts.append("\n")

    def handle_data(self, data: str):
        if not self._suppressed:
            self.parts.append(data)

    def lines(self) -> tuple[str, ...]:
        text = "".join(self.parts)
        return tuple(" ".join(line.split()) for line in text.splitlines() if line.strip())


def _safe_display_html(value) -> str:
    parser = _DisplayTextExtractor()
    parser.feed(str(value if value is not None else "—"))
    parser.close()
    lines = parser.lines() or ("—",)
    return "<br>".join(escape(line) for line in lines)


class AnalyticsWorkspace(QFrame):
    """KPI → отчёт → методика → исходные случаи для уже выбранного показателя."""
    MODES = ("Результат", "Как рассчитано", "Какие пациенты вошли")
    refresh_requested = Signal()
    comparison_requested = Signal()
    save_view_requested = Signal()
    load_view_requested = Signal()
    delete_view_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("AnalyticsWorkspace")
        self.snapshot: StatisticsSnapshot | None = None
        self.selected_metric: MetricResult | None = None
        self.scope = MetricScope.RAO
        self._filters: list[CohortFilter] = []
        self._definitions: dict[str, MetricDefinition] = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        cohort_section = QFrame(self)
        cohort_section.setObjectName("AnalyticsSection")
        cohort_layout = QVBoxLayout(cohort_section)
        cohort_layout.setContentsMargins(12, 10, 12, 10)
        cohort_layout.setSpacing(7)
        cohort_title = QLabel("Группа пациентов (необязательно)", cohort_section)
        cohort_title.setObjectName("AnalyticsSectionTitle")
        cohort_layout.addWidget(cohort_title)
        controls = QGridLayout(); controls.setHorizontalSpacing(7); controls.setVerticalSpacing(4)
        self.metric_selector = QComboBox(self); self.metric_selector.setObjectName("AnalyticsMetricSelector")
        self.metric_selector.currentIndexChanged.connect(self._metric_changed)
        self.cohort_field = QComboBox(self); self.cohort_field.setObjectName("AnalyticsCohortField")
        self.cohort_operator = QComboBox(self); self.cohort_operator.setObjectName("AnalyticsCohortOperator")
        for label, value in (("равно", "equals"), ("содержит", "contains"), ("не меньше", "gte"), ("не больше", "lte"), ("да / нет", "truthy")):
            self.cohort_operator.addItem(label, value)
        self.cohort_value = QLineEdit(self); self.cohort_value.setObjectName("AnalyticsCohortValue"); self.cohort_value.setPlaceholderText("Значение когорты")
        self.btn_add_filter = QPushButton("Добавить", self); self.btn_add_filter.setObjectName("ArchiveStatisticsOption"); self.btn_add_filter.clicked.connect(self._add_filter)
        self.btn_remove_filter = QPushButton("Убрать", self); self.btn_remove_filter.setObjectName("ArchiveStatisticsOption"); self.btn_remove_filter.clicked.connect(self._remove_filter)
        self.active_filters = QLabel("Все случаи", self); self.active_filters.setObjectName("AnalyticsWorkspaceText"); self.active_filters.setWordWrap(True)
        self.comparison_mode = QComboBox(self); self.comparison_mode.setObjectName("AnalyticsComparisonMode"); self.comparison_mode.addItems(["Прошлый год", "Ручной A/B"])
        self.period_b_from = ArchiveDateEdit(QDate.currentDate().addYears(-1), self); self.period_b_from.setObjectName("AnalyticsPeriodBFrom")
        self.period_b_to = ArchiveDateEdit(QDate.currentDate(), self); self.period_b_to.setObjectName("AnalyticsPeriodBTo")
        self.period_b_from.setFixedWidth(132); self.period_b_to.setFixedWidth(132)
        self.btn_update = QPushButton("Рассчитать", self); self.btn_update.setObjectName("ArchiveStatisticsRefresh"); self.btn_update.clicked.connect(self.refresh_requested)
        self.btn_compare = QPushButton("Сравнить", self); self.btn_compare.setObjectName("ArchiveStatisticsSecondary"); self.btn_compare.clicked.connect(self.comparison_requested)
        for column, text in enumerate(("Поле", "Условие", "Значение")):
            label = QLabel(text, cohort_section); label.setObjectName("AnalyticsFieldLabel"); controls.addWidget(label, 0, column)
        controls.addWidget(self.cohort_field, 1, 0)
        controls.addWidget(self.cohort_operator, 1, 1)
        controls.addWidget(self.cohort_value, 1, 2, 1, 2)
        controls.addWidget(self.active_filters, 2, 0)
        controls.addWidget(self.btn_add_filter, 2, 1)
        controls.addWidget(self.btn_remove_filter, 2, 2)
        controls.addWidget(self.btn_update, 2, 3)
        controls.setColumnMinimumWidth(1, 175)
        controls.setColumnStretch(0, 2)
        controls.setColumnStretch(2, 2)
        controls.setColumnStretch(3, 1)
        cohort_layout.addLayout(controls)
        outer.addWidget(cohort_section)

        step_two = QLabel("2. Основные показатели", self)
        step_two.setObjectName("AnalyticsStepTitle")
        outer.addWidget(step_two)
        self.kpi_host = QWidget(self)
        self.kpi_grid = QGridLayout(self.kpi_host)
        self.kpi_grid.setContentsMargins(0, 0, 0, 0)
        self.kpi_grid.setHorizontalSpacing(8); self.kpi_grid.setVerticalSpacing(8)
        self.kpi_placeholder = QLabel("Нажмите «Рассчитать», чтобы получить показатели.", self.kpi_host)
        self.kpi_placeholder.setObjectName("AnalyticsWorkspaceText")
        self.kpi_grid.addWidget(self.kpi_placeholder, 0, 0)
        outer.addWidget(self.kpi_host)

        step_three = QLabel("3. Разобрать выбранный показатель", self)
        step_three.setObjectName("AnalyticsStepTitle")
        outer.addWidget(step_three)
        metric_section = QFrame(self)
        metric_section.setObjectName("AnalyticsSection")
        metric_layout = QVBoxLayout(metric_section)
        metric_layout.setContentsMargins(12, 10, 12, 10)
        metric_layout.setSpacing(7)
        metric_controls = QGridLayout(); metric_controls.setHorizontalSpacing(7); metric_controls.setVerticalSpacing(4)
        for column, text in enumerate(("Показатель для разбора", "Сравнение", "Период B: с", "по")):
            label = QLabel(text, metric_section); label.setObjectName("AnalyticsFieldLabel"); metric_controls.addWidget(label, 0, column)
            if column >= 2:
                label.setObjectName("AnalyticsComparisonPeriodLabel")
        metric_controls.addWidget(self.metric_selector, 1, 0)
        metric_controls.addWidget(self.comparison_mode, 1, 1)
        metric_controls.addWidget(self.period_b_from, 1, 2)
        metric_controls.addWidget(self.period_b_to, 1, 3)
        metric_controls.addWidget(self.btn_compare, 1, 4)
        metric_controls.setColumnStretch(0, 3); metric_controls.setColumnStretch(1, 1)
        metric_layout.addLayout(metric_controls)

        nav = QHBoxLayout(); nav.setSpacing(5)
        self.mode_group = QButtonGroup(self); self.mode_group.setExclusive(True)
        self.mode_buttons: dict[str, QPushButton] = {}
        self.stack = QStackedWidget(self)
        for index, label in enumerate(self.MODES):
            button = QPushButton(label, self); button.setObjectName("ArchivePageButton"); button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, page=index: self.stack.setCurrentIndex(page))
            self.mode_group.addButton(button, index); self.mode_buttons[label] = button; nav.addWidget(button)
        nav.addStretch(1); metric_layout.addLayout(nav)

        self.selected_result = QTextBrowser(self)
        self.selected_result.setObjectName("AnalyticsSelectedResult")
        self.selected_result.setOpenExternalLinks(False)
        self.selected_result.setHtml("Выберите показатель и нажмите «Рассчитать».")
        self.kpi = self.selected_result
        self.methodology = QLabel("Здесь будут показаны формула, включения и исключения.", self); self.methodology.setObjectName("AnalyticsWorkspaceText"); self.methodology.setWordWrap(True)
        self.source_cases = QTableWidget(0, 4, self); self.source_cases.setObjectName("ArchiveDataTable")
        self.source_cases.setHorizontalHeaderLabels(["Номер истории болезни", "ФИО", "Дата", "Основание включения"])
        source_header = self.source_cases.horizontalHeader()
        source_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        source_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        source_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        source_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.source_cases.setMinimumHeight(150)
        for page in (self.selected_result, self.methodology, self.source_cases): self.stack.addWidget(page)
        metric_layout.addWidget(self.stack, 1)
        outer.addWidget(metric_section, 1)
        self.mode_buttons["Результат"].setChecked(True)

        views_section = QFrame(self)
        views_section.setObjectName("AnalyticsSection")
        views = QGridLayout(views_section); views.setContentsMargins(12, 8, 12, 8); views.setHorizontalSpacing(7); views.setVerticalSpacing(4)
        self.view_name = QLineEdit(self); self.view_name.setObjectName("AnalyticsViewName"); self.view_name.setPlaceholderText("Название вида")
        self.saved_views = QComboBox(self); self.saved_views.setObjectName("AnalyticsSavedViews")
        self.btn_save_view = QPushButton("Сохранить", self); self.btn_save_view.setObjectName("ArchiveStatisticsOption"); self.btn_save_view.clicked.connect(self.save_view_requested)
        self.btn_load_view = QPushButton("Загрузить", self); self.btn_load_view.setObjectName("ArchiveStatisticsOption"); self.btn_load_view.clicked.connect(self.load_view_requested)
        self.btn_delete_view = QPushButton("Удалить", self); self.btn_delete_view.setObjectName("ArchiveStatisticsOption"); self.btn_delete_view.clicked.connect(self.delete_view_requested)
        title = QLabel("Сохранённые наборы", views_section); title.setObjectName("AnalyticsSectionTitle")
        views.addWidget(title, 0, 0)
        for column, item in enumerate((self.view_name, self.saved_views, self.btn_save_view, self.btn_load_view, self.btn_delete_view)): views.addWidget(item, 1, column)
        views.setColumnStretch(0, 2); views.setColumnStretch(1, 2)
        outer.addWidget(views_section)

        self._comparison_period_labels = self.findChildren(QLabel, "AnalyticsComparisonPeriodLabel")
        for combo in (self.metric_selector, self.cohort_field, self.cohort_operator, self.comparison_mode, self.saved_views):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(12 if combo is not self.metric_selector else 24)
            combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.cohort_field.setMinimumWidth(165)
        self.cohort_operator.setMinimumWidth(175)
        self.cohort_value.setMinimumWidth(160)
        for field in (self.cohort_value, self.view_name):
            field.setMinimumWidth(0)
            field.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.comparison_mode.currentIndexChanged.connect(self._comparison_mode_changed)
        self._comparison_mode_changed(self.comparison_mode.currentIndex())

    def set_scope(self, scope: MetricScope):
        self.scope = scope
        fields = (("Пол", "sex"), ("Возраст", "age"), ("Диагноз / МКБ", "diagnosis"), ("Отделение-источник", "source_department"), ("Исход", "outcome"), ("Койка пробуждения", "recovery_bed_stay")) if scope is MetricScope.RAO else (("Операционная / стол", "table_code"), ("Статус", "status"), ("Срочность", "priority"), ("Диагноз", "diagnosis"), ("Персонал", "personnel"))
        self.cohort_field.clear()
        for label, value in fields: self.cohort_field.addItem(label, value)

    def cohort_definition(self) -> CohortDefinition:
        return CohortDefinition(scope=self.scope, filters=tuple(self._filters))

    def set_cohort_definition(self, cohort: CohortDefinition):
        self._filters = list(cohort.filters)
        self._refresh_filter_label()

    def _add_filter(self):
        value = self.cohort_value.text().strip()
        if not value:
            return
        operator = self.cohort_operator.currentData()
        parsed: object = value
        if operator in {"gte", "lte"}:
            try:
                parsed = float(value)
            except ValueError:
                return
        if operator == "truthy":
            parsed = value.casefold() in {"1", "да", "true"}
        self._filters.append(CohortFilter(self.cohort_field.currentData(), operator, parsed))
        self.cohort_value.clear()
        self._refresh_filter_label()

    def _remove_filter(self):
        if self._filters:
            self._filters.pop()
        self._refresh_filter_label()

    def _refresh_filter_label(self):
        field_labels = {self.cohort_field.itemData(i): self.cohort_field.itemText(i) for i in range(self.cohort_field.count())}
        operator_labels = {self.cohort_operator.itemData(i): self.cohort_operator.itemText(i) for i in range(self.cohort_operator.count())}
        self.active_filters.setText("; ".join(f"{field_labels.get(item.field, item.field)} {operator_labels.get(item.operator, item.operator)} {item.value}" for item in self._filters) or "Все случаи")

    def _comparison_mode_changed(self, index: int):
        manual = int(index) == 1
        for widget in (*self._comparison_period_labels, self.period_b_from, self.period_b_to):
            widget.setVisible(manual)

    def _metric_changed(self, _index: int):
        metric_id = self.metric_selector.currentData()
        if self.snapshot and metric_id in self.snapshot.results:
            self.set_metric(self.snapshot.results[metric_id])
            return
        if metric_id in self._definitions:
            definition = self._definitions[metric_id]
            self.selected_metric = None
            self.selected_result.setHtml(
                f"<h3>{escape(definition.title)}</h3>"
                "<p>Рассчитываем выбранный показатель…</p>"
            )
            self.refresh_requested.emit()

    def set_registry_definitions(self, definitions):
        selected = self.metric_selector.currentData()
        self._definitions = {item.id: item for item in definitions}
        self.metric_selector.blockSignals(True); self.metric_selector.clear()
        for definition in definitions:
            group = "KPI" if definition.is_kpi else ("Графики" if definition.kind.value == "graph" else "Отчёт")
            self.metric_selector.addItem(f"[{group}] {definition.title}", definition.id)
        self.metric_selector.blockSignals(False)
        if selected:
            index = self.metric_selector.findData(selected)
            if index >= 0: self.metric_selector.setCurrentIndex(index)

    def set_snapshot(self, snapshot: StatisticsSnapshot):
        self.snapshot = snapshot
        self.set_scope(snapshot.scope)
        if not self._definitions:
            self.set_registry_definitions([result.definition for result in snapshot.results.values()])
        self._set_kpi_cards([result for result in snapshot.results.values() if result.definition.is_kpi])
        if snapshot.results: self.set_metric(snapshot.results.get(self.metric_selector.currentData()) or next(iter(snapshot.results.values())))

    def _set_kpi_cards(self, results: list[MetricResult]):
        while self.kpi_grid.count():
            item = self.kpi_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if not results:
            placeholder = QLabel("Нет KPI для выбранной области.", self.kpi_host)
            placeholder.setObjectName("AnalyticsWorkspaceText")
            self.kpi_grid.addWidget(placeholder, 0, 0)
            return
        for index, result in enumerate(results):
            card = QFrame(self.kpi_host)
            card.setObjectName("AnalyticsKpiCard")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(10, 8, 10, 8)
            card_layout.setSpacing(3)
            title = QLabel(result.definition.title, card)
            title.setObjectName("AnalyticsKpiTitle")
            title.setWordWrap(True)
            shown = self._format_value(result.value)
            value = QLabel(f"{shown} {result.definition.unit}".strip(), card)
            value.setObjectName("AnalyticsKpiValue")
            card_layout.addWidget(title)
            card_layout.addWidget(value)
            self.kpi_grid.addWidget(card, index // 3, index % 3)

    @staticmethod
    def _format_value(value) -> str:
        if value is None:
            return "н/д"
        if isinstance(value, float):
            return f"{value:.2f}"
        if isinstance(value, Mapping):
            return str(value.get("display_value") or value.get("summary") or "структурированный результат")
        if isinstance(value, (tuple, list)):
            return f"{len(value)} показателей"
        return str(value)

    def set_saved_views(self, names: list[str]):
        current = self.saved_views.currentText(); self.saved_views.clear(); self.saved_views.addItems(names)
        if current: self.saved_views.setCurrentText(current)

    def set_metric(self, result: MetricResult):
        self.selected_metric = result
        definition = result.definition
        structured_rows = tuple(row for row in result.rows if isinstance(row, Mapping))
        self.selected_result.setHtml(self._result_html(result, structured_rows))
        methodology_rows = ""
        if structured_rows and any(row.get("name") for row in structured_rows):
            items = "".join(
                f"<li><b>{escape(str(row.get('name') or 'Показатель'))}</b><br>"
                f"{escape(str(row.get('formula') or 'Формула не указана'))}</li>"
                for row in structured_rows
            )
            methodology_rows = f"<br><b>Расчёты раздела:</b><ul>{items}</ul>"
        self.methodology.setText(
            f"<b>{escape(definition.title)}</b><br>{escape(definition.description)}"
            f"{methodology_rows if methodology_rows else f'<br><b>Формула:</b> {escape(definition.formula)}'}"
            f"<br><b>Числитель:</b> {definition.numerator or '—'}"
            f"<br><b>Знаменатель:</b> {definition.denominator or '—'}"
            f"<br><b>Единица:</b> {definition.unit}"
            f"<br><b>Правило времени:</b> {definition.time_basis}"
            f"<br><b>Популяция:</b> {population_kind_label(definition.population_kind)}"
            f"<br><b>Источники:</b> {', '.join(definition.source_tables) or '—'}"
            f"<br><b>Включения:</b> {'; '.join(definition.inclusions) or '—'}"
            f"<br><b>Исключения:</b> {'; '.join(definition.exclusions) or '—'}"
            f"<br><b>Статус качества:</b> {definition.quality_status}"
        )
        self.source_cases.setRowCount(len(result.source_cases))
        for row, case in enumerate(result.source_cases):
            attrs = case.attributes
            history = str(
                attrs.get("history_number") or attrs.get("medical_history_number")
                or attrs.get("case_number") or case.local_id or "—"
            )
            patient = str(
                attrs.get("full_name") or attrs.get("patient_name")
                or attrs.get("patient_full_name") or attrs.get("fio") or "—"
            )
            started = case.started_at.strftime("%d.%m.%Y %H:%M") if case.started_at else "—"
            self.source_cases.setItem(row, 0, QTableWidgetItem(history))
            self.source_cases.setItem(row, 1, QTableWidgetItem(patient))
            self.source_cases.setItem(row, 2, QTableWidgetItem(started))
            self.source_cases.setItem(row, 3, QTableWidgetItem(case.inclusion_reason))

    def _result_html(self, result: MetricResult, rows: tuple[Mapping, ...]) -> str:
        definition = result.definition
        named_rows = tuple(row for row in rows if row.get("name"))
        if named_rows:
            body = "".join(
                "<tr>"
                f"<td>{escape(str(row.get('name') or '—'))}</td>"
                f"<td><b>{_safe_display_html(row.get('display_value') or self._row_display_value(row))}</b></td>"
                "</tr>"
                for row in named_rows
            )
            return (
                f"<h3>{escape(definition.title)}</h3>"
                "<table width='100%' cellspacing='0' cellpadding='6'>"
                "<tr><th align='left'>Показатель</th><th align='left'>Значение</th></tr>"
                f"{body}</table>"
            )
        shown = escape(self._format_value(result.value))
        unit = escape(definition.unit)
        context = ""
        if result.numerator is not None or result.denominator is not None:
            context = (
                f"<p>Числитель: {escape(str(result.numerator if result.numerator is not None else '—'))}"
                f" · Знаменатель: {escape(str(result.denominator if result.denominator is not None else '—'))}</p>"
            )
        return f"<h3>{escape(definition.title)}</h3><p><b>{shown} {unit}</b></p>{context}"

    @staticmethod
    def _row_display_value(row: Mapping) -> str:
        value = row.get("value")
        unit = str(row.get("unit") or "").strip()
        return f"{value if value is not None else '—'} {unit}".strip()
