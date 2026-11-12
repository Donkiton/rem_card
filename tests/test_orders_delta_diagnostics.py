import threading

import pytest

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


@pytest.mark.parametrize("entities,expected,unchanged", [
    (["vitals"], True, True),
    (["fluids", "oral_intake_events", "lab_orders"], True, True),
    (["vitals", "administrations"], True, False),
    (["administrations", "orders", "vitals"], False, False),
    (["vitals", "future_entity"], False, False),
    ([""], False, False),
])
def test_delta_plan_separates_payload_changes_from_proven_irrelevant_entities(entities, expected, unchanged):
    applier = reads.ChangeLogApplier(None)
    rows = [(11 + index, entity, 1, 17, "update", None, None, None)
            for index, entity in enumerate(entities)]
    plan = applier.get_orders_delta_plan(base_snapshot={"version": 10}, change_rows=rows, complete_window=True)
    assert plan["apply"] is expected
    assert bool(plan.get("payload_unchanged")) is unchanged


def test_irrelevant_entity_is_not_ignored_without_history_completeness():
    applier = reads.ChangeLogApplier(None)
    plan = applier.get_orders_delta_plan(
        base_snapshot={"version": 10}, change_rows=[(11, "vitals", 1, 17, "update", None, None, None)],
    )
    assert plan["apply"] is False


def test_conservative_history_fallback_is_a_skip_and_not_a_delta_error(monkeypatch):
    coordinator = reads.ReadCoordinator(None)
    fields = []
    monkeypatch.setattr(reads, "record_metric", lambda name, value, **payload: fields.append(payload))
    coordinator._record_orders_load_telemetry(
        source="monitor", cache_hit=False, delta_applied=False,
        delta_failure_reason="orders_change_window_incomplete", delta_time_ms=1,
        elapsed_ms=5, fallback_after_delta=True,
    )
    assert fields[0]["delta_failed"] == 0 and fields[0]["delta_reason"] == "history_unavailable"
    assert fields[0]["full_reload"] == fields[0]["fallback_after_delta"] == 1
    summary = coordinator._build_orders_telemetry_snapshot_locked()
    assert summary["delta_failed"] == 0 and summary["delta_skip_other_count"] == 1
