from datetime import datetime
import sqlite3
import pytest
from rem_card.app.operblock_schema import _apply_operblock_schema
from rem_card.app.unified_db_schema import ensure_unified_schema
from rem_card.services.analytics.operblock_statistics_service import OperBlockStatisticsReportBuilder
from rem_card.services.analytics.detailed_statistics_service import DetailedStatisticsReportBuilder, build_detailed_statistics_report_html
from rem_card.services.analytics.multi_db_analytics import AnalyticsConnectionManager, create_multi_db_analytics_manager
from rem_card.services.analytics.platform import AnalyticsEngine, AnalyticsPeriod, CohortDefinition, CohortFilter, MetricScope, SourceCase, StatisticsRepository, default_metric_registry, materialize_cohort_snapshot
from rem_card.services.analytics.graph_catalog import GRAPH_GROUPS


class Repository:
    def fingerprints(self): return ("fixture",)
    def source_cases(self, scope, _period):
        return (SourceCase("db", "1", scope, datetime(2026, 1, 1), {"transfer_datetime": "2026-01-03 00:00:00", "death_datetime": "2026-01-02 00:00:00"}),) if scope is MetricScope.RAO else ()


def test_engine_calculates_structured_rao_results():
    engine = AnalyticsEngine(Repository()); snapshot = engine.snapshot(MetricScope.RAO, AnalyticsPeriod.from_values("2026-01-01", "2026-01-03"), metric_ids=("rao.admissions", "rao.deaths", "rao.mortality"))
    assert snapshot.results["rao.admissions"].value == 1
    assert snapshot.results["rao.deaths"].value == 1
    assert snapshot.results["rao.mortality"].value == 100


def test_kpi_population_kinds_apply_event_terminal_interval_recovery_and_related_events():
    period = AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 1, 4))
    carry_in = SourceCase("kpi", "carry", MetricScope.RAO, datetime(2025, 12, 30), {"transfer_datetime": "2026-01-02"})
    new_admission = SourceCase("kpi", "new", MetricScope.RAO, datetime(2026, 1, 2), {"transfer_datetime": "2026-01-03"})
    death_in_period = SourceCase("kpi", "death", MetricScope.RAO, datetime(2025, 12, 31), {"death_datetime": "2026-01-02", "outcome": "умер"})
    post_period_death = SourceCase("kpi", "late", MetricScope.RAO, datetime(2026, 1, 2), {"death_datetime": "2026-01-08", "outcome": "умер"})
    recovery = SourceCase("kpi", "recovery", MetricScope.RAO, datetime(2026, 1, 2), {"recovery_bed_stay": True})
    event_case = SourceCase("kpi", "events", MetricScope.RAO, datetime(2026, 1, 2), {
        "operations": ({"operation_datetime": "2026-01-02 08:00"}, {"operation_datetime": "2026-01-02 10:00"}),
        "transfusions": ({"datetime": "2026-01-02 09:00"}, {"datetime": "2026-01-03 09:00"}),
    })
    class Repo:
        def fingerprints(self): return ("kpi-population",)
        def source_cases(self, scope, _period): return (carry_in, new_admission, death_in_period, post_period_death, recovery, event_case)
    snapshot = AnalyticsEngine(Repo()).snapshot(
        MetricScope.RAO, period,
        CohortDefinition(scope=MetricScope.RAO, include_recovery_beds=False),
        ("rao.admissions", "rao.bed_days", "rao.average_los", "rao.deaths", "rao.mortality", "rao.recovery_cases", "rao.operations", "rao.transfusions"),
    )
    assert snapshot.results["rao.admissions"].source_cases == (new_admission, post_period_death, event_case)
    assert snapshot.results["rao.deaths"].source_cases == (death_in_period,)
    assert snapshot.results["rao.mortality"].value == pytest.approx(20.0)  # 1 terminal death / 5 general RAO cases
    assert snapshot.results["rao.mortality"].numerator == 1
    assert snapshot.results["rao.mortality"].denominator == 5
    assert snapshot.results["rao.average_los"].numerator == snapshot.results["rao.bed_days"].value
    assert snapshot.results["rao.average_los"].denominator == 5
    assert snapshot.results["rao.average_los"].value == pytest.approx(
        snapshot.results["rao.average_los"].numerator / 5
    )
    assert snapshot.results["rao.recovery_cases"].source_cases == (recovery,)
    assert snapshot.results["rao.operations"].value == 2 and snapshot.results["rao.operations"].source_cases == (event_case,)
    assert snapshot.results["rao.transfusions"].value == 2 and snapshot.results["rao.transfusions"].source_cases == (event_case,)
    assert snapshot.results["rao.bed_days"].value > 0  # carry-in remains an interval/census case
    registry = default_metric_registry()
    assert registry.get("rao.deaths").population_kind == "terminal_event"
    assert registry.get("rao.recovery_cases").population_kind == "recovery_subpopulation"


