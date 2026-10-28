from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest

from rem_card.services import read_coordinator as module
from rem_card.services import remcard_facade as facade
from rem_card.services.read_coordinator import OrdersRefreshCancelled, ReadCoordinator
from rem_card.services.remcard_facade import OrdersSnapshotCancelled


SHIFT = datetime(2026, 10, 28, 8)


def snapshot(version=20):
    return {
        "admission_id": 17, "shift_date": SHIFT, "only_committed": True,
        "orders": [], "admin_rows": [], "has_any_orders": False,
        "has_any_administrations": False, "has_any_draft": False,
        "change_id": version,
    }


def sqlite_error(message="database is locked", code=None):
    error = sqlite3.OperationalError(message)
    if code is not None:
        error.sqlite_errorcode = code
    return error


class Service:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.before_build = lambda _attempt: None
        self.probe_version = 20
        self.delta_version = 20

    def build_orders_snapshot(self, *args, **kwargs):
        self.calls.append((args, kwargs, facade._ORDERS_SNAPSHOT_CALLER.get()))
        self.before_build(len(self.calls))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def get_latest_change_id(self, **_kwargs):
        return self.probe_version

    def get_changes_since(self, *_args, **_kwargs):
        return [(self.delta_version, "orders", 1, 17, "update", None, None, None)]


def coordinator(service):
    result = ReadCoordinator(service)
    context = result.make_orders_context(
        source_db="live", admission_id=17, shift_date=SHIFT,
        role="nurse", mode="live", variant="committed",
    )
    return result, context


def seed(coordinator, context, version=10):
    frozen = coordinator._finalize_snapshot(
        snapshot=snapshot(version), scope="orders_tab", tab_name="orders",
        cache_key=context.cache_key(), context_hash=context.hash(), role=context.role,
        mode=context.mode, source_db=context.source_db, variant=context.variant,
        load_strategy="test", load_trace_id="seed", source="test", stale=False,
        invalidate_reason=None,
    )
    coordinator._store_orders_tab(context, frozen)


@pytest.fixture
def metrics(monkeypatch):
    records = []
    monkeypatch.setattr(
        module, "record_metric",
        lambda name, value, **fields: records.append((name, value, fields)),
    )
    return records


@pytest.fixture
def clock(monkeypatch):
    elapsed = [100.0]
    monkeypatch.setattr(
        module, "time",
        SimpleNamespace(perf_counter=lambda: elapsed[0], monotonic=time.monotonic),
    )
    return elapsed


def assert_released(coordinator):
    assert coordinator._orders_active_refreshes == {}
    assert coordinator._active_load_tokens == {}
    assert coordinator._orders_reads_inflight == {}


@pytest.mark.parametrize("error", [
    ValueError("bad deterministic input"), TypeError("bad arguments"),
    sqlite3.ProgrammingError("bad binding"),
    sqlite_error("no such table: orders", sqlite3.SQLITE_ERROR),
    sqlite_error("database schema has changed", sqlite3.SQLITE_SCHEMA),
    sqlite_error("database is locked", sqlite3.SQLITE_CORRUPT),
    sqlite_error("readonly database", sqlite3.SQLITE_READONLY),
    sqlite_error("disk I/O error", sqlite3.SQLITE_IOERR),
    PermissionError("access denied"), TimeoutError("expired request"),
    OrdersSnapshotCancelled("detached worker"), OrdersRefreshCancelled("obsolete"),
])
def test_deterministic_cancelled_or_expired_failure_does_not_rebuild(error, metrics):
    service = Service([error, snapshot()])
    reader, context = coordinator(service)

    with pytest.raises(type(error)) as caught:
        reader.load_orders_tab(context, source="click")

    assert caught.value is error
    assert len(service.calls) == 1
    assert reader.get_cached_tab(context) is None
    if not isinstance(error, OrdersRefreshCancelled):
        first = [fields for name, _value, fields in metrics if name == "build_orders_snapshot_time_ms"]
        assert [(fields["attempt"], fields["status"]) for fields in first] == [(1, "error")]
    assert_released(reader)


@pytest.mark.parametrize("error", [
    sqlite_error(), sqlite_error("database table is locked"),
    sqlite_error("busy", sqlite3.SQLITE_BUSY),
    sqlite_error("locked", sqlite3.SQLITE_LOCKED),
    sqlite_error("busy snapshot", sqlite3.SQLITE_BUSY | (2 << 8)),
])
def test_transient_lock_retries_once_and_preserves_caller_and_failure_metrics(error, metrics):
    service = Service([error, snapshot()])
    reader, context = coordinator(service)

    result = reader.load_orders_tab(context, source="click")

    assert result["version"] == 20
    assert [call[2] for call in service.calls] == ["read_coordinator", "legacy_fallback"]
    assert service.calls[0][:2] == service.calls[1][:2]
    attempts = [fields for name, _value, fields in metrics if name == "build_orders_snapshot_time_ms"]
    assert [(item["attempt"], item["status"]) for item in attempts] == [(1, "error"), (2, "ok")]
    decision = next(fields for name, _value, fields in metrics if name == "orders_snapshot_retry_decision")
    assert decision["retry_allowed"] == 1
    assert decision["reason"] == "transient_sqlite_lock"
    assert "bad deterministic input" not in str(decision)
    assert_released(reader)


