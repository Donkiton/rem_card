#!/usr/bin/env python
"""
Emergency DB acceptance runner for release smoke gates.

The runner uses only temporary fixture databases and explicit paths. It does not
touch the production Baza directory, does not require SMB, and does not add merge
logic. All merge behavior is delegated to the existing emergency services.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import traceback
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
try:
    from _local_rem_card_bootstrap import bootstrap_local_rem_card

    bootstrap_local_rem_card()
except Exception:
    pass


ACCEPTANCE_CHANGED_BY = "acceptance"
REMOTE_CHANGED_BY = "remote_acceptance"
NO_JSON_FALLBACK_MARKER = "no_json_fallback"


@dataclass
class ScenarioResult:
    name: str
    ok: bool
    details: str
    artifacts: dict[str, str] = field(default_factory=dict)
    duration_sec: float = 0.0
    traceback_text: str = ""


class AcceptanceFailure(AssertionError):
    def __init__(self, message: str, **artifacts: str):
        super().__init__(message)
        self.artifacts = {key: str(value) for key, value in artifacts.items() if value}


def _prepare_isolated_environment(temp_root: Path) -> None:
    guard_baza = temp_root / "guard_network_baza_not_used"
    os.environ["LOCALAPPDATA"] = str(temp_root / "localappdata")
    os.environ["REMCARD_BAZA_DIR"] = str(guard_baza)
    os.environ["REMCARD_LOCAL_LOGS_DIR"] = str(temp_root / "logs")
    os.environ["REMCARD_LOCAL_FIRST_SYNC"] = "0"
    os.environ["REMCARD_LOCAL_OUTBOX_SYNC"] = "0"
    os.environ["REMCARD_LOCAL_SYNC_INTERVAL_SEC"] = "999"
    os.environ["REMCARD_LOCAL_CACHE_RETENTION_DAYS"] = "1"
    os.environ["REMCARD_LOCAL_CACHE_MAX_FILES"] = "50"
    os.environ.pop("REMCARD_EMERGENCY_WORKSTATION_ALLOWED", None)
    guard_baza.mkdir(parents=True, exist_ok=True)


def _require(condition: bool, message: str, **artifacts: str) -> None:
    if not condition:
        raise AcceptanceFailure(message, **artifacts)


def _read_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json(path: str | Path, payload: dict[str, Any]) -> None:
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    path_obj.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _sqlite_connect(path: str | Path) -> sqlite3.Connection:
    from rem_card.app.sqlite_shared import configure_connection

    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=5.0)
    configure_connection(conn, profile="network")
    return conn


def _open_readonly(path: str | Path) -> sqlite3.Connection:
    from rem_card.app.sqlite_shared import configure_connection

    uri = f"file:{Path(path).resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False, isolation_level=None, timeout=5.0)
    configure_connection(conn, readonly=True, profile="network")
    return conn


def _create_medical_fixture(path: Path) -> None:
    from rem_card.app.unified_db_schema import ensure_unified_schema

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _sqlite_connect(path)
    try:
        ensure_unified_schema(conn)
        conn.execute(
            """
            INSERT INTO patients(id, full_name, admission_uid, birth_date, last_name, first_name)
            VALUES (1, 'Acceptance Patient', 'ACC-UID-1', '1980-01-01', 'Acceptance', 'Patient')
            """
        )
        conn.execute(
            """
            INSERT INTO admissions(
                id, patient_id, bed_number, history_number, admission_datetime,
                patient_age, patient_gender, diagnosis_text
            )
            VALUES (1, 1, 1, 'ACC-1', '2026-01-01T08:00:00', 46, 'unknown', 'Acceptance')
            """
        )
        conn.commit()
    finally:
        conn.close()


def _create_settings_fixture(network_baza: Path) -> Path:
    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.services.settings.settings_service import SettingsService

    service = SettingsService(SettingsDatabase(baza_dir=str(network_baza)))
    info = service.ensure_ready()
    return Path(str(info.get("settings_db_path") or service.db.db_path))


def _write_client_policy(path: Path) -> None:
    from rem_card.app.sqlite_shared import NETWORK_SAFE_DB_PROFILE
    from rem_card.app.version import APP_VERSION

    _write_json(
        path,
        {
            "schema_version": 1,
            "min_client_version": APP_VERSION,
            "required_db_profile": NETWORK_SAFE_DB_PROFILE,
            "wal_allowed_on_shared_db": False,
        },
    )


def _build_network_fixture(temp_root: Path, scenario_name: str) -> dict[str, str]:
    scenario_dir = temp_root / scenario_name
    network_baza = scenario_dir / "network_baza"
    medical_path = network_baza / "archiv" / "rao_journal.db"
    for directory in (
        medical_path.parent,
        network_baza / "settings",
        network_baza / "locks",
        network_baza / "session_locks",
        network_baza / "backups" / "valid",
        network_baza / "backup_health",
        network_baza / "backup_health" / "invalid_backups",
        network_baza / "config",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    _create_medical_fixture(medical_path)
    settings_path = _create_settings_fixture(network_baza)
    _write_client_policy(network_baza / "config" / "client_policy.json")
    return {
        "scenario_dir": str(scenario_dir),
        "network_baza": str(network_baza),
        "medical_path": str(medical_path),
        "settings_path": str(settings_path),
        "emergency_root": str(scenario_dir / "emergency_root"),
    }


def _create_standby(paths: dict[str, str]):
    from rem_card.app.emergency_standby import EmergencyStandbyManager
    from rem_card.app.emergency_store import EmergencyLocalStore

    store = EmergencyLocalStore(root=paths["emergency_root"], source_role="nurse")
    manager = EmergencyStandbyManager(
        root=paths["emergency_root"],
        source_medical_db_path=paths["medical_path"],
        source_settings_db_path=paths["settings_path"],
        settings_required=True,
        is_safe_to_refresh=lambda: True,
        store=store,
    )
    result = manager.create_or_refresh_standby(forced=True)
    _require(result.ok and result.metadata is not None, f"standby refresh failed: {result}", scenario=paths["scenario_dir"])
    return store, result


def _move_temporarily(paths: list[str]) -> Callable[[], None]:
    moved: list[tuple[str, str]] = []
    for source in paths:
        offline = f"{source}.offline"
        os.replace(source, offline)
        moved.append((offline, source))

    def restore() -> None:
        for offline_path, source_path in reversed(moved):
            if os.path.exists(offline_path):
                os.replace(offline_path, source_path)

    return restore


def _start_nurse_emergency(paths: dict[str, str], *, simulate_unavailable: bool = True):
    from rem_card.app.emergency_startup import (
        emergency_workstation_marker_path,
        prepare_emergency_startup,
        start_or_resume_emergency_session,
    )

    store, standby = _create_standby(paths)
    marker = Path(emergency_workstation_marker_path(paths["emergency_root"]))
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("allowed\n", encoding="utf-8")

    restore_network = lambda: None
    if simulate_unavailable:
        restore_network = _move_temporarily([paths["medical_path"], paths["settings_path"]])
    try:
        decision = prepare_emergency_startup("nurse", root=paths["emergency_root"])
        _require(
            decision.allowed and decision.status == "standby_available",
            f"nurse emergency startup was not allowed from standby: {decision}",
            scenario=paths["scenario_dir"],
        )
        session = start_or_resume_emergency_session(decision, root=paths["emergency_root"])
    finally:
        restore_network()
    return store, standby, session


def _apply_controlled_local_emergency_writes(db_path: str) -> dict[str, Any]:
    conn = _sqlite_connect(db_path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO vitals(admission_id, datetime, sys, dia, pulse, temp, spo2, rr, last_modified_by)
            VALUES (1, '2026-05-30T08:01:00', 120, 80, 76, 36.6, 98, 16, ?)
            """,
            (ACCEPTANCE_CHANGED_BY,),
        )
        vital_id = int(cursor.lastrowid)
        cursor = conn.execute(
            """
            INSERT INTO orders(admission_id, datetime, text, type, status, is_committed, last_modified_by)
            VALUES (1, '2026-05-30T08:02:00', 'Acceptance order', 'medication', 'active', 1, ?)
            """,
            (ACCEPTANCE_CHANGED_BY,),
        )
        order_id = int(cursor.lastrowid)
        cursor = conn.execute(
            """
            INSERT INTO administrations(order_id, cell_role, planned_time, status, last_modified_by)
            VALUES (?, 'main', '2026-05-30T09:00:00', 'planned', ?)
            """,
            (order_id, ACCEPTANCE_CHANGED_BY),
        )
        administration_id = int(cursor.lastrowid)
        cursor = conn.execute(
            """
            INSERT INTO fluids(admission_id, datetime, iv_input, oral_input, urine, last_modified_by)
            VALUES (1, '2026-05-30T08:03:00', 100, 50, 20, ?)
            """,
            (ACCEPTANCE_CHANGED_BY,),
        )
        fluid_id = int(cursor.lastrowid)
        conn.commit()
    finally:
        conn.close()

    local_validation = _validate_medical(db_path)
    return {
        "vital_id": vital_id,
        "order_id": order_id,
        "administration_id": administration_id,
        "fluid_id": fluid_id,
        "local_last_change_id": int(local_validation.last_change_id or 0),
    }


