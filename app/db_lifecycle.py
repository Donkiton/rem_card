import logging
import os
import socket
import sqlite3
import time
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from rem_card.app.schema_migration_guard import ensure_unified_schema_with_migration_backup
from rem_card.app.local_metrics import record_metric
from rem_card.app.sqlite_shared import (
    FileWriteLock,
    backup_connection,
    configure_connection,
    describe_sqlite_lock_holder,
    run_quick_check,
)
from rem_card.app.sqlite_uri import build_sqlite_file_uri


DB_CYCLE_META_KEY = "db_cycle_started_at"
ROTATION_ROLE_LOCK_STALE_TIMEOUT_SEC = 75.0
ROTATION_BLOCKING_EMERGENCY_STATUSES = {"active", "merge_pending", "merging", "merge_failed"}
MANUAL_ROTATION_UNDO_STATE_FILE = "manual_rotation_undo.json"
MANUAL_ROTATION_UNDO_WINDOW = timedelta(hours=24)
REPLICA_SNAPSHOT_WAIT_SEC = 20.0


def _busy_lock_result(status: str, lock_path: str, logger: logging.Logger) -> dict[str, Any]:
    owner = describe_sqlite_lock_holder(lock_path)
    logger.warning(
        "DB lifecycle lock is busy: status=%s path=%s holder_host=%s holder_pid=%s "
        "holder_user_id=%s holder_source=%s readable=%s reason=%s",
        status,
        lock_path,
        owner.get("holder_host"),
        owner.get("holder_pid"),
        owner.get("holder_user_id"),
        owner.get("holder_source"),
        owner.get("readable"),
        owner.get("reason"),
    )
    return {
        "status": status,
        "lock_path": lock_path,
        "lock_owner": owner,
    }


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return bool(row)


def _read_cycle_started_at(
    conn: sqlite3.Connection,
    db_path: str,
    logger: logging.Logger,
    *,
    initialize_missing: bool = True,
) -> int:
    fallback_ts = int(os.path.getmtime(db_path))

    if not _table_exists(conn, "meta"):
        return fallback_ts

    row = conn.execute("SELECT value FROM meta WHERE key = ?", (DB_CYCLE_META_KEY,)).fetchone()
    if row and row[0] is not None:
        try:
            return int(row[0])
        except Exception:
            pass

    if initialize_missing:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                (DB_CYCLE_META_KEY, fallback_ts),
            )
        except Exception as exc:
            logger.warning("Failed to initialize %s meta key: %s", DB_CYCLE_META_KEY, exc)

    return fallback_ts


def _baza_dir_for_db(db_path: str) -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(db_path)))


def _replica_snapshot_lease_dir(db_path: str) -> str:
    return os.path.join(_baza_dir_for_db(db_path), "locks", "replica_snapshots")


def _malformed_lock_quarantine_dir(db_path: str) -> str:
    return os.path.join(_baza_dir_for_db(db_path), "quarantine", "locks")


