from __future__ import annotations

import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from rem_card.services import read_coordinator as read_coordinator_module
from rem_card.services.read_coordinator import OrdersRefreshCancelled, ReadCoordinator
from rem_card.services.remcard_facade import RemCardService


SHIFT = datetime(2026, 8, 6, 8, 0)


def _snapshot(
    admission_id: int,
    shift_date: datetime,
    version: int,
    *,
    only_committed: bool = True,
    orders=None,
    admin_rows=None,
):
    return {
        "admission_id": admission_id,
        "shift_date": shift_date,
        "only_committed": only_committed,
        "orders": list(orders or []),
        "admin_rows": list(admin_rows or []),
        "has_any_draft": False,
        "has_any_orders": False,
        "has_any_administrations": False,
        "change_id": version,
    }


def _seed_context(coordinator: ReadCoordinator, context, version: int, *, snapshot=None):
    frozen = coordinator._finalize_snapshot(
        snapshot=snapshot or _snapshot(context.admission_id, context.shift_date, version),
        scope="orders_tab",
        tab_name="orders",
        cache_key=context.cache_key(),
        context_hash=context.hash(),
        role=context.role,
        mode=context.mode,
        source_db=context.source_db,
        variant=context.variant,
        load_strategy="test",
        load_trace_id=f"test-{context.role}-{version}",
        source="test",
        stale=False,
        invalidate_reason=None,
    )
    coordinator._store_orders_tab(context, frozen)


def _context(coordinator: ReadCoordinator):
    return coordinator.make_orders_context(
        source_db="live",
        admission_id=17,
        shift_date=SHIFT,
        role="nurse",
        mode="live",
        variant="committed",
    )


def test_committed_snapshot_replaces_doctor_cache_and_invalidates_nurse_cache():
    coordinator = ReadCoordinator(SimpleNamespace())
    doctor = coordinator.make_orders_context(
        source_db="live",
        admission_id=17,
        shift_date=SHIFT,
        role="doctor",
        mode="live",
        variant="full",
    )
    nurse = coordinator.make_orders_context(
        source_db="live",
        admission_id=17,
        shift_date=SHIFT,
        role="nurse",
        mode="live",
        variant="committed",
    )
    other_shift = coordinator.make_orders_context(
        source_db="live",
        admission_id=17,
        shift_date=SHIFT + timedelta(days=1),
        role="doctor",
        mode="live",
        variant="full",
    )
    _seed_context(coordinator, doctor, 14)
    _seed_context(coordinator, nurse, 36)
    _seed_context(coordinator, other_shift, 40)

    accepted = coordinator.accept_committed_orders_snapshot(
        doctor,
        _snapshot(17, SHIFT, 74),
    )

    assert accepted["version"] == 74
    assert accepted["context_hash"] == doctor.hash()
    assert coordinator.get_cached_tab(doctor)["version"] == 74
    assert coordinator.get_cached_tab(nurse) is None
    assert coordinator.get_cached_tab(other_shift)["version"] == 40


class _InvalidationSpy:
    def __init__(self):
        self.admissions = []
        self.invalidate_all_reasons = []

    def invalidate_orders_for_admission(self, admission_id, *, reason, shift_date=None):
        self.admissions.append((admission_id, reason, shift_date))

    def invalidate_all_orders(self, *, reason):
        self.invalidate_all_reasons.append(reason)


def test_service_invalidates_orders_cache_even_without_visible_widget():
    coordinator = _InvalidationSpy()
    service = RemCardService.__new__(RemCardService)
    service.read_coordinator = coordinator
    service._vitals = SimpleNamespace(invalidate_cache=lambda: None)

    service._handle_data_changes_for_cache(
        {
            "changed_entities": ["orders", "administrations"],
            "changes": [
                {"entity_name": "orders", "admission_id": 17},
                {"entity_name": "administrations", "admission_id": 17},
            ],
        }
    )

    assert coordinator.admissions == [(17, "change_log_orders", None)]
    assert coordinator.invalidate_all_reasons == []


def test_unscoped_orders_event_invalidates_every_orders_cache():
    coordinator = _InvalidationSpy()
    service = RemCardService.__new__(RemCardService)
    service.read_coordinator = coordinator
    service._vitals = SimpleNamespace(invalidate_cache=lambda: None)

    service._handle_data_changes_for_cache(
        {
            "forced": True,
            "changed_entities": ["orders"],
            "changes": [],
        }
    )

    assert coordinator.admissions == []
    assert coordinator.invalidate_all_reasons == ["unscoped_change_log_orders"]


