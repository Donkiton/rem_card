from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from rem_card.services.read_coordinator import ReadCoordinator
from rem_card.services.remcard_facade import RemCardService


SHIFT = datetime(2026, 8, 6, 8, 0)


def _snapshot(admission_id: int, shift_date: datetime, version: int, *, only_committed: bool = True):
    return {
        "admission_id": admission_id,
        "shift_date": shift_date,
        "only_committed": only_committed,
        "orders": [],
        "admin_rows": [],
        "has_any_draft": False,
        "has_any_orders": False,
        "has_any_administrations": False,
        "change_id": version,
    }


def _seed_context(coordinator: ReadCoordinator, context, version: int):
    frozen = coordinator._finalize_snapshot(
        snapshot=_snapshot(context.admission_id, context.shift_date, version),
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
