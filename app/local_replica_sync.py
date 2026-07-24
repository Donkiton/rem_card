from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Optional

from rem_card.app.local_metrics import record_metric
from rem_card.app.local_replica_worker import (
    DEFAULT_REPLICA_SYNC_TIMEOUT_SEC,
    LocalReplicaWorkerClient,
)
from rem_card.app.sqlite_shared import configure_connection
from rem_card.app.sqlite_uri import build_sqlite_file_uri


def build_local_replica_path(
    *,
    cache_dir: str,
    central_db_path: str,
    client_id: str,
    role: str | None,
) -> str:
    normalized_path = os.path.normcase(
        os.path.abspath(os.path.normpath(str(central_db_path or "")))
    )
    database_key = hashlib.sha256(
        normalized_path.encode("utf-8", errors="surrogatepass")
    ).hexdigest()[:16]
    safe_client_id = re.sub(
        r"[^a-zA-Z0-9_-]+",
        "_",
        str(client_id or "unknown-client"),
    ).strip("_") or "unknown-client"
    safe_role = re.sub(
        r"[^a-zA-Z0-9_-]+",
        "_",
        str(role or "default").lower(),
    ).strip("_") or "default"
    return os.path.join(
        os.path.abspath(cache_dir),
        "replicas",
        safe_client_id,
        f"rao_journal_local_replica_{safe_role}_{database_key}.db",
    )


