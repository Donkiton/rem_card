import glob
import os
import sqlite3
import time
from datetime import datetime, timedelta

from rem_card.app.logger import logger
from rem_card.app.paths import (
    ARCHIV_DIR,
    BACKUPS_RC_DIR,
    BACKUPS_VALID_DIR,
    DB_LOCK_PATH,
    INVALID_BACKUPS_DIR,
    LOGS_DIR,
    REMCARD_DB_PATH,
    REPORT_DIR,
    ensure_directories,
)
from rem_card.app.sqlite_shared import (
    FileWriteLock,
    backup_connection,
    configure_connection,
    list_backup_candidates,
    run_quick_check,
    validate_sqlite_file,
)


# Более консервативные дефолты, чтобы рост backup-каталога оставался контролируемым
# даже без ручной настройки окружения.
BACKUP_RETENTION_DAYS = max(1, int(os.environ.get("REMCARD_BACKUP_RETENTION_DAYS", "21")))
BACKUP_MAX_COUNT = max(5, int(os.environ.get("REMCARD_BACKUP_MAX_COUNT", "21")))
BACKUP_MAX_TOTAL_BYTES = max(
    256 * 1024 * 1024,
    int(float(os.environ.get("REMCARD_BACKUP_MAX_TOTAL_GB", "1.0")) * 1024 * 1024 * 1024),
)

CHANGE_LOG_RETENTION_DAYS = max(1, int(os.environ.get("REMCARD_CHANGELOG_RETENTION_DAYS", "14")))
REPORT_RETENTION_DAYS = max(1, int(os.environ.get("REMCARD_REPORT_RETENTION_DAYS", "7")))
CHANGE_LOG_MAX_ROWS = max(10_000, int(os.environ.get("REMCARD_CHANGELOG_MAX_ROWS", "120000")))
CHANGE_LOG_PRUNE_BATCH = max(1000, int(os.environ.get("REMCARD_CHANGELOG_PRUNE_BATCH", "50000")))
CHANGE_LOG_COMPACT_MIN_FREE_BYTES = max(
    8 * 1024 * 1024,
    int(float(os.environ.get("REMCARD_CHANGELOG_COMPACT_MIN_MB", "16")) * 1024 * 1024),
)
CHANGE_LOG_COMPACT_MIN_INTERVAL_SEC = max(
    3600,
    int(float(os.environ.get("REMCARD_CHANGELOG_COMPACT_MIN_HOURS", "24")) * 3600),
)
CHANGE_LOG_COMPACT_STAMP_PATH = os.path.join(ARCHIV_DIR, ".last_change_log_compact")

LOCKED_ERROR_MARKERS = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
)
BACKUP_HEALTH_CHECK_SCAN_LIMIT = max(
    1,
    int(os.environ.get("REMCARD_BACKUP_HEALTH_CHECK_SCAN_LIMIT", "8")),
)


