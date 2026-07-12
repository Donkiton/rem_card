from __future__ import annotations

import threading
from datetime import datetime

import pytest

from rem_card.services import persistent_snapshot_cache as persistent_cache
from rem_card.services.read_coordinator import ReadCoordinator


SHIFT_DATE = datetime(2026, 7, 12, 8, 0, 0)
ADMISSION_ID = 41


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    assert persistent_cache.flush(timeout_sec=5.0)
    monkeypatch.setattr(
        persistent_cache,
        "PERSISTENT_SNAPSHOT_CACHE_DIR",
        tmp_path / "patient_snapshots",
    )
    persistent_cache._LAST_PRUNE_MONOTONIC.clear()
    yield
    assert persistent_cache.flush(timeout_sec=5.0)


class BlockingSnapshotService:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def _snapshot(self):
        self.started.set()
        if not self.release.wait(5.0):
            raise TimeoutError("snapshot builder was not released")
        return {
            "admission_id": ADMISSION_ID,
            "change_id": 101,
        }

    def build_vitals_snapshot(self, *_args, **_kwargs):
        return self._snapshot()

    def build_full_card_snapshot(self, *_args, **_kwargs):
        return self._snapshot()

    def build_balance_snapshot(self, *_args, **_kwargs):
        return self._snapshot()


@pytest.mark.parametrize(
    ("load_kind", "variant", "namespace", "cache_attribute", "invalidation_kind"),
    [
        ("vitals", "vitals", "patient_vitals", "_patient_vitals_cache", "exact_vitals"),
        ("card", "card_full", "patient_card", "_patient_card_cache", "admission_card"),
        ("balance", "balance_full", "patient_scope", "_patient_scope_cache", "admission_card"),
    ],
)
def test_invalidation_rejects_late_patient_snapshot_store(
    isolated_cache,
    load_kind,
    variant,
    namespace,
    cache_attribute,
    invalidation_kind,
):
    service = BlockingSnapshotService()
    coordinator = ReadCoordinator(service)
    context = coordinator.make_patient_snapshot_context(
        source_db="live",
        admission_id=ADMISSION_ID,
        shift_date=SHIFT_DATE,
        role="nurse",
        mode="live",
        variant=variant,
    )
    cache_key = context.cache_key()
    result: dict[str, object] = {}
    thread_errors: list[BaseException] = []

    def run_load():
        try:
            if load_kind == "vitals":
                result["snapshot"] = coordinator.load_patient_vitals_snapshot(
                    ADMISSION_ID,
                    SHIFT_DATE,
                    role="nurse",
                )
            elif load_kind == "card":
                result["snapshot"] = coordinator.load_patient_card_snapshot(
                    ADMISSION_ID,
                    SHIFT_DATE,
                    role="nurse",
                )
            else:
                result["snapshot"] = coordinator.load_balance_snapshot(
                    ADMISSION_ID,
                    SHIFT_DATE,
                    role="nurse",
                )
        except BaseException as exc:  # pragma: no cover - asserted in the parent thread
            thread_errors.append(exc)

    load_thread = threading.Thread(target=run_load, daemon=True)
    load_thread.start()
    try:
        assert service.started.wait(5.0)
        if invalidation_kind == "exact_vitals":
            coordinator.invalidate_patient_vitals(
                source_db="live",
                admission_id=ADMISSION_ID,
                shift_date=SHIFT_DATE,
                role="nurse",
                mode="live",
            )
        else:
            coordinator.invalidate_patient_card_for_admission(
                ADMISSION_ID,
                reason="deterministic_race_test",
            )
    finally:
        service.release.set()

    load_thread.join(5.0)
    assert not load_thread.is_alive()
    assert thread_errors == []
    assert result["snapshot"]["version"] == 101
    assert cache_key not in getattr(coordinator, cache_attribute)
    assert persistent_cache.flush(timeout_sec=5.0)
    assert persistent_cache.load_snapshot(namespace, cache_key) is None
