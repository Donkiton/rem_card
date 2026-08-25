from datetime import datetime
import os
import pytest
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from rem_card.services.analytics.platform import CohortDefinition, CohortFilter, MetricScope, SavedAnalyticsView, SavedAnalyticsViewStore, SourceCase
from rem_card.ui.archive_center.analytics_workspace import AnalyticsWorkspace


def test_serializable_cohort_filters_scope_and_attributes():
    cohort = CohortDefinition("Женщины", MetricScope.RAO, (CohortFilter("sex", "equals", "ж"),))
    restored = CohortDefinition.deserialize(cohort.serialize())
    cases = (SourceCase("a", "1", MetricScope.RAO, datetime.now(), {"sex": "ж"}), SourceCase("a", "2", MetricScope.RAO, datetime.now(), {"sex": "м"}))
    assert [item.local_id for item in restored.apply(cases)] == ["1"]


def test_multi_filter_and_saved_view_roundtrip(tmp_path):
    cohort = CohortDefinition("Женщины ПСО", MetricScope.RAO, (CohortFilter("sex", "equals", "ж"), CohortFilter("source_department", "contains", "ПСО")), True)
    cases = (SourceCase("a", "1", MetricScope.RAO, datetime.now(), {"sex": "ж", "source_department": "ПСО", "recovery_bed_stay": 0}), SourceCase("a", "2", MetricScope.RAO, datetime.now(), {"sex": "ж", "source_department": "ХО"}))
    assert [item.local_id for item in cohort.apply(cases)] == ["1"]
    store = SavedAnalyticsViewStore(tmp_path / "views.json"); view = SavedAnalyticsView("вид", MetricScope.RAO, cohort, ("s1", "g1"), ("2025-01-01", "2025-01-31"), "g1", "manual"); store.save((view,))
    assert store.load() == (view,)
    app = QApplication.instance() or QApplication([])
    assert app is not None
    workspace = AnalyticsWorkspace(); workspace.set_scope(MetricScope.RAO)
    try:
        workspace.cohort_field.setCurrentIndex(workspace.cohort_field.findData("sex")); workspace.cohort_operator.setCurrentIndex(workspace.cohort_operator.findData("equals")); workspace.cohort_value.setText("ж"); workspace._add_filter()
        workspace.cohort_field.setCurrentIndex(workspace.cohort_field.findData("source_department")); workspace.cohort_operator.setCurrentIndex(workspace.cohort_operator.findData("contains")); workspace.cohort_value.setText("ПСО"); workspace._add_filter()
        assert workspace.cohort_definition().filters == cohort.filters
        assert "sex" not in workspace.cohort_field.currentText() and "contains" not in workspace.cohort_operator.currentText()
    finally: workspace.close()


def test_saved_view_backward_compatible_previous_year_mode(tmp_path):
    path = tmp_path / "old.json"; path.write_text('[{"name":"old","scope":"rao","cohort":{"name":"Все случаи","scope":"rao","filters":[],"include_recovery_beds":false},"metric_ids":["s1"]}]', encoding="utf-8")
    view = SavedAnalyticsViewStore(path).load()[0]
    assert view.comparison_mode == "previous_year" and view.selected_metric_id is None


@pytest.mark.parametrize("scope,field,operator,positive,negative", [
    (MetricScope.RAO, "sex", "equals", "ж", "м"), (MetricScope.RAO, "age", "gte", 50, 30),
    (MetricScope.RAO, "age", "lte", 50, 70), (MetricScope.RAO, "diagnosis", "contains", "J18", "K35"),
    (MetricScope.RAO, "source_department", "equals", "ПСО", "ХО"), (MetricScope.RAO, "outcome", "equals", "выписан", "умер"),
    (MetricScope.RAO, "recovery_bed_stay", "truthy", True, False),
    (MetricScope.OPERBLOCK, "table_code", "equals", "planned", "emergency"), (MetricScope.OPERBLOCK, "status", "equals", "completed", "active"),
    (MetricScope.OPERBLOCK, "priority", "equals", "emergency", "planned"), (MetricScope.OPERBLOCK, "diagnosis", "contains", "J18", "K35"),
    (MetricScope.OPERBLOCK, "personnel", "contains", "Иванов", "Петров"),
])
def test_declared_cohort_fields_select_positive_and_exclude_negative(scope, field, operator, positive, negative):
    attrs = {"sex":"ж", "age":50, "diagnosis":"J18", "source_department":"ПСО", "outcome":"выписан", "recovery_bed_stay":True, "table_code":"planned", "status":"completed", "priority":"emergency", "personnel":"Иванов"}
    positive_case = SourceCase("db", "1", scope, datetime.now(), dict(attrs)); negative_attrs = dict(attrs); negative_attrs[field] = negative
    negative_case = SourceCase("db", "2", scope, datetime.now(), negative_attrs)
    cohort = CohortDefinition(scope=scope, filters=(CohortFilter(field, operator, positive),), include_recovery_beds=True)
    assert cohort.apply((positive_case, negative_case)) == (positive_case,)
