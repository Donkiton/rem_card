from rem_card.services.analytics.detailed_statistics_service import SECTION_GROUPS
from rem_card.services.analytics.operblock_statistics_service import OPERBLOCK_SECTION_GROUPS
from rem_card.services.analytics.platform import MetricScope, PopulationKind, default_metric_registry
from rem_card.ui.analytics.graphs_catalog import GRAPH_GROUPS


def test_registry_covers_existing_selectors_and_mandatory_kpis():
    registry = default_metric_registry()
    assert registry.validate_coverage(section_groups=SECTION_GROUPS, operblock_groups=OPERBLOCK_SECTION_GROUPS, graph_groups=GRAPH_GROUPS) == ()
    assert registry.get("rao.admissions").scope is MetricScope.RAO
    assert registry.get("operblock.total").scope is MetricScope.OPERBLOCK
    assert registry.get("s1").supports_source_cases


def test_all_legacy_definitions_have_static_non_placeholder_metadata():
    registry = default_metric_registry()
    keys = [key for group in SECTION_GROUPS.values() for key in group] + [key for group in OPERBLOCK_SECTION_GROUPS.values() for key in group]
    for key in keys:
        definition = registry.get(key)
        assert definition.formula and "Payload" not in definition.formula and "точная формула" not in definition.formula
        assert definition.unit and definition.source_tables and definition.time_basis


def test_every_graph_has_reviewable_method_and_dataset_contract():
    registry = default_metric_registry()
    keys = [key for group in GRAPH_GROUPS.values() for key in group]
    for key in keys:
        definition = registry.get(key)
        assert definition.kind.value == "graph"
        assert definition.formula and "Выбранный график" not in definition.formula
        assert definition.time_basis and definition.source_tables
        assert definition.numerator and definition.unit
    assert registry.get("g29").denominator == "Все госпитализации месяца"
    assert registry.get("g41").source_tables == ("admissions", "events", "census_events")
    assert registry.get("g45").source_tables == ("admissions", "ivl_episodes")
    assert registry.get("g9").population_kind == PopulationKind.ADMISSION_EVENT.value
    assert registry.get("g41").population_kind == PopulationKind.ADMISSION_EVENT.value
    assert "пять" in registry.get("g50").formula.lower()


def test_all_core_kpis_persist_a_non_placeholder_population_contract():
    registry = default_metric_registry()
    core_ids = (
        "rao.admissions", "rao.bed_days", "rao.average_los", "rao.deaths", "rao.mortality",
        "rao.transfers", "rao.recovery_cases", "rao.ivl_patients", "rao.operations", "rao.transfusions",
        "operblock.total", "operblock.closed", "operblock.active", "operblock.emergency", "operblock.planned",
        "operblock.night", "operblock.average_room_duration",
    )
    for metric_id in core_ids:
        definition = registry.get(metric_id)
        assert PopulationKind(definition.population_kind)
        assert definition.population_kind not in {"", "general", "all_cases"}
    assert registry.get("rao.admissions").population_kind == PopulationKind.ADMISSION_EVENT.value
    assert registry.get("rao.deaths").population_kind == PopulationKind.TERMINAL_EVENT.value
    assert registry.get("rao.bed_days").population_kind == PopulationKind.INTERVAL_CENSUS.value


def test_core_kpi_time_rules_match_their_population_contracts():
    registry = default_metric_registry()
    assert "admission_datetime" in registry.get("rao.admissions").time_basis
    assert "пересекает" in registry.get("rao.bed_days").time_basis
    assert "пересекает" in registry.get("rao.average_los").time_basis
    assert "death_datetime" in registry.get("rao.deaths").time_basis
    assert "знаменатель" in registry.get("rao.mortality").time_basis
    assert "первому терминальному" in registry.get("rao.transfers").time_basis
    assert "койке пробуждения" in registry.get("rao.recovery_cases").time_basis
    assert "Эпизод ИВЛ пересекает" in registry.get("rao.ivl_patients").time_basis
    assert "операции попадает" in registry.get("rao.operations").time_basis
    assert "переливания попадает" in registry.get("rao.transfusions").time_basis
    assert "started_at операции" in registry.get("operblock.average_room_duration").time_basis


def test_kpi_methodology_html_exposes_metric_specific_time_rules():
    from rem_card.services.analytics.platform import AnalyticsPeriod, CohortDefinition, analytics_context_html

    registry = default_metric_registry()
    html = analytics_context_html(
        period=AnalyticsPeriod.from_values("2026-01-01", "2026-01-03"),
        cohort=CohortDefinition(scope=MetricScope.RAO),
        definitions=(
            registry.get("rao.bed_days"),
            registry.get("rao.deaths"),
            registry.get("rao.operations"),
        ),
    )
    assert "Интервал пребывания пересекает" in html
    assert "death_datetime попадает" in html
    assert "Дата связанной операции попадает" in html