def test_legacy_s1_interval_rows_match_engine_for_carry_in_and_new_admission():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
      CREATE TABLE admissions(id INTEGER, patient_id INTEGER, admission_datetime TEXT, transfer_datetime TEXT, death_datetime TEXT, outcome TEXT, patient_age REAL, patient_age_unit TEXT, patient_gender TEXT, source_department TEXT, diagnosis_code TEXT, diagnosis_text TEXT, recovery_bed_stay INTEGER, unit_scope TEXT);
      CREATE TABLE operations(id INTEGER, admission_id INTEGER, operation_datetime TEXT);
      CREATE TABLE transfusions(id INTEGER, admission_id INTEGER, datetime TEXT, type TEXT, volume_ml REAL);
      CREATE TABLE ivl_episodes(id INTEGER, admission_id INTEGER, start_time TEXT, end_time TEXT);
    """)
    conn.executemany("INSERT INTO admissions VALUES (?, ?, ?, ?, NULL, NULL, 40, 'years', 'ж', 'ПСО', 'J18', 'Тест', 0, 'rao')", [
        (1, 1, "2025-12-31 00:00:00", "2026-01-03 00:00:00"),
        (2, 2, "2026-01-02 00:00:00", "2026-01-03 00:00:00"),
    ])
    conn.commit()
    manager = AnalyticsConnectionManager(conn, db_path="legacy-parity")
    period = AnalyticsPeriod.from_values("2026-01-01", "2026-01-03")
    engine = AnalyticsEngine(StatisticsRepository(manager)).snapshot(
        MetricScope.RAO, period, metric_ids=("rao.admissions", "rao.bed_days", "rao.average_los")
    )
    builder = DetailedStatisticsReportBuilder(manager, "2026-01-01", "2026-01-03")
    rows = {row["name"]: row for row in builder.structured_section_rows("s1")}
    html = build_detailed_statistics_report_html(manager, "2026-01-01", "2026-01-03", ("s1",))
    assert engine.results["rao.admissions"].value == 1
    assert float(rows["1.2 Койко-дни"]["value"]) == engine.results["rao.bed_days"].value == 3.0
    assert float(rows["1.3 Средняя длительность лечения"]["value"]) == engine.results["rao.average_los"].value == 1.5
    assert "3.00" in html and "1.50" in html


def test_legacy_procedure_and_mortality_rows_use_interval_population():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
      CREATE TABLE admissions(id INTEGER, patient_id INTEGER, admission_datetime TEXT, transfer_datetime TEXT, death_datetime TEXT, outcome TEXT, patient_age REAL, patient_age_unit TEXT, patient_gender TEXT, source_department TEXT, diagnosis_code TEXT, diagnosis_text TEXT, recovery_bed_stay INTEGER, unit_scope TEXT);
      CREATE TABLE operations(id INTEGER, admission_id INTEGER, operation_datetime TEXT);
      CREATE TABLE transfusions(id INTEGER, admission_id INTEGER, datetime TEXT, type TEXT, volume_ml REAL);
      CREATE TABLE ivl_episodes(id INTEGER, admission_id INTEGER, start_time TEXT, end_time TEXT);
    """)
    conn.executemany("INSERT INTO admissions VALUES (?, ?, ?, ?, ?, ?, 40, 'years', 'ж', 'ПСО', 'J18', 'Тест', 0, 'rao')", [
        # Carry-in: all three procedures happen in the selected period.
        (1, 1, "2025-12-30", "2026-01-04", None, "выписан"),
        # Transfer precedes a later death timestamp and therefore censors it.
        (2, 2, "2026-01-01", "2026-01-02", "2026-01-03", "умер"),
    ])
    conn.execute("INSERT INTO operations VALUES (1, 1, '2026-01-02 08:00:00')")
    conn.execute("INSERT INTO transfusions VALUES (1, 1, '2026-01-02 09:00:00', 'plasma', 250)")
    conn.execute("INSERT INTO ivl_episodes VALUES (1, 1, '2025-12-31 12:00:00', '2026-01-02 12:00:00')")
    conn.commit()
    manager = AnalyticsConnectionManager(conn, db_path="legacy-procedure-parity")
    builder = DetailedStatisticsReportBuilder(manager, "2026-01-01", "2026-01-03")
    payload = builder.calculate_payload()
    assert payload["N"] == 1 and payload["N_interval"] == 2
    assert payload["operations_count"] == payload["transfusion_units"] == payload["ivl_episodes_count"] == 1
    assert payload["ivl_days"] == pytest.approx(1.5)
    assert payload["deaths"] == 0 and payload["mortality_pct"] == 0
    legacy_death = AnalyticsEngine(StatisticsRepository(manager)).snapshot(
        MetricScope.RAO, AnalyticsPeriod.from_values("2026-01-01", "2026-01-03"), metric_ids=("s6",)
    ).results["s6"]
    assert legacy_death.source_cases == ()


def test_transfer_before_later_death_is_censored_for_kpi_graphs_and_kaplan_meier():
    case = SourceCase("terminal", "1", MetricScope.RAO, datetime(2026, 1, 1), {
        "transfer_datetime": "2026-01-02 00:00:00", "death_datetime": "2026-01-05 00:00:00", "outcome": "умер",
    })
    class Repo:
        def fingerprints(self): return ("terminal",)
        def source_cases(self, scope, _period): return (case,)
    period = AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 1, 6))
    results = AnalyticsEngine(Repo()).snapshot(MetricScope.RAO, period, metric_ids=(
        "rao.deaths", "rao.mortality", "rao.transfers", "g21", "g26", "g28", "g37", "g41", "g49",
    )).results
    assert results["rao.deaths"].value == 0 and results["rao.mortality"].value == 0
    assert results["rao.transfers"].value == {"переведен": 1}
    assert results["g28"].rows == ({"label": "переведен", "value": 1},)
    assert results["g49"].rows == () and results["g49"].source_cases == ()
    assert all(results[key].numerator == 0 for key in ("rao.deaths", "rao.mortality", "g21", "g26", "g37"))
    assert results["g41"].artifact["series"] == ({"label": "1.00", "x": 1.0, "value": 1.0},)


def test_future_transfer_does_not_leak_raw_discharge_outcome_before_cutoff():
    case = SourceCase("cutoff", "1", MetricScope.RAO, datetime(2026, 1, 1), {
        "transfer_datetime": "2026-01-10 00:00:00", "outcome": "выписан",
    })

    class Repo:
        def fingerprints(self): return ("cutoff",)
        def source_cases(self, scope, _period): return (case,)

    results = AnalyticsEngine(Repo()).snapshot(
        MetricScope.RAO,
        AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 1, 4)),
        metric_ids=("rao.transfers", "g28", "g49"),
    ).results
    assert results["rao.transfers"].value == {"в отделении": 1}
    assert results["g28"].rows == ({"label": "в отделении", "value": 1},)
    assert results["g49"].rows == () and results["g49"].source_cases == ()


def test_graph_selection_is_an_artifact_not_a_fake_case_count():
    result = AnalyticsEngine(Repository()).snapshot(MetricScope.RAO, AnalyticsPeriod.from_values("2026-01-01", "2026-01-03"), metric_ids=("g1",)).results["g1"]
    assert isinstance(result.value, dict)
    assert result.artifact["metric_id"] == "g1"
    assert result.artifact["source_case_ids"] == ("db:rao:1",)
    assert result.numerator == 1