def test_delayed_cached_result_yields_newer_committed_snapshot() -> None:
    probe_started = threading.Event()
    release_probe = threading.Event()

    class Service:
        @staticmethod
        def get_latest_change_id(*, admission_id, include_global):
            assert admission_id == 17
            assert include_global is False
            probe_started.set()
            assert release_probe.wait(2.0)
            return 10

    coordinator = ReadCoordinator(Service())
    context = _context(coordinator)
    _seed_context(coordinator, context, 10)
    result = []
    errors = []

    def load_cached():
        try:
            result.append(coordinator.load_orders_tab(context, source="click"))
        except Exception as exc:  # pragma: no cover - surfaced by assertion
            errors.append(exc)

    worker = threading.Thread(target=load_cached)
    worker.start()
    assert probe_started.wait(1.0)
    coordinator.accept_committed_orders_snapshot(
        context,
        _snapshot(17, SHIFT, 20),
    )
    release_probe.set()
    worker.join(2.0)

    assert not worker.is_alive()
    assert errors == []
    assert [int(snapshot["version"]) for snapshot in result] == [20]
    assert int(coordinator.get_cached_tab(context)["version"]) == 20


@pytest.mark.parametrize("early_path", ["cached", "delta"])
@pytest.mark.parametrize("eviction", ["patient_lru", "tabs_lru"])
def test_delayed_orders_read_cannot_regress_after_commit_and_cache_eviction(early_path, eviction):
    read_started = threading.Event()
    release_read = threading.Event()
    order = SimpleNamespace(id=1, is_committed=1, draft_sort_order=None)

    def pause_read():
        read_started.set()
        assert release_read.wait(2.0)

    class Service:
        @staticmethod
        def get_latest_change_id(**_kwargs):
            if early_path == "cached":
                pause_read()
            return 10 if early_path == "cached" else 11

        @staticmethod
        def get_changes_since(*_args, **_kwargs):
            pause_read()
            return [(11, "administrations", 1, 17, "update", None, None, None)]

        @staticmethod
        def get_latest_administrations(**_kwargs):
            return [{"id": 1, "order_id": 1, "planned_time": "2026-08-06 10:00:00",
                     "status": "done", "is_committed": 1}]

        @staticmethod
        def build_orders_snapshot(*_args, **_kwargs):
            return _snapshot(17, SHIFT, 11, orders=[order])

    coordinator = ReadCoordinator(Service(), max_cached_patients=1, max_tabs_per_patient=1)
    context = _context(coordinator)
    _seed_context(coordinator, context, 10, snapshot=_snapshot(17, SHIFT, 10, orders=[order]))
    result, errors = [], []

    def load():
        try:
            result.append(coordinator.load_orders_tab(context, source="monitor"))
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=load)
    worker.start()
    try:
        assert read_started.wait(1.0)
        coordinator.accept_committed_orders_snapshot(context, _snapshot(17, SHIFT, 20, orders=[order]))
        other = coordinator.make_orders_context(
            source_db="live", admission_id=18 if eviction == "patient_lru" else 17,
            shift_date=SHIFT if eviction == "patient_lru" else SHIFT + timedelta(days=1),
            role="nurse", mode="live", variant="committed",
        )
        _seed_context(coordinator, other, 30)
        assert coordinator.get_cached_tab(context) is None
    finally:
        release_read.set()
        worker.join(2.0)

    assert not worker.is_alive()
    assert result == []
    assert len(errors) == 1 and isinstance(errors[0], OrdersRefreshCancelled)
    assert coordinator.get_cached_tab(context) is None
    # The protection is retained for active reads, not every evicted patient forever.
    assert context.cache_key() not in coordinator._orders_cache_epochs
    assert context.cache_key() not in coordinator._orders_stale_versions
    assert coordinator._orders_reads_inflight == {}