class LocalReplicaSync:
    """Неблокирующая локальная read-only реплика сетевой SQLite."""

    def __init__(
        self,
        *,
        central_db_path: str,
        local_db_path: str,
        rotation_lock_path: str | None = None,
        logger: Optional[logging.Logger] = None,
        sync_interval_sec: float = 2.0,
        sync_timeout_sec: float = DEFAULT_REPLICA_SYNC_TIMEOUT_SEC,
        worker_client: LocalReplicaWorkerClient | None = None,
    ):
        self.central_db_path = os.path.abspath(central_db_path)
        self.local_db_path = os.path.abspath(local_db_path)
        self.rotation_lock_path = (
            os.path.abspath(rotation_lock_path)
            if rotation_lock_path
            else ""
        )
        self.sync_interval_sec = max(1.0, float(sync_interval_sec))
        self.sync_timeout_sec = max(0.2, min(30.0, float(sync_timeout_sec)))
        self.logger = logger or logging.getLogger(__name__)

        self._lock = threading.RLock()
        self._sync_lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._fast_sync_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._local_conn: Optional[sqlite3.Connection] = None
        self._worker_client = worker_client or LocalReplicaWorkerClient(
            central_db_path=self.central_db_path,
            rotation_lock_path=self.rotation_lock_path,
            timeout_sec=self.sync_timeout_sec,
        )

        self.last_sync_ok_ts: float = 0.0
        self.last_sync_error: Optional[str] = None
        self.last_sync_error_class: str = ""
        self.consecutive_failures: int = 0
        self.last_state: dict[str, Any] = {}
        self._failure_callback: Callable[[dict[str, Any]], None] | None = None

    def start(self) -> None:
        os.makedirs(os.path.dirname(self.local_db_path), exist_ok=True)
        try:
            self._ensure_local_conn()
        except Exception as exc:
            self.logger.warning(
                "Existing local replica is unavailable and will be replaced: %s",
                exc,
            )
            self._close_local_conn()
        self._start_worker()
        self.trigger_fast_sync()

    def stop(self) -> None:
        self._stop_evt.set()
        self._fast_sync_evt.set()
        self._worker_client.close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        self._close_local_conn()

    def trigger_fast_sync(self) -> None:
        self._fast_sync_evt.set()

    def set_failure_callback(
        self,
        callback: Callable[[dict[str, Any]], None] | None,
    ) -> None:
        with self._lock:
            self._failure_callback = callback

    def is_ready(self, *, max_stale_sec: float) -> bool:
        with self._lock:
            if self._local_conn is None or self.last_sync_ok_ts <= 0:
                return False
            age_sec = max(0.0, time.time() - self.last_sync_ok_ts)
            return age_sec <= max(1.0, float(max_stale_sec))

    def health_snapshot(self) -> dict[str, Any]:
        with self._lock:
            age_sec = (
                max(0.0, time.time() - self.last_sync_ok_ts)
                if self.last_sync_ok_ts > 0
                else None
            )
            return {
                "enabled": True,
                "ready": self._local_conn is not None and self.last_sync_ok_ts > 0,
                "last_sync_ok_ts": float(self.last_sync_ok_ts),
                "last_sync_age_sec": age_sec,
                "last_sync_error": str(self.last_sync_error or ""),
                "last_sync_error_class": str(self.last_sync_error_class or ""),
                "consecutive_failures": int(self.consecutive_failures),
                "state": dict(self.last_state),
                "local_db_path": self.local_db_path,
            }

    def fetch_all(self, query: str, params=()):
        with self._lock:
            if not self._local_conn:
                raise RuntimeError("Local replica connection is not initialized")
            cursor = self._local_conn.execute(query, params)
            try:
                return cursor.fetchall()
            finally:
                cursor.close()

    def fetch_one(self, query: str, params=()):
        with self._lock:
            if not self._local_conn:
                raise RuntimeError("Local replica connection is not initialized")
            cursor = self._local_conn.execute(query, params)
            try:
                return cursor.fetchone()
            finally:
                cursor.close()

    def sync_once(self) -> bool:
        if not self._sync_lock.acquire(blocking=False):
            return False
        started = time.perf_counter()
        temp_path = self._build_temp_path()
        try:
            local_state = self._read_local_state()
            response = self._worker_client.sync(
                local_state=local_state,
                temp_db_path=temp_path,
                timeout_sec=self.sync_timeout_sec,
            )
            status = str(response.get("status") or "")
            state = dict(response.get("state") or {})
            if status == "snapshot_ready":
                self._swap_local_replica(str(response.get("temp_db_path") or temp_path))
            elif status != "unchanged":
                raise RuntimeError(f"Unexpected local replica worker status: {status}")
            with self._lock:
                self.last_sync_ok_ts = time.time()
                self.last_sync_error = None
                self.last_sync_error_class = ""
                self.consecutive_failures = 0
                self.last_state = state
            record_metric(
                "local_replica_sync_duration_ms",
                round((time.perf_counter() - started) * 1000.0, 3),
                result=status,
                change_cursor=state.get("change_cursor"),
                db_cycle=str(state.get("db_cycle") or ""),
            )
            return True
        except Exception as exc:
            with self._lock:
                self.last_sync_error = str(exc)
                self.last_sync_error_class = str(
                    getattr(exc, "remote_error_class", "")
                    or type(exc).__name__
                )
                self.consecutive_failures += 1
                failure_count = self.consecutive_failures
                failure_callback = self._failure_callback
            record_metric(
                "local_replica_sync_failed",
                1,
                force_flush=failure_count >= 2,
                error_class=type(exc).__name__,
                consecutive_failures=failure_count,
                duration_ms=round((time.perf_counter() - started) * 1000.0, 3),
            )
            self.logger.warning(
                "Local replica sync failed (%s -> %s): %s",
                self.central_db_path,
                self.local_db_path,
                exc,
            )
            if failure_count >= 2 and failure_callback is not None:
                try:
                    failure_callback(self.health_snapshot())
                except Exception as callback_exc:
                    self.logger.warning(
                        "Local replica failure callback failed: %s",
                        callback_exc,
                    )
            return False
        finally:
            self._remove_replica_with_sidecars(temp_path)
            self._sync_lock.release()

    def _worker(self) -> None:
        while not self._stop_evt.is_set():
            triggered = self._fast_sync_evt.wait(self.sync_interval_sec)
            if self._stop_evt.is_set():
                return
            if triggered:
                self._fast_sync_evt.clear()
            self.sync_once()

    def _start_worker(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._worker,
            name=f"LocalReplicaSync:{os.path.basename(self.local_db_path)}",
            daemon=True,
        )
        self._thread.start()

    def _ensure_local_conn(self) -> None:
        with self._lock:
            if self._local_conn is not None:
                return
            if (
                not os.path.isfile(self.local_db_path)
                or os.path.getsize(self.local_db_path) <= 0
            ):
                return
            conn = self._open_local_readonly(self.local_db_path)
            try:
                row = conn.execute("PRAGMA quick_check").fetchone()
                if not row or str(row[0] or "").lower() != "ok":
                    raise sqlite3.DatabaseError(
                        f"local replica quick_check failed: {row[0] if row else 'empty'}"
                    )
            except Exception:
                conn.close()
                raise
            self._local_conn = conn

    @staticmethod
    def _open_local_readonly(db_path: str) -> sqlite3.Connection:
        conn = sqlite3.connect(
            build_sqlite_file_uri(db_path, mode="ro"),
            uri=True,
            check_same_thread=False,
            isolation_level=None,
            timeout=1.0,
        )
        configure_connection(conn, readonly=True, profile="local_replica")
        return conn

    def _read_local_state(self) -> dict[str, Any]:
        with self._lock:
            if self._local_conn is None:
                return {}
            try:
                cycle_row = self._local_conn.execute(
                    "SELECT value FROM meta WHERE key = 'db_cycle_started_at'"
                ).fetchone()
                cursor_row = self._local_conn.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM change_log"
                ).fetchone()
                schema_row = self._local_conn.execute(
                    "SELECT value FROM meta "
                    "WHERE key = 'unified_schema_fastpath_rev'"
                ).fetchone()
                return {
                    "db_cycle": str(
                        cycle_row[0]
                        if cycle_row and cycle_row[0] is not None
                        else ""
                    ),
                    "change_cursor": int(cursor_row[0] or 0)
                    if cursor_row
                    else 0,
                    "schema_revision": str(
                        schema_row[0]
                        if schema_row and schema_row[0] is not None
                        else ""
                    ),
                }
            except Exception:
                return {}

    def _build_temp_path(self) -> str:
        return (
            f"{self.local_db_path}.sync_tmp."
            f"{os.getpid()}_{threading.get_ident()}_{int(time.time() * 1000)}"
        )

    def _swap_local_replica(self, temp_path: str) -> None:
        if not temp_path or not os.path.isfile(temp_path):
            raise FileNotFoundError(f"Local replica snapshot is missing: {temp_path}")
        with self._lock:
            self._close_local_conn_locked()
            self._remove_replica_sidecars(self.local_db_path)
            os.replace(temp_path, self.local_db_path)
            self._remove_replica_sidecars(temp_path)
            self._local_conn = self._open_local_readonly(self.local_db_path)

    def _close_local_conn(self) -> None:
        with self._lock:
            self._close_local_conn_locked()

    def _close_local_conn_locked(self) -> None:
        if self._local_conn:
            try:
                self._local_conn.close()
            except Exception:
                pass
            self._local_conn = None

    @staticmethod
    def _remove_replica_sidecars(db_path: str) -> None:
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                sidecar_path = f"{db_path}{suffix}"
                if os.path.isfile(sidecar_path):
                    os.remove(sidecar_path)
            except OSError:
                pass

    @classmethod
    def _remove_replica_with_sidecars(cls, db_path: str) -> None:
        try:
            if os.path.isfile(db_path):
                os.remove(db_path)
        except OSError:
            pass
        cls._remove_replica_sidecars(db_path)
