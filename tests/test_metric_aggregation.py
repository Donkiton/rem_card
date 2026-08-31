from __future__ import annotations

import json
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rem_card.app import metric_aggregation as policy


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch):
    monkeypatch.delenv("REMCARD_METRICS_DETAIL_UNTIL", raising=False)
    monkeypatch.setenv("REMCARD_METRICS_AGGREGATION_ENABLED", "1")


def event(name="read_duration_ms", value=10, **fields):
    return {"ts": "2026-08-31T10:00:00+10:00", "metric": name, "value": value,
            "host": "synthetic-test", "pid": 123, **fields}


def test_exact_counts_latencies_and_errors_without_double_counting():
    clock = Clock()
    agg = policy.MetricAggregator(clock=clock)
    raw = []
    samples = [1, 10, 11, 50, 51, 100, 101, 499, 500, 1000, 1500]
    for value in samples:
        p = event(value=value, status="ok", source="central")
        if not agg.observe(p):
            raw.append(p)
    assert [p["value"] for p in raw] == [500, 1000, 1500]
    assert agg.drain() == []
    clock.now = 60
    summary, = agg.drain()
    assert summary["count"] == 11
    assert summary["aggregated_count"] == 8
    assert summary["raw_count"] == 3
    assert summary["duration_avg_ms"] == round(sum(samples) / len(samples), 3)
    assert summary["duration_max_ms"] == 1500
    assert summary["latency_counts"] == [2, 2, 2, 3, 1, 1]
    assert summary["error_count"] == 0
    assert agg.drain(force=True) == []


@pytest.mark.parametrize("fields", [
    {"status": "error"}, {"status": "cancelled"}, {"status": "unknown"},
    {"status": "ok", "fallback_used": True}, {"status": "ok", "error_class": "OSError"},
    {"status": "ok", "conflict": True}, {"status": "ok", "fallback_after_delta": 1},
])
def test_anomalies_are_raw_and_counted(fields):
    agg = policy.MetricAggregator()
    p = event(**fields, trace_id="preserve-this-context")
    before = dict(p)
    assert not agg.observe(p)
    assert p == before
    summary, = agg.drain(force=True)
    assert summary["count"] == summary["raw_count"] == summary["error_count"] == 1


@pytest.mark.parametrize("name", [
    "sqlite_write_lock_timeout", "write_duration_ms", "write_result_unknown",
    "orders_optimistic_conflict", "local_replica_sync_recovered", "runtime_outage_started",
    "runtime_outage_recovered", "event_loop_pause_ms", "ui_hard_hang_stack_dump",
    "backup_result", "new_unclassified_metric", "maintenance_task_started",
    "maintenance_task_finished", "local_replica_sync_deferred",
])
def test_unlisted_and_critical_events_are_untouched(name):
    agg = policy.MetricAggregator()
    assert not agg.observe(event(name, 1))
    assert agg.drain(force=True) == []


def test_only_successful_replica_locks_are_aggregated():
    agg = policy.MetricAggregator()
    for name in ("sqlite_write_lock_acquired", "sqlite_write_lock_released"):
        assert agg.observe(event(name, 1, source="local_replica_snapshot"))
        assert not agg.observe(event(name, 0, source="local_replica_snapshot"))
        assert not agg.observe(event(name, 1, source="nurse_order_mark:test"))
        assert not agg.observe(event(name, 1, source="db_rotation"))
        assert not agg.observe(event(name, 1, source="local_replica_snapshot", committed=False))
    assert not agg.observe(event("sqlite_write_lock_acquired", 1, source="local_replica_snapshot",
                                 total_wait_ms=100))
    assert not agg.observe(event("sqlite_write_lock_acquired", 1, source="local_replica_snapshot", attempts=2))
    assert not agg.observe(event("sqlite_write_lock_released", 1, source="local_replica_snapshot", held_ms=100))
    assert sum(s["aggregated_count"] for s in agg.drain(force=True)) == 2


def test_replica_state_changes_and_recovery_are_kept():
    agg = policy.MetricAggregator()
    p = event("local_replica_sync_duration_ms", 10, result="unchanged", change_cursor=100, db_cycle="cycle-a")
    assert not agg.observe(p)
    assert agg.observe(p)
    assert not agg.observe({**p, "change_cursor": 101})
    assert not agg.observe({**p, "db_cycle": "cycle-b"})
    assert not agg.observe({**p, "result": "snapshot_ready"})
    assert not agg.observe(p)
    assert agg.observe(p)
    assert not agg.observe(event("local_replica_sync_failed", 1, duration_ms=5, error_class="OSError"))
    summaries = agg.drain(force=True)
    assert sum(s["count"] for s in summaries) == 8
    assert sum(s["error_count"] for s in summaries) == 1


