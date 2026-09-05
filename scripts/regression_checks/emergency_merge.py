"""Safety-сценарии: emergency_merge."""

from __future__ import annotations

from .common import PROJECT_ROOT
from pathlib import Path
from .common import _REGRESSION_RESTORE_PROBES
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import uuid


def _write_restore_probe_client_policy(path: str) -> None:
    from rem_card.app.sqlite_shared import NETWORK_SAFE_DB_PROFILE
    from rem_card.app.version import APP_VERSION

    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "schema_version": 1,
        "min_client_version": APP_VERSION,
        "required_db_profile": NETWORK_SAFE_DB_PROFILE,
        "wal_allowed_on_shared_db": False,
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _prepare_restore_probe_fixture(
    temp_root: str,
    *,
    success_rounds_required: int = 1,
    role: str = "nurse",
    mode: str = "emergency",
    write_idle: bool = True,
    maintenance_idle: bool = True,
):
    from .emergency_standby import _prepare_emergency_store_fixture
    from dataclasses import replace

    from rem_card.app.emergency_restore_probe import EmergencyRestoreProbe
    from rem_card.app.emergency_validation import validate_medical_db_snapshot

    store, standby = _prepare_emergency_store_fixture(temp_root)
    session_id = f"restore_probe_{uuid.uuid4().hex[:10]}"
    session = store.create_active_session_from_standby(standby, session_id=session_id)
    network_baza = os.path.join(temp_root, "restore_probe_network", session_id)
    medical_path = os.path.join(network_baza, "archiv", "rao_journal.db")
    settings_path = os.path.join(network_baza, "settings", "remcard_settings.db")
    for directory in (
        os.path.dirname(medical_path),
        os.path.dirname(settings_path),
        os.path.join(network_baza, "backup_health"),
        os.path.join(network_baza, "session_locks"),
        os.path.join(network_baza, "locks"),
        os.path.join(network_baza, "config"),
    ):
        os.makedirs(directory, exist_ok=True)
    shutil.copy2(session.base_snapshot_path, medical_path)
    shutil.copy2(str(session.settings_snapshot_path), settings_path)
    _write_restore_probe_client_policy(os.path.join(network_baza, "config", "client_policy.json"))
    remote_validation = validate_medical_db_snapshot(medical_path)
    session = replace(
        session,
        base_remote_db_path=medical_path,
        base_remote_fingerprint=dict(remote_validation.fingerprint),
        base_last_change_id=int(remote_validation.last_change_id or 0),
        standby_last_change_id=int(remote_validation.last_change_id or 0),
        last_observed_remote_change_id=int(remote_validation.last_change_id or 0),
    )
    store.write_active_session(session)
    runtime_context = store.build_active_runtime_context(session.emergency_session_id)
    if mode != "emergency":
        runtime_context = replace(runtime_context, mode=mode, is_network=(mode == "network"), is_emergency=False, is_snapshot=False)
    probe = EmergencyRestoreProbe(
        role=role,
        runtime_context=runtime_context,
        store=store,
        session_metadata=session,
        success_rounds_required=success_rounds_required,
        stability_window_sec=60.0,
        source_medical_db_path=medical_path,
        source_settings_db_path=settings_path,
        network_baza_dir=network_baza,
        is_local_write_idle=lambda: write_idle,
        is_local_maintenance_idle=lambda: maintenance_idle,
        is_shutdown=lambda: False,
    )
    _REGRESSION_RESTORE_PROBES.append(probe)
    return {
        "store": store,
        "session": session,
        "probe": probe,
        "network_baza": network_baza,
        "medical_path": medical_path,
        "settings_path": settings_path,
    }


def _copy_restore_probe_network_fixture(fixture: dict, target_baza: str) -> tuple[str, str]:
    medical_path = os.path.join(target_baza, "archiv", "rao_journal.db")
    settings_path = os.path.join(target_baza, "settings", "remcard_settings.db")
    for directory in (
        os.path.dirname(medical_path),
        os.path.dirname(settings_path),
        os.path.join(target_baza, "backup_health"),
        os.path.join(target_baza, "session_locks"),
        os.path.join(target_baza, "locks"),
        os.path.join(target_baza, "config"),
    ):
        os.makedirs(directory, exist_ok=True)
    shutil.copy2(fixture["medical_path"], medical_path)
    shutil.copy2(fixture["settings_path"], settings_path)
    _write_restore_probe_client_policy(os.path.join(target_baza, "config", "client_policy.json"))
    return medical_path, settings_path