@pytest.mark.parametrize("probed_version,delta_version,committed_version,full_version,expected_version", [
    pytest.param(11, 11, 20, 11, 20, id="newer-commit"),
    pytest.param(25, 25, 20, 25, 25, id="probe-floor"),
    pytest.param(11, 26, 20, 26, 26, id="delta-floor"),
    pytest.param(11, 26, 20, 20, None, id="reject-old-full-fallback"),
])
def test_delayed_delta_cannot_overwrite_newer_committed_snapshot(
    probed_version, delta_version, committed_version, full_version, expected_version,
) -> None:
    delta_started = threading.Event()
    release_delta = threading.Event()
    order = SimpleNamespace(id=1, is_committed=1, draft_sort_order=None)
    base_snapshot = _snapshot(17, SHIFT, 10, orders=[order])
    base_snapshot["orders_admin_cursor"] = "1970-01-01 00:00:00.000|0"

    class Service:
        @staticmethod
        def get_latest_change_id(*, admission_id, include_global):
            assert admission_id == 17
            assert include_global is False
            return probed_version

        @staticmethod
        def get_changes_since(last_change_id, *, admission_id, include_global):
            assert last_change_id == 10
            assert admission_id == 17
            assert include_global is False
            delta_started.set()
            assert release_delta.wait(2.0)
            return [(delta_version, "administrations", 1, 17, "update", None, None, None)]

        @staticmethod
        def build_orders_snapshot(*_args, **_kwargs):
            return _snapshot(17, SHIFT, full_version, orders=[order])

        @staticmethod
        def get_latest_administrations(**_kwargs):
            return [
                {
                    "id": 1,
                    "order_id": 1,
                    "planned_time": "2026-08-06 10:00:00",
                    "status": "done",
                    "is_committed": 1,
                }
            ]

    coordinator = ReadCoordinator(Service())
    context = _context(coordinator)
    _seed_context(coordinator, context, 10, snapshot=base_snapshot)
    result = []
    errors = []

    def load_delta():
        try:
            result.append(coordinator.load_orders_tab(context, source="monitor"))
        except Exception as exc:  # pragma: no cover - surfaced by assertion
            errors.append(exc)

    worker = threading.Thread(target=load_delta)
    worker.start()
    assert delta_started.wait(1.0)
    coordinator.accept_committed_orders_snapshot(
        context,
        _snapshot(17, SHIFT, committed_version, orders=[order]),
    )
    release_delta.set()
    worker.join(2.0)

    assert not worker.is_alive()
    if expected_version is None:
        assert result == []
        assert len(errors) == 1 and isinstance(errors[0], OrdersRefreshCancelled)
        assert int(coordinator.get_cached_tab(context)["version"]) == committed_version
        return
    assert errors == []
    assert [int(snapshot["version"]) for snapshot in result] == [expected_version]
    assert int(coordinator.get_cached_tab(context)["version"]) == expected_version


def test_orders_total_metric_contains_bounded_phase_timings(monkeypatch) -> None:
    metrics = []

    class Service:
        @staticmethod
        def get_latest_change_id(*, admission_id, include_global):
            assert admission_id == 17
            assert include_global is False
            return 10

    monkeypatch.setattr(
        read_coordinator_module,
        "record_metric",
        lambda metric, value, **fields: metrics.append((metric, value, fields)),
    )
    coordinator = ReadCoordinator(Service())
    context = _context(coordinator)
    _seed_context(coordinator, context, 10)

    snapshot = coordinator.load_orders_tab(context, source="click")

    assert int(snapshot["version"]) == 10
    total_fields = next(fields for metric, _value, fields in metrics if metric == "orders_load_time_ms")
    assert {
        "cache_lookup_ms",
        "version_probe_ms",
        "delta_attempt_ms",
        "load_slot_wait_ms",
        "full_build_ms",
        "finalize_ms",
        "apply_handoff_ms",
    }.issubset(total_fields)
    assert total_fields["source"] == "cache"
    assert total_fields["request_source"] == "click"
    assert total_fields["trace_id"] == "test-nurse-10"
    assert total_fields["request_id"].startswith("orders-")
    assert total_fields["request_id"] != total_fields["trace_id"]


def test_orders_delta_metric_keeps_request_and_result_provenance(monkeypatch) -> None:
    metrics = []
    order = SimpleNamespace(id=1, is_committed=1, draft_sort_order=None)
    base_snapshot = _snapshot(17, SHIFT, 10, orders=[order])
    base_snapshot["orders_admin_cursor"] = "1970-01-01 00:00:00.000|0"

    class Service:
        @staticmethod
        def get_latest_change_id(*, admission_id, include_global):
            assert (admission_id, include_global) == (17, False)
            return 11

        @staticmethod
        def get_changes_since(last_change_id, *, admission_id, include_global):
            assert (last_change_id, admission_id, include_global) == (10, 17, False)
            return [(11, "administrations", 1, 17, "update", None, None, None)]

        @staticmethod
        def get_latest_administrations(**_kwargs):
            return [
                {
                    "id": 1,
                    "order_id": 1,
                    "planned_time": "2026-08-06 10:00:00",
                    "status": "done",
                    "is_committed": 1,
                }
            ]

    monkeypatch.setattr(
        read_coordinator_module,
        "record_metric",
        lambda metric, value, **fields: metrics.append((metric, value, fields)),
    )
    coordinator = ReadCoordinator(Service())
    context = _context(coordinator)
    _seed_context(coordinator, context, 10, snapshot=base_snapshot)

    snapshot = coordinator.load_orders_tab(context, source="post_finalize")

    assert int(snapshot["version"]) == 11
    total_fields = next(fields for metric, _value, fields in metrics if metric == "orders_load_time_ms")
    assert total_fields["source"] == "cache"
    assert total_fields["request_source"] == "post_finalize"
    assert total_fields["delta_applied"] == 1
    assert total_fields["base"] == "cache"
    assert total_fields["request_id"].startswith("orders-")
    assert total_fields["trace_id"] == total_fields["request_id"]