def _apply_remote_change_after_base(db_path: str) -> int:
    conn = _sqlite_connect(db_path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO vitals(admission_id, datetime, sys, dia, pulse, temp, spo2, rr, last_modified_by)
            VALUES (1, '2026-05-30T10:00:00', 130, 82, 88, 37.1, 97, 18, ?)
            """,
            (REMOTE_CHANGED_BY,),
        )
        _ = int(cursor.lastrowid)
        conn.commit()
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM change_log").fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def _count_change_log_by(db_path: str, changed_by: str) -> int:
    conn = _open_readonly(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) FROM change_log WHERE changed_by = ?", (changed_by,)).fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def _count_table_rows_by(db_path: str, table_name: str, changed_by: str) -> int:
    conn = _open_readonly(db_path)
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table_name} WHERE last_modified_by = ?", (changed_by,)).fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def _validate_medical(path: str):
    from rem_card.app.emergency_validation import validate_medical_db_snapshot

    return validate_medical_db_snapshot(path)


def _file_hash(path: str) -> str:
    from rem_card.app.emergency_validation import compute_file_hash

    return compute_file_hash(path)


def _assert_network_sqlite_profile_unchanged() -> None:
    from rem_card.app.sqlite_shared import _resolve_sqlite_profile_settings

    profile = _resolve_sqlite_profile_settings("network")
    _require(profile.get("journal_mode") == "DELETE", f"network journal_mode changed: {profile}")
    _require(profile.get("synchronous") == "EXTRA", f"network synchronous changed: {profile}")
    _require(int(profile.get("mmap_mb") or 0) == 0, f"network mmap changed: {profile}")


def _assert_sqlite_checks(path: str) -> dict[str, str]:
    from rem_card.app.sqlite_shared import run_integrity_check, run_quick_check

    conn = _open_readonly(path)
    try:
        quick_ok, quick_reason = run_quick_check(conn)
        integrity_ok, integrity_reason = run_integrity_check(conn)
    finally:
        conn.close()
    _require(quick_ok, f"quick_check failed: {quick_reason}", db_path=path)
    _require(integrity_ok, f"integrity_check failed: {integrity_reason}", db_path=path)
    return {"quick_check": quick_reason, "integrity_check": integrity_reason}


def _assert_settings_untouched(settings_path: str, settings_hash_before: str) -> None:
    settings_hash_after = _file_hash(settings_path)
    _require(
        settings_hash_after == settings_hash_before,
        "remote settings DB was modified by emergency acceptance flow",
        settings_path=settings_path,
    )


def _run_restore_probe(paths: dict[str, str], store: Any, startup_session: Any) -> dict[str, Any]:
    from rem_card.app.emergency_restore_probe import EmergencyRestoreProbe

    probe = EmergencyRestoreProbe(
        role="nurse",
        runtime_context=startup_session.runtime_context,
        store=store,
        session_metadata=startup_session.metadata,
        success_rounds_required=1,
        stability_window_sec=1.0,
        source_medical_db_path=paths["medical_path"],
        source_settings_db_path=paths["settings_path"],
        network_baza_dir=paths["network_baza"],
        is_shutdown=lambda: False,
        is_local_write_idle=lambda: True,
        is_local_maintenance_idle=lambda: True,
    )
    status = probe.run_probe_once()
    return {"status": status, "probe": probe}


def _run_dry_run(paths: dict[str, str], store: Any, session_id: str, marker_path: str | None):
    from rem_card.app.emergency_merge_dry_run import EmergencyMergeDryRunService

    service = EmergencyMergeDryRunService(
        role="nurse",
        runtime_context=store.build_active_runtime_context(session_id),
        store=store,
        source_medical_db_path=paths["medical_path"],
        source_settings_db_path=paths["settings_path"],
        network_baza_dir=paths["network_baza"],
    )
    return service.run_dry_run(session_id, marker_path)


def _run_mode_a_merge(
    paths: dict[str, str],
    store: Any,
    session_id: str,
    dry_run_report_path: str | None,
    marker_path: str | None,
    *,
    role: str = "nurse",
    service_mutator: Callable[[Any], None] | None = None,
):
    from rem_card.app.emergency_merge_mode_a import EmergencyModeAMergeService

    service = EmergencyModeAMergeService(
        role=role,
        runtime_context=store.build_active_runtime_context(session_id),
        store=store,
        source_medical_db_path=paths["medical_path"],
        source_settings_db_path=paths["settings_path"],
        network_baza_dir=paths["network_baza"],
    )
    if service_mutator is not None:
        service_mutator(service)
    return service.run_merge(session_id, dry_run_report_path, marker_path)


def _write_manual_merge_ready_marker(paths: dict[str, str], store: Any, session: Any) -> str:
    from rem_card.app.emergency_restore_probe import write_merge_ready_marker

    return write_merge_ready_marker(
        store.resolve_root(),
        session,
        remote_last_change_id=int(session.base_last_change_id or 0),
        remote_fingerprint=dict(session.base_remote_fingerprint or {}),
    )


def scenario_full_mode_a_path(temp_root: Path) -> ScenarioResult:
    from rem_card.app.emergency_startup import find_resumable_active_session

    paths = _build_network_fixture(temp_root, "full_mode_a_path")
    settings_hash_before = _file_hash(paths["settings_path"])
    store, _standby, startup_session = _start_nurse_emergency(paths, simulate_unavailable=True)
    local_writes = _apply_controlled_local_emergency_writes(startup_session.metadata.local_db_path)

    probe_payload = _run_restore_probe(paths, store, startup_session)
    status = probe_payload["status"]
    _require(status.get("status") == "merge_ready_mode_a", f"restore probe was not Mode A ready: {status}")
    marker_path = probe_payload["probe"].mark_merge_ready()

    dry_run = _run_dry_run(paths, store, startup_session.metadata.emergency_session_id, marker_path)
    _require(dry_run.ok and dry_run.result_status == "ready_mode_a", f"dry-run not ready Mode A: {dry_run.to_dict()}")

    merge_result = _run_mode_a_merge(
        paths,
        store,
        startup_session.metadata.emergency_session_id,
        dry_run.report_path,
        marker_path,
    )
    _require(merge_result.ok and merge_result.result_status == "success", f"Mode A merge failed: {merge_result.to_dict()}")

    remote_validation = _validate_medical(paths["medical_path"])
    _require(remote_validation.ok, f"final remote validation failed: {remote_validation}")
    _require(
        int(remote_validation.last_change_id or 0) == int(local_writes["local_last_change_id"]),
        f"final remote last_change_id mismatch: {remote_validation.last_change_id} != {local_writes['local_last_change_id']}",
    )
    _require(_count_change_log_by(paths["medical_path"], ACCEPTANCE_CHANGED_BY) >= 4, "local emergency change_log rows missing")
    for table in ("vitals", "orders", "administrations", "fluids"):
        _require(_count_table_rows_by(paths["medical_path"], table, ACCEPTANCE_CHANGED_BY) >= 1, f"{table} row not visible")
    sqlite_checks = _assert_sqlite_checks(paths["medical_path"])
    _assert_settings_untouched(paths["settings_path"], settings_hash_before)

    archive_path = Path(merge_result.archive_path)
    expected_archive_files = (
        "rao_journal_emergency.db",
        "base_snapshot.db",
        "remcard_settings_snapshot.db",
        "emergency_session.json",
    )
    missing = [name for name in expected_archive_files if not (archive_path / name).is_file()]
    _require(not missing, f"archived session missing files: {missing}", archive_path=str(archive_path))
    active_session, reason = find_resumable_active_session(store)
    _require(active_session is None, f"merged session is still resumable: {active_session} reason={reason}")

    return ScenarioResult(
        name="full_mode_a_path",
        ok=True,
        details=f"merged last_change_id={remote_validation.last_change_id}, checks={sqlite_checks}",
        artifacts={"archive_path": str(archive_path), "merge_report": merge_result.report_path},
    )


def scenario_remote_changed_authoritative(temp_root: Path) -> ScenarioResult:
    paths = _build_network_fixture(temp_root, "remote_changed_authoritative")
    store, _standby, startup_session = _start_nurse_emergency(paths, simulate_unavailable=True)
    _apply_controlled_local_emergency_writes(startup_session.metadata.local_db_path)
    settings_hash_before = _file_hash(paths["settings_path"])
    changed_last = _apply_remote_change_after_base(paths["medical_path"])
    remote_hash_after_change = _file_hash(paths["medical_path"])

    probe_payload = _run_restore_probe(paths, store, startup_session)
    status = probe_payload["status"]
    _require(
        status.get("status") == "remote_changed_conflict_pending",
        f"restore probe did not report remote_changed: {status}",
    )
    marker_path = probe_payload["probe"].mark_merge_ready()

    dry_run = _run_dry_run(paths, store, startup_session.metadata.emergency_session_id, marker_path)
    _require(
        dry_run.result_status == "ready_emergency_authoritative",
        f"dry-run did not allow remote_changed authoritative merge: {dry_run.to_dict()}",
    )

    merge_result = _run_mode_a_merge(
        paths,
        store,
        startup_session.metadata.emergency_session_id,
        dry_run.report_path,
        marker_path,
    )
    _require(merge_result.ok, f"authoritative merge failed: {merge_result.to_dict()}")
    _require(_file_hash(paths["medical_path"]) != remote_hash_after_change, "remote DB was not replaced by emergency DB")
    _require(_count_change_log_by(paths["medical_path"], ACCEPTANCE_CHANGED_BY) > 0, "emergency rows were not applied")
    _require(_count_table_rows_by(paths["medical_path"], "vitals", REMOTE_CHANGED_BY) == 0, "remote-only row survived")
    _assert_settings_untouched(paths["settings_path"], settings_hash_before)
    _require(os.path.isfile(merge_result.remote_backup_path), "remote pre-merge backup missing")
    _require(os.path.isfile(merge_result.local_backup_path), "local emergency backup missing")

    dry_run_report_path = merge_result.dry_run_report_path or dry_run.report_path
    report = _read_json(dry_run_report_path)
    report_text = json.dumps(report, ensure_ascii=False)
    _require("remote_changed_emergency_authoritative" in report_text, "dry-run report does not mention authoritative mode")
    return ScenarioResult(
        name="remote_changed_authoritative",
        ok=True,
        details=f"remote_changed authoritative merge applied over change_id={changed_last}",
        artifacts={"dry_run_report": dry_run_report_path, "merge_report": merge_result.report_path},
    )


def scenario_failure_rollback(temp_root: Path) -> ScenarioResult:
    paths = _build_network_fixture(temp_root, "failure_rollback")
    store, _standby, startup_session = _start_nurse_emergency(paths, simulate_unavailable=True)
    _apply_controlled_local_emergency_writes(startup_session.metadata.local_db_path)

    probe_payload = _run_restore_probe(paths, store, startup_session)
    status = probe_payload["status"]
    _require(status.get("status") == "merge_ready_mode_a", f"restore probe was not Mode A ready: {status}")
    marker_path = probe_payload["probe"].mark_merge_ready()
    dry_run = _run_dry_run(paths, store, startup_session.metadata.emergency_session_id, marker_path)
    _require(dry_run.result_status == "ready_mode_a", f"dry-run was not ready: {dry_run.to_dict()}")
    remote_hash_before = _file_hash(paths["medical_path"])

    def force_final_validation_failure(service: Any) -> None:
        service.validate_final_remote_db = lambda *_args, **_kwargs: {
            "ok": False,
            "reason": "acceptance forced final validation failure",
        }

    merge_result = _run_mode_a_merge(
        paths,
        store,
        startup_session.metadata.emergency_session_id,
        dry_run.report_path,
        marker_path,
        service_mutator=force_final_validation_failure,
    )
    _require(merge_result.result_status == "rolled_back", f"merge did not roll back: {merge_result.to_dict()}")
    _require(merge_result.rollback_status == "restored", f"remote rollback was not restored: {merge_result.to_dict()}")
    _require(_file_hash(paths["medical_path"]) == remote_hash_before, "remote DB hash changed after rollback")
    _require(os.path.isfile(startup_session.metadata.local_db_path), "local emergency DB was not preserved after rollback")
    loaded = store.read_active_session(startup_session.metadata.emergency_session_id)
    _require(loaded.status == "merge_failed", f"session not marked merge_failed: {loaded.status}")
    _require(os.path.isfile(merge_result.report_path), f"rollback report missing: {merge_result.report_path}")
    return ScenarioResult(
        name="failure_rollback",
        ok=True,
        details="forced final validation failure rolled back remote and preserved local DB",
        artifacts={"merge_report": merge_result.report_path},
    )


def _db_files(root: Path) -> dict[str, int]:
    return {str(path): path.stat().st_size for path in root.rglob("*.db") if path.is_file()}


def scenario_no_standby_no_settings_block(temp_root: Path) -> ScenarioResult:
    from rem_card.app.emergency_startup import emergency_workstation_marker_path, prepare_emergency_startup

    paths = _build_network_fixture(temp_root, "no_standby_no_settings_block")
    root = Path(paths["emergency_root"])
    marker = Path(emergency_workstation_marker_path(str(root)))
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("allowed\n", encoding="utf-8")
    decision = prepare_emergency_startup("nurse", root=str(root))
    _require(not decision.allowed and decision.status == "no_valid_standby", f"startup without standby was not blocked: {decision}")
    _require(not list(root.rglob("*.db")), "startup without standby created a DB file", emergency_root=str(root))

    store, _standby, startup_session = _start_nurse_emergency(paths, simulate_unavailable=True)
    settings_snapshot = str(startup_session.metadata.settings_snapshot_path or "")
    _require(settings_snapshot and os.path.isfile(settings_snapshot), "settings snapshot fixture missing")
    os.remove(settings_snapshot)
    before = _db_files(root)
    decision = prepare_emergency_startup("nurse", root=str(root))
    after = _db_files(root)
    _require(
        not decision.allowed and decision.status == "active_session_invalid",
        f"startup without settings snapshot was not blocked: {decision}",
    )
    _require(before == after, "startup without settings snapshot created or changed DB files")
    _require(not any(path.stat().st_size == 0 for path in root.rglob("*.db")), "empty DB file was created")
    fallback_files = [str(path) for path in root.rglob("*fallback*")]
    _require(not fallback_files, f"{NO_JSON_FALLBACK_MARKER}: fallback files were created: {fallback_files}")
    loaded = store.read_active_session(startup_session.metadata.emergency_session_id)
    _require(loaded.settings_snapshot_path == settings_snapshot, "session metadata stopped pointing to missing settings snapshot")
    return ScenarioResult(
        name="no_standby_no_settings_block",
        ok=True,
        details="missing standby and missing settings snapshot both blocked without fallback",
        artifacts={"emergency_root": str(root)},
    )


def scenario_doctor_blocked(temp_root: Path) -> ScenarioResult:
    from rem_card.app.emergency_startup import (
        emergency_workstation_marker_path,
        prepare_emergency_startup,
    )

    paths = _build_network_fixture(temp_root, "doctor_blocked")
    store, _standby = _create_standby(paths)
    marker = Path(emergency_workstation_marker_path(paths["emergency_root"]))
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("allowed\n", encoding="utf-8")
    decision = prepare_emergency_startup("doctor", root=paths["emergency_root"])
    _require(not decision.allowed and decision.status == "role_not_allowed", f"doctor emergency startup was not blocked: {decision}")
    _require(not list((Path(paths["emergency_root"]) / "active").glob("*")), "doctor startup created/opened an emergency DB")

    store, _standby, startup_session = _start_nurse_emergency(paths, simulate_unavailable=True)
    _apply_controlled_local_emergency_writes(startup_session.metadata.local_db_path)
    session = store.read_active_session(startup_session.metadata.emergency_session_id)
    marker_path = _write_manual_merge_ready_marker(paths, store, session)
    dry_run = _run_dry_run(paths, store, session.emergency_session_id, marker_path)
    _require(dry_run.result_status == "ready_mode_a", f"nurse dry-run fixture not ready: {dry_run.to_dict()}")
    merge_result = _run_mode_a_merge(
        paths,
        store,
        session.emergency_session_id,
        dry_run.report_path,
        marker_path,
        role="doctor",
    )
    _require(
        not merge_result.ok and merge_result.error_code == "role_not_allowed",
        f"doctor Mode A merge was not blocked: {merge_result.to_dict()}",
    )
    _require(os.path.isfile(session.local_db_path), "doctor-blocked merge removed local emergency DB")
    return ScenarioResult(
        name="doctor_blocked",
        ok=True,
        details="doctor startup and Mode A merge were blocked",
        artifacts={"merge_report": merge_result.report_path},
    )


def scenario_unconfirmed_write(temp_root: Path) -> ScenarioResult:
    from rem_card.app.runtime_outage import (
        build_runtime_outage_dialog_message,
        validate_runtime_outage_startup_request_marker,
        write_runtime_outage_startup_request,
    )

    root = temp_root / "unconfirmed_write" / "emergency_root"
    marker_path, payload = write_runtime_outage_startup_request(
        root=str(root),
        source_role="nurse",
        last_observed_remote_change_id=2,
        standby_last_change_id=1,
        unconfirmed_writes=True,
    )
    validation = validate_runtime_outage_startup_request_marker(marker_path)
    _require(validation.ok, f"runtime outage marker invalid: {validation}")
    _require(bool(validation.payload.get("unconfirmed_writes")), "unconfirmed write flag was not preserved")
    _require(bool(validation.payload.get("stale_gap_detected")), "stale gap was not recorded")
    message = build_runtime_outage_dialog_message(unconfirmed_writes=True, stale_standby=True)
    _require("не были подтверждены" in message, "runtime outage message does not warn about unconfirmed writes")
    _require(not list(root.rglob("*outbox*")), "outbox/replay path was created for unconfirmed write")
    _require(not list(root.rglob("rao_journal_emergency.db")), "emergency DB was created by outage marker")
    _require(payload.get("unconfirmed_writes") is True, "written payload did not mark unconfirmed write")
    return ScenarioResult(
        name="unconfirmed_write",
        ok=True,
        details="unconfirmed write marker created without outbox or replay",
        artifacts={"startup_request": marker_path},
    )


SCENARIOS: tuple[tuple[str, Callable[[Path], ScenarioResult]], ...] = (
    ("full_mode_a_path", scenario_full_mode_a_path),
    ("remote_changed_authoritative", scenario_remote_changed_authoritative),
    ("failure_rollback", scenario_failure_rollback),
    ("no_standby_no_settings_block", scenario_no_standby_no_settings_block),
    ("doctor_blocked", scenario_doctor_blocked),
    ("unconfirmed_write", scenario_unconfirmed_write),
)


def _write_summary(path: Path, results: list[ScenarioResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "failed" if any(not item.ok for item in results) else "passed",
        "total": len(results),
        "failed": sum(1 for item in results if not item.ok),
        "passed": sum(1 for item in results if item.ok),
        "results": [asdict(item) for item in results],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _failure_result(name: str, started: float, exc: BaseException) -> ScenarioResult:
    artifacts = getattr(exc, "artifacts", {}) if isinstance(exc, AcceptanceFailure) else {}
    return ScenarioResult(
        name=name,
        ok=False,
        details=str(exc),
        artifacts=dict(artifacts),
        duration_sec=round(time.perf_counter() - started, 3),
        traceback_text=traceback.format_exc(limit=30),
    )


def main() -> int:
    temp_root = Path(tempfile.mkdtemp(prefix="remcard_emergency_acceptance_"))
    _prepare_isolated_environment(temp_root)
    _assert_network_sqlite_profile_unchanged()
    summary_path = temp_root / "artifacts" / "emergency_acceptance_summary.json"
    results: list[ScenarioResult] = []

    for name, scenario in SCENARIOS:
        started = time.perf_counter()
        try:
            result = scenario(temp_root)
            result.duration_sec = round(time.perf_counter() - started, 3)
            results.append(result)
            print(f"{name}: OK ({result.duration_sec:.3f}s) - {result.details}")
        except Exception as exc:
            result = _failure_result(name, started, exc)
            results.append(result)
            print(f"{name}: FAIL ({result.duration_sec:.3f}s) - {result.details}")
            if result.traceback_text:
                print(result.traceback_text)

    _write_summary(summary_path, results)
    failures = [item for item in results if not item.ok]
    if failures:
        print(f"summary: {summary_path}")
        print(f"temp_root: {temp_root}")
        for failure in failures:
            print(f"{failure.name} artifacts:")
            for key, value in sorted(failure.artifacts.items()):
                print(f"  {key}: {value}")
        return 1

    print(f"summary: {summary_path}")
    print("emergency_acceptance: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
