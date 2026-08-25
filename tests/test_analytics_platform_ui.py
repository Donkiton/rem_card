import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from rem_card.ui.archive_center.analytics_workspace import AnalyticsWorkspace
from rem_card.ui.archive_center.archive_main_widget import ArchiveMainWidget
from rem_card.services.analytics.platform import CohortDefinition, CohortFilter, MetricScope, SavedAnalyticsView, SavedAnalyticsViewStore
from rem_card.services.analytics.multi_db_analytics import AnalyticsConnectionManager
from rem_card.ui.archive_center.graphs_page import ArchiveGraphsPage
import sqlite3
import threading
import pytest


def test_workspace_has_stable_drillthrough_modes():
    app = QApplication.instance() or QApplication([])
    widget = AnalyticsWorkspace(); widget.resize(900, 220); widget.show(); app.processEvents()
    try:
        assert tuple(widget.mode_buttons) == ("Результат", "Как рассчитано", "Какие пациенты вошли")
        assert widget.source_cases.columnCount() == 4
        assert [widget.source_cases.horizontalHeaderItem(column).text() for column in range(4)] == [
            "Номер истории болезни", "ФИО", "Дата", "Основание включения",
        ]
    finally: widget.close()


def test_clinical_scenario_workspace_builds_kpi_cards_and_metric_detail():
    from datetime import datetime
    from rem_card.services.analytics.platform import AnalyticsEngine, AnalyticsPeriod, SourceCase

    class Repo:
        def fingerprints(self): return ("clinical-ui",)
        def source_cases(self, scope, _period):
            return (SourceCase("ui", "1", scope, datetime(2026, 1, 2), {}),)

    app = QApplication.instance() or QApplication([])
    widget = AnalyticsWorkspace()
    widget.set_registry_definitions(AnalyticsEngine(Repo()).registry.for_scope(MetricScope.RAO))
    snapshot = AnalyticsEngine(Repo()).snapshot(
        MetricScope.RAO,
        AnalyticsPeriod.from_values("2026-01-01", "2026-01-03"),
        metric_ids=("rao.admissions", "rao.bed_days"),
    )
    try:
        widget.set_snapshot(snapshot)
        app.processEvents()
        assert widget.kpi_grid.count() == 2
        assert "Госпитализации" in widget.selected_result.toPlainText()
        assert tuple(widget.mode_buttons) == ("Результат", "Как рассчитано", "Какие пациенты вошли")
        assert widget.btn_update.text() == "Рассчитать"
    finally:
        widget.close(); app.processEvents()


def test_structured_result_and_source_cases_are_rendered_without_python_repr():
    from datetime import datetime
    from rem_card.services.analytics.platform import MetricResult, SourceCase, default_metric_registry

    app = QApplication.instance() or QApplication([])
    widget = AnalyticsWorkspace()
    rows = (
        {
            "name": "1.1 Госпитализации",
            "formula": "Число госпитализаций",
            "value": "9",
            "unit": "случаев",
            "display_value": "9 случаев",
        },
        {
            "name": "1.2 Койко-дни",
            "formula": "Сумма пересечения пребывания с выбранным периодом",
            "value": "331.18",
            "unit": "койко-дня",
            "display_value": "331.18 койко-дня<br/>75+: 1/3 (33.3%)<b></b>",
        },
    )
    case = SourceCase(
        "main",
        "42",
        MetricScope.RAO,
        datetime(2026, 8, 24, 9, 30),
        {"history_number": "ИБ-42", "full_name": "Иванов Иван Иванович"},
        "Госпитализация пересекает выбранный период.",
    )
    result = MetricResult(
        default_metric_registry().get("s1"),
        rows,
        denominator=1,
        source_cases=(case,),
        rows=rows,
    )
    try:
        widget.set_metric(result)
        app.processEvents()
        plain_result = widget.selected_result.toPlainText()
        assert "1.1 Госпитализации" in plain_result
        assert "9 случаев" in plain_result
        assert "1.2 Койко-дни" in plain_result
        assert "331.18 койко-дня" in plain_result
        assert "75+: 1/3 (33.3%)" in plain_result
        assert "<br" not in plain_result and "<b>" not in plain_result
        assert "{'name'" not in plain_result
        assert "'formula':" not in plain_result
        assert "Число госпитализаций" in widget.methodology.text()
        assert widget.source_cases.rowCount() == 1
        assert [widget.source_cases.item(0, column).text() for column in range(4)] == [
            "ИБ-42",
            "Иванов Иван Иванович",
            "24.08.2026 09:30",
            "Госпитализация пересекает выбранный период.",
        ]
    finally:
        widget.close(); app.processEvents()


def test_selecting_metric_missing_from_snapshot_starts_calculation_instead_of_showing_nd():
    from datetime import datetime
    from rem_card.services.analytics.platform import AnalyticsEngine, AnalyticsPeriod, SourceCase, default_metric_registry

    class Repo:
        def fingerprints(self): return ("metric-switch",)
        def source_cases(self, scope, _period):
            return (SourceCase("ui", "1", scope, datetime(2026, 1, 2), {}),)

    app = QApplication.instance() or QApplication([])
    registry = default_metric_registry()
    engine = AnalyticsEngine(Repo(), registry)
    definitions = (registry.get("g1"), registry.get("g2"))
    period = AnalyticsPeriod.from_values("2026-01-01", "2026-01-03")
    widget = AnalyticsWorkspace()
    widget.set_registry_definitions(definitions)
    widget.set_snapshot(engine.snapshot(MetricScope.RAO, period, metric_ids=("g1",)))
    refreshes = []
    widget.refresh_requested.connect(lambda: refreshes.append(widget.metric_selector.currentData()))
    try:
        widget.metric_selector.setCurrentIndex(widget.metric_selector.findData("g2"))
        app.processEvents()
        assert refreshes == ["g2"]
        assert widget.selected_metric is None
        assert "Рассчитываем выбранный показатель" in widget.selected_result.toPlainText()
        assert "н/д" not in widget.selected_result.toPlainText()

        widget.set_snapshot(engine.snapshot(MetricScope.RAO, period, metric_ids=("g2",)))
        app.processEvents()
        assert widget.selected_metric is not None
        assert widget.selected_metric.definition.id == "g2"
        assert "н/д" not in widget.selected_result.toPlainText()
    finally:
        widget.close(); app.processEvents()


