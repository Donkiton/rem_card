from __future__ import annotations

import io
import logging
import threading
from datetime import datetime, timezone

import pytest

from rem_card.app import compact_logging
from rem_card.services import crash_reports


class Clock:
    def __init__(self, value=0.0):
        self.now = value

    def __call__(self):
        return self.now


def record(message, *args, level=logging.INFO, exc_info=None, name="RemCard"):
    return logging.LogRecord(name, level, __file__, 123, message, args, exc_info, func="test_source")


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("REMCARD_TEXT_LOG_COMPACTION_ENABLED", "1")
    monkeypatch.delenv("REMCARD_TEXT_LOG_DETAIL_UNTIL", raising=False)
    monkeypatch.delenv("REMCARD_UI_HANG_MAX_DUMPS", raising=False)
    monkeypatch.delenv("REMCARD_UI_HANG_MAX_DUMPS_PER_SIGNATURE", raising=False)
    monkeypatch.delenv("REMCARD_UI_HANG_MAX_NATIVE_BYTES", raising=False)


@pytest.mark.parametrize("message,args", [
    ("[OrdersClick] click_accept admission_id=%s row=%s", (10, 4)),
    ("[TabPerf] set_active_tab_start admission_id=%s", (10,)),
    ("patient_form_refresh_start role=%s bed=%s", ("doctor", 2)),
    ("[ReadCoordinator] patient_card cache_hit=1 admission_id=%s", (10,)),
    ("[ReadCoordinator] orders_snapshot_step_end step=%s status=ok", ("orders",)),
    ("[RemCardService] orders_snapshot_sql_step_ms=3 step=%s", ("rows",)),
    ("[OrdersWidget] snapshot loaded admission_id=%s", (10,)),
])
def test_routine_info_is_suppressed(message, args):
    policy = compact_logging.CompactLogPolicy()
    assert policy.process(record(message, *args)) == []


@pytest.mark.parametrize("message", [
    "Application started version=4.3.0 role=doctor",
    "Backup completed successfully",
    "[ReadCoordinator] fallback to central error=OSError",
    "[OrdersClick] write_error admission_id=1",
    "[OrdersWidget] orders_refresh_late_result_ignored reason=stale",
    "Unknown future INFO event",
])
def test_important_and_unknown_info_fails_open(message):
    policy = compact_logging.CompactLogPolicy()
    original = record(message)
    assert policy.process(original)[-1] is original


def test_first_error_has_breadcrumbs_full_error_and_repeats_have_summary():
    clock = Clock()
    policy = compact_logging.CompactLogPolicy(clock=clock)
    policy.process(record("[OrdersClick] click_accept admission_id=%s", 777))
    policy.process(record("Service maintenance tick %s", "ok"))
    first = record("Save failed: %s", "disk unavailable", level=logging.ERROR)
    output = policy.process(first)
    assert len(output) == 2
    assert output[0].getMessage().startswith("TECHNICAL_BREADCRUMBS ")
    assert output[1] is first
    assert "777" not in output[0].getMessage()
    assert "disk unavailable" not in output[0].getMessage()
    for _ in range(8):
        assert policy.process(record("Save failed: %s", "disk unavailable", level=logging.ERROR)) == []
    checkpoint = policy.process(record("Save failed: %s", "disk unavailable", level=logging.ERROR))
    assert len(checkpoint) == 1
    payload = checkpoint[0].getMessage()
    assert "LOG_INCIDENT_SUMMARY" in payload and '"count":10' in payload
    assert '"suppressed_count":9' in payload
    final = policy.close()
    assert len(final) == 1 and '"status":"shutdown"' in final[0].getMessage()


def test_quiet_incident_closes_on_next_record():
    clock = Clock()
    policy = compact_logging.CompactLogPolicy(clock=clock)
    error = record("Repeated failure", level=logging.ERROR)
    policy.process(error)
    policy.process(error)
    clock.now = compact_logging.INCIDENT_QUIET_SEC
    output = policy.process(record("ordinary result"))
    assert output[-1].getMessage() == "ordinary result"
    assert any('"status":"quiet"' in item.getMessage() for item in output[:-1])
    assert policy.close() == []


