import logging
import os
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

from rem_card.Rao_jornal.config.settings import ARCHIVE_DIR, BACKUP_DIR, MAX_BACKUPS, NUM_BEDS
from rem_card.app.db_lifecycle import DB_CYCLE_META_KEY, maybe_rotate_database_if_due
from rem_card.app.db_availability import (
    is_database_unavailable_error,
    notify_database_unavailable,
)
from rem_card.app.durable_sql_outbox import (
    DeferredWriteCursor,
    DurableSqlOutbox,
    RecordingCursor,
    is_corruption_write_error,
    is_retryable_write_error,
)
from rem_card.app.local_replica_sync import LocalReplicaSync
from rem_card.app.paths import (
    BACKUPS_VALID_DIR,
    CORRUPTED_DB_DIR,
    DB_LOCK_PATH,
    DB_ROTATION_LOCK_PATH,
    INVALID_BACKUPS_DIR,
    LOCAL_CACHE_DIR,
    LOCAL_JOURNAL_OUTBOX_PATH,
    LOCAL_JOURNAL_REPLICA_PATH,
)
from rem_card.app.sqlite_shared import (
    FileWriteLock,
    SQLiteWriteController,
    backup_connection,
    configure_connection,
    find_latest_backup,
    list_backup_candidates,
    restore_database,
    restore_from_best_available_source,
    run_integrity_check,
    run_quick_check,
)
from rem_card.app.unified_db_schema import ensure_unified_schema