def test_archive_statistics_workspace_is_reachable_at_target_geometries():
    class PatientService:
        def get_archived_patients_page(self, **_kwargs): return {"records": [], "total_count": 0, "page": 1, "page_size": 50}
    class OperblockService:
        db = object()
        def list_archived_operation_cases_page(self, **_kwargs): return {"records": [], "total_count": 0, "page": 1, "page_size": 50}
    app = QApplication.instance() or QApplication([])
    for width, height in ((1280, 720), (1024, 640)):
        widget = ArchiveMainWidget(PatientService(), operblock_service=OperblockService())
        widget.resize(width, height); widget.show(); app.processEvents()
        try:
            widget.select_destination(3); app.processEvents()
            workspace = widget.rao_statistics.analytics_workspace
            assert workspace.isVisible() and workspace.width() > 0
            assert widget.rao_analysis.isAncestorOf(workspace)
            assert all(button.isVisible() for button in workspace.mode_buttons.values())
            assert workspace.source_cases.horizontalHeader().count() == 4
            controls = (workspace.metric_selector, workspace.cohort_field, workspace.cohort_operator, workspace.cohort_value, workspace.comparison_mode, workspace.btn_update, workspace.btn_compare, workspace.view_name, workspace.saved_views, workspace.btn_save_view, workspace.btn_load_view, workspace.btn_delete_view)
            assert all(control.isVisible() and control.geometry().right() <= workspace.width() for control in controls)
            longest_operator = max(
                (workspace.cohort_operator.itemText(index) for index in range(workspace.cohort_operator.count())),
                key=len,
            )
            required_operator_width = workspace.cohort_operator.fontMetrics().horizontalAdvance(longest_operator) + 50
            assert workspace.cohort_operator.width() >= max(175, required_operator_width)
            assert workspace.cohort_operator.geometry().right() < workspace.cohort_value.geometry().left()
            style = widget.styleSheet()
            assert "QComboBox#AnalyticsCohortOperator::down-arrow" in style
            assert "QComboBox#AnalyticsCohortOperator QAbstractItemView" in style
            assert not workspace.period_b_from.isVisible() and not workspace.period_b_to.isVisible()
            workspace.comparison_mode.setCurrentIndex(1); app.processEvents()
            assert workspace.period_b_from.isVisible() and workspace.period_b_to.isVisible()
            assert workspace.period_b_to.geometry().right() <= workspace.width()
        finally: widget.close()


def test_statistics_refresh_syncs_period_cohort_and_recovery_with_graphs():
    class PatientService:
        def get_archived_patients_page(self, **_kwargs): return {"records": [], "total_count": 0, "page": 1, "page_size": 50}
    class OperblockService:
        db = object()
        def list_archived_operation_cases_page(self, **_kwargs): return {"records": [], "total_count": 0, "page": 1, "page_size": 50}
    app = QApplication.instance() or QApplication([]); widget = ArchiveMainWidget(PatientService(), operblock_service=OperblockService()); widget.show(); app.processEvents()
    try:
        page = widget.rao_statistics; page.analytics_workspace._filters = [CohortFilter("sex", "equals", "ж")]
        page.chk_include_recovery.setChecked(True); page.date_from.setDate(page.date_from.date().addDays(1)); page.analytics_workspace.refresh_requested.emit(); app.processEvents()
        assert widget.rao_graphs.analytics_cohort.filters == (CohortFilter("sex", "equals", "ж"),)
        assert widget.rao_graphs.analytics_context_period == (page.date_from.date().toString("yyyy-MM-dd"), page.date_to.date().toString("yyyy-MM-dd"))
        assert widget.rao_graphs.analytics_context_recovery
    finally: widget.close()


def test_analysis_refresh_is_independent_from_legacy_report_selection(monkeypatch):
    from rem_card.ui.archive_center.statistics_page import ArchiveStatisticsPage
    app = QApplication.instance() or QApplication([])
    page = ArchiveStatisticsPage(source_mode="rao")
    calls = []
    try:
        page._deselect_all()
        monkeypatch.setattr(page, "_start_request", lambda kind, selected: calls.append((kind, selected)))
        page.analytics_workspace.refresh_requested.emit()
        app.processEvents()
        assert calls == [("analysis", [])]
    finally:
        page.close(); app.processEvents()


def test_loading_saved_rao_view_emits_common_graph_context_refresh(tmp_path):
    class PatientService:
        def get_archived_patients_page(self, **_kwargs): return {"records": [], "total_count": 0, "page": 1, "page_size": 50}
    class OperblockService:
        db = object()
        def list_archived_operation_cases_page(self, **_kwargs): return {"records": [], "total_count": 0, "page": 1, "page_size": 50}
    app = QApplication.instance() or QApplication([])
    widget = ArchiveMainWidget(PatientService(), operblock_service=OperblockService()); widget.show(); app.processEvents()
    try:
        page = widget.rao_statistics; store = SavedAnalyticsViewStore(tmp_path / "views.json")
        cohort = CohortDefinition("женщины", MetricScope.RAO, (CohortFilter("sex", "equals", "ж"),), True)
        store.save((SavedAnalyticsView("вид", MetricScope.RAO, cohort, ("s1",), selected_metric_id="g33"),))
        page.analytics_view_store = store; page._refresh_saved_views(); page.analytics_workspace.saved_views.setCurrentText("вид")
        page._load_analytics_view(); app.processEvents()
        assert widget.rao_graphs.analytics_cohort.filters == cohort.filters
        assert widget.rao_graphs.analytics_context_recovery is True
    finally:
        widget.close(); app.processEvents()


def test_saved_view_widget_roundtrip_restores_period_a_and_manual_b(tmp_path):
    from rem_card.ui.archive_center.statistics_page import ArchiveStatisticsPage
    app = QApplication.instance() or QApplication([]); page = ArchiveStatisticsPage(source_mode="rao")
    try:
        page.analytics_view_store = SavedAnalyticsViewStore(tmp_path / "views.json")
        page.date_from.setDate(page.date_from.date().fromString("2026-03-01", "yyyy-MM-dd")); page.date_to.setDate(page.date_to.date().fromString("2026-03-10", "yyyy-MM-dd"))
        ws = page.analytics_workspace; ws.view_name.setText("A/B"); ws.comparison_mode.setCurrentIndex(1)
        ws.period_b_from.setDate(ws.period_b_from.date().fromString("2025-03-01", "yyyy-MM-dd")); ws.period_b_to.setDate(ws.period_b_to.date().fromString("2025-03-10", "yyyy-MM-dd"))
        page._save_analytics_view(); page.date_from.setDate(page.date_from.date().addYears(-3)); ws.comparison_mode.setCurrentIndex(0)
        page._refresh_saved_views(); ws.saved_views.setCurrentText("A/B"); page._load_analytics_view(); app.processEvents()
        assert page.date_from.date().toString("yyyy-MM-dd") == "2026-03-01" and ws.comparison_mode.currentIndex() == 1
        assert ws.period_b_to.date().toString("yyyy-MM-dd") == "2025-03-10"
    finally: page.close(); app.processEvents()


