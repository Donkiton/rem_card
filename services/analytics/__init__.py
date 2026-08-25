"""Analytics services for archive/report views."""

# Реэкспорт не обязателен для старого кода, но делает новый контракт доступным
# через ``services.analytics`` и ``services.analytics.platform``.
from .platform import (  # noqa: F401
    AnalyticsEngine, AnalyticsPeriod, CohortDefinition, CohortFilter, MetricDefinition,
    MetricKind, MetricRegistry, MetricScope, SnapshotCache, StatisticsRepository,
    default_metric_registry, materialize_cohort_snapshot,
)