def test_graph_artifact_uses_numerator_source_cases_and_clipped_ivl_intervals():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
      CREATE TABLE admissions(id INTEGER, patient_id INTEGER, admission_datetime TEXT, transfer_datetime TEXT, death_datetime TEXT, outcome TEXT, patient_age REAL, patient_age_unit TEXT, patient_gender TEXT, source_department TEXT, diagnosis_code TEXT, diagnosis_text TEXT, bed_number INTEGER, recovery_bed_stay INTEGER);
      CREATE TABLE ivl_episodes(id INTEGER, admission_id INTEGER, start_time TEXT, end_time TEXT);
    """)
    conn.execute("INSERT INTO admissions VALUES (1, 1, '2026-01-01', NULL, NULL, NULL, 40, 'years', 'ж', 'ПСО', 'J18', 'Тест', 1, 0)")
    conn.execute("INSERT INTO ivl_episodes VALUES (1, 1, '2025-12-31 12:00:00', '2026-01-01 12:00:00')")
    manager = AnalyticsConnectionManager(conn, db_path="graph-artifact")
    period = AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 1, 3))
    result = AnalyticsEngine(StatisticsRepository(manager)).snapshot(MetricScope.RAO, period, metric_ids=("g44", "g45")).results
    assert result["g44"].numerator == pytest.approx(0.5)
    assert result["g44"].denominator == 1
    assert result["g44"].artifact["source_case_ids"] == (result["g44"].source_cases[0].id,)
    assert result["g45"].artifact["series"] == ({"label": "2026-01", "value": 0.5},)


def test_g42_ivl_share_uses_full_rao_cohort_as_denominator():
    cases = (
        SourceCase("ivl-share", "1", MetricScope.RAO, datetime(2026, 1, 1), {
            "ivl_episodes": ({"start_time": "2026-01-01", "end_time": "2026-01-02"},),
        }),
        SourceCase("ivl-share", "2", MetricScope.RAO, datetime(2026, 1, 1), {}),
    )

    class Repo:
        def fingerprints(self): return ("ivl-share",)
        def source_cases(self, scope, _period): return cases

    result = AnalyticsEngine(Repo()).snapshot(
        MetricScope.RAO,
        AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 1, 3)),
        metric_ids=("g42",),
    ).results["g42"]
    assert result.numerator == 1 and result.denominator == 2
    assert result.rows == (
        {"label": "ИВЛ", "value": 50.0},
        {"label": "Без ИВЛ", "value": 50.0},
    )


def _graph_cases():
    return (
        SourceCase("a", "1", MetricScope.RAO, datetime(2026, 1, 2, 8), {"transfer_datetime": "2026-01-10", "sex": "ж", "age": 38, "diagnosis": "J18", "outcome": "выписан", "source_department": "ПСО", "bed_number": "1", "operations": ({"operation_datetime": "2026-01-03", "description": "Операция"},), "transfusions": ({"datetime": "2026-01-04", "type": "Эритроциты"},), "ivl_episodes": ({"start_time": "2026-01-03", "end_time": "2026-01-04"},)}),
        SourceCase("a", "2", MetricScope.RAO, datetime(2026, 1, 3, 9), {"death_datetime": "2026-01-03 20:00", "sex": "м", "age": 71, "diagnosis": "K35", "outcome": "умер", "source_department": "ХО", "bed_number": "2", "recovery_bed_stay": True}),
    )


class GraphRepository:
    def fingerprints(self): return ("graph-dispatch",)
    def source_cases(self, scope, _period): return _graph_cases() if scope is MetricScope.RAO else ()


def test_every_exposed_graph_has_explicit_aggregate_artifact_dispatch():
    graph_ids = tuple(key for group in GRAPH_GROUPS.values() for key in group)
    results = AnalyticsEngine(GraphRepository()).snapshot(MetricScope.RAO, AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 2, 1)), metric_ids=graph_ids).results
    assert set(results) == set(graph_ids)
    for key, result in results.items():
        assert result.artifact["metric_id"] == key
        assert "series" in result.artifact and "source_case_ids" in result.artifact
        assert all("source_case_id" not in row for row in result.rows), key


@pytest.mark.parametrize("key", ["g1", "g6", "g16", "g20", "g23", "g28", "g33", "g37", "g42", "g46", "g51", "g56", "g61"])
def test_graph_family_artifacts_have_renderer_labels(key):
    result = AnalyticsEngine(GraphRepository()).snapshot(MetricScope.RAO, AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 2, 1)), CohortDefinition(scope=MetricScope.RAO, include_recovery_beds=True), (key,)).results[key]
    assert result.rows and all("label" in row and "value" in row for row in result.rows)


def test_g50_is_average_clipped_duration_by_diagnosis_not_diagnosis_count():
    result = AnalyticsEngine(GraphRepository()).snapshot(MetricScope.RAO, AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 2, 1)), CohortDefinition(scope=MetricScope.RAO, include_recovery_beds=True), ("g50",)).results["g50"]
    values = {row["label"]: row["value"] for row in result.rows}
    assert values["J18"] == pytest.approx(7 + 16 / 24)
    assert values["K35"] == pytest.approx(11 / 24)


def test_g9_turnover_and_g14_high_load_days_match_declared_units():
    cases = tuple(
        SourceCase(
            "load", str(index), MetricScope.RAO, datetime(2026, 1, 1, 8 + index),
            {"transfer_datetime": "2026-01-02 00:00:00", "bed_number": str(index + 1)},
        )
        for index in range(4)
    )

    class Repo:
        def fingerprints(self): return ("load-contract",)
        def source_cases(self, scope, _period): return cases

    results = AnalyticsEngine(Repo()).snapshot(
        MetricScope.RAO,
        AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 1, 4)),
        metric_ids=("g9", "g14"),
    ).results
    assert results["g9"].rows == ({"label": "2026-01", "value": 1.0},)
    assert results["g9"].numerator == 4 and results["g9"].denominator == 4
    assert results["g9"].artifact["summary"].endswith(": 1 пациентов на койку")
    assert results["g14"].rows == (
        {"label": "2026-01-01", "value": 1},
        {"label": "2026-01-02", "value": 0},
        {"label": "2026-01-03", "value": 0},
    )
    assert results["g14"].numerator == 1


def test_g41_excludes_carry_in_from_admission_based_kaplan_meier():
    carry_in = SourceCase(
        "km-population", "carry", MetricScope.RAO, datetime(2025, 12, 31),
        {"transfer_datetime": "2026-01-02"},
    )
    admitted = SourceCase(
        "km-population", "new", MetricScope.RAO, datetime(2026, 1, 1),
        {"death_datetime": "2026-01-02"},
    )

    class Repo:
        def fingerprints(self): return ("km-population",)
        def source_cases(self, scope, _period): return (carry_in, admitted)

    result = AnalyticsEngine(Repo()).snapshot(
        MetricScope.RAO,
        AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 1, 4)),
        metric_ids=("g41",),
    ).results["g41"]
    assert result.source_cases == (admitted,)
    assert result.artifact["source_case_ids"] == (admitted.id,)
    assert result.rows == ({"label": "1.00", "x": 1.0, "value": 0.0},)


def test_g49_uses_only_completed_death_or_discharge_outcomes():
    cases = (
        SourceCase("outcome", "active", MetricScope.RAO, datetime(2026, 1, 1), {}),
        SourceCase("outcome", "future", MetricScope.RAO, datetime(2026, 1, 1), {"transfer_datetime": "2026-01-06", "outcome": "выписан"}),
        SourceCase("outcome", "transfer", MetricScope.RAO, datetime(2026, 1, 1), {"transfer_datetime": "2026-01-02", "outcome": "переведен"}),
        SourceCase("outcome", "discharge", MetricScope.RAO, datetime(2026, 1, 1), {"transfer_datetime": "2026-01-02", "outcome": "выписан"}),
        SourceCase("outcome", "death", MetricScope.RAO, datetime(2026, 1, 1), {"death_datetime": "2026-01-03", "outcome": "умер"}),
    )

    class Repo:
        def fingerprints(self): return ("outcome-contract",)
        def source_cases(self, scope, _period): return cases

    result = AnalyticsEngine(Repo()).snapshot(
        MetricScope.RAO,
        AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 1, 4)),
        metric_ids=("g49",),
    ).results["g49"]
    assert result.rows == (
        {"label": "выписан", "value": 1.0},
        {"label": "умер", "value": 2.0},
    )
    assert result.source_cases == (cases[3], cases[4])
    assert result.numerator == 3.0 and result.denominator == 2


def test_g50_returns_ranked_top_five_and_matching_drill_through_cases():
    cases = tuple(
        SourceCase(
            "diagnosis-top", str(days), MetricScope.RAO, datetime(2026, 1, 1),
            {"transfer_datetime": f"2026-01-{days + 1:02d}", "diagnosis": f"D{days}"},
        )
        for days in range(1, 7)
    )

    class Repo:
        def fingerprints(self): return ("diagnosis-top",)
        def source_cases(self, scope, _period): return cases

    result = AnalyticsEngine(Repo()).snapshot(
        MetricScope.RAO,
        AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 1, 10)),
        metric_ids=("g50",),
    ).results["g50"]
    assert [row["label"] for row in result.rows] == ["D6", "D5", "D4", "D3", "D2"]
    assert [row["value"] for row in result.rows] == [6.0, 5.0, 4.0, 3.0, 2.0]
    assert tuple(item.local_id for item in result.source_cases) == ("2", "3", "4", "5", "6")


def test_recovery_artifacts_keep_table_rendering_and_hour_buckets():
    durations = (1, 3, 10, 30)
    cases = tuple(
        SourceCase(
            "recovery-contract", str(index), MetricScope.RAO, datetime(2026, 1, 1),
            {
                "transfer_datetime": f"2026-01-01 {hours:02d}:00:00" if hours < 24 else "2026-01-02 06:00:00",
                "recovery_bed_stay": True,
                "patient_id": index,
                "outcome": "выписан",
            },
        )
        for index, hours in enumerate(durations, start=1)
    )

    class Repo:
        def fingerprints(self): return ("recovery-contract",)
        def source_cases(self, scope, _period): return cases

    results = AnalyticsEngine(Repo()).snapshot(
        MetricScope.RAO,
        AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 1, 3)),
        CohortDefinition(scope=MetricScope.RAO, include_recovery_beds=True),
        ("recovery_flow_table", "recovery_flow_duration"),
    ).results
    assert results["recovery_flow_table"].artifact["chart_kind"] == "table"
    assert results["recovery_flow_table"].rows[0] == {
        "label": "Госпитализаций через койки пробуждения", "value": 4, "unit": "случаев",
    }
    assert results["recovery_flow_duration"].rows == (
        {"label": "до 2 часов", "value": 1},
        {"label": "2-6 часов", "value": 1},
        {"label": "6-24 часа", "value": 1},
        {"label": "более 24 часов", "value": 1},
    )


def test_recovery_unique_patient_identity_includes_source_database():
    cases = (
        SourceCase("db-a", "10", MetricScope.RAO, datetime(2026, 1, 1), {
            "patient_id": 1, "recovery_bed_stay": True, "transfer_datetime": "2026-01-01 02:00",
        }),
        SourceCase("db-b", "20", MetricScope.RAO, datetime(2026, 1, 2), {
            "patient_id": 1, "recovery_bed_stay": True, "transfer_datetime": "2026-01-02 02:00",
        }),
    )

    class Repo:
        def fingerprints(self): return ("db-a", "db-b")
        def source_cases(self, scope, _period): return cases

    result = AnalyticsEngine(Repo()).snapshot(
        MetricScope.RAO,
        AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 1, 3)),
        CohortDefinition(scope=MetricScope.RAO, include_recovery_beds=True),
        ("recovery_flow_table",),
    ).results["recovery_flow_table"]
    unique_row = next(row for row in result.rows if row["label"] == "Уникальных пациентов")
    assert unique_row["value"] == 2


def test_recovery_bed_number_contract_matches_kpi_legacy_graph_and_drillthrough():
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
    conn.execute(
        "INSERT INTO admissions VALUES (1, 1, '2026-01-01', '2026-01-02', NULL, 'переведен', "
        "40, 'years', 'ж', 'ПСО', 'J18', 'Тест', 10, 0)"
    )
    conn.commit()
    manager = AnalyticsConnectionManager(conn, db_path="recovery-bed-number")
    try:
        results = AnalyticsEngine(StatisticsRepository(manager)).snapshot(
            MetricScope.RAO,
            AnalyticsPeriod.from_values("2026-01-01", "2026-01-03"),
            CohortDefinition(scope=MetricScope.RAO, include_recovery_beds=False),
            ("rao.recovery_cases", "s1", "s_recovery", "recovery_flow_table"),
        ).results
        assert results["rao.recovery_cases"].value == 1
        assert len(results["rao.recovery_cases"].source_cases) == 1
        assert results["s1"].source_cases == ()
        assert next(row for row in results["s1"].rows if row["name"] == "1.1 Госпитализации")["value"] == "0"
        assert len(results["s_recovery"].source_cases) == 1
        recovery_row = next(row for row in results["s_recovery"].rows if row["name"] == "Проведено через пробуждение")
        assert recovery_row["display_value"].startswith("1 (")
        graph = results["recovery_flow_table"]
        assert len(graph.source_cases) == 1
        assert graph.rows[0]["value"] == 1
    finally:
        conn.close()


def test_recovery_legacy_and_graph_use_same_earliest_terminal_outcome():
    from rem_card.services.analytics.recovery_summary import build_recovery_bed_summary

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE admissions(id INTEGER, patient_id INTEGER, admission_datetime TEXT, transfer_datetime TEXT, "
        "death_datetime TEXT, outcome TEXT, bed_number INTEGER, recovery_bed_stay INTEGER)"
    )
    conn.execute(
        "INSERT INTO admissions VALUES (1, 1, '2026-01-01', '2026-01-02', "
        "'2026-01-03', 'умер', 10, 0)"
    )
    conn.commit()
    manager = AnalyticsConnectionManager(conn, db_path="recovery-terminal")
    try:
        summary = build_recovery_bed_summary(conn, "2026-01-01", "2026-01-03")
        graph = AnalyticsEngine(StatisticsRepository(manager)).snapshot(
            MetricScope.RAO,
            AnalyticsPeriod.from_values("2026-01-01", "2026-01-03"),
            CohortDefinition(scope=MetricScope.RAO, include_recovery_beds=True),
            ("recovery_flow_outcomes",),
        ).results["recovery_flow_outcomes"]
        assert summary.transferred == 1 and summary.deceased == 0
        assert graph.rows == ({"label": "переведен", "value": 1},)
    finally:
        conn.close()


def test_multi_db_operblock_keeps_late_related_rows_and_admission_enrichment(tmp_path):
    from rem_card.services.analytics.multi_db_analytics import create_multi_db_analytics_manager

    path = tmp_path / "oper-late-related.db"
    conn = sqlite3.connect(path)
    ensure_unified_schema(conn)
    _apply_operblock_schema(conn.cursor())
    conn.execute("INSERT INTO patients (id, full_name) VALUES (1, 'Иванов Иван Иванович')")
    conn.execute(
        "INSERT INTO admissions (id, patient_id, bed_number, history_number, admission_datetime, unit_scope, diagnosis_code) "
        "VALUES (1, 1, 1, 'ИБ-1', '2026-01-01 20:00', 'operblock', 'J18')"
    )
    conn.execute(
        "INSERT INTO operation_cases (id, patient_id, admission_id, table_code, status, created_at, started_at, ended_at, planned_operation_name) "
        "VALUES (1, 1, 1, 'planned', 'closed', '2026-01-01 20:00', '2026-01-01 23:30', '2026-01-02 01:30', 'Операция')"
    )
    conn.execute(
        "INSERT INTO operblock_timeline_events (id, operation_case_id, admission_id, event_type, event_time, status) "
        "VALUES (1, 1, 1, 'clinical_event', '2026-01-02 00:30', 'active')"
    )
    conn.execute(
        "INSERT INTO orders (id, admission_id, datetime, text, status) "
        "VALUES (1, 1, '2026-01-02 00:40', 'Препарат', 'active')"
    )
    conn.execute(
        "INSERT INTO vitals (id, admission_id, datetime, sys, dia, pulse) "
        "VALUES (1, 1, '2026-01-02 00:50', 120, 80, 70)"
    )
    conn.commit()
    conn.close()

    manager = create_multi_db_analytics_manager(
        (str(path),), start_dt="2026-01-01", end_dt="2026-01-01",
    )
    try:
        context = OperBlockStatisticsReportBuilder._fetch_context(
            manager, "2026-01-01 00:00:00", "2026-01-02 00:00:00",
        )
        assert len(context["cases"]) == 1
        assert context["cases"][0]["history_number"] == "ИБ-1"
        assert context["cases"][0]["full_name"] == "Иванов Иван Иванович"
        assert len(context["timeline"]) == len(context["orders"]) == len(context["vitals"]) == 1
    finally:
        manager.close_connection()


def test_month_age_is_normalized_to_years_before_cohort_and_graph_calculation():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE admissions(id INTEGER, patient_id INTEGER, admission_datetime TEXT, "
        "transfer_datetime TEXT, patient_age REAL, patient_age_unit TEXT, recovery_bed_stay INTEGER)"
    )
    conn.execute("INSERT INTO admissions VALUES (1, 1, '2026-01-01', '2026-01-02', 6, 'месяцев', 0)")
    conn.commit()
    manager = AnalyticsConnectionManager(conn, db_path="age-months")
    try:
        engine = AnalyticsEngine(StatisticsRepository(manager))
        period = AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 1, 3))
        result = engine.snapshot(MetricScope.RAO, period, metric_ids=("g19",)).results["g19"]
        filtered = engine.snapshot(
            MetricScope.RAO,
            period,
            CohortDefinition(scope=MetricScope.RAO, filters=(CohortFilter("age", "lte", 1),)),
            ("rao.admissions",),
        ).results["rao.admissions"]
        assert result.rows == ({"label": "до 1 г", "value": 1},)
        assert filtered.value == 1
    finally:
        conn.close()


def test_operblock_average_uses_only_cases_with_valid_room_duration():
    from rem_card.services.analytics.platform import metric_result_has_data

    completed = SourceCase(
        "oper", "1", MetricScope.OPERBLOCK, datetime(2026, 1, 1, 8),
        {"ended_at": "2026-01-01 09:30"},
    )
    unfinished = SourceCase(
        "oper", "2", MetricScope.OPERBLOCK, datetime(2026, 1, 1, 10), {},
    )

    class Repo:
        def fingerprints(self): return ("oper-average",)
        def source_cases(self, scope, _period): return (completed, unfinished)

    result = AnalyticsEngine(Repo()).snapshot(
        MetricScope.OPERBLOCK,
        AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 1, 2)),
        metric_ids=("operblock.average_room_duration",),
    ).results["operblock.average_room_duration"]
    assert result.value == result.numerator == 90
    assert result.denominator == 1
    assert result.source_cases == (completed,)
    assert metric_result_has_data(result)

    class EmptyDurationRepo(Repo):
        def source_cases(self, scope, _period): return (unfinished,)

    empty = AnalyticsEngine(EmptyDurationRepo()).snapshot(
        MetricScope.OPERBLOCK,
        AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 1, 2)),
        metric_ids=("operblock.average_room_duration",),
    ).results["operblock.average_room_duration"]
    assert empty.value is None and empty.numerator == 0 and empty.denominator == 0
    assert empty.source_cases == () and not metric_result_has_data(empty)


def test_graph_comparison_retains_current_and_previous_structured_artifacts():
    engine = AnalyticsEngine(GraphRepository())
    comparison = engine.compare(MetricScope.RAO, "g1", AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 2, 1)), comparison_period=AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 2, 1)))
    assert comparison.previous is not None
    assert comparison.current.artifact["summary"] and comparison.previous.artifact["summary"]


def test_kaplan_meier_artifact_has_real_numeric_time_coordinates():
    cases = (
        SourceCase("km", "1", MetricScope.RAO, datetime(2026, 1, 1), {"death_datetime": "2026-01-01 12:00"}),
        SourceCase("km", "2", MetricScope.RAO, datetime(2026, 1, 1), {"transfer_datetime": "2026-01-03"}),
    )
    class Repo:
        def fingerprints(self): return ("km",)
        def source_cases(self, scope, _period): return cases
    artifact = AnalyticsEngine(Repo()).snapshot(MetricScope.RAO, AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 1, 4)), metric_ids=("g41",)).results["g41"].artifact
    assert [row["x"] for row in artifact["series"]] == [0.5, 2.0]


@pytest.mark.parametrize("attrs", [
    {"transfer_datetime": "2026-01-05", "death_datetime": "2026-01-03"},
    {"transfer_datetime": "2026-01-03", "death_datetime": "2026-01-05"},
])
def test_earliest_terminal_endpoint_is_used_regardless_of_column_order(attrs):
    case = SourceCase("x", "1", MetricScope.RAO, datetime(2026, 1, 1), attrs)
    period = AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 1, 10))
    assert AnalyticsEngine._duration_days(case, period) == 2


def test_gender_diagnosis_ranking_and_mortality_strata_are_semantic():
    cases = (
        SourceCase("x", "1", MetricScope.RAO, datetime(2026, 1, 1), {"patient_gender": "ж", "diagnosis": "Z99", "diagnosis_code": "Z99", "transfer_datetime": "2026-01-03"}),
        SourceCase("x", "2", MetricScope.RAO, datetime(2026, 1, 1), {"sex": "м", "diagnosis": "A01", "diagnosis_code": "A01", "death_datetime": "2026-01-01 12:00", "outcome": "умер"}),
        SourceCase("x", "3", MetricScope.RAO, datetime(2026, 1, 1), {"patient_gender": "ж", "diagnosis": "A01", "diagnosis_code": "A01", "death_datetime": "2026-01-04", "outcome": "умер"}),
    )
    class Repo:
        def fingerprints(self): return ("semantic",)
        def source_cases(self, scope, period): return cases
    results = AnalyticsEngine(Repo()).snapshot(MetricScope.RAO, AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 1, 8)), metric_ids=("g20", "g23", "g24", "g25", "g30", "g38")).results
    assert {row["label"]: row["value"] for row in results["g20"].rows} == {"ж": 2, "м": 1}
    assert results["g23"].rows[0] == {"label": "A01", "value": 2}
    assert results["g24"].rows[0] == {"label": "A01", "value": 2}
    assert results["g25"].rows[0] == {"label": "A01", "value": 2}
    assert {row["label"] for row in results["g38"].rows} == {"<24 ч", "1–3 суток", "4–7 суток", ">7 суток"}


def test_g15_reports_separate_contiguous_high_load_runs_and_g60_never_negative():
    cases = tuple(SourceCase("x", str(index), MetricScope.RAO, datetime(2026, 1, 1), {"transfer_datetime": "2026-01-02"}) for index in range(4)) + tuple(SourceCase("x", str(index + 4), MetricScope.RAO, datetime(2026, 1, 3), {"transfer_datetime": "2026-01-04"}) for index in range(4))
    operation_case = SourceCase("x", "op", MetricScope.RAO, datetime(2026, 1, 1), {"transfer_datetime": "2026-01-02", "operations": ({"operation_datetime": "2026-01-03"},)})
    class Repo:
        def fingerprints(self): return ("runs",)
        def source_cases(self, scope, period): return cases + (operation_case,)
    results = AnalyticsEngine(Repo()).snapshot(MetricScope.RAO, AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 1, 5)), metric_ids=("g15", "g60")).results
    assert [row["value"] for row in results["g15"].rows] == [1, 1]
    assert results["g60"].rows[0]["value"] == 0


def test_g63_keeps_per_case_clipped_duration_and_ward_group():
    cases = tuple(SourceCase("x", str(index), MetricScope.RAO, datetime(2026, 1, 1), {"transfer_datetime": "2026-01-02", "source_department": f"Отделение {index}"}) for index in range(1, 8))
    class Repo:
        def fingerprints(self): return ("wards",)
        def source_cases(self, scope, period): return cases
    result = AnalyticsEngine(Repo()).snapshot(MetricScope.RAO, AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 1, 3)), metric_ids=("g63",)).results["g63"]
    assert len(result.rows) == 7 and {row["group"] for row in result.rows} == {f"Отделение {index}" for index in range(1, 8)}


def test_metric_population_separates_admission_census_terminal_and_recovery():
    carry = SourceCase("x", "carry", MetricScope.RAO, datetime(2025, 12, 30), {"transfer_datetime": "2026-01-02", "death_datetime": "2026-01-10", "outcome": "умер"})
    recovery = SourceCase("x", "rec", MetricScope.RAO, datetime(2026, 1, 2), {"transfer_datetime": "2026-01-03", "recovery_bed_stay": True})
    starts = tuple(SourceCase("x", str(index), MetricScope.RAO, datetime(2026, 1, 2, index), {"transfer_datetime": f"2026-01-02 {index + 1:02d}:00"}) for index in range(3))
    class Repo:
        def fingerprints(self): return ("populations",)
        def source_cases(self, scope, period): return (carry, recovery, *starts)
    engine = AnalyticsEngine(Repo()); period = AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 1, 4))
    results = engine.snapshot(MetricScope.RAO, period, CohortDefinition(scope=MetricScope.RAO, include_recovery_beds=False), ("g1", "g16", "g37", "recovery_flow_months", "g33")).results
    assert results["g1"].artifact["source_case_ids"] == tuple(item.id for item in starts)
    assert results["g16"].rows[0]["value"] == 1
    assert results["g37"].rows == ()  # outcome alone cannot turn post-period death into an event
    assert results["recovery_flow_months"].artifact["source_case_ids"] == (recovery.id,)
    assert all(row["label"].startswith("Случай") for row in results["g33"].rows)


def test_b_only_archive_repository_produces_distinct_comparison_source_identity(tmp_path):
    paths = []
    for year in (2026, 2025):
        path = tmp_path / f"archive-{year}.db"; paths.append(str(path)); conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE admissions(id INTEGER, admission_datetime TEXT, unit_scope TEXT)")
        conn.execute("INSERT INTO admissions VALUES (1, ?, 'rao')", (f"{year}-01-02",)); conn.commit(); conn.close()
    current = AnalyticsEngine(StatisticsRepository(db_paths=(paths[0],))).snapshot(MetricScope.RAO, AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 1, 4)), metric_ids=("g1",))
    previous = AnalyticsEngine(StatisticsRepository(db_paths=(paths[1],))).snapshot(MetricScope.RAO, AnalyticsPeriod(datetime(2025, 1, 1), datetime(2025, 1, 4)), metric_ids=("g1",))
    assert current.results["g1"].source_cases[0].id != previous.results["g1"].source_cases[0].id


def test_multi_db_carry_in_ivl_episode_is_clipped_and_identity_safe(tmp_path):
    paths = []
    for index, end_time in enumerate((None, "2026-01-01 12:00:00")):
        path = tmp_path / f"ivl-{index}.db"; paths.append(str(path)); conn = sqlite3.connect(path)
        conn.executescript("CREATE TABLE admissions(id INTEGER, admission_datetime TEXT, unit_scope TEXT); CREATE TABLE ivl_episodes(id INTEGER, admission_id INTEGER, start_time TEXT, end_time TEXT);")
        conn.execute("INSERT INTO admissions VALUES (1, '2026-01-01', 'rao')")
        conn.execute("INSERT INTO ivl_episodes VALUES (1, 1, '2025-12-31 12:00:00', ?)", (end_time,))
        conn.commit(); conn.close()

    # Exercise the real aggregate manager used by legacy builders, rather
    # than StatisticsRepository(db_paths), which reads every source directly.
    manager = create_multi_db_analytics_manager(
        paths, start_dt="2026-01-01", end_dt="2026-01-02"
    )
    try:
        aggregate = manager.get_connection()
        rows = aggregate.execute(
            "SELECT id, admission_id, start_time, end_time, analytics_source_id "
            "FROM ivl_episodes ORDER BY analytics_source_id"
        ).fetchall()
        assert len(rows) == 2
        assert len({row[0] for row in rows}) == len({row[1] for row in rows}) == 2
        assert {row[4] for row in rows} == {"db0", "db1"}

        result = AnalyticsEngine(StatisticsRepository(manager)).snapshot(
            MetricScope.RAO,
            AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 1, 2)),
            metric_ids=("g42", "g44", "g45"),
        ).results
        assert result["g42"].numerator == 2
        # The ongoing episode contributes one day; the episode ending at noon
        # contributes one half-day after half-open clipping.
        assert result["g44"].numerator == pytest.approx(1.5)
        assert result["g45"].artifact["series"] == ({"label": "2026-01", "value": 1.5},)
        assert len(result["g42"].artifact["source_case_ids"]) == 2
    finally:
        manager.close_connection()


def test_cache_key_separates_canonical_cohort_and_comparison_state():
    from rem_card.services.analytics.platform import SnapshotCache
    cache = SnapshotCache(); engine = AnalyticsEngine(GraphRepository(), cache=cache); period = AnalyticsPeriod(datetime(2026, 1, 1), datetime(2026, 2, 1))
    women = CohortDefinition(scope=MetricScope.RAO, filters=(CohortFilter("sex", "equals", "ж"),)); men = CohortDefinition(scope=MetricScope.RAO, filters=(CohortFilter("sex", "equals", "м"),))
    first = engine.snapshot(MetricScope.RAO, period, women, ("g1",)); again = engine.snapshot(MetricScope.RAO, period, women, ("g1",)); other = engine.snapshot(MetricScope.RAO, period, men, ("g1",))
    assert first is again and other is not first and cache.hits >= 1


def test_legacy_structured_row_uses_the_same_cohort_as_source_cases():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
      CREATE TABLE admissions(id INTEGER, patient_id INTEGER, admission_datetime TEXT, transfer_datetime TEXT, death_datetime TEXT, outcome TEXT, patient_age REAL, patient_age_unit TEXT, patient_gender TEXT, source_department TEXT, diagnosis_code TEXT, diagnosis_text TEXT, bed_number INTEGER, recovery_bed_stay INTEGER);
      CREATE TABLE operations(id INTEGER, admission_id INTEGER, operation_datetime TEXT, description TEXT);
      CREATE TABLE transfusions(id INTEGER, admission_id INTEGER, datetime TEXT, type TEXT, volume_ml REAL);
      CREATE TABLE ivl_episodes(id INTEGER, admission_id INTEGER, start_time TEXT, end_time TEXT);
    """)
    conn.executemany("INSERT INTO admissions VALUES (?, ?, '2026-01-02', NULL, NULL, NULL, 40, 'years', ?, 'ПСО', 'J18', 'Тест', 1, 0)", [(1, 1, 'ж'), (2, 2, 'м')])
    conn.commit()
    engine = AnalyticsEngine(StatisticsRepository(AnalyticsConnectionManager(conn, db_path="fixture")))
    snapshot = engine.snapshot(MetricScope.RAO, AnalyticsPeriod.from_values("2026-01-01", "2026-01-03"), CohortDefinition(scope=MetricScope.RAO, filters=(CohortFilter("sex", "equals", "ж"),)), ("s1",))
    result = snapshot.results["s1"]
    assert len(result.source_cases) == result.denominator == 1
    assert any(row["value"] == "1" for row in result.rows)