def perform_daily_backup_and_cleanup():
    try:
        ensure_directories()

        now = datetime.now()
        prune_stats = _prune_change_log_and_maybe_compact(REMCARD_DB_PATH, now)

        # 1) Determine current "resuscitation day"
        # Resuscitation day starts at 8:00 AM.
        # If current time is < 8:00 AM, it belongs to previous calendar day.
        if now.hour < 8:
            resuscitation_date = (now - timedelta(days=1)).date()
        else:
            resuscitation_date = now.date()

        backup_date_str = resuscitation_date.strftime("%Y-%m-%d")
        db_base_name = os.path.splitext(os.path.basename(REMCARD_DB_PATH))[0]
        backup_file_name = f"{db_base_name}_{backup_date_str}.db"
        backup_file_path = os.path.join(BACKUPS_VALID_DIR, backup_file_name)

        # 2) Check if backup for this day already exists
        if not os.path.exists(backup_file_path):
            if os.path.exists(REMCARD_DB_PATH):
                _create_safe_sqlite_backup(REMCARD_DB_PATH, backup_file_path)
                logger.info("Daily backup created via SQLite backup API: %s", backup_file_path)
            else:
                logger.warning("Database not found at %s, skipping backup.", REMCARD_DB_PATH)
        else:
            logger.info("Backup for %s already exists, skipping backup.", backup_date_str)

        # 3) Cleanup old backups by age + enforce hard count/size caps
        if _can_cleanup_old_backups(REMCARD_DB_PATH, BACKUPS_RC_DIR):
            cutoff_30_days = now - timedelta(days=BACKUP_RETENTION_DAYS)
            _cleanup_old_files(BACKUPS_RC_DIR, "*.db", cutoff_30_days, "backup")
            _cleanup_old_files(BACKUPS_VALID_DIR, "*.db", cutoff_30_days, "validated backup")
            _cleanup_old_files(BACKUPS_VALID_DIR, "*.meta.json", cutoff_30_days, "validated backup metadata")
            _enforce_backup_limits(BACKUPS_RC_DIR)
            _enforce_backup_limits(BACKUPS_VALID_DIR)
        else:
            logger.warning(
                "Backup cleanup skipped: no healthy backup source is available yet. "
                "Old backups are preserved to reduce recovery risk."
            )

        # 4) Cleanup old reports (> 1 week)
        cutoff_report_days = now - timedelta(days=REPORT_RETENTION_DAYS)
        _cleanup_old_report_files(REPORT_DIR, cutoff_report_days)

        # 5) Cleanup old local runtime logs (> 30 days)
        cutoff_30_days = now - timedelta(days=30)
        _cleanup_old_files(LOGS_DIR, "*.log", cutoff_30_days, "local log")

        if prune_stats and prune_stats.get("deleted_rows", 0) > 0:
            logger.info(
                "Change-log maintenance: deleted=%s, before=%s, after=%s, compacted=%s",
                prune_stats.get("deleted_rows"),
                prune_stats.get("rows_before"),
                prune_stats.get("rows_after"),
                prune_stats.get("compacted"),
            )

    except Exception as exc:
        logger.error("Error during backup and cleanup: %s", exc, exc_info=True)


def _is_primary_db_healthy(db_path: str) -> bool:
    if not os.path.exists(db_path):
        return False

    conn = None
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=4.0)
        configure_connection(conn, readonly=True)
        ok, _result = run_quick_check(conn)
        return bool(ok)
    except Exception as exc:
        logger.warning("Primary DB health check failed for backup cleanup: %s", exc)
        return False
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _has_healthy_backup(backup_dir: str) -> bool:
    candidates = list_backup_candidates(backup_dir=backup_dir)
    if not candidates:
        return False

    for backup_path in candidates[:BACKUP_HEALTH_CHECK_SCAN_LIMIT]:
        ok, _reason = validate_sqlite_file(backup_path)
        if ok:
            return True
    return False


def _can_cleanup_old_backups(db_path: str, backup_dir: str) -> bool:
    if not _is_primary_db_healthy(db_path):
        return False
    return _has_healthy_backup(backup_dir)


def _create_safe_sqlite_backup(db_path: str, backup_file_path: str):
    os.makedirs(os.path.dirname(backup_file_path), exist_ok=True)
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=5.0)
    try:
        configure_connection(conn, readonly=True)
        backup_connection(
            conn,
            backup_file_path,
            invalid_dir=INVALID_BACKUPS_DIR,
            logger=logger,
            lock_path=DB_LOCK_PATH,
            source="daily_backup",
        )
    finally:
        conn.close()


def _cleanup_old_files(directory, pattern, cutoff_date, file_type):
    if not os.path.exists(directory):
        return

    search_pattern = os.path.join(directory, pattern)
    for filepath in glob.glob(search_pattern):
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
            if mtime < cutoff_date:
                os.remove(filepath)
                logger.info("Deleted old %s: %s", file_type, filepath)
        except Exception as exc:
            logger.error("Failed to delete old %s %s: %s", file_type, filepath, exc)