def test_orders_fallback_delta_failure_and_slow_delta_are_kept():
    agg = policy.MetricAggregator()
    p = event("orders_load_time_ms", 10, source="cache", cache_hit=1)
    assert agg.observe(p)
    for fields in ({"delta_failed": 1}, {"fallback_used": 1}, {"source": "fallback"},
                   {"delta_time_ms": 500}, {"delta_time_ms": None}):
        assert not agg.observe({**p, **fields})


def test_only_repeated_current_standby_skips_are_aggregated():
    agg = policy.MetricAggregator()
    p = event("emergency_standby_refresh_skipped", 1, status="current",
              detail="standby is already current", reason="timer")
    assert not agg.observe(p)
    assert agg.observe(p)
    assert not agg.observe({**p, "status": "deferred", "detail": "network_maintenance_busy"})
    assert not agg.observe({**p, "detail": "unclassified reason"})


def test_memory_bound_and_no_patient_or_operation_identifiers_in_summaries():
    agg = policy.MetricAggregator(max_groups=8)
    suppressed = 0
    for i in range(2000):
        suppressed += agg.observe(event("settings_cache_hit", 1, catalog_key=f"catalog-{i}",
                                        admission_id=i, trace_id=str(i), query="sensitive SQL"))
    assert suppressed == 8
    summaries = agg.drain(force=True)
    assert len(summaries) == 8
    assert all(not {"admission_id", "trace_id", "query"} & s.keys() for s in summaries)
    assert not agg.observe(event("settings_cache_hit", 1, catalog_key="x" * 129))
    assert len(agg._buckets) == 0


@pytest.mark.parametrize("value", [None, "fast", -1, float("nan"), float("inf"), True])
def test_invalid_duration_is_not_suppressed(value):
    agg = policy.MetricAggregator()
    assert not agg.observe(event(value=value, status="ok"))
    assert agg.drain(force=True)[0]["duration_count"] == 0


def test_concurrent_producers_and_flush_preserve_counts():
    agg = policy.MetricAggregator()
    summaries = []

    def producer(index):
        for _ in range(1000):
            assert agg.observe(event(status="ok", operation=str(index)))
            if index == 0:
                summaries.extend(agg.drain(force=True))

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(producer, range(4)))
    summaries.extend(agg.drain(force=True))
    assert sum(s["count"] for s in summaries) == 4000
    assert sum(s["duration_count"] for s in summaries) == 4000


def test_detailed_mode_deadline_is_bounded_and_not_extended_by_restart(monkeypatch):
    clock = Clock()
    wall = 1800000000
    until = datetime.fromtimestamp(wall + 120, timezone.utc).isoformat()
    monkeypatch.setenv("REMCARD_METRICS_DETAIL_UNTIL", until)
    agg = policy.MetricAggregator(clock=clock, wall_clock=lambda: wall)
    assert agg.detailed()
    clock.now = 119
    assert agg.detailed()
    clock.now = 120
    assert not agg.detailed()
    assert not policy.MetricAggregator(wall_clock=lambda: wall + 121).detailed()
    monkeypatch.setenv("REMCARD_METRICS_DETAIL_UNTIL", datetime.fromtimestamp(wall + 86400, timezone.utc).isoformat())
    assert agg.detailed()
    clock.now += 1800
    assert not agg.detailed()
    monkeypatch.setenv("REMCARD_METRICS_DETAIL_UNTIL", "invalid")
    assert not agg.detailed()


def test_rollback_and_forced_records_keep_raw_and_pending_counts(monkeypatch):
    agg = policy.MetricAggregator()
    p = event(status="ok")
    assert agg.observe(p)
    assert not agg.observe(p, force_raw=True)
    monkeypatch.setenv("REMCARD_METRICS_AGGREGATION_ENABLED", "0")
    assert not agg.observe(p)
    summary, = agg.drain(force=True)
    assert summary["count"] == 2
    assert summary["raw_count"] == 1