def test_different_causes_are_not_combined():
    policy = compact_logging.CompactLogPolicy()
    first = policy.process(record("Save failed: %s", "network", level=logging.ERROR))
    second = policy.process(record("Save failed: %s", "permission", level=logging.ERROR))
    assert first[-1].getMessage().endswith("network")
    assert second[-1].getMessage().endswith("permission")
    assert len(policy._incidents) == 2


def test_incident_capacity_exhaustion_never_hides_new_error(monkeypatch):
    monkeypatch.setattr(compact_logging, "MAX_INCIDENTS", 2)
    policy = compact_logging.CompactLogPolicy()
    for value in ("one", "two"):
        policy.process(record("Failure %s", value, level=logging.ERROR))
    overflow = record("Failure %s", "three", level=logging.ERROR)
    assert policy.process(overflow)[-1] is overflow
    assert len(policy._incidents) == 2


def test_breadcrumb_buffer_is_bounded_and_contains_no_form_or_sql_values():
    policy = compact_logging.CompactLogPolicy()
    for index in range(500):
        policy.process(record("Patient form %s SQL %s", f"patient-{index}", f"SELECT secret-{index}"))
    assert len(policy._breadcrumbs) <= compact_logging.MAX_BREADCRUMBS
    assert policy._breadcrumbs[-1]["count"] == 500
    output = policy.process(record("Failure", level=logging.ERROR))[0].getMessage()
    assert "patient-" not in output and "SELECT" not in output and "secret-" not in output