def test_statistics_page_comparison_uses_b_only_archive_discovery(monkeypatch, tmp_path):
    from rem_card.ui.archive_center import statistics_page as module
    from rem_card.services.analytics.platform import MetricResult, default_metric_registry
    app = QApplication.instance() or QApplication([]); paths = []
    for year in (2026, 2025):
        path = tmp_path / f"{year}.db"; paths.append(str(path)); conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE admissions(id INTEGER, admission_datetime TEXT, unit_scope TEXT)"); conn.execute("INSERT INTO admissions VALUES (1, ?, 'rao')", (f"{year}-01-02",)); conn.commit(); conn.close()
    page = module.ArchiveStatisticsPage(source_mode="rao")
    try:
        page.date_from.setDate(page.date_from.date().fromString("2026-01-01", "yyyy-MM-dd")); page.date_to.setDate(page.date_to.date().fromString("2026-01-03", "yyyy-MM-dd"))
        page.analytics_workspace.selected_metric = MetricResult(default_metric_registry().get("g1"), {})
        page.analytics_workspace.comparison_mode.setCurrentIndex(1)
        page.analytics_workspace.period_b_from.setDate(page.analytics_workspace.period_b_from.date().fromString("2025-01-01", "yyyy-MM-dd")); page.analytics_workspace.period_b_to.setDate(page.analytics_workspace.period_b_to.date().fromString("2025-01-03", "yyyy-MM-dd"))
        monkeypatch.setattr(module, "get_analytics_base_manager", lambda **_kwargs: None)
        page._archive_db_paths = lambda start, _end: [paths[0] if start.startswith("2026") else paths[1]]
        page._compare_selected_metric()
        assert page._comparison_worker.wait(5000)
        app.processEvents()
        comparison_text = page.analytics_workspace.kpi.toPlainText()
        assert "A:" in comparison_text and "B:" in comparison_text
    finally: page.close(); app.processEvents()


def test_statistics_comparison_calculation_does_not_block_gui_thread(monkeypatch):
    from rem_card.ui.archive_center import statistics_page as module
    from rem_card.services.analytics.platform import MetricResult, default_metric_registry
    app = QApplication.instance() or QApplication([])
    page = module.ArchiveStatisticsPage(source_mode="rao")
    started = threading.Event()
    release = threading.Event()

    def blocking_comparison(**_kwargs):
        started.set()
        release.wait(2)
        return "готово"

    try:
        page.analytics_workspace.selected_metric = MetricResult(default_metric_registry().get("g1"), {})
        monkeypatch.setattr(module, "get_analytics_base_manager", lambda **_kwargs: None)
        monkeypatch.setattr(page, "_archive_db_paths", lambda *_args: [])
        monkeypatch.setattr(page, "_build_comparison_message", blocking_comparison)
        page._compare_selected_metric()
        assert started.wait(1)
        assert page._comparison_worker is not None and page._comparison_worker.isRunning()
        assert not page.analytics_workspace.btn_compare.isEnabled()
        release.set()
        assert page._comparison_worker.wait(5000)
        app.processEvents()
        assert page.analytics_workspace.kpi.toPlainText() == "готово"
        assert page.analytics_workspace.btn_compare.isEnabled()
    finally:
        release.set()
        page.close()
        app.processEvents()


def test_statistics_report_race_rejects_result_after_filters_change():
    from rem_card.ui.archive_center.statistics_page import ArchiveStatisticsPage
    app = QApplication.instance() or QApplication([])
    page = ArchiveStatisticsPage(source_mode="rao")
    try:
        signature = page._selection_signature()
        original_html = page._latest_report_html
        page.date_to.setDate(page.date_to.date().addDays(-1))
        page._result_ready(
            page._request_token,
            ("report", "<html><body>устаревший отчёт</body></html>", None, ""),
            signature,
        )
        assert page._latest_report_html == original_html
        assert "устаревший отчёт" not in page.report.toPlainText()
        assert "Фильтры изменены" in page.status.text()
    finally:
        page.close(); app.processEvents()


def test_graph_race_rejects_result_and_cleans_png_after_filters_change(tmp_path):
    app = QApplication.instance() or QApplication([])
    page = ArchiveGraphsPage()
    png = tmp_path / "graph_changed_filters.png"
    png.write_bytes(b"temporary")
    result = type("Result", (), {"html": "<html>устаревший график</html>", "image_paths": [str(png)]})()
    try:
        signature = page._signature()
        page.date_to.setDate(page.date_to.date().addDays(-1))
        page._graphs_ready(page._request_token, result, False, signature)
        assert page._latest_html == ""
        assert "устаревший график" not in page.report.toPlainText()
        assert not png.exists()
        assert "Фильтры изменены" in page.status.text()
    finally:
        page.close(); app.processEvents()


def test_comparison_race_rejects_result_after_filters_change():
    from rem_card.ui.archive_center.statistics_page import ArchiveStatisticsPage
    app = QApplication.instance() or QApplication([])
    page = ArchiveStatisticsPage(source_mode="rao")
    try:
        signature = page._selection_signature()
        original_kpi = page.analytics_workspace.kpi.toPlainText()
        page.analytics_workspace.btn_compare.setEnabled(False)
        page.date_to.setDate(page.date_to.date().addDays(-1))
        page._comparison_ready(page._comparison_token, "устаревшее сравнение", signature)
        assert page.analytics_workspace.kpi.toPlainText() == original_kpi
        assert page.analytics_workspace.btn_compare.isEnabled()
        assert "Фильтры изменены" in page.status.text()
    finally:
        page.close(); app.processEvents()


def test_output_signatures_separate_cohort_and_comparison_configuration():
    from rem_card.ui.archive_center.statistics_page import ArchiveStatisticsPage
    app = QApplication.instance() or QApplication([]); page = ArchiveStatisticsPage(source_mode="rao"); graphs = ArchiveGraphsPage()
    try:
        base = page._selection_signature(); graph_base = graphs._signature()
        page.analytics_workspace._filters = [CohortFilter("sex", "equals", "ж")]
        graphs.set_cohort_definition(CohortDefinition(scope=MetricScope.RAO, filters=(CohortFilter("sex", "equals", "ж"),)))
        assert page._selection_signature() != base and graphs._signature() != graph_base
        stable = page._selection_signature(); assert page._selection_signature() == stable
        page.analytics_workspace.comparison_mode.setCurrentIndex(1); page.analytics_workspace.period_b_from.setDate(page.analytics_workspace.period_b_from.date().addDays(-1))
        assert page._selection_signature() != stable
    finally: page.close(); graphs.close(); app.processEvents()


def test_graph_html_consumes_authoritative_artifact_and_preserves_source_ids(monkeypatch):
    from rem_card.services.analytics import graphs_service
    conn = sqlite3.connect(":memory:"); conn.execute("CREATE TABLE admissions(id INTEGER, patient_id INTEGER, admission_datetime TEXT, transfer_datetime TEXT, death_datetime TEXT, outcome TEXT, patient_age REAL, patient_age_unit TEXT, patient_gender TEXT, source_department TEXT, diagnosis_code TEXT, diagnosis_text TEXT, bed_number INTEGER)"); conn.execute("INSERT INTO admissions VALUES (1, 1, '2026-01-02', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)"); conn.commit()
    manager = AnalyticsConnectionManager(conn, db_path="artifact-html")
    monkeypatch.setattr(graphs_service, "_load_generators", lambda: (_ for _ in ()).throw(AssertionError("legacy SQL renderer must not run for artifact")))
    artifact = {"metric_id": "g1", "summary": "1. Поступления: 1 случаев", "source_case_ids": ("db:rao:1",), "series": ({"source_case_id": "db:rao:1", "value": 1},)}
    result = graphs_service.build_graphs_html(manager, "2026-01-01", "2026-01-03", ["g1"], authoritative_artifacts={"g1": artifact})
    assert result.artifacts["g1"] == artifact
    assert "Поступления: 1" in result.html and "db:rao:1" in result.html
    conn.close()