@pytest.mark.parametrize("include_recovery, expected", [(False, "1"), (True, "2")])
def test_legacy_rao_rows_and_source_cases_share_recovery_cohort(include_recovery, expected):
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
      CREATE TABLE admissions(id INTEGER, patient_id INTEGER, admission_datetime TEXT, transfer_datetime TEXT, death_datetime TEXT, outcome TEXT, patient_age REAL, patient_age_unit TEXT, patient_gender TEXT, source_department TEXT, diagnosis_code TEXT, diagnosis_text TEXT, bed_number INTEGER, recovery_bed_stay INTEGER);
      CREATE TABLE operations(id INTEGER, admission_id INTEGER, operation_datetime TEXT, description TEXT); CREATE TABLE transfusions(id INTEGER, admission_id INTEGER, datetime TEXT, type TEXT, volume_ml REAL); CREATE TABLE ivl_episodes(id INTEGER, admission_id INTEGER, start_time TEXT, end_time TEXT);
    """)
    conn.executemany("INSERT INTO admissions VALUES (?, ?, '2026-01-02', NULL, NULL, NULL, 40, 'years', 'ж', 'ПСО', 'J18', 'Тест', 1, ?)", [(1, 1, 0), (2, 2, 1)])
    conn.commit(); manager = AnalyticsConnectionManager(conn, db_path="recovery-fixture")
    cohort = CohortDefinition(scope=MetricScope.RAO, include_recovery_beds=include_recovery)
    results = AnalyticsEngine(StatisticsRepository(manager)).snapshot(
        MetricScope.RAO,
        AnalyticsPeriod.from_values("2026-01-01", "2026-01-03"),
        cohort,
        ("s1", "s_recovery"),
    ).results
    result = results["s1"]
    try:
        assert result.denominator == len(result.source_cases) == int(expected)
        assert any(row["value"] == expected for row in result.rows)
        recovery = results["s_recovery"]
        assert len(recovery.source_cases) == 1
        assert recovery.source_cases[0].attributes["recovery_bed_stay"] == 1
        summary = next(row for row in recovery.rows if row["name"] == "Проведено через пробуждение")
        assert summary["display_value"].startswith("1 (")
    finally:
        conn.close()


def test_carry_in_mortality_and_intervention_indexes_use_interval_population():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
      CREATE TABLE admissions(id INTEGER, patient_id INTEGER, admission_datetime TEXT, transfer_datetime TEXT,
        death_datetime TEXT, outcome TEXT, patient_age REAL, patient_age_unit TEXT, patient_gender TEXT,
        source_department TEXT, diagnosis_code TEXT, diagnosis_text TEXT, recovery_bed_stay INTEGER,
        cardiac_arrest_measures_json TEXT);
      CREATE TABLE operations(id INTEGER, admission_id INTEGER, operation_datetime TEXT, description TEXT);
      CREATE TABLE transfusions(id INTEGER, admission_id INTEGER, datetime TEXT, type TEXT, volume_ml REAL);
      CREATE TABLE ivl_episodes(id INTEGER, admission_id INTEGER, start_time TEXT, end_time TEXT);
    """)
    conn.execute(
        "INSERT INTO admissions VALUES (1, 1, '2025-12-30', NULL, '2026-01-02', 'умер', "
        "40, 'years', 'м', 'ПСО', 'J18', 'Тест', 0, ?)",
        ('{"death_protocol":{"doctor":"Врач А"}}',),
    )
    conn.execute(
        "INSERT INTO admissions VALUES (2, 2, '2026-01-01', '2026-01-03', NULL, 'выписан', "
        "40, 'years', 'м', 'ПСО', 'J18', 'Тест', 0, NULL)"
    )
    conn.execute("INSERT INTO operations VALUES (1, 1, '2026-01-01 10:00', 'Операция')")
    conn.execute("INSERT INTO ivl_episodes VALUES (1, 1, '2026-01-01', '2026-01-02')")
    conn.commit()
    manager = AnalyticsConnectionManager(conn, db_path="carry-in-indexes")
    try:
        builder = DetailedStatisticsReportBuilder(manager, "2026-01-01", "2026-01-03")
        payload = builder.calculate_payload()
        assert payload["N"] == 1 and payload["N_interval"] == 2
        assert payload["mortality_pct"] == 50
        assert payload["intensity_index"] == 1
        assert payload["technology_index"] == 50

        s8 = {row["name"]: row for row in builder.structured_section_rows("s8", payload)}
        assert "18–44: 1/2 (50.0%)" in s8["8.1 Летальность по группам"]["display_value"]
        assert "Врач А: 1 (100.0% от смертей; 50.0% от пребываний периода)" in s8[
            "8.2 Смертность по врачу протокола"
        ]["display_value"]
        s18 = {row["name"]: row for row in builder.structured_section_rows("s18", payload)}
        assert s18["19.2 Индекс интенсивности лечения"]["value"] == "1.00"
        assert s18["19.5 Индекс технологичности"]["display_value"] == "50.00%"

        engine = AnalyticsEngine(StatisticsRepository(manager)).snapshot(
            MetricScope.RAO,
            AnalyticsPeriod.from_values("2026-01-01", "2026-01-03"),
            metric_ids=("rao.mortality",),
        )
        assert engine.results["rao.mortality"].value == 50
        html = build_detailed_statistics_report_html(
            manager, "2026-01-01", "2026-01-03", ("s8", "s18"),
        )
        assert "18–44: 1/2 (50.0%)" in html
        assert "50.0% от пребываний периода" in html
    finally:
        conn.close()


