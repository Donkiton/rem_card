import logging
import os
import sqlite3
import threading
import time
from typing import Optional

from rem_card.app.sqlite_uri import build_sqlite_file_uri
from rem_card.app.sqlite_shared import configure_connection


class LocalReplicaSync:
    """
    Локальная read-реплика SQLite:
    - читает из local-файла;
    - в фоне копирует central -> local через SQLite backup API.
    """

    def __init__(
        self,
        *,
        central_db_path: str,
        local_db_path: str,
        logger: Optional[logging.Logger] = None,
        sync_interval_sec: float = 5.0,
    ):
        self.central_db_path = central_db_path
        self.local_db_path = local_db_path
        self.sync_interval_sec = max(1.0, float(sync_interval_sec))
        self.logger = logger or logging.getLogger(__name__)

        self._lock = threading.RLock()
        self._stop_evt = threading.Event()
        self._fast_sync_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._local_conn: Optional[sqlite3.Connection] = None

        self.last_sync_ok_ts: float = 0.0
        self.last_sync_error: Optional[str] = None

    def start(self):
        os.makedirs(os.path.dirname(self.local_db_path), exist_ok=True)
        self._ensure_local_conn()

        # Если local-реплики нет, делаем первичный sync блокирующе.
        # Если есть, не блокируем запуск и синхронизируем фоном.
        if not os.path.exists(self.local_db_path) or os.path.getsize(self.local_db_path) == 0:
            self.sync_once()
        else:
            self.trigger_fast_sync()

        self._start_worker()

    def stop(self):
        self._stop_evt.set()
        self._fast_sync_evt.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        self._close_local_conn()

    def trigger_fast_sync(self):
        self._fast_sync_evt.set()

    def fetch_all(self, query: str, params=()):
        with self._lock:
            if not self._local_conn:
                raise RuntimeError("Local replica connection is not initialized")
            cursor = self._local_conn.cursor()
            cursor.execute(query, params)
            return cursor.fetchall()

    def fetch_one(self, query: str, params=()):
        with self._lock:
            if not self._local_conn:
                raise RuntimeError("Local replica connection is not initialized")
            cursor = self._local_conn.cursor()
            cursor.execute(query, params)
            return cursor.fetchone()

    def sync_once(self):
        central_conn = None
        temp_path = None
        try:
            if not os.path.exists(self.central_db_path):
                self.last_sync_error = f"central DB missing: {self.central_db_path}"
                return False

            uri = build_sqlite_file_uri(self.central_db_path, mode="ro")
            central_conn = sqlite3.connect(
                uri,
                uri=True,
                check_same_thread=False,
                isolation_level=None,
                timeout=5.0,
            )
            configure_connection(central_conn, readonly=True, profile="network")
            central_change_cursor = self._read_change_log_cursor(central_conn)

            with self._lock:
                self._ensure_local_conn()
                local_change_cursor = self._read_change_log_cursor(self._local_conn)

            # Ключевая оптимизация: если курсор change_log не изменился,
            # пропускаем полный backup central -> local, снижая I/O на сеть.
            should_copy = not (
                central_change_cursor is not None
                and local_change_cursor is not None
                and local_change_cursor >= central_change_cursor
            )
            if should_copy:
                # ВАЖНО: backup выполняем в отдельный временный файл без удержания _lock,
                # чтобы локальные read-запросы не блокировались на все время копирования.
                temp_path = self._build_temp_replica_copy(central_conn)
                self._swap_local_replica(temp_path)
                temp_path = None

            self.last_sync_ok_ts = time.time()
            self.last_sync_error = None
            return True
        except Exception as exc:
            self.last_sync_error = str(exc)
            self.logger.warning("Local replica sync failed (%s -> %s): %s", self.central_db_path, self.local_db_path, exc)
            return False
        finally:
            if temp_path:
                try:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                except Exception:
                    pass
                self._remove_replica_sidecars(temp_path)
            try:
                if central_conn:
                    central_conn.close()
            except Exception:
                pass

    @staticmethod
    def _read_change_log_cursor(conn: sqlite3.Connection) -> Optional[int]:
        try:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM change_log").fetchone()
            if not row or row[0] is None:
                return 0
            return int(row[0])
        except Exception:
            # Для редких случаев (например, до инициализации schema) просто
            # возвращаем None и не блокируем sync.
            return None

    def _start_worker(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._worker,
            name=f"LocalReplicaSync:{os.path.basename(self.local_db_path)}",
            daemon=True,
        )
        self._thread.start()

    def _worker(self):
        while not self._stop_evt.is_set():
            triggered = self._fast_sync_evt.wait(self.sync_interval_sec)
            if self._stop_evt.is_set():
                return
            if triggered:
                self._fast_sync_evt.clear()
            self.sync_once()

    def _ensure_local_conn(self):
        with self._lock:
            if self._local_conn:
                return
            self._local_conn = self._open_local_conn(self.local_db_path)

    def _open_local_conn(self, db_path: str, *, profile: str = "local_replica") -> sqlite3.Connection:
        conn = sqlite3.connect(
            db_path,
            check_same_thread=False,
            isolation_level=None,
            timeout=5.0,
        )
        configure_connection(conn, profile=profile)
        return conn

    def _build_temp_replica_copy(self, central_conn: sqlite3.Connection) -> str:
        os.makedirs(os.path.dirname(self.local_db_path), exist_ok=True)
        temp_path = (
            f"{self.local_db_path}.sync_tmp."
            f"{os.getpid()}_{threading.get_ident()}_{int(time.time() * 1000)}"
        )
        temp_conn = None
        try:
            temp_conn = self._open_local_conn(temp_path, profile="local_replica")
            with temp_conn:
                central_conn.backup(temp_conn)
        finally:
            if temp_conn:
                try:
                    temp_conn.close()
                except Exception:
                    pass
        return temp_path

    def _swap_local_replica(self, temp_path: str):
        if not temp_path:
            return
        with self._lock:
            self._close_local_conn_locked()
            self._remove_replica_sidecars(self.local_db_path)
            os.replace(temp_path, self.local_db_path)
            self._remove_replica_sidecars(temp_path)
            self._local_conn = self._open_local_conn(self.local_db_path)

    def _close_local_conn(self):
        with self._lock:
            self._close_local_conn_locked()

    def _close_local_conn_locked(self):
        if self._local_conn:
            try:
                self._local_conn.close()
            except Exception:
                pass
            self._local_conn = None

    @staticmethod
    def _remove_replica_sidecars(db_path: str):
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar_path = f"{db_path}{suffix}"
            if not os.path.exists(sidecar_path):
                continue
            try:
                os.remove(sidecar_path)
            except Exception:
                pass