def test_ratio_graph_uses_same_display_value_in_artifact_html_and_ab(monkeypatch):
    from datetime import datetime
    from rem_card.services.analytics import graphs_service
    from rem_card.services.analytics.platform import AnalyticsEngine, AnalyticsPeriod, SourceCase
    from rem_card.ui.analytics import graphs_generators_1
    from rem_card.ui.archive_center.statistics_page import ArchiveStatisticsPage

    class Repo:
        def fingerprints(self): return ("ratio-summary",)
        def source_cases(self, scope, period):
            count = 4 if period.start.year == 2026 else 2
            return tuple(SourceCase(
                "ratio", str(index), scope, datetime(period.start.year, 1, 2),
                {"transfer_datetime": f"{period.start.year}-01-03"},
            ) for index in range(count))

    comparison = AnalyticsEngine(Repo()).compare(
        MetricScope.RAO,
        "g9",
        AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 2, 1)),
        comparison_period=AnalyticsPeriod(datetime(2025, 1, 1), datetime(2025, 2, 1)),
    )
    assert comparison.current.rows == ({"label": "2026-01", "value": 1.0},)
    assert comparison.current.artifact["summary"].endswith(": 1 пациентов на койку")
    assert comparison.previous.artifact["summary"].endswith(": 0.5 пациентов на койку")
    message = ArchiveStatisticsPage._structured_comparison_message(comparison)
    assert ": 1 пациентов на койку" in message and ": 0.5 пациентов на койку" in message

    monkeypatch.setattr(graphs_generators_1, "save_plot", lambda title, _paths: f"<figure>{title}</figure>")
    artifact = comparison.current.artifact
    rendered = graphs_service.build_graphs_html(
        object(), "2026-01-01", "2026-01-31", ["g9"],
        authoritative_artifacts={"g9": artifact},
    )
    assert artifact["summary"] in rendered.html


def test_statistics_ui_comparison_rejects_carry_in_only_previous_population():
    from datetime import datetime
    from rem_card.services.analytics.platform import AnalyticsPeriod
    from rem_card.ui.archive_center.statistics_page import ArchiveStatisticsPage

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE admissions(id INTEGER, patient_id INTEGER, admission_datetime TEXT, "
        "transfer_datetime TEXT, recovery_bed_stay INTEGER)"
    )
    conn.executemany("INSERT INTO admissions VALUES (?, ?, ?, ?, 0)", (
        (1, 1, "2024-12-31", "2025-01-03"),
        (2, 2, "2026-01-02", "2026-01-03"),
    ))
    conn.commit()
    manager = AnalyticsConnectionManager(conn, db_path="carry-in-ui")
    app = QApplication.instance() or QApplication([])
    page = ArchiveStatisticsPage(source_mode="rao")
    try:
        message = page._build_comparison_message(
            source=manager,
            current_paths=[],
            previous_paths=[],
            scope=MetricScope.RAO,
            cohort=CohortDefinition(scope=MetricScope.RAO),
            current_period=AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 2, 1)),
            previous_period=AnalyticsPeriod(datetime(2025, 1, 1), datetime(2025, 2, 1)),
            metric_id="g1",
            manual=False,
        )
        assert message == "Нет данных за предыдущий год"
    finally:
        page.close()
        app.processEvents()
        conn.close()


def test_kpi_result_ui_shows_real_mortality_aggregates():
    from datetime import datetime
    from rem_card.services.analytics.platform import AnalyticsEngine, AnalyticsPeriod, SourceCase

    cases = tuple(
        SourceCase(
            "mortality-ui", str(index), MetricScope.RAO, datetime(2026, 1, 1),
            {"death_datetime": "2026-01-02"} if index == 0 else {"transfer_datetime": "2026-01-02"},
        )
        for index in range(5)
    )

    class Repo:
        def fingerprints(self): return ("mortality-ui",)
        def source_cases(self, scope, _period): return cases

    result = AnalyticsEngine(Repo()).snapshot(
        MetricScope.RAO,
        AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 1, 3)),
        metric_ids=("rao.mortality",),
    ).results["rao.mortality"]
    app = QApplication.instance() or QApplication([])
    widget = AnalyticsWorkspace()
    try:
        widget.set_metric(result)
        app.processEvents()
        plain = widget.selected_result.toPlainText()
        assert "20.00 %" in plain
        assert "Числитель: 1 · Знаменатель: 5" in plain
    finally:
        widget.close()
        app.processEvents()


def test_operblock_ui_comparison_rejects_period_without_computable_durations():
    from datetime import datetime
    from rem_card.services.analytics.platform import AnalyticsPeriod
    from rem_card.ui.archive_center.statistics_page import ArchiveStatisticsPage

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE operation_cases(id INTEGER, patient_id INTEGER, started_at TEXT, "
        "ended_at TEXT, status TEXT, is_deleted INTEGER)"
    )
    conn.executemany("INSERT INTO operation_cases VALUES (?, ?, ?, ?, 'completed', 0)", (
        (1, 1, "2025-01-02 08:00", None),
        (2, 2, "2026-01-02 08:00", "2026-01-02 09:00"),
    ))
    conn.commit()
    manager = AnalyticsConnectionManager(conn, db_path="oper-duration-ui")
    app = QApplication.instance() or QApplication([])
    page = ArchiveStatisticsPage(source_mode="operblock")
    try:
        message = page._build_comparison_message(
            source=manager,
            current_paths=[],
            previous_paths=[],
            scope=MetricScope.OPERBLOCK,
            cohort=CohortDefinition(scope=MetricScope.OPERBLOCK),
            current_period=AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 2, 1)),
            previous_period=AnalyticsPeriod(datetime(2025, 1, 1), datetime(2025, 2, 1)),
            metric_id="operblock.average_room_duration",
            manual=False,
        )
        assert message == "Нет данных за предыдущий год"
    finally:
        page.close()
        app.processEvents()
        conn.close()


def test_graph_rendering_finishes_in_background_thread_with_agg_backend():
    import warnings
    from rem_card.services.analytics.graphs_service import build_graphs_html

    artifact = {
        "metric_id": "g1",
        "title": "Поступления",
        "summary": "Поступления: 1",
        "unit": "случаев",
        "chart_kind": "bar",
        "series": ({"label": "2026-01", "value": 1},),
        "source_case_ids": (),
    }
    outcome = {}

    def render():
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                outcome["result"] = build_graphs_html(
                    object(),
                    "2026-01-01",
                    "2026-01-03",
                    ["g1"],
                    authoritative_artifacts={"g1": artifact},
                )
            outcome["warnings"] = tuple(str(item.message) for item in caught)
            import matplotlib
            outcome["backend"] = str(matplotlib.get_backend())
        except Exception as error:
            outcome["error"] = error

    worker = threading.Thread(target=render, daemon=True)
    worker.start(); worker.join(10)
    try:
        assert not worker.is_alive(), "Фоновый рендер Matplotlib завис"
        assert "error" not in outcome
        assert outcome["backend"].casefold() == "agg"
        assert not any("Matplotlib GUI outside of the main thread" in item for item in outcome["warnings"])
        assert len(outcome["result"].image_paths) == 1
    finally:
        for path in getattr(outcome.get("result"), "image_paths", ()):
            try: os.remove(path)
            except OSError: pass