def test_generic_engine_builds_identity_safe_multi_db_legacy_source(tmp_path):
    paths = []
    for number, sex in enumerate(("ж", "м")):
        path = tmp_path / f"m{number}.db"; paths.append(str(path)); conn = sqlite3.connect(path)
        conn.executescript("""
          CREATE TABLE admissions(id INTEGER, patient_id INTEGER, admission_datetime TEXT, transfer_datetime TEXT, death_datetime TEXT, outcome TEXT, patient_age REAL, patient_age_unit TEXT, patient_gender TEXT, source_department TEXT, diagnosis_code TEXT, diagnosis_text TEXT, bed_number INTEGER, recovery_bed_stay INTEGER);
          CREATE TABLE operations(id INTEGER, admission_id INTEGER, operation_datetime TEXT, description TEXT); CREATE TABLE transfusions(id INTEGER, admission_id INTEGER, datetime TEXT, type TEXT, volume_ml REAL); CREATE TABLE ivl_episodes(id INTEGER, admission_id INTEGER, start_time TEXT, end_time TEXT);
        """)
        conn.execute("INSERT INTO admissions VALUES (1, 1, '2026-01-02', NULL, NULL, NULL, 40, 'years', ?, 'ПСО', 'J18', 'x', 1, 0)", (sex,)); conn.commit(); conn.close()
    engine = AnalyticsEngine(StatisticsRepository(None, db_paths=paths))
    snapshot = engine.snapshot(MetricScope.RAO, AnalyticsPeriod.from_values("2026-01-01", "2026-01-03"), CohortDefinition(scope=MetricScope.RAO, filters=(CohortFilter("sex", "equals", "ж"),)), ("s1",))
    assert len(snapshot.results["s1"].source_cases) == snapshot.results["s1"].denominator == 1
    assert any(row["value"] == "1" for row in snapshot.results["s1"].rows)


