from datetime import datetime
from rem_card.services.analytics.platform import AnalyticsEngine, AnalyticsPeriod, MetricScope, SnapshotCache, SourceCase


class Repository:
    calls = 0
    def fingerprints(self): return ("fixture",)
    def source_cases(self, scope, _period):
        self.calls += 1; return (SourceCase("db", "1", scope, datetime.now(), {}),)


def test_cache_reuses_snapshot_and_quality_reports_missing_operblock_name():
    repo, cache = Repository(), SnapshotCache(2); engine = AnalyticsEngine(repo, cache=cache); period = AnalyticsPeriod.from_values("2026-01-01", "2026-01-01")
    first = engine.snapshot(MetricScope.OPERBLOCK, period, metric_ids=("operblock.total",)); second = engine.snapshot(MetricScope.OPERBLOCK, period, metric_ids=("operblock.total",))
    assert first is second and repo.calls == 1 and cache.hits == 1
    assert first.quality.issues[0].code == "missing_operation_name"


def test_quality_detects_duplicate_and_invalid_time_and_cache_lru():
    cases = (
        SourceCase("db", "1", MetricScope.OPERBLOCK, datetime(2026, 2, 2), {"ended_at": "2026-02-01", "unit_scope": "operblock"}),
        SourceCase("db", "1", MetricScope.OPERBLOCK, datetime(2026, 2, 2), {}),
    )
    report = AnalyticsEngine.quality(cases)
    assert {issue.code for issue in report.issues} >= {"duplicate_source_identity", "end_before_start", "missing_operation_name", "missing_personnel"}
    cache = SnapshotCache(1); cache.put("a", 1); cache.put("b", 2)
    assert cache.get("a") is None and cache.get("b") == 2