def _rotation_age_preflight(
    db_path: str,
    *,
    max_age_days: int,
    logger: logging.Logger,
) -> dict[str, Any]:
    conn = None
    try:
        conn = sqlite3.connect(
            build_sqlite_file_uri(db_path, mode="ro"),
            uri=True,
            check_same_thread=False,
            isolation_level=None,
            timeout=1.0,
        )
        configure_connection(conn, readonly=True)
        cycle_started_at = _read_cycle_started_at(
            conn,
            db_path,
            logger,
            initialize_missing=False,
        )
        age_days = max(0, int(time.time()) - int(cycle_started_at)) / 86400.0
        return {
            "ok": True,
            "age_days": age_days,
            "due": age_days >= int(max_age_days),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        if conn is not None:
            conn.close()


def _active_replica_snapshot_leases(
    db_path: str,
    *,
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    lease_dir = _replica_snapshot_lease_dir(db_path)
    if not os.path.isdir(lease_dir):
        return []
    active: list[dict[str, Any]] = []
    quarantine_dir = _malformed_lock_quarantine_dir(db_path)
    for name in sorted(os.listdir(lease_dir)):
        if not name.lower().endswith(".lock"):
            continue
        path = os.path.join(lease_dir, name)
        lease = FileWriteLock(
            path,
            stale_timeout_sec=60.0,
            logger=logger,
            allow_expired_lease_cleanup=True,
            allow_legacy_replica_cleanup=True,
            allow_malformed_cleanup=True,
            malformed_quarantine_dir=quarantine_dir,
        )
        if lease.cleanup_abandoned(source="db_rotation_replica_wait"):
            continue
        if not os.path.exists(path):
            continue
        active.append({"path": path, "name": name})
    return active


def _wait_for_replica_snapshot_leases(
    db_path: str,
    *,
    logger: logging.Logger,
    timeout_sec: float = REPLICA_SNAPSHOT_WAIT_SEC,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    while True:
        active = _active_replica_snapshot_leases(db_path, logger=logger)
        if not active:
            return []
        if time.monotonic() >= deadline:
            return active
        time.sleep(0.1)


def _count_active_beds(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, "beds"):
        return 0
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM beds
        WHERE status = 'OCCUPIED' OR current_admission_id IS NOT NULL
        """
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _build_unique_archive_path(archive_dir: str, base_name: str) -> str:
    """
    Формирует уникальное имя архивной БД в целевой папке.
    Это защищает от коллизий имени при одновременных стартах/ротациях.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = os.path.join(archive_dir, f"{base_name}_archived_{ts}.db")
    if not os.path.exists(candidate):
        return candidate

    ts_us = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    candidate = os.path.join(archive_dir, f"{base_name}_archived_{ts_us}.db")
    if not os.path.exists(candidate):
        return candidate

    suffix = 1
    while True:
        fallback = os.path.join(archive_dir, f"{base_name}_archived_{ts_us}_{suffix}.db")
        if not os.path.exists(fallback):
            return fallback
        suffix += 1


def _db_file_fingerprint(db_path: str) -> dict[str, int]:
    stat = os.stat(db_path)
    return {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}


def _manual_rotation_undo_state_path(archive_dir: str) -> str:
    return os.path.join(archive_dir, MANUAL_ROTATION_UNDO_STATE_FILE)


def _read_manual_rotation_undo_state(archive_dir: str) -> dict[str, Any] | None:
    try:
        with open(_manual_rotation_undo_state_path(archive_dir), "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except FileNotFoundError:
        return None
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _write_manual_rotation_undo_state(archive_dir: str, payload: dict[str, Any]) -> str:
    os.makedirs(archive_dir, exist_ok=True)
    path = _manual_rotation_undo_state_path(archive_dir)
    temp_path = f"{path}.{uuid.uuid4().hex}.tmp"
    with open(temp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temp_path, path)
    return path


def _remove_manual_rotation_undo_state(archive_dir: str) -> None:
    try:
        os.remove(_manual_rotation_undo_state_path(archive_dir))
    except FileNotFoundError:
        pass


def _rotation_change_cursor(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, "change_log"):
        return 0
    row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM change_log").fetchone()
    return int(row[0] or 0) if row else 0


def manual_rotation_undo_status(*, db_path: str, archive_dir: str) -> dict[str, Any]:
    """Проверяет, можно ли вернуть последний ручной цикл без потери пациента."""
    state = _read_manual_rotation_undo_state(archive_dir)
    if not state:
        return {"available": False, "reason": "not_available"}
    try:
        expires_at = datetime.fromisoformat(str(state.get("expires_at_utc") or ""))
        if expires_at.tzinfo is None:
            return {"available": False, "reason": "invalid_state"}
    except ValueError:
        return {"available": False, "reason": "invalid_state"}
    if datetime.now(timezone.utc) >= expires_at:
        return {"available": False, "reason": "expired", "expires_at_utc": state.get("expires_at_utc")}
    archived_path = os.path.abspath(str(state.get("archived_path") or ""))
    if not archived_path or not os.path.isfile(archived_path) or not os.path.isfile(db_path):
        return {"available": False, "reason": "files_changed"}
    if os.path.normcase(os.path.abspath(str(state.get("db_path") or ""))) != os.path.normcase(os.path.abspath(db_path)):
        return {"available": False, "reason": "files_changed"}
    conn = None
    try:
        conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None, timeout=5.0)
        configure_connection(conn, readonly=True)
        baseline = int(state.get("change_log_id") or 0)
        if _table_exists(conn, "change_log"):
            row = conn.execute(
                """
                SELECT 1 FROM change_log
                WHERE id > ? AND entity_name IN ('patients', 'admissions')
                LIMIT 1
                """,
                (baseline,),
            ).fetchone()
            if row:
                return {"available": False, "reason": "patient_data_added"}
        return {"available": True, "expires_at_utc": state.get("expires_at_utc"), "state": state}
    except Exception:
        return {"available": False, "reason": "check_failed"}
    finally:
        if conn is not None:
            conn.close()


def _record_manual_rotation_undo_state(
    *,
    source: str,
    db_path: str,
    archive_dir: str,
    archived_path: str,
    backup_path: str,
) -> str:
    try:
        if source != "manual_rotation":
            return ""
        baseline_conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None, timeout=5.0)
        try:
            configure_connection(baseline_conn, readonly=True)
            change_log_id = _rotation_change_cursor(baseline_conn)
        finally:
            baseline_conn.close()
        created_at = datetime.now(timezone.utc)
        return _write_manual_rotation_undo_state(
            archive_dir,
            {
                "version": 1,
                "db_path": os.path.abspath(db_path),
                "archived_path": os.path.abspath(archived_path),
                "backup_path": os.path.abspath(backup_path),
                "change_log_id": change_log_id,
                "created_at_utc": created_at.isoformat(),
                "expires_at_utc": (created_at + MANUAL_ROTATION_UNDO_WINDOW).isoformat(),
            },
        )
    except Exception:
        return ""


def _build_pre_rotation_backup_path(backup_dir: str, db_path: str, source: str) -> str:
    os.makedirs(backup_dir, exist_ok=True)
    base_name = os.path.splitext(os.path.basename(db_path))[0]
    safe_source = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in str(source or "rotation"))
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(backup_dir, f"pre_rotation_{safe_source}_{base_name}_{ts}.db")


def _build_temp_new_db_path(db_path: str) -> str:
    directory = os.path.dirname(os.path.abspath(db_path)) or "."
    base_name = os.path.splitext(os.path.basename(db_path))[0]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return os.path.join(directory, f".{base_name}_new_{stamp}_{os.getpid()}_{uuid.uuid4().hex[:8]}.db")


def _remove_db_with_sidecars(db_path: str) -> None:
    for candidate in (db_path, f"{db_path}-journal", f"{db_path}-wal", f"{db_path}-shm"):
        try:
            if os.path.exists(candidate):
                os.remove(candidate)
        except Exception:
            pass


def _write_rotation_backup_context(backup_path: str, context: dict) -> None:
    meta_path = f"{backup_path}.meta.json"
    payload = {}
    try:
        with open(meta_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        payload = {}
    payload["rotation"] = context
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def _runtime_allows_rotation(runtime_mode: str | None) -> bool:
    return str(runtime_mode or "network").lower() == "network"


def _normalise_role_lock_paths(blocked_role_lock_paths: Any) -> list[tuple[str, str]]:
    if not blocked_role_lock_paths:
        return []
    if isinstance(blocked_role_lock_paths, (str, bytes, os.PathLike)):
        return [("", os.fspath(blocked_role_lock_paths))]
    if isinstance(blocked_role_lock_paths, Mapping):
        return [
            (str(role or "").strip().lower(), str(path or ""))
            for role, path in blocked_role_lock_paths.items()
        ]
    result: list[tuple[str, str]] = []
    for item in blocked_role_lock_paths:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            role, path = item[0], item[1]
        else:
            role, path = "", item
        result.append((str(role or "").strip().lower(), str(path or "")))
    return result


def find_active_rotation_role_locks(
    blocked_role_lock_paths: Any,
    *,
    ignored_lock_nonces: Mapping[str, str] | None = None,
    stale_timeout_sec: float = ROTATION_ROLE_LOCK_STALE_TIMEOUT_SEC,
    logger: Optional[logging.Logger] = None,
) -> list[dict[str, str]]:
    logger = logger or logging.getLogger(__name__)
    active: list[dict[str, str]] = []
    ignored_by_path = {
        os.path.normcase(os.path.abspath(str(path))): str(nonce or "")
        for path, nonce in dict(ignored_lock_nonces or {}).items()
        if path and nonce
    }
    for role, lock_path in _normalise_role_lock_paths(blocked_role_lock_paths):
        if not lock_path:
            continue
        role_key = role or os.path.splitext(os.path.basename(lock_path))[0]
        try:
            from rem_card.app.role_session_lock import RoleSessionLock

            lock = RoleSessionLock(
                lock_path=lock_path,
                role=role_key,
                owner_id=f"{socket.gethostname()}:{os.getpid()}:db_rotation_role_check:{role_key}",
                stale_timeout_sec=stale_timeout_sec,
                heartbeat_sec=60.0,
                logger=logger,
            )
            ignored_nonce = ignored_by_path.get(os.path.normcase(os.path.abspath(lock_path)), "")
            if lock.is_held_by_other(ignored_nonce=ignored_nonce):
                active.append(
                    {
                        "role": role_key,
                        "path": os.path.abspath(lock_path),
                        "holder": lock.describe_holder(),
                    }
                )
        except Exception as exc:
            logger.warning("Failed to check rotation role lock %s: %s", lock_path, exc)
            active.append(
                {
                    "role": role_key,
                    "path": os.path.abspath(lock_path),
                    "holder": f"lock check failed: {exc}",
                }
            )
    return active


def _normalise_emergency_roots(blocked_emergency_roots: Any) -> list[str]:
    roots: list[str] = []
    candidates = blocked_emergency_roots
    if not candidates:
        try:
            from rem_card.app.emergency_paths import resolve_emergency_root

            candidates = [resolve_emergency_root()]
        except Exception:
            candidates = []
    elif isinstance(candidates, (str, bytes, os.PathLike)):
        candidates = [candidates]

    seen: set[str] = set()
    for raw in candidates:
        if not raw:
            continue
        path = os.path.abspath(os.path.normpath(os.fspath(raw)))
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        roots.append(path)
    return roots


def find_active_emergency_nurse_sessions(
    blocked_emergency_roots: Any = None,
    *,
    logger: Optional[logging.Logger] = None,
) -> list[dict[str, str]]:
    logger = logger or logging.getLogger(__name__)
    active: list[dict[str, str]] = []
    try:
        from rem_card.app.emergency_paths import active_dir, active_session_metadata_path
    except Exception as exc:
        logger.warning("Failed to import emergency session paths for DB rotation check: %s", exc)
        return active

    for root in _normalise_emergency_roots(blocked_emergency_roots):
        directory = active_dir(root)
        if not os.path.isdir(directory):
            continue
        try:
            session_names = list(os.listdir(directory))
        except OSError as exc:
            active.append(
                {
                    "role": "nurse",
                    "status": "unknown",
                    "session_id": "",
                    "path": directory,
                    "holder": f"не удалось проверить active emergency sessions: {exc}",
                }
            )
            continue

        for name in session_names:
            session_path = active_session_metadata_path(root, name)
            if not os.path.isfile(session_path):
                continue
            try:
                with open(session_path, "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
            except Exception as exc:
                active.append(
                    {
                        "role": "nurse",
                        "status": "unknown",
                        "session_id": str(name),
                        "path": os.path.abspath(session_path),
                        "holder": f"metadata недоступна: {exc}",
                    }
                )
                continue
            if not isinstance(payload, dict):
                continue
            role = str(payload.get("source_role") or "").strip().lower()
            status = str(payload.get("status") or "").strip().lower()
            if role == "nurse" and status in ROTATION_BLOCKING_EMERGENCY_STATUSES:
                active.append(
                    {
                        "role": role,
                        "status": status,
                        "session_id": str(payload.get("emergency_session_id") or name),
                        "path": os.path.abspath(session_path),
                        "holder": str(payload.get("source_machine") or payload.get("source_client_id") or ""),
                    }
                )
    return active


def rotate_database_now(
    *,
    db_path: str,
    archive_dir: str,
    rotation_lock_path: str,
    db_lock_path: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
    backup_dir: Optional[str] = None,
    invalid_dir: Optional[str] = None,
    runtime_mode: str | None = "network",
    source: str = "manual_rotation",
    max_age_days: int = 180,
    blocked_role_lock_paths: Any = None,
    blocked_emergency_roots: Any = None,
    ignored_lock_nonces: Mapping[str, str] | None = None,
) -> dict:
    return maybe_rotate_database_if_due(
        db_path=db_path,
        archive_dir=archive_dir,
        rotation_lock_path=rotation_lock_path,
        db_lock_path=db_lock_path,
        logger=logger,
        max_age_days=max_age_days,
        force=True,
        backup_dir=backup_dir,
        invalid_dir=invalid_dir,
        runtime_mode=runtime_mode,
        source=source,
        blocked_role_lock_paths=blocked_role_lock_paths,
        blocked_emergency_roots=blocked_emergency_roots,
        ignored_lock_nonces=ignored_lock_nonces,
    )


def _rotation_request_preflight(
    *,
    db_path: str,
    runtime_mode: str | None,
    max_age_days: int,
    force: bool,
    logger: logging.Logger,
) -> dict[str, Any] | None:
    if not _runtime_allows_rotation(runtime_mode):
        return {"status": "rotation_forbidden_runtime", "runtime_mode": str(runtime_mode or "")}
    if not os.path.exists(db_path):
        return {"status": "missing"}
    if force:
        return None
    preflight = _rotation_age_preflight(
        db_path,
        max_age_days=max_age_days,
        logger=logger,
    )
    if not preflight.get("ok"):
        return {
            "status": "check_failed",
            "error": str(preflight.get("error") or "rotation age preflight failed"),
        }
    if preflight.get("due"):
        return None
    return {
        "status": "not_due",
        "age_days": round(float(preflight.get("age_days") or 0.0), 2),
    }


def _rotation_source_eligibility(
    conn: sqlite3.Connection,
    *,
    db_path: str,
    max_age_days: int,
    force: bool,
    blocked_role_lock_paths: Any,
    blocked_emergency_roots: Any,
    ignored_lock_nonces: Mapping[str, str] | None,
    logger: logging.Logger,
) -> tuple[dict[str, Any] | None, float]:
    cycle_started_at = _read_cycle_started_at(conn, db_path, logger)
    age_seconds = max(0, int(time.time()) - int(cycle_started_at))
    age_days = age_seconds / 86400.0
    if not force and age_days < max_age_days:
        return {"status": "not_due", "age_days": round(age_days, 2)}, age_days

    active_role_locks = find_active_rotation_role_locks(
        blocked_role_lock_paths,
        ignored_lock_nonces=ignored_lock_nonces,
        logger=logger,
    )
    if active_role_locks:
        logger.info(
            "DB rotation is due (age=%.1f days), but blocking role lock(s) are active: %s",
            age_days,
            active_role_locks,
        )
        return {
            "status": "deferred_active_role_lock",
            "age_days": round(age_days, 2),
            "blocked_roles": active_role_locks,
        }, age_days

    active_emergency_sessions = find_active_emergency_nurse_sessions(
        blocked_emergency_roots,
        logger=logger,
    )
    if active_emergency_sessions:
        logger.info(
            "DB rotation is due (age=%.1f days), but emergency nurse session(s) are active: %s",
            age_days,
            active_emergency_sessions,
        )
        return {
            "status": "deferred_active_emergency_session",
            "age_days": round(age_days, 2),
            "blocked_emergency_sessions": active_emergency_sessions,
        }, age_days

    active_beds = _count_active_beds(conn)
    if active_beds > 0:
        logger.info(
            "DB rotation is due (age=%.1f days), but %s occupied bed(s) still active. Rotation deferred.",
            age_days,
            active_beds,
        )
        return {
            "status": "deferred_active_beds",
            "age_days": round(age_days, 2),
            "active_beds": active_beds,
        }, age_days

    ok, quick_result = run_quick_check(conn)
    if ok:
        return None, age_days
    return {
        "status": "source_quick_check_failed",
        "age_days": round(age_days, 2),
        "error": str(quick_result),
    }, age_days


def _create_pre_rotation_backup(
    conn: sqlite3.Connection,
    *,
    db_path: str,
    backup_dir: str | None,
    invalid_dir: str | None,
    runtime_mode: str | None,
    source: str,
    force: bool,
    max_age_days: int,
    age_days: float,
    logger: logging.Logger,
) -> tuple[dict[str, Any] | None, str, dict[str, int] | None]:
    fingerprint = _db_file_fingerprint(db_path)
    baza_dir = os.path.dirname(os.path.dirname(db_path))
    effective_backup_dir = backup_dir or os.path.join(baza_dir, "backups", "valid")
    effective_invalid_dir = invalid_dir or os.path.join(baza_dir, "backup_health", "invalid_backups")
    backup_path = _build_pre_rotation_backup_path(effective_backup_dir, db_path, source)
    try:
        backup_connection(
            conn,
            backup_path,
            invalid_dir=effective_invalid_dir,
            logger=logger,
            validate=True,
            source=f"{source}_pre_rotation",
        )
        _write_rotation_backup_context(
            backup_path,
            {
                "source": source,
                "runtime_mode": str(runtime_mode or ""),
                "db_path": os.path.abspath(db_path),
                "db_fingerprint": fingerprint,
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "force": bool(force),
                "max_age_days": int(max_age_days),
                "age_days": round(age_days, 2),
            },
        )
    except Exception as exc:
        logger.error("Pre-rotation backup failed: %s", exc, exc_info=True)
        return {
            "status": "pre_rotation_backup_failed",
            "age_days": round(age_days, 2),
            "error": str(exc),
            "backup_path": backup_path,
        }, backup_path, fingerprint
    return None, backup_path, fingerprint


def _prepare_rotation_source(
    *,
    db_path: str,
    backup_dir: str | None,
    invalid_dir: str | None,
    runtime_mode: str | None,
    source: str,
    force: bool,
    max_age_days: int,
    blocked_role_lock_paths: Any,
    blocked_emergency_roots: Any,
    ignored_lock_nonces: Mapping[str, str] | None,
    logger: logging.Logger,
) -> tuple[dict[str, Any] | None, str, dict[str, int] | None]:
    conn = None
    try:
        conn = sqlite3.connect(
            db_path,
            check_same_thread=False,
            isolation_level=None,
            timeout=5.0,
        )
        configure_connection(conn)
        failure, age_days = _rotation_source_eligibility(
            conn,
            db_path=db_path,
            max_age_days=max_age_days,
            force=force,
            blocked_role_lock_paths=blocked_role_lock_paths,
            blocked_emergency_roots=blocked_emergency_roots,
            ignored_lock_nonces=ignored_lock_nonces,
            logger=logger,
        )
        if failure:
            return failure, "", None
        return _create_pre_rotation_backup(
            conn,
            db_path=db_path,
            backup_dir=backup_dir,
            invalid_dir=invalid_dir,
            runtime_mode=runtime_mode,
            source=source,
            force=force,
            max_age_days=max_age_days,
            age_days=age_days,
            logger=logger,
        )
    except Exception as exc:
        logger.error("DB lifecycle check failed: %s", exc, exc_info=True)
        return {"status": "check_failed", "error": str(exc)}, "", None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _rotation_fingerprint_failure(
    *,
    db_path: str,
    backup_path: str,
    fingerprint_before: dict[str, int] | None,
    logger: logging.Logger,
) -> dict[str, Any] | None:
    try:
        fingerprint_after = _db_file_fingerprint(db_path)
    except Exception as exc:
        return {
            "status": "source_fingerprint_failed",
            "error": str(exc),
            "backup_path": backup_path,
        }
    if fingerprint_before is not None and fingerprint_after == fingerprint_before:
        return None
    logger.warning(
        "DB rotation aborted: DB changed after pre-rotation backup. before=%s after=%s",
        fingerprint_before,
        fingerprint_after,
    )
    return {
        "status": "source_changed_after_backup",
        "backup_path": backup_path,
        "before": fingerprint_before,
        "after": fingerprint_after,
    }


def _prepare_fresh_rotation_database(
    *,
    db_path: str,
    backup_path: str,
    backup_dir: str | None,
    invalid_dir: str | None,
    logger: logging.Logger,
) -> tuple[dict[str, Any] | None, str]:
    temp_new_db_path = _build_temp_new_db_path(db_path)
    new_conn = None
    try:
        new_conn = sqlite3.connect(
            temp_new_db_path,
            check_same_thread=False,
            isolation_level=None,
            timeout=5.0,
        )
        configure_connection(new_conn)
        baza_dir = os.path.dirname(os.path.dirname(db_path))
        ensure_unified_schema_with_migration_backup(
            new_conn,
            db_path=temp_new_db_path,
            backup_dir=backup_dir or os.path.join(baza_dir, "backups", "valid"),
            invalid_dir=invalid_dir or os.path.join(baza_dir, "backup_health", "invalid_backups"),
            policy_path=os.path.join(baza_dir, "config", "client_policy.json"),
            baza_dir=baza_dir,
            logger=logger,
            source="db_rotation_schema_init",
        )
        with new_conn:
            new_conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (DB_CYCLE_META_KEY, int(time.time())),
            )
        ok, quick_result = run_quick_check(new_conn)
        if not ok:
            raise RuntimeError(f"fresh DB quick_check failed: {quick_result}")
    except Exception as exc:
        logger.error("DB rotation failed while preparing fresh DB: %s", exc, exc_info=True)
        return {
            "status": "new_db_failed",
            "error": str(exc),
            "backup_path": backup_path,
            "current_preserved": True,
        }, temp_new_db_path
    finally:
        if new_conn is not None:
            try:
                new_conn.close()
            except Exception:
                pass
    return None, temp_new_db_path


def _move_rotation_sidecars(source_path: str, target_path: str) -> None:
    for extension in ("-journal", "-wal", "-shm"):
        source_sidecar = f"{source_path}{extension}"
        if os.path.exists(source_sidecar):
            os.replace(source_sidecar, f"{target_path}{extension}")


def _rollback_rotation_install(db_path: str, archived_path: str) -> tuple[bool, str]:
    if not os.path.exists(archived_path) or os.path.exists(db_path):
        return False, ""
    try:
        os.replace(archived_path, db_path)
        _move_rotation_sidecars(archived_path, db_path)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _install_fresh_rotation_database(
    *,
    db_path: str,
    archive_dir: str,
    temp_new_db_path: str,
    backup_path: str,
    logger: logging.Logger,
) -> tuple[dict[str, Any] | None, str]:
    os.makedirs(archive_dir, exist_ok=True)
    base_name = os.path.splitext(os.path.basename(db_path))[0]
    archived_path = _build_unique_archive_path(archive_dir=archive_dir, base_name=base_name)
    try:
        os.replace(db_path, archived_path)
        _move_rotation_sidecars(db_path, archived_path)
        os.replace(temp_new_db_path, db_path)
    except Exception as exc:
        rollback_ok, rollback_error = _rollback_rotation_install(db_path, archived_path)
        logger.error(
            "DB rotation install failed: %s rollback_ok=%s rollback_error=%s",
            exc,
            rollback_ok,
            rollback_error,
            exc_info=True,
        )
        return {
            "status": "rotate_failed",
            "error": str(exc),
            "archived_path": archived_path,
            "backup_path": backup_path,
            "rollback_ok": rollback_ok,
            "rollback_error": rollback_error,
        }, archived_path
    return None, archived_path


def _rotation_success_result(
    *,
    source: str,
    db_path: str,
    archive_dir: str,
    archived_path: str,
    backup_path: str,
    logger: logging.Logger,
) -> dict[str, Any]:
    undo_state_path = _record_manual_rotation_undo_state(
        source=source,
        db_path=db_path,
        archive_dir=archive_dir,
        archived_path=archived_path,
        backup_path=backup_path,
    )
    logger.warning(
        "DB lifecycle rotation completed: %s -> %s | backup=%s | source=%s",
        db_path,
        archived_path,
        backup_path,
        source,
    )
    return {
        "status": "rotated",
        "archived_path": archived_path,
        "backup_path": backup_path,
        "undo_state_path": undo_state_path,
    }


def _run_rotation_under_lock(
    *,
    db_path: str,
    archive_dir: str,
    db_lock_path: str | None,
    owner_id: str,
    backup_dir: str | None,
    invalid_dir: str | None,
    runtime_mode: str | None,
    source: str,
    force: bool,
    max_age_days: int,
    blocked_role_lock_paths: Any,
    blocked_emergency_roots: Any,
    ignored_lock_nonces: Mapping[str, str] | None,
    logger: logging.Logger,
) -> dict[str, Any]:
    db_lock = None
    temp_new_db_path = ""
    try:
        active_replica_leases = _wait_for_replica_snapshot_leases(db_path, logger=logger)
        if active_replica_leases:
            return {
                "status": "replica_snapshot_busy",
                "active_replica_leases": active_replica_leases,
            }
        if db_lock_path:
            db_lock = FileWriteLock(db_lock_path, stale_timeout_sec=10 * 60, logger=logger)
            if not db_lock.acquire(owner_id=owner_id, source="db_rotation"):
                return _busy_lock_result("db_lock_busy", db_lock_path, logger)

        failure, backup_path, fingerprint = _prepare_rotation_source(
            db_path=db_path,
            backup_dir=backup_dir,
            invalid_dir=invalid_dir,
            runtime_mode=runtime_mode,
            source=source,
            force=force,
            max_age_days=max_age_days,
            blocked_role_lock_paths=blocked_role_lock_paths,
            blocked_emergency_roots=blocked_emergency_roots,
            ignored_lock_nonces=ignored_lock_nonces,
            logger=logger,
        )
        if failure:
            return failure
        failure = _rotation_fingerprint_failure(
            db_path=db_path,
            backup_path=backup_path,
            fingerprint_before=fingerprint,
            logger=logger,
        )
        if failure:
            return failure
        failure, temp_new_db_path = _prepare_fresh_rotation_database(
            db_path=db_path,
            backup_path=backup_path,
            backup_dir=backup_dir,
            invalid_dir=invalid_dir,
            logger=logger,
        )
        if failure:
            return failure
        failure, archived_path = _install_fresh_rotation_database(
            db_path=db_path,
            archive_dir=archive_dir,
            temp_new_db_path=temp_new_db_path,
            backup_path=backup_path,
            logger=logger,
        )
        if failure:
            return failure
        temp_new_db_path = ""
        return _rotation_success_result(
            source=source,
            db_path=db_path,
            archive_dir=archive_dir,
            archived_path=archived_path,
            backup_path=backup_path,
            logger=logger,
        )
    finally:
        if temp_new_db_path:
            _remove_db_with_sidecars(temp_new_db_path)
        if db_lock:
            db_lock.release()


def maybe_rotate_database_if_due(
    *,
    db_path: str,
    archive_dir: str,
    rotation_lock_path: str,
    db_lock_path: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
    max_age_days: int = 180,
    force: bool = False,
    backup_dir: Optional[str] = None,
    invalid_dir: Optional[str] = None,
    runtime_mode: str | None = "network",
    source: str = "auto_rotation",
    blocked_role_lock_paths: Any = None,
    blocked_emergency_roots: Any = None,
    ignored_lock_nonces: Mapping[str, str] | None = None,
) -> dict:
    """
    Архивирует сетевую БД после проверок возраста, активных сессий и backup.
    """
    logger = logger or logging.getLogger(__name__)
    preflight_failure = _rotation_request_preflight(
        db_path=db_path,
        runtime_mode=runtime_mode,
        max_age_days=max_age_days,
        force=force,
        logger=logger,
    )
    if preflight_failure:
        return preflight_failure

    lock = FileWriteLock(
        rotation_lock_path,
        stale_timeout_sec=60.0,
        logger=logger,
        allow_expired_lease_cleanup=True,
        allow_legacy_replica_cleanup=True,
        allow_malformed_cleanup=True,
        malformed_quarantine_dir=_malformed_lock_quarantine_dir(db_path),
    )
    owner_id = f"{socket.gethostname()}:{os.getpid()}:db_rotation"
    if not lock.acquire(owner_id=owner_id, source="db_rotation"):
        return _busy_lock_result("rotation_lock_busy", rotation_lock_path, logger)
    record_metric(
        "db_rotation_lock_created",
        1,
        source=source,
        force=bool(force),
        rotation_lock_path=rotation_lock_path,
    )
    try:
        return _run_rotation_under_lock(
            db_path=db_path,
            archive_dir=archive_dir,
            db_lock_path=db_lock_path,
            owner_id=owner_id,
            backup_dir=backup_dir,
            invalid_dir=invalid_dir,
            runtime_mode=runtime_mode,
            source=source,
            force=force,
            max_age_days=max_age_days,
            blocked_role_lock_paths=blocked_role_lock_paths,
            blocked_emergency_roots=blocked_emergency_roots,
            ignored_lock_nonces=ignored_lock_nonces,
            logger=logger,
        )
    finally:
        released = lock.release()
        record_metric(
            "db_rotation_lock_released",
            1 if released else 0,
            source=source,
            rotation_lock_path=rotation_lock_path,
        )


def cancel_manual_rotation(
    *,
    db_path: str,
    archive_dir: str,
    rotation_lock_path: str,
    db_lock_path: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
    blocked_role_lock_paths: Any = None,
    blocked_emergency_roots: Any = None,
    ignored_lock_nonces: Mapping[str, str] | None = None,
) -> dict:
    """Возвращает предыдущий цикл только пока в новом цикле не было пациента."""
    logger = logger or logging.getLogger(__name__)
    status = manual_rotation_undo_status(db_path=db_path, archive_dir=archive_dir)
    if not status.get("available"):
        return {"status": "undo_unavailable", "reason": status.get("reason", "not_available")}
    state = dict(status["state"])
    lock = FileWriteLock(
        rotation_lock_path,
        stale_timeout_sec=60.0,
        logger=logger,
        allow_expired_lease_cleanup=True,
        allow_legacy_replica_cleanup=True,
        allow_malformed_cleanup=True,
        malformed_quarantine_dir=_malformed_lock_quarantine_dir(db_path),
    )
    owner_id = f"{socket.gethostname()}:{os.getpid()}:db_rotation_undo"
    if not lock.acquire(owner_id=owner_id, source="db_rotation_undo"):
        return _busy_lock_result("rotation_lock_busy", rotation_lock_path, logger)
    db_lock = None
    moved_new_path = ""
    try:
        active_replica_leases = _wait_for_replica_snapshot_leases(
            db_path,
            logger=logger,
        )
        if active_replica_leases:
            return {
                "status": "replica_snapshot_busy",
                "active_replica_leases": active_replica_leases,
            }
        if db_lock_path:
            db_lock = FileWriteLock(db_lock_path, stale_timeout_sec=10 * 60, logger=logger)
            if not db_lock.acquire(owner_id=owner_id, source="db_rotation_undo"):
                return _busy_lock_result("db_lock_busy", db_lock_path, logger)
        active_role_locks = find_active_rotation_role_locks(
            blocked_role_lock_paths,
            ignored_lock_nonces=ignored_lock_nonces,
            logger=logger,
        )
        if active_role_locks:
            return {"status": "deferred_active_role_lock", "blocked_roles": active_role_locks}
        active_emergency_sessions = find_active_emergency_nurse_sessions(
            blocked_emergency_roots,
            logger=logger,
        )
        if active_emergency_sessions:
            return {
                "status": "deferred_active_emergency_session",
                "blocked_emergency_sessions": active_emergency_sessions,
            }
        status = manual_rotation_undo_status(db_path=db_path, archive_dir=archive_dir)
        if not status.get("available"):
            return {"status": "undo_unavailable", "reason": status.get("reason", "not_available")}
        archived_path = os.path.abspath(str(state["archived_path"]))
        for path in (db_path, archived_path):
            conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=5.0)
            try:
                configure_connection(conn, readonly=True)
                ok, result = run_quick_check(conn)
                if not ok:
                    return {"status": "undo_validation_failed", "path": path, "error": str(result)}
            finally:
                conn.close()
        base_name = os.path.splitext(os.path.basename(db_path))[0]
        moved_new_path = _build_unique_archive_path(archive_dir, f"{base_name}_manual_rotation_cancelled")
        try:
            os.replace(db_path, moved_new_path)
            for ext in ("-journal", "-wal", "-shm"):
                if os.path.exists(f"{db_path}{ext}"):
                    os.replace(f"{db_path}{ext}", f"{moved_new_path}{ext}")
            os.replace(archived_path, db_path)
            for ext in ("-journal", "-wal", "-shm"):
                if os.path.exists(f"{archived_path}{ext}"):
                    os.replace(f"{archived_path}{ext}", f"{db_path}{ext}")
        except Exception as exc:
            rollback_error = ""
            if moved_new_path and os.path.exists(moved_new_path) and not os.path.exists(db_path):
                try:
                    os.replace(moved_new_path, db_path)
                    for ext in ("-journal", "-wal", "-shm"):
                        if os.path.exists(f"{moved_new_path}{ext}"):
                            os.replace(f"{moved_new_path}{ext}", f"{db_path}{ext}")
                except Exception as rollback_exc:
                    rollback_error = str(rollback_exc)
            return {"status": "undo_failed", "error": str(exc), "rollback_error": rollback_error}
        _remove_manual_rotation_undo_state(archive_dir)
        logger.warning("Manual DB rotation cancelled: restored=%s retained_new_cycle=%s", db_path, moved_new_path)
        return {"status": "undo_rotated", "retained_new_cycle_path": moved_new_path}
    finally:
        if db_lock:
            db_lock.release()
        lock.release()