def test_mixed_delta_entities_probe_once_and_fall_back_without_delta_rows() -> None:
    order = SimpleNamespace(id=1, is_committed=1, draft_sort_order=None)
    version_probes = []
    delta_row_reads = []
    full_builds = []

    class Service:
        @staticmethod
        def get_latest_change_id(*, admission_id, include_global):
            version_probes.append((admission_id, include_global))
            return 12

        @staticmethod
        def get_changes_since(last_change_id, *, admission_id, include_global):
            assert (last_change_id, admission_id, include_global) == (10, 17, False)
            return [
                (11, "administrations", 1, 17, "update", None, None, None),
                (12, "orders", 1, 17, "update", None, None, None),
            ]

        @staticmethod
        def get_latest_administrations(**kwargs):
            delta_row_reads.append(kwargs)
            raise AssertionError("mixed unsafe delta must not query administration rows")

        @staticmethod
        def build_orders_snapshot(admission_id, shift_date, *, only_committed, include_change_cursor):
            full_builds.append(
                (admission_id, shift_date, only_committed, include_change_cursor)
            )
            return _snapshot(admission_id, shift_date, 12, orders=[order])

    coordinator = ReadCoordinator(Service())
    context = _context(coordinator)
    _seed_context(
        coordinator,
        context,
        10,
        snapshot=_snapshot(17, SHIFT, 10, orders=[order]),
    )

    snapshot = coordinator.load_orders_tab(context, source="click")

    assert int(snapshot["version"]) == 12
    assert version_probes == [(17, False)]
    assert delta_row_reads == []
    assert full_builds == [(17, SHIFT, True, True)]


@pytest.mark.parametrize("probe,delta_cursor,entity,full_version", [
    (20, 20, "orders", 15),
    (11, 26, "orders", 20),
    (11, 26, "administrations", 20),
])
def test_full_fallback_below_probed_required_cursor_is_rejected(
    probe, delta_cursor, entity, full_version,
) -> None:
    order = SimpleNamespace(id=1, is_committed=1, draft_sort_order=None)

    class Service:
        @staticmethod
        def get_latest_change_id(*, admission_id, include_global):
            assert (admission_id, include_global) == (17, False)
            return probe

        @staticmethod
        def get_changes_since(last_change_id, *, admission_id, include_global):
            assert (last_change_id, admission_id, include_global) == (10, 17, False)
            return [(delta_cursor, entity, 1, 17, "update", None, None, None)]

        @staticmethod
        def get_latest_administrations(**_kwargs):
            return []

        @staticmethod
        def build_orders_snapshot(admission_id, shift_date, *, only_committed, include_change_cursor):
            assert only_committed is True
            assert include_change_cursor is True
            return _snapshot(admission_id, shift_date, full_version, orders=[order])

    coordinator = ReadCoordinator(Service())
    context = _context(coordinator)
    _seed_context(
        coordinator,
        context,
        10,
        snapshot=_snapshot(17, SHIFT, 10, orders=[order]),
    )

    with pytest.raises(OrdersRefreshCancelled, match="below required version"):
        coordinator.load_orders_tab(context, source="monitor")

    assert int(coordinator.get_cached_tab(context)["version"]) == 10


