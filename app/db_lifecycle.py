import logging
import os
import socket
import sqlite3
import time
import json
import uuid
from datetime import datetime
from typing import Any, Mapping, Optional

from rem_card.app.schema_migration_guard import ensure_unified_schema_with_migration_backup
from rem_card.app.sqlite_shared import FileWriteLock, backup_connection, configure_connection, run_quick_check


DB_CYCLE_META_KEY = "db_cycle_started_at"
ROTATION_ROLE_LOCK_STALE_TIMEOUT_SEC = 75.0
ROTATION_BLOCKING_EMERGENCY_STATUSES = {"active", "merge_pending", "merging", "merge_failed"}


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return bool(row)


def _read_cycle_started_at(conn: sqlite3.Connection, db_path: str, logger: logging.Logger) -> int:
    fallback_ts = int(os.path.getmtime(db_path))

    if not _table_exists(conn, "meta"):
        return fallback_ts

    row = conn.execute("SELECT value FROM meta WHERE key = ?", (DB_CYCLE_META_KEY,)).fetchone()
    if row and row[0] is not None:
        try:
            return int(row[0])
        except Exception:
            pass

    try:
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
            (DB_CYCLE_META_KEY, fallback_ts),
        )
    except Exception as exc:
        logger.warning("Failed to initialize %s meta key: %s", DB_CYCLE_META_KEY, exc)

    return fallback_ts


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
    stale_timeout_sec: float = ROTATION_ROLE_LOCK_STALE_TIMEOUT_SEC,
    logger: Optional[logging.Logger] = None,
) -> list[dict[str, str]]:
    logger = logger or logging.getLogger(__name__)
    active: list[dict[str, str]] = []
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
            if lock.is_held_by_other():
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
    )


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
) -> dict:
    """
    Архивирует БД, только если:
    1) используется сетевой runtime, а не аварийная/локальная БД,
    2) возраст БД >= max_age_days или передан force=True,
    3) нет активных ролей, которые держат БД открытой для ротации,
    4) нет активной/незавершенной аварийной сессии медсестры,
    5) нет занятых коек,
    6) создан и валидирован pre-rotation backup.
    """
    logger = logger or logging.getLogger(__name__)

    if not _runtime_allows_rotation(runtime_mode):
        return {"status": "rotation_forbidden_runtime", "runtime_mode": str(runtime_mode or "")}

    if not os.path.exists(db_path):
        return {"status": "missing"}

    lock = FileWriteLock(rotation_lock_path, stale_timeout_sec=60.0, logger=logger)
    owner_id = f"{socket.gethostname()}:{os.getpid()}:db_rotation"
    if not lock.acquire(owner_id=owner_id, source="db_rotation"):
        return {"status": "rotation_lock_busy"}

    db_lock = None
    conn = None
    backup_path = ""
    fingerprint_before_backup: dict[str, int] | None = None
    temp_new_db_path = ""
    try:
        if db_lock_path:
            db_lock = FileWriteLock(db_lock_path, stale_timeout_sec=10 * 60, logger=logger)
            if not db_lock.acquire(owner_id=owner_id, source="db_rotation"):
                return {"status": "db_lock_busy"}

        try:
            conn = sqlite3.connect(
                db_path,
                check_same_thread=False,
                isolation_level=None,
                timeout=5.0,
            )
            configure_connection(conn)

            cycle_started_at = _read_cycle_started_at(conn, db_path, logger)
            age_seconds = max(0, int(time.time()) - int(cycle_started_at))
            age_days = age_seconds / 86400.0

            if not force and age_days < max_age_days:
                return {
                    "status": "not_due",
                    "age_days": round(age_days, 2),
                }

            active_role_locks = find_active_rotation_role_locks(
                blocked_role_lock_paths,
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
                }

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
                }

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
                }

            ok, quick_result = run_quick_check(conn)
            if not ok:
                return {
                    "status": "source_quick_check_failed",
                    "age_days": round(age_days, 2),
                    "error": str(quick_result),
                }

            fingerprint_before_backup = _db_file_fingerprint(db_path)
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
                        "db_fingerprint": fingerprint_before_backup,
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
                }
        except Exception as exc:
            logger.error("DB lifecycle check failed: %s", exc, exc_info=True)
            return {"status": "check_failed", "error": str(exc)}
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None

        try:
            fingerprint_after_backup = _db_file_fingerprint(db_path)
        except Exception as exc:
            return {
                "status": "source_fingerprint_failed",
                "error": str(exc),
                "backup_path": backup_path,
            }
        if fingerprint_before_backup is None or fingerprint_after_backup != fingerprint_before_backup:
            logger.warning(
                "DB rotation aborted: DB changed after pre-rotation backup. before=%s after=%s",
                fingerprint_before_backup,
                fingerprint_after_backup,
            )
            return {
                "status": "source_changed_after_backup",
                "backup_path": backup_path,
                "before": fingerprint_before_backup,
                "after": fingerprint_after_backup,
            }

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
            }
        finally:
            if new_conn is not None:
                try:
                    new_conn.close()
                except Exception:
                    pass

        # Выполняем ротацию под тем же lock, чтобы исключить гонки.
        os.makedirs(archive_dir, exist_ok=True)
        base_name = os.path.splitext(os.path.basename(db_path))[0]
        archived_path = _build_unique_archive_path(archive_dir=archive_dir, base_name=base_name)

        try:
            os.replace(db_path, archived_path)
            for ext in ("-journal", "-wal", "-shm"):
                src = f"{db_path}{ext}"
                if os.path.exists(src):
                    os.replace(src, f"{archived_path}{ext}")
            os.replace(temp_new_db_path, db_path)
            temp_new_db_path = ""
        except Exception as exc:
            rollback_ok = False
            rollback_error = ""
            if os.path.exists(archived_path) and not os.path.exists(db_path):
                try:
                    os.replace(archived_path, db_path)
                    for ext in ("-journal", "-wal", "-shm"):
                        archived_sidecar = f"{archived_path}{ext}"
                        if os.path.exists(archived_sidecar):
                            os.replace(archived_sidecar, f"{db_path}{ext}")
                    rollback_ok = True
                except Exception as rollback_exc:
                    rollback_error = str(rollback_exc)
            logger.error("DB rotation install failed: %s rollback_ok=%s rollback_error=%s", exc, rollback_ok, rollback_error, exc_info=True)
            return {
                "status": "rotate_failed",
                "error": str(exc),
                "archived_path": archived_path,
                "backup_path": backup_path,
                "rollback_ok": rollback_ok,
                "rollback_error": rollback_error,
            }

        logger.warning(
            "DB lifecycle rotation completed: %s -> %s | backup=%s | source=%s",
            db_path,
            archived_path,
            backup_path,
            source,
        )
        return {"status": "rotated", "archived_path": archived_path, "backup_path": backup_path}
    finally:
        if temp_new_db_path:
            _remove_db_with_sidecars(temp_new_db_path)
        if db_lock:
            db_lock.release()
        lock.release()