@pytest.fixture
def metrics(monkeypatch, tmp_path):
    from rem_card.app import local_metrics
    local_metrics.shutdown_metrics()
    monkeypatch.setenv("REMCARD_LOCAL_LOGS_DIR", str(tmp_path))
    monkeypatch.setenv("REMCARD_LOCAL_METRICS_ENABLED", "1")
    monkeypatch.setenv("REMCARD_LOCAL_METRICS_SYNC", "1")
    monkeypatch.setenv("REMCARD_LOCAL_METRICS_FLUSH_SEC", "0.1")
    monkeypatch.setattr(local_metrics, "_AGGREGATOR", policy.MetricAggregator())
    monkeypatch.setattr(local_metrics, "_METRICS_QUEUE", queue.Queue(maxsize=100))
    monkeypatch.setattr(local_metrics, "_METRICS_THREAD", None)
    monkeypatch.setattr(local_metrics, "_METRICS_STOP", threading.Event())
    monkeypatch.setattr(local_metrics, "_DROPPED_METRICS", 0)
    monkeypatch.setattr(local_metrics, "_CACHED_PATH", None)
    monkeypatch.setattr(local_metrics, "_CACHED_PATH_DAY", None)
    monkeypatch.setattr(local_metrics, "_LATEST_CHANGE_METRIC_STATE", {})
    yield local_metrics
    local_metrics.shutdown_metrics()


def test_background_summary_flushes_after_silence(metrics, monkeypatch):
    monkeypatch.setattr(policy, "WINDOW_SEC", 0.01)
    written = []
    ready = threading.Event()

    def capture(batch):
        written.extend(batch)
        if any(p["metric"] == "local_metrics_summary" for p in batch):
            ready.set()

    monkeypatch.setattr(metrics, "_write_payloads", capture)
    metrics.record_metric("read_duration_ms", 10, status="ok")
    assert ready.wait(3)
    assert written[0]["count"] == 1


def test_noisy_routine_events_do_not_fill_queue_or_drop_errors(metrics, monkeypatch):
    monkeypatch.setenv("REMCARD_LOCAL_METRICS_SYNC", "0")
    monkeypatch.setattr(metrics, "_ensure_worker_started", lambda: metrics._METRICS_QUEUE)
    for _ in range(10000):
        metrics.record_metric("read_duration_ms", 1, status="ok")
    metrics.record_metric("read_duration_ms", 2, status="error", error_class="OSError")
    metrics.record_metric("sqlite_write_lock_timeout", 1, operation_name="synthetic-save")
    assert metrics._DROPPED_METRICS == 0
    assert metrics._METRICS_QUEUE.qsize() == 2
    batch = metrics._drain_queue(force_summaries=True)
    assert [p["metric"] for p in batch[:2]] == ["read_duration_ms", "sqlite_write_lock_timeout"]
    assert sum(p["aggregated_count"] for p in batch if p["metric"] == "local_metrics_summary") == 10000


def test_shutdown_writes_partial_window_and_legacy_analyzer_reads_counts(metrics, tmp_path):
    from rem_card.scripts import analyze_ui_stall_logs as analyzer
    for value in (1, 10, 1500):
        metrics.record_metric("read_duration_ms", value, status="ok", source="central")
    metrics.record_metric("event_loop_pause_ms", 1700)
    metrics.shutdown_metrics()
    paths = list(tmp_path.glob("metrics_*.jsonl"))
    assert paths
    payloads = [json.loads(line) for path in paths for line in path.read_text(encoding="utf-8").splitlines()]
    assert sum(p.get("aggregated_count", 0) for p in payloads) == 2
    events = analyzer._load_events(tmp_path, None, None)
    summary = analyzer.build_summary(events, window_sec=90)
    assert summary["top_metric_counts"]["read_duration_ms"] == 3
    assert summary["ui_pause_count"] == 1
    assert summary["classifications"]["user_visible_read_stall"] == 1
    assert summary["metric_summaries"][0]["duration_max_ms"] == 1500