def test_long_daily_graph_trims_empty_years_and_limits_axis_labels():
    from datetime import date, timedelta
    from rem_card.services.analytics.graphs_service import (
        _format_axis_label,
        _sparse_tick_indexes,
        _trim_empty_time_edges,
    )

    start = date(2000, 1, 1)
    labels = [(start + timedelta(days=index)).isoformat() for index in range(9700)]
    values = [0.0] * 9695 + [1.0, 2.0, 3.0, 4.0, 1.0]

    visible_labels, visible_values = _trim_empty_time_edges(labels, values)
    ticks = _sparse_tick_indexes(len(visible_labels))

    assert visible_values == [0.0, 1.0, 2.0, 3.0, 4.0, 1.0]
    assert len(ticks) <= 10
    assert ticks[0] == 0 and ticks[-1] == len(visible_labels) - 1
    assert _format_axis_label("2026-08-24") == "24.08.2026"
    assert _format_axis_label("2026-08") == "08.2026"


def test_statistics_and_graph_pdf_html_carry_canonical_analytics_context(monkeypatch, tmp_path):
    """PDF consumes HTML, therefore inspect the actual intermediate story input."""
    from rem_card.services.analytics import graphs_service
    from rem_card.ui.archive_center import statistics_page as statistics_module
    from rem_card.ui.archive_center.statistics_page import ArchiveStatisticsPage
    from rem_card.services.analytics.platform import AnalyticsPeriod, analytics_context_html, default_metric_registry
    registry = default_metric_registry()
    cohort = CohortDefinition("женщины", MetricScope.RAO, (CohortFilter("sex", "equals", "ж"),), True)
    context = analytics_context_html(
        period=AnalyticsPeriod.from_values("2026-01-01", "2026-01-03"), cohort=cohort,
        definitions=(registry.get("g1"),), comparison_mode="manual", comparison_period=("2025-01-01", "2025-01-03"),
        source_fingerprints=("fingerprint-a",),
    )
    assert "data-analytics-context-summary='1'" in context
    assert "data-analytics-metric-card='1'" in context
    assert "data-analytics-definition-table='1'" in context
    assert all(label in context for label in ("Формула", "Числитель", "Знаменатель", "Единица измерения", "Правило времени", "Популяция", "Источники данных"))
    assert "формула —" not in context
    db_path = tmp_path / "pdf-context.db"
    conn = sqlite3.connect(db_path)
    manager = AnalyticsConnectionManager(conn, db_path=str(db_path))
    try:
        result = graphs_service.build_graphs_html(
            manager, "2026-01-01", "2026-01-03", ["g1"],
            authoritative_artifacts={"g1": {"title": "Поступления", "summary": "Поступления: 1", "unit": "случаев", "series": ({"label": "2026-01", "value": 1},), "source_case_ids": ()}},
            analytics_context=context,
        )
        graph_story = " ".join(item.value for item in graphs_service._parse_graphs_pdf_items(result.html))
        assert "Контекст аналитического отчёта" in graph_story and "Ручной A/B" in graph_story
        assert "g1" in graph_story and "fingerprint-a" in graph_story and "Пол равно ж" in graph_story

        app = QApplication.instance() or QApplication([])
        page = ArchiveStatisticsPage(source_mode="rao")
        monkeypatch.setattr(statistics_module, "get_analytics_base_manager", lambda **_kwargs: manager)
        monkeypatch.setattr(statistics_module, "resolve_readonly_analytics_manager", lambda *_args, **_kwargs: (manager, None))
        monkeypatch.setattr(statistics_module, "materialize_cohort_snapshot", lambda manager, *_args, **_kwargs: (manager, ()))
        monkeypatch.setattr(statistics_module, "build_detailed_statistics_report_html", lambda *_args, **_kwargs: "<html><body><h2>Отчёт</h2></body></html>")
        try:
            from rem_card.services.analytics.platform import StatisticsRepository
            fingerprint = StatisticsRepository(manager).clinical_fingerprints()[0]
            html = page._build_report(
                "2026-01-01", "2026-01-03", [], ["s1"], True, cohort,
                "manual", ("2025-01-01", "2025-01-03"),
                source_fingerprints=(fingerprint,),
            )
            assert "Контекст аналитического отчёта" in html and "Ручной A/B" in html
            assert "s1" in html and "Формула" in html and "Популяция" in html
            assert fingerprint in html and "Отпечатки источников" in html
        finally:
            page.close(); app.processEvents()
    finally:
        conn.close()


def test_stale_graph_worker_result_removes_unregistered_png(tmp_path):
    app = QApplication.instance() or QApplication([])
    page = ArchiveGraphsPage()
    png = tmp_path / "graph_stale.png"
    png.write_bytes(b"temporary")
    result = type("Result", (), {"html": "<html/>", "image_paths": [str(png)]})()
    token = page._request_token
    try:
        page.shutdown()
        page._graphs_ready(token, result, False)
        assert not png.exists()
        assert str(png) not in page._temp_graph_paths
    finally:
        page.close(); app.processEvents()


def test_graph_worker_disposes_png_when_success_signal_cannot_be_delivered(monkeypatch, tmp_path):
    from rem_card.ui.shared.async_call import AsyncCallThread
    png = tmp_path / "graph_orphan.png"
    png.write_bytes(b"temporary")
    result = type("Result", (), {"image_paths": [str(png)]})()
    worker = AsyncCallThread(
        lambda: result,
        result_disposer=lambda payload: ArchiveGraphsPage._discard_result_image_paths(payload.image_paths),
    )

    def deleted_receiver(_result):
        raise RuntimeError("wrapped C/C++ object has been deleted")

    monkeypatch.setattr(worker, "_emit_success", deleted_receiver)
    worker._run_wrapper()
    assert not png.exists()


def test_g63_uses_dynamic_grid_for_more_than_six_departments(monkeypatch):
    from rem_card.ui.analytics import graphs_generators_3 as generators
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE admissions(id INTEGER, patient_id INTEGER, admission_datetime TEXT, transfer_datetime TEXT, death_datetime TEXT, outcome TEXT, patient_age REAL, patient_age_unit TEXT, patient_gender TEXT, source_department TEXT, diagnosis_code TEXT, diagnosis_text TEXT, bed_number INTEGER)")
    conn.executemany("INSERT INTO admissions VALUES (?, ?, '2026-01-01', '2026-01-02', NULL, NULL, NULL, NULL, NULL, ?, NULL, NULL, NULL)", [(index, index, f"Отделение {index}") for index in range(1, 8)]); conn.commit()
    calls = []; monkeypatch.setattr(generators.plt, "subplot", lambda rows, columns, index: calls.append((rows, columns, index)))
    monkeypatch.setattr(generators, "save_plot", lambda *_args: "<figure>63</figure>")
    generators.generate_g61_g65(["g63"], conn, ("2026-01-01 00:00:00", "2026-01-03 00:00:00"), ["#123"] * 8, [], "")
    assert len(calls) == 7 and all(rows == 4 and columns == 2 for rows, columns, _ in calls)
    generators.plt.close("all"); conn.close()