def _check_remote_identity_rejects_same_suffix_different_unc(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_merge_dry_run import _path_identity_compatible as dry_run_identity
    from rem_card.app.emergency_restore_probe import _path_identity_compatible as restore_identity

    _ = temp_root
    left = r"\\serverA\Baza\archiv\rao_journal.db"
    right = r"\\serverB\Baza\archiv\rao_journal.db"
    if restore_identity(left, right) or dry_run_identity(left, right):
        return False, "same suffix on different UNC servers was accepted as same identity"
    if not restore_identity(left, left) or not dry_run_identity(left, left):
        return False, "same exact remote path was not accepted"
    return True, "ok"


def _check_dry_run_blocks_remote_identity_mismatch(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_merge_dry_run import EmergencyMergeDryRunService

    fixture = _prepare_restore_probe_fixture(temp_root, success_rounds_required=1)
    fixture["probe"].run_probe_once()
    marker_path = fixture["probe"].mark_merge_ready()
    other_baza = os.path.join(temp_root, "other_network", fixture["session"].emergency_session_id)
    other_medical, other_settings = _copy_restore_probe_network_fixture(fixture, other_baza)
    service = EmergencyMergeDryRunService(
        role="nurse",
        runtime_context=fixture["store"].build_active_runtime_context(fixture["session"].emergency_session_id),
        store=fixture["store"],
        source_medical_db_path=other_medical,
        source_settings_db_path=other_settings,
        network_baza_dir=other_baza,
    )
    result = service.run_dry_run(fixture["session"].emergency_session_id, marker_path)
    if result.ok or not result.blockers:
        return False, f"identity mismatch dry-run was not blocked: {result.to_dict()}"
    reasons = " ".join(str(item.get("reason") or "") for item in result.blockers)
    if "identity" not in reasons:
        return False, f"dry-run blocker is not identity mismatch: {result.to_dict()}"
    return True, "ok"


def _check_merge_blocks_remote_identity_mismatch(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _append_emergency_medical_change
    from rem_card.app.emergency_merge_dry_run import EmergencyMergeDryRunService
    from rem_card.app.emergency_merge_mode_a import EmergencyModeAMergeService

    fixture = _prepare_restore_probe_fixture(temp_root, success_rounds_required=1)
    _append_emergency_medical_change(fixture["session"].local_db_path, entity_id=6201)
    fixture["probe"].run_probe_once()
    marker_path = fixture["probe"].mark_merge_ready()
    dry_run = EmergencyMergeDryRunService(
        role="nurse",
        runtime_context=fixture["store"].build_active_runtime_context(fixture["session"].emergency_session_id),
        store=fixture["store"],
        source_medical_db_path=fixture["medical_path"],
        source_settings_db_path=fixture["settings_path"],
        network_baza_dir=fixture["network_baza"],
    ).run_dry_run(fixture["session"].emergency_session_id, marker_path)
    if not dry_run.ok:
        return False, f"fixture dry-run failed before identity mismatch merge check: {dry_run.to_dict()}"
    other_baza = os.path.join(temp_root, "other_network_merge", fixture["session"].emergency_session_id)
    other_medical, other_settings = _copy_restore_probe_network_fixture(fixture, other_baza)
    before_hash = _file_hash(other_medical)
    merge = EmergencyModeAMergeService(
        role="nurse",
        runtime_context=fixture["store"].build_active_runtime_context(fixture["session"].emergency_session_id),
        store=fixture["store"],
        source_medical_db_path=other_medical,
        source_settings_db_path=other_settings,
        network_baza_dir=other_baza,
    ).run_merge(fixture["session"].emergency_session_id, dry_run.report_path, marker_path)
    if merge.ok or merge.error_code != "blocked_remote_identity_mismatch":
        return False, f"identity mismatch merge was not blocked: {merge.to_dict()}"
    if _file_hash(other_medical) != before_hash:
        return False, "identity mismatch merge modified remote DB"
    return True, "ok"


def _check_restore_probe_does_not_write_merge_ready_on_identity_mismatch(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_restore_probe import merge_ready_marker_path

    fixture = _prepare_restore_probe_fixture(temp_root, success_rounds_required=1)
    other_baza = os.path.join(temp_root, "other_network_probe", fixture["session"].emergency_session_id)
    other_medical, other_settings = _copy_restore_probe_network_fixture(fixture, other_baza)
    fixture["probe"].source_medical_db_path = other_medical
    fixture["probe"].source_settings_db_path = other_settings
    fixture["probe"].network_baza_dir = other_baza
    status = fixture["probe"].run_probe_once()
    if status.get("status") != "remote_identity_mismatch":
        return False, f"restore probe did not report identity mismatch: {status}"
    marker_path = merge_ready_marker_path(fixture["store"].resolve_root(), fixture["session"].emergency_session_id)
    if os.path.exists(marker_path):
        return False, "restore probe wrote merge-ready marker despite identity mismatch"
    return True, "ok"


def _run_restore_probe_rounds(probe, count: int) -> dict:
    status = {}
    for _ in range(count):
        status = probe.run_probe_once()
    return status


def _check_restore_probe_runs_in_any_emergency_role(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_restore_probe import EmergencyRestoreProbe

    if not EmergencyRestoreProbe.is_enabled_for_runtime("nurse", "emergency"):
        return False, "nurse emergency mode must enable restore probe"
    if not EmergencyRestoreProbe.is_enabled_for_runtime("doctor", "emergency"):
        return False, "doctor emergency mode must enable restore probe"
    for role, mode in (("admin", "emergency"), ("nurse", "network"), ("doctor", "network")):
        if EmergencyRestoreProbe.is_enabled_for_runtime(role, mode):
            return False, f"restore probe unexpectedly enabled for role={role} mode={mode}"
    fixture = _prepare_restore_probe_fixture(temp_root, role="doctor")
    status = fixture["probe"].run_probe_once()
    if status.get("status") != "merge_ready_mode_a":
        return False, f"doctor emergency probe did not run normally: {status}"
    return True, "ok"


def _check_restore_probe_requires_active_emergency_session(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_restore_probe_fixture(temp_root)
    store = fixture["store"]
    session = fixture["session"]
    store.mark_session_status(session.emergency_session_id, "merge_pending")
    status = fixture["probe"].run_probe_once()
    if status.get("status") != "active_emergency_session_required":
        return False, f"inactive session was accepted: {status}"
    return True, "ok"


def _check_restore_probe_checks_medical_and_settings_db(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_restore_probe_fixture(temp_root)
    os.remove(fixture["settings_path"])
    status = fixture["probe"].run_probe_once()
    if status.get("status") != "network_settings_db_missing":
        return False, f"missing settings DB was not detected: {status}"
    os.remove(fixture["medical_path"])
    status = fixture["probe"].run_probe_once()
    if status.get("status") != "network_medical_db_missing":
        return False, f"missing medical DB was not detected: {status}"
    return True, "ok"


def _check_restore_probe_requires_multiple_success_rounds(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_restore_probe_fixture(temp_root, success_rounds_required=3)
    first = fixture["probe"].run_probe_once()
    if first.get("status") == "merge_ready_mode_a" or first.get("merge_ready"):
        return False, f"probe became stable after one round: {first}"
    final = _run_restore_probe_rounds(fixture["probe"], 2)
    if final.get("status") != "merge_ready_mode_a" or not final.get("merge_ready"):
        return False, f"probe did not become stable after three rounds: {final}"
    return True, "ok"


def _check_restore_probe_resets_stability_on_failure(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_restore_probe_fixture(temp_root, success_rounds_required=3)
    first = fixture["probe"].run_probe_once()
    if int(first.get("consecutive_successes") or 0) != 1:
        return False, f"first success was not counted: {first}"
    os.remove(fixture["settings_path"])
    failed = fixture["probe"].run_probe_once()
    if int(failed.get("consecutive_successes") or 0) != 0:
        return False, f"failure did not reset stability: {failed}"
    return True, "ok"


def _check_restore_probe_no_recovery_on_unavailable(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    text = (PROJECT_ROOT / "app" / "emergency_restore_probe.py").read_text(encoding="utf-8")
    forbidden = ("recover_shared_db", "recover_shared_db_with_locks", "_recover_shared_db", "run_startup_db_guard")
    for token in forbidden:
        if token in text:
            return False, f"restore probe references recovery path: {token}"
    return True, "ok"


def _check_restore_probe_no_merge_code(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    text = (PROJECT_ROOT / "app" / "emergency_restore_probe.py").read_text(encoding="utf-8").lower()
    forbidden = ("dry_run", "dry-run", "restore_database(", "replace remote", "remote replace", "archive_session(")
    for token in forbidden:
        if token in text:
            return False, f"restore probe contains forbidden merge/replace token: {token}"
    return True, "ok"


def _check_restore_probe_remote_unchanged_sets_mode_a_ready(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_restore_probe_fixture(temp_root, success_rounds_required=1)
    status = fixture["probe"].run_probe_once()
    if status.get("status") != "merge_ready_mode_a" or status.get("merge_mode") != "mode_a_remote_unchanged":
        return False, f"remote unchanged was not mode A ready: {status}"
    return True, "ok"


def _check_restore_probe_remote_changed_sets_conflict_pending(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _append_emergency_medical_change
    fixture = _prepare_restore_probe_fixture(temp_root, success_rounds_required=1)
    _append_emergency_medical_change(fixture["medical_path"], entity_id=202)
    status = fixture["probe"].run_probe_once()
    if status.get("status") != "remote_changed_conflict_pending" or status.get("merge_ready"):
        return False, f"remote changed did not become conflict-pending: {status}"
    return True, "ok"


def _check_restore_probe_remote_less_than_base_is_inconsistent(temp_root: str) -> tuple[bool, str]:
    from dataclasses import replace

    fixture = _prepare_restore_probe_fixture(temp_root, success_rounds_required=1)
    store = fixture["store"]
    session = replace(fixture["session"], base_last_change_id=99)
    store.write_active_session(session)
    status = fixture["probe"].run_probe_once()
    if status.get("status") != "remote_inconsistent":
        return False, f"remote < base was not inconsistent: {status}"
    return True, "ok"


def _check_restore_probe_checks_session_locks(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_restore_probe_fixture(temp_root)
    lock_path = os.path.join(fixture["network_baza"], "session_locks", "doctor.lock")
    Path(lock_path).write_text("busy", encoding="utf-8")
    status = fixture["probe"].run_probe_once()
    if status.get("status") != "session_lock_active":
        return False, f"session lock was not detected: {status}"
    return True, "ok"


def _check_restore_probe_marks_network_emergency_nurse_role(temp_root: str) -> tuple[bool, str]:
    from rem_card.app import runtime_paths
    from rem_card.app.role_session_lock import RoleSessionLock

    dev_fixture = _prepare_restore_probe_fixture(os.path.join(temp_root, "dev"))
    dev_marker_path = os.path.join(
        dev_fixture["network_baza"],
        "session_locks",
        "nurse_emergency.lock",
    )
    dev_status = dev_fixture["probe"].run_probe_once()
    if dev_status.get("status") != "merge_ready_mode_a":
        return False, f"dev restore probe did not reach network marker path: {dev_status}"
    if os.path.exists(dev_marker_path):
        return False, "dev restore probe must not create a network role marker"

    fixture = _prepare_restore_probe_fixture(os.path.join(temp_root, "compiled"))
    marker_path = os.path.join(fixture["network_baza"], "session_locks", "nurse_emergency.lock")
    original_is_compiled = runtime_paths.is_compiled
    try:
        runtime_paths.is_compiled = lambda: True
        status = fixture["probe"].run_probe_once()
    finally:
        runtime_paths.is_compiled = original_is_compiled
    if status.get("status") != "merge_ready_mode_a":
        return False, f"restore probe did not reach network marker path: {status}"
    if not os.path.isfile(marker_path):
        return False, "network emergency nurse marker was not created"
    checker = RoleSessionLock(
        lock_path=marker_path,
        role="nurse_emergency",
        owner_id=f"{socket.gethostname()}:{os.getpid()}:regression_rotation_check",
        stale_timeout_sec=75.0,
        heartbeat_sec=60.0,
    )
    if not checker.is_held_by_other():
        return False, "network emergency nurse marker is not seen as active"
    fixture["probe"].release_network_emergency_role_marker()
    if os.path.exists(marker_path):
        return False, "network emergency nurse marker was not released"
    return True, "ok"


def _check_restore_probe_checks_merge_lock(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_restore_probe_fixture(temp_root)
    lock_path = os.path.join(fixture["network_baza"], "locks", "emergency_merge.lock")
    Path(lock_path).write_text("busy", encoding="utf-8")
    status = fixture["probe"].run_probe_once()
    if status.get("status") != "emergency_merge_lock_active":
        return False, f"merge lock was not detected: {status}"
    return True, "ok"


def _check_restore_probe_checks_probe_file_write_delete(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_restore_probe_fixture(temp_root)
    status = fixture["probe"].run_probe_once()
    probe_file = os.path.join(fixture["network_baza"], "backup_health", "emergency_restore_probe.tmp")
    if os.path.exists(probe_file):
        return False, f"probe file was not deleted: {probe_file}"
    if status.get("status") != "merge_ready_mode_a":
        return False, f"healthy probe did not pass probe-file check: {status}"
    return True, "ok"


def _check_restore_probe_writes_merge_ready_marker_for_stable_merge_modes(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _append_emergency_medical_change
    conflict = _prepare_restore_probe_fixture(temp_root, success_rounds_required=1)
    _append_emergency_medical_change(conflict["medical_path"], entity_id=303)
    conflict_status = conflict["probe"].run_probe_once()
    conflict_marker_path = conflict["probe"].write_merge_ready_marker()
    conflict_marker = _read_json(conflict_marker_path)
    if conflict_status.get("status") != "remote_changed_conflict_pending":
        return False, f"fixture did not become remote_changed: {conflict_status}"
    if conflict_marker.get("mode") != "remote_changed_emergency_authoritative":
        return False, f"remote_changed marker mode mismatch: {conflict_marker}"

    mode_a = _prepare_restore_probe_fixture(temp_root, success_rounds_required=1)
    mode_a["probe"].run_probe_once()
    marker_path = mode_a["probe"].write_merge_ready_marker()
    marker = _read_json(marker_path)
    if not os.path.isfile(marker_path) or marker.get("mode") != "mode_a_remote_unchanged":
        return False, f"mode A marker mismatch: {marker_path} {marker}"
    return True, "ok"


def _check_restore_probe_close_for_merge_sets_session_merge_pending(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_restore_probe_fixture(temp_root, success_rounds_required=1)
    fixture["probe"].run_probe_once()
    marker_path = fixture["probe"].mark_merge_ready()
    loaded = fixture["store"].read_active_session(fixture["session"].emergency_session_id)
    if loaded.status != "merge_pending":
        return False, f"session was not marked merge_pending: {loaded.status}"
    if not os.path.isfile(marker_path):
        return False, f"merge-ready marker missing: {marker_path}"
    return True, "ok"


def _check_restore_probe_continue_emergency_does_not_write_marker(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_restore_probe_fixture(temp_root, success_rounds_required=1)
    status = fixture["probe"].run_probe_once()
    marker_path = status.get("merge_ready_marker_path")
    if os.path.exists(str(marker_path)):
        return False, f"marker exists before explicit close-for-merge: {marker_path}"
    loaded = fixture["store"].read_active_session(fixture["session"].emergency_session_id)
    if loaded.status != "active":
        return False, f"continue emergency changed session status: {loaded.status}"
    return True, "ok"


def _check_restore_probe_worker_does_not_update_qwidget_directly(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    probe_text = (PROJECT_ROOT / "app" / "emergency_restore_probe.py").read_text(encoding="utf-8")
    for token in ("PySide6", "QWidget", "QLabel", "QMessageBox", "CustomMessageBox"):
        if token in probe_text:
            return False, f"restore probe worker references UI token: {token}"
    ui_text = (PROJECT_ROOT / "ui" / "main_window.py").read_text(encoding="utf-8")
    if "restore_probe_status" not in ui_text or "Qt.QueuedConnection" not in ui_text:
        return False, "restore probe UI path must use queued Qt signal delivery"
    return True, "ok"


def _check_restore_probe_shutdown_stops_worker(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_restore_probe import EmergencyRestoreProbeScheduler

    fixture = _prepare_restore_probe_fixture(temp_root)
    scheduler = EmergencyRestoreProbeScheduler(fixture["probe"], interval_sec=60.0, on_status=lambda payload: None)
    if not scheduler.start():
        return False, "restore probe scheduler did not start"
    if not scheduler.stop(timeout=2.0):
        return False, f"restore probe scheduler did not stop: {scheduler.get_status()}"
    if scheduler.get_status().get("running"):
        return False, "restore probe scheduler reports running after stop"
    return True, "ok"


def _check_restore_probe_dialog_text_mentions_close_for_merge_and_no_other_pcs(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_restore_probe import MERGE_READY_MODE_A_MESSAGE, REMOTE_CHANGED_CONFLICT_MESSAGE

    main_window_text = (PROJECT_ROOT / "ui" / "main_window.py").read_text(encoding="utf-8")
    if "закрыть RemCard" not in MERGE_READY_MODE_A_MESSAGE:
        return False, "merge-ready dialog must tell user to close RemCard"
    if "Не запускайте RemCard на других компьютерах" not in MERGE_READY_MODE_A_MESSAGE:
        return False, "merge-ready dialog must warn about other PCs"
    if "сетевая база изменилась" not in REMOTE_CHANGED_CONFLICT_MESSAGE:
        return False, "remote-changed warning text missing"
    for token in (
        "EmergencyActionDialog.ask",
        "Да, объединить",
        "Без объединения",
        "time.monotonic() + 60.0",
        "_close_for_emergency_discard",
        "remote_changed_conflict_pending",
        "EmergencyPasswordDialog.verify",
        "verify_emergency_password",
        "mark_session_discarded",
        "finalize_pending_emergency_discard",
        "Вернуться в аварийный режим",
    ):
        if token not in main_window_text:
            return False, f"restore prompt action token missing: {token}"
    main_text = (PROJECT_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    if "finalize_pending_emergency_discard" not in main_text or "db_shutdown_ok" not in main_text:
        return False, "discard finalization must run after DB shutdown"
    return True, "ok"


def _check_restore_probe_no_sqlite_profile_changes(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _check_no_sqlite_safety_changes
    return _check_no_sqlite_safety_changes(temp_root)


def _prepare_merge_dry_run_fixture(
    temp_root: str,
    *,
    marker: bool = True,
    status: str = "merge_pending",
    role: str = "nurse",
):
    from rem_card.app.emergency_merge_dry_run import EmergencyMergeDryRunService
    from rem_card.app.emergency_restore_probe import write_merge_ready_marker

    fixture = _prepare_restore_probe_fixture(temp_root, success_rounds_required=1, role="nurse")
    store = fixture["store"]
    session = fixture["session"]
    marker_path = ""
    if marker:
        marker_path = write_merge_ready_marker(
            store.resolve_root(),
            session,
            remote_last_change_id=session.base_last_change_id,
            remote_fingerprint=dict(session.base_remote_fingerprint or {}),
        )
    if status != session.status:
        store.mark_session_status(session.emergency_session_id, status)
    service = EmergencyMergeDryRunService(
        role=role,
        runtime_context=store.build_active_runtime_context(session.emergency_session_id),
        store=store,
        source_medical_db_path=fixture["medical_path"],
        source_settings_db_path=fixture["settings_path"],
        network_baza_dir=fixture["network_baza"],
    )
    fixture["marker_path"] = marker_path
    fixture["service"] = service
    return fixture


def _write_current_merge_ready_marker(fixture: dict, marker_mode: str) -> str:
    from rem_card.app.emergency_restore_probe import write_merge_ready_marker
    from rem_card.app.emergency_validation import validate_medical_db_snapshot

    validation = validate_medical_db_snapshot(fixture["medical_path"])
    marker_path = write_merge_ready_marker(
        fixture["store"].resolve_root(),
        fixture["session"],
        remote_last_change_id=int(validation.last_change_id or 0),
        remote_fingerprint=dict(validation.fingerprint or {}),
        marker_mode=marker_mode,
    )
    fixture["marker_path"] = marker_path
    return marker_path


def _run_merge_dry_run_fixture(fixture: dict) -> object:
    service = fixture["service"]
    session = fixture["session"]
    marker_path = fixture.get("marker_path") or None
    return service.run_dry_run(session.emergency_session_id, marker_path)


def _read_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _file_hash(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_merge_dry_run_requires_merge_ready_marker(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_merge_dry_run_fixture(temp_root, marker=False, status="active")
    result = _run_merge_dry_run_fixture(fixture)
    if result.result_status != "blocked_marker_required":
        return False, f"missing marker did not block dry-run: {result.to_dict()}"
    return True, "ok"


def _check_merge_dry_run_rejects_expired_marker(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_merge_dry_run_fixture(temp_root)
    marker_path = fixture["marker_path"]
    payload = _read_json(marker_path)
    payload["requested_at"] = "2000-01-01T00:00:00+00:00"
    Path(marker_path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    result = _run_merge_dry_run_fixture(fixture)
    if result.result_status != "blocked_marker_invalid":
        return False, f"expired marker was accepted: {result.to_dict()}"
    return True, "ok"


def _check_merge_dry_run_validates_active_session(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_merge_dry_run_fixture(temp_root)
    metadata_path = os.path.join(
        fixture["store"].resolve_root(),
        "active",
        fixture["session"].emergency_session_id,
        "emergency_session.json",
    )
    Path(metadata_path).write_text("{not-json", encoding="utf-8")
    result = _run_merge_dry_run_fixture(fixture)
    if result.result_status != "blocked_local_invalid":
        return False, f"corrupt emergency_session.json did not block: {result.to_dict()}"
    if not os.path.isfile(result.report_path):
        return False, "dry-run did not write local report for corrupt session metadata"
    return True, "ok"


def _check_merge_dry_run_validates_base_snapshot_hash(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_merge_dry_run_fixture(temp_root)
    with open(fixture["session"].base_snapshot_path, "ab") as fh:
        fh.write(b"hash mismatch")
    result = _run_merge_dry_run_fixture(fixture)
    if result.result_status != "blocked_local_invalid":
        return False, f"base hash mismatch did not block: {result.to_dict()}"
    return True, "ok"


def _check_merge_dry_run_validates_local_emergency_db(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_merge_dry_run_fixture(temp_root)
    Path(fixture["session"].local_db_path).write_bytes(b"not sqlite")
    result = _run_merge_dry_run_fixture(fixture)
    if result.result_status != "blocked_local_invalid":
        return False, f"corrupt local emergency DB did not block: {result.to_dict()}"
    return True, "ok"


def _check_merge_dry_run_validates_settings_snapshot(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_merge_dry_run_fixture(temp_root)
    os.remove(str(fixture["session"].settings_snapshot_path))
    result = _run_merge_dry_run_fixture(fixture)
    if result.result_status != "blocked_local_invalid":
        return False, f"missing settings snapshot did not block: {result.to_dict()}"
    return True, "ok"


def _check_merge_dry_run_validates_remote_medical_and_settings_db(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_merge_dry_run_fixture(temp_root)
    os.remove(fixture["medical_path"])
    result = _run_merge_dry_run_fixture(fixture)
    if result.result_status != "blocked_remote_invalid":
        return False, f"missing remote medical DB did not block: {result.to_dict()}"
    fixture = _prepare_merge_dry_run_fixture(temp_root)
    Path(fixture["settings_path"]).write_bytes(b"not sqlite")
    result = _run_merge_dry_run_fixture(fixture)
    if result.result_status != "blocked_remote_invalid":
        return False, f"corrupt remote settings DB did not block: {result.to_dict()}"
    return True, "ok"


def _check_merge_dry_run_acquires_or_checks_remote_db_lock(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_merge_dry_run_fixture(temp_root)
    lock_path = os.path.join(fixture["network_baza"], "archiv", "db.lock")
    Path(lock_path).write_text("busy", encoding="utf-8")
    result = _run_merge_dry_run_fixture(fixture)
    if result.result_status != "blocked_locks":
        return False, f"active remote db.lock did not block: {result.to_dict()}"
    return True, "ok"


def _check_merge_dry_run_checks_emergency_merge_lock(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_merge_dry_run_fixture(temp_root)
    lock_path = os.path.join(fixture["network_baza"], "locks", "emergency_merge.lock")
    Path(lock_path).write_text("busy", encoding="utf-8")
    result = _run_merge_dry_run_fixture(fixture)
    if result.result_status != "blocked_locks":
        return False, f"active emergency_merge.lock did not block: {result.to_dict()}"
    return True, "ok"


def _check_merge_dry_run_checks_role_session_locks(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_merge_dry_run_fixture(temp_root)
    lock_path = os.path.join(fixture["network_baza"], "session_locks", "nurse.lock")
    Path(lock_path).write_text("busy", encoding="utf-8")
    result = _run_merge_dry_run_fixture(fixture)
    if result.result_status != "blocked_locks":
        return False, f"role/session lock did not block: {result.to_dict()}"
    return True, "ok"


def _check_merge_dry_run_remote_unchanged_ready_mode_a(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_merge_dry_run_fixture(temp_root)
    result = _run_merge_dry_run_fixture(fixture)
    if result.result_status != "ready_mode_a" or result.merge_mode != "remote_unchanged_mode_a":
        return False, f"remote unchanged was not ready Mode A: {result.to_dict()}"
    return True, "ok"


def _check_merge_dry_run_remote_changed_ready_authoritative(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _append_emergency_medical_change
    fixture = _prepare_merge_dry_run_fixture(temp_root, marker=False)
    before_hash = _file_hash(fixture["medical_path"])
    _append_emergency_medical_change(fixture["medical_path"], entity_id=707)
    _write_current_merge_ready_marker(fixture, "remote_changed_emergency_authoritative")
    changed_hash = _file_hash(fixture["medical_path"])
    result = _run_merge_dry_run_fixture(fixture)
    if result.result_status != "ready_emergency_authoritative" or result.merge_mode != "remote_changed_emergency_authoritative":
        return False, f"remote changed was not ready authoritative: {result.to_dict()}"
    if _file_hash(fixture["medical_path"]) != changed_hash or changed_hash == before_hash:
        return False, "remote changed dry-run unexpectedly modified remote DB"
    if not any("row-level merge will preserve remote-only rows" in item for item in result.warnings):
        return False, f"row-level preserve warning missing: {result.warnings}"
    if not any("local emergency rows win RemCard conflicts" in item for item in result.warnings):
        return False, f"authoritative warning missing: {result.warnings}"
    return True, "ok"


def _check_merge_dry_run_remote_less_than_base_blocked(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _append_emergency_medical_change
    from dataclasses import replace
    from rem_card.app.emergency_validation import compute_file_hash

    fixture = _prepare_merge_dry_run_fixture(temp_root)
    _append_emergency_medical_change(fixture["session"].base_snapshot_path, entity_id=1001)
    _append_emergency_medical_change(fixture["session"].local_db_path, entity_id=1001)
    session = replace(
        fixture["session"],
        base_last_change_id=1,
        base_snapshot_hash=compute_file_hash(fixture["session"].base_snapshot_path),
    )
    fixture["store"].write_active_session(session)
    fixture["session"] = session
    result = _run_merge_dry_run_fixture(fixture)
    if result.result_status != "blocked_marker_invalid":
        return False, f"marker should reject base mismatch before inconsistent remote: {result.to_dict()}"
    marker_path = fixture["marker_path"]
    payload = _read_json(marker_path)
    payload["base_last_change_id"] = 1
    payload["remote_last_change_id"] = 1
    Path(marker_path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    result = _run_merge_dry_run_fixture(fixture)
    if result.result_status != "blocked_inconsistent":
        return False, f"remote < base did not block as inconsistent: {result.to_dict()}"
    return True, "ok"


def _check_merge_dry_run_never_writes_remote_db(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_merge_dry_run_fixture(temp_root)
    before_medical = _file_hash(fixture["medical_path"])
    before_settings = _file_hash(fixture["settings_path"])
    result = _run_merge_dry_run_fixture(fixture)
    if result.result_status != "ready_mode_a":
        return False, f"fixture did not reach ready Mode A: {result.to_dict()}"
    if _file_hash(fixture["medical_path"]) != before_medical:
        return False, "dry-run changed remote medical DB"
    if _file_hash(fixture["settings_path"]) != before_settings:
        return False, "dry-run changed remote settings DB"
    return True, "ok"


def _check_merge_dry_run_no_remote_replacement_code(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    text = (PROJECT_ROOT / "app" / "emergency_merge_dry_run.py").read_text(encoding="utf-8").lower()
    forbidden = ("os.replace", "shutil.copy", "copy2(", "restore_database(", "backup_connection(")
    for token in forbidden:
        if token in text:
            return False, f"dry-run module contains remote replacement/backup token: {token}"
    return True, "ok"


def _check_merge_dry_run_writes_local_report(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_merge_dry_run_fixture(temp_root)
    result = _run_merge_dry_run_fixture(fixture)
    if not os.path.isfile(result.report_path):
        return False, f"report was not written: {result.report_path}"
    report = _read_json(result.report_path)
    if report.get("emergency_session_id") != fixture["session"].emergency_session_id:
        return False, f"report session mismatch: {report}"
    return True, "ok"


def _check_merge_dry_run_updates_local_session_metadata_only(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_merge_dry_run_fixture(temp_root)
    before_medical = _file_hash(fixture["medical_path"])
    before_settings = _file_hash(fixture["settings_path"])
    result = _run_merge_dry_run_fixture(fixture)
    loaded = fixture["store"].read_active_session(fixture["session"].emergency_session_id)
    if loaded.last_dry_run_status != result.result_status or not loaded.last_dry_run_report_path:
        return False, f"session dry-run metadata was not updated: {loaded}"
    if _file_hash(fixture["medical_path"]) != before_medical or _file_hash(fixture["settings_path"]) != before_settings:
        return False, "dry-run metadata update touched remote DB"
    return True, "ok"


def _check_merge_dry_run_change_summary_counts_tables(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _append_emergency_medical_change
    fixture = _prepare_merge_dry_run_fixture(temp_root)
    _append_emergency_medical_change(fixture["session"].local_db_path, entity_id=808)
    result = _run_merge_dry_run_fixture(fixture)
    summary = result.change_summary.changed_tables_summary
    if not summary or "regression" not in summary:
        return False, f"change summary missing table counts: {result.to_dict()}"
    if int(result.change_summary.emergency_change_count or 0) <= 0:
        return False, f"emergency change count not positive: {result.change_summary}"
    return True, "ok"


def _check_merge_dry_run_backup_readiness_blocker(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_merge_dry_run_fixture(temp_root)
    backup_dir = os.path.join(fixture["network_baza"], "backups", "valid")
    os.makedirs(os.path.dirname(backup_dir), exist_ok=True)
    if os.path.isdir(backup_dir):
        shutil.rmtree(backup_dir)
    Path(backup_dir).write_text("not a directory", encoding="utf-8")
    result = _run_merge_dry_run_fixture(fixture)
    if result.result_status != "blocked_backup_not_ready":
        return False, f"backup readiness failure did not block: {result.to_dict()}"
    return True, "ok"


def _check_merge_dry_run_no_recovery_on_unavailable(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    text = (PROJECT_ROOT / "app" / "emergency_merge_dry_run.py").read_text(encoding="utf-8")
    forbidden = ("recover_shared_db", "recover_shared_db_with_locks", "_recover_shared_db", "run_startup_db_guard")
    for token in forbidden:
        if token in text:
            return False, f"dry-run references recovery: {token}"
    return True, "ok"


def _check_merge_dry_run_no_sqlite_profile_changes(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _check_no_sqlite_safety_changes
    return _check_no_sqlite_safety_changes(temp_root)


def _check_merge_dry_run_no_doctor_emergency_path(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_merge_dry_run_fixture(temp_root, role="doctor")
    result = _run_merge_dry_run_fixture(fixture)
    if result.result_status != "blocked_role_not_allowed":
        return False, f"doctor dry-run was not blocked: {result.to_dict()}"
    return True, "ok"


def _check_merge_dry_run_remote_changed_authoritative_no_remote_write(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _append_emergency_medical_change
    fixture = _prepare_merge_dry_run_fixture(temp_root, marker=False)
    _append_emergency_medical_change(fixture["session"].local_db_path, entity_id=909)
    _append_emergency_medical_change(fixture["medical_path"], entity_id=910)
    _write_current_merge_ready_marker(fixture, "remote_changed_emergency_authoritative")
    before_remote = _file_hash(fixture["medical_path"])
    result = _run_merge_dry_run_fixture(fixture)
    if result.result_status != "ready_emergency_authoritative":
        return False, f"remote changed did not become authoritative dry-run: {result.to_dict()}"
    if _file_hash(fixture["medical_path"]) != before_remote:
        return False, "authoritative dry-run applied emergency rows to remote DB"
    return True, "ok"


def _check_merge_dry_run_preserves_emergency_db_on_failure(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_merge_dry_run_fixture(temp_root)
    before_local = _file_hash(fixture["session"].local_db_path)
    os.remove(fixture["medical_path"])
    result = _run_merge_dry_run_fixture(fixture)
    if result.result_status != "blocked_remote_invalid":
        return False, f"fixture did not fail on missing remote: {result.to_dict()}"
    if _file_hash(fixture["session"].local_db_path) != before_local:
        return False, "failed dry-run modified local emergency DB"
    return True, "ok"


def _check_merge_dry_run_status_ready_does_not_switch_network(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_merge_dry_run_fixture(temp_root)
    result = _run_merge_dry_run_fixture(fixture)
    loaded = fixture["store"].read_active_session(fixture["session"].emergency_session_id)
    if result.result_status != "ready_mode_a":
        return False, f"fixture did not reach ready status: {result.to_dict()}"
    if loaded.status == "merged":
        return False, "ready dry-run marked session merged"
    text = (PROJECT_ROOT / "app" / "emergency_merge_dry_run.py").read_text(encoding="utf-8")
    for token in ("bootstrap(", "launch_emergency_restart", "runtime_context=build_network"):
        if token in text:
            return False, f"dry-run contains network switch token: {token}"
    return True, "ok"


def _prepare_mode_a_merge_fixture(
    temp_root: str,
    *,
    marker: bool = True,
    dry_run: bool = True,
    role: str = "nurse",
    local_change: bool = True,
    before_replace_hook=None,
    after_temp_created_hook=None,
):
    from .emergency_standby import _append_emergency_medical_change
    from rem_card.app.emergency_merge_mode_a import EmergencyModeAMergeService

    fixture = _prepare_merge_dry_run_fixture(temp_root, marker=marker, status="merge_pending")
    if local_change:
        _append_emergency_medical_change(fixture["session"].local_db_path, entity_id=2000 + (uuid.uuid4().int % 1000))
    dry_run_result = _run_merge_dry_run_fixture(fixture) if dry_run else None
    service = EmergencyModeAMergeService(
        role=role,
        runtime_context=fixture["store"].build_active_runtime_context(fixture["session"].emergency_session_id),
        store=fixture["store"],
        source_medical_db_path=fixture["medical_path"],
        source_settings_db_path=fixture["settings_path"],
        network_baza_dir=fixture["network_baza"],
        before_replace_hook=before_replace_hook,
        after_temp_created_hook=after_temp_created_hook,
    )
    fixture["mode_a_service"] = service
    fixture["dry_run_result"] = dry_run_result
    return fixture


def _run_mode_a_merge_fixture(fixture: dict):
    service = fixture["mode_a_service"]
    session = fixture["session"]
    dry_run_result = fixture.get("dry_run_result")
    return service.run_merge(
        session.emergency_session_id,
        None if dry_run_result is None else dry_run_result.report_path,
        fixture.get("marker_path") or None,
    )


def _archived_session_metadata(result) -> dict:
    return _read_json(os.path.join(result.archive_path, "emergency_session.json"))


def _check_mode_a_merge_requires_dry_run_ready_report(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_mode_a_merge_fixture(temp_root, dry_run=False)
    result = _run_mode_a_merge_fixture(fixture)
    if result.result_status != "blocked" or result.error_code != "dry_run_ready_report_required":
        return False, f"missing dry-run report did not block: {result.to_dict()}"
    if _file_hash(fixture["medical_path"]) != _file_hash(fixture["session"].base_snapshot_path):
        return False, "remote changed when dry-run report was missing"
    return True, "ok"


def _check_mode_a_merge_requires_merge_ready_marker(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_mode_a_merge_fixture(temp_root)
    os.remove(fixture["marker_path"])
    result = _run_mode_a_merge_fixture(fixture)
    if result.result_status != "blocked" or result.error_code != "merge_ready_marker_required":
        return False, f"missing marker did not block: {result.to_dict()}"
    return True, "ok"


def _check_mode_a_merge_replaces_remote_when_remote_changed(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _append_emergency_medical_change, _count_emergency_medical_change
    fixture = _prepare_mode_a_merge_fixture(temp_root)
    _append_emergency_medical_change(fixture["medical_path"], entity_id=3001)
    before_hash = _file_hash(fixture["medical_path"])
    result = _run_mode_a_merge_fixture(fixture)
    if result.result_status != "success":
        return False, f"remote changed authoritative merge failed: {result.to_dict()}"
    if _file_hash(fixture["medical_path"]) == before_hash:
        return False, "remote changed DB was not replaced by emergency DB"
    if _count_emergency_medical_change(fixture["medical_path"], 3001) != 0:
        return False, "remote-only change survived authoritative replacement"
    if int(result.remote_last_change_id_after or 0) != int(result.local_last_change_id or 0):
        return False, f"authoritative merge last_change mismatch: {result.to_dict()}"
    if not os.path.isfile(result.remote_backup_path) or not os.path.isfile(result.local_backup_path):
        return False, f"authoritative merge backups missing: {result.to_dict()}"
    if not any("remote medical DB changed after emergency base" in item for item in result.warnings):
        return False, f"authoritative merge warning missing: {result.warnings}"
    return True, "ok"


def _check_mode_a_merge_rejects_remote_inconsistent(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _append_emergency_medical_change, _create_valid_emergency_medical_db
    from dataclasses import replace
    from rem_card.app.emergency_restore_probe import write_merge_ready_marker
    from rem_card.app.emergency_validation import compute_file_hash, validate_medical_db_snapshot

    fixture = _prepare_merge_dry_run_fixture(temp_root, marker=False, status="merge_pending")
    _append_emergency_medical_change(fixture["session"].base_snapshot_path, entity_id=3101)
    _append_emergency_medical_change(fixture["session"].local_db_path, entity_id=3101)
    _append_emergency_medical_change(fixture["medical_path"], entity_id=3101)
    remote_validation = validate_medical_db_snapshot(fixture["medical_path"])
    session = replace(
        fixture["session"],
        base_last_change_id=1,
        base_snapshot_hash=compute_file_hash(fixture["session"].base_snapshot_path),
        base_remote_fingerprint=dict(remote_validation.fingerprint),
    )
    fixture["store"].write_active_session(session)
    fixture["session"] = session
    fixture["marker_path"] = write_merge_ready_marker(
        fixture["store"].resolve_root(),
        session,
        remote_last_change_id=1,
        remote_fingerprint=dict(remote_validation.fingerprint),
    )
    fixture["service"] = fixture["service"].__class__(
        role="nurse",
        runtime_context=fixture["store"].build_active_runtime_context(session.emergency_session_id),
        store=fixture["store"],
        source_medical_db_path=fixture["medical_path"],
        source_settings_db_path=fixture["settings_path"],
        network_baza_dir=fixture["network_baza"],
    )
    dry_run_result = _run_merge_dry_run_fixture(fixture)
    if dry_run_result.result_status != "ready_mode_a":
        return False, f"inconsistent fixture did not start ready: {dry_run_result.to_dict()}"
    lower_remote = os.path.join(os.path.dirname(fixture["medical_path"]), "remote_lower.db")
    _create_valid_emergency_medical_db(lower_remote)
    os.replace(lower_remote, fixture["medical_path"])
    mode_fixture = _prepare_mode_a_merge_fixture(temp_root, marker=True, dry_run=True)
    mode_fixture.update(fixture)
    from rem_card.app.emergency_merge_mode_a import EmergencyModeAMergeService
    mode_fixture["dry_run_result"] = dry_run_result
    mode_fixture["mode_a_service"] = EmergencyModeAMergeService(
        role="nurse",
        runtime_context=fixture["store"].build_active_runtime_context(session.emergency_session_id),
        store=fixture["store"],
        source_medical_db_path=fixture["medical_path"],
        source_settings_db_path=fixture["settings_path"],
        network_baza_dir=fixture["network_baza"],
    )
    result = _run_mode_a_merge_fixture(mode_fixture)
    if result.result_status != "blocked" or result.error_code != "blocked_remote_inconsistent":
        return False, f"remote < base was not blocked: {result.to_dict()}"
    return True, "ok"


def _check_mode_a_merge_requires_base_snapshot_hash_match(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_mode_a_merge_fixture(temp_root)
    with open(fixture["session"].base_snapshot_path, "ab") as fh:
        fh.write(b"hash mismatch after dry-run")
    result = _run_mode_a_merge_fixture(fixture)
    if result.result_status != "blocked" or result.error_code != "local_invalid":
        return False, f"base hash mismatch did not block merge: {result.to_dict()}"
    return True, "ok"


def _check_mode_a_merge_requires_local_emergency_quick_check(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_mode_a_merge_fixture(temp_root)
    before_remote = _file_hash(fixture["medical_path"])
    Path(fixture["session"].local_db_path).write_bytes(b"not sqlite")
    result = _run_mode_a_merge_fixture(fixture)
    if result.result_status != "blocked" or result.error_code != "local_invalid":
        return False, f"corrupt local DB did not block merge: {result.to_dict()}"
    if _file_hash(fixture["medical_path"]) != before_remote:
        return False, "corrupt local DB path overwrote remote"
    return True, "ok"


def _check_mode_a_merge_requires_remote_quick_check(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_mode_a_merge_fixture(temp_root)
    Path(fixture["medical_path"]).write_bytes(b"not sqlite")
    before_remote = _file_hash(fixture["medical_path"])
    result = _run_mode_a_merge_fixture(fixture)
    if result.result_status != "blocked" or result.error_code != "remote_invalid":
        return False, f"corrupt remote DB did not block merge: {result.to_dict()}"
    if _file_hash(fixture["medical_path"]) != before_remote:
        return False, "corrupt remote DB was overwritten"
    return True, "ok"


def _check_mode_a_merge_requires_settings_validation(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_mode_a_merge_fixture(temp_root)
    Path(fixture["settings_path"]).write_bytes(b"not sqlite")
    before_settings = _file_hash(fixture["settings_path"])
    result = _run_mode_a_merge_fixture(fixture)
    if result.result_status != "blocked" or result.error_code != "remote_invalid":
        return False, f"corrupt remote settings DB did not block merge: {result.to_dict()}"
    if _file_hash(fixture["settings_path"]) != before_settings:
        return False, "merge wrote remote settings DB"
    return True, "ok"


def _check_mode_a_merge_requires_no_role_session_locks(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_mode_a_merge_fixture(temp_root)
    lock_path = os.path.join(fixture["network_baza"], "session_locks", "doctor.lock")
    Path(lock_path).write_text("busy", encoding="utf-8")
    result = _run_mode_a_merge_fixture(fixture)
    if result.result_status != "blocked" or result.error_code != "active_session_lock":
        return False, f"role/session lock did not block merge: {result.to_dict()}"
    if os.path.exists(os.path.join(fixture["network_baza"], "archiv", "db.lock")):
        return False, "remote db.lock was not released after session lock blocker"
    return True, "ok"


def _check_mode_a_merge_requires_remote_db_lock(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_mode_a_merge_fixture(temp_root)
    lock_path = os.path.join(fixture["network_baza"], "archiv", "db.lock")
    Path(lock_path).write_text("busy", encoding="utf-8")
    result = _run_mode_a_merge_fixture(fixture)
    if result.result_status != "blocked" or result.error_code != "locks_unavailable":
        return False, f"remote db.lock did not block merge: {result.to_dict()}"
    return True, "ok"


def _check_mode_a_merge_requires_emergency_merge_lock(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_mode_a_merge_fixture(temp_root)
    lock_path = os.path.join(fixture["network_baza"], "locks", "emergency_merge.lock")
    Path(lock_path).write_text("busy", encoding="utf-8")
    result = _run_mode_a_merge_fixture(fixture)
    if result.result_status != "blocked" or result.error_code != "locks_unavailable":
        return False, f"emergency_merge.lock did not block merge: {result.to_dict()}"
    return True, "ok"


def _check_mode_a_merge_creates_remote_pre_merge_backup(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_validation import validate_medical_db_snapshot

    fixture = _prepare_mode_a_merge_fixture(temp_root)
    result = _run_mode_a_merge_fixture(fixture)
    validation = validate_medical_db_snapshot(result.remote_backup_path)
    if result.result_status != "success" or not validation.ok:
        return False, f"remote backup invalid/missing: {result.to_dict()} {validation}"
    return True, "ok"


def _check_mode_a_merge_creates_local_emergency_backup(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_validation import validate_medical_db_snapshot

    fixture = _prepare_mode_a_merge_fixture(temp_root)
    result = _run_mode_a_merge_fixture(fixture)
    validation = validate_medical_db_snapshot(result.local_backup_path)
    if result.result_status != "success" or not validation.ok:
        return False, f"local emergency backup invalid/missing: {result.to_dict()} {validation}"
    return True, "ok"


def _check_mode_a_merge_creates_temp_db_from_local_via_sqlite_backup(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_validation import validate_medical_db_snapshot

    fixture = _prepare_mode_a_merge_fixture(temp_root)
    local_last = validate_medical_db_snapshot(fixture["session"].local_db_path).last_change_id
    result = _run_mode_a_merge_fixture(fixture)
    remote_last = validate_medical_db_snapshot(fixture["medical_path"]).last_change_id
    text = (PROJECT_ROOT / "app" / "emergency_merge_mode_a.py").read_text(encoding="utf-8")
    if "backup_connection(" not in text or "temp_from_local_backup_api" not in text:
        return False, "Mode A temp DB must be created through SQLite Backup API"
    if "copy2(" in text or "copyfile(" in text:
        return False, "Mode A module must not use plain file copy for DB merge"
    if result.result_status != "success" or int(remote_last or 0) != int(local_last or 0):
        return False, f"temp/local merge result mismatch: {result.to_dict()}"
    return True, "ok"


def _check_mode_a_merge_validates_temp_before_replace(temp_root: str) -> tuple[bool, str]:
    def corrupt_temp(path: str) -> None:
        Path(path).write_bytes(b"not sqlite")

    fixture = _prepare_mode_a_merge_fixture(temp_root, after_temp_created_hook=corrupt_temp)
    before_remote = _file_hash(fixture["medical_path"])
    result = _run_mode_a_merge_fixture(fixture)
    if result.result_status != "failed" or "temp DB validation failed" not in str(result.error):
        return False, f"invalid temp did not fail before replacement: {result.to_dict()}"
    if _file_hash(fixture["medical_path"]) != before_remote:
        return False, "invalid temp changed remote DB"
    return True, "ok"


def _check_mode_a_merge_rechecks_remote_last_change_before_replace(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _append_emergency_medical_change, _count_emergency_medical_change
    holder: dict[str, str] = {}

    def change_remote() -> None:
        _append_emergency_medical_change(holder["medical_path"], entity_id=3201)

    fixture = _prepare_mode_a_merge_fixture(temp_root, before_replace_hook=change_remote)
    holder["medical_path"] = fixture["medical_path"]
    before_hash = _file_hash(fixture["medical_path"])
    result = _run_mode_a_merge_fixture(fixture)
    after_hash = _file_hash(fixture["medical_path"])
    if result.result_status != "success":
        return False, f"remote last_change recheck did not allow authoritative merge: {result.to_dict()}"
    if _count_emergency_medical_change(fixture["medical_path"], 3201) != 0:
        return False, "late remote-only change survived authoritative replacement"
    if int(result.remote_last_change_id_after or 0) != int(result.local_last_change_id or 0):
        return False, f"authoritative merge last_change mismatch after late remote change: {result.to_dict()}"
    if after_hash == before_hash:
        return False, "test hook did not exercise remote replacement"
    return True, "ok"


def _check_mode_a_merge_replaces_remote_only_after_backups_and_locks(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_mode_a_merge_fixture(temp_root)
    result = _run_mode_a_merge_fixture(fixture)
    if result.result_status != "success":
        return False, f"fixture did not merge: {result.to_dict()}"
    if not result.locks_acquired.get("ok"):
        return False, f"locks were not recorded as acquired: {result.locks_acquired}"
    for path in (result.remote_backup_path, result.local_backup_path):
        if not os.path.isfile(path):
            return False, f"backup missing before replacement success: {path}"
    return True, "ok"


def _check_mode_a_merge_final_remote_matches_local_last_change_id(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_validation import validate_medical_db_snapshot

    fixture = _prepare_mode_a_merge_fixture(temp_root)
    local_last = validate_medical_db_snapshot(fixture["session"].local_db_path).last_change_id
    result = _run_mode_a_merge_fixture(fixture)
    remote_last = validate_medical_db_snapshot(fixture["medical_path"]).last_change_id
    if result.result_status != "success" or int(remote_last or 0) != int(local_last or 0):
        return False, f"final remote last_change_id mismatch: local={local_last} remote={remote_last} result={result.to_dict()}"
    return True, "ok"


def _check_mode_a_merge_post_quick_check_required(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_mode_a_merge_fixture(temp_root)
    result = _run_mode_a_merge_fixture(fixture)
    if result.result_status != "success" or result.post_quick_check_status != "ok":
        return False, f"post quick_check status missing: {result.to_dict()}"
    return True, "ok"


def _check_mode_a_merge_rollback_restores_remote_on_final_validation_failure(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_mode_a_merge_fixture(temp_root)
    before_remote = _file_hash(fixture["medical_path"])
    fixture["mode_a_service"].validate_final_remote_db = lambda *args, **kwargs: {
        "ok": False,
        "reason": "forced final validation failure",
    }
    result = _run_mode_a_merge_fixture(fixture)
    if result.result_status != "rolled_back" or result.rollback_status != "restored":
        return False, f"final validation failure did not rollback: {result.to_dict()}"
    if _file_hash(fixture["medical_path"]) != before_remote:
        return False, "rollback did not restore original remote DB"
    if not os.path.isfile(fixture["session"].local_db_path):
        return False, "rollback deleted local emergency DB"
    return True, "ok"


def _check_mode_a_merge_marks_session_merged_on_success(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_mode_a_merge_fixture(temp_root)
    result = _run_mode_a_merge_fixture(fixture)
    metadata = _archived_session_metadata(result)
    if metadata.get("status") != "merged" or metadata.get("merge_result") != "success":
        return False, f"merged session metadata mismatch: {metadata}"
    return True, "ok"


def _check_mode_a_merge_marks_session_merge_failed_on_failure(temp_root: str) -> tuple[bool, str]:
    def corrupt_temp(path: str) -> None:
        Path(path).write_bytes(b"not sqlite")

    fixture = _prepare_mode_a_merge_fixture(temp_root, after_temp_created_hook=corrupt_temp)
    result = _run_mode_a_merge_fixture(fixture)
    loaded = fixture["store"].read_active_session(fixture["session"].emergency_session_id)
    if result.result_status != "failed" or loaded.status != "merge_failed":
        return False, f"failed merge did not mark session merge_failed: {result.to_dict()} {loaded}"
    if not os.path.isfile(fixture["session"].local_db_path):
        return False, "failed merge deleted local emergency DB"
    return True, "ok"


def _check_mode_a_merge_clears_merge_ready_marker_on_success(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_mode_a_merge_fixture(temp_root)
    result = _run_mode_a_merge_fixture(fixture)
    if result.result_status != "success":
        return False, f"fixture did not merge: {result.to_dict()}"
    if os.path.exists(fixture["marker_path"]):
        return False, f"merge-ready marker still exists: {fixture['marker_path']}"
    return True, "ok"


def _check_mode_a_merge_does_not_delete_emergency_db(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_mode_a_merge_fixture(temp_root)
    result = _run_mode_a_merge_fixture(fixture)
    archived_db = os.path.join(result.archive_path, "rao_journal_emergency.db")
    if result.result_status != "success" or not os.path.isfile(archived_db):
        return False, f"archived emergency DB missing: {result.to_dict()}"
    return True, "ok"


def _check_mode_a_merge_next_startup_network_not_emergency(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_mode_a_merge_fixture(temp_root)
    result = _run_mode_a_merge_fixture(fixture)
    active_dir_path = os.path.join(fixture["store"].resolve_root(), "active", fixture["session"].emergency_session_id)
    if result.result_status != "success" or os.path.exists(active_dir_path):
        return False, f"merged session should not remain active for emergency resume: {result.to_dict()}"
    return True, "ok"


def _check_mode_a_merge_does_not_write_remote_settings_db(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_mode_a_merge_fixture(temp_root)
    before_settings = _file_hash(fixture["settings_path"])
    result = _run_mode_a_merge_fixture(fixture)
    if result.result_status != "success":
        return False, f"fixture did not merge: {result.to_dict()}"
    if _file_hash(fixture["settings_path"]) != before_settings:
        return False, "Mode A merge modified remote settings DB"
    return True, "ok"


def _check_mode_a_merge_remote_changed_authoritative_merge(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _append_emergency_medical_change, _count_emergency_medical_change
    fixture = _prepare_mode_a_merge_fixture(temp_root)
    _append_emergency_medical_change(fixture["medical_path"], entity_id=3301)
    before_remote = _file_hash(fixture["medical_path"])
    before_settings = _file_hash(fixture["settings_path"])
    result = _run_mode_a_merge_fixture(fixture)
    if result.result_status != "success":
        return False, f"remote changed authoritative merge failed: {result.to_dict()}"
    if _file_hash(fixture["medical_path"]) == before_remote:
        return False, "remote changed authoritative merge did not apply local emergency DB"
    if _count_emergency_medical_change(fixture["medical_path"], 3301) != 0:
        return False, "remote-only change survived authoritative merge"
    if int(result.remote_last_change_id_after or 0) != int(result.local_last_change_id or 0):
        return False, f"authoritative merge last_change mismatch: {result.to_dict()}"
    if _file_hash(fixture["settings_path"]) != before_settings:
        return False, "authoritative merge modified remote settings DB"
    return True, "ok"


def _check_mode_a_merge_no_change_log_replay(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    text = (PROJECT_ROOT / "app" / "emergency_merge_mode_a.py").read_text(encoding="utf-8").lower()
    forbidden = ("from change_log", "insert into", "update ", "delete from")
    for token in forbidden:
        if token in text:
            return False, f"Mode A merge module contains row-level replay/write token: {token}"
    return True, "ok"


def _check_mode_a_merge_no_sqlite_profile_changes(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _check_no_sqlite_safety_changes
    return _check_no_sqlite_safety_changes(temp_root)


def _check_mode_a_merge_no_doctor_path(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_mode_a_merge_fixture(temp_root, role="doctor")
    result = _run_mode_a_merge_fixture(fixture)
    if result.result_status != "blocked" or result.error_code != "role_not_allowed":
        return False, f"doctor triggered Mode A merge: {result.to_dict()}"
    return True, "ok"


def _check_pending_emergency_merge_defaults_to_row_level(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    text = (PROJECT_ROOT / "app" / "emergency_pending_merge.py").read_text(encoding="utf-8")
    required = (
        "ROW_LEVEL_MERGE_STRATEGY",
        "EmergencyRowLevelMergeService",
        "legacy_file_replacement_manual_fallback",
    )
    missing = [token for token in required if token not in text]
    if missing:
        return False, f"pending merge default row-level tokens missing: {missing}"
    if "strategy = str(os.environ.get(\"REMCARD_EMERGENCY_MERGE_STRATEGY\") or ROW_LEVEL_MERGE_STRATEGY)" not in text:
        return False, "pending merge does not default to row-level strategy"
    return True, "ok"


def _check_recovery_bed_transfer_order_10_11_12(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from rem_card.services.patient_bed_management.recovery_beds import RECOVERY_BED_TRANSFER_ORDER

    if tuple(RECOVERY_BED_TRANSFER_ORDER) != (10, 11, 12):
        return False, f"wrong recovery bed order: {RECOVERY_BED_TRANSFER_ORDER}"
    return True, "ok"


def _check_mode_a_merge_report_written(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_mode_a_merge_fixture(temp_root)
    result = _run_mode_a_merge_fixture(fixture)
    if not os.path.isfile(result.report_path):
        return False, f"merge report was not written: {result.report_path}"
    report = _read_json(result.report_path)
    if report.get("result_status") != "success" or report.get("mode") != "emergency_authoritative_replacement":
        return False, f"merge report contents mismatch: {report}"
    return True, "ok"


def _check_mode_a_merge_preserves_reports_and_backups_in_archive(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_mode_a_merge_fixture(temp_root)
    result = _run_mode_a_merge_fixture(fixture)
    expected = [
        os.path.join(result.archive_path, "base_snapshot.db"),
        os.path.join(result.archive_path, "rao_journal_emergency.db"),
        os.path.join(result.archive_path, "remcard_settings_snapshot.db"),
        os.path.join(result.archive_path, "emergency_session.json"),
        result.report_path,
        result.local_backup_path,
    ]
    missing = [path for path in expected if not os.path.isfile(path)]
    if result.result_status != "success" or missing:
        return False, f"archive did not preserve expected files: missing={missing} result={result.to_dict()}"
    return True, "ok"


def _emergency_acceptance_runner_path() -> Path:
    return PROJECT_ROOT / "scripts" / "emergency_db_acceptance_runner.py"


def _emergency_acceptance_runner_text() -> str:
    path = _emergency_acceptance_runner_path()
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _check_emergency_acceptance_runner_exists(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    path = _emergency_acceptance_runner_path()
    if not path.is_file():
        return False, f"runner missing: {path}"
    return True, "ok"


def _check_emergency_acceptance_runner_has_full_mode_a_scenario(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    text = _emergency_acceptance_runner_text()
    required = ("scenario_full_mode_a_path", "ready_mode_a", "EmergencyModeAMergeService", "find_resumable_active_session")
    missing = [token for token in required if token not in text]
    if missing:
        return False, f"full Mode A scenario tokens missing: {missing}"
    return True, "ok"


def _check_pending_emergency_merge_runner_runs_merge_pending_session(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _append_real_emergency_vital_change
    from rem_card.app.emergency_pending_merge import find_pending_emergency_merge_session, run_pending_emergency_merge
    from rem_card.app.emergency_validation import validate_medical_db_snapshot

    fixture = _prepare_restore_probe_fixture(temp_root, success_rounds_required=1)
    local_change = _append_real_emergency_vital_change(fixture["session"].local_db_path)
    local_last = int(validate_medical_db_snapshot(fixture["session"].local_db_path).last_change_id or 0)
    before_settings = _file_hash(fixture["settings_path"])
    fixture["probe"].run_probe_once()
    marker_path = fixture["probe"].mark_merge_ready()
    if not os.path.isfile(marker_path):
        return False, f"merge-ready marker was not written: {marker_path}"
    pending_id = find_pending_emergency_merge_session(fixture["store"])
    if pending_id != fixture["session"].emergency_session_id:
        return False, f"pending session was not found: {pending_id}"
    result = run_pending_emergency_merge(
        root=fixture["store"].resolve_root(),
        source_medical_db_path=fixture["medical_path"],
        source_settings_db_path=fixture["settings_path"],
        network_baza_dir=fixture["network_baza"],
    )
    remote_last = validate_medical_db_snapshot(fixture["medical_path"]).last_change_id
    if not result.attempted or not result.ok:
        return False, f"pending merge did not complete: {result}"
    if int(remote_last or 0) <= int(fixture["session"].base_last_change_id or 0):
        return False, f"pending merge did not advance remote change id: local={local_last} remote={remote_last}"
    conn = sqlite3.connect(fixture["medical_path"])
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM vitals WHERE last_modified_by = ? AND sys = ?",
            ("regression_pending", 120),
        ).fetchone()
    finally:
        conn.close()
    if int(row[0] or 0) != 1:
        return False, f"pending merge did not apply local vital row: {local_change}"
    if _file_hash(fixture["settings_path"]) != before_settings:
        return False, "pending merge modified remote settings DB"
    own_active_dir = os.path.join(
        fixture["store"].resolve_root(),
        "active",
        fixture["session"].emergency_session_id,
    )
    if os.path.exists(own_active_dir):
        return False, "merged pending session remained active after successful merge"
    if not os.path.isfile(result.dry_run_report_path):
        return False, f"pending dry-run report missing: {result.dry_run_report_path}"
    if not os.path.isfile(result.merge_report_path):
        return False, f"pending merge report missing: {result.merge_report_path}"
    return True, "ok"


def _check_pending_merge_handles_merge_failed_explicitly(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.emergency_pending_merge import find_pending_emergency_merge_candidate, run_pending_emergency_merge

    fixture = _prepare_restore_probe_fixture(temp_root, success_rounds_required=1)
    session_id = fixture["session"].emergency_session_id
    fixture["store"].mark_session_status(session_id, "merge_failed", "simulated previous failure")
    candidate = find_pending_emergency_merge_candidate(fixture["store"])
    if candidate.session_id != session_id or candidate.status != "merge_failed":
        return False, f"merge_failed candidate was not found explicitly: {candidate}"
    result = run_pending_emergency_merge(
        root=fixture["store"].resolve_root(),
        source_medical_db_path=fixture["medical_path"],
        source_settings_db_path=fixture["settings_path"],
        network_baza_dir=fixture["network_baza"],
    )
    if result.ok or result.attempted or result.error != "merge_failed_unresolved":
        return False, f"merge_failed pending result was not controlled: {result}"
    return True, "ok"


def _check_merge_failed_does_not_silently_block_standby_forever(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _prepare_emergency_standby_manager_fixture
    from rem_card.app.emergency_standby_scheduler import EmergencyStandbyScheduler

    manager, _source_medical, _source_settings = _prepare_emergency_standby_manager_fixture(temp_root)
    standby = manager.create_or_refresh_standby(forced=True)
    if not standby.ok or not standby.metadata:
        return False, f"standby refresh failed: {standby}"
    session = manager.store.create_active_session_from_standby(standby.metadata, session_id="failed_session")
    manager.store.mark_session_status(session.emergency_session_id, "merge_failed", "simulated previous failure")
    scheduler = EmergencyStandbyScheduler(role="nurse", mode="network", manager=manager, cooldown_sec=0)
    reason = scheduler._refresh_block_reason()
    if reason != "merge_failed_unresolved":
        return False, f"merge_failed block reason is not explicit: {reason}"
    return True, "ok"


def _check_merged_or_discarded_session_does_not_block_standby_refresh(temp_root: str) -> tuple[bool, str]:
    from .emergency_standby import _prepare_emergency_standby_manager_fixture
    from rem_card.app.emergency_standby_scheduler import EmergencyStandbyScheduler

    manager, _source_medical, _source_settings = _prepare_emergency_standby_manager_fixture(temp_root)
    standby = manager.create_or_refresh_standby(forced=True)
    if not standby.ok or not standby.metadata:
        return False, f"standby refresh failed: {standby}"
    merged = manager.store.create_active_session_from_standby(standby.metadata, session_id="merged_session_for_scheduler")
    manager.store.mark_session_status(merged.emergency_session_id, "merged")
    scheduler = EmergencyStandbyScheduler(role="nurse", mode="network", manager=manager, cooldown_sec=0)
    reason = scheduler._refresh_block_reason()
    if reason in {"active_emergency_session", "merge_failed_unresolved"}:
        return False, f"merged session blocked standby refresh: {reason}"
    discarded = manager.store.create_active_session_from_standby(standby.metadata, session_id="discarded_session_for_scheduler")
    manager.store.mark_session_discarded(discarded.emergency_session_id, reason="regression", requested_by_role="nurse")
    reason = scheduler._refresh_block_reason()
    if reason in {"active_emergency_session", "merge_failed_unresolved"}:
        return False, f"discarded session blocked standby refresh: {reason}"
    return True, "ok"


def _check_merge_failed_preserves_local_emergency_db(temp_root: str) -> tuple[bool, str]:
    fixture = _prepare_restore_probe_fixture(temp_root, success_rounds_required=1)
    session_id = fixture["session"].emergency_session_id
    local_db_path = fixture["session"].local_db_path
    fixture["store"].mark_session_status(session_id, "merge_failed", "simulated previous failure")
    loaded = fixture["store"].read_active_session(session_id)
    if loaded.status != "merge_failed" or not os.path.isfile(local_db_path):
        return False, f"merge_failed did not preserve local DB: {loaded}"
    return True, "ok"