def test_temporary_detail_returns_to_summary_mode(metrics, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(metrics, "_AGGREGATOR", policy.MetricAggregator(clock=clock))
    capture = Mock()
    monkeypatch.setattr(metrics, "_write_payloads", capture)
    metrics.enable_detailed_metrics(60)
    metrics.record_metric("read_duration_ms", 1, status="ok")
    assert capture.call_args.args[0][0]["metric"] == "read_duration_ms"
    capture.reset_mock()
    clock.now = 61
    metrics.record_metric("read_duration_ms", 1, status="ok")
    assert not capture.called
    metrics.record_metric("read_duration_ms", 1, status="ok", force_flush=True)
    assert capture.call_args.args[0][0]["metric"] == "read_duration_ms"


@pytest.mark.parametrize("method", ["fetch_all_remcard", "fetch_one_remcard"])
def test_successful_central_fallback_is_explicit_and_kept(method, monkeypatch):
    from rem_card.data.dao import db_manager
    p = Mock()
    p._in_current_thread_remcard_transaction.return_value = False
    p._should_read_from_local.return_value = True
    p._local_replica = SimpleNamespace(fetch_all=Mock(side_effect=OSError("replica unavailable")),
                                       fetch_one=Mock(side_effect=OSError("replica unavailable")))
    p._fetch_all_central.return_value = [(1,)]
    p._fetch_one_central.return_value = (1,)
    captured = Mock()
    monkeypatch.setattr(db_manager, "record_metric", captured)
    result = getattr(db_manager.DatabaseManager, method)(p, "SELECT 1")
    assert result == ([(1,)] if "all" in method else (1,))
    args, fields = captured.call_args
    assert fields["status"] == "ok" and fields["fallback_used"] is True
    assert not policy.MetricAggregator().observe(event(*args, **fields))


def test_identical_workload_reduction_and_accounting():
    clock = Clock()
    agg = policy.MetricAggregator(clock=clock)
    original, reduced = [], []
    for minute in range(20):
        for cycle in range(30):
            batch = [
                event("sqlite_write_lock_acquired", 1, source="local_replica_snapshot",
                      lock_path="C:/synthetic-replica/snapshot.lock", lock_token=f"{minute}-{cycle}"),
                event("sqlite_write_lock_released", 1, source="local_replica_snapshot",
                      lock_path="C:/synthetic-replica/snapshot.lock", lock_token=f"{minute}-{cycle}"),
                event("local_replica_sync_duration_ms", 10, result="unchanged", db_cycle="test-cycle", change_cursor=100),
                event("read_duration_ms", 3, status="ok", source="local_replica", operation="fetch_one"),
            ]
            for p in batch:
                original.append(p)
                if not agg.observe(p):
                    reduced.append(p)
        clock.now += 60
        reduced.extend({"ts": "2026-08-31T10:00:00+10:00", "host": "synthetic-test", "pid": 123, **s}
                       for s in agg.drain())
    original_bytes = sum(len(json.dumps(p, ensure_ascii=False).encode()) + 1 for p in original)
    reduced_bytes = sum(len(json.dumps(p, ensure_ascii=False).encode()) + 1 for p in reduced)
    print(f"Identical workload: {len(original)} -> {len(reduced)} records; "
          f"{original_bytes} -> {reduced_bytes} bytes; reduction={100 * (1 - reduced_bytes / original_bytes):.2f}%")
    assert reduced_bytes / original_bytes < 0.15
    raw_count = sum(p["metric"] != "local_metrics_summary" for p in reduced)
    assert raw_count + sum(p.get("aggregated_count", 0) for p in reduced) == len(original)


def test_latest_change_state_is_bounded_without_hiding_new_cursors(metrics, monkeypatch):
    captured = []
    monkeypatch.setattr(metrics, "_write_payloads", captured.extend)
    for i in range(1100):
        metrics.record_metric("latest_change_id", i, admission_id=i)
    assert len(captured) == 1100
    assert len(metrics._LATEST_CHANGE_METRIC_STATE) == 1024
    metrics.record_metric("latest_change_id", 2000, admission_id=1099)
    assert captured[-1]["value"] == 2000
    metrics.record_metric("latest_change_id", 2000, admission_id=1099, source="fallback")
    assert captured[-1]["source"] == "fallback"


def test_overflow_of_important_event_queue_remains_visible(metrics, monkeypatch):
    monkeypatch.setenv("REMCARD_LOCAL_METRICS_SYNC", "0")
    monkeypatch.setattr(metrics, "_ensure_worker_started", lambda: metrics._METRICS_QUEUE)
    for _ in range(101):
        metrics.record_metric("unclassified_probe", 1)
    batch = metrics._drain_queue(max_items=200)
    assert batch[-1]["metric"] == "local_metrics_dropped"
    assert batch[-1]["value"] == 1


def test_diagnostics_failure_keeps_raw_event(metrics, monkeypatch):
    monkeypatch.setattr(metrics._AGGREGATOR, "observe", Mock(side_effect=RuntimeError("test")))
    captured = []
    monkeypatch.setattr(metrics, "_write_payloads", captured.extend)
    metrics.record_metric("read_duration_ms", 1, status="ok")
    assert captured[0]["metric"] == "read_duration_ms"
