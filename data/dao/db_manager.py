import os
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, Optional

from rem_card.app.db_lifecycle import DB_CYCLE_META_KEY, maybe_rotate_database_if_due
from rem_card.app.durable_sql_outbox import (
    DeferredWriteCursor,
    DurableSqlOutbox,
    RecordingCursor,
    is_corruption_write_error,
    is_retryable_write_error,
)
from rem_card.app.db_availability import (
    is_database_unavailable_error,
    notify_database_unavailable,
)
from rem_card.app.logger import logger
from rem_card.app.local_replica_sync import LocalReplicaSync
from rem_card.app.local_metrics import record_metric
from rem_card.app.paths import (
    BAZA_DIR,
    BACKUPS_RC_DIR,
    BACKUPS_VALID_DIR,
    CLIENT_POLICY_PATH,
    DB_LOCK_PATH,
    DB_ROTATION_LOCK_PATH,
    INVALID_BACKUPS_DIR,
    LOCAL_CACHE_DIR,
    LOCAL_REMCARD_OUTBOX_PATH,
    LOCAL_REMCARD_REPLICA_PATH,
)
from rem_card.app.schema_migration_guard import ensure_unified_schema_with_migration_backup
from rem_card.app.startup_db_guard import (
    is_confirmed_db_corruption_reason,
    is_retryable_db_availability_reason,
    recover_shared_db_with_locks,
)
from rem_card.app.sqlite_shared import (
    FileWriteLock,
    SQLiteWriteController,
    backup_connection,
    configure_connection,
    find_latest_backup,
    list_backup_candidates,
    run_integrity_check,
    run_quick_check,
)


GLOBAL_CHANGELOG_ENTITIES = ("patients", "admissions", "beds", "operations", "diet_templates")
RUNTIME_BACKUP_PREFIXES = ("startup_", "periodic_", "backup_", "shutdown_")
MAX_RUNTIME_BACKUPS = max(5, int(os.environ.get("REMCARD_MAX_RUNTIME_BACKUPS", "6")))
RUNTIME_BACKUP_MAX_TOTAL_BYTES = max(
    128 * 1024 * 1024,
    int(float(os.environ.get("REMCARD_RUNTIME_BACKUP_MAX_TOTAL_GB", "0.75")) * 1024 * 1024 * 1024),
)
RUNTIME_BACKUP_RETENTION_DAYS = max(3, int(os.environ.get("REMCARD_RUNTIME_BACKUP_RETENTION_DAYS", "21")))
PERIODIC_BACKUP_INTERVAL_SEC = 10 * 60
INTEGRITY_CHECK_INTERVAL_SEC = 30 * 60
INTEGRITY_START_DELAY_SEC = 45
LOCAL_READ_AFTER_WRITE_GRACE_SEC = 1.5
OUTBOX_REPLAY_INTERVAL_SEC = 3.0
OUTBOX_MAX_ATTEMPTS = 80
OUTBOX_MAX_RETRY_DELAY_SEC = 120.0
OUTBOX_HEALTH_LOG_INTERVAL_SEC = max(
    10.0,
    float(os.environ.get("REMCARD_OUTBOX_HEALTH_LOG_INTERVAL_SEC", "60")),
)
OUTBOX_HEALTH_WARN_PENDING = max(
    1,
    int(os.environ.get("REMCARD_OUTBOX_HEALTH_WARN_PENDING", "40")),
)
DEFERRED_WRITE_FALLBACK_ENABLED = os.environ.get("REMCARD_DEFERRED_WRITE_FALLBACK", "0") == "1"
READ_LOCK_RETRIES = 2
READ_LOCK_RETRY_DELAY_SEC = 0.08
LOCKED_READ_LOG_INTERVAL_SEC = 15.0
STARTUP_QUICKCHECK_TTL_SEC = max(
    0.0,
    float(os.environ.get("REMCARD_STARTUP_QUICKCHECK_TTL_SEC", "120")),
)
STARTUP_QUICKCHECK_META_KEY = "startup_last_quick_check_ts"
SHUTDOWN_BACKUP_MIN_INTERVAL_SEC = max(
    0.0,
    float(os.environ.get("REMCARD_SHUTDOWN_BACKUP_MIN_INTERVAL_SEC", str(6 * 60 * 60))),
)
CHANGELOG_LIVE_TRIM_ENABLED = os.environ.get("REMCARD_CHANGELOG_LIVE_TRIM_ENABLED", "1") != "0"
CHANGELOG_LIVE_CAP_ROWS = max(20_000, int(os.environ.get("REMCARD_CHANGELOG_LIVE_CAP_ROWS", "120000")))
CHANGELOG_LIVE_TRIM_BATCH = max(1_000, int(os.environ.get("REMCARD_CHANGELOG_LIVE_TRIM_BATCH", "20000")))
CHANGELOG_LIVE_TRIM_INTERVAL_SEC = max(
    10.0,
    float(os.environ.get("REMCARD_CHANGELOG_LIVE_TRIM_INTERVAL_SEC", "90")),
)
CHANGELOG_LIVE_TRIM_ON_STARTUP = os.environ.get("REMCARD_CHANGELOG_LIVE_TRIM_ON_STARTUP", "0") == "1"
CHANGELOG_LIVE_TRIM_STARTUP_GRACE_SEC = max(
    0.0,
    float(os.environ.get("REMCARD_CHANGELOG_LIVE_TRIM_STARTUP_GRACE_SEC", "8.0")),
)
LOCAL_CACHE_RETENTION_DAYS = max(3, int(os.environ.get("REMCARD_LOCAL_CACHE_RETENTION_DAYS", "21")))
LOCAL_CACHE_MAX_FILES = max(20, int(os.environ.get("REMCARD_LOCAL_CACHE_MAX_FILES", "30")))
LOCAL_CACHE_CLEANUP_MIN_INTERVAL_SEC = max(
    60.0,
    float(os.environ.get("REMCARD_LOCAL_CACHE_CLEANUP_MIN_INTERVAL_SEC", "900")),
)


def _is_local_cache_path_writable(db_path: str) -> bool:
    dir_path = os.path.dirname(db_path) or "."
    try:
        os.makedirs(dir_path, exist_ok=True)
        probe = os.path.join(
            dir_path,
            f".remcard_write_probe_{os.getpid()}_{int(time.time() * 1000)}.tmp",
        )
        with open(probe, "wb") as fh:
            fh.write(b"1")
        os.remove(probe)
    except Exception:
        return False

    if os.path.exists(db_path):
        try:
            with open(db_path, "ab"):
                pass
        except Exception:
            return False
    return True


