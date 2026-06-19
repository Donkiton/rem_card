import json
import os
import random
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from rem_card.app.db_lifecycle import (
    DB_CYCLE_META_KEY,
    find_active_emergency_nurse_sessions,
    find_active_rotation_role_locks,
    maybe_rotate_database_if_due,
    rotate_database_now,
)
from rem_card.app.db_runtime_context import DbRuntimeContext, build_network_runtime_context
from rem_card.app.durable_sql_outbox import (
    DeferredWriteCursor,
    DurableSqlOutbox,
    RecordingCursor,
    is_corruption_write_error,
    is_retryable_write_error,
)
from rem_card.app.db_availability import (
    DatabaseClosedError,
    is_database_unavailable_error,
    notify_database_unavailable,
)
from rem_card.app.foreground_activity import foreground_activity_snapshot, should_defer_background_io
from rem_card.app.logger import logger
from rem_card.app.local_replica_sync import LocalReplicaSync
from rem_card.app.local_metrics import record_metric
from rem_card.app.maintenance_activity import active_maintenance_snapshot, maintenance_task
from rem_card.app.paths import (
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
from rem_card.app.unified_db_schema import (
    SCHEMA_FASTPATH_META_KEY,
    SCHEMA_FASTPATH_REV,
    SCHEMA_MIN_MIGRATION_VERSION,
)
from rem_card.app.sqlite_shared import (
    FileWriteLock,
    NETWORK_SAFE_DB_PROFILE,
    SQLiteWriteController,
    backup_connection,
    configure_connection,
    find_latest_backup,
    list_backup_candidates,
    run_integrity_check,
    run_quick_check,
)
from rem_card.app.version import APP_VERSION


_NETWORK_RUNTIME_CONTEXT = build_network_runtime_context()
BAZA_DIR = _NETWORK_RUNTIME_CONTEXT.baza_dir
BACKUP_HEALTH_DIR = _NETWORK_RUNTIME_CONTEXT.medical_backup_health_dir
BACKUPS_RC_DIR = _NETWORK_RUNTIME_CONTEXT.medical_backups_root_dir
BACKUPS_VALID_DIR = _NETWORK_RUNTIME_CONTEXT.medical_backups_valid_dir
CLIENT_POLICY_PATH = _NETWORK_RUNTIME_CONTEXT.medical_client_policy_path
DB_LOCK_PATH = _NETWORK_RUNTIME_CONTEXT.medical_db_lock_path
DB_ROTATION_LOCK_PATH = _NETWORK_RUNTIME_CONTEXT.medical_db_rotation_lock_path
INVALID_BACKUPS_DIR = _NETWORK_RUNTIME_CONTEXT.medical_invalid_backups_dir
QUARANTINE_DIR = _NETWORK_RUNTIME_CONTEXT.medical_quarantine_dir

GLOBAL_CHANGELOG_ENTITIES = ("patients", "admissions", "beds", "operations", "diet_templates")
RUNTIME_BACKUP_PREFIXES = ("startup_", "periodic_", "backup_", "shutdown_")
MAX_RUNTIME_BACKUPS = max(5, int(os.environ.get("REMCARD_MAX_RUNTIME_BACKUPS", "6")))
RUNTIME_BACKUP_MAX_TOTAL_BYTES = max(
    128 * 1024 * 1024,
    int(float(os.environ.get("REMCARD_RUNTIME_BACKUP_MAX_TOTAL_GB", "0.75")) * 1024 * 1024 * 1024),
)
RUNTIME_BACKUP_RETENTION_DAYS = max(3, int(os.environ.get("REMCARD_RUNTIME_BACKUP_RETENTION_DAYS", "21")))
PERIODIC_BACKUP_INTERVAL_SEC = 10 * 60
PERIODIC_BACKUP_FOREGROUND_IDLE_SEC = max(
    0.0,
    float(os.environ.get("REMCARD_PERIODIC_BACKUP_FOREGROUND_IDLE_SEC", "5")),
)
HEAVY_MAINTENANCE_FOREGROUND_IDLE_SEC = max(
    PERIODIC_BACKUP_FOREGROUND_IDLE_SEC,
    float(os.environ.get("REMCARD_HEAVY_MAINTENANCE_FOREGROUND_IDLE_SEC", "120")),
)
HEAVY_MAINTENANCE_STARTUP_GRACE_SEC = max(
    0.0,
    float(os.environ.get("REMCARD_HEAVY_MAINTENANCE_STARTUP_GRACE_SEC", "900")),
)
MAINTENANCE_DEFER_WARN_SEC = max(
    60.0,
    float(os.environ.get("REMCARD_MAINTENANCE_DEFER_WARN_SEC", "1800")),
)
CENTRAL_IO_LOCK_WAIT_WARN_MS = max(
    0.0,
    float(os.environ.get("REMCARD_CENTRAL_IO_LOCK_WAIT_WARN_MS", "250")),
)
INTEGRITY_CHECK_INTERVAL_SEC = 30 * 60
INTEGRITY_START_DELAY_SEC = 45
INTEGRITY_DEFER_RETRY_SEC = max(
    5.0,
    float(os.environ.get("REMCARD_INTEGRITY_DEFER_RETRY_SEC", "60")),
)
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
CONNECTION_PROFILE_LOCK_TIMEOUT_SEC = max(
    0.1,
    float(os.environ.get("REMCARD_CONNECTION_PROFILE_LOCK_TIMEOUT_SEC", "12")),
)
CONNECTION_PROFILE_LOCK_RETRY_MIN_SEC = max(
    0.01,
    float(os.environ.get("REMCARD_CONNECTION_PROFILE_LOCK_RETRY_MIN_SEC", "0.05")),
)
CONNECTION_PROFILE_LOCK_RETRY_MAX_SEC = max(
    CONNECTION_PROFILE_LOCK_RETRY_MIN_SEC,
    float(os.environ.get("REMCARD_CONNECTION_PROFILE_LOCK_RETRY_MAX_SEC", "0.15")),
)
STARTUP_QUICKCHECK_TTL_SEC = max(
    0.0,
    float(os.environ.get("REMCARD_STARTUP_QUICKCHECK_TTL_SEC", "120")),
)
STARTUP_GUARD_QUICKCHECK_ENV = "REMCARD_STARTUP_GUARD_QUICKCHECK_OK"
STARTUP_GUARD_QUICKCHECK_MAX_AGE_SEC = 10 * 60
STARTUP_QUICKCHECK_META_KEY = "startup_last_quick_check_ts"
STARTUP_QUICKCHECK_STATE_VERSION = 3
STARTUP_QUICKCHECK_STATE_PATH = os.path.join(BACKUP_HEALTH_DIR, "startup_quick_check_state.json")
STARTUP_QUICKCHECK_BACKGROUND_DELAY_SEC = max(
    0.0,
    float(os.environ.get("REMCARD_STARTUP_QUICKCHECK_BACKGROUND_DELAY_SEC", "20")),
)
STARTUP_QUICKCHECK_BACKGROUND_INTERVAL_SEC = max(
    60.0,
    float(os.environ.get("REMCARD_STARTUP_QUICKCHECK_BACKGROUND_INTERVAL_SEC", str(STARTUP_QUICKCHECK_TTL_SEC))),
)
STARTUP_QUICKCHECK_BACKGROUND_ENABLED = os.environ.get("REMCARD_STARTUP_QUICKCHECK_BACKGROUND_ENABLED", "0") == "1"
STARTUP_QUICKCHECK_IDLE_GRACE_SEC = max(
    0.0,
    float(os.environ.get("REMCARD_STARTUP_QUICKCHECK_IDLE_GRACE_SEC", "3")),
)
STARTUP_QUICKCHECK_SLOW_MS = max(
    1000.0,
    float(os.environ.get("REMCARD_STARTUP_QUICKCHECK_SLOW_MS", "2000")),
)
STARTUP_QUICKCHECK_SLOW_BACKOFF_SEC = max(
    STARTUP_QUICKCHECK_BACKGROUND_INTERVAL_SEC,
    float(os.environ.get("REMCARD_STARTUP_QUICKCHECK_SLOW_BACKOFF_SEC", "600")),
)
BACKGROUND_MAINTENANCE_IO_COOLDOWN_SEC = max(
    0.0,
    float(os.environ.get("REMCARD_BACKGROUND_MAINTENANCE_IO_COOLDOWN_SEC", "20")),
)
SHUTDOWN_BACKUP_MIN_INTERVAL_SEC = max(
    0.0,
    float(os.environ.get("REMCARD_SHUTDOWN_BACKUP_MIN_INTERVAL_SEC", str(6 * 60 * 60))),
)
SHUTDOWN_CENTRAL_IO_LOCK_TIMEOUT_SEC = max(
    0.1,
    float(os.environ.get("REMCARD_SHUTDOWN_CENTRAL_IO_LOCK_TIMEOUT_SEC", "3.0")),
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
    def __init__(
        self,
        journal_db_path,
        remcard_db_path=None,
        runtime_context: DbRuntimeContext | None = None,
    ):
        self.runtime_context = self._resolve_runtime_context(journal_db_path, remcard_db_path, runtime_context)
        self.journal_db_path = self.runtime_context.medical_db_path
        self.remcard_db_path = self.runtime_context.medical_db_path
        self.db_path = self.runtime_context.medical_db_path
        self.medical_db_lock_path = self.runtime_context.medical_db_lock_path
        self.medical_backups_valid_dir = self.runtime_context.medical_backups_valid_dir
        self.medical_backups_root_dir = self.runtime_context.medical_backups_root_dir
        self.medical_backup_health_dir = self.runtime_context.medical_backup_health_dir
        self.medical_invalid_backups_dir = self.runtime_context.medical_invalid_backups_dir
        self.medical_quarantine_dir = self.runtime_context.medical_quarantine_dir
        self.medical_db_rotation_lock_path = self.runtime_context.medical_db_rotation_lock_path
        self.medical_client_policy_path = self.runtime_context.medical_client_policy_path
        self.medical_startup_quickcheck_state_path = self.runtime_context.medical_startup_quickcheck_state_path
        self.baza_dir = self.runtime_context.baza_dir
        self._periodic_backup_interval_sec = PERIODIC_BACKUP_INTERVAL_SEC
        self._last_backup_ts = time.time()
        self._startup_ts = time.time()
        self._integrity_stop_evt = threading.Event()
        self._integrity_thread: Optional[threading.Thread] = None
        self._startup_quickcheck_stop_evt = threading.Event()
        self._startup_quickcheck_thread: Optional[threading.Thread] = None
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
        self._closed = False
        self.startup_metrics: dict[str, float] = {}
        self._startup_pre_connect_fingerprint: Optional[dict[str, Any]] = self._startup_db_fingerprint()
        self._startup_quickcheck_ok_ts: Optional[int] = None
        self._startup_quickcheck_skipped_ts: Optional[int] = None
        self._write_activity_lock = threading.Lock()
        self._active_write_count = 0
        self._last_write_activity_ts = 0.0
        self._write_queue_idle_probe: Optional[Callable[[], bool]] = None
        self._close_state_lock = threading.Lock()
        self._closing = False
        self._startup_quickcheck_next_allowed_ts = 0.0
        self._last_heavy_maintenance_ts = 0.0
        self._last_heavy_maintenance_source = ""
        self._maintenance_deferred_since: dict[str, float] = {}

        owner_id = f"{socket.gethostname()}:{os.getpid()}:rem_card"
        self.write_controller = SQLiteWriteController(
            db_path=self.db_path,
            lock_path=self.medical_db_lock_path,
            owner_id=owner_id,
            logger=logger,
        )

        self._journal_conn: Optional[sqlite3.Connection] = None
        self._remcard_conn: Optional[sqlite3.Connection] = None
        self._central_read_conns: dict[threading.Thread, sqlite3.Connection] = {}

        self._measure_startup_phase("connection_profile_ms", self._init_connections)
        self._verify_quick_integrity_or_restore()
        self._measure_startup_phase("schema_init_ms", self._init_unified_schema)
        self._ensure_cycle_meta_initialized()
        self._measure_startup_phase("cache_cleanup_ms", lambda: self._cleanup_local_cache_artifacts(force=True))
        self._start_outbox_replay()
        self._start_local_replica_sync()
        self._start_integrity_monitor()
        self._start_startup_quickcheck_updater()
        if CHANGELOG_LIVE_TRIM_ON_STARTUP:
            self._maybe_trim_change_log_live(force=True)

    @staticmethod
    def _resolve_runtime_context(
        journal_db_path,
        remcard_db_path=None,
        runtime_context: DbRuntimeContext | None = None,
    ) -> DbRuntimeContext:
        if runtime_context is not None:
            return runtime_context
        context = build_network_runtime_context()
        requested_db_path = remcard_db_path or journal_db_path or context.medical_db_path
        requested_db_path = os.path.abspath(os.path.normpath(str(requested_db_path)))
        if os.path.normcase(requested_db_path) == os.path.normcase(context.medical_db_path):
            return context
        return replace(context, medical_db_path=requested_db_path)

    def _record_startup_metric(self, name: str, value_ms: float):
        try:
            self.startup_metrics[name] = round(float(value_ms), 3)
        except Exception:
            pass

    def _measure_startup_phase(self, name: str, func: Callable[[], Any]):
        started = time.perf_counter()
        try:
            return func()
        finally:
            self._record_startup_metric(name, (time.perf_counter() - started) * 1000.0)

    def set_write_queue_idle_probe(self, probe: Optional[Callable[[], bool]]):
        self._write_queue_idle_probe = probe

    @contextmanager
    def _mark_write_activity(self):
        lock = getattr(self, "_write_activity_lock", None)
        if lock is None:
            yield
            return
        with lock:
            self._active_write_count = int(getattr(self, "_active_write_count", 0) or 0) + 1
            self._last_write_activity_ts = time.time()
        try:
            yield
        finally:
            with lock:
                self._active_write_count = max(0, int(getattr(self, "_active_write_count", 0) or 0) - 1)
                self._last_write_activity_ts = time.time()

    def _is_startup_quickcheck_idle(self) -> bool:
        if getattr(self, "_closed", False):
            return False
        stop_evt = getattr(self, "_startup_quickcheck_stop_evt", None)
        if stop_evt is not None and stop_evt.is_set():
            return False
        now_ts = time.time()
        next_allowed = float(getattr(self, "_startup_quickcheck_next_allowed_ts", 0.0) or 0.0)
        if next_allowed > now_ts:
            remaining_sec = max(0.0, next_allowed - now_ts)
            record_metric(
                "startup_quick_check_deferred_slow_backoff",
                1,
                remaining_sec=round(remaining_sec, 3),
            )
            return False
        cooldown_remaining = self._maintenance_io_cooldown_remaining("startup_quick_check")
        if cooldown_remaining > 0:
            record_metric(
                "startup_quick_check_deferred_maintenance_cooldown",
                1,
                remaining_sec=round(cooldown_remaining, 3),
                last_source=getattr(self, "_last_heavy_maintenance_source", ""),
            )
            return False
        lock = getattr(self, "_write_activity_lock", None)
        if lock is not None:
            with lock:
                if int(getattr(self, "_active_write_count", 0) or 0) > 0:
                    return False
                last_write_ts = float(getattr(self, "_last_write_activity_ts", 0.0) or 0.0)
            if last_write_ts and (time.time() - last_write_ts) < STARTUP_QUICKCHECK_IDLE_GRACE_SEC:
                return False
        probe = getattr(self, "_write_queue_idle_probe", None)
        if probe is not None:
            try:
                if not bool(probe()):
                    return False
            except Exception as exc:
                logger.debug("Startup quick_check idle probe failed: %s", exc)
                return False
        should_defer, reason, age_sec = should_defer_background_io(
            idle_window_sec=max(STARTUP_QUICKCHECK_IDLE_GRACE_SEC, PERIODIC_BACKUP_FOREGROUND_IDLE_SEC),
            names=None,
        )
        if should_defer:
            logger.info(
                "Background startup quick_check deferred: foreground read is active/recent (reason=%s age_sec=%s idle_window_sec=%.1f)",
                reason,
                None if age_sec is None else round(age_sec, 3),
                max(STARTUP_QUICKCHECK_IDLE_GRACE_SEC, PERIODIC_BACKUP_FOREGROUND_IDLE_SEC),
            )
            self._record_maintenance_deferred(
                "startup_quick_check",
                f"foreground_activity:{reason}",
                source="startup_quick_check",
                age_sec=None if age_sec is None else round(age_sec, 3),
                idle_window_sec=max(STARTUP_QUICKCHECK_IDLE_GRACE_SEC, PERIODIC_BACKUP_FOREGROUND_IDLE_SEC),
            )
            record_metric(
                "startup_quick_check_deferred_foreground_read",
                1,
                reason=reason,
                age_sec=None if age_sec is None else round(age_sec, 3),
                idle_window_sec=max(STARTUP_QUICKCHECK_IDLE_GRACE_SEC, PERIODIC_BACKUP_FOREGROUND_IDLE_SEC),
            )
            return False
        return True

    def _maintenance_io_cooldown_remaining(self, source: str) -> float:
        cooldown_sec = float(BACKGROUND_MAINTENANCE_IO_COOLDOWN_SEC or 0.0)
        if cooldown_sec <= 0:
            return 0.0
        last_ts = float(getattr(self, "_last_heavy_maintenance_ts", 0.0) or 0.0)
        if last_ts <= 0:
            return 0.0
        last_source = str(getattr(self, "_last_heavy_maintenance_source", "") or "")
        if last_source == str(source or ""):
            return 0.0
        elapsed = max(0.0, time.time() - last_ts)
        return max(0.0, cooldown_sec - elapsed)

    def _mark_heavy_maintenance_io(self, source: str) -> None:
        self._last_heavy_maintenance_ts = time.time()
        self._last_heavy_maintenance_source = str(source or "maintenance")

    def _write_activity_defer_reason(self, *, idle_grace_sec: float = LOCAL_READ_AFTER_WRITE_GRACE_SEC) -> str | None:
        lock = getattr(self, "_write_activity_lock", None)
        if lock is not None:
            with lock:
                if int(getattr(self, "_active_write_count", 0) or 0) > 0:
                    return "active_write"
                last_write_ts = float(getattr(self, "_last_write_activity_ts", 0.0) or 0.0)
            if last_write_ts and (time.time() - last_write_ts) < max(0.0, float(idle_grace_sec or 0.0)):
                return "recent_write"

        probe = getattr(self, "_write_queue_idle_probe", None)
        if probe is not None:
            try:
                if not bool(probe()):
                    return "write_queue_busy"
            except Exception as exc:
                logger.debug("Maintenance write queue idle probe failed: %s", exc)
                return "write_queue_probe_error"
        return None

    def _write_queue_depth_hint(self) -> int:
        depth = 0
        lock = getattr(self, "_write_activity_lock", None)
        if lock is not None:
            try:
                with lock:
                    depth = max(depth, int(getattr(self, "_active_write_count", 0) or 0))
            except Exception:
                depth = max(depth, 1)
        probe = getattr(self, "_write_queue_idle_probe", None)
        if probe is not None:
            try:
                if not bool(probe()):
                    depth = max(depth, 1)
            except Exception:
                depth = max(depth, 1)
        return depth

    def _maintenance_defer_reason(
        self,
        task_type: str,
        *,
        source: str,
        idle_window_sec: float,
        startup_grace: bool = False,
        check_writes: bool = True,
        cooldown_source: str | None = None,
        stop_event: threading.Event | None = None,
    ) -> tuple[str | None, dict[str, Any]]:
        if getattr(self, "_closed", False):
            return "closed", {}
        if stop_event is not None and stop_event.is_set():
            return "stopping", {}

        now_ts = time.time()
        if startup_grace:
            started_ts = float(getattr(self, "_startup_ts", now_ts) or now_ts)
            elapsed_sec = max(0.0, now_ts - started_ts)
            if elapsed_sec < HEAVY_MAINTENANCE_STARTUP_GRACE_SEC:
                return "startup_grace", {
                    "remaining_sec": round(HEAVY_MAINTENANCE_STARTUP_GRACE_SEC - elapsed_sec, 3),
                    "startup_grace_sec": round(HEAVY_MAINTENANCE_STARTUP_GRACE_SEC, 3),
                }

        cooldown_remaining = self._maintenance_io_cooldown_remaining(cooldown_source or task_type)
        if cooldown_remaining > 0:
            return "maintenance_cooldown", {
                "remaining_sec": round(cooldown_remaining, 3),
                "last_source": getattr(self, "_last_heavy_maintenance_source", ""),
            }

        if check_writes:
            write_reason = self._write_activity_defer_reason()
            if write_reason:
                return "write_queue_not_idle", {
                    "write_queue_reason": write_reason,
                    "write_queue_depth": self._write_queue_depth_hint(),
                }

        should_defer, reason, age_sec = should_defer_background_io(
            idle_window_sec=idle_window_sec,
            names=None,
        )
        if should_defer:
            return f"foreground_activity:{reason}", {
                "age_sec": None if age_sec is None else round(age_sec, 3),
                "idle_window_sec": round(max(0.0, float(idle_window_sec or 0.0)), 3),
            }
        return None, {}

    def _record_maintenance_deferred(
        self,
        task_type: str,
        reason: str,
        *,
        source: str,
        retry_sec: float | None = None,
        **fields: Any,
    ) -> None:
        task = str(task_type or "maintenance")
        now_ts = time.time()
        deferred_since = getattr(self, "_maintenance_deferred_since", None)
        if deferred_since is None:
            deferred_since = {}
            self._maintenance_deferred_since = deferred_since
        first_ts = float(deferred_since.setdefault(task, now_ts) or now_ts)
        deferred_for_sec = max(0.0, now_ts - first_ts)
        payload = {
            "task_type": task,
            "source": str(source or "background"),
            "reason": str(reason or "unknown"),
            "deferred_for_sec": round(deferred_for_sec, 3),
        }
        try:
            foreground_snapshot = foreground_activity_snapshot(limit=4)
            foreground_names = [
                str(item.get("name") or "")
                for item in (foreground_snapshot.get("active") or []) + (foreground_snapshot.get("recent") or [])
                if isinstance(item, dict) and str(item.get("name") or "")
            ]
            if foreground_names:
                payload["foreground_activity"] = ",".join(dict.fromkeys(foreground_names))
        except Exception:
            pass
        payload.update(fields)
        if retry_sec is not None:
            payload["retry_sec"] = round(max(0.0, float(retry_sec or 0.0)), 3)
        record_metric("maintenance_task_deferred", 1, **payload)
        if deferred_for_sec >= MAINTENANCE_DEFER_WARN_SEC:
            logger.warning(
                "Background maintenance still deferred task=%s source=%s reason=%s deferred_for_sec=%.1f",
                task,
                source,
                reason,
                deferred_for_sec,
            )
            record_metric("maintenance_task_defer_warn", 1, **payload)
            record_metric(
                "maintenance_deferral_max_age",
                round(deferred_for_sec, 3),
                **payload,
            )
            deferred_since[task] = now_ts

    def _clear_maintenance_deferred(self, task_type: str) -> None:
        deferred_since = getattr(self, "_maintenance_deferred_since", None)
        if isinstance(deferred_since, dict):
            deferred_since.pop(str(task_type or "maintenance"), None)

    @contextmanager
    def _central_io_lock_scope(self, operation: str, *, source: str = "db"):
        started = time.perf_counter()
        lock = self._central_io_lock
        lock.acquire()
        wait_ms = (time.perf_counter() - started) * 1000.0
        if wait_ms >= CENTRAL_IO_LOCK_WAIT_WARN_MS:
            snapshot = active_maintenance_snapshot(limit=4)
            active_tasks = ",".join(
                str(item.get("task_type") or "")
                for item in (snapshot.get("active") or [])
                if isinstance(item, dict)
            )
            logger.warning(
                "[DBReadWait] central_io_lock_wait_ms=%.1f operation=%s source=%s active_maintenance=%s",
                wait_ms,
                operation,
                source,
                active_tasks,
            )
            record_metric(
                "central_io_lock_wait_ms",
                round(wait_ms, 3),
                operation=str(operation or ""),
                source=str(source or ""),
                active_maintenance_count=int(snapshot.get("active_count") or 0),
                active_maintenance=active_tasks,
            )
        try:
            yield
        finally:
            lock.release()

    def _rotation_blocking_role_lock_paths(self) -> dict[str, str]:
        session_locks_dir = os.path.join(str(getattr(self, "baza_dir", BAZA_DIR)), "session_locks")
        return {
            "nurse": os.path.join(session_locks_dir, "nurse.lock"),
            "nurse_emergency": os.path.join(session_locks_dir, "nurse_emergency.lock"),
        }

    def _rotation_blocking_emergency_roots(self) -> list[str]:
        roots: list[str] = []
        try:
            from rem_card.app.emergency_paths import resolve_emergency_root

            roots.append(resolve_emergency_root())
        except Exception:
            pass
        network_candidate = os.path.join(str(getattr(self, "baza_dir", BAZA_DIR)), "emergency_db")
        roots.append(network_candidate)

        result: list[str] = []
        seen: set[str] = set()
        for root in roots:
            key = os.path.normcase(os.path.abspath(root))
            if key in seen:
                continue
            seen.add(key)
            result.append(os.path.abspath(root))
        return result

    def active_rotation_role_locks(self) -> list[dict[str, str]]:
        return find_active_rotation_role_locks(
            self._rotation_blocking_role_lock_paths(),
            logger=logger,
        )

    def active_rotation_emergency_sessions(self) -> list[dict[str, str]]:
        return find_active_emergency_nurse_sessions(
            self._rotation_blocking_emergency_roots(),
            logger=logger,
        )

    def _maybe_rotate_db_lifecycle(self, *, source: str = "auto_rotation") -> dict:
        result = maybe_rotate_database_if_due(
            db_path=self.db_path,
            archive_dir=os.path.dirname(self.db_path),
            rotation_lock_path=getattr(self, "medical_db_rotation_lock_path", DB_ROTATION_LOCK_PATH),
            db_lock_path=getattr(self, "medical_db_lock_path", DB_LOCK_PATH),
            logger=logger,
            max_age_days=180,
            backup_dir=getattr(self, "medical_backups_valid_dir", BACKUPS_VALID_DIR),
            invalid_dir=getattr(self, "medical_invalid_backups_dir", INVALID_BACKUPS_DIR),
            runtime_mode=getattr(self.runtime_context, "mode", "network"),
            source=source,
            blocked_role_lock_paths=self._rotation_blocking_role_lock_paths(),
            blocked_emergency_roots=self._rotation_blocking_emergency_roots(),
        )
        status = result.get("status")
        if status in (
            "rotated",
            "deferred_active_beds",
            "deferred_active_role_lock",
            "deferred_active_emergency_session",
        ):
            logger.warning("DB lifecycle status: %s | %s", status, result)
        elif status not in ("missing", "not_due", "rotation_lock_busy", "rotation_forbidden_runtime"):
            logger.info("DB lifecycle status: %s | %s", status, result)
        if status not in (
            "not_due",
            "rotation_lock_busy",
            "deferred_active_beds",
            "deferred_active_role_lock",
            "deferred_active_emergency_session",
            "rotation_forbidden_runtime",
        ):
            self._startup_pre_connect_fingerprint = None
        return result

    def maybe_rotate_database_after_doctor_exit(self) -> dict:
        return self._maybe_rotate_db_lifecycle(source="doctor_exit_auto_rotation")

    def rotate_database_manually(self) -> dict:
        if getattr(self.runtime_context, "mode", "network") != "network":
            return {
                "status": "rotation_forbidden_runtime",
                "runtime_mode": getattr(self.runtime_context, "mode", ""),
            }

        active_role_locks = self.active_rotation_role_locks()
        if active_role_locks:
            return {
                "status": "deferred_active_role_lock",
                "blocked_roles": active_role_locks,
            }

        active_emergency_sessions = self.active_rotation_emergency_sessions()
        if active_emergency_sessions:
            return {
                "status": "deferred_active_emergency_session",
                "blocked_emergency_sessions": active_emergency_sessions,
            }

        with self._central_io_lock:
            self._close_central_read_connection()
            if self._remcard_conn:
                with self.write_controller.connection_guard(self._remcard_conn):
                    self._remcard_conn.close()
            self._remcard_conn = None
            self._journal_conn = None

            result = rotate_database_now(
                db_path=self.db_path,
                archive_dir=os.path.dirname(self.db_path),
                rotation_lock_path=getattr(self, "medical_db_rotation_lock_path", DB_ROTATION_LOCK_PATH),
                db_lock_path=getattr(self, "medical_db_lock_path", DB_LOCK_PATH),
                logger=logger,
                backup_dir=getattr(self, "medical_backups_valid_dir", BACKUPS_VALID_DIR),
                invalid_dir=getattr(self, "medical_invalid_backups_dir", INVALID_BACKUPS_DIR),
                runtime_mode=getattr(self.runtime_context, "mode", "network"),
                source="manual_rotation",
                blocked_role_lock_paths=self._rotation_blocking_role_lock_paths(),
                blocked_emergency_roots=self._rotation_blocking_emergency_roots(),
            )
            self._startup_pre_connect_fingerprint = None
            status = result.get("status")
            db_available = os.path.isfile(self.db_path)
            if (status == "new_db_failed" and not result.get("current_preserved")) or (
                status == "rotate_failed" and not db_available
            ):
                logger.critical("Manual DB rotation left current DB unavailable: %s", result)
            else:
                self._init_connections()

        if self._local_replica:
            self._local_replica.trigger_fast_sync()
        return result

    def _init_connections(self):
        logger.info("Initializing unified DB connection at %s", self.db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        profile_lock = FileWriteLock(
            getattr(self, "medical_db_lock_path", DB_LOCK_PATH),
            stale_timeout_sec=10 * 60,
            logger=logger,
        )
        owner_id = f"{socket.gethostname()}:{os.getpid()}:remcard_init"
        self._acquire_connection_profile_lock(profile_lock, owner_id)
        try:
            if self._startup_pre_connect_fingerprint is None:
                self._startup_pre_connect_fingerprint = self._startup_db_fingerprint()
            connect_started = time.perf_counter()
            conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                isolation_level=None,
                timeout=5.0,
            )
            configure_connection(conn, profile="network")
            self._record_startup_metric("sqlite_connect_ms", (time.perf_counter() - connect_started) * 1000.0)
        except Exception as exc:
            if is_database_unavailable_error(exc):
                raise notify_database_unavailable(exc, context="remcard_init", logger=logger) from exc
            raise
        finally:
            profile_lock.release()
        self._remcard_conn = conn
        self._journal_conn = conn
        self._closed = False

    def _acquire_connection_profile_lock(self, profile_lock: FileWriteLock, owner_id: str):
        lock_started = time.perf_counter()
        deadline = lock_started + CONNECTION_PROFILE_LOCK_TIMEOUT_SEC
        attempts = 0
        while True:
            attempts += 1
            if profile_lock.acquire(owner_id=owner_id, source="connection_profile"):
                self._record_startup_metric(
                    "connection_lock_wait_ms",
                    (time.perf_counter() - lock_started) * 1000.0,
                )
                if attempts > 1:
                    logger.info(
                        "Acquired connection_profile lock after waiting %.3fs attempts=%s",
                        time.perf_counter() - lock_started,
                        attempts,
                    )
                return

            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                self._record_startup_metric(
                    "connection_lock_wait_ms",
                    (time.perf_counter() - lock_started) * 1000.0,
                )
                holder = self._describe_file_lock_holder(profile_lock)
                raise sqlite3.OperationalError(
                    "Could not acquire db lock for connection profile "
                    f"after {CONNECTION_PROFILE_LOCK_TIMEOUT_SEC:.1f}s; {holder}"
                )

            delay = min(
                remaining,
                random.uniform(CONNECTION_PROFILE_LOCK_RETRY_MIN_SEC, CONNECTION_PROFILE_LOCK_RETRY_MAX_SEC),
            )
            time.sleep(delay)

    @staticmethod
    def _describe_file_lock_holder(lock: FileWriteLock) -> str:
        try:
            payload = lock._try_read_payload()
        except Exception as exc:
            return f"lock holder unavailable: {exc}"
        if not payload:
            return "lock holder unavailable"
        if not isinstance(payload, dict):
            return "lock holder unreadable"
        age_sec = None
        try:
            age_sec = max(0.0, time.time() - float(payload.get("timestamp")))
        except Exception:
            pass
        parts = [
            f"host={payload.get('host', 'unknown')}",
            f"pid={payload.get('pid', 'unknown')}",
            f"source={payload.get('source', 'unknown')}",
        ]
        if age_sec is not None:
            parts.append(f"age_sec={age_sec:.1f}")
        return "lock holder: " + ", ".join(parts)

    def _reconnect(self):
        with self._central_io_lock:
            self._close_central_read_connection()
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
        value = int(time.time()) if ts is None else int(ts)
        self._startup_quickcheck_ok_ts = value
        self._startup_quickcheck_skipped_ts = None
        self._write_startup_quickcheck_state(value, result="ok")

    def _normalized_startup_db_path(self) -> str:
        return os.path.normcase(os.path.abspath(str(self.db_path)))

    def _startup_db_fingerprint(self) -> Optional[dict[str, Any]]:
        try:
            stat_result = os.stat(self.db_path)
        except Exception as exc:
            logger.debug("Failed to stat startup quick_check DB: %s", exc)
            return None
        return {
            "db_path_norm": self._normalized_startup_db_path(),
            "size_bytes": int(stat_result.st_size),
            "mtime_ns": int(getattr(stat_result, "st_mtime_ns", int(stat_result.st_mtime * 1_000_000_000))),
            "db_profile": NETWORK_SAFE_DB_PROFILE,
            "state_version": STARTUP_QUICKCHECK_STATE_VERSION,
        }

    def _startup_schema_migration_state(self) -> Optional[dict[str, Any]]:
        conn = None
        try:
            with self._central_io_lock:
                conn = sqlite3.connect(
                    self._readonly_db_uri(),
                    uri=True,
                    check_same_thread=True,
                    isolation_level=None,
                    timeout=5.0,
                )
                configure_connection(conn, readonly=True, profile="network")
                schema_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
                ).fetchone()
                if schema_table:
                    migration_row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
                    max_migration_version = int(migration_row[0] or 0) if migration_row else 0
                else:
                    max_migration_version = 0

                fastpath_value = None
                meta_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
                ).fetchone()
                if meta_table:
                    fastpath_row = conn.execute(
                        "SELECT value FROM meta WHERE key = ?",
                        (SCHEMA_FASTPATH_META_KEY,),
                    ).fetchone()
                    if fastpath_row and fastpath_row[0] is not None:
                        try:
                            fastpath_value = int(fastpath_row[0])
                        except Exception:
                            fastpath_value = str(fastpath_row[0])
                conn.close()
                conn = None

            return {
                "required_min_migration_version": int(SCHEMA_MIN_MIGRATION_VERSION),
                "required_fastpath_rev": int(SCHEMA_FASTPATH_REV),
                "max_migration_version": int(max_migration_version),
                "fastpath_meta_value": fastpath_value,
            }
        except Exception as exc:
            logger.debug("Failed to read startup schema migration state: %s", exc)
            return None
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass

    def _startup_quickcheck_fingerprint(self, *, prefer_pre_connect: bool = False) -> Optional[dict[str, Any]]:
        base = None
        if prefer_pre_connect:
            base = getattr(self, "_startup_pre_connect_fingerprint", None)
        fingerprint = dict(base or self._startup_db_fingerprint() or {})
        if not fingerprint:
            return None
        schema_state = self._startup_schema_migration_state()
        if schema_state is None:
            return None
        fingerprint["schema_migration_state"] = schema_state
        return fingerprint

    @staticmethod
    def _latest_mtime_ns_under(path: str) -> int:
        if not path or not os.path.exists(path):
            return 0
        latest = 0
        for root, dirs, files in os.walk(path):
            for name in list(dirs) + list(files):
                candidate = os.path.join(root, name)
                try:
                    stat_result = os.stat(candidate)
                except OSError:
                    continue
                latest = max(
                    latest,
                    int(getattr(stat_result, "st_mtime_ns", int(stat_result.st_mtime * 1_000_000_000))),
                )
        return latest

    def _startup_failure_marker_mtime_ns(self) -> int:
        return max(
            self._latest_mtime_ns_under(getattr(self, "medical_invalid_backups_dir", INVALID_BACKUPS_DIR)),
            self._latest_mtime_ns_under(getattr(self, "medical_quarantine_dir", QUARANTINE_DIR)),
        )

    def _read_startup_quickcheck_state(self) -> Optional[dict[str, Any]]:
        state_path = getattr(self, "medical_startup_quickcheck_state_path", STARTUP_QUICKCHECK_STATE_PATH)
        try:
            with open(state_path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except FileNotFoundError:
            return None
        except Exception as exc:
            logger.debug("Failed to read startup quick_check state: %s", exc)
            return None
        return payload if isinstance(payload, dict) else None

    def _write_startup_quickcheck_state(self, checked_at_epoch: int, *, result: str):
        fingerprint = self._startup_quickcheck_fingerprint(prefer_pre_connect=False)
        if not fingerprint:
            return
        state_path = getattr(self, "medical_startup_quickcheck_state_path", STARTUP_QUICKCHECK_STATE_PATH)
        payload = {
            **fingerprint,
            "checked_at_epoch": int(checked_at_epoch),
            "result": str(result or ""),
            "app_version": APP_VERSION,
            "failure_marker_mtime_ns": self._startup_failure_marker_mtime_ns(),
        }
        try:
            os.makedirs(os.path.dirname(state_path), exist_ok=True)
            temp_path = (
                f"{state_path}.tmp_"
                f"{os.getpid()}_{threading.get_ident()}_{int(time.time() * 1000)}"
            )
            with open(temp_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(temp_path, state_path)
        except Exception as exc:
            logger.debug("Failed to write startup quick_check state: %s", exc)

    def _startup_quickcheck_state_matches(self, state: dict[str, Any], fingerprint: dict[str, Any]) -> bool:
        if int(state.get("state_version") or 0) != STARTUP_QUICKCHECK_STATE_VERSION:
            return False
        if str(state.get("result") or "") != "ok":
            return False
        for key in ("db_path_norm", "size_bytes", "mtime_ns", "db_profile", "schema_migration_state"):
            if state.get(key) != fingerprint.get(key):
                return False
        try:
            stored_marker = int(state.get("failure_marker_mtime_ns") or 0)
        except Exception:
            return False
        return self._startup_failure_marker_mtime_ns() <= stored_marker

    def _startup_guard_quickcheck_matches(self, fingerprint: dict[str, Any]) -> tuple[bool, Optional[float]]:
        raw = os.environ.get(STARTUP_GUARD_QUICKCHECK_ENV)
        if not raw:
            return False, None
        try:
            state = json.loads(raw)
        except Exception:
            return False, None
        if not isinstance(state, dict):
            return False, None
        if str(state.get("result") or "") != "ok":
            return False, None
        try:
            if int(state.get("pid") or -1) != os.getpid():
                return False, None
        except Exception:
            return False, None
        for key in ("db_path_norm", "size_bytes", "mtime_ns", "db_profile"):
            if state.get(key) != fingerprint.get(key):
                return False, None
        try:
            checked_at = float(state.get("checked_at_epoch"))
        except Exception:
            return False, None
        age_sec = max(0.0, time.time() - checked_at)
        if age_sec > STARTUP_GUARD_QUICKCHECK_MAX_AGE_SEC:
            return False, age_sec
        self._startup_quickcheck_skipped_ts = int(checked_at)
        return True, age_sec

    def _should_run_startup_quickcheck(
        self,
        *,
        allow_startup_guard: bool = True,
        prefer_pre_connect: bool = True,
    ) -> tuple[bool, Optional[float]]:
        if STARTUP_QUICKCHECK_TTL_SEC <= 0:
            return True, None

        fingerprint = self._startup_quickcheck_fingerprint(prefer_pre_connect=prefer_pre_connect)
        if allow_startup_guard and fingerprint:
            guard_matches, guard_age_sec = self._startup_guard_quickcheck_matches(fingerprint)
            if guard_matches:
                return False, guard_age_sec

        state = self._read_startup_quickcheck_state()
        if not state:
            return True, None
        if not fingerprint or not self._startup_quickcheck_state_matches(state, fingerprint):
            return True, None
        try:
            checked_at = float(state.get("checked_at_epoch"))
        except Exception:
            return True, None
        age_sec = max(0.0, time.time() - checked_at)
        should_run = age_sec >= STARTUP_QUICKCHECK_TTL_SEC
        if not should_run:
            self._startup_quickcheck_skipped_ts = int(checked_at)
        return should_run, age_sec

    def _verify_quick_integrity_or_restore(self):
        decision_started = time.perf_counter()
        should_run, age_sec = self._should_run_startup_quickcheck()
        self._record_startup_metric("quick_check_decision_ms", (time.perf_counter() - decision_started) * 1000.0)
        if not should_run:
            self._record_startup_metric("quick_check_ms", 0.0)
            logger.info(
                "Skipping startup quick_check for %s (last=%.1fs ago, ttl=%.1fs)",
                self.db_path,
                float(age_sec or 0.0),
                STARTUP_QUICKCHECK_TTL_SEC,
            )
            return

        try:
            quick_started = time.perf_counter()
            ok, result = run_quick_check(self._remcard_conn)
            self._record_startup_metric("quick_check_ms", (time.perf_counter() - quick_started) * 1000.0)
        except Exception as exc:
            self._record_startup_metric("quick_check_ms", (time.perf_counter() - quick_started) * 1000.0 if "quick_started" in locals() else 0.0)
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
            baza_dir=getattr(self, "baza_dir", BAZA_DIR),
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
        self._close_central_read_connection()
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
            backup_dir=getattr(self, "medical_backups_valid_dir", BACKUPS_VALID_DIR),
            invalid_dir=getattr(self, "medical_invalid_backups_dir", INVALID_BACKUPS_DIR),
            policy_path=getattr(self, "medical_client_policy_path", CLIENT_POLICY_PATH),
            role=None,
            baza_dir=getattr(self, "baza_dir", BAZA_DIR),
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

    def _start_startup_quickcheck_updater(self):
        if not STARTUP_QUICKCHECK_BACKGROUND_ENABLED:
            logger.info(
                "Background startup quick_check updater is disabled by default; "
                "set REMCARD_STARTUP_QUICKCHECK_BACKGROUND_ENABLED=1 to enable it."
            )
            return
        if self._startup_quickcheck_thread and self._startup_quickcheck_thread.is_alive():
            return
        self._startup_quickcheck_stop_evt.clear()
        self._startup_quickcheck_thread = threading.Thread(
            target=self._startup_quickcheck_updater_worker,
            name="RemCardStartupQuickCheckUpdater",
            daemon=True,
        )
        self._startup_quickcheck_thread.start()

    def _startup_quickcheck_updater_worker(self):
        if self._startup_quickcheck_stop_evt.wait(STARTUP_QUICKCHECK_BACKGROUND_DELAY_SEC):
            return

        while not self._startup_quickcheck_stop_evt.is_set():
            try:
                self._run_startup_quickcheck_background_once()
            except Exception as exc:
                logger.debug("Background startup quick_check updater failed: %s", exc, exc_info=True)
            if self._startup_quickcheck_stop_evt.wait(STARTUP_QUICKCHECK_BACKGROUND_INTERVAL_SEC):
                return

    def _run_startup_quickcheck_background_once(self) -> bool:
        if not self._is_startup_quickcheck_idle():
            return False

        should_run, _age_sec = self._should_run_startup_quickcheck(
            allow_startup_guard=False,
            prefer_pre_connect=False,
        )
        if not should_run:
            return False
        if not self._is_startup_quickcheck_idle():
            return False

        conn = None
        cancelled = False

        def cancel_if_not_idle():
            nonlocal cancelled
            if self._is_startup_quickcheck_idle():
                return 0
            cancelled = True
            return 1

        try:
            with maintenance_task("startup_quick_check", source="startup_quick_check", db_path=self.db_path):
                with self._central_io_lock_scope("startup_quick_check", source="startup_quick_check"):
                    if not self._is_startup_quickcheck_idle():
                        return False
                    conn = sqlite3.connect(
                        self._readonly_db_uri(),
                        uri=True,
                        check_same_thread=True,
                        isolation_level=None,
                        timeout=5.0,
                    )
                    configure_connection(conn, readonly=True, profile="network")
                    try:
                        conn.set_progress_handler(cancel_if_not_idle, 1000)
                        quick_started = time.perf_counter()
                        ok, result = run_quick_check(conn)
                    finally:
                        try:
                            conn.set_progress_handler(None, 0)
                            conn.close()
                        finally:
                            conn = None
            elapsed_ms = (time.perf_counter() - quick_started) * 1000.0
            if elapsed_ms >= STARTUP_QUICKCHECK_SLOW_MS:
                self._startup_quickcheck_next_allowed_ts = time.time() + STARTUP_QUICKCHECK_SLOW_BACKOFF_SEC
                record_metric(
                    "startup_quick_check_slow_backoff",
                    round(elapsed_ms, 3),
                    backoff_sec=round(STARTUP_QUICKCHECK_SLOW_BACKOFF_SEC, 3),
                )
            if elapsed_ms >= STARTUP_QUICKCHECK_SLOW_MS or ok:
                self._mark_heavy_maintenance_io("startup_quick_check")
            if ok:
                self._write_startup_quickcheck_state(int(time.time()), result="ok")
                logger.info("Background startup quick_check state updated for %s", self.db_path)
                return True
            if cancelled:
                logger.info("Background startup quick_check cancelled because writes became active")
                return False
            logger.warning("Background startup quick_check failed for %s: %s", self.db_path, result)
            return False
        except sqlite3.OperationalError as exc:
            if cancelled or "interrupted" in str(exc).lower():
                logger.info("Background startup quick_check cancelled because writes became active")
                return False
            logger.warning("Background startup quick_check could not run: %s", exc)
            return False
        except Exception as exc:
            logger.warning("Background startup quick_check could not run: %s", exc)
            return False
        finally:
            if conn is not None:
                try:
                    conn.set_progress_handler(None, 0)
                    conn.close()
                except Exception:
                    pass

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

        with self._mark_write_activity():
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

    def _readonly_db_uri(self) -> str:
        db_path = str(self.db_path)
        if db_path.startswith("\\\\"):
            # sqlite3 on Windows rejects Path.as_uri() UNC authorities
            # (file://server/share/...), while file:\\server\share works.
            return f"file:{db_path}?mode=ro"
        try:
            path_uri = Path(db_path).as_uri()
        except ValueError:
            path_uri = Path(os.path.abspath(db_path)).as_uri()
        return f"{path_uri}?mode=ro"

    def _open_readonly_central_connection(self) -> sqlite3.Connection:
        if self._closed or self._remcard_conn is None:
            raise DatabaseClosedError("RemCard database connection is closed for readonly connection")
        conn = sqlite3.connect(
            self._readonly_db_uri(),
            uri=True,
            check_same_thread=True,
            isolation_level=None,
            timeout=5.0,
        )
        configure_connection(conn, readonly=True, profile="network")
        return conn

    def _get_central_write_connection_for_read(self, context: str) -> sqlite3.Connection:
        if self._closed or self._remcard_conn is None:
            raise DatabaseClosedError(f"RemCard database connection is closed for {context}")
        return self._remcard_conn

    def _get_central_read_connection(self, context: str) -> sqlite3.Connection:
        if self._closed or self._remcard_conn is None:
            raise DatabaseClosedError(f"RemCard database connection is closed for {context}")
        self._close_finished_thread_read_connections_locked()
        current_thread = threading.current_thread()
        conn = self._central_read_conns.get(current_thread)
        if conn is None:
            conn = self._open_readonly_central_connection()
            self._central_read_conns[current_thread] = conn
        return conn

    def _close_central_read_connection(self):
        conns = list(self._central_read_conns.values())
        self._central_read_conns.clear()
        for conn in conns:
            if conn is None:
                continue
            try:
                with self.write_controller.connection_guard(conn):
                    conn.close()
            except Exception as exc:
                logger.debug("Failed to close central read connection: %s", exc)

    def _close_finished_thread_read_connections_locked(self):
        current_thread = threading.current_thread()
        finished_threads = [
            thread
            for thread in self._central_read_conns
            if thread is not current_thread and not thread.is_alive()
        ]
        for thread in finished_threads:
            conn = self._central_read_conns.pop(thread, None)
            if conn is None:
                continue
            try:
                with self.write_controller.connection_guard(conn):
                    conn.close()
            except Exception as exc:
                logger.debug("Failed to close finished thread central read connection: %s", exc)

    @staticmethod
    def _read_cancel_requested(cancel_check, cancel_state: dict[str, Any]) -> bool:
        if cancel_check is None:
            return False
        if bool(cancel_state.get("cancelled")):
            return True
        try:
            requested = bool(cancel_check())
        except Exception as exc:
            cancel_state["cancelled"] = True
            cancel_state["exception"] = exc
            return True
        if requested:
            cancel_state["cancelled"] = True
        return requested

    @staticmethod
    def _cancelled_read_exception(cancel_state: dict[str, Any]) -> Exception:
        exc = cancel_state.get("exception")
        if isinstance(exc, Exception):
            return exc
        return RuntimeError("SQLite read cancelled")

    def _fetch_all_with_cancel(self, conn, query, params=(), *, cancel_check=None):
        cancel_state: dict[str, Any] = {"cancelled": False, "exception": None}
        done_event = threading.Event()
        watchdog_thread = None

        def should_cancel() -> bool:
            return self._read_cancel_requested(cancel_check, cancel_state)

        def progress_handler() -> int:
            return 1 if should_cancel() else 0

        def cancel_watchdog() -> None:
            while not done_event.wait(0.10):
                if not should_cancel():
                    continue
                try:
                    conn.interrupt()
                except Exception:
                    pass
                return

        if cancel_check is not None:
            try:
                conn.set_progress_handler(progress_handler, 1000)
            except Exception:
                logger.debug("Failed to install SQLite read progress handler", exc_info=True)
            watchdog_thread = threading.Thread(
                target=cancel_watchdog,
                name="SQLiteReadCancelWatchdog",
                daemon=True,
            )
            watchdog_thread.start()

        try:
            if should_cancel():
                raise self._cancelled_read_exception(cancel_state)
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
            if should_cancel():
                raise self._cancelled_read_exception(cancel_state)
            return rows
        except sqlite3.OperationalError as exc:
            if bool(cancel_state.get("cancelled")) and "interrupted" in str(exc).lower():
                raise self._cancelled_read_exception(cancel_state) from exc
            raise
        finally:
            done_event.set()
            if cancel_check is not None:
                try:
                    conn.set_progress_handler(None, 0)
                except Exception:
                    pass
            if watchdog_thread is not None and watchdog_thread.is_alive():
                watchdog_thread.join(timeout=0.2)

    def _fetch_all_central(self, query, params=(), *, use_write_connection: bool = False, cancel_check=None):
        # Чтения внутри текущей транзакции должны видеть незакоммиченные строки.
        # Обычные фоновые чтения открывают короткоживущее read-only connection
        # на конкретный запрос. Worker-потоки здесь короткие, поэтому кэшировать
        # sqlite3.Connection по Thread рискованно: соединение переживает поток и
        # позже закрывается/переиспользуется уже из другого потока, что на Windows
        # может завершиться native access violation без Python-исключения.
        try:
            with self._central_io_lock_scope("remcard_read_all", source="fetch_all"):
                if use_write_connection or self._in_current_thread_remcard_transaction():
                    conn = self._get_central_write_connection_for_read("remcard_read_all")
                    with self.write_controller.connection_guard(conn):
                        return self._fetch_all_with_cancel(conn, query, params, cancel_check=cancel_check)
                conn = self._open_readonly_central_connection()
                try:
                    return self._fetch_all_with_cancel(conn, query, params, cancel_check=cancel_check)
                finally:
                    try:
                        conn.close()
                    except Exception as close_exc:
                        logger.debug("Failed to close short-lived central read connection: %s", close_exc)
        except Exception as exc:
            if is_database_unavailable_error(exc):
                raise notify_database_unavailable(exc, context="remcard_read_all", logger=logger) from exc
            raise

    def _fetch_one_central(self, query, params=(), *, use_write_connection: bool = False):
        try:
            with self._central_io_lock_scope("remcard_read_one", source="fetch_one"):
                if use_write_connection or self._in_current_thread_remcard_transaction():
                    conn = self._get_central_write_connection_for_read("remcard_read_one")
                    with self.write_controller.connection_guard(conn):
                        cursor = conn.cursor()
                        cursor.execute(query, params)
                        return cursor.fetchone()
                conn = self._open_readonly_central_connection()
                try:
                    cursor = conn.cursor()
                    cursor.execute(query, params)
                    return cursor.fetchone()
                finally:
                    try:
                        conn.close()
                    except Exception as close_exc:
                        logger.debug("Failed to close short-lived central read connection: %s", close_exc)
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
            ran = self._run_integrity_check_background_once()
            wait_sec = INTEGRITY_CHECK_INTERVAL_SEC if ran else INTEGRITY_DEFER_RETRY_SEC
            if self._integrity_stop_evt.wait(wait_sec):
                return

    def _run_integrity_check_background_once(self) -> bool:
        reason, fields = self._maintenance_defer_reason(
            "integrity_check",
            source="integrity_monitor",
            idle_window_sec=HEAVY_MAINTENANCE_FOREGROUND_IDLE_SEC,
            startup_grace=True,
            check_writes=True,
            cooldown_source="integrity_check",
            stop_event=getattr(self, "_integrity_stop_evt", None),
        )
        if reason:
            self._record_maintenance_deferred(
                "integrity_check",
                reason,
                source="integrity_monitor",
                retry_sec=INTEGRITY_DEFER_RETRY_SEC,
                **fields,
            )
            logger.info(
                "Background integrity_check deferred for %s: reason=%s retry_sec=%.1f",
                self.db_path,
                reason,
                INTEGRITY_DEFER_RETRY_SEC,
            )
            return False

        conn = None
        try:
            with maintenance_task("integrity_check", source="integrity_monitor", db_path=self.db_path):
                conn = sqlite3.connect(
                    self._readonly_db_uri(),
                    uri=True,
                    check_same_thread=True,
                    isolation_level=None,
                    timeout=5.0,
                )
                configure_connection(conn, readonly=True, profile="network")
                try:
                    ok, result = run_integrity_check(conn)
                finally:
                    conn.close()
                    conn = None
            if ok:
                self._clear_maintenance_deferred("integrity_check")
                self._mark_heavy_maintenance_io("integrity_check")
                logger.info("Background integrity_check passed for %s", self.db_path)
                return True

            self._clear_maintenance_deferred("integrity_check")
            self._mark_heavy_maintenance_io("integrity_check")
            latest_backup = find_latest_backup(getattr(self, "medical_backups_root_dir", BACKUPS_RC_DIR))
            logger.critical(
                "Background integrity_check failed for %s: %s. Latest backup: %s",
                self.db_path,
                result,
                latest_backup or "not found",
            )
            return True
        except Exception as exc:
            logger.error("Background integrity_check failed to run: %s", exc, exc_info=True)
            return True
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    def _create_named_backup(self, prefix: str, source: str):
        if not self._remcard_conn:
            return None

        backup_dir = getattr(self, "medical_backups_valid_dir", BACKUPS_VALID_DIR)
        invalid_dir = getattr(self, "medical_invalid_backups_dir", INVALID_BACKUPS_DIR)
        lock_path = getattr(self, "medical_db_lock_path", DB_LOCK_PATH)
        os.makedirs(backup_dir, exist_ok=True)
        db_name = os.path.splitext(os.path.basename(self.db_path))[0]
        backup_name = f"{prefix}_{db_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        backup_path = os.path.join(backup_dir, backup_name)
        try:
            with maintenance_task(f"{prefix}_backup", source=str(source or "backup"), db_path=self.db_path):
                with self._central_io_lock_scope(f"{prefix}_backup", source=str(source or "backup")):
                    with self.write_controller.connection_guard(self._remcard_conn):
                        backup_connection(
                            self._remcard_conn,
                            backup_path,
                            invalid_dir=invalid_dir,
                            logger=logger,
                            lock_path=lock_path,
                            source=f"{prefix}_backup",
                        )
            self._rotate_backups()
            self._last_backup_ts = time.time()
            self._mark_heavy_maintenance_io(f"{prefix}_backup")
            self._clear_maintenance_deferred(f"{prefix}_backup")
            logger.info("%s backup created (%s): %s", prefix.capitalize(), source, backup_path)
            return backup_path
        except Exception as exc:
            logger.warning("Failed to create %s backup (%s): %s", prefix, source, exc)
            return None

    def create_validated_backup(self, prefix: str, source: str):
        return self._create_named_backup(prefix=prefix, source=source)

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
        reason, fields = self._maintenance_defer_reason(
            "periodic_backup",
            source=str(source or "write"),
            idle_window_sec=HEAVY_MAINTENANCE_FOREGROUND_IDLE_SEC,
            startup_grace=False,
            check_writes=True,
            cooldown_source="periodic_backup",
        )
        if reason:
            self._record_maintenance_deferred(
                "periodic_backup",
                reason,
                source=str(source or "write"),
                **fields,
            )
            if reason == "maintenance_cooldown":
                record_metric(
                    "periodic_backup_deferred_maintenance_cooldown",
                    1,
                    source=str(source or "write"),
                    remaining_sec=fields.get("remaining_sec"),
                    last_source=fields.get("last_source", ""),
                )
            elif reason.startswith("foreground_activity:"):
                foreground_reason = reason.split(":", 1)[1]
                logger.info(
                    "Skipping periodic backup: foreground activity is active/recent (source=%s reason=%s age_sec=%s idle_window_sec=%.1f)",
                    source,
                    foreground_reason,
                    fields.get("age_sec"),
                    HEAVY_MAINTENANCE_FOREGROUND_IDLE_SEC,
                )
                record_metric(
                    "periodic_backup_deferred_foreground_read",
                    1,
                    source=str(source or "write"),
                    reason=foreground_reason,
                    age_sec=fields.get("age_sec"),
                    idle_window_sec=HEAVY_MAINTENANCE_FOREGROUND_IDLE_SEC,
                )
            else:
                record_metric(
                    "periodic_backup_deferred",
                    1,
                    source=str(source or "write"),
                    reason=reason,
                )
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

    def _find_latest_runtime_backup_by_prefix(self, prefix: str) -> Optional[str]:
        candidates = list_backup_candidates(getattr(self, "medical_backups_root_dir", BACKUPS_RC_DIR), prefix=prefix)
        if not candidates:
            return None
        candidates.sort(key=os.path.getmtime, reverse=True)
        return candidates[0]

    def _list_runtime_backups(self) -> list[str]:
        return [
            path
            for path in list_backup_candidates(getattr(self, "medical_backups_root_dir", BACKUPS_RC_DIR))
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
            with self._mark_write_activity():
                with self._central_io_lock:
                    if self._closed or self._remcard_conn is None:
                        raise DatabaseClosedError(f"RemCard database connection is closed for {source}")
                    conn = self._remcard_conn
                    with self.write_controller.transaction(conn, source=source) as cursor:
                        with self._mark_current_thread_remcard_transaction():
                            wrapped_cursor = RecordingCursor(cursor, statement_sink) if outer_transaction else cursor
                            yield wrapped_cursor
                    if conn and not self._closed and outer_transaction:
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
            with self._mark_write_activity():
                with self._central_io_lock:
                    if self._closed or self._remcard_conn is None:
                        raise DatabaseClosedError(f"RemCard database connection is closed for {source}")
                    conn = self._remcard_conn
                    cursor = self.write_controller.execute(conn, query, params, source=source)
                    if conn and not self._closed and not self._in_current_thread_remcard_transaction():
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
                    with self._mark_write_activity():
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

    def fetch_all_remcard(self, query, params=(), *, cancel_check=None):
        logger.debug("SQL RemCard FetchAll: %s | Params: %s", query, params)
        started = time.perf_counter()
        source = "central"
        status = "error"
        try:
            if self._in_current_thread_remcard_transaction():
                rows = self._fetch_all_central(query, params, use_write_connection=True, cancel_check=cancel_check)
                status = "ok"
                return rows
            if self._should_read_from_local():
                try:
                    if cancel_check is not None:
                        cancel_state: dict[str, Any] = {"cancelled": False, "exception": None}
                        if self._read_cancel_requested(cancel_check, cancel_state):
                            raise self._cancelled_read_exception(cancel_state)
                    rows = self._local_replica.fetch_all(query, params)
                    if cancel_check is not None:
                        cancel_state = {"cancelled": False, "exception": None}
                        if self._read_cancel_requested(cancel_check, cancel_state):
                            raise self._cancelled_read_exception(cancel_state)
                    source = "local_replica"
                    status = "ok"
                    return rows
                except Exception as exc:
                    exc_name = exc.__class__.__name__.lower()
                    if cancel_check is not None and ("cancel" in exc_name or "cancelled" in str(exc).lower()):
                        raise
                    logger.debug("Local replica fetch_all failed, fallback to central: %s", exc)
            rows = self._fetch_all_central(query, params, cancel_check=cancel_check)
            status = "ok"
            return rows
        except Exception as exc:
            exc_name = exc.__class__.__name__.lower()
            if cancel_check is not None and ("cancel" in exc_name or "cancelled" in str(exc).lower()):
                status = "cancelled"
            raise
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

    def close(self, timeout_sec: Optional[float] = None) -> bool:
        started = time.perf_counter()
        with self._close_state_lock:
            if self._closing:
                logger.warning("Database close skipped because close is already in progress")
                return False
            if self._closed and self._remcard_conn is None:
                logger.info("Database close skipped because connection is already closed")
                return True
            self._closing = True
            self._closed = True

        io_lock_acquired = False
        ok = False
        io_timeout_sec = SHUTDOWN_CENTRAL_IO_LOCK_TIMEOUT_SEC if timeout_sec is None else max(0.1, float(timeout_sec))
        logger.info("Database shutdown started")
        try:
            self._startup_quickcheck_stop_evt.set()
            if self._startup_quickcheck_thread and self._startup_quickcheck_thread.is_alive():
                self._startup_quickcheck_thread.join(timeout=1.5)
                if self._startup_quickcheck_thread.is_alive():
                    logger.warning("Startup quick_check thread did not stop before database shutdown timeout")
            self._startup_quickcheck_thread = None

            self._integrity_stop_evt.set()
            if self._integrity_thread and self._integrity_thread.is_alive():
                self._integrity_thread.join(timeout=1.5)
                if self._integrity_thread.is_alive():
                    logger.warning("Integrity monitor thread did not stop before database shutdown timeout")
            self._integrity_thread = None

            self._stop_outbox_replay()
            self._stop_local_replica_sync()

            logger.info("Database shutdown acquiring central IO lock timeout_sec=%.1f", io_timeout_sec)
            io_lock_acquired = self._central_io_lock.acquire(timeout=io_timeout_sec)
            if not io_lock_acquired:
                logger.warning(
                    "Database shutdown skipped SQLite connection close: central IO lock was busy for %.1fs",
                    io_timeout_sec,
                )
                return False

            logger.info("Database shutdown central IO lock acquired")
            self._close_central_read_connection()
            if self._remcard_conn:
                conn = self._remcard_conn
                if conn is not None:
                    with self.write_controller.connection_guard(conn):
                        logger.info("Database shutdown backup step started")
                        self._create_shutdown_backup()
                        logger.info("Database shutdown closing SQLite connection")
                        conn.close()
                    self._remcard_conn = None
                    self._journal_conn = None
            else:
                self._journal_conn = None
            ok = True
            return True
        finally:
            if io_lock_acquired:
                self._central_io_lock.release()
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            record_metric("database_shutdown_duration_ms", round(elapsed_ms, 3), result="ok" if ok else "incomplete")
            logger.info("Database shutdown finished result=%s elapsed_ms=%.1f", "ok" if ok else "incomplete", elapsed_ms)
            with self._close_state_lock:
                self._closing = False