def _cleanup_old_report_files(directory, cutoff_date):
    if not os.path.exists(directory):
        return

    search_pattern = os.path.join(directory, "*")
    for filepath in glob.glob(search_pattern):
        if not os.path.isfile(filepath):
            continue
        try:
            created_at = datetime.fromtimestamp(_get_report_creation_timestamp(filepath))
            if created_at < cutoff_date:
                os.remove(filepath)
                logger.info("Deleted old report: %s", filepath)
        except Exception as exc:
            logger.error("Failed to delete old report %s: %s", filepath, exc)


def _get_report_creation_timestamp(filepath: str) -> float:
    try:
        created_at = os.path.getctime(filepath)
    except OSError:
        return os.path.getmtime(filepath)

    if os.name == "nt":
        return created_at

    try:
        return min(created_at, os.path.getmtime(filepath))
    except OSError:
        return created_at


def _enforce_backup_limits(backup_dir: str):
    if not os.path.isdir(backup_dir):
        return

    files = [
        os.path.join(backup_dir, name)
        for name in os.listdir(backup_dir)
        if name.lower().endswith(".db") and os.path.isfile(os.path.join(backup_dir, name))
    ]
    files.sort(key=os.path.getmtime, reverse=True)

    # Hard limit by count
    for old_path in files[BACKUP_MAX_COUNT:]:
        try:
            _remove_backup_with_meta(old_path)
            logger.info("Deleted backup by count-limit: %s", old_path)
        except Exception as exc:
            logger.warning("Failed to delete backup by count-limit %s: %s", old_path, exc)

    # Recompute after count cleanup
    files = [
        os.path.join(backup_dir, name)
        for name in os.listdir(backup_dir)
        if name.lower().endswith(".db") and os.path.isfile(os.path.join(backup_dir, name))
    ]
    files.sort(key=os.path.getmtime, reverse=True)

    total_size = sum(os.path.getsize(path) for path in files)
    if total_size <= BACKUP_MAX_TOTAL_BYTES:
        return

    for old_path in reversed(files):
        if total_size <= BACKUP_MAX_TOTAL_BYTES:
            break
        try:
            size = os.path.getsize(old_path)
            _remove_backup_with_meta(old_path)
            total_size -= size
            logger.info("Deleted backup by size-limit: %s", old_path)
        except Exception as exc:
            logger.warning("Failed to delete backup by size-limit %s: %s", old_path, exc)


def _remove_backup_with_meta(db_path: str):
    os.remove(db_path)
    meta_path = f"{db_path}.meta.json"
    if os.path.exists(meta_path):
        os.remove(meta_path)