def test_authoritative_renderer_keeps_pie_step_histogram_and_dynamic_ward_grid(monkeypatch):
    from rem_card.services.analytics import graphs_service
    import matplotlib.pyplot as plt
    from rem_card.ui.analytics import graphs_generators_1
    calls = {"pie": 0, "step": 0, "step_x": None, "hist": 0, "subplots": []}
    originals = {name: getattr(plt, name) for name in ("pie", "step", "hist", "subplots")}
    monkeypatch.setattr(plt, "pie", lambda *args, **kwargs: (calls.__setitem__("pie", calls["pie"] + 1), originals["pie"](*args, **kwargs))[1])
    monkeypatch.setattr(plt, "step", lambda *args, **kwargs: (calls.__setitem__("step", calls["step"] + 1), calls.__setitem__("step_x", list(args[0])), originals["step"](*args, **kwargs))[2])
    monkeypatch.setattr(plt, "hist", lambda *args, **kwargs: (calls.__setitem__("hist", calls["hist"] + 1), originals["hist"](*args, **kwargs))[1])
    def record_subplots(rows, columns, *args, **kwargs):
        calls["subplots"].append((rows, columns)); return originals["subplots"](rows, columns, *args, **kwargs)
    monkeypatch.setattr(plt, "subplots", record_subplots)
    monkeypatch.setattr(graphs_generators_1, "save_plot", lambda title, _paths: f"<figure>{title}</figure>")
    artifacts = {
        "g28": {"title": "Исходы", "unit": "случаев", "chart_kind": "pie", "series": ({"label": "выписан", "value": 2}, {"label": "умер", "value": 1})},
        "g41": {"title": "KM", "unit": "доля", "chart_kind": "step", "series": ({"label": "0,5", "x": 0.5, "value": 1.0}, {"label": "2", "x": 2.0, "value": 0.5})},
        "g33": {"title": "LOS", "unit": "суток", "chart_kind": "histogram", "series": ({"label": "0–1", "value": 1}, {"label": "2–3", "value": 2})},
        "g63": {"title": "Палаты", "unit": "суток", "chart_kind": "ward_histograms", "series": tuple({"group": f"Отделение {index}", "label": "Случай", "value": index} for index in range(1, 8))},
    }
    html = graphs_service._render_authoritative_artifacts(list(artifacts), artifacts, ["#123"] * 8, [], "")
    assert "Палаты" in html and calls["pie"] == calls["step"] == 1 and calls["hist"] >= 1
    assert calls["subplots"] == [(4, 2)]
    assert calls["step_x"] == [0.5, 2.0]
    plt.close("all")


def test_authoritative_recovery_table_renders_html_without_fake_chart(monkeypatch):
    from rem_card.services.analytics import graphs_service
    from rem_card.ui.analytics import graphs_generators_1

    def fail_save_plot(*_args, **_kwargs):
        raise AssertionError("Табличный артефакт не должен отрисовываться через Matplotlib")

    monkeypatch.setattr(graphs_generators_1, "save_plot", fail_save_plot)
    artifact = {
        "title": "Пациенты через койки пробуждения (таблица)",
        "unit": "случаев",
        "chart_kind": "table",
        "series": (
            {"label": "Всего случаев", "value": 4, "unit": "случаев"},
            {"label": "Средняя длительность", "value": 11.0, "unit": "часов"},
        ),
    }

    html = graphs_service._render_authoritative_artifacts(
        ["recovery_flow_table"],
        {"recovery_flow_table": artifact},
        ["#123456"],
        [],
        "",
    )

    assert "<table" in html
    assert "Показатель" in html
    assert "Средняя длительность" in html
    assert "часов" in html


@pytest.mark.parametrize("selector", ["g1", "g20", "g33", "recovery_flow_table"])
def test_graph_renderer_receives_only_cohort_filtered_snapshot(monkeypatch, selector):
    from rem_card.ui.archive_center import graphs_page as module
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE admissions(id INTEGER, admission_datetime TEXT, patient_gender TEXT)")
    conn.executemany("INSERT INTO admissions VALUES (?, '2026-01-02', ?)", [(1, "ж"), (2, "м")]); conn.commit()
    manager = AnalyticsConnectionManager(conn, db_path="graphs-fixture")
    seen = []
    monkeypatch.setattr(module, "get_analytics_base_manager", lambda **_kwargs: manager)
    monkeypatch.setattr(module, "resolve_readonly_analytics_manager", lambda *_args, **_kwargs: (manager, None))
    def renderer(input_manager, *_args, **_kwargs):
        seen.extend(row[0] for row in input_manager.get_connection().execute("SELECT id FROM admissions"))
        return type("Result", (), {"html": "", "image_paths": []})()
    monkeypatch.setattr(module, "build_graphs_html", renderer)
    page = ArchiveGraphsPage(); page.set_cohort_definition(CohortDefinition(scope=MetricScope.RAO, filters=(CohortFilter("sex", "equals", "ж"),)))
    try:
        page._build_graphs("2026-01-01", "2026-01-03", [selector], False)
        assert seen == [1]
    finally: page.close(); conn.close()


@pytest.mark.parametrize("include_recovery, expected", [(False, [1]), (True, [1, 2])])
def test_graph_renderer_materializes_recovery_as_part_of_cohort(monkeypatch, include_recovery, expected):
    from rem_card.ui.archive_center import graphs_page as module
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE admissions(id INTEGER, admission_datetime TEXT, recovery_bed_stay INTEGER)")
    conn.executemany("INSERT INTO admissions VALUES (?, '2026-01-02', ?)", [(1, 0), (2, 1)]); conn.commit()
    manager = AnalyticsConnectionManager(conn, db_path="graphs-recovery")
    monkeypatch.setattr(module, "get_analytics_base_manager", lambda **_kwargs: manager)
    monkeypatch.setattr(module, "resolve_readonly_analytics_manager", lambda *_args, **_kwargs: (manager, None))
    seen = []
    def renderer(input_manager, *_args, **_kwargs):
        seen.extend(row[0] for row in input_manager.get_connection().execute("SELECT id FROM admissions ORDER BY id"))
        return type("Result", (), {"html": "", "image_paths": []})()
    monkeypatch.setattr(module, "build_graphs_html", renderer)
    page = ArchiveGraphsPage()
    try:
        page._build_graphs("2026-01-01", "2026-01-03", ["g1"], include_recovery)
        assert seen == expected
    finally: page.close(); conn.close()


