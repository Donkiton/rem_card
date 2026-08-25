from datetime import datetime
from rem_card.services.analytics.platform import AnalyticsEngine, MetricScope, SourceCase


class EmptyPrevious:
    def fingerprints(self): return ("db",)
    def source_cases(self, scope, period):
        return (SourceCase("db", "1", scope, datetime(2026, 3, 1), {}),) if period.start.year == 2026 else ()


def test_previous_year_absence_is_not_zero():
    comparison = AnalyticsEngine(EmptyPrevious()).compare(MetricScope.RAO, "rao.admissions", ("2026-03-01", "2026-03-10"))
    assert comparison.previous is None and comparison.absolute_delta is None
    assert comparison.message == "Нет данных за предыдущий год"


def test_manual_empty_period_has_its_own_label():
    comparison = AnalyticsEngine(EmptyPrevious()).compare(
        MetricScope.RAO, "rao.admissions", ("2026-03-01", "2026-03-10"), comparison_period=("2025-01-01", "2025-01-02")
    )
    assert comparison.message == "Нет данных за ручной период"


class CarryInOnlyPrevious:
    def fingerprints(self): return ("carry-in-comparison",)
    def source_cases(self, scope, period):
        if period.start.year == 2026:
            return (SourceCase("db", "current", scope, datetime(2026, 3, 2), {}),)
        return (SourceCase(
            "db", "carry", scope, datetime(2024, 12, 31),
            {"transfer_datetime": "2025-03-02"},
        ),)


def test_admission_metric_does_not_treat_previous_year_carry_in_as_data():
    comparison = AnalyticsEngine(CarryInOnlyPrevious()).compare(
        MetricScope.RAO,
        "g1",
        ("2026-03-01", "2026-03-10"),
    )
    assert comparison.previous is None
    assert comparison.message == "Нет данных за предыдущий год"