def test_concurrent_repeated_error_has_one_full_record_and_exact_count():
    policy = compact_logging.CompactLogPolicy()
    accepted = []
    lock = threading.Lock()

    def produce():
        result = policy.process(record("Concurrent failure", level=logging.ERROR))
        with lock:
            accepted.extend(result)

    threads = [threading.Thread(target=produce) for _ in range(100)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    full = [item for item in accepted if item.getMessage() == "Concurrent failure"]
    assert len(full) == 1
    final = policy.close()[-1].getMessage()
    assert '"count":100' in final and '"suppressed_count":99' in final


def test_detail_mode_and_rollback_restore_original_records(monkeypatch):
    clock = Clock()
    wall = 2_000_000_000.0
    policy = compact_logging.CompactLogPolicy(clock=clock, wall_clock=lambda: wall)
    until = datetime.fromtimestamp(wall + 30, timezone.utc).isoformat()
    monkeypatch.setenv("REMCARD_TEXT_LOG_DETAIL_UNTIL", until)
    routine = record("[OrdersClick] click_accept")
    assert policy.process(routine) == [routine]
    error = record("Failure", level=logging.ERROR)
    assert policy.process(error) == [error]
    clock.now = 31
    assert policy.process(routine) == []
    monkeypatch.setenv("REMCARD_TEXT_LOG_COMPACTION_ENABLED", "0")
    assert policy.process(routine) == [routine]
    assert policy.process(error) == [error]


def test_detail_helper_rejects_unbounded_or_nonfinite_duration():
    for value in (-1, 1801, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            compact_logging.enable_detailed_text_logs(value)
    until = compact_logging.enable_detailed_text_logs(10)
    assert datetime.fromisoformat(until).tzinfo is not None


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, source):
        self.records.append(source)


def test_handler_flushes_incident_summary_on_close():
    target = ListHandler()
    handler = compact_logging.CompactLogHandler(target)
    error = record("Repeated", level=logging.ERROR)
    handler.handle(error)
    handler.handle(error)
    handler.close()
    assert [item.getMessage() for item in target.records].count("Repeated") == 1
    assert any("LOG_INCIDENT_SUMMARY" in item.getMessage() for item in target.records)
    handler.close()


def test_handler_policy_failure_keeps_source_record(monkeypatch):
    target = ListHandler()
    policy = compact_logging.CompactLogPolicy()
    monkeypatch.setattr(policy, "process", lambda source: (_ for _ in ()).throw(RuntimeError("test")))
    handler = compact_logging.CompactLogHandler(target, policy)
    source = record("must survive", level=logging.ERROR)
    handler.handle(source)
    assert target.records == [source]
    handler.close()


@pytest.fixture
def fault_file(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(crash_reports, "_FAULT_FILE", stream)
    crash_reports._reset_ui_hang_state_locked()
    monkeypatch.setattr(crash_reports.faulthandler, "dump_traceback", lambda **kwargs: kwargs["file"].write("STACK\n"))
    monkeypatch.setattr(crash_reports, "_ui_hang_stack_signature", lambda: "same-stack")
    yield stream
    crash_reports._reset_ui_hang_state_locked()


def test_hang_dump_keeps_representatives_and_summarizes_repeats(fault_file):
    for _ in range(20):
        assert crash_reports.dump_current_thread_stacks(reason="ui_hang_test")
    crash_reports._write_ui_hang_summary_locked(fault_file, status="shutdown", now=crash_reports.time.monotonic())
    content = fault_file.getvalue()
    assert content.count("STACK") == crash_reports.UI_HANG_MAX_DUMPS_PER_SIGNATURE
    assert content.count("REMCARD_THREAD_DUMP reason=") == crash_reports.UI_HANG_MAX_DUMPS_PER_SIGNATURE
    assert "REMCARD_THREAD_DUMP_SUMMARY" in content
    assert '"suppressed":18' in content


def test_hang_dump_limits_total_across_distinct_stacks(fault_file, monkeypatch):
    counter = iter(f"stack-{index}" for index in range(20))
    monkeypatch.setattr(crash_reports, "_ui_hang_stack_signature", lambda: next(counter))
    for _ in range(20):
        assert crash_reports.dump_current_thread_stacks()
    assert fault_file.getvalue().count("STACK") == crash_reports.UI_HANG_MAX_DUMPS
    assert len(crash_reports._UI_HANG_SIGNATURES) <= 16


def test_hang_dump_incident_resets_after_quiet(fault_file, monkeypatch):
    clock = Clock(1)
    monkeypatch.setattr(crash_reports.time, "monotonic", clock)
    crash_reports.dump_current_thread_stacks()
    crash_reports.dump_current_thread_stacks()
    crash_reports.dump_current_thread_stacks()
    clock.now += crash_reports.UI_HANG_INCIDENT_RESET_SEC
    crash_reports.dump_current_thread_stacks()
    content = fault_file.getvalue()
    assert content.count("STACK") == 3
    assert '"status":"quiet"' in content


def test_hang_dump_respects_byte_cap_and_rollback(fault_file, monkeypatch):
    monkeypatch.setenv("REMCARD_UI_HANG_MAX_NATIVE_BYTES", "1")
    fault_file.write("xx")
    for _ in range(5):
        crash_reports.dump_current_thread_stacks()
    assert "STACK" not in fault_file.getvalue()
    monkeypatch.setenv("REMCARD_TEXT_LOG_COMPACTION_ENABLED", "0")
    for _ in range(5):
        crash_reports.dump_current_thread_stacks()
    assert fault_file.getvalue().count("STACK") == 5


def test_hang_summary_contains_no_paths_or_form_content(fault_file, monkeypatch):
    monkeypatch.setattr(crash_reports, "_ui_hang_stack_signature", lambda: "opaque-fingerprint")
    for _ in range(3):
        crash_reports.dump_current_thread_stacks(reason="C:/Patients/secret-form.json")
    content = fault_file.getvalue()
    summary = content[content.rfind("REMCARD_THREAD_DUMP_SUMMARY"):]
    assert "Patients" not in summary and "secret-form" not in summary
    assert "opaque-fingerprint" in summary
