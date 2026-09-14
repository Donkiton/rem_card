from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path

import pytest

from rem_card.app.unified_access import (
    MaintenanceConflictError,
    MaintenanceStore,
    SessionLease,
)


def _session_worker(root: str, ready, release, result) -> None:
    lease = SessionLease(root, "doctor")
    acquired = lease.acquire()
    result.put((acquired, lease.ownership))
    ready.set()
    if acquired:
        release.wait(10)
        lease.release()


def test_read_pristine_root_is_open_without_creating_control_files(tmp_path: Path) -> None:
    store = MaintenanceStore(tmp_path)

    assert store.read()["state"] == "open"
    assert not (tmp_path / "session_locks").exists()


def test_missing_root_and_malformed_state_fail_closed(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    assert MaintenanceStore(missing).read()["state"] == "unknown"

    store = MaintenanceStore(tmp_path)
    store.control_dir.mkdir(parents=True)
    store.state_guard_path.write_bytes(b"\0")
    store.state_path.write_text("{broken", encoding="utf-8")

    assert store.read()["state"] == "unknown"
    lease = SessionLease(tmp_path, "nurse")
    assert lease.acquire() is False
    assert lease.rejection_state["state"] == "unknown"


def test_same_role_has_multiple_independent_session_leases(tmp_path: Path) -> None:
    first = SessionLease(tmp_path, "doctor")
    second = SessionLease(tmp_path, "doctor")
    assert first.acquire()
    assert second.acquire()
    assert first.session_id != second.session_id
    assert first.metadata_path.is_file()
    assert second.metadata_path.is_file()

    first.release()
    assert not first.metadata_path.exists()
    assert second.held
    second.release()


def test_begin_serializes_with_entry_and_blocks_late_session(tmp_path: Path) -> None:
    store = MaintenanceStore(tmp_path)
    draining = store.begin("schema migration", expected_generation=0)

    late = SessionLease(tmp_path, "operblock_planned")
    assert late.acquire() is False
    assert late.rejection_state["state"] == "draining"
    assert late.rejection_state["operation_id"] == draining["operation_id"]

    reopened = store.cancel(expected_generation=draining["generation"])
    assert reopened["state"] == "open"
    admitted = SessionLease(tmp_path, "operblock_planned")
    assert admitted.acquire()
    admitted.release()


def test_cross_process_session_blocks_exclusive_until_os_lock_release(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    result = context.Queue()
    process = context.Process(target=_session_worker, args=(str(tmp_path), ready, release, result))
    process.start()
    try:
        assert ready.wait(10), "child session did not start"
        acquired, ownership = result.get(timeout=5)
        assert acquired
        assert ownership["pid"] == process.pid

        store = MaintenanceStore(tmp_path)
        draining = store.begin("exclusive test")
        assert store.try_exclusive() is None

        release.set()
        process.join(10)
        assert process.exitcode == 0

        exclusive = store.try_exclusive()
        assert exclusive is not None and exclusive.held
        assert store.read()["state"] == "maintenance"
        reopened = store.finish()
        assert reopened["state"] == "open"
        assert not exclusive.held
        assert reopened["generation"] > draining["generation"]
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)


def test_stale_generation_and_wrong_controller_cannot_reopen(tmp_path: Path) -> None:
    owner = MaintenanceStore(tmp_path)
    draining = owner.begin("owned operation")

    stale = MaintenanceStore(tmp_path)
    with pytest.raises(MaintenanceConflictError):
        stale.cancel(
            expected_generation=draining["generation"] - 1,
            operation_id=draining["operation_id"],
            owner_token=draining["owner_token"],
        )

    exclusive = owner.try_exclusive()
    assert exclusive is not None
    current = owner.read()
    with pytest.raises(MaintenanceConflictError):
        stale.finish(
            expected_generation=current["generation"],
            operation_id=current["operation_id"],
            owner_token=current["owner_token"],
        )
    assert owner.read()["state"] == "maintenance"
    owner.finish()


def test_another_shell_can_resume_persistent_maintenance(tmp_path: Path) -> None:
    initiator = MaintenanceStore(tmp_path)
    initiator.begin("initiator may exit")
    abandoned = initiator.try_exclusive()
    assert abandoned is not None
    abandoned.release()

    resumed = MaintenanceStore(tmp_path)
    state = resumed.read()
    exclusive = resumed.try_exclusive(
        expected_generation=state["generation"],
        operation_id=state["operation_id"],
        owner_token=state["owner_token"],
    )
    assert exclusive is not None
    assert resumed.finish(expected_generation=exclusive.generation)["state"] == "open"


def test_legacy_role_marker_blocks_exclusive_without_ttl_takeover(tmp_path: Path) -> None:
    legacy_dir = tmp_path / "session_locks"
    legacy_dir.mkdir()
    legacy = legacy_dir / "doctor.lock"
    legacy.write_text(json.dumps({"timestamp": 0, "role": "doctor"}), encoding="utf-8")
    os.utime(legacy, (1, 1))

    store = MaintenanceStore(tmp_path)
    draining = store.begin("legacy coexistence")
    assert str(legacy) in store.legacy_session_markers()
    assert store.try_exclusive() is None
    assert legacy.exists()
    store.cancel(expected_generation=draining["generation"])


def test_missing_or_stale_session_metadata_is_never_safety_evidence(tmp_path: Path) -> None:
    lease = SessionLease(tmp_path, "nurse")
    assert lease.acquire()
    lease.metadata_path.unlink()

    store = MaintenanceStore(tmp_path)
    store.begin("metadata is advisory")
    assert store.try_exclusive() is None
    lease.release()
    assert store.try_exclusive() is not None
    store.finish()
