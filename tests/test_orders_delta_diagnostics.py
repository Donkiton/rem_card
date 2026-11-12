import threading

from rem_card.services import read_coordinator as reads


def test_expected_delta_skips_and_real_failures_have_consistent_raw_and_summary_counts(monkeypatch):
    coordinator = object.__new__(reads.ReadCoordinator)
    coordinator._orders_telemetry = reads.OrdersTelemetryState()
    coordinator._orders_telemetry_lock = threading.Lock()
    coordinator._maybe_log_orders_telemetry_summary_locked = lambda: None
    recorded = []
    monkeypatch.setattr(reads, "record_metric", lambda name, value, **fields: recorded.append((name, fields)))
    reasons = (
        "stale_base_age_exceeded:100", "unsupported_entities:some_table", "empty_change_rows",
        "orders_context_superseded", "delta_unknown_order", "unrecognized failure details",
    )
    for reason in reasons:
        coordinator._record_orders_load_telemetry(
            source="monitor", cache_hit=False, delta_applied=False, delta_failure_reason=reason,
            delta_time_ms=1, elapsed_ms=10, fallback_after_delta=True,
        )
    fields = [fields for name, fields in recorded if name == "orders_load_time_ms"]
    assert [field["delta_failed"] for field in fields] == [0, 0, 0, 0, 1, 1]
    assert [field["delta_reason"] for field in fields] == [
        "stale_base", "unsupported_scope", "empty_changes", "superseded", "merge_error", "other_error",
    ]
    assert all(field["fallback_after_delta"] == 1 and field["full_reload"] == 1 for field in fields)
    assert all(reason not in str(fields) for reason in reasons)
    summary = coordinator._build_orders_telemetry_snapshot_locked()
    assert summary["delta_failed"] == 2
    assert summary["delta_fail_merge_error_count"] == summary["delta_fail_other_count"] == 1
    assert summary["delta_skip_stale_base_count"] == 1
    assert summary["delta_skip_unsupported_scope_count"] == 1
    assert summary["delta_skip_other_count"] == 2
    assert summary["fallback_after_delta"] == 6
    assert summary["delta_skip_rate"] == 4 / 6
    assert summary["delta_fail_rate"] == 2 / 6


def test_no_delta_reason_does_not_create_an_error_or_skip(monkeypatch):
    coordinator = object.__new__(reads.ReadCoordinator)
    coordinator._orders_telemetry = reads.OrdersTelemetryState()
    coordinator._orders_telemetry_lock = threading.Lock()
    coordinator._maybe_log_orders_telemetry_summary_locked = lambda: None
    recorded = []
    monkeypatch.setattr(reads, "record_metric", lambda name, value, **fields: recorded.append(fields))
    coordinator._record_orders_load_telemetry(
        source="click", cache_hit=True, delta_applied=False, delta_failure_reason="",
        delta_time_ms=0, elapsed_ms=1, fallback_after_delta=False,
    )
    assert recorded[0]["delta_failed"] == 0
    assert recorded[0]["delta_reason"] == "none"
    summary = coordinator._build_orders_telemetry_snapshot_locked()
    assert summary["delta_skip_rate"] == summary["delta_fail_rate"] == 0