@pytest.mark.parametrize("base_kind", ["cache", "stale"])
@pytest.mark.parametrize("completed_version", [None, 25, 26, 27])
def test_coalesced_orders_load_respects_known_delta_floor(base_kind, completed_version, monkeypatch):
    order = SimpleNamespace(id=1, is_committed=1, draft_sort_order=None)
    coalesced_entered = threading.Event()
    completion_errors = []

    class Service:
        @staticmethod
        def get_latest_change_id(**_kwargs):
            return 21

        @staticmethod
        def get_changes_since(*_args, **_kwargs):
            return [(26, "orders", 1, 17, "update", None, None, None)]

        @staticmethod
        def build_orders_snapshot(*_args, **_kwargs):
            raise AssertionError("the already active full refresh must be coalesced")

    coordinator = ReadCoordinator(Service())
    context = _context(coordinator)
    _seed_context(coordinator, context, 20, snapshot=_snapshot(17, SHIFT, 20, orders=[order]))
    if base_kind == "stale":
        coordinator.invalidate_tab(context, "test")
    active_id = "coalesced-floor-active"
    active = coordinator._begin_orders_refresh(
        context=context, context_hash=context.hash(), request_id=active_id,
        source="monitor", priority="LOW", done_event=threading.Event(),
    )
    assert active["started"] is True
    original_coalesced = coordinator._coalesced_orders_snapshot

    def coalesced(**kwargs):
        coalesced_entered.set()
        return original_coalesced(**kwargs)

    monkeypatch.setattr(coordinator, "_coalesced_orders_snapshot", coalesced)

    def complete_active():
        try:
            assert coalesced_entered.wait(2.0)
            _seed_context(coordinator, context, completed_version)
            coordinator._finish_orders_refresh(context.cache_key(), request_id=active_id)
        except Exception as exc:
            completion_errors.append(exc)

    worker = threading.Thread(target=complete_active) if completed_version is not None else None
    if worker is not None:
        worker.start()
    try:
        if completed_version is None or completed_version < 26:
            with pytest.raises(TimeoutError):
                coordinator.load_orders_tab(context, source="monitor", priority="LOW")
        else:
            result = coordinator.load_orders_tab(context, source="monitor", priority="LOW")
            assert int(result["version"]) == completed_version
    finally:
        if worker is not None:
            worker.join(2.0)
            assert not worker.is_alive()
        coordinator._finish_orders_refresh(context.cache_key(), request_id=active_id)
    assert completion_errors == []


@pytest.mark.parametrize("force_refresh", [False, True])
@pytest.mark.parametrize("retirement", ["admission", "tab", "poison", "expire", "supersede", "cancel"])
def test_full_orders_publication_is_fenced_at_store(retirement, force_refresh, monkeypatch):
    store_entered, release_store = threading.Event(), threading.Event()
    result, errors = [], []

    class Service:
        get_latest_change_id = staticmethod(lambda **_kwargs: 11)
        get_changes_since = staticmethod(
            lambda *_args, **_kwargs: [(11, "orders", 1, 17, "update", None, None, None)]
        )
        build_orders_snapshot = staticmethod(lambda *_args, **_kwargs: _snapshot(17, SHIFT, 11))

    coordinator = ReadCoordinator(Service())
    context = _context(coordinator)
    _seed_context(coordinator, context, 10)
    original_store = coordinator._store_orders_tab

    def paused_store(context, snapshot, **kwargs):
        store_entered.set()
        assert release_store.wait(2.0)
        return original_store(context, snapshot, **kwargs)

    monkeypatch.setattr(coordinator, "_store_orders_tab", paused_store)

    def load():
        try:
            result.append(coordinator.load_orders_tab(
                context, source="monitor", priority="LOW", force_refresh=force_refresh,
            ))
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=load)
    worker.start()
    try:
        assert store_entered.wait(1.0)
        with coordinator._orders_refresh_lock:
            active = dict(coordinator._orders_active_refreshes[context.cache_key()])
        request_id = active["request_id"]
        if retirement == "admission":
            coordinator.invalidate_orders_for_admission(17, reason="external_commit")
        elif retirement == "tab":
            coordinator.invalidate_tab(context, "external_commit")
        elif retirement == "poison":
            coordinator._poison_orders_refresh(context.cache_key(), request_id=request_id, reason="test")
        elif retirement == "expire":
            coordinator._expire_orders_refresh(context.cache_key(), request_id=request_id, reason="test")
        elif retirement == "supersede":
            replacement = coordinator._begin_orders_refresh(
                context=context, context_hash=context.hash(), request_id="replacement",
                source="click", priority="HIGH", done_event=threading.Event(),
            )
            assert replacement["started"]
        else:
            with coordinator._orders_refresh_lock:
                coordinator._retire_orders_refresh_locked(
                    context.cache_key(), active, status="cancelled", reason="test", set_done=True,
                )
    finally:
        release_store.set()
        worker.join(2.0)

    assert not worker.is_alive()
    assert result == []
    assert len(errors) == 1 and isinstance(errors[0], OrdersRefreshCancelled)
    cached = coordinator.get_cached_tab(context)
    assert cached is None or int(cached["version"]) == 10
    if retirement == "supersede":
        with coordinator._orders_refresh_lock:
            assert coordinator._orders_active_refreshes[context.cache_key()]["request_id"] == "replacement"