def test_second_transient_failure_is_final_and_recorded(metrics):
    first, second = sqlite_error(), sqlite_error("database table is locked")
    service = Service([first, second, snapshot()])
    reader, context = coordinator(service)

    with pytest.raises(sqlite3.OperationalError) as caught:
        reader.load_orders_tab(context, source="click")

    assert caught.value is second
    assert len(service.calls) == 2
    attempts = [fields for name, _value, fields in metrics if name == "build_orders_snapshot_time_ms"]
    assert [(item["attempt"], item["status"]) for item in attempts] == [(1, "error"), (2, "error")]
    assert reader.get_cached_tab(context) is None
    assert_released(reader)


def test_legacy_reentry_flag_cannot_reset_the_attempt_limit(metrics):
    error = sqlite_error()
    service = Service([error, snapshot()])
    reader, context = coordinator(service)

    with pytest.raises(sqlite3.OperationalError):
        reader.load_orders_tab(context, _retry=True)

    assert len(service.calls) == 1
    decision = next(fields for name, _value, fields in metrics if name == "orders_snapshot_retry_decision")
    assert (decision["retry_allowed"], decision["reason"]) == (0, "attempt_limit")
    assert_released(reader)


def test_exhausted_total_budget_prevents_retry_and_keeps_cached_epoch(clock, metrics):
    service = Service([sqlite_error(), snapshot()])
    reader, context = coordinator(service)
    seed(reader, context)
    old_epoch = reader._orders_cache_epochs.get(context.cache_key(), 0)
    service.before_build = lambda _attempt: clock.__setitem__(0, clock[0] + 5)

    with pytest.raises(OrdersRefreshCancelled, match="read_budget_exhausted"):
        reader.load_orders_tab(context, timeout_sec=5)

    assert len(service.calls) == 1
    assert reader.get_cached_tab(context)["version"] == 10
    assert reader._orders_cache_epochs.get(context.cache_key(), 0) == old_epoch
    decision = next(fields for name, _value, fields in metrics if name == "orders_snapshot_retry_decision")
    assert (decision["retry_allowed"], decision["remaining_ms"]) == (0, 0)
    assert_released(reader)


def test_second_attempt_does_not_receive_a_fresh_timeout_or_publish_late_data(clock, metrics):
    service = Service([sqlite_error(), snapshot()])
    reader, context = coordinator(service)
    seed(reader, context)
    service.before_build = lambda _attempt: clock.__setitem__(0, clock[0] + 3)

    with pytest.raises(OrdersRefreshCancelled, match="read_budget_exhausted"):
        reader.load_orders_tab(context, timeout_sec=5)

    assert len(service.calls) == 2
    assert reader.get_cached_tab(context)["version"] == 10
    assert_released(reader)


def test_probe_delta_and_queue_share_the_attempt_budget(clock, metrics, monkeypatch):
    service = Service([sqlite_error(), snapshot()])
    reader, context = coordinator(service)
    seed(reader, context)
    original_changes = service.get_changes_since
    original_slot = reader._load_slot
    queue_budgets = []

    def delayed_changes(*args, **kwargs):
        clock[0] += 2
        return original_changes(*args, **kwargs)

    @contextmanager
    def delayed_slot(**kwargs):
        queue_budgets.append(kwargs["timeout_sec"])
        with original_slot(**kwargs) as token:
            clock[0] += 2
            yield token

    service.get_changes_since = delayed_changes
    monkeypatch.setattr(reader, "_load_slot", delayed_slot)
    service.before_build = lambda _attempt: clock.__setitem__(0, clock[0] + 1)

    with pytest.raises(OrdersRefreshCancelled, match="read_budget_exhausted"):
        reader.load_orders_tab(context, timeout_sec=5)

    assert queue_budgets == [3]
    assert len(service.calls) == 1
    assert_released(reader)


@pytest.mark.parametrize("retirement", ["cancel", "expire", "poison"])
def test_cancelled_or_retired_request_cannot_retry_a_transient_failure(retirement, metrics):
    service = Service([sqlite_error(), snapshot()])
    reader, context = coordinator(service)
    cancelled = threading.Event()

    def retire(_attempt):
        if retirement == "cancel":
            cancelled.set()
            return
        active = reader._orders_active_refreshes[context.cache_key()]
        method = reader._expire_orders_refresh if retirement == "expire" else reader._poison_orders_refresh
        method(context.cache_key(), request_id=active["request_id"], reason="test")

    service.before_build = retire
    with pytest.raises(OrdersRefreshCancelled):
        reader.load_orders_tab(context, cancel_check=cancelled.is_set)

    assert len(service.calls) == 1
    assert reader.get_cached_tab(context) is None
    assert_released(reader)


def test_retry_result_below_changelog_floor_is_still_rejected(metrics):
    service = Service([sqlite_error(), snapshot(20)])
    service.delta_version = 26
    reader, context = coordinator(service)
    seed(reader, context)

    with pytest.raises(OrdersRefreshCancelled, match="below required version"):
        reader.load_orders_tab(context)

    assert len(service.calls) == 2
    assert reader.get_cached_tab(context)["version"] == 10
    assert_released(reader)