def test_graph_artifact_keeps_file_fingerprints_for_equal_local_ids(monkeypatch, tmp_path):
    from datetime import datetime
    from rem_card.services.analytics.platform import AnalyticsEngine, AnalyticsPeriod, StatisticsRepository
    from rem_card.ui.archive_center import graphs_page as module

    paths = []
    for name, admission in (("rotation-a.db", "2026-01-02"), ("rotation-b.db", "2026-01-03")):
        path = tmp_path / name
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE admissions(id INTEGER, patient_id INTEGER, admission_datetime TEXT, "
            "transfer_datetime TEXT, recovery_bed_stay INTEGER)"
        )
        conn.execute("INSERT INTO admissions VALUES (1, 1, ?, '2026-01-04', 0)", (admission,))
        conn.commit()
        conn.close()
        paths.append(str(path))

    base_conn = sqlite3.connect(paths[0])
    base = AnalyticsConnectionManager(base_conn, db_path=paths[0])
    monkeypatch.setattr(module, "get_analytics_base_manager", lambda **_kwargs: base)
    captured = {}

    def renderer(_manager, *_args, **kwargs):
        captured.update(kwargs["authoritative_artifacts"])
        return type("Result", (), {"html": "", "image_paths": []})()

    monkeypatch.setattr(module, "build_graphs_html", renderer)
    period = AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 1, 5))
    expected = AnalyticsEngine(StatisticsRepository(db_paths=paths)).snapshot(
        MetricScope.RAO,
        period,
        CohortDefinition(scope=MetricScope.RAO),
        ("g1",),
    ).results["g1"].artifact["source_case_ids"]
    page = ArchiveGraphsPage()
    try:
        page._build_graphs(
            "2026-01-01", "2026-01-04", ["g1"], False,
            archive_paths=tuple(paths),
        )
        actual = captured["g1"]["source_case_ids"]
        assert actual == expected and len(actual) == 2
        assert len({item.split(":", 1)[0] for item in actual}) == 2
        assert all(not item.startswith(("manager:", "connection:")) for item in actual)
    finally:
        page.close()
        base_conn.close()


def test_saved_views_are_scoped_to_owning_statistics_page(tmp_path):
    from rem_card.ui.archive_center.statistics_page import ArchiveStatisticsPage
    app = QApplication.instance() or QApplication([])
    store = SavedAnalyticsViewStore(tmp_path / "views.json")
    store.save((
        SavedAnalyticsView("одинаковое", MetricScope.RAO, CohortDefinition(scope=MetricScope.RAO), ("s1",), selected_metric_id="s1"),
        SavedAnalyticsView("одинаковое", MetricScope.OPERBLOCK, CohortDefinition(scope=MetricScope.OPERBLOCK), ("ob1",), selected_metric_id="ob1"),
    ))
    rao = ArchiveStatisticsPage(source_mode="rao"); oper = ArchiveStatisticsPage(source_mode="operblock")
    try:
        for page in (rao, oper):
            page.analytics_view_store = store; page._refresh_saved_views(); page.refresh_report = lambda: None
            assert page.analytics_workspace.saved_views.count() == 1
            page.analytics_workspace.saved_views.setCurrentText("одинаковое"); page._load_analytics_view()
        assert rao.analytics_workspace.scope is MetricScope.RAO
        assert oper.analytics_workspace.scope is MetricScope.OPERBLOCK
        oper._delete_analytics_view()
        remaining = store.load()
        assert len(remaining) == 1 and remaining[0].scope is MetricScope.RAO
    finally:
        rao.close(); oper.close(); app.processEvents()


def test_statistics_report_closes_materialized_manager_on_builder_error(monkeypatch):
    from rem_card.ui.archive_center import statistics_page as module
    app = QApplication.instance() or QApplication([])
    conn = sqlite3.connect(":memory:"); base = AnalyticsConnectionManager(conn, db_path="base")
    class Spy:
        closed = False
        def close_connection(self): self.closed = True
    spy = Spy()
    monkeypatch.setattr(module, "get_analytics_base_manager", lambda **_kwargs: base)
    monkeypatch.setattr(module, "resolve_readonly_analytics_manager", lambda *_args, **_kwargs: (base, None))
    monkeypatch.setattr(module, "materialize_cohort_snapshot", lambda *_args, **_kwargs: (spy, ()))
    monkeypatch.setattr(module, "build_detailed_statistics_report_html", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("builder")))
    page = module.ArchiveStatisticsPage(source_mode="rao")
    try:
        with pytest.raises(RuntimeError, match="builder"):
            page._build_report("2026-01-01", "2026-01-03", [], ["s1"], False, CohortDefinition(scope=MetricScope.RAO))
        assert spy.closed
    finally:
        page.close(); conn.close(); app.processEvents()


def test_statistics_report_keeps_recovery_section_when_general_toggle_is_off(monkeypatch):
    import re

    from rem_card.ui.archive_center import statistics_page as module

    conn = sqlite3.connect(":memory:")
    conn.executescript("""
      CREATE TABLE admissions(id INTEGER, patient_id INTEGER, admission_datetime TEXT, transfer_datetime TEXT,
        death_datetime TEXT, outcome TEXT, patient_age REAL, patient_age_unit TEXT, patient_gender TEXT,
        source_department TEXT, diagnosis_code TEXT, diagnosis_text TEXT, bed_number INTEGER,
        recovery_bed_stay INTEGER);
      CREATE TABLE operations(id INTEGER, admission_id INTEGER, operation_datetime TEXT, description TEXT);
      CREATE TABLE transfusions(id INTEGER, admission_id INTEGER, datetime TEXT, type TEXT, volume_ml REAL);
      CREATE TABLE ivl_episodes(id INTEGER, admission_id INTEGER, start_time TEXT, end_time TEXT);
    """)
    conn.executemany(
        "INSERT INTO admissions VALUES (?, ?, '2026-01-02', '2026-01-03', NULL, 'выписан', "
        "40, 'years', 'ж', 'ПСО', 'J18', 'Тест', ?, ?)",
        ((1, 1, 1, 0), (2, 2, 101, 1)),
    )
    conn.commit()
    manager = AnalyticsConnectionManager(conn, db_path="recovery-report-ui")
    monkeypatch.setattr(module, "get_analytics_base_manager", lambda **_kwargs: manager)
    monkeypatch.setattr(module, "resolve_readonly_analytics_manager", lambda *_args, **_kwargs: (manager, None))
    app = QApplication.instance() or QApplication([])
    page = module.ArchiveStatisticsPage(source_mode="rao")
    try:
        html = page._build_report(
            "2026-01-01", "2026-01-03", [], ["s1", "s_recovery"], False,
            CohortDefinition(scope=MetricScope.RAO, include_recovery_beds=False),
        )
        assert re.search(
            r"1\.1 Госпитализации.*?<td class=\"value\"[^>]*>1</td>.*?"
            r"<td class=\"unit\"[^>]*>случай</td>",
            html,
            re.DOTALL,
        )
        assert "Проведено через пробуждение" in html
        assert "50.0%" in html and "от всех" in html
    finally:
        page.close()
        app.processEvents()
        conn.close()


def test_every_catalog_graph_has_a_production_dispatch():
    from rem_card.services.analytics.graph_catalog import GRAPH_GROUPS
    from rem_card.services.analytics.graphs_service import PRODUCTION_GRAPH_DISPATCH
    from rem_card.ui.analytics.graphs_generators_2 import CANONICAL_GRAPH_KEYS
    catalog_keys = {key for group in GRAPH_GROUPS.values() for key in group}
    assert catalog_keys == set(PRODUCTION_GRAPH_DISPATCH)
    assert CANONICAL_GRAPH_KEYS == {f"g{number}" for number in range(23, 46)}


