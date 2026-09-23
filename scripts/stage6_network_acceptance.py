#!/usr/bin/env python
"""Bounded Stage 6.5 acceptance checks on the authorized synthetic SMB root.

The script refuses every root except ``AUTHORIZED_ROOT``.  Every run creates a
new child directory and uses only data created there.  RemCard modules are
imported only after the process environment has been redirected to that child.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUTHORIZED_ROOT = Path(
    r"\\fs.acrb-amursk.ru\common\РАО\Пациенты\remcard\test\_etap6"
)
RUN_PREFIX = "stage6_"


@dataclass
class Step:
    name: str
    ok: bool
    duration_sec: float
    details: dict[str, Any]


def _normalized(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(os.fspath(path)))).rstrip("\\/")


def _require_authorized_root(path: str | os.PathLike[str]) -> Path:
    requested = Path(os.fspath(path))
    if _normalized(requested) != _normalized(AUTHORIZED_ROOT):
        raise ValueError(
            "Stage 6.5 may run only in the explicitly authorized synthetic root: "
            f"{AUTHORIZED_ROOT}"
        )
    return AUTHORIZED_ROOT


def _require_run_child(path: Path) -> Path:
    parent = path.parent
    if _normalized(parent) != _normalized(AUTHORIZED_ROOT) or not path.name.startswith(RUN_PREFIX):
        raise ValueError(f"Unsafe fixture path outside the Stage 6.5 root: {path}")
    return path


def _bootstrap_local_package() -> None:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from _local_rem_card_bootstrap import bootstrap_local_rem_card

    bootstrap_local_rem_card()


def build_isolated_child_environment(
    run_root: Path,
    local_root: Path,
    *,
    allow_setup: bool = False,
) -> dict[str, str]:
    """Return a complete path isolation profile before any RemCard import."""
    state = local_root / "State"
    paths = {
        "localappdata": state / "LocalAppData",
        "appdata": state / "AppData",
        "programdata": state / "ProgramData",
        "profile": state / "UserProfile",
        "temp": state / "Temp",
        "logs": state / "Logs",
        "crash": state / "CrashOutbox",
        "emergency": state / "Emergency",
        "cache": state / "Cache",
        "outbox": state / "Outbox" / "remcard_outbox.db",
        "replica": state / "Cache" / "rao_journal_local_replica.db",
        "config": state / "Config" / "remcard_data_path.json",
        "dev_config": state / "Config" / "dev_database_paths.json",
        "style": state / "QtSettings" / "style.json",
        "display": state / "QtSettings" / "display.json",
        "background": state / "QtSettings" / "background.json",
        "labs": state / "QtSettings" / "lab_columns.json",
        "qt": state / "QtSettings",
        "media": state / "MediaCache",
    }
    for key, path in paths.items():
        directory = path if key in {
            "localappdata", "appdata", "programdata", "profile", "temp", "logs",
            "crash", "emergency", "cache", "qt", "media",
        } else path.parent
        directory.mkdir(parents=True, exist_ok=True)
    (paths["profile"] / "Desktop").mkdir(exist_ok=True)

    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("REMCARD_")
    }
    env.update(
        {
            "PYTHONPATH": str(PROJECT_ROOT),
            "REMCARD_BAZA_DIR": str(run_root),
            "REMCARD_DEV_BAZA_DIR": str(run_root),
            "REMCARD_DEV_DATABASE_CONFIG": str(paths["dev_config"]),
            "REMCARD_DATA_PATH_CONFIG": str(paths["config"]),
            "REMCARD_LOCAL_LOGS_DIR": str(paths["logs"]),
            "REMCARD_CRASH_OUTBOX_DIR": str(paths["crash"]),
            "REMCARD_STYLE_SETTINGS_PATH": str(paths["style"]),
            "REMCARD_DISPLAY_SETTINGS_PATH": str(paths["display"]),
            "REMCARD_BACKGROUND_SETTINGS_PATH": str(paths["background"]),
            "REMCARD_LAB_COLUMNS_SETTINGS_PATH": str(paths["labs"]),
            "REMCARD_CI_SETTINGS_DIR": str(paths["qt"]),
            "REMCARD_EMERGENCY_DB_ROOT": str(paths["emergency"]),
            "REMCARD_MEDIA_CACHE_DIR": str(paths["media"]),
            "REMCARD_OUTBOX_PATH": str(paths["outbox"]),
            "REMCARD_REPLICA_PATH": str(paths["replica"]),
            "REMCARD_TEST_INSTANCE_NAMESPACE": str(local_root),
            "REMCARD_LOCAL_CACHE_SUFFIX": "stage6",
            "REMCARD_LOCAL_FIRST_SYNC": "0",
            "REMCARD_LOCAL_OUTBOX_SYNC": "0",
            "REMCARD_LOCAL_SYNC_INTERVAL_SEC": "999",
            "REMCARD_LOCAL_METRICS_ENABLED": "0",
            "REMCARD_STARTUP_QUICKCHECK_BACKGROUND_ENABLED": "0",
            "REMCARD_PERSISTENT_SNAPSHOT_CACHE": "0",
            "REMCARD_DEV_EXISTING_BAZA_ONLY": "0" if allow_setup else "1",
            "REMCARD_PATH_SETUP_MODE": "1" if allow_setup else "0",
            "REMCARD_UI_ROLE": "stage6_acceptance",
            "QT_QPA_PLATFORM": "offscreen",
            "LOCALAPPDATA": str(paths["localappdata"]),
            "APPDATA": str(paths["appdata"]),
            "ProgramData": str(paths["programdata"]),
            "USERPROFILE": str(paths["profile"]),
            "HOME": str(paths["profile"]),
            "HOMEDRIVE": paths["profile"].drive,
            "HOMEPATH": str(paths["profile"])[len(paths["profile"].drive):],
            "TEMP": str(paths["temp"]),
            "TMP": str(paths["temp"]),
        }
    )
    return env


def _apply_environment(env: dict[str, str]) -> None:
    for key in list(os.environ):
        if key.startswith("REMCARD_"):
            os.environ.pop(key, None)
    os.environ.update(env)
    _bootstrap_local_package()
    from rem_card.app.isolated_test_runtime import isolate_qsettings

    isolate_qsettings(env["REMCARD_CI_SETTINGS_DIR"])


def _path_is_within(path: str | os.PathLike[str], root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _verify_isolation(env: dict[str, str], run_root: Path, local_root: Path) -> dict[str, Any]:
    network_keys = ("REMCARD_BAZA_DIR", "REMCARD_DEV_BAZA_DIR")
    local_keys = (
        "REMCARD_DEV_DATABASE_CONFIG", "REMCARD_DATA_PATH_CONFIG",
        "REMCARD_LOCAL_LOGS_DIR", "REMCARD_CRASH_OUTBOX_DIR",
        "REMCARD_STYLE_SETTINGS_PATH", "REMCARD_DISPLAY_SETTINGS_PATH",
        "REMCARD_BACKGROUND_SETTINGS_PATH", "REMCARD_LAB_COLUMNS_SETTINGS_PATH",
        "REMCARD_CI_SETTINGS_DIR", "REMCARD_EMERGENCY_DB_ROOT",
        "REMCARD_MEDIA_CACHE_DIR", "REMCARD_OUTBOX_PATH", "REMCARD_REPLICA_PATH",
        "LOCALAPPDATA", "APPDATA", "ProgramData", "USERPROFILE", "HOME", "TEMP", "TMP",
    )
    outside = {
        key: env.get(key, "")
        for key in network_keys
        if _normalized(env.get(key, "")) != _normalized(run_root)
    }
    outside.update(
        {
            key: env.get(key, "")
            for key in local_keys
            if not _path_is_within(env.get(key, ""), local_root)
        }
    )
    return {
        "ok": not outside,
        "outside": outside,
        "network_root": str(run_root),
        "local_state_root": str(local_root),
        "explicit_keys": [*network_keys, *local_keys],
    }


def _wait_json(path: Path, *, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
        except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            last_error = str(exc)
        time.sleep(0.05)
    raise TimeoutError(f"Timed out waiting for {path}: {last_error}")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _session_client_target(
    run_root: str,
    role: str,
    ready_path: str,
    release_path: str,
    result_path: str,
) -> None:
    """Spawn target that behaves as an ordinary, non-admin role client."""
    _bootstrap_local_package()
    from rem_card.app import local_administrator
    from rem_card.app.unified_access import SessionLease

    local_administrator.is_local_administrator = lambda: False
    lease = SessionLease(run_root, role)
    acquired = False
    error = ""
    try:
        acquired = lease.acquire()
        _atomic_json(
            Path(ready_path),
            {
                "pid": os.getpid(),
                "role": role,
                "acquired": acquired,
                "ownership": lease.ownership,
                "rejection_state": lease.rejection_state,
            },
        )
        if acquired:
            deadline = time.monotonic() + 30.0
            while not Path(release_path).exists() and time.monotonic() < deadline:
                time.sleep(0.05)
    except Exception as exc:  # child evidence is returned to the controller
        error = f"{type(exc).__name__}: {exc}"
    finally:
        lease.release()
        _atomic_json(
            Path(result_path),
            {"pid": os.getpid(), "role": role, "acquired": acquired, "released": not lease.held, "error": error},
        )


def _slow_read_target(db_path: str, sender: Any) -> None:
    """Read-only worker held until run_startup_probe cancels and terminates it."""
    _bootstrap_local_package()
    from rem_card.app.sqlite_uri import build_sqlite_file_uri

    pid_path = Path(os.environ["REMCARD_STAGE6_SLOW_PID_FILE"])
    ready_path = Path(os.environ["REMCARD_STAGE6_SLOW_READY_FILE"])
    connection = sqlite3.connect(build_sqlite_file_uri(db_path, mode="ro"), uri=True, timeout=5.0)
    try:
        connection.execute("BEGIN")
        connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        _atomic_json(pid_path, {"pid": os.getpid()})
        ready_path.write_text("ready", encoding="utf-8")
        while True:
            time.sleep(1.0)
    finally:
        connection.close()
        sender.close()


def _timed_step(name: str, function: Callable[[], dict[str, Any]]) -> Step:
    started = time.perf_counter()
    try:
        details = function()
        ok = bool(details.pop("ok", True))
    except Exception as exc:
        ok = False
        details = {"error": f"{type(exc).__name__}: {exc}"}
    return Step(name=name, ok=ok, duration_sec=round(time.perf_counter() - started, 3), details=details)


def _prepare_database(run_root: Path) -> dict[str, Any]:
    from rem_card.app.bootstrap import bootstrap
    from rem_card.app.runtime_paths import get_journal_db_path
    from rem_card.app.unified_database_setup import prepare_database_root
    from rem_card.services.patient_bed_management.service import PatientBedManagementService

    ok, reason = prepare_database_root(run_root)
    if not ok:
        return {"ok": False, "reason": reason}
    container = bootstrap()
    try:
        with container.db_manager.remcard_transaction(source="stage6_acceptance_seed_beds") as cursor:
            for bed_number in range(1, 4):
                cursor.execute(
                    "INSERT OR IGNORE INTO beds (bed_number, status, current_admission_id) "
                    "VALUES (?, 'FREE', NULL)",
                    (bed_number,),
                )
        service = PatientBedManagementService(container.db_manager)
        admission_id = service.create_patient_and_admission(
            {"full_name": "Синтетический Пациент Этап 6.5"},
            {
                "bed_number": 1,
                "history_number": f"STAGE65-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                "admission_datetime": datetime.now() - timedelta(hours=1),
                "patient_age": 40,
                "patient_gender": "M",
                "diagnosis_text": "Синтетическая проверка Stage 6.5",
                "department_profile": "test",
            },
        )
        return {
            "ok": True,
            "prepare_result": reason,
            "admission_id": int(admission_id),
            "db_path": get_journal_db_path(str(run_root)),
        }
    finally:
        try:
            container.data_service.shutdown()
        finally:
            container.db_manager.close()


def _startup_probe(db_path: Path) -> dict[str, Any]:
    from rem_card.app.startup_check_worker import run_startup_probe

    result = run_startup_probe(str(db_path), threading.Event(), timeout=90.0)
    return {"ok": result == (True, "ok", False), "result": list(result)}


def _shared_sessions_and_maintenance(run_root: Path, local_root: Path) -> dict[str, Any]:
    from rem_card.app.unified_access import MaintenanceStore

    context = multiprocessing.get_context("spawn")
    control = local_root / "State" / "HarnessControl"
    clients: list[dict[str, Any]] = []
    processes: list[multiprocessing.Process] = []
    for role in ("doctor_stage6", "nurse_stage6"):
        ready = control / f"{role}.ready.json"
        release = control / f"{role}.release"
        result = control / f"{role}.result.json"
        process = context.Process(
            target=_session_client_target,
            args=(str(run_root), role, str(ready), str(release), str(result)),
            name=f"Stage6-{role}",
        )
        process.start()
        processes.append(process)
        clients.append({"role": role, "ready": ready, "release": release, "result": result, "process": process})

    maintenance_state: dict[str, Any] | None = None
    blocked_while_sessions = False
    ordinary_blocked = False
    ordinary_payload: dict[str, Any] = {}
    store = MaintenanceStore(run_root)
    try:
        ready_payloads = [_wait_json(item["ready"], timeout=20.0) for item in clients]
        if not all(payload.get("acquired") for payload in ready_payloads):
            raise RuntimeError(f"shared admission failed: {ready_payloads}")
        maintenance_state = store.begin("stage6 synthetic maintenance")
        blocked_while_sessions = store.try_exclusive(
            expected_generation=maintenance_state["generation"],
            operation_id=maintenance_state["operation_id"],
            owner_token=maintenance_state["owner_token"],
        ) is None
        for item in clients:
            item["release"].write_text("release", encoding="utf-8")
        for item in clients:
            item["process"].join(10.0)
            if item["process"].is_alive():
                raise RuntimeError(f"session client did not stop: {item['role']}")
        client_results = [_wait_json(item["result"], timeout=3.0) for item in clients]

        lease = store.try_exclusive(
            expected_generation=maintenance_state["generation"],
            operation_id=maintenance_state["operation_id"],
            owner_token=maintenance_state["owner_token"],
        )
        if lease is None:
            raise RuntimeError("maintenance did not become exclusive after both clients released")
        maintenance_state = store.read()

        ready = control / "ordinary_blocked.ready.json"
        release = control / "ordinary_blocked.release"
        result = control / "ordinary_blocked.result.json"
        ordinary = context.Process(
            target=_session_client_target,
            args=(str(run_root), "ordinary_stage6", str(ready), str(release), str(result)),
            name="Stage6-ordinary-blocked",
        )
        ordinary.start()
        processes.append(ordinary)
        ordinary_payload = _wait_json(ready, timeout=20.0)
        ordinary.join(10.0)
        if ordinary.is_alive():
            ordinary.terminate()
            ordinary.join(3.0)
            raise RuntimeError("ordinary admission child did not stop")
        ordinary_blocked = not bool(ordinary_payload.get("acquired"))

        reopened = store.finish(
            expected_generation=maintenance_state["generation"],
            operation_id=maintenance_state["operation_id"],
            owner_token=maintenance_state["owner_token"],
        )
        no_metadata = not any(store.sessions_dir.glob("*.json"))
        return {
            "ok": blocked_while_sessions and ordinary_blocked and no_metadata,
            "clients": ready_payloads,
            "client_results": client_results,
            "maintenance_blocked_while_clients_active": blocked_while_sessions,
            "ordinary_admission_blocked": ordinary_blocked,
            "ordinary_payload": ordinary_payload,
            "reopened_state": reopened,
            "session_metadata_clean": no_metadata,
        }
    finally:
        for item in clients:
            try:
                item["release"].write_text("release", encoding="utf-8")
            except OSError:
                pass
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(3.0)
            try:
                process.close()
            except ValueError:
                pass
        current = store.read()
        if current.get("state") in {"draining", "maintenance"}:
            try:
                store.finish(
                    expected_generation=current.get("generation"),
                    operation_id=current.get("operation_id"),
                    owner_token=current.get("owner_token"),
                )
            except Exception:
                pass


def _rotation_locks(run_root: Path, db_path: Path) -> dict[str, Any]:
    from rem_card.app.db_lifecycle import rotate_database_now
    from rem_card.app.role_session_lock import RoleSessionLock

    role_lock_path = run_root / "session_locks" / "nurse.lock"
    role_lock = RoleSessionLock(
        str(role_lock_path),
        "nurse",
        f"{os.getpid()}:stage6-rotation",
        stale_timeout_sec=75.0,
        heartbeat_sec=60.0,
    )
    if not role_lock.acquire():
        return {"ok": False, "reason": "could not acquire synthetic nurse role lock"}

    common = {
        "db_path": str(db_path),
        "archive_dir": str(run_root / "archiv"),
        "rotation_lock_path": str(run_root / "archiv" / "db_rotation.lock"),
        "db_lock_path": str(run_root / "archiv" / "db.lock"),
        "backup_dir": str(run_root / "backups" / "valid"),
        "invalid_dir": str(run_root / "backup_health" / "invalid_backups"),
        "runtime_mode": "network",
        "source": "stage6_network_acceptance",
        "blocked_role_lock_paths": {"nurse": str(role_lock_path)},
        "blocked_emergency_roots": [os.environ["REMCARD_EMERGENCY_DB_ROOT"]],
    }
    try:
        role_result = rotate_database_now(**common)
    finally:
        role_lock.release()
    bed_result = rotate_database_now(**common)
    archived = list((run_root / "archiv").glob("rao_journal_archived_*.db"))
    rotation_locks_left = [
        str(path) for path in (run_root / "archiv").glob("*.lock") if path.is_file()
    ]
    return {
        "ok": (
            role_result.get("status") == "deferred_active_role_lock"
            and bed_result.get("status") == "deferred_active_beds"
            and not archived
            and not rotation_locks_left
        ),
        "active_role_result": role_result,
        "active_bed_result": bed_result,
        "archives_created": [str(path) for path in archived],
        "rotation_locks_left": rotation_locks_left,
    }


def _corrupt_and_unavailable_probes(run_root: Path) -> dict[str, Any]:
    from rem_card.app.startup_check_worker import run_startup_probe
    from rem_card.app.startup_db_guard import _startup_access_category

    cases = run_root / "stage6_probe_cases"
    cases.mkdir(exist_ok=True)
    corrupt = cases / "synthetic_corrupt.db"
    corrupt.write_bytes(b"STAGE6 SYNTHETIC CORRUPT SQLITE - NOT CLINICAL DATA\n")
    missing = cases / "synthetic_unavailable" / "missing.db"
    cancel = threading.Event()
    corrupt_result = run_startup_probe(str(corrupt), cancel, timeout=90.0)
    missing_result = run_startup_probe(str(missing), cancel, timeout=90.0)
    corrupt_category = _startup_access_category(
        corrupt_result[1], confirmed_corruption=corrupt_result[2]
    )
    missing_category = _startup_access_category(
        missing_result[1], confirmed_corruption=missing_result[2]
    )
    return {
        "ok": (
            corrupt_result[0] is False
            and corrupt_result[2] is True
            and corrupt_category == "corruption"
            and missing_result[0] is False
            and missing_result[2] is False
            and missing_category == "missing_db"
        ),
        "corrupt_path": str(corrupt),
        "corrupt_result": list(corrupt_result),
        "corrupt_category": corrupt_category,
        "missing_path": str(missing),
        "missing_result": list(missing_result),
        "missing_category": missing_category,
    }


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        return f'"{pid}"' in completed.stdout
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _slow_worker_cancellation(db_path: Path, local_root: Path) -> dict[str, Any]:
    from rem_card.app.startup_check_worker import StartupCheckAborted, run_startup_probe

    control = local_root / "State" / "SlowWorker"
    control.mkdir(parents=True, exist_ok=True)
    pid_file = control / "pid.json"
    ready_file = control / "ready"
    os.environ["REMCARD_STAGE6_SLOW_PID_FILE"] = str(pid_file)
    os.environ["REMCARD_STAGE6_SLOW_READY_FILE"] = str(ready_file)
    cancel = threading.Event()

    def cancel_when_ready() -> None:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if ready_file.exists():
                cancel.set()
                return
            time.sleep(0.025)

    canceller = threading.Thread(target=cancel_when_ready, name="Stage6CancelSlowProbe")
    canceller.start()
    aborted_reason = ""
    cleanup_failed = False
    try:
        run_startup_probe(str(db_path), cancel, timeout=30.0, _target=_slow_read_target)
    except StartupCheckAborted as exc:
        aborted_reason = exc.reason
        cleanup_failed = exc.cleanup_failed
    finally:
        canceller.join(16.0)
    pid_payload = _wait_json(pid_file, timeout=3.0)
    worker_pid = int(pid_payload["pid"])
    alive = _pid_alive(worker_pid)

    connection = sqlite3.connect(str(db_path), isolation_level=None, timeout=5.0)
    exclusive_ok = False
    try:
        connection.execute("BEGIN EXCLUSIVE")
        connection.execute("ROLLBACK")
        exclusive_ok = True
    finally:
        connection.close()
    return {
        "ok": aborted_reason == "cancelled" and not cleanup_failed and not alive and exclusive_ok,
        "worker_pid": worker_pid,
        "aborted_reason": aborted_reason,
        "cleanup_failed": cleanup_failed,
        "worker_alive_after_return": alive,
        "exclusive_sqlite_lock_after_cancel": exclusive_ok,
        "pid_file": str(pid_file),
    }


def _inspect(root: Path) -> dict[str, Any]:
    parent = root.parent
    payload: dict[str, Any] = {
        "mode": "inspect",
        "authorized_root": str(root),
        "parent": str(parent),
        "parent_exists": parent.is_dir(),
        "root_exists": root.is_dir(),
        "runs": [],
    }
    if root.is_dir():
        payload["runs"] = [
            {"name": path.name, "modified": datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat()}
            for path in sorted(root.glob(f"{RUN_PREFIX}*"))
            if path.is_dir()
        ]
    return payload


def _create_run_root(root: Path) -> Path:
    _require_authorized_root(root)
    expected_parent = AUTHORIZED_ROOT.parent
    expected_grandparent = expected_parent.parent
    if _normalized(root.parent) != _normalized(expected_parent):
        raise ValueError(f"Unexpected Stage 6.5 parent: {root.parent}")
    if not expected_parent.exists():
        if not expected_grandparent.is_dir():
            raise FileNotFoundError(f"Authorized database test parent is unavailable: {expected_grandparent}")
        # The user authorized this exact missing test/_etap6 hierarchy.  Do not
        # use parents=True: the known remcard directory must already exist.
        expected_parent.mkdir()
    if not root.exists():
        root.mkdir()
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_root = _require_run_child(root / f"{RUN_PREFIX}{stamp}_{uuid.uuid4().hex[:8]}")
    run_root.mkdir()
    return run_root


def _run(root: Path) -> tuple[int, dict[str, Any]]:
    started = datetime.now(timezone.utc)
    run_root = _create_run_root(root)
    local_root = Path(os.environ.get("TEMP") or PROJECT_ROOT / "tmp") / f"remcard-stage6-{uuid.uuid4().hex}"
    local_root.mkdir(parents=True)
    env = build_isolated_child_environment(run_root, local_root, allow_setup=True)
    isolation = _verify_isolation(env, run_root, local_root)
    if not isolation["ok"]:
        raise RuntimeError(f"Isolation profile escaped its fixture: {isolation['outside']}")
    _apply_environment(env)

    steps: list[Step] = [Step("environment_isolation", True, 0.0, isolation)]
    steps.append(_timed_step("prepare_synthetic_database", lambda: _prepare_database(run_root)))
    prepare = steps[-1].details
    db_path = Path(str(prepare.get("db_path") or run_root / "archiv" / "rao_journal.db"))
    if steps[-1].ok:
        env["REMCARD_DEV_EXISTING_BAZA_ONLY"] = "1"
        env["REMCARD_PATH_SETUP_MODE"] = "0"
        os.environ.update(env)
        steps.append(_timed_step("startup_probe_normal", lambda: _startup_probe(db_path)))
        steps.append(
            _timed_step(
                "shared_sessions_and_maintenance",
                lambda: _shared_sessions_and_maintenance(run_root, local_root),
            )
        )
        steps.append(_timed_step("rotation_and_role_locks", lambda: _rotation_locks(run_root, db_path)))
        steps.append(
            _timed_step(
                "corrupt_and_unavailable_classification",
                lambda: _corrupt_and_unavailable_probes(run_root),
            )
        )
        steps.append(
            _timed_step(
                "slow_worker_cancellation",
                lambda: _slow_worker_cancellation(db_path, local_root),
            )
        )

    ok = all(step.ok for step in steps)
    report = {
        "schema_version": 1,
        "mode": "run",
        "ok": ok,
        "synthetic_only": True,
        "authorized_root": str(root),
        "run_root": str(run_root),
        "local_state_root": str(local_root),
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "controller_pid": os.getpid(),
        "steps": [asdict(step) for step in steps],
    }
    evidence = run_root / "evidence" / "stage6_network_acceptance.json"
    local_evidence = local_root / "State" / "stage6_network_acceptance.json"
    report["evidence_path"] = str(evidence)
    report["local_evidence_path"] = str(local_evidence)
    _atomic_json(evidence, report)
    _atomic_json(local_evidence, report)
    return (0 if ok else 1), report


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    parser = argparse.ArgumentParser(
        description="Stage 6.5 synthetic SMB acceptance harness (production paths are refused)"
    )
    parser.add_argument("mode", choices=("inspect", "run"))
    parser.add_argument("--root", default=str(AUTHORIZED_ROOT))
    args = parser.parse_args()
    try:
        root = _require_authorized_root(args.root)
        if args.mode == "inspect":
            report = _inspect(root)
            code = 0 if report["parent_exists"] else 2
        else:
            code, report = _run(root)
    except Exception as exc:
        code = 2
        report = {
            "ok": False,
            "mode": args.mode,
            "authorized_root": str(AUTHORIZED_ROOT),
            "error": f"{type(exc).__name__}: {exc}",
        }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
