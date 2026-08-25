"""Версионированный read-only слой клинической аналитики RemCard.

Этот модуль намеренно не меняет клинические SQLite базы.  Он превращает строки
нескольких снимков БД в изолированные производные случаи, чтобы один и тот же
локальный ``id`` из архивных файлов никогда не склеивался с другим пациентом.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections import OrderedDict, Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from enum import Enum
from html import escape as html_escape
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from rem_card.services.analytics.period import AnalyticsDatePeriod, normalize_analytics_period, parse_analytics_datetime
from rem_card.services.patient_bed_management.recovery_beds import RECOVERY_BED_NUMBERS


REGISTRY_VERSION = "1.0"
SAVED_VIEW_SCHEMA_VERSION = 2


class MetricScope(str, Enum):
    RAO = "rao"
    OPERBLOCK = "operblock"


class MetricKind(str, Enum):
    COUNT = "count"
    SUM = "sum"
    RATIO = "ratio"
    AVERAGE = "average"
    DISTRIBUTION = "distribution"
    TEXT = "text"
    GRAPH = "graph"


class PopulationKind(str, Enum):
    ADMISSION_EVENT = "admission_event"
    INTERVAL_CENSUS = "interval_census"
    TERMINAL_EVENT = "terminal_event"
    RECOVERY_SUBPOPULATION = "recovery_subpopulation"
    PROCEDURE_EVENT = "procedure_event"


def population_kind_label(value: str | PopulationKind) -> str:
    labels = {
        PopulationKind.ADMISSION_EVENT.value: "событие поступления",
        PopulationKind.INTERVAL_CENSUS.value: "интервал пребывания / census",
        PopulationKind.TERMINAL_EVENT.value: "терминальное событие смерти",
        PopulationKind.RECOVERY_SUBPOPULATION.value: "подпопуляция коек пробуждения",
        PopulationKind.PROCEDURE_EVENT.value: "событие процедуры",
    }
    code = value.value if isinstance(value, PopulationKind) else str(value)
    return f"{labels.get(code, code)} ({code})"


@dataclass(frozen=True)
class AnalyticsPeriod:
    """Совместимая half-open оболочка вокруг календарного периода."""

    start: datetime
    end: datetime

    @classmethod
    def from_values(cls, start: Any, end: Any) -> "AnalyticsPeriod":
        period = normalize_analytics_period(start, end)
        return cls(period.start_inclusive, period.end_exclusive)

    @classmethod
    def previous_calendar_year(cls, period: "AnalyticsPeriod") -> "AnalyticsPeriod":
        def shift(value: datetime) -> datetime:
            try:
                return value.replace(year=value.year - 1)
            except ValueError:  # 29 февраля
                return value.replace(year=value.year - 1, day=28)
        return cls(shift(period.start), shift(period.end))

    @property
    def sql_bounds(self) -> tuple[str, str]:
        return (self.start.strftime("%Y-%m-%d %H:%M:%S"), self.end.strftime("%Y-%m-%d %H:%M:%S"))

    @property
    def key(self) -> tuple[str, str]:
        return self.sql_bounds


def _effective_rao_outcome(attributes: Mapping[str, Any], period: AnalyticsPeriod) -> str:
    """Derived outcome at the selected cutoff, ordered by terminal cause."""
    raw = str(attributes.get("outcome") or "").strip()
    raw_folded = raw.casefold()
    death = parse_analytics_datetime(attributes.get("death_datetime"))
    transfer = parse_analytics_datetime(attributes.get("transfer_datetime"))
    death_is_first = death is not None and (transfer is None or death <= transfer)
    transfer_is_first = transfer is not None and (death is None or transfer < death)
    if death_is_first and period.start <= death < period.end:
        return "умер"
    if transfer_is_first and period.start <= transfer < period.end:
        # transfer_datetime also represents ordinary discharge in legacy DBs.
        # Preserve an explicit non-death outcome; correct only contradictions.
        return "переведен" if raw_folded in {"", "умер", "death", "deceased"} else raw
    terminal = min((value for value in (death, transfer) if value is not None), default=None)
    if terminal is not None and terminal >= period.end:
        return "в отделении"
    if raw_folded in {"умер", "death", "deceased"}:
        return "без исхода в периоде"
    return raw or "в отделении"


def normalize_age_years(age_value: Any, age_unit: Any = "") -> float | None:
    """Нормализует сохранённый возраст в годы по legacy-контракту RemCard."""
    if age_value is None:
        return None
    try:
        age = float(age_value)
    except (TypeError, ValueError):
        return None
    return age / 12.0 if "меся" in str(age_unit or "").strip().casefold() else age


def is_recovery_case(attributes: Mapping[str, Any]) -> bool:
    """Единый контракт recovery: явный флаг либо номер койки пробуждения."""
    if bool(attributes.get("recovery_bed_stay")):
        return True
    try:
        return int(attributes.get("bed_number") or 0) in RECOVERY_BED_NUMBERS
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class MetricDefinition:
    id: str
    version: str
    scope: MetricScope
    title: str
    description: str
    kind: MetricKind
    formula: str
    numerator: str | None = None
    denominator: str | None = None
    unit: str = "случаев"
    time_basis: str = "Дата начала случая попадает в [начало, конец)."
    population_kind: str = "admission_event"
    inclusions: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    source_tables: tuple[str, ...] = ()
    quality_status: str = "требует подтверждения врача"
    legacy_key: str | None = None
    report_key: str | None = None
    graph_key: str | None = None
    supports_source_cases: bool = True
    is_kpi: bool = False


@dataclass(frozen=True)
class CohortFilter:
    field: str
    operator: str = "equals"
    value: Any = None

    def matches(self, case: "SourceCase") -> bool:
        current = case.attributes.get(self.field)
        wanted = self.value
        if self.operator == "equals":
            return str(current or "").casefold() == str(wanted or "").casefold()
        if self.operator == "in":
            return str(current or "").casefold() in {str(x).casefold() for x in (wanted or ())}
        if self.operator == "contains":
            return str(wanted or "").casefold() in str(current or "").casefold()
        if self.operator == "gte":
            return current is not None and current >= wanted
        if self.operator == "lte":
            return current is not None and current <= wanted
        if self.operator == "truthy":
            return bool(current) is bool(wanted)
        return False


@dataclass(frozen=True)
class CohortDefinition:
    name: str = "Все случаи"
    scope: MetricScope | None = None
    filters: tuple[CohortFilter, ...] = ()
    include_recovery_beds: bool = False

    def serialize(self) -> dict[str, Any]:
        return {"name": self.name, "scope": self.scope.value if self.scope else None,
                "filters": [asdict(item) for item in self.filters],
                "include_recovery_beds": self.include_recovery_beds}

    @classmethod
    def deserialize(cls, payload: Mapping[str, Any]) -> "CohortDefinition":
        scope = payload.get("scope")
        return cls(str(payload.get("name") or "Все случаи"), MetricScope(scope) if scope else None,
                   tuple(CohortFilter(**dict(item)) for item in payload.get("filters", ())),
                   bool(payload.get("include_recovery_beds")))

    def apply(self, cases: Iterable["SourceCase"]) -> tuple["SourceCase", ...]:
        return tuple(case for case in cases if (self.scope is None or case.scope == self.scope)
                     and (self.include_recovery_beds or not is_recovery_case(case.attributes))
                     and all(item.matches(case) for item in self.filters))


@dataclass(frozen=True)
class SourceCase:
    source_db_id: str
    local_id: str
    scope: MetricScope
    started_at: datetime | None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    inclusion_reason: str = "Соответствует критериям производной популяции."

    @property
    def id(self) -> str:
        return f"{self.source_db_id}:{self.scope.value}:{self.local_id}"


@dataclass(frozen=True)
class MetricResult:
    definition: MetricDefinition
    value: Any
    numerator: float | int | None = None
    denominator: float | int | None = None
    source_cases: tuple[SourceCase, ...] = ()
    explanation: str = ""
    rows: tuple[Mapping[str, Any], ...] = ()
    artifact: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class GraphMetricArtifact:
    """Authoritative, serializable graph payload produced before rendering."""
    metric_id: str
    title: str
    period: tuple[str, str]
    source_case_ids: tuple[str, ...]
    numerator: int | float | None
    denominator: int | float | None
    unit: str
    time_basis: str
    summary: str
    series: tuple[Mapping[str, Any], ...] = ()
    chart_kind: str = "bar"

    def serialize(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PeriodComparison:
    current: MetricResult
    previous: MetricResult | None
    absolute_delta: float | None
    relative_delta_percent: float | None
    previous_period: AnalyticsPeriod
    message: str = ""


@dataclass(frozen=True)
class DataQualityIssue:
    code: str
    severity: str
    message: str
    source_case_id: str | None = None


@dataclass(frozen=True)
class DataQualityReport:
    issues: tuple[DataQualityIssue, ...] = ()

    @property
    def has_errors(self) -> bool:
        return any(item.severity == "error" for item in self.issues)


@dataclass(frozen=True)
class StatisticsSnapshot:
    registry_version: str
    scope: MetricScope
    period: AnalyticsPeriod
    cohort: CohortDefinition
    cases: tuple[SourceCase, ...]
    results: Mapping[str, MetricResult]
    quality: DataQualityReport
    db_fingerprints: tuple[str, ...] = ()


class MetricRegistry:
    def __init__(self, definitions: Iterable[MetricDefinition] = ()):
        items = tuple(definitions)
        if len({item.id for item in items}) != len(items):
            raise ValueError("В реестре аналитики есть повторяющиеся ID метрик.")
        for item in items:
            if not item.formula or not item.time_basis or not item.source_tables:
                raise ValueError(f"Неполное определение метрики: {item.id}")
            try:
                PopulationKind(item.population_kind)
            except ValueError as error:
                raise ValueError(f"Не задан корректный population_kind: {item.id}") from error
        self._definitions = {item.id: item for item in items}

    def __iter__(self):
        return iter(self._definitions.values())

    def get(self, metric_id: str) -> MetricDefinition:
        return self._definitions[metric_id]

    def for_scope(self, scope: MetricScope) -> tuple[MetricDefinition, ...]:
        return tuple(item for item in self._definitions.values() if item.scope == scope)

    def ids(self) -> tuple[str, ...]:
        return tuple(self._definitions)

    def validate_coverage(self, *, section_groups: Mapping[str, Mapping[str, str]] | None = None,
                          operblock_groups: Mapping[str, Mapping[str, str]] | None = None,
                          graph_groups: Mapping[str, Mapping[str, str]] | None = None) -> tuple[str, ...]:
        missing: list[str] = []
        for groups in (section_groups, operblock_groups, graph_groups):
            for items in (groups or {}).values():
                for key in items:
                    if key not in self._definitions:
                        missing.append(key)
        for metric_id, definition in self._definitions.items():
            if metric_id.startswith("ob") and definition.scope is not MetricScope.OPERBLOCK:
                missing.append(f"invalid-scope:{metric_id}")
            if (metric_id.startswith("s") or metric_id.startswith("g") or metric_id.startswith("recovery_")) and definition.scope is not MetricScope.RAO:
                missing.append(f"invalid-scope:{metric_id}")
        return tuple(dict.fromkeys(missing))


def _legacy_definitions() -> list[MetricDefinition]:
    # Import lazily: legacy public builders remain independent of this module.
    from rem_card.services.analytics.detailed_statistics_service import SECTION_GROUPS
    from rem_card.services.analytics.operblock_statistics_service import OPERBLOCK_SECTION_GROUPS
    from rem_card.services.analytics.graph_catalog import GRAPH_GROUPS, GRAPH_METRIC_METADATA
    from rem_card.services.analytics.metric_metadata import legacy_metric_metadata
    metadata = legacy_metric_metadata()
    result: list[MetricDefinition] = []
    legacy_rao_population = {
        # s1 combines a flow row with interval rows; the section-level source
        # population is the broader interval so its bed-days/LOS rows retain
        # carry-in cases.  Each row's formula remains explicit in the builder.
        "s1": PopulationKind.INTERVAL_CENSUS.value,
        "s2": PopulationKind.INTERVAL_CENSUS.value,
        "s3": PopulationKind.ADMISSION_EVENT.value,
        "s4": PopulationKind.ADMISSION_EVENT.value,
        "s5": PopulationKind.ADMISSION_EVENT.value,
        "s6": PopulationKind.TERMINAL_EVENT.value,
        "s7": PopulationKind.TERMINAL_EVENT.value,
        "s8": PopulationKind.TERMINAL_EVENT.value,
        "s_recovery": PopulationKind.RECOVERY_SUBPOPULATION.value,
        "s9": PopulationKind.PROCEDURE_EVENT.value,
        "s10": PopulationKind.PROCEDURE_EVENT.value,
        "s11": PopulationKind.PROCEDURE_EVENT.value,
        "s12": PopulationKind.PROCEDURE_EVENT.value,
        "s13": PopulationKind.PROCEDURE_EVENT.value,
        "s14": PopulationKind.PROCEDURE_EVENT.value,
        "s16": PopulationKind.INTERVAL_CENSUS.value,
        "s17": PopulationKind.INTERVAL_CENSUS.value,
        "s18": PopulationKind.INTERVAL_CENSUS.value,
        "s19": PopulationKind.INTERVAL_CENSUS.value,
        "sx": PopulationKind.INTERVAL_CENSUS.value,
    }
    rao_inclusions = ("Госпитализации РАО, не являющиеся объединёнными или операционными случаями.",)
    rao_exclusions = ("unit_scope=operblock", "admission_type=operblock", "merged_into_admission_id задан", "самостоятельный operation_case")
    for groups, scope, prefix in ((SECTION_GROUPS, MetricScope.RAO, ""), (OPERBLOCK_SECTION_GROUPS, MetricScope.OPERBLOCK, ""), (GRAPH_GROUPS, MetricScope.RAO, "")):
        for entries in groups.values():
            for key, title in entries.items():
                is_graph = scope == MetricScope.RAO and (key.startswith("g") or key.startswith("recovery_"))
                source_kind = "графика" if is_graph else "раздела статистического отчёта"
                exact = metadata.get(key, {})
                graph_contract = GRAPH_METRIC_METADATA.get(key, {}) if is_graph else {}
                result.append(MetricDefinition(id=f"{prefix}{key}", version=REGISTRY_VERSION, scope=scope, title=title,
                    description=f"{title}. Определение связано с {source_kind} RemCard.", kind=MetricKind.GRAPH if is_graph else MetricKind.COUNT,
                    formula=graph_contract.get("formula") or exact.get("formula") or f"Структурированная строка «{title}» legacy-отчёта.",
                    numerator=graph_contract.get("numerator") or exact.get("numerator"), denominator=graph_contract.get("denominator") or exact.get("denominator"),
                    inclusions=rao_inclusions if scope == MetricScope.RAO else ("operation_cases по started_at",),
                    exclusions=rao_exclusions if scope == MetricScope.RAO else ("cancelled", "deleted"),
                    source_tables=tuple(graph_contract.get("source_tables") or (("admissions",) if scope == MetricScope.RAO else ("operation_cases",))),
                    legacy_key=key if not prefix else None, report_key=key if key.startswith(("s", "ob")) else None,
                    graph_key=key if key.startswith("g") or "recovery" in key else None,
                    unit=graph_contract.get("unit") or ("артефакт" if is_graph else exact.get("unit", "строка отчёта")),
                    time_basis=graph_contract.get("time_basis") or "Дата начала случая попадает в [начало, конец).",
                    population_kind=graph_contract.get("population_kind") or (
                        legacy_rao_population.get(key, PopulationKind.ADMISSION_EVENT.value)
                        if scope == MetricScope.RAO else PopulationKind.PROCEDURE_EVENT.value
                    )))
    known = {
        "rao.admissions": (MetricScope.RAO, "Госпитализации РАО", MetricKind.COUNT, "Количество производных госпитализаций РАО", "случаев"),
        "rao.bed_days": (MetricScope.RAO, "Койко-дни", MetricKind.SUM, "Сумма дней пересечения пребывания с периодом", "койко-дней"),
        "rao.average_los": (MetricScope.RAO, "Средняя длительность пребывания", MetricKind.AVERAGE, "Койко-дни / госпитализации", "суток"),
        "rao.deaths": (MetricScope.RAO, "Смерти", MetricKind.COUNT, "Госпитализации с датой смерти", "случаев"),
        "rao.mortality": (MetricScope.RAO, "Летальность", MetricKind.RATIO, "Смерти / госпитализации × 100", "%"),
        "rao.transfers": (MetricScope.RAO, "Переводы и текущие исходы", MetricKind.DISTRIBUTION, "Распределение исходов", "случаев"),
        "rao.recovery_cases": (MetricScope.RAO, "Случаи пробуждения", MetricKind.COUNT, "Госпитализации с признаком койки пробуждения", "случаев"),
        "rao.ivl_patients": (MetricScope.RAO, "Пациенты на ИВЛ", MetricKind.COUNT, "Госпитализации с признаком ИВЛ", "случаев"),
        "rao.operations": (MetricScope.RAO, "Операции", MetricKind.COUNT, "Связанные операции", "случаев"),
        "rao.transfusions": (MetricScope.RAO, "Переливания", MetricKind.COUNT, "Связанные переливания", "случаев"),
        "operblock.total": (MetricScope.OPERBLOCK, "Всего операций", MetricKind.COUNT, "Количество operation_cases", "случаев"),
        "operblock.closed": (MetricScope.OPERBLOCK, "Закрытые случаи", MetricKind.COUNT, "Завершённые operation_cases", "случаев"),
        "operblock.active": (MetricScope.OPERBLOCK, "Активные случаи", MetricKind.COUNT, "Незавершённые operation_cases", "случаев"),
        "operblock.emergency": (MetricScope.OPERBLOCK, "Экстренные операции", MetricKind.COUNT, "Страта экстренных операций", "случаев"),
        "operblock.planned": (MetricScope.OPERBLOCK, "Плановые операции", MetricKind.COUNT, "Страта плановых операций", "случаев"),
        "operblock.night": (MetricScope.OPERBLOCK, "Ночные операции", MetricKind.COUNT, "Операции, начатые с 22:00 до 06:00", "случаев"),
        "operblock.average_room_duration": (MetricScope.OPERBLOCK, "Средняя длительность в операционной", MetricKind.AVERAGE, "Средняя ended_at - started_at", "минут"),
    }
    kpi_population = {
        "rao.admissions": PopulationKind.ADMISSION_EVENT.value,
        "rao.bed_days": PopulationKind.INTERVAL_CENSUS.value,
        "rao.average_los": PopulationKind.INTERVAL_CENSUS.value,
        "rao.deaths": PopulationKind.TERMINAL_EVENT.value,
        "rao.mortality": PopulationKind.TERMINAL_EVENT.value,
        "rao.transfers": PopulationKind.INTERVAL_CENSUS.value,
        "rao.recovery_cases": PopulationKind.RECOVERY_SUBPOPULATION.value,
        "rao.ivl_patients": PopulationKind.PROCEDURE_EVENT.value,
        "rao.operations": PopulationKind.PROCEDURE_EVENT.value,
        "rao.transfusions": PopulationKind.PROCEDURE_EVENT.value,
    }
    kpi_sources = {
        "rao.operations": ("admissions", "operations"),
        "rao.transfusions": ("admissions", "transfusions"),
        "rao.ivl_patients": ("admissions", "ivl_episodes"),
    }
    kpi_parts = {
        "rao.admissions": ("Госпитализации, начатые в периоде", None),
        "rao.bed_days": ("Дни пересечения пребывания с периодом", None),
        "rao.average_los": ("Койко-дни", "Госпитализации с пересечением периода"),
        "rao.deaths": ("Смерти с death_datetime в периоде", None),
        "rao.mortality": ("Смерти с death_datetime в периоде", "Госпитализации общего RAO-периода"),
        "rao.transfers": ("Госпитализации по исходу", None),
        "rao.recovery_cases": ("Госпитализации с recovery_bed_stay", None),
        "rao.ivl_patients": ("Госпитализации с пересекающимся эпизодом ИВЛ", None),
        "rao.operations": ("Связанные операции в периоде", None),
        "rao.transfusions": ("Связанные переливания в периоде", None),
    }
    kpi_time_basis = {
        "rao.admissions": "admission_datetime попадает в полуоткрытый интервал [начало, конец).",
        "rao.bed_days": "Интервал пребывания пересекает [начало, конец); длительность ограничивается границами периода.",
        "rao.average_los": "Интервал пребывания пересекает [начало, конец); койко-дни и госпитализации считаются по одной interval-популяции.",
        "rao.deaths": "Первое терминальное событие является смертью и death_datetime попадает в [начало, конец).",
        "rao.mortality": "Смерти фиксируются по первому терминальному событию в [начало, конец); знаменатель — RAO-пребывания, пересекающие период.",
        "rao.transfers": "RAO-пребывание пересекает период; исход определяется по первому терминальному событию до конца периода.",
        "rao.recovery_cases": "admission_datetime случая на койке пробуждения попадает в [начало, конец).",
        "rao.ivl_patients": "Эпизод ИВЛ пересекает полуоткрытый интервал [начало, конец).",
        "rao.operations": "Дата связанной операции попадает в полуоткрытый интервал [начало, конец).",
        "rao.transfusions": "Дата связанного переливания попадает в полуоткрытый интервал [начало, конец).",
    }
    for metric_id, (scope, title, kind, formula, unit) in known.items():
        numerator, denominator = kpi_parts.get(metric_id, ("operation_cases в периоде", None))
        result.append(MetricDefinition(metric_id, REGISTRY_VERSION, scope, title, formula, kind, formula,
            numerator=numerator, denominator=denominator, unit=unit,
            time_basis=kpi_time_basis.get(
                metric_id,
                "started_at операции попадает в полуоткрытый интервал [начало, конец).",
            ),
            inclusions=rao_inclusions if scope == MetricScope.RAO else ("operation_cases по started_at",),
            exclusions=rao_exclusions if scope == MetricScope.RAO else ("cancelled", "deleted"),
            source_tables=kpi_sources.get(metric_id, ("admissions",) if scope == MetricScope.RAO else ("operation_cases",)),
            quality_status="проверено технически", is_kpi=True,
            population_kind=kpi_population.get(metric_id, PopulationKind.PROCEDURE_EVENT.value)))
    return result


_DEFAULT_REGISTRY: MetricRegistry | None = None


def default_metric_registry() -> MetricRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = MetricRegistry(_legacy_definitions())
    return _DEFAULT_REGISTRY


def analytics_context_html(
    *,
    period: AnalyticsPeriod,
    cohort: CohortDefinition,
    definitions: Iterable[MetricDefinition],
    comparison_mode: str = "previous_year",
    comparison_period: tuple[str, str] | None = None,
    source_fingerprints: Iterable[str] = (),
) -> str:
    """Visible, serializable context carried by HTML and then PDF output.

    It intentionally contains only filters, definitions and hashed source
    fingerprints — never patient identifiers or source-case details.
    """
    field_names = {
        "sex": "Пол", "age": "Возраст", "diagnosis": "Диагноз / МКБ",
        "source_department": "Отделение-источник", "outcome": "Исход",
        "recovery_bed_stay": "Койка пробуждения", "table_code": "Операционная / стол",
        "status": "Статус", "priority": "Срочность", "personnel": "Персонал",
    }
    operator_names = {"equals": "равно", "contains": "содержит", "gte": "не меньше", "lte": "не больше", "truthy": "да / нет", "in": "в списке"}
    filters = "; ".join(
        f"{field_names.get(item.field, item.field)} {operator_names.get(item.operator, item.operator)} {item.value}"
        for item in cohort.filters
    ) or "Все случаи"
    comparison = "Предыдущий календарный год" if comparison_mode != "manual" else (
        f"Ручной A/B: {comparison_period[0]} — {comparison_period[1]}" if comparison_period else "Ручной A/B"
    )
    label_style = (
        "width:24%; padding:7px 10px; color:#526273; background:#f3f6f8; "
        "border-bottom:1px solid #dce3e8; font-weight:700;"
    )
    value_style = (
        "width:76%; padding:7px 10px; color:#17324d; background:#ffffff; "
        "border-bottom:1px solid #dce3e8;"
    )

    def detail_row(label: str, value: str) -> str:
        return (
            f"<tr><td style='{label_style}'>{html_escape(label)}</td>"
            f"<td style='{value_style}'>{html_escape(value)}</td></tr>"
        )

    period_text = (
        f"{period.start.date().isoformat()} — "
        f"{(period.end - timedelta(days=1)).date().isoformat()}"
    )
    cohort_text = (
        f"{filters}. Койки пробуждения в общих показателях: "
        f"{'да' if cohort.include_recovery_beds else 'нет'}"
    )
    lines = [
        "<section data-analytics-context='1' style='margin-top:18px;'>",
        "<h2 style='margin:0 0 10px 0; color:#17324d;'>Контекст аналитического отчёта</h2>",
        "<div data-analytics-context-summary='1' style='border:1px solid #d5dde3; border-radius:6px; margin-bottom:14px;'>",
        "<table style='width:100%; table-layout:fixed; border-collapse:collapse;'>",
        detail_row("Период A", period_text),
        detail_row("Сравнение", comparison),
        detail_row("Когорта", cohort_text),
        "</table></div>",
        "<h3 style='margin:12px 0 8px 0; color:#17324d;'>Показатели и методика расчёта</h3>",
    ]
    metric_ids: list[str] = []
    for definition in definitions:
        metric_ids.append(definition.id)
        lines.extend((
            "<div data-analytics-metric-card='1' data-metric-id='{id}' "
            "style='border:1px solid #d5dde3; border-radius:6px; margin:0 0 10px 0; background:#ffffff;'>".format(
                id=html_escape(definition.id),
            ),
            "<h4 style='margin:0; padding:10px 12px; color:#17324d; background:#f3f6f8; "
            "border-bottom:1px solid #d5dde3;'>{title} <span style='color:#6a7885;'>[{version}]</span></h4>".format(
                title=html_escape(definition.title), version=html_escape(definition.version),
            ),
            "<table data-analytics-definition-table='1' style='width:100%; table-layout:fixed; border-collapse:collapse;'>",
            detail_row("Формула", definition.formula),
            detail_row("Числитель", definition.numerator or "—"),
            detail_row("Знаменатель", definition.denominator or "—"),
            detail_row("Единица измерения", definition.unit),
            detail_row("Правило времени", definition.time_basis),
            detail_row("Популяция", population_kind_label(definition.population_kind)),
            detail_row("Источники данных", ", ".join(definition.source_tables) or "—"),
            "</table></div>",
        ))
    lines.extend((
        "<div data-analytics-provenance='1' style='border:1px solid #d5dde3; border-radius:6px; margin-top:12px;'>",
        "<table style='width:100%; table-layout:fixed; border-collapse:collapse;'>",
        detail_row("Выбранные ID", ", ".join(metric_ids) or "—"),
    ))
    fingerprints = tuple(str(item) for item in source_fingerprints if item)
    if fingerprints:
        lines.append(detail_row("Отпечатки источников", ", ".join(fingerprints)))
    lines.append("</table></div></section>")
    return "".join(lines)


class StatisticsRepository:
    """Извлекает только исходные строки, без DDL, WAL и записи в клиническую БД."""
    def __init__(self, source: Any = None, *, db_paths: Sequence[str] = ()):
        self.source = source
        self.db_paths = tuple(str(item) for item in db_paths if item)

    @staticmethod
    def _fingerprint(path: str) -> str:
        try:
            stat = os.stat(path)
            payload = f"{os.path.abspath(path)}:{stat.st_size}:{stat.st_mtime_ns}"
        except OSError:
            payload = str(path)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def fingerprints(self) -> tuple[str, ...]:
        paths = self._paths()
        return tuple(self._fingerprint(path) for path in paths) or ("manager",)

    def clinical_fingerprints(self) -> tuple[str, ...]:
        """Только стабильные отпечатки исходных клинических файлов.

        ``manager`` и in-memory cohort snapshots пригодны для cache key, но
        недопустимы как якобы источник в экспортируемом клиническом отчёте.
        """
        return tuple(self._fingerprint(path) for path in self._paths())

    def source_name(self, source_id: str) -> str:
        for path in self._paths():
            if self._fingerprint(path) == source_id:
                return Path(path).name
        return "Текущая БД" if source_id in {"manager", "connection"} else str(source_id)

    def _paths(self) -> tuple[str, ...]:
        if self.db_paths:
            return self.db_paths
        path = getattr(self.source, "db_path", "")
        return (str(path),) if path and os.path.isfile(str(path)) else ()

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
        return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())

    @staticmethod
    def _rows(conn: sqlite3.Connection, query: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        try:
            cursor = conn.execute(query, tuple(params))
        except sqlite3.Error:
            return []
        columns = [item[0] for item in cursor.description or ()]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def _connections(self):
        paths = self._paths()
        if paths:
            for path in paths:
                conn = sqlite3.connect(path, check_same_thread=False)
                conn.execute("PRAGMA query_only=ON")
                try:
                    yield self._fingerprint(path), conn
                finally:
                    conn.close()
            return
        manager = self.source
        if isinstance(manager, sqlite3.Connection):
            yield "connection", manager
            return
        if manager is not None and hasattr(manager, "get_connection"):
            yield "manager", manager.get_connection()

    @staticmethod
    def _parse(value: Any) -> datetime | None:
        return parse_analytics_datetime(value)

    @classmethod
    def _terminal(cls, row: Mapping[str, Any]) -> datetime | None:
        """Earliest valid terminal event; death and transfer may coexist."""
        values = (cls._parse(row.get("transfer_datetime")), cls._parse(row.get("death_datetime")))
        return min((value for value in values if value is not None), default=None)

    @staticmethod
    def _patient_full_name(patient: Mapping[str, Any]) -> str:
        full_name = str(patient.get("full_name") or "").strip()
        if full_name:
            return full_name
        return " ".join(
            str(patient.get(key) or "").strip()
            for key in ("last_name", "first_name", "middle_name")
            if str(patient.get(key) or "").strip()
        )

    def _patient_index(self, conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        if not self._table_exists(conn, "patients"):
            return {}
        return {str(item.get("id")): item for item in self._rows(conn, "SELECT * FROM patients")}

    def _rao_related_events(self, conn: sqlite3.Connection) -> dict[str, dict[str, list[dict[str, Any]]]]:
        related: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for table_name, attribute in (("operations", "operations"), ("transfusions", "transfusions")):
            if not self._table_exists(conn, table_name):
                continue
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")}
            if "admission_id" not in columns:
                continue
            grouped: dict[str, list[dict[str, Any]]] = {}
            for event in self._rows(conn, f"SELECT * FROM {table_name}"):
                grouped.setdefault(str(event.get("admission_id")), []).append(event)
            related[attribute] = grouped
        return related

    def _rao_ivl_index(self, conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
        if not self._table_exists(conn, "ivl_episodes"):
            return {}
        columns = {row[1] for row in conn.execute("PRAGMA table_info(ivl_episodes)")}
        if "admission_id" not in columns:
            return {}
        grouped: dict[str, list[dict[str, Any]]] = {}
        for episode in self._rows(conn, "SELECT * FROM ivl_episodes"):
            grouped.setdefault(str(episode.get("admission_id")), []).append(episode)
        return grouped

    def _rao_operation_links(self, conn: sqlite3.Connection) -> tuple[set[str], set[str]]:
        linked_admissions: set[str] = set()
        reliable_links: set[str] = set()
        if not self._table_exists(conn, "operation_cases"):
            return linked_admissions, reliable_links
        columns = {row[1] for row in conn.execute("PRAGMA table_info(operation_cases)")}
        candidates = (
            "admission_id", "source_rao_admission_id",
            "resolved_rao_admission_id", "future_rao_admission_id",
        )
        selected = [name for name in candidates if name in columns]
        if not selected:
            return linked_admissions, reliable_links
        for operation in self._rows(conn, f"SELECT {', '.join(selected)} FROM operation_cases"):
            if operation.get("admission_id") not in (None, ""):
                linked_admissions.add(str(operation["admission_id"]))
            for name in candidates[1:]:
                if operation.get(name) not in (None, ""):
                    reliable_links.add(str(operation[name]))
        return linked_admissions, reliable_links

    def _rao_case(
        self,
        source_id: str,
        row: Mapping[str, Any],
        patient: Mapping[str, Any],
        period: AnalyticsPeriod,
        ivl_by_admission: Mapping[str, Sequence[Mapping[str, Any]]],
        related_events: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    ) -> SourceCase | None:
        admission = self._parse(row.get("admission_datetime"))
        terminal = self._terminal(row)
        if admission is None or (terminal is not None and terminal <= period.start):
            return None
        if str(row.get("unit_scope") or "").casefold() == "operblock":
            return None
        if str(row.get("admission_type") or "").casefold() == "operblock":
            return None
        if row.get("merged_into_admission_id") not in (None, "", 0):
            return None
        admission_id = str(row.get("id"))
        attrs = dict(row)
        if not attrs.get("full_name"):
            attrs["full_name"] = self._patient_full_name(patient)
        attrs.update({
            "source_name": self.source_name(source_id),
            "age": normalize_age_years(row.get("patient_age"), row.get("patient_age_unit")),
            "sex": row.get("patient_gender"),
            "diagnosis": row.get("diagnosis_code") or row.get("diagnosis_text"),
            "raw_outcome": row.get("outcome"),
            "outcome": _effective_rao_outcome(attrs, period),
        })
        attrs["ivl_episodes"] = tuple(ivl_by_admission.get(admission_id, ()))
        attrs["ivl"] = bool(attrs["ivl_episodes"]) or bool(row.get("ivl"))
        for attribute, values_by_admission in related_events.items():
            attrs[attribute] = tuple(values_by_admission.get(admission_id, ()))
        return SourceCase(
            source_id, admission_id, MetricScope.RAO, admission, attrs,
            "Госпитализация РАО пересекает период; исключены операционные и объединённые записи.",
        )

    def _rao_source_cases(
        self, source_id: str, conn: sqlite3.Connection, period: AnalyticsPeriod
    ) -> tuple[SourceCase, ...]:
        if not self._table_exists(conn, "admissions"):
            return ()
        rows = self._rows(
            conn,
            "SELECT * FROM admissions WHERE datetime(admission_datetime) < datetime(?)",
            (period.sql_bounds[1],),
        )
        patients = self._patient_index(conn)
        ivl_by_admission = self._rao_ivl_index(conn)
        related_events = self._rao_related_events(conn)
        linked_admissions, reliable_links = self._rao_operation_links(conn)
        result = []
        for row in rows:
            admission_id = str(row.get("id"))
            explicit_scope = (
                str(row.get("unit_scope") or "").casefold() == "rao"
                or str(row.get("admission_type") or "").casefold() == "rao"
            )
            if admission_id in linked_admissions and not explicit_scope and admission_id not in reliable_links:
                continue
            item = self._rao_case(
                source_id,
                row,
                patients.get(str(row.get("patient_id")), {}),
                period,
                ivl_by_admission,
                related_events,
            )
            if item is not None:
                result.append(item)
        return tuple(result)

    def _operblock_source_cases(
        self, source_id: str, conn: sqlite3.Connection, period: AnalyticsPeriod
    ) -> tuple[SourceCase, ...]:
        if not self._table_exists(conn, "operation_cases"):
            return ()
        admissions = (
            {str(item.get("id")): item for item in self._rows(conn, "SELECT * FROM admissions")}
            if self._table_exists(conn, "admissions") else {}
        )
        patients = self._patient_index(conn)
        start, end = period.sql_bounds
        rows = self._rows(
            conn,
            "SELECT * FROM operation_cases WHERE datetime(started_at) >= datetime(?) "
            "AND datetime(started_at) < datetime(?)",
            (start, end),
        )
        result = []
        for row in rows:
            status = str(row.get("status") or "").casefold()
            if status in {"cancelled", "deleted"} or bool(row.get("is_deleted")):
                continue
            admission = admissions.get(str(row.get("admission_id")), {})
            patient_id = row.get("patient_id") or admission.get("patient_id")
            attrs = dict(row)
            attrs["source_name"] = self.source_name(source_id)
            attrs.update({
                f"admission_{key}": value
                for key, value in admission.items()
                if key not in attrs
            })
            attrs.setdefault("diagnosis_code", admission.get("diagnosis_code"))
            attrs.setdefault("diagnosis_text", admission.get("diagnosis_text"))
            if not attrs.get("full_name"):
                attrs["full_name"] = self._patient_full_name(patients.get(str(patient_id), {}))
            if not attrs.get("history_number"):
                attrs["history_number"] = admission.get("history_number")
            attrs["diagnosis"] = attrs.get("diagnosis_code") or attrs.get("diagnosis_text")
            attrs["personnel"] = row.get("planned_anesthesiologist") or row.get("anesthesiologist")
            attrs["priority"] = row.get("priority") or row.get("operation_type") or row.get("table_code")
            age_value = row.get("patient_age")
            age_unit = row.get("patient_age_unit")
            if age_value is None:
                age_value = admission.get("patient_age")
                age_unit = admission.get("patient_age_unit")
            attrs["age"] = normalize_age_years(age_value, age_unit)
            result.append(SourceCase(
                source_id,
                str(row.get("id")),
                MetricScope.OPERBLOCK,
                self._parse(row.get("started_at")),
                attrs,
                "operation_case начат в периоде; отменённые и удалённые случаи исключены.",
            ))
        return tuple(result)

    def source_cases(self, scope: MetricScope, period: AnalyticsPeriod) -> tuple[SourceCase, ...]:
        result: list[SourceCase] = []
        for source_id, conn in self._connections():
            loader = self._rao_source_cases if scope == MetricScope.RAO else self._operblock_source_cases
            result.extend(loader(source_id, conn, period))
        return tuple(result)


def materialize_cohort_snapshot(source: Any, scope: MetricScope, period: AnalyticsPeriod,
                                cohort: CohortDefinition, *, db_paths: Sequence[str] = ()):
    """Создаёт writable только-в-памяти снимок для legacy builders.

    Клинический файл не меняется: SQLite Backup API переносит его в :memory:,
    затем связанные строки, не принадлежащие когорте, удаляются исключительно
    из этого временного снимка.
    """
    from rem_card.services.analytics.multi_db_analytics import AnalyticsConnectionManager
    repository = StatisticsRepository(source, db_paths=db_paths)
    cases = cohort.apply(repository.source_cases(scope, period))
    source_conn = source if isinstance(source, sqlite3.Connection) else source.get_connection()
    target = sqlite3.connect(":memory:", check_same_thread=False)
    source_conn.backup(target)
    allowed = {str(item.local_id) for item in cases}
    def table_exists(name: str) -> bool:
        return bool(target.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())
    if scope is MetricScope.RAO and table_exists("admissions"):
        ids = ",".join("?" for _ in allowed) or "''"
        target.execute(f"DELETE FROM admissions WHERE CAST(id AS TEXT) NOT IN ({ids})", tuple(allowed))
        for name in ("operations", "transfusions", "ivl_episodes", "orders", "vitals", "procedures"):
            if table_exists(name):
                columns = {row[1] for row in target.execute(f"PRAGMA table_info({name})")}
                if "admission_id" in columns:
                    target.execute(f"DELETE FROM {name} WHERE CAST(admission_id AS TEXT) NOT IN ({ids})", tuple(allowed))
    if scope is MetricScope.OPERBLOCK and table_exists("operation_cases"):
        ids = ",".join("?" for _ in allowed) or "''"
        target.execute(f"DELETE FROM operation_cases WHERE CAST(id AS TEXT) NOT IN ({ids})", tuple(allowed))
        for name in ("operblock_timeline_events",):
            if table_exists(name): target.execute(f"DELETE FROM {name} WHERE CAST(operation_case_id AS TEXT) NOT IN ({ids})", tuple(allowed))
    target.commit()
    return AnalyticsConnectionManager(target, db_path="analytics-cohort-memory"), cases


class AnalyticsEngine:
    def __init__(self, repository: StatisticsRepository, registry: MetricRegistry | None = None, cache: "SnapshotCache | None" = None):
        self.repository, self.registry, self.cache = repository, registry or default_metric_registry(), cache

    @staticmethod
    def _duration_days(case: SourceCase, period: AnalyticsPeriod) -> float:
        start = max(case.started_at or period.start, period.start)
        end = AnalyticsEngine._terminal_end(case.attributes, period.end)
        end = min(end, period.end)
        return max(0.0, (end - start).total_seconds() / 86400.0)

    @staticmethod
    def _terminal_end(attributes: Mapping[str, Any], fallback: datetime) -> datetime:
        values = (parse_analytics_datetime(attributes.get("transfer_datetime")), parse_analytics_datetime(attributes.get("death_datetime")))
        return min((value for value in values if value is not None), default=fallback)

    @staticmethod
    def _is_terminal_death(case: SourceCase, period: AnalyticsPeriod) -> bool:
        death = parse_analytics_datetime(case.attributes.get("death_datetime"))
        transfer = parse_analytics_datetime(case.attributes.get("transfer_datetime"))
        return bool(
            death
            and period.start <= death < period.end
            and (transfer is None or death <= transfer)
        )

    @staticmethod
    def _graph_population_kind(key: str) -> PopulationKind:
        if key.startswith("recovery_"): return PopulationKind.RECOVERY_SUBPOPULATION
        if key in {"g1", "g2", "g3", "g4", "g5", "g19", "g20", "g21", "g22", "g23", "g24", "g25", "g26", "g27", "g28", "g29", "g30", "g31", "g32", "g34", "g65"}: return PopulationKind.ADMISSION_EVENT
        if key in {"g37", "g38", "g39", "g40"}: return PopulationKind.TERMINAL_EVENT
        if key in {"g42", "g43", "g44", "g45", "g56", "g57", "g58", "g59", "g60"}: return PopulationKind.PROCEDURE_EVENT
        return PopulationKind.INTERVAL_CENSUS

    @staticmethod
    def _general_cases(base_cases: tuple[SourceCase, ...], cohort: CohortDefinition) -> tuple[SourceCase, ...]:
        return base_cases if cohort.include_recovery_beds else tuple(
            item for item in base_cases if not is_recovery_case(item.attributes)
        )

    @staticmethod
    def _event_stamp(event: Mapping[str, Any]) -> datetime | None:
        return parse_analytics_datetime(
            event.get("operation_datetime") or event.get("datetime") or event.get("performed_at")
            or event.get("started_at")
        )

    @classmethod
    def _related_events(cls, cases: Iterable[SourceCase], attribute: str, period: AnalyticsPeriod) -> tuple[tuple[SourceCase, Mapping[str, Any]], ...]:
        return tuple(
            (case, event)
            for case in cases
            for event in (case.attributes.get(attribute) or ())
            if isinstance(event, Mapping)
            and (stamp := cls._event_stamp(event)) is not None
            and period.start <= stamp < period.end
        )

    @staticmethod
    def _ivl_cases(cases: Iterable[SourceCase], period: AnalyticsPeriod) -> tuple[SourceCase, ...]:
        selected: list[SourceCase] = []
        for case in cases:
            for episode in case.attributes.get("ivl_episodes") or ():
                if not isinstance(episode, Mapping):
                    continue
                start = parse_analytics_datetime(episode.get("start_time") or episode.get("started_at"))
                end = parse_analytics_datetime(episode.get("end_time") or episode.get("ended_at")) or period.end
                if start is not None and start < period.end and end > period.start:
                    selected.append(case)
                    break
            else:
                if case.attributes.get("ivl") or case.attributes.get("ivl_episode"):
                    selected.append(case)
        return tuple(selected)

    @staticmethod
    def _unique_cases(cases: Iterable[SourceCase]) -> tuple[SourceCase, ...]:
        seen: set[str] = set()
        return tuple(item for item in cases if not (item.id in seen or seen.add(item.id)))

    def _metric_cases(self, definition: MetricDefinition, base_cases: tuple[SourceCase, ...], period: AnalyticsPeriod, cohort: CohortDefinition) -> tuple[SourceCase, ...]:
        """Применяет сохранённый population_kind для KPI, legacy и graphs.

        Нельзя оставлять KPI на общей когорте, когда definition уже задаёт
        событие поступления, терминальное событие либо интервал пребывания.
        """
        general = self._general_cases(base_cases, cohort)
        try:
            kind = PopulationKind(definition.population_kind)
        except ValueError as error:
            raise ValueError(f"Неизвестный population_kind для {definition.id}: {definition.population_kind}") from error
        if kind is PopulationKind.RECOVERY_SUBPOPULATION:
            return tuple(
                item for item in base_cases
                if is_recovery_case(item.attributes)
                and item.started_at
                and period.start <= item.started_at < period.end
            )
        if kind is PopulationKind.ADMISSION_EVENT:
            return tuple(item for item in general if item.started_at and period.start <= item.started_at < period.end)
        if kind is PopulationKind.TERMINAL_EVENT:
            return tuple(item for item in general if self._is_terminal_death(item, period))
        if kind is PopulationKind.PROCEDURE_EVENT:
            if definition.scope is MetricScope.OPERBLOCK:
                return tuple(item for item in general if item.started_at and period.start <= item.started_at < period.end)
            key = definition.graph_key or definition.id
            if key in {"rao.operations", "g56", "g57", "g60"}:
                return self._unique_cases(case for case, _ in self._related_events(general, "operations", period))
            if key in {"rao.transfusions", "g58", "g59"}:
                return self._unique_cases(case for case, _ in self._related_events(general, "transfusions", period))
            if key in {"rao.ivl_patients", "g42", "g43", "g44", "g45"}:
                return self._ivl_cases(general, period)
        return general

    def _legacy_result(self, definition: MetricDefinition, cases: tuple[SourceCase, ...], period: AnalyticsPeriod,
                       cohort: CohortDefinition | None = None, denominator_cases: tuple[SourceCase, ...] | None = None) -> MetricResult | None:
        """Структурирует старые расчётные строки без хрупкого разбора HTML."""
        if definition.kind == MetricKind.GRAPH:
            artifact = self._graph_artifact(definition, cases, period, denominator_cases=denominator_cases)
            payload = artifact.serialize()
            selected = tuple(item for item in cases if item.id in set(artifact.source_case_ids))
            return MetricResult(definition, payload, artifact.numerator, artifact.denominator, selected,
                explanation=definition.formula, rows=artifact.series, artifact=payload)
        source = getattr(self.repository, "source", None)
        if source is None and not getattr(self.repository, "db_paths", ()):
            return None
        cohort_manager = None
        multi_manager = None
        try:
            builder_source = source
            if self.repository.db_paths:
                # Legacy builders require one relational manager.  Build an
                # identity-safe in-memory aggregate instead of choosing an
                # arbitrary source file; it is always closed in finally.
                from rem_card.services.analytics.multi_db_analytics import create_multi_db_analytics_manager
                multi_manager = create_multi_db_analytics_manager(
                    self.repository.db_paths,
                    start_dt=period.start.date().isoformat(),
                    end_dt=(period.end - timedelta(days=1)).date().isoformat(),
                )
                builder_source = multi_manager
            # Один и тот же CohortDefinition используется для source cases и
            # public payload builders; временный manager не касается clinical DB.
            if cohort is not None:
                materialized_cohort = cohort
                if definition.scope is MetricScope.RAO and definition.id.startswith("s"):
                    # Специальный recovery-раздел всегда должен видеть свою
                    # популяцию. Builder сам исключит её только из общих строк,
                    # если пользовательский переключатель выключен.
                    materialized_cohort = replace(cohort, include_recovery_beds=True)
                cohort_manager, _ = materialize_cohort_snapshot(
                    builder_source, definition.scope, period, materialized_cohort,
                )
                builder_source = cohort_manager
            if definition.id.startswith("s"):
                from rem_card.services.analytics.detailed_statistics_service import DetailedStatisticsReportBuilder
                builder = DetailedStatisticsReportBuilder(builder_source, period.start.date().isoformat(), (period.end - timedelta(days=1)).date().isoformat(), include_recovery_beds=bool(cohort and cohort.include_recovery_beds))
                rows = tuple(builder.structured_section_rows(definition.id))
            elif definition.id.startswith("ob"):
                from rem_card.services.analytics.operblock_statistics_service import OperBlockStatisticsReportBuilder
                builder = OperBlockStatisticsReportBuilder(builder_source, period.start.date().isoformat(), (period.end - timedelta(days=1)).date().isoformat())
                row = builder.structured_indicator_rows((definition.id,)).get(definition.id)
                rows = (row,) if row else ()
            else:
                return None
        except Exception as error:
            return MetricResult(definition, None, source_cases=cases,
                explanation=f"Структурированная строка недоступна: {error}")
        finally:
            if cohort_manager is not None:
                cohort_manager.close_connection()
            if multi_manager is not None:
                multi_manager.close_connection()
        if not rows:
            return MetricResult(definition, None, source_cases=cases, explanation="Для показателя нет строк в текущем снимке.")
        formulas = "; ".join(str(item.get("formula") or "") for item in rows)
        exact_definition = replace(definition, formula=formulas or definition.formula,
            description=str(rows[0].get("name") or definition.description))
        numerator_cases = self._specialized_cases(exact_definition.id, cases, period)
        return MetricResult(exact_definition, rows, source_cases=numerator_cases,
            denominator=len(cases), explanation=exact_definition.formula, rows=rows)

    @staticmethod
    def _graph_artifact(
        definition: MetricDefinition,
        cases: tuple[SourceCase, ...],
        period: AnalyticsPeriod,
        *,
        denominator_cases: tuple[SourceCase, ...] | None = None,
    ) -> GraphMetricArtifact:
        from rem_card.services.analytics.platform.graph_builder import build_graph_artifact

        return build_graph_artifact(
            AnalyticsEngine,
            definition,
            cases,
            period,
            denominator_cases,
        )

    @classmethod
    def _specialized_cases(cls, metric_id: str, cases: tuple[SourceCase, ...], period: AnalyticsPeriod) -> tuple[SourceCase, ...]:
        if metric_id in {"s6", "s7", "s8"}:
            return tuple(item for item in cases if cls._is_terminal_death(item, period))
        if metric_id == "s_recovery": return tuple(item for item in cases if is_recovery_case(item.attributes))
        if metric_id == "s9": return cls._ivl_cases(cases, period)
        if metric_id == "s10": return cls._unique_cases(case for case, _ in cls._related_events(cases, "operations", period))
        if metric_id == "s11": return cls._unique_cases(case for case, _ in cls._related_events(cases, "transfusions", period))
        if metric_id.startswith("ob4"): return tuple(item for item in cases if str(item.attributes.get("operation_type") or item.attributes.get("priority") or "").casefold() in {"emergency", "urgent", "экстренная"})
        if metric_id.startswith("ob5"): return tuple(item for item in cases if str(item.attributes.get("operation_type") or item.attributes.get("priority") or "").casefold() not in {"emergency", "urgent", "экстренная"})
        if metric_id.startswith("ob11"): return tuple(item for item in cases if item.started_at and (item.started_at.hour >= 22 or item.started_at.hour < 6))
        return cases

    def _rao_result_values(
        self,
        definition: MetricDefinition,
        cases: tuple[SourceCase, ...],
        period: AnalyticsPeriod,
        denominator_cases: tuple[SourceCase, ...] | None,
    ) -> tuple[Any, tuple[SourceCase, ...], int | float | None, int | float | None]:
        deaths = tuple(item for item in cases if self._is_terminal_death(item, period))
        bed_days = sum(self._duration_days(item, period) for item in cases)
        recovery = tuple(item for item in cases if is_recovery_case(item.attributes))
        if definition.id == "rao.admissions":
            return len(cases), cases, len(cases), None
        if definition.id == "rao.bed_days":
            return bed_days, cases, bed_days, None
        if definition.id == "rao.average_los":
            return (bed_days / len(cases) if cases else None), cases, bed_days, len(cases)
        if definition.id == "rao.deaths":
            return len(deaths), deaths, len(deaths), None
        if definition.id == "rao.mortality":
            denominator = denominator_cases if denominator_cases is not None else cases
            return ((len(deaths) * 100 / len(denominator)) if denominator else None), deaths, len(deaths), len(denominator)
        if definition.id == "rao.recovery_cases":
            return len(recovery), recovery, len(recovery), None
        if definition.id == "rao.ivl_patients":
            selected = tuple(
                item for item in cases
                if bool(item.attributes.get("ivl")) or bool(item.attributes.get("ivl_episode"))
            )
            return len(selected), selected, len(selected), None
        if definition.id == "rao.transfers":
            return dict(Counter(_effective_rao_outcome(item.attributes, period) for item in cases)), cases, None, None
        if definition.id in {"rao.operations", "rao.transfusions"}:
            attribute = "operations" if definition.id.endswith("operations") else "transfusions"
            events = self._related_events(cases, attribute, period)
            selected = self._unique_cases(case for case, _ in events)
            return len(events), selected, len(events), None
        raise KeyError(f"Для метрики {definition.id} не задан расчёт.")

    @staticmethod
    def _operblock_result_values(
        definition: MetricDefinition,
        cases: tuple[SourceCase, ...],
    ) -> tuple[Any, tuple[SourceCase, ...], int | float | None, int | float | None]:
        def status(item: SourceCase) -> str:
            return str(item.attributes.get("status") or "").casefold()

        closed = tuple(item for item in cases if status(item) in {"closed", "completed", "finished"})
        active = tuple(item for item in cases if item not in closed)
        emergency = tuple(
            item for item in cases
            if str(item.attributes.get("operation_type") or item.attributes.get("priority") or "").casefold()
            in {"emergency", "urgent", "экстренная"}
        )
        planned = tuple(item for item in cases if item not in emergency)
        night = tuple(item for item in cases if item.started_at and (item.started_at.hour >= 22 or item.started_at.hour < 6))
        populations = {
            "operblock.total": cases,
            "operblock.closed": closed,
            "operblock.active": active,
            "operblock.emergency": emergency,
            "operblock.planned": planned,
            "operblock.night": night,
        }
        if definition.id in populations:
            selected = populations[definition.id]
            return len(selected), selected, len(selected), None
        if definition.id == "operblock.average_room_duration":
            durations: list[float] = []
            selected: list[SourceCase] = []
            for item in cases:
                ended_at = parse_analytics_datetime(item.attributes.get("ended_at"))
                if item.started_at and ended_at and ended_at >= item.started_at:
                    durations.append((ended_at - item.started_at).total_seconds() / 60)
                    selected.append(item)
            total = sum(durations)
            return (total / len(durations) if durations else None), tuple(selected), total, len(durations)
        raise KeyError(f"Для метрики {definition.id} не задан расчёт.")

    def _result(self, definition: MetricDefinition, cases: tuple[SourceCase, ...], period: AnalyticsPeriod,
                cohort: CohortDefinition | None = None, *, denominator_cases: tuple[SourceCase, ...] | None = None) -> MetricResult:
        legacy = self._legacy_result(definition, cases, period, cohort, denominator_cases)
        if legacy is not None:
            return legacy
        if definition.scope == MetricScope.RAO:
            value, selected, numerator, denominator = self._rao_result_values(
                definition, cases, period, denominator_cases,
            )
        else:
            value, selected, numerator, denominator = self._operblock_result_values(definition, cases)
        return MetricResult(definition, value, numerator, denominator, selected, definition.formula)

    def snapshot(self, scope: MetricScope, period: AnalyticsPeriod | AnalyticsDatePeriod | tuple[Any, Any], cohort: CohortDefinition | None = None,
                 metric_ids: Iterable[str] | None = None) -> StatisticsSnapshot:
        if isinstance(period, AnalyticsDatePeriod): period = AnalyticsPeriod(period.start_inclusive, period.end_exclusive)
        if not isinstance(period, AnalyticsPeriod): period = AnalyticsPeriod.from_values(*period)
        cohort = cohort or CohortDefinition(scope=scope)
        metric_ids = tuple(metric_ids or (item.id for item in self.registry.for_scope(scope)))
        key = (self.registry.get(metric_ids[0]).version if metric_ids else REGISTRY_VERSION, scope.value, period.key,
               json.dumps(cohort.serialize(), ensure_ascii=False, sort_keys=True), self.repository.fingerprints(), metric_ids)
        if self.cache:
            found = self.cache.get(key)
            if found is not None: return found
        # Filters are applied to immutable base rows first.  Recovery is kept
        # in base so recovery graphs stay valid even when general metrics hide it.
        base_cohort = CohortDefinition(cohort.name, cohort.scope, cohort.filters, True)
        base_cases = base_cohort.apply(self.repository.source_cases(scope, period))
        cases = self._general_cases(base_cases, cohort)
        issues = self.quality(cases)
        results = {
            metric_id: self._result(
                self.registry.get(metric_id),
                self._metric_cases(self.registry.get(metric_id), base_cases, period, cohort),
                period,
                cohort,
                denominator_cases=cases,
            )
            for metric_id in metric_ids
        }
        snapshot = StatisticsSnapshot(REGISTRY_VERSION, scope, period, cohort, cases, results, issues, self.repository.fingerprints())
        if self.cache: self.cache.put(key, snapshot)
        return snapshot

    @staticmethod
    def quality(cases: Iterable[SourceCase]) -> DataQualityReport:
        issues: list[DataQualityIssue] = []
        seen: set[str] = set()
        for item in cases:
            if item.id in seen: issues.append(DataQualityIssue("duplicate_source_identity", "error", "Повторяется идентификатор исходного случая.", item.id))
            seen.add(item.id)
            if item.started_at is None: issues.append(DataQualityIssue("missing_started_at", "warning", "Не указана дата начала случая.", item.id))
            end = parse_analytics_datetime(item.attributes.get("ended_at")) or parse_analytics_datetime(item.attributes.get("transfer_datetime"))
            if item.started_at and end and end < item.started_at: issues.append(DataQualityIssue("end_before_start", "error", "Дата окончания раньше даты начала.", item.id))
            if item.scope is MetricScope.RAO and (str(item.attributes.get("unit_scope") or "").casefold() == "operblock" or str(item.attributes.get("admission_type") or "").casefold() == "operblock"):
                issues.append(DataQualityIssue("cross_scope_admission", "error", "Операционный admission попал в популяцию РАО.", item.id))
            if item.scope == MetricScope.OPERBLOCK and not item.attributes.get("planned_operation_name"):
                issues.append(DataQualityIssue("missing_operation_name", "warning", "Не указано название операции.", item.id))
            if item.scope == MetricScope.OPERBLOCK and not (item.attributes.get("planned_anesthesiologist") or item.attributes.get("anesthesiologist")):
                issues.append(DataQualityIssue("missing_personnel", "warning", "Не указан анестезиолог операции.", item.id))
        return DataQualityReport(tuple(issues))

    def compare(self, scope: MetricScope, metric_id: str, current_period: Any, cohort: CohortDefinition | None = None,
                comparison_period: Any = None) -> PeriodComparison:
        current = self.snapshot(scope, current_period, cohort, (metric_id,)).results[metric_id]
        current_period = current_period if isinstance(current_period, AnalyticsPeriod) else AnalyticsPeriod.from_values(*current_period) if isinstance(current_period, tuple) else current_period
        if isinstance(current_period, AnalyticsDatePeriod): current_period = AnalyticsPeriod(current_period.start_inclusive, current_period.end_exclusive)
        manual = comparison_period is not None
        previous_period = comparison_period or AnalyticsPeriod.previous_calendar_year(current_period)
        if not isinstance(previous_period, AnalyticsPeriod): previous_period = AnalyticsPeriod.from_values(*previous_period)
        previous_snapshot = self.snapshot(scope, previous_period, cohort, (metric_id,))
        previous = previous_snapshot.results[metric_id]
        if not metric_result_has_data(previous):
            return PeriodComparison(current, None, None, None, previous_period, "Нет данных за ручной период" if manual else "Нет данных за предыдущий год")
        if not isinstance(current.value, (int, float)) or not isinstance(previous.value, (int, float)):
            return PeriodComparison(current, previous, None, None, previous_period)
        delta = float(current.value) - float(previous.value)
        relative = delta * 100 / float(previous.value) if previous.value else None
        return PeriodComparison(current, previous, delta, relative, previous_period)


def metric_result_has_data(result: MetricResult) -> bool:
    """Проверяет наличие популяции именно выбранной метрики, а не общей когорты."""
    if result.definition.kind in {MetricKind.AVERAGE, MetricKind.RATIO}:
        return result.denominator is not None and float(result.denominator) > 0
    if result.definition.kind is MetricKind.GRAPH:
        artifact = result.artifact or {}
        denominator = artifact.get("denominator")
        if denominator is not None:
            return float(denominator) > 0
    return bool(result.source_cases)


class SnapshotCache:
    def __init__(self, maxsize: int = 32):
        self.maxsize, self._items, self._lock = max(1, maxsize), OrderedDict(), threading.RLock()
        self.hits = self.misses = 0
    def get(self, key: Any):
        with self._lock:
            if key not in self._items:
                self.misses += 1; return None
            self.hits += 1; self._items.move_to_end(key); return self._items[key]
    def put(self, key: Any, value: StatisticsSnapshot):
        with self._lock:
            self._items[key] = value; self._items.move_to_end(key)
            while len(self._items) > self.maxsize: self._items.popitem(last=False)
    def invalidate(self):
        with self._lock: self._items.clear()


@dataclass(frozen=True)
class SavedAnalyticsView:
    name: str
    scope: MetricScope
    cohort: CohortDefinition
    metric_ids: tuple[str, ...] = ()
    comparison_period: tuple[str, str] | None = None
    selected_metric_id: str | None = None
    comparison_mode: str = "previous_year"
    schema_version: int = SAVED_VIEW_SCHEMA_VERSION
    period_a: tuple[str, str] | None = None


class SavedAnalyticsViewStore:
    """Настройки пользователя вне клинической БД."""
    def __init__(self, path: str | os.PathLike[str] | None = None):
        self.path = Path(path or Path.home() / ".remcard_analytics_views.json")
    def load(self) -> tuple[SavedAnalyticsView, ...]:
        try: data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError): return ()
        return tuple(SavedAnalyticsView(str(row["name"]), MetricScope(row["scope"]), CohortDefinition.deserialize(row["cohort"]), tuple(row.get("metric_ids", ())), tuple(row["comparison_period"]) if row.get("comparison_period") else None, row.get("selected_metric_id"), row.get("comparison_mode") or ("manual" if row.get("comparison_period") else "previous_year"), int(row.get("schema_version") or 1), tuple(row["period_a"]) if row.get("period_a") else None) for row in data)
    def save(self, views: Iterable[SavedAnalyticsView]):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = [{"schema_version": SAVED_VIEW_SCHEMA_VERSION, "name": item.name, "scope": item.scope.value, "cohort": item.cohort.serialize(), "metric_ids": list(item.metric_ids), "comparison_period": item.comparison_period, "selected_metric_id": item.selected_metric_id, "comparison_mode": item.comparison_mode, "period_a": item.period_a} for item in views]
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