class DatabaseManager:
    def __init__(self, journal_db_path, remcard_db_path):
        self.journal_db_path = journal_db_path
        self.remcard_db_path = remcard_db_path or journal_db_path
        self.db_path = self.remcard_db_path
        self._periodic_backup_interval_sec = PERIODIC_BACKUP_INTERVAL_SEC
        self._last_backup_ts = time.time()
        self._integrity_stop_evt = threading.Event()
        self._integrity_thread: Optional[threading.Thread] = None
        self._local_first_enabled = os.environ.get("REMCARD_LOCAL_FIRST_SYNC", "0") == "1"
        self._local_sync_interval_sec = max(1.0, float(os.environ.get("REMCARD_LOCAL_SYNC_INTERVAL_SEC", "5")))
        self._local_replica: Optional[LocalReplicaSync] = None
        self._outbox_enabled = os.environ.get("REMCARD_LOCAL_OUTBOX_SYNC", "0") == "1"
        self._outbox_replay_interval_sec = max(1.0, float(os.environ.get("REMCARD_LOCAL_OUTBOX_REPLAY_SEC", str(OUTBOX_REPLAY_INTERVAL_SEC))))
        self._outbox: Optional[DurableSqlOutbox] = None
        self._outbox_stop_evt = threading.Event()
        self._outbox_wakeup_evt = threading.Event()
        self._outbox_thread: Optional[threading.Thread] = None
        self._node_id = f"{socket.gethostname()}:{os.getpid()}:rem_card"
        self._prefer_central_reads_until = 0.0
        self._outbox_health_log_interval_sec = OUTBOX_HEALTH_LOG_INTERVAL_SEC
        self._outbox_health_warn_pending = OUTBOX_HEALTH_WARN_PENDING
        self._last_outbox_health_log_ts = 0.0
        self._last_seen_change_cursor = 0
        self._last_locked_read_log_ts = 0.0
        self._changelog_live_trim_enabled = CHANGELOG_LIVE_TRIM_ENABLED
        self._changelog_live_cap_rows = CHANGELOG_LIVE_CAP_ROWS
        self._changelog_live_trim_batch = CHANGELOG_LIVE_TRIM_BATCH
        self._changelog_live_trim_interval_sec = CHANGELOG_LIVE_TRIM_INTERVAL_SEC
        self._last_changelog_live_trim_ts = 0.0
        self._changelog_live_trim_grace_until = time.time() + CHANGELOG_LIVE_TRIM_STARTUP_GRACE_SEC
        self._last_local_cache_cleanup_ts = 0.0
        self._thread_state = threading.local()
        self._central_io_lock = threading.RLock()

        self._maybe_rotate_db_lifecycle()

        owner_id = f"{socket.gethostname()}:{os.getpid()}:rem_card"
        self.write_controller = SQLiteWriteController(
            db_path=self.db_path,
            lock_path=DB_LOCK_PATH,
            owner_id=owner_id,
            logger=logger,
        )

        self._journal_conn: Optional[sqlite3.Connection] = None
        self._remcard_conn: Optional[sqlite3.Connection] = None

        self._init_connections()
        self._verify_quick_integrity_or_restore()
        self._init_unified_schema()
        self._ensure_cycle_meta_initialized()
        self._cleanup_local_cache_artifacts(force=True)
        self._start_outbox_replay()
        self._start_local_replica_sync()
        self._start_integrity_monitor()
        if CHANGELOG_LIVE_TRIM_ON_STARTUP:
            self._maybe_trim_change_log_live(force=True)

    def _maybe_rotate_db_lifecycle(self):
        result = maybe_rotate_database_if_due(
            db_path=self.db_path,
            archive_dir=os.path.dirname(self.db_path),
            rotation_lock_path=DB_ROTATION_LOCK_PATH,
            db_lock_path=DB_LOCK_PATH,
            logger=logger,
            max_age_days=180,
        )
        status = result.get("status")
        if status in ("rotated", "deferred_active_beds"):
            logger.warning("DB lifecycle status: %s | %s", status, result)
        elif status not in ("missing", "not_due", "rotation_lock_busy"):
            logger.info("DB lifecycle status: %s | %s", status, result)

    def _init_connections(self):
        logger.info("Initializing unified DB connection at %s", self.db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        profile_lock = FileWriteLock(DB_LOCK_PATH, stale_timeout_sec=10 * 60, logger=logger)
        owner_id = f"{socket.gethostname()}:{os.getpid()}:remcard_init"
        if not profile_lock.acquire(owner_id=owner_id, source="connection_profile"):
            raise sqlite3.OperationalError("Could not acquire db lock for connection profile")
        try:
            conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                isolation_level=None,
                timeout=5.0,
            )
            configure_connection(conn, profile="network")
        except Exception as exc:
            if is_database_unavailable_error(exc):
                raise notify_database_unavailable(exc, context="remcard_init", logger=logger) from exc
            raise
        finally:
            profile_lock.release()
        self._remcard_conn = conn
        self._journal_conn = conn

    def _reconnect(self):
        with self._central_io_lock:
            if self._remcard_conn:
                with self.write_controller.connection_guard(self._remcard_conn):
                    self._remcard_conn.close()
            self._init_connections()
        if self._local_replica:
            self._local_replica.trigger_fast_sync()

    @staticmethod
    def _is_managed_local_cache_file(name: str) -> bool:
        lower_name = str(name or "").lower()
        if lower_name.startswith("rao_journal_local_replica_"):
            return True
        if lower_name.startswith("remcard_outbox_"):
            return True
        if lower_name.startswith("journal_outbox_"):
            return True
        if ".sync_tmp." in lower_name:
            return True
        return False

    @staticmethod
    def _remove_file_with_sidecars(path: str) -> int:
        removed = 0
        for candidate in (path, f"{path}-wal", f"{path}-shm", f"{path}-journal"):
            try:
                if os.path.isfile(candidate):
                    os.remove(candidate)
                    removed += 1
            except Exception:
                continue
        return removed

    def _cleanup_local_cache_artifacts(self, force: bool = False):
        now = time.time()
        if not force and (now - self._last_local_cache_cleanup_ts) < LOCAL_CACHE_CLEANUP_MIN_INTERVAL_SEC:
            return
        self._last_local_cache_cleanup_ts = now

        if not os.path.isdir(LOCAL_CACHE_DIR):
            return

        keep_paths = {
            os.path.abspath(LOCAL_REMCARD_REPLICA_PATH),
            os.path.abspath(LOCAL_REMCARD_OUTBOX_PATH),
        }
        for base_path in list(keep_paths):
            keep_paths.add(f"{base_path}-wal")
            keep_paths.add(f"{base_path}-shm")
            keep_paths.add(f"{base_path}-journal")

        managed_files: list[str] = []
        for name in os.listdir(LOCAL_CACHE_DIR):
            full_path = os.path.join(LOCAL_CACHE_DIR, name)
            if not os.path.isfile(full_path):
                continue
            if not self._is_managed_local_cache_file(name):
                continue
            abs_path = os.path.abspath(full_path)
            if abs_path in keep_paths:
                continue
            managed_files.append(abs_path)

        if not managed_files:
            return

        retention_sec = float(LOCAL_CACHE_RETENTION_DAYS) * 86400.0
        removed_total = 0

        for path in managed_files:
            try:
                age_sec = max(0.0, now - os.path.getmtime(path))
            except Exception:
                age_sec = 0.0
            if age_sec < retention_sec:
                continue
            removed_total += self._remove_file_with_sidecars(path)

        remaining: list[str] = []
        for path in managed_files:
            if os.path.isfile(path):
                remaining.append(path)
        remaining.sort(key=os.path.getmtime, reverse=True)

        for old_path in remaining[LOCAL_CACHE_MAX_FILES:]:
            removed_total += self._remove_file_with_sidecars(old_path)

        if removed_total > 0:
            logger.info(
                "Local cache cleanup removed %s stale file(s) (retention=%s days, keep<=%s files).",
                removed_total,
                LOCAL_CACHE_RETENTION_DAYS,
                LOCAL_CACHE_MAX_FILES,
            )

    def _meta_table_exists(self) -> bool:
        if not self._remcard_conn:
            return False
        try:
            row = self._remcard_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
            ).fetchone()
            return bool(row)
        except Exception:
            return False

    def _read_startup_quickcheck_ts(self) -> Optional[int]:
        if not self._remcard_conn or not self._meta_table_exists():
            return None
        try:
            row = self._remcard_conn.execute(
                "SELECT value FROM meta WHERE key = ?",
                (STARTUP_QUICKCHECK_META_KEY,),
            ).fetchone()
        except Exception:
            return None
        if not row or row[0] is None:
            return None
        try:
            return int(row[0])
        except Exception:
            return None

    def _write_startup_quickcheck_ts(self, ts: Optional[int] = None):
        if not self._remcard_conn or not self._meta_table_exists():
            return
        value = int(time.time()) if ts is None else int(ts)
        try:
            self._remcard_conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (STARTUP_QUICKCHECK_META_KEY, value),
            )
        except Exception as exc:
            logger.debug("Failed to update startup quick_check marker: %s", exc)

    def _should_run_startup_quickcheck(self) -> tuple[bool, Optional[float]]:
        if STARTUP_QUICKCHECK_TTL_SEC <= 0:
            return True, None
        last_ts = self._read_startup_quickcheck_ts()
        if not last_ts:
            return True, None
        age_sec = max(0.0, time.time() - float(last_ts))
        return age_sec >= STARTUP_QUICKCHECK_TTL_SEC, age_sec

    def _verify_quick_integrity_or_restore(self):
        should_run, age_sec = self._should_run_startup_quickcheck()
        if not should_run:
            logger.info(
                "Skipping startup quick_check for %s (last=%.1fs ago, ttl=%.1fs)",
                self.db_path,
                float(age_sec or 0.0),
                STARTUP_QUICKCHECK_TTL_SEC,
            )
            return

        try:
            ok, result = run_quick_check(self._remcard_conn)
        except Exception as exc:
            result = str(exc)
            if is_database_unavailable_error(exc) or is_retryable_db_availability_reason(result):
                raise RuntimeError(f"Database quick_check could not run because DB is unavailable: {result}") from exc
            if not is_confirmed_db_corruption_reason(result):
                raise
            ok = False

        if ok:
            logger.info("SQLite quick_check passed for %s", self.db_path)
            self._write_startup_quickcheck_ts()
            return

        if not is_confirmed_db_corruption_reason(result):
            raise RuntimeError(f"Database quick_check failed without confirmed corruption: {result}")

        logger.error("SQLite quick_check failed for %s: %s", self.db_path, result)
        self._close_connections_for_restore()
        recovery_result = recover_shared_db_with_locks(
            baza_dir=BAZA_DIR,
            db_path=self.db_path,
            role=None,
            failure_reason=f"startup quick_check failed: {result}",
        )
        if not recovery_result.ok:
            raise RuntimeError(
                f"Database quick_check failed and safe recovery was not completed: {recovery_result.technical_reason or result}"
            )
        logger.warning("Auto-recovery selected source: %s", recovery_result.restored_from)
        if recovery_result.quarantine_path:
            logger.warning("Corrupted primary DB moved to quarantine: %s", recovery_result.quarantine_path)

        self._init_connections()

        ok, result = run_integrity_check(self._remcard_conn)
        if not ok:
            raise RuntimeError(f"Database restore failed integrity_check: {result}")

    def _close_connections_for_restore(self):
        if self._remcard_conn:
            try:
                self._remcard_conn.close()
            except Exception:
                pass
        self._remcard_conn = None
        self._journal_conn = None

    def _init_unified_schema(self):
        ensure_unified_schema_with_migration_backup(
            self._remcard_conn,
            db_path=self.db_path,
            backup_dir=BACKUPS_VALID_DIR,
            invalid_dir=INVALID_BACKUPS_DIR,
            policy_path=CLIENT_POLICY_PATH,
            role=None,
            baza_dir=BAZA_DIR,
            logger=logger,
            controller=self.write_controller,
            source="schema_init",
        )

    def _ensure_cycle_meta_initialized(self):
        try:
            with self.remcard_transaction(source="db_cycle_meta_init") as cursor:
                cursor.execute(
                    "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                    (DB_CYCLE_META_KEY, int(time.time())),
                )
        except Exception as exc:
            logger.warning("Failed to initialize %s in meta: %s", DB_CYCLE_META_KEY, exc)

    def _start_integrity_monitor(self):
        if self._integrity_thread and self._integrity_thread.is_alive():
            return
        self._integrity_stop_evt.clear()
        self._integrity_thread = threading.Thread(
            target=self._integrity_monitor_worker,
            name="RemCardIntegrityMonitor",
            daemon=True,
        )
        self._integrity_thread.start()

    def _start_local_replica_sync(self):
        if not self._local_first_enabled:
            logger.info("Local-first sync is disabled by env REMCARD_LOCAL_FIRST_SYNC=0")
            return
        if not _is_local_cache_path_writable(LOCAL_REMCARD_REPLICA_PATH):
            self._local_first_enabled = False
            logger.warning(
                "Local-first sync disabled: local replica path is not writable (%s)",
                LOCAL_REMCARD_REPLICA_PATH,
            )
            return
        try:
            self._local_replica = LocalReplicaSync(
                central_db_path=self.db_path,
                local_db_path=LOCAL_REMCARD_REPLICA_PATH,
                logger=logger,
                sync_interval_sec=self._local_sync_interval_sec,
            )
            self._local_replica.start()
            logger.info("Local-first sync enabled (interval=%ss, local=%s)", self._local_sync_interval_sec, LOCAL_REMCARD_REPLICA_PATH)
        except Exception as exc:
            self._local_replica = None
            logger.warning("Failed to enable local-first sync replica: %s", exc)

    def _stop_local_replica_sync(self):
        if self._local_replica:
            try:
                self._local_replica.stop()
            except Exception as exc:
                logger.warning("Failed to stop local replica sync: %s", exc)
        self._local_replica = None

    def _start_outbox_replay(self):
        if not self._outbox_enabled:
            logger.info("Local outbox sync is disabled by env REMCARD_LOCAL_OUTBOX_SYNC=0")
            return
        if not _is_local_cache_path_writable(LOCAL_REMCARD_OUTBOX_PATH):
            self._outbox_enabled = False
            logger.warning(
                "Durable outbox disabled: outbox path is not writable (%s)",
                LOCAL_REMCARD_OUTBOX_PATH,
            )
            return
        try:
            self._outbox = DurableSqlOutbox(LOCAL_REMCARD_OUTBOX_PATH, logger=logger)
        except Exception as exc:
            self._outbox = None
            logger.warning("Failed to initialize durable outbox: %s", exc)
            return

        if self._outbox_thread and self._outbox_thread.is_alive():
            return

        self._outbox_stop_evt.clear()
        self._outbox_thread = threading.Thread(
            target=self._outbox_replay_worker,
            name="RemCardOutboxReplay",
            daemon=True,
        )
        self._outbox_thread.start()
        logger.info("Durable outbox replay enabled (interval=%ss, path=%s)", self._outbox_replay_interval_sec, LOCAL_REMCARD_OUTBOX_PATH)

    def _stop_outbox_replay(self):
        self._outbox_stop_evt.set()
        self._outbox_wakeup_evt.set()
        if self._outbox_thread and self._outbox_thread.is_alive():
            self._outbox_thread.join(timeout=2.0)
        self._outbox_thread = None
        self._outbox = None

    def _outbox_replay_worker(self):
        while not self._outbox_stop_evt.is_set():
            self._drain_outbox_once()
            if self._outbox_stop_evt.is_set():
                return
            if self._outbox_wakeup_evt.wait(self._outbox_replay_interval_sec):
                self._outbox_wakeup_evt.clear()

    def _drain_outbox_once(self, limit: int = 20):
        if not self._outbox:
            return
        try:
            operations = self._outbox.fetch_ready(limit=limit)
        except Exception as exc:
            logger.warning("Failed to fetch outbox operations: %s", exc)
            return

        applied_row_ids: list[int] = []
        retry_rows: list[tuple[int, int, str, float]] = []
        failed_rows: list[tuple[int, str]] = []

        for operation in operations:
            if self._outbox_stop_evt.is_set():
                return
            try:
                applied = self._apply_outbox_operation(operation)
                if applied:
                    applied_row_ids.append(operation.row_id)
                else:
                    attempts = operation.attempts + 1
                    if attempts >= OUTBOX_MAX_ATTEMPTS:
                        failed_rows.append((operation.row_id, "Exceeded max retry attempts"))
                    else:
                        delay = min(OUTBOX_MAX_RETRY_DELAY_SEC, 1.5 * (2 ** min(attempts, 8)))
                        retry_rows.append((operation.row_id, attempts, "Central DB unavailable", delay))
            except Exception as exc:
                attempts = operation.attempts + 1
                if is_retryable_write_error(exc):
                    try:
                        self._reconnect()
                    except Exception:
                        pass
                if attempts >= OUTBOX_MAX_ATTEMPTS or not is_retryable_write_error(exc):
                    failed_rows.append((operation.row_id, str(exc)))
                    logger.error("Outbox operation marked failed (%s): %s", operation.op_id, exc)
                else:
                    delay = min(OUTBOX_MAX_RETRY_DELAY_SEC, 1.5 * (2 ** min(attempts, 8)))
                    retry_rows.append((operation.row_id, attempts, str(exc), delay))

        try:
            self._outbox.apply_replay_outcomes(
                applied_row_ids=applied_row_ids,
                retry_rows=retry_rows,
                failed_rows=failed_rows,
            )
        except Exception as exc:
            logger.warning("Failed to apply batched outbox replay outcomes: %s", exc)

        try:
            self._outbox.prune_applied(keep_last=3000)
        except Exception:
            pass
        self._maybe_log_outbox_health()

    def _apply_outbox_operation(self, operation) -> bool:
        if not self._remcard_conn:
            self._init_connections()

        with self._central_io_lock:
            with self.write_controller.transaction(self._remcard_conn, source=f"outbox_replay:{operation.source}") as cursor:
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO sync_applied_ops (op_id, source, node_id)
                    VALUES (?, ?, ?)
                    """,
                    (operation.op_id, operation.source, self._node_id),
                )
                if cursor.rowcount == 0:
                    return True

                for statement in operation.statements:
                    cursor.execute(statement.sql, tuple(statement.params))

            self._maybe_create_periodic_backup(source=f"outbox_replay:{operation.source}")
            self._after_write_committed()
        return True

    def _enqueue_outbox_fallback(self, statements: list[tuple[str, tuple]], source: str, exc: Exception) -> Optional[str]:
        if not self._outbox:
            return None
        if is_corruption_write_error(exc):
            logger.critical(
                "Central SQLite DB corruption detected; write will not be queued to outbox (source=%s): %s",
                source,
                exc,
            )
            return None
        if not is_retryable_write_error(exc):
            return None
        op_id = self._outbox.enqueue(statements, source=source)
        if not op_id:
            return None
        self._outbox_wakeup_evt.set()
        logger.warning(
            "Central DB unavailable. Write operation queued to durable outbox (source=%s, op_id=%s, statements=%s)",
            source,
            op_id,
            len(statements),
        )
        self._maybe_log_outbox_health(force=True)
        return op_id

    def _maybe_log_outbox_health(self, force: bool = False):
        if not self._outbox:
            return
        now = time.time()
        if not force and (now - self._last_outbox_health_log_ts) < self._outbox_health_log_interval_sec:
            return
        self._last_outbox_health_log_ts = now

        try:
            snapshot = self._outbox.get_health_snapshot()
        except Exception as exc:
            logger.debug("Failed to read outbox health snapshot: %s", exc)
            return

        pending = int(snapshot.get("pending", 0) or 0)
        failed = int(snapshot.get("failed", 0) or 0)
        if failed > 0 or pending >= self._outbox_health_warn_pending:
            logger.warning(
                "Outbox health: pending=%s retry_pending=%s failed=%s oldest_pending=%.1fs next_retry_in=%.1fs",
                pending,
                int(snapshot.get("pending_retry", 0) or 0),
                failed,
                float(snapshot.get("oldest_pending_age_sec", 0.0) or 0.0),
                float(snapshot.get("next_retry_in_sec", 0.0) or 0.0),
            )
            return

        if pending > 0:
            logger.info(
                "Outbox health: pending=%s retry_pending=%s oldest_pending=%.1fs",
                pending,
                int(snapshot.get("pending_retry", 0) or 0),
                float(snapshot.get("oldest_pending_age_sec", 0.0) or 0.0),
            )

    def get_sync_health_snapshot(self) -> dict[str, Any]:
        outbox_snapshot = {}
        if self._outbox:
            try:
                outbox_snapshot = self._outbox.get_health_snapshot()
            except Exception as exc:
                outbox_snapshot = {"error": str(exc)}

        replica_age_sec = 0.0
        if self._local_replica and self._local_replica.last_sync_ok_ts:
            replica_age_sec = max(0.0, time.time() - float(self._local_replica.last_sync_ok_ts))
        replica_snapshot = {
            "enabled": bool(self._local_replica),
            "last_sync_ok_ts": float(self._local_replica.last_sync_ok_ts) if self._local_replica else 0.0,
            "last_sync_age_sec": replica_age_sec,
            "last_sync_error": str(self._local_replica.last_sync_error or "") if self._local_replica else "",
            "sync_interval_sec": float(self._local_sync_interval_sec),
        }
        return {
            "replica": replica_snapshot,
            "outbox": outbox_snapshot,
            "local_reads_preferred_after_ts": float(self._prefer_central_reads_until),
        }

    def _after_write_committed(self):
        self._prefer_central_reads_until = time.time() + LOCAL_READ_AFTER_WRITE_GRACE_SEC
        if time.time() >= self._changelog_live_trim_grace_until:
            self._maybe_trim_change_log_live()
        self._cleanup_local_cache_artifacts(force=False)
        if self._local_replica and (time.time() - self._local_replica.last_sync_ok_ts) >= self._local_sync_interval_sec:
            self._local_replica.trigger_fast_sync()

    def _maybe_trim_change_log_live(self, force: bool = False):
        if not self._changelog_live_trim_enabled:
            return
        if not self._remcard_conn:
            return

        now = time.time()
        if not force and (now - self._last_changelog_live_trim_ts) < self._changelog_live_trim_interval_sec:
            return
        self._last_changelog_live_trim_ts = now

        try:
            row = self._fetch_one_central_with_retry(
                "SELECT id FROM change_log ORDER BY id DESC LIMIT 1 OFFSET ?",
                (self._changelog_live_cap_rows,),
            )
        except sqlite3.OperationalError as exc:
            if self._is_retryable_read_error(exc):
                self._log_locked_read_throttled("Live change_log trim skipped due to lock", exc)
                return
            logger.warning("Live change_log trim check failed: %s", exc)
            return
        except Exception as exc:
            logger.warning("Live change_log trim check failed: %s", exc)
            return

        if not row or row[0] is None:
            return

        cutoff_id = int(row[0])
        if cutoff_id <= 0:
            return

        deleted_total = 0
        for _ in range(8):
            try:
                with self._central_io_lock:
                    cursor = self.write_controller.execute(
                        self._remcard_conn,
                        """
                        DELETE FROM change_log
                        WHERE id IN (
                            SELECT id
                            FROM change_log
                            WHERE id <= ?
                            ORDER BY id ASC
                            LIMIT ?
                        )
                        """,
                        (cutoff_id, self._changelog_live_trim_batch),
                        source="changelog_live_trim",
                    )
            except sqlite3.OperationalError as exc:
                if is_retryable_write_error(exc):
                    logger.debug("Live change_log trim write skipped due to lock: %s", exc)
                    break
                logger.warning("Live change_log trim write failed: %s", exc)
                break
            except Exception as exc:
                logger.warning("Live change_log trim write failed: %s", exc)
                break

            changed = int(cursor.rowcount or 0)
            if changed <= 0:
                break
            deleted_total += changed
            if changed < self._changelog_live_trim_batch:
                break

        if deleted_total > 0:
            logger.info(
                "Live change_log trim deleted %s row(s) (cap=%s, cutoff_id=%s)",
                deleted_total,
                self._changelog_live_cap_rows,
                cutoff_id,
            )

    def _should_read_from_local(self) -> bool:
        if not self._local_replica:
            return False
        return time.time() >= self._prefer_central_reads_until

    def _current_thread_remcard_tx_depth(self) -> int:
        try:
            return int(getattr(self._thread_state, "remcard_tx_depth", 0) or 0)
        except Exception:
            return 0

    def _in_current_thread_remcard_transaction(self) -> bool:
        return self._current_thread_remcard_tx_depth() > 0

    @contextmanager
    def _mark_current_thread_remcard_transaction(self):
        depth = self._current_thread_remcard_tx_depth()
        self._thread_state.remcard_tx_depth = depth + 1
        try:
            yield
        finally:
            if depth <= 0:
                try:
                    delattr(self._thread_state, "remcard_tx_depth")
                except AttributeError:
                    pass
            else:
                self._thread_state.remcard_tx_depth = depth

    def _open_readonly_central_connection(self) -> sqlite3.Connection:
        uri = f"file:{self.db_path}?mode=ro"
        conn = sqlite3.connect(
            uri,
            uri=True,
            check_same_thread=True,
            isolation_level=None,
            timeout=5.0,
        )
        configure_connection(conn, readonly=True, profile="network")
        return conn

    def _fetch_all_central(self, query, params=(), *, use_write_connection: bool = False):
        # Чтения внутри текущей транзакции должны видеть незакоммиченные строки.
        # Обычные фоновые чтения открывают отдельный read-only connection, чтобы
        # QThread-снимки не делили один sqlite3.Connection с очередью записи.
        # Central IO gate не дает network SQLite открывать read-only connection
        # одновременно с локальной write-транзакцией.
        try:
            if use_write_connection or self._in_current_thread_remcard_transaction():
                with self._central_io_lock:
                    with self.write_controller.connection_guard(self._remcard_conn):
                        cursor = self._remcard_conn.cursor()
                        cursor.execute(query, params)
                        return cursor.fetchall()

            with self._central_io_lock:
                conn = self._open_readonly_central_connection()
                try:
                    cursor = conn.cursor()
                    cursor.execute(query, params)
                    return cursor.fetchall()
                finally:
                    conn.close()
        except Exception as exc:
            if is_database_unavailable_error(exc):
                raise notify_database_unavailable(exc, context="remcard_read_all", logger=logger) from exc
            raise

    def _fetch_one_central(self, query, params=(), *, use_write_connection: bool = False):
        try:
            if use_write_connection or self._in_current_thread_remcard_transaction():
                with self._central_io_lock:
                    with self.write_controller.connection_guard(self._remcard_conn):
                        cursor = self._remcard_conn.cursor()
                        cursor.execute(query, params)
                        return cursor.fetchone()

            with self._central_io_lock:
                conn = self._open_readonly_central_connection()
                try:
                    cursor = conn.cursor()
                    cursor.execute(query, params)
                    return cursor.fetchone()
                finally:
                    conn.close()
        except Exception as exc:
            if is_database_unavailable_error(exc):
                raise notify_database_unavailable(exc, context="remcard_read_one", logger=logger) from exc
            raise

    @staticmethod
    def _is_retryable_read_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return "database is locked" in message or "database table is locked" in message

    def _fetch_one_central_with_retry(self, query, params=()):
        last_exc = None
        for attempt in range(1, READ_LOCK_RETRIES + 1):
            try:
                return self._fetch_one_central(query, params)
            except sqlite3.OperationalError as exc:
                if not self._is_retryable_read_error(exc):
                    raise
                last_exc = exc
                if attempt < READ_LOCK_RETRIES:
                    time.sleep(READ_LOCK_RETRY_DELAY_SEC * attempt)
                    continue
                raise
        if last_exc:
            raise last_exc
        return None

    def _fetch_all_central_with_retry(self, query, params=()):
        last_exc = None
        for attempt in range(1, READ_LOCK_RETRIES + 1):
            try:
                return self._fetch_all_central(query, params)
            except sqlite3.OperationalError as exc:
                if not self._is_retryable_read_error(exc):
                    raise
                last_exc = exc
                if attempt < READ_LOCK_RETRIES:
                    time.sleep(READ_LOCK_RETRY_DELAY_SEC * attempt)
                    continue
                raise
        if last_exc:
            raise last_exc
        return []

    def _log_locked_read_throttled(self, context: str, exc: Exception):
        now = time.time()
        if (now - self._last_locked_read_log_ts) < LOCKED_READ_LOG_INTERVAL_SEC:
            return
        self._last_locked_read_log_ts = now
        logger.warning("%s: %s", context, exc)

    def _integrity_monitor_worker(self):
        if self._integrity_stop_evt.wait(INTEGRITY_START_DELAY_SEC):
            return

        while not self._integrity_stop_evt.is_set():
            self._run_integrity_check_background_once()
            if self._integrity_stop_evt.wait(INTEGRITY_CHECK_INTERVAL_SEC):
                return

    def _run_integrity_check_background_once(self):
        conn = None
        try:
            conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                isolation_level=None,
                timeout=5.0,
            )
            configure_connection(conn, readonly=True, profile="network")
            ok, result = run_integrity_check(conn)
            if ok:
                logger.info("Background integrity_check passed for %s", self.db_path)
                return

            latest_backup = find_latest_backup(BACKUPS_RC_DIR)
            logger.critical(
                "Background integrity_check failed for %s: %s. Latest backup: %s",
                self.db_path,
                result,
                latest_backup or "not found",
            )
        except Exception as exc:
            logger.error("Background integrity_check failed to run: %s", exc, exc_info=True)
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    def _create_named_backup(self, prefix: str, source: str):
        if not self._remcard_conn:
            return None

        os.makedirs(BACKUPS_VALID_DIR, exist_ok=True)
        db_name = os.path.splitext(os.path.basename(self.db_path))[0]
        backup_name = f"{prefix}_{db_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        backup_path = os.path.join(BACKUPS_VALID_DIR, backup_name)
        try:
            with self._central_io_lock:
                with self.write_controller.connection_guard(self._remcard_conn):
                    backup_connection(
                        self._remcard_conn,
                        backup_path,
                        invalid_dir=INVALID_BACKUPS_DIR,
                        logger=logger,
                        lock_path=DB_LOCK_PATH,
                        source=f"{prefix}_backup",
                    )
            self._rotate_backups()
            self._last_backup_ts = time.time()
            logger.info("%s backup created (%s): %s", prefix.capitalize(), source, backup_path)
            return backup_path
        except Exception as exc:
            logger.warning("Failed to create %s backup (%s): %s", prefix, source, exc)
            return None

    def _create_shutdown_backup(self):
        if SHUTDOWN_BACKUP_MIN_INTERVAL_SEC > 0:
            latest_shutdown_backup = self._find_latest_runtime_backup_by_prefix("shutdown_")
            if latest_shutdown_backup:
                age_sec = max(0.0, time.time() - os.path.getmtime(latest_shutdown_backup))
                if age_sec < SHUTDOWN_BACKUP_MIN_INTERVAL_SEC:
                    logger.info(
                        "Skipping shutdown backup: latest shutdown backup is recent (%ss < %ss): %s",
                        round(age_sec, 1),
                        round(SHUTDOWN_BACKUP_MIN_INTERVAL_SEC, 1),
                        latest_shutdown_backup,
                    )
                    return
        self._create_named_backup(prefix="shutdown", source="close")

    def _maybe_create_periodic_backup(self, source: str = "write"):
        now = time.time()
        if now - self._last_backup_ts < self._periodic_backup_interval_sec:
            return
        self._create_named_backup(prefix="periodic", source=source)

    def _rotate_backups(self):
        files = self._list_runtime_backups()
        if not files:
            return

        files.sort(key=os.path.getmtime, reverse=True)

        now_ts = time.time()
        retention_sec = float(RUNTIME_BACKUP_RETENTION_DAYS) * 86400.0
        for old_file in list(files):
            try:
                age_sec = max(0.0, now_ts - os.path.getmtime(old_file))
            except Exception:
                age_sec = 0.0
            if age_sec < retention_sec:
                continue
            try:
                self._remove_backup_with_meta(old_file)
                files.remove(old_file)
            except Exception as exc:
                logger.warning("Failed to remove old backup %s: %s", old_file, exc)

        for old_file in files[MAX_RUNTIME_BACKUPS:]:
            try:
                self._remove_backup_with_meta(old_file)
            except Exception as exc:
                logger.warning("Failed to remove old backup %s: %s", old_file, exc)

        files = self._list_runtime_backups()
        files.sort(key=os.path.getmtime, reverse=True)

        total_size = sum(os.path.getsize(path) for path in files)
        if total_size <= RUNTIME_BACKUP_MAX_TOTAL_BYTES:
            return

        for old_file in reversed(files):
            if total_size <= RUNTIME_BACKUP_MAX_TOTAL_BYTES:
                break
            try:
                size = os.path.getsize(old_file)
                self._remove_backup_with_meta(old_file)
                total_size -= size
            except Exception as exc:
                logger.warning("Failed to remove old backup %s: %s", old_file, exc)

    @staticmethod
    def _find_latest_runtime_backup_by_prefix(prefix: str) -> Optional[str]:
        candidates = list_backup_candidates(BACKUPS_RC_DIR, prefix=prefix)
        if not candidates:
            return None
        candidates.sort(key=os.path.getmtime, reverse=True)
        return candidates[0]

    @staticmethod
    def _list_runtime_backups() -> list[str]:
        return [
            path
            for path in list_backup_candidates(BACKUPS_RC_DIR)
            if os.path.basename(path).startswith(RUNTIME_BACKUP_PREFIXES)
        ]

    @staticmethod
    def _remove_backup_with_meta(db_path: str):
        os.remove(db_path)
        meta_path = f"{db_path}.meta.json"
        if os.path.exists(meta_path):
            os.remove(meta_path)

    @contextmanager
    def remcard_transaction(self, source: str = "remcard_tx"):
        statement_sink: list[tuple[str, tuple]] = []
        outer_transaction = not self._in_current_thread_remcard_transaction()
        try:
            with self._central_io_lock:
                with self.write_controller.transaction(self._remcard_conn, source=source) as cursor:
                    with self._mark_current_thread_remcard_transaction():
                        wrapped_cursor = RecordingCursor(cursor, statement_sink) if outer_transaction else cursor
                        yield wrapped_cursor
                if self._remcard_conn and outer_transaction:
                    self._maybe_create_periodic_backup(source=source)
                    self._after_write_committed()
        except (sqlite3.OperationalError, sqlite3.ProgrammingError, sqlite3.DatabaseError, OSError) as exc:
            if is_database_unavailable_error(exc):
                raise notify_database_unavailable(exc, context=f"remcard_transaction:{source}", logger=logger) from exc
            op_id = None
            if is_corruption_write_error(exc):
                logger.critical("Central SQLite DB corruption detected during transaction (source=%s): %s", source, exc)
            if DEFERRED_WRITE_FALLBACK_ENABLED and outer_transaction and statement_sink:
                op_id = self._enqueue_outbox_fallback(statement_sink, source=source, exc=exc)
            if op_id:
                return
            raise

    def run_write_operation(self, operation: Callable[[sqlite3.Cursor], Any], source: str = "write_operation"):
        with self.remcard_transaction(source=source) as cursor:
            return operation(cursor)

    def execute_remcard(self, query, params=(), source: str = "execute_remcard"):
        logger.debug("SQL RemCard Exec: %s | Params: %s", query, params)
        try:
            with self._central_io_lock:
                cursor = self.write_controller.execute(self._remcard_conn, query, params, source=source)
                if self._remcard_conn and not self._in_current_thread_remcard_transaction():
                    self._maybe_create_periodic_backup(source=source)
                    self._after_write_committed()
            return cursor
        except (sqlite3.OperationalError, sqlite3.ProgrammingError, sqlite3.DatabaseError, OSError) as exc:
            if is_database_unavailable_error(exc):
                raise notify_database_unavailable(exc, context=f"remcard_write:{source}", logger=logger) from exc
            if is_corruption_write_error(exc):
                logger.critical("Central SQLite DB corruption detected during write (source=%s): %s", source, exc)
            if is_retryable_write_error(exc):
                try:
                    self._reconnect()
                    with self._central_io_lock:
                        cursor = self.write_controller.execute(self._remcard_conn, query, params, source=f"{source}:reconnect")
                        if self._remcard_conn and not self._in_current_thread_remcard_transaction():
                            self._maybe_create_periodic_backup(source=f"{source}:reconnect")
                            self._after_write_committed()
                    return cursor
                except Exception:
                    pass
            op_id = None
            if DEFERRED_WRITE_FALLBACK_ENABLED:
                op_id = self._enqueue_outbox_fallback([(query, tuple(params or ()))], source=source, exc=exc)
            if op_id:
                return DeferredWriteCursor(op_id=op_id)
            raise

    def fetch_all_remcard(self, query, params=()):
        logger.debug("SQL RemCard FetchAll: %s | Params: %s", query, params)
        started = time.perf_counter()
        source = "central"
        status = "error"
        try:
            if self._in_current_thread_remcard_transaction():
                rows = self._fetch_all_central(query, params, use_write_connection=True)
                status = "ok"
                return rows
            if self._should_read_from_local():
                try:
                    rows = self._local_replica.fetch_all(query, params)
                    source = "local_replica"
                    status = "ok"
                    return rows
                except Exception as exc:
                    logger.debug("Local replica fetch_all failed, fallback to central: %s", exc)
            rows = self._fetch_all_central(query, params)
            status = "ok"
            return rows
        finally:
            record_metric(
                "read_duration_ms",
                round((time.perf_counter() - started) * 1000.0, 3),
                operation="fetch_all",
                source=source,
                status=status,
            )

    def fetch_one_remcard(self, query, params=()):
        logger.debug("SQL RemCard FetchOne: %s | Params: %s", query, params)
        started = time.perf_counter()
        source = "central"
        status = "error"
        try:
            if self._in_current_thread_remcard_transaction():
                row = self._fetch_one_central(query, params, use_write_connection=True)
                status = "ok"
                return row
            if self._should_read_from_local():
                try:
                    row = self._local_replica.fetch_one(query, params)
                    source = "local_replica"
                    status = "ok"
                    return row
                except Exception as exc:
                    logger.debug("Local replica fetch_one failed, fallback to central: %s", exc)
            row = self._fetch_one_central(query, params)
            status = "ok"
            return row
        finally:
            record_metric(
                "read_duration_ms",
                round((time.perf_counter() - started) * 1000.0, 3),
                operation="fetch_one",
                source=source,
                status=status,
            )

    def fetch_all_journal(self, query, params=()):
        """Compatibility alias for legacy journal callers."""
        return self.fetch_all_remcard(query, params)

    def fetch_one_journal(self, query, params=()):
        """Compatibility alias for legacy journal callers."""
        return self.fetch_one_remcard(query, params)

    def get_data_version(self) -> int:
        return self.get_latest_change_id()

    def get_latest_change_id(self, admission_id: Optional[int] = None, include_global: bool = True) -> int:
        query = "SELECT COALESCE(MAX(id), 0) FROM change_log"
        params = []
        if admission_id is not None:
            if include_global:
                # Вариант с OR по двум индексам заметно медленнее на больших change_log.
                # Считаем два MAX по покрывающим индексам и берем большее значение.
                placeholders = ",".join("?" for _ in GLOBAL_CHANGELOG_ENTITIES)
                query = f"""
                    WITH
                        by_admission AS (
                            SELECT COALESCE(MAX(id), 0) AS max_id
                            FROM change_log
                            WHERE admission_id = ?
                        ),
                        by_global AS (
                            SELECT COALESCE(MAX(id), 0) AS max_id
                            FROM change_log
                            WHERE entity_name IN ({placeholders})
                        )
                    SELECT
                        CASE
                            WHEN by_admission.max_id > by_global.max_id THEN by_admission.max_id
                            ELSE by_global.max_id
                        END
                    FROM by_admission, by_global
                """
                params = [admission_id, *GLOBAL_CHANGELOG_ENTITIES]
            else:
                query += " WHERE admission_id = ?"
                params = [admission_id]
        params_tuple = tuple(params)
        row = None
        used_central = False

        try:
            row = self._fetch_one_central_with_retry(query, params_tuple)
            used_central = True
        except sqlite3.OperationalError as exc:
            if not self._is_retryable_read_error(exc):
                raise
            self._log_locked_read_throttled("get_latest_change_id: central DB locked, using cached cursor", exc)
            if self._local_replica:
                try:
                    row = self._local_replica.fetch_one(query, params_tuple)
                except Exception as local_exc:
                    logger.debug("Local fallback get_latest_change_id failed after central lock: %s", local_exc)

        current = int(row[0]) if row and row[0] is not None else 0
        if current < self._last_seen_change_cursor:
            current = self._last_seen_change_cursor
        if self._local_replica and current > self._last_seen_change_cursor:
            self._last_seen_change_cursor = current
            if used_central:
                self._prefer_central_reads_until = time.time() + LOCAL_READ_AFTER_WRITE_GRACE_SEC
            if (time.time() - self._local_replica.last_sync_ok_ts) >= self._local_sync_interval_sec:
                self._local_replica.trigger_fast_sync()
        record_metric(
            "latest_change_id",
            current,
            admission_id=admission_id,
            include_global=include_global,
            source="central" if used_central else "fallback",
        )
        return current

    def fetch_changes_since(self, last_change_id: int, admission_id: Optional[int] = None, include_global: bool = True):
        query = "SELECT * FROM change_log WHERE id > ?"
        params = [last_change_id]
        if admission_id is not None:
            if include_global:
                placeholders = ",".join("?" for _ in GLOBAL_CHANGELOG_ENTITIES)
                query += f" AND (admission_id = ? OR entity_name IN ({placeholders}))"
                params.extend([admission_id, *GLOBAL_CHANGELOG_ENTITIES])
            else:
                query += " AND admission_id = ?"
                params.append(admission_id)
        query += " ORDER BY id ASC"
        params_tuple = tuple(params)
        try:
            return self._fetch_all_central_with_retry(query, params_tuple)
        except sqlite3.OperationalError as exc:
            if not self._is_retryable_read_error(exc):
                raise
            self._log_locked_read_throttled("fetch_changes_since: central DB locked, trying local replica", exc)
            if self._local_replica:
                try:
                    cursor_row = self._local_replica.fetch_one("SELECT COALESCE(MAX(id), 0) FROM change_log")
                    local_cursor = int(cursor_row[0]) if cursor_row and cursor_row[0] is not None else 0
                    replica_age_sec = max(0.0, time.time() - float(self._local_replica.last_sync_ok_ts or 0.0))
                    if local_cursor <= int(last_change_id):
                        logger.warning(
                            "fetch_changes_since: local replica is stale after central lock (last_change_id=%s local_cursor=%s replica_age=%.1fs error=%s)",
                            last_change_id,
                            local_cursor,
                            replica_age_sec,
                            self._local_replica.last_sync_error,
                        )
                        raise sqlite3.OperationalError("local change_log replica is stale")
                    rows = self._local_replica.fetch_all(query, params_tuple)
                    logger.warning(
                        "fetch_changes_since: served change_log from local replica after central lock (last_change_id=%s local_cursor=%s rows=%s replica_age=%.1fs)",
                        last_change_id,
                        local_cursor,
                        len(rows),
                        replica_age_sec,
                    )
                    return rows
                except Exception as local_exc:
                    logger.debug("Local fallback fetch_changes_since failed after central lock: %s", local_exc)
            raise sqlite3.OperationalError("fetch_changes_since unavailable after central lock and local fallback") from exc

    def get_changed_entities_since(
        self,
        last_change_id: int,
        admission_id: Optional[int] = None,
        include_global: bool = True,
    ) -> set[str]:
        query = "SELECT DISTINCT entity_name FROM change_log WHERE id > ?"
        params = [last_change_id]
        if admission_id is not None:
            if include_global:
                placeholders = ",".join("?" for _ in GLOBAL_CHANGELOG_ENTITIES)
                query += f" AND (admission_id = ? OR entity_name IN ({placeholders}))"
                params.extend([admission_id, *GLOBAL_CHANGELOG_ENTITIES])
            else:
                query += " AND admission_id = ?"
                params.append(admission_id)

        params_tuple = tuple(params)
        rows = []
        try:
            rows = self._fetch_all_central_with_retry(query, params_tuple)
        except sqlite3.OperationalError as exc:
            if not self._is_retryable_read_error(exc):
                raise
            self._log_locked_read_throttled("get_changed_entities_since: central DB locked, trying local replica", exc)
            if self._local_replica:
                try:
                    rows = self._local_replica.fetch_all(query, params_tuple)
                except Exception as local_exc:
                    logger.debug("Local fallback get_changed_entities_since failed after central lock: %s", local_exc)
        return {
            str(row[0])
            for row in rows
            if row and row[0] is not None
        }

    def checkpoint_wal(self):
        logger.info("WAL checkpoint skipped because WAL mode is disabled")

    def close(self):
        logger.info("Closing unified database connection")
        self._integrity_stop_evt.set()
        if self._integrity_thread and self._integrity_thread.is_alive():
            self._integrity_thread.join(timeout=1.5)
        self._integrity_thread = None
        self._stop_outbox_replay()
        self._stop_local_replica_sync()

        if self._remcard_conn:
            with self._central_io_lock:
                with self.write_controller.connection_guard(self._remcard_conn):
                    self._create_shutdown_backup()
                    self._remcard_conn.close()
        self._remcard_conn = None
        self._journal_conn = None