def test_generic_engine_builds_filtered_identity_safe_operblock_multi_db_source(tmp_path):
    paths = []
    for index, table_code in enumerate(("planned", "emergency")):
        path = tmp_path / f"oper-{index}.db"; paths.append(str(path)); conn = sqlite3.connect(path)
        ensure_unified_schema(conn); _apply_operblock_schema(conn.cursor())
        conn.execute("INSERT INTO patients (id, full_name) VALUES (1, 'Тест')")
        conn.execute("INSERT INTO admissions (id, patient_id, history_number, bed_number, admission_datetime) VALUES (1, 1, 'ИБ', 1, '2026-01-02')")
        conn.execute("INSERT INTO operation_cases (id, patient_id, admission_id, table_code, status, created_at, started_at, planned_anesthesiologist, planned_operation_name) VALUES (1, 1, 1, ?, 'closed', '2026-01-02', '2026-01-02 09:00:00', 'Иванов', 'Операция')", (table_code,))
        conn.commit(); conn.close()
    engine = AnalyticsEngine(StatisticsRepository(None, db_paths=paths))
    cohort = CohortDefinition(scope=MetricScope.OPERBLOCK, filters=(CohortFilter("table_code", "equals", "planned"),))
    snapshot = engine.snapshot(MetricScope.OPERBLOCK, AnalyticsPeriod.from_values("2026-01-01", "2026-01-03"), cohort, ("ob1",))
    result = snapshot.results["ob1"]
    assert len(result.source_cases) == result.denominator == 1
    assert result.rows[0]["value"] == "1"