INTEGRITY_CHECK_INTERVAL_SEC = 30 * 60
INTEGRITY_START_DELAY_SEC = 45
PERIODIC_BACKUP_INTERVAL_SEC = 10 * 60
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
STARTUP_QUICKCHECK_TTL_SEC = max(
    0.0,
    float(os.environ.get("REMCARD_STARTUP_QUICKCHECK_TTL_SEC", "120")),
)
STARTUP_QUICKCHECK_META_KEY = "startup_last_quick_check_ts"
JOURNAL_RUNTIME_BACKUP_RETENTION_DAYS = max(
    3,
    int(os.environ.get("REMCARD_RUNTIME_BACKUP_RETENTION_DAYS", "21")),
)
JOURNAL_RUNTIME_BACKUP_PREFIXES = ("backup_", "startup_", "periodic_", "shutdown_")
JOURNAL_RUNTIME_BACKUP_MAX_TOTAL_BYTES = max(
    128 * 1024 * 1024,
    int(float(os.environ.get("REMCARD_RUNTIME_BACKUP_MAX_TOTAL_GB", "0.75")) * 1024 * 1024 * 1024),
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
            f".journal_write_probe_{os.getpid()}_{int(time.time() * 1000)}.tmp",
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


class DBManager:
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.conn: Optional[sqlite3.Connection] = None
        self.db_path = self._get_current_year_db_path()
        self._backup_interval_sec = PERIODIC_BACKUP_INTERVAL_SEC
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
        self._node_id = f"{socket.gethostname()}:{os.getpid()}:rao_jornal"
        self._prefer_central_reads_until = 0.0
        self._outbox_health_log_interval_sec = OUTBOX_HEALTH_LOG_INTERVAL_SEC
        self._outbox_health_warn_pending = OUTBOX_HEALTH_WARN_PENDING
        self._last_outbox_health_log_ts = 0.0
        self._last_local_cache_cleanup_ts = 0.0

        self._maybe_rotate_db_lifecycle()

        self.write_controller = SQLiteWriteController(
            db_path=self.db_path,
            lock_path=DB_LOCK_PATH,
            owner_id=f"{socket.gethostname()}:{os.getpid()}:rao_jornal",
            logger=self.logger,
        )
        self._connect_db()
        self._verify_quick_integrity_or_restore()
        self._initialize_schema()
        self._ensure_cycle_meta_initialized()
        self._migrate_admission_uids()
        self._initialize_beds()
        self._cleanup_local_cache_artifacts(force=True)
        self._start_outbox_replay()
        self._start_local_replica_sync()
        self._start_integrity_monitor()

    def _maybe_rotate_db_lifecycle(self):
        result = maybe_rotate_database_if_due(
            db_path=self.db_path,
            archive_dir=os.path.dirname(self.db_path),
            rotation_lock_path=DB_ROTATION_LOCK_PATH,
            db_lock_path=DB_LOCK_PATH,
            logger=self.logger,
            max_age_days=180,
        )
        status = result.get("status")
        if status in ("rotated", "deferred_active_beds"):
            self.logger.warning("DB lifecycle status: %s | %s", status, result)
        elif status not in ("missing", "not_due", "rotation_lock_busy"):
            self.logger.info("DB lifecycle status: %s | %s", status, result)

    def _start_integrity_monitor(self):
        if self._integrity_thread and self._integrity_thread.is_alive():
            return
        self._integrity_stop_evt.clear()
        self._integrity_thread = threading.Thread(
            target=self._integrity_monitor_worker,
            name="JournalIntegrityMonitor",
            daemon=True,
        )
        self._integrity_thread.start()

    def _start_local_replica_sync(self):
        if not self._local_first_enabled:
            self.logger.info("Local-first sync is disabled by env REMCARD_LOCAL_FIRST_SYNC=0")
            return
        if not _is_local_cache_path_writable(LOCAL_JOURNAL_REPLICA_PATH):
            self._local_first_enabled = False
            self.logger.warning(
                "Journal local-first sync disabled: local replica path is not writable (%s)",
                LOCAL_JOURNAL_REPLICA_PATH,
            )
            return
        try:
            self._local_replica = LocalReplicaSync(
                central_db_path=self.db_path,
                local_db_path=LOCAL_JOURNAL_REPLICA_PATH,
                logger=self.logger,
                sync_interval_sec=self._local_sync_interval_sec,
            )
            self._local_replica.start()
            self.logger.info("Journal local-first sync enabled (interval=%ss, local=%s)", self._local_sync_interval_sec, LOCAL_JOURNAL_REPLICA_PATH)
        except Exception as exc:
            self._local_replica = None
            self.logger.warning("Failed to enable journal local-first sync replica: %s", exc)

    def _stop_local_replica_sync(self):
        if self._local_replica:
            try:
                self._local_replica.stop()
            except Exception as exc:
                self.logger.warning("Failed to stop journal local replica sync: %s", exc)
        self._local_replica = None

    def _start_outbox_replay(self):
        if not self._outbox_enabled:
            self.logger.info("Local outbox sync is disabled by env REMCARD_LOCAL_OUTBOX_SYNC=0")
            return
        if not _is_local_cache_path_writable(LOCAL_JOURNAL_OUTBOX_PATH):
            self._outbox_enabled = False
            self.logger.warning(
                "Journal durable outbox disabled: outbox path is not writable (%s)",
                LOCAL_JOURNAL_OUTBOX_PATH,
            )
            return
        try:
            self._outbox = DurableSqlOutbox(LOCAL_JOURNAL_OUTBOX_PATH, logger=self.logger)
        except Exception as exc:
            self._outbox = None
            self.logger.warning("Failed to initialize durable journal outbox: %s", exc)
            return

        if self._outbox_thread and self._outbox_thread.is_alive():
            return

        self._outbox_stop_evt.clear()
        self._outbox_thread = threading.Thread(
            target=self._outbox_replay_worker,
            name="JournalOutboxReplay",
            daemon=True,
        )
        self._outbox_thread.start()
        self.logger.info("Journal durable outbox replay enabled (interval=%ss, path=%s)", self._outbox_replay_interval_sec, LOCAL_JOURNAL_OUTBOX_PATH)

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
            self.logger.warning("Failed to fetch journal outbox operations: %s", exc)
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
                    self.logger.error("Journal outbox operation marked failed (%s): %s", operation.op_id, exc)
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
            self.logger.warning("Failed to apply batched journal outbox replay outcomes: %s", exc)

        try:
            self._outbox.prune_applied(keep_last=3000)
        except Exception:
            pass
        self._maybe_log_outbox_health()

    def _apply_outbox_operation(self, operation) -> bool:
        if not self.conn:
            self._connect_db()

        with self.write_controller.transaction(self.conn, source=f"journal_outbox_replay:{operation.source}") as cursor:
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

        self._maybe_create_periodic_backup(source=f"journal_outbox_replay:{operation.source}")
        self._after_write_committed()
        return True

    def _enqueue_outbox_fallback(self, statements: list[tuple[str, tuple]], source: str, exc: Exception) -> Optional[str]:
        if not self._outbox:
            return None
        if is_corruption_write_error(exc):
            self.logger.critical(
                "Central SQLite DB corruption detected; journal write will not be queued to outbox (source=%s): %s",
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
        self.logger.warning(
            "Central DB unavailable. Journal write queued to durable outbox (source=%s, op_id=%s, statements=%s)",
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
            self.logger.debug("Failed to read journal outbox health snapshot: %s", exc)
            return

        pending = int(snapshot.get("pending", 0) or 0)
        failed = int(snapshot.get("failed", 0) or 0)
        if failed > 0 or pending >= self._outbox_health_warn_pending:
            self.logger.warning(
                "Journal outbox health: pending=%s retry_pending=%s failed=%s oldest_pending=%.1fs next_retry_in=%.1fs",
                pending,
                int(snapshot.get("pending_retry", 0) or 0),
                failed,
                float(snapshot.get("oldest_pending_age_sec", 0.0) or 0.0),
                float(snapshot.get("next_retry_in_sec", 0.0) or 0.0),
            )
            return

        if pending > 0:
            self.logger.info(
                "Journal outbox health: pending=%s retry_pending=%s oldest_pending=%.1fs",
                pending,
                int(snapshot.get("pending_retry", 0) or 0),
                float(snapshot.get("oldest_pending_age_sec", 0.0) or 0.0),
            )

    def get_sync_health_snapshot(self) -> dict:
        outbox_snapshot = {}
        if self._outbox:
            try:
                outbox_snapshot = self._outbox.get_health_snapshot()
            except Exception as exc:
                outbox_snapshot = {"error": str(exc)}

        replica_snapshot = {
            "enabled": bool(self._local_replica),
            "last_sync_ok_ts": float(self._local_replica.last_sync_ok_ts) if self._local_replica else 0.0,
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
        self._cleanup_local_cache_artifacts(force=False)
        if self._local_replica and (time.time() - self._local_replica.last_sync_ok_ts) >= self._local_sync_interval_sec:
            self._local_replica.trigger_fast_sync()

    def _should_read_from_local(self) -> bool:
        if not self._local_replica:
            return False
        return time.time() >= self._prefer_central_reads_until

    def _fetch_all_central(self, query: str, parameters: tuple = ()):
        try:
            with self.write_controller.connection_guard(self.conn):
                cursor = self.conn.cursor()
                cursor.execute(query, parameters)
                return cursor.fetchall()
        except Exception as exc:
            if is_database_unavailable_error(exc):
                raise notify_database_unavailable(exc, context="journal_read_all", logger=self.logger) from exc
            raise

    def _fetch_one_central(self, query: str, parameters: tuple = ()):
        try:
            with self.write_controller.connection_guard(self.conn):
                cursor = self.conn.cursor()
                cursor.execute(query, parameters)
                return cursor.fetchone()
        except Exception as exc:
            if is_database_unavailable_error(exc):
                raise notify_database_unavailable(exc, context="journal_read_one", logger=self.logger) from exc
            raise

    def _fetch_all(self, query: str, parameters: tuple = ()):
        if self.conn and self.conn.in_transaction:
            return self._fetch_all_central(query, parameters)
        if self._should_read_from_local():
            try:
                return self._local_replica.fetch_all(query, parameters)
            except Exception as exc:
                self.logger.debug("Journal local replica fetch_all failed, fallback to central: %s", exc)
        return self._fetch_all_central(query, parameters)

    def _fetch_one(self, query: str, parameters: tuple = ()):
        if self.conn and self.conn.in_transaction:
            return self._fetch_one_central(query, parameters)
        if self._should_read_from_local():
            try:
                return self._local_replica.fetch_one(query, parameters)
            except Exception as exc:
                self.logger.debug("Journal local replica fetch_one failed, fallback to central: %s", exc)
        return self._fetch_one_central(query, parameters)

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
            configure_connection(conn, readonly=True)
            ok, result = run_integrity_check(conn)
            if ok:
                self.logger.info("Background integrity_check passed for %s", self.db_path)
                return

            latest_backup = find_latest_backup(BACKUP_DIR)
            self.logger.critical(
                "Background integrity_check failed for %s: %s. Latest backup: %s",
                self.db_path,
                result,
                latest_backup or "not found",
            )
        except Exception as exc:
            self.logger.error("Background integrity_check failed to run: %s", exc, exc_info=True)
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    def _ensure_cycle_meta_initialized(self):
        try:
            with self.write_transaction(source="journal_cycle_meta_init") as cursor:
                cursor.execute(
                    "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                    (DB_CYCLE_META_KEY, int(time.time())),
                )
        except Exception as exc:
            self.logger.warning("Failed to initialize %s in meta: %s", DB_CYCLE_META_KEY, exc)

    def _get_current_year_db_path(self):
        db_name = "rao_journal.db"
        db_path = os.path.join(ARCHIVE_DIR, db_name)

        current_year = datetime.now().year
        old_db_name = f"rao-{current_year}.db"
        old_db_path = os.path.join(ARCHIVE_DIR, old_db_name)

        if os.path.exists(old_db_path) and not os.path.exists(db_path):
            os.rename(old_db_path, db_path)

        return db_path

    def _connect_db(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        profile_lock = FileWriteLock(DB_LOCK_PATH, stale_timeout_sec=10 * 60, logger=self.logger)
        owner_id = f"{socket.gethostname()}:{os.getpid()}:journal_init"
        if not profile_lock.acquire(owner_id=owner_id, source="connection_profile"):
            raise sqlite3.OperationalError("Could not acquire db lock for connection profile")
        try:
            self.conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                isolation_level=None,
                timeout=5.0,
            )
            configure_connection(self.conn)
        except Exception as exc:
            if is_database_unavailable_error(exc):
                raise notify_database_unavailable(exc, context="journal_init", logger=self.logger) from exc
            raise
        finally:
            profile_lock.release()

    def _reconnect(self):
        if self.conn:
            self.conn.close()
        self._connect_db()
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
            os.path.abspath(LOCAL_JOURNAL_REPLICA_PATH),
            os.path.abspath(LOCAL_JOURNAL_OUTBOX_PATH),
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

        remaining = [path for path in managed_files if os.path.isfile(path)]
        remaining.sort(key=os.path.getmtime, reverse=True)
        for old_path in remaining[LOCAL_CACHE_MAX_FILES:]:
            removed_total += self._remove_file_with_sidecars(old_path)

        if removed_total > 0:
            self.logger.info(
                "Journal local cache cleanup removed %s stale file(s) (retention=%s days, keep<=%s files).",
                removed_total,
                LOCAL_CACHE_RETENTION_DAYS,
                LOCAL_CACHE_MAX_FILES,
            )

    def _meta_table_exists(self) -> bool:
        if not self.conn:
            return False
        try:
            row = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
            ).fetchone()
            return bool(row)
        except Exception:
            return False

    def _read_startup_quickcheck_ts(self) -> Optional[int]:
        if not self.conn or not self._meta_table_exists():
            return None
        try:
            row = self.conn.execute(
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
        if not self.conn or not self._meta_table_exists():
            return
        value = int(time.time()) if ts is None else int(ts)
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (STARTUP_QUICKCHECK_META_KEY, value),
            )
        except Exception as exc:
            self.logger.debug("Failed to update startup quick_check marker: %s", exc)

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
            self.logger.info(
                "Skipping startup quick_check for %s (last=%.1fs ago, ttl=%.1fs)",
                self.db_path,
                float(age_sec or 0.0),
                STARTUP_QUICKCHECK_TTL_SEC,
            )
            return

        ok, result = run_quick_check(self.conn)
        if ok:
            self.logger.info("SQLite quick_check passed for %s", self.db_path)
            self._write_startup_quickcheck_ts()
            return

        self.logger.error("SQLite quick_check failed for %s: %s", self.db_path, result)
        self._close_connection_for_restore()
        try:
            restored_from, quarantined_path = restore_from_best_available_source(
                db_path=self.db_path,
                backup_dir=BACKUP_DIR,
                preferred_sources=[LOCAL_JOURNAL_REPLICA_PATH],
                logger=self.logger,
                quarantine_dir=CORRUPTED_DB_DIR,
                failure_reason=f"startup quick_check failed: {result}",
            )
            self.logger.warning("Auto-recovery selected source: %s", restored_from)
            if quarantined_path:
                self.logger.warning("Corrupted primary DB moved to quarantine: %s", quarantined_path)
        except Exception as exc:
            raise RuntimeError(f"Database quick_check failed and no healthy recovery source was found: {result}") from exc

        self._connect_db()

        ok, result = run_integrity_check(self.conn)
        if not ok:
            raise RuntimeError(f"Database restore failed integrity_check: {result}")

    def _close_connection_for_restore(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = None

    def _initialize_schema(self):
        with self.write_transaction(source="rao_jornal_schema_init"):
            ensure_unified_schema(self.conn, logger=self.logger)

    def _migrate_admission_uids(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT id FROM patients WHERE admission_uid IS NULL")
        patients_without_uid = cursor.fetchall()
        if patients_without_uid:
            import uuid

            self.logger.warning("Patients without admission_uid detected. Starting migration.")
            with self.write_transaction(source="migrate_admission_uid") as tx:
                for patient_row in patients_without_uid:
                    patient_id = patient_row["id"]
                    new_uid = str(uuid.uuid4())
                    tx.execute("UPDATE patients SET admission_uid = ? WHERE id = ?", (new_uid, patient_id))

    def _initialize_beds(self):
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS beds (bed_number INTEGER PRIMARY KEY, status TEXT NOT NULL, current_admission_id INTEGER, FOREIGN KEY (current_admission_id) REFERENCES admissions(id))"
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_beds_current_admission ON beds(current_admission_id)")
        row = self.conn.execute("SELECT COUNT(*) FROM beds").fetchone()
        if row and row[0] == 0:
            with self.write_transaction(source="initialize_beds") as cursor:
                for i in range(1, NUM_BEDS + 1):
                    cursor.execute("INSERT INTO beds (bed_number, status) VALUES (?, ?)", (i, "FREE"))

    def _create_named_backup(self, prefix: str, source: str):
        if not self.conn:
            return None

        os.makedirs(BACKUPS_VALID_DIR, exist_ok=True)
        db_name = os.path.splitext(os.path.basename(self.db_path))[0]
        backup_name = f"{prefix}_{db_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        backup_path = os.path.join(BACKUPS_VALID_DIR, backup_name)
        try:
            backup_connection(
                self.conn,
                backup_path,
                invalid_dir=INVALID_BACKUPS_DIR,
                logger=self.logger,
                lock_path=DB_LOCK_PATH,
                source=f"{prefix}_backup",
            )
            self._rotate_backups()
            self._last_backup_ts = time.time()
            self.logger.info("%s backup created (%s): %s", prefix.capitalize(), source, backup_path)
            return backup_path
        except Exception as exc:
            self.logger.warning("Failed to create %s backup (%s): %s", prefix, source, exc)
            return None

    def _create_shutdown_backup(self):
        self._create_named_backup(prefix="shutdown", source="close")

    def _maybe_create_periodic_backup(self, source: str = "write"):
        now = time.time()
        if now - self._last_backup_ts < self._backup_interval_sec:
            return
        self._create_named_backup(prefix="periodic", source=source)

    @contextmanager
    def write_transaction(self, source: str = "rao_jornal_write"):
        statement_sink: list[tuple[str, tuple]] = []
        outer_transaction = bool(self.conn) and not self.conn.in_transaction
        try:
            with self.write_controller.transaction(self.conn, source=source) as cursor:
                wrapped_cursor = RecordingCursor(cursor, statement_sink) if outer_transaction else cursor
                yield wrapped_cursor
            if self.conn and not self.conn.in_transaction:
                self._maybe_create_periodic_backup(source=source)
                self._after_write_committed()
        except (sqlite3.OperationalError, sqlite3.ProgrammingError, sqlite3.DatabaseError, OSError) as exc:
            if is_database_unavailable_error(exc):
                raise notify_database_unavailable(exc, context=f"journal_transaction:{source}", logger=self.logger) from exc
            op_id = None
            if is_corruption_write_error(exc):
                self.logger.critical("Central SQLite DB corruption detected during journal transaction (source=%s): %s", source, exc)
            if DEFERRED_WRITE_FALLBACK_ENABLED and outer_transaction and statement_sink:
                op_id = self._enqueue_outbox_fallback(statement_sink, source=source, exc=exc)
            if op_id:
                return
            raise

    def execute_write(self, query: str, parameters: tuple = (), source: str = "rao_jornal_execute"):
        try:
            cursor = self.write_controller.execute(self.conn, query, parameters, source=source)
            if self.conn and not self.conn.in_transaction:
                self._maybe_create_periodic_backup(source=source)
                self._after_write_committed()
            return cursor
        except (sqlite3.OperationalError, sqlite3.ProgrammingError, sqlite3.DatabaseError, OSError) as exc:
            if is_database_unavailable_error(exc):
                raise notify_database_unavailable(exc, context=f"journal_write:{source}", logger=self.logger) from exc
            if is_corruption_write_error(exc):
                self.logger.critical("Central SQLite DB corruption detected during journal write (source=%s): %s", source, exc)
            if is_retryable_write_error(exc):
                try:
                    self._reconnect()
                    cursor = self.write_controller.execute(self.conn, query, parameters, source=f"{source}:reconnect")
                    if self.conn and not self.conn.in_transaction:
                        self._maybe_create_periodic_backup(source=f"{source}:reconnect")
                        self._after_write_committed()
                    return cursor
                except Exception:
                    pass
            op_id = None
            if DEFERRED_WRITE_FALLBACK_ENABLED:
                op_id = self._enqueue_outbox_fallback([(query, tuple(parameters or ()))], source=source, exc=exc)
            if op_id:
                return DeferredWriteCursor(op_id=op_id)
            raise

    def get_connection(self):
        return self.conn

    def close_connection(self):
        self._integrity_stop_evt.set()
        if self._integrity_thread and self._integrity_thread.is_alive():
            self._integrity_thread.join(timeout=1.5)
        self._integrity_thread = None
        self._stop_outbox_replay()
        self._stop_local_replica_sync()

        if self.conn:
            self._create_shutdown_backup()
            self.conn.close()
            self.conn = None

    def get_all_beds(self):
        return self._fetch_all("SELECT * FROM beds ORDER BY bed_number")

    def get_bed_by_number(self, bed_number: int):
        return self._fetch_one("SELECT * FROM beds WHERE bed_number = ?", (bed_number,))

    def get_beds_snapshot(self):
        query = """
            SELECT
                b.bed_number,
                b.status,
                b.current_admission_id,
                p.full_name,
                a.history_number,
                a.diagnosis_text
            FROM beds b
            LEFT JOIN admissions a ON a.id = b.current_admission_id
            LEFT JOIN patients p ON p.id = a.patient_id
            ORDER BY b.bed_number
        """
        return self._fetch_all(query)

    def update_bed_status(self, bed_number: int, status: str, admission_id: int = None):
        self.execute_write(
            "UPDATE beds SET status = ?, current_admission_id = ? WHERE bed_number = ?",
            (status, admission_id, bed_number),
            source="update_bed_status",
        )
        self._check_for_backup()

    def execute_query(self, query: str, parameters: tuple = ()):
        self.execute_write(query, parameters, source="execute_query")

    def _check_for_backup(self):
        try:
            count = self.conn.execute("SELECT COUNT(*) FROM admissions").fetchone()[0]
            if count > 0 and count % 10 == 0:
                self.create_backup()
        except Exception as exc:
            self.logger.error("Error checking for backup: %s", exc)

    def create_backup(self):
        os.makedirs(BACKUPS_VALID_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        db_filename = os.path.basename(self.db_path)
        backup_filename = f"backup_{timestamp}_{db_filename}"
        backup_path = os.path.join(BACKUPS_VALID_DIR, backup_filename)

        try:
            backup_connection(
                self.conn,
                backup_path,
                invalid_dir=INVALID_BACKUPS_DIR,
                logger=self.logger,
                lock_path=DB_LOCK_PATH,
                source="manual_backup",
            )
            self._rotate_backups()
            return True, f"Резервная копия успешно создана:\n{backup_path}"
        except Exception as exc:
            err_msg = f"Failed to create backup: {exc}"
            self.logger.error(err_msg)
            return False, err_msg

    def restore_backup(self, backup_path):
        if not os.path.exists(backup_path):
            return False, "Файл бэкапа не найден."

        try:
            self.close_connection()
            restore_database(self.db_path, backup_path)
            self._connect_db()
            self._initialize_schema()
            self._initialize_beds()
            self._start_outbox_replay()
            self._start_local_replica_sync()
            self._start_integrity_monitor()
            return True, "База данных успешно восстановлена."
        except Exception as exc:
            err_msg = f"Ошибка при восстановлении базы данных:\n{exc}"
            self.logger.error(err_msg)
            try:
                self._connect_db()
                self._start_outbox_replay()
                self._start_local_replica_sync()
                self._start_integrity_monitor()
            except Exception:
                pass
            return False, err_msg

    def _rotate_backups(self):
        files = self._list_runtime_backups()
        if not files:
            return

        files.sort(key=os.path.getmtime, reverse=True)

        now_ts = time.time()
        retention_sec = float(JOURNAL_RUNTIME_BACKUP_RETENTION_DAYS) * 86400.0
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
                self.logger.error("Error removing old backup %s: %s", old_file, exc)

        for old_file in files[MAX_BACKUPS:]:
            try:
                self._remove_backup_with_meta(old_file)
            except Exception as exc:
                self.logger.error("Error removing old backup %s: %s", old_file, exc)

        files = self._list_runtime_backups()
        files.sort(key=os.path.getmtime, reverse=True)
        total_size = sum(os.path.getsize(path) for path in files)
        if total_size <= JOURNAL_RUNTIME_BACKUP_MAX_TOTAL_BYTES:
            return

        for old_file in reversed(files):
            if total_size <= JOURNAL_RUNTIME_BACKUP_MAX_TOTAL_BYTES:
                break
            try:
                size = os.path.getsize(old_file)
                self._remove_backup_with_meta(old_file)
                total_size -= size
            except Exception as exc:
                self.logger.error("Error removing old backup %s: %s", old_file, exc)

    @staticmethod
    def _list_runtime_backups() -> list[str]:
        return [
            path
            for path in list_backup_candidates(BACKUP_DIR)
            if os.path.basename(path).startswith(JOURNAL_RUNTIME_BACKUP_PREFIXES)
        ]

    @staticmethod
    def _remove_backup_with_meta(db_path: str):
        os.remove(db_path)
        meta_path = f"{db_path}.meta.json"
        if os.path.exists(meta_path):
            os.remove(meta_path)

    def get_active_admissions(self) -> list:
        rows = self._fetch_all(
            """
            SELECT
                p.admission_uid,
                p.full_name,
                a.bed_number,
                a.history_number
            FROM patients p
            JOIN admissions a ON p.id = a.patient_id
            JOIN beds b ON a.id = b.current_admission_id
            WHERE b.status = 'OCCUPIED'
            """
        )
        return [
            {
                "admission_uid": row["admission_uid"],
                "full_name": row["full_name"],
                "bed_number": row["bed_number"],
                "history_number": row["history_number"],
            }
            for row in rows
        ]

    def is_admission_active(self, admission_uid: str) -> bool:
        row = self._fetch_one(
            """
            SELECT COUNT(b.bed_number)
            FROM patients p
            JOIN admissions a ON p.id = a.patient_id
            JOIN beds b ON a.id = b.current_admission_id
            WHERE p.admission_uid = ? AND b.status = 'OCCUPIED'
            """,
            (admission_uid,),
        )
        count = row[0] if row else 0
        return count > 0

    def get_patient_by_uid(self, admission_uid: str) -> Optional[dict]:
        row = self._fetch_one("SELECT * FROM patients WHERE admission_uid = ?", (admission_uid,))
        if row:
            return dict(row)
        return None

    def get_all_admissions(self) -> list[dict]:
        rows = self._fetch_all(
            """
            SELECT
                a.id,
                p.admission_uid,
                p.full_name,
                a.bed_number,
                a.history_number,
                a.admission_datetime,
                a.patient_age,
                a.patient_months,
                a.patient_age_unit,
                a.patient_gender,
                a.diagnosis_code,
                a.diagnosis_text,
                a.department_profile,
                a.source_department,
                a.transfer_datetime,
                a.transfer_department,
                a.outcome,
                a.transfer_lpu,
                a.transfer_lpu_other,
                a.death_datetime,
                a.created_at,
                a.updated_at
            FROM patients p
            JOIN admissions a ON p.id = a.patient_id
            """
        )
        return [dict(row) for row in rows]
