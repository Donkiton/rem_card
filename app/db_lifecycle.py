import logging
import os
import socket
import sqlite3
import time
from datetime import datetime
from typing import Optional

from rem_card.app.schema_migration_guard import ensure_unified_schema_with_migration_backup
from rem_card.app.sqlite_shared import FileWriteLock, configure_connection


DB_CYCLE_META_KEY = "db_cycle_started_at"


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


def maybe_rotate_database_if_due(
    *,
    db_path: str,
    archive_dir: str,
    rotation_lock_path: str,
    db_lock_path: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
    max_age_days: int = 180,
) -> dict:
    """
    Архивирует БД, только если:
    1) возраст БД >= max_age_days,
    2) нет занятых коек.
    """
    logger = logger or logging.getLogger(__name__)

    if not os.path.exists(db_path):
        return {"status": "missing"}

    lock = FileWriteLock(rotation_lock_path, stale_timeout_sec=60.0, logger=logger)
    owner_id = f"{socket.gethostname()}:{os.getpid()}:db_rotation"
    if not lock.acquire(owner_id=owner_id, source="db_rotation"):
        return {"status": "rotation_lock_busy"}

    db_lock = None
    conn = None
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

            if age_days < max_age_days:
                return {
                    "status": "not_due",
                    "age_days": round(age_days, 2),
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
        except Exception as exc:
            logger.error("DB rotation rename failed: %s", exc, exc_info=True)
            return {"status": "rotate_failed", "error": str(exc)}

        try:
            new_conn = sqlite3.connect(
                db_path,
                check_same_thread=False,
                isolation_level=None,
                timeout=5.0,
            )
            configure_connection(new_conn)
            baza_dir = os.path.dirname(os.path.dirname(db_path))
            ensure_unified_schema_with_migration_backup(
                new_conn,
                db_path=db_path,
                backup_dir=os.path.join(baza_dir, "backups", "valid"),
                invalid_dir=os.path.join(baza_dir, "backup_health", "invalid_backups"),
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
            new_conn.close()
        except Exception as exc:
            logger.error("DB rotation failed while creating fresh DB: %s", exc, exc_info=True)
            return {"status": "new_db_failed", "error": str(exc), "archived_path": archived_path}

        logger.warning("DB lifecycle rotation completed: %s -> %s", db_path, archived_path)
        return {"status": "rotated", "archived_path": archived_path}
    finally:
        if db_lock:
            db_lock.release()
        lock.release()