def test_canonical_g23_g45_renderer_dispatch_produces_every_selected_artifact(monkeypatch):
    pytest.importorskip("pandas"); pytest.importorskip("matplotlib")
    from rem_card.ui.analytics import graphs_generators_2 as generators
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE admissions(id INTEGER, admission_datetime TEXT, transfer_datetime TEXT, death_datetime TEXT, outcome TEXT, patient_age REAL, patient_age_unit TEXT, patient_gender TEXT, source_department TEXT, diagnosis_code TEXT, diagnosis_text TEXT);
        CREATE TABLE ivl_episodes(id INTEGER, admission_id INTEGER, start_time TEXT, end_time TEXT);
    """)
    conn.executemany("INSERT INTO admissions VALUES (?, ?, ?, ?, ?, ?, 'years', ?, ?, ?, ?)", [
        (1, "2026-01-02 00:00:00", "2026-01-12 00:00:00", None, "выписан", 35, "м", "ПСО", "J18", "Пневмония"),
        (2, "2026-01-03 00:00:00", None, "2026-01-03 12:00:00", "умер", 70, "ж", "ХО", "K35", "Сепсис"),
        (3, "2026-01-04 00:00:00", "2026-01-24 00:00:00", None, "выписан", 50, "ж", "ПСО", "J18", "Пневмония"),
    ])
    conn.executemany("INSERT INTO ivl_episodes VALUES (?, ?, ?, ?)", [(1, 1, "2026-01-02 01:00:00", "2026-01-04 01:00:00"), (2, 2, "2026-01-03 01:00:00", "2026-01-03 11:00:00")]); conn.commit()
    def capture_plot(title, _paths):
        generators.plt.close()
        return f"<figure>{title}</figure>"
    monkeypatch.setattr(generators, "save_plot", capture_plot)
    colors = ["#123456"] * 8; params = ("2026-01-01 00:00:00", "2026-02-01 00:00:00"); selected = [f"g{number}" for number in range(23, 46)]
    html = generators.generate_g23_g30(selected, conn, params, colors, [], "")
    html = generators.generate_g31_g35(selected, conn, params, colors, [], html)
    rows = [dict(zip([column[0] for column in cursor.description], row)) for cursor in [conn.execute("SELECT * FROM admissions")] for row in cursor.fetchall()]
    html = generators.generate_g36_g40(selected, conn, params, colors, [], rows, html)
    html = generators.generate_g41_g45(selected, conn, params, colors, [], html)
    try:
        assert all(f"{number}." in html for number in range(23, 46))
        assert "Kaplan–Meier" in html and "ИВЛ" in html
    finally:
        conn.close()


def test_graph_zero_series_is_rendered_not_reported_as_no_data(monkeypatch):
    from rem_card.ui.analytics import graphs_generators_2 as generators
    captured = []
    monkeypatch.setattr(generators, "save_plot", lambda title, _paths: captured.append(title) or f"<figure>{title}</figure>")
    html = generators._bar(29, "Летальность по месяцам", ["2026-01"], [0], "#123", [], "")
    html = generators._bar(40, "Индекс тяжести", ["Ранние смерти"], [0], "#123", [], html)
    html = generators._bar(43, "Число эпизодов ИВЛ", ["Эпизоды"], [0], "#123", [], html)
    assert captured == ["29. Летальность по месяцам", "40. Индекс тяжести", "43. Число эпизодов ИВЛ"]
    generators.plt.close("all")


def test_period_clipped_death_and_carry_in_census_boundaries():
    from datetime import datetime
    from rem_card.ui.analytics.graphs_generators_1 import _calc_daily_counts
    from rem_card.ui.analytics.graphs_generators_2 import _death_mask, _duration_days
    start, end = datetime(2026, 1, 1), datetime(2026, 1, 3)
    post_period_death = {"admission_datetime": "2025-12-30", "death_datetime": "2026-01-10", "transfer_datetime": None}
    assert _duration_days(post_period_death, end, start, end) == 2
    import pandas as pd
    assert not bool(_death_mask(pd.DataFrame([{**post_period_death, "outcome": "умер"}]), end).iloc[0])
    carry_in = {"admission_datetime": "2025-12-30", "transfer_datetime": "2026-01-02 12:00:00", "death_datetime": None, "outcome": "выписан"}
    transfer_at_start = {"admission_datetime": "2025-12-30", "transfer_datetime": "2026-01-01 00:00:00", "death_datetime": None, "outcome": "выписан"}
    counts, _dates = _calc_daily_counts([carry_in, transfer_at_start], "2026-01-01", "2026-01-02")
    assert counts == [1, 1]


def test_ivl_half_open_interval_is_clipped_and_excludes_end_boundary(monkeypatch):
    from rem_card.ui.analytics import graphs_generators_2 as generators
    conn = sqlite3.connect(":memory:")
    conn.executescript("CREATE TABLE admissions(id INTEGER, admission_datetime TEXT, transfer_datetime TEXT, death_datetime TEXT, outcome TEXT, patient_age REAL, patient_age_unit TEXT, patient_gender TEXT, source_department TEXT, diagnosis_code TEXT, diagnosis_text TEXT); CREATE TABLE ivl_episodes(id INTEGER, admission_id INTEGER, start_time TEXT, end_time TEXT);")
    conn.execute("INSERT INTO admissions VALUES (1, '2026-01-01', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)")
    conn.executemany("INSERT INTO ivl_episodes VALUES (?, 1, ?, ?)", [(1, "2025-12-31 12:00:00", "2026-01-01 12:00:00"), (2, "2026-01-03 00:00:00", "2026-01-04 00:00:00")]); conn.commit()
    bars = []; monkeypatch.setattr(generators, "save_plot", lambda title, _paths: f"<figure>{title}</figure>")
    original_bar = generators._bar
    def capture(number, title, labels, values, *args): bars.append((number, list(values))); return original_bar(number, title, labels, values, *args)
    monkeypatch.setattr(generators, "_bar", capture)
    try:
        generators.generate_g41_g45(["g43", "g44", "g45"], conn, ("2026-01-01 00:00:00", "2026-01-03 00:00:00"), ["#123"] * 8, [], "")
        assert (43, [1]) in bars and any(number == 44 and values == [0.5] for number, values in bars)
    finally:
        generators.plt.close("all"); conn.close()


def test_graph_capacity_and_duration_helpers_use_calendar_and_half_open_bounds():
    from datetime import datetime
    from rem_card.ui.analytics.graphs_generators_1 import _calendar_days_by_month
    from rem_card.ui.analytics.graphs_generators_3 import _observed_duration_days
    assert _calendar_days_by_month("2024-02-27", "2024-03-02") == {"2024-02": 3, "2024-03": 2}
    row = {"admission_datetime": "2025-12-20", "transfer_datetime": "2026-01-10", "death_datetime": None}
    assert _observed_duration_days(row, datetime(2026, 1, 1), datetime(2026, 1, 3)) == 2