def test_service_observer_checks_total_budget_even_below_individual_sql_timeout(clock, metrics):
    service = Service([snapshot(), snapshot()])
    reader, context = coordinator(service)

    def poll(_attempt):
        observer = facade._ORDERS_SNAPSHOT_STEP_OBSERVER.get()
        observer("start", "get_orders", {})
        clock[0] += 2
        observer("end", "get_orders", {"status": "ok"})
        observer("start", "get_latest_administrations", {})
        clock[0] += 3
        observer("poll", "get_latest_administrations", {"elapsed_ms": 3000})

    service.before_build = poll
    with pytest.raises(OrdersRefreshCancelled, match="read_budget_exhausted"):
        reader.load_orders_tab(context, timeout_sec=5)

    assert len(service.calls) == 1
    assert reader.get_cached_tab(context) is None
    assert_released(reader)


def test_coalesced_wait_uses_remaining_budget_without_retiring_other_request(clock, metrics, monkeypatch):
    service = Service([snapshot()])
    reader, context = coordinator(service)
    seed(reader, context)
    waits = []

    class Completion(threading.Event):
        def wait(self, timeout=None):
            waits.append(timeout)
            clock[0] += timeout
            return True

    reader._begin_orders_refresh(
        context=context, context_hash=context.hash(), request_id="other",
        source="click", priority="HIGH", done_event=Completion(),
    )
    begin = reader._begin_orders_refresh

    def delayed_begin(**kwargs):
        clock[0] += 4.9
        return begin(**kwargs)

    monkeypatch.setattr(reader, "_begin_orders_refresh", delayed_begin)
    try:
        with pytest.raises(OrdersRefreshCancelled, match="read_budget_exhausted"):
            reader.load_orders_tab(context, source="click", force_refresh=True, timeout_sec=5)
        assert waits == [pytest.approx(0.1)]
        assert service.calls == []
        assert reader._orders_active_refreshes[context.cache_key()]["request_id"] == "other"
    finally:
        reader._finish_orders_refresh(context.cache_key(), request_id="other")
    assert_released(reader)


def test_existing_watchdog_poison_detaches_the_request_and_prevents_retry(metrics, monkeypatch):
    monkeypatch.setattr(module, "READ_ORDERS_STALL_THRESHOLD_SEC", 0.01)
    monkeypatch.setattr(module, "READ_ORDERS_POISON_THRESHOLD_SEC", 0.02)
    service = Service([sqlite_error(), snapshot()])
    reader, context = coordinator(service)
    entered, release, poisoned = threading.Event(), threading.Event(), threading.Event()
    errors = []
    original_poison = reader._poison_orders_refresh

    def poison(*args, **kwargs):
        result = original_poison(*args, **kwargs)
        poisoned.set()
        return result

    monkeypatch.setattr(reader, "_poison_orders_refresh", poison)

    def block(_attempt):
        entered.set()
        assert release.wait(2)

    def load():
        try:
            reader.load_orders_tab(context, timeout_sec=1)
        except Exception as exc:
            errors.append(exc)

    service.before_build = block
    worker = threading.Thread(target=load)
    worker.start()
    try:
        assert entered.wait(1)
        assert poisoned.wait(1)
        # The watchdog retires the result; it cannot kill an arbitrary blocked
        # synchronous backend call. That call leaves only when released.
        assert worker.is_alive()
    finally:
        release.set()
        worker.join(2)

    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], OrdersRefreshCancelled)
    assert len(service.calls) == 1
    assert reader.get_cached_tab(context) is None
    assert_released(reader)


def test_real_sqlite_busy_read_can_recover_with_one_retry(tmp_path, metrics):
    path = tmp_path / "locked.db"
    with closing(sqlite3.connect(path, isolation_level=None)) as setup:
        setup.execute("CREATE TABLE version (value INTEGER)")
        setup.execute("INSERT INTO version VALUES (20)")
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute("BEGIN EXCLUSIVE")
    service = Service([])
    read_attempts = []
    first_errors = []

    def build(*args, **kwargs):
        service.calls.append((args, kwargs, facade._ORDERS_SNAPSHOT_CALLER.get()))
        with closing(sqlite3.connect(path, isolation_level=None, timeout=0)) as conn:
            read_attempts.append(1)
            try:
                row = conn.execute("SELECT value FROM version").fetchone()
            except sqlite3.OperationalError as exc:
                first_errors.append(exc)
                writer.rollback()
                raise
        return snapshot(row[0])

    service.build_orders_snapshot = build
    reader, context = coordinator(service)
    try:
        result = reader.load_orders_tab(context, source="click")
    finally:
        writer.close()

    assert result["version"] == 20
    assert len(read_attempts) == 2
    assert len(service.calls) == 2
    assert len(first_errors) == 1
    assert module.ReadCoordinator._orders_snapshot_retry_reason(first_errors[0]) == "transient_sqlite_lock"
    assert_released(reader)