def _prune_change_log_and_maybe_compact(db_path: str, now: datetime):
    if not os.path.exists(db_path):
        return None

    lock = FileWriteLock(DB_LOCK_PATH, stale_timeout_sec=10 * 60, logger=logger)
    if not lock.acquire(owner_id=f"{os.getpid()}:backup_cleanup", source="change_log_cleanup"):
        logger.warning("Change-log maintenance skipped: db.lock is busy.")
        return None

    conn = None
    deleted_rows = 0
    rows_before = 0
    rows_after = 0
    compacted = False

    try:
        conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None, timeout=2.0)
        configure_connection(conn)

        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='change_log'"
        ).fetchone()
        if not exists:
            return None

        rows_before = int(conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0] or 0)
        if rows_before <= 0:
            return {
                "rows_before": 0,
                "rows_after": 0,
                "deleted_rows": 0,
                "compacted": False,
            }

        cutoff_by_age = 0
        if CHANGE_LOG_RETENTION_DAYS > 0:
            cutoff_dt = now - timedelta(days=CHANGE_LOG_RETENTION_DAYS)
            cutoff_str = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")
            cutoff_by_age = int(
                conn.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM change_log WHERE changed_at < ?",
                    (cutoff_str,),
                ).fetchone()[0]
                or 0
            )

        cutoff_by_count = 0
        if CHANGE_LOG_MAX_ROWS > 0 and rows_before > CHANGE_LOG_MAX_ROWS:
            overflow = rows_before - CHANGE_LOG_MAX_ROWS
            row = conn.execute(
                "SELECT id FROM change_log ORDER BY id ASC LIMIT 1 OFFSET ?",
                (max(0, overflow - 1),),
            ).fetchone()
            cutoff_by_count = int(row[0]) if row and row[0] is not None else 0

        cutoff_id = max(cutoff_by_age, cutoff_by_count)
        if cutoff_id <= 0:
            rows_after = rows_before
            return {
                "rows_before": rows_before,
                "rows_after": rows_after,
                "deleted_rows": 0,
                "compacted": False,
            }

        conn.execute("BEGIN IMMEDIATE")
        while True:
            cursor = conn.execute(
                """
                DELETE FROM change_log
                WHERE id IN (
                    SELECT id FROM change_log
                    WHERE id <= ?
                    ORDER BY id ASC
                    LIMIT ?
                )
                """,
                (cutoff_id, CHANGE_LOG_PRUNE_BATCH),
            )
            changed = int(cursor.rowcount or 0)
            if changed <= 0:
                break
            deleted_rows += changed
            if changed < CHANGE_LOG_PRUNE_BATCH:
                break
        conn.execute("COMMIT")

        rows_after = int(conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0] or 0)

        if deleted_rows > 0:
            compacted = _maybe_compact_db(conn, db_path, now)

        return {
            "rows_before": rows_before,
            "rows_after": rows_after,
            "deleted_rows": deleted_rows,
            "compacted": compacted,
        }
    except sqlite3.OperationalError as exc:
        if conn and conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
        if _is_locked_error(exc):
            logger.warning("Change-log maintenance skipped due to DB lock: %s", exc)
            return None
        raise
    finally:
        if conn:
            conn.close()
        lock.release()


def _maybe_compact_db(conn: sqlite3.Connection, db_path: str, now: datetime) -> bool:
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0] or 0)
    freelist_count = int(conn.execute("PRAGMA freelist_count").fetchone()[0] or 0)
    free_bytes = page_size * freelist_count

    if free_bytes < CHANGE_LOG_COMPACT_MIN_FREE_BYTES:
        return False
    if not _compact_due(now):
        return False

    try:
        before_size = os.path.getsize(db_path)
        conn.execute("VACUUM")
        after_size = os.path.getsize(db_path)
        _mark_compact_ts(now)
        logger.info(
            "SQLite compacted after change-log prune: before=%s MB, after=%s MB",
            round(before_size / (1024 * 1024), 2),
            round(after_size / (1024 * 1024), 2),
        )
        return True
    except sqlite3.OperationalError as exc:
        if _is_locked_error(exc):
            logger.warning("SQLite compact skipped due to lock: %s", exc)
            return False
        logger.warning("SQLite compact failed: %s", exc)
        return False


def _compact_due(now: datetime) -> bool:
    try:
        if not os.path.exists(CHANGE_LOG_COMPACT_STAMP_PATH):
            return True
        with open(CHANGE_LOG_COMPACT_STAMP_PATH, "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
        last_ts = float(raw)
        return (time.time() - last_ts) >= CHANGE_LOG_COMPACT_MIN_INTERVAL_SEC
    except Exception:
        return True


def _mark_compact_ts(now: datetime):
    try:
        with open(CHANGE_LOG_COMPACT_STAMP_PATH, "w", encoding="utf-8") as fh:
            fh.write(str(time.time()))
    except Exception as exc:
        logger.debug("Failed to persist compact timestamp: %s", exc)


def _is_locked_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in LOCKED_ERROR_MARKERS)