@pytest.mark.parametrize("field,operator,value", [
    ("table_code", "equals", "planned"), ("status", "equals", "closed"),
    ("priority", "equals", "planned"), ("diagnosis", "contains", "J18"),
    ("personnel", "contains", "Иванов"),
])
def test_real_operblock_ob1_builder_equals_filtered_source_case_count(field, operator, value):
    conn = sqlite3.connect(":memory:"); ensure_unified_schema(conn); _apply_operblock_schema(conn.cursor())
    conn.execute("INSERT INTO patients (full_name) VALUES ('Иванов')"); patient_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO admissions (patient_id, history_number, bed_number, admission_datetime, diagnosis_code, diagnosis_text) VALUES (?, 'ИБ-1', 1, '2026-01-02', 'J18', 'Пневмония')", (patient_id,)); admission_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO operation_cases (patient_id, admission_id, table_code, status, created_at, started_at, planned_anesthesiologist, planned_operation_name) VALUES (?, ?, 'planned', 'closed', '2026-01-02', '2026-01-02 09:00:00', 'Иванов', 'Операция')", (patient_id, admission_id))
    conn.execute("INSERT INTO patients (full_name) VALUES ('Петров')"); second_patient_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO admissions (patient_id, history_number, bed_number, admission_datetime, diagnosis_code, diagnosis_text) VALUES (?, 'ИБ-2', 2, '2026-01-02', 'K35', 'Другое')", (second_patient_id,)); second_admission_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO operation_cases (patient_id, admission_id, table_code, status, created_at, started_at, planned_anesthesiologist, planned_operation_name) VALUES (?, ?, 'emergency', 'active', '2026-01-02', '2026-01-02 10:00:00', 'Петров', 'Другая')", (second_patient_id, second_admission_id)); conn.commit()
    manager = AnalyticsConnectionManager(conn, db_path="operblock-fixture"); period = AnalyticsPeriod.from_values("2026-01-01", "2026-01-03")
    cohort = CohortDefinition(scope=MetricScope.OPERBLOCK, filters=(CohortFilter(field, operator, value),))
    snapshot_manager, cases = materialize_cohort_snapshot(manager, MetricScope.OPERBLOCK, period, cohort)
    try:
        row = OperBlockStatisticsReportBuilder(snapshot_manager, "2026-01-01", "2026-01-03").structured_indicator_rows(("ob1",))["ob1"]
        assert row["value"] == "1" and len(cases) == 1
    finally: snapshot_manager.close_connection(); conn.close()
