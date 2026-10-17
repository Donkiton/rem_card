from __future__ import annotations

import hashlib
import logging
import multiprocessing
import os
import re
import socket
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from rem_card.app.db_lifecycle import DB_CYCLE_META_KEY
from rem_card.app.db_wait_diagnostics import observe, observed_call, mark_stage, current_operation, bind_connection
from rem_card.app.sqlite_shared import FileWriteLock, configure_connection, describe_sqlite_lock_holder
from rem_card.app.sqlite_uri import build_sqlite_file_uri


DEFAULT_REPLICA_SYNC_TIMEOUT_SEC = 6.0
LOCAL_REPLICA_BACKUP_PAGES = 256
WRITER_LOCK_PROBE_INTERVAL_SEC = 0.1
WORKER_POLL_SLICE_SEC = 0.05
WORKER_STOP_WAIT_SEC = 0.5
STOPPED_WORKER_CLEANUP_TIMEOUT_SEC = 0.5


class LocalReplicaWorkerError(RuntimeError):
    def __init__(self, message: str, *, remote_error_class: str = ""):
        self.remote_error_class = str(remote_error_class or "")
        super().__init__(str(message or "Ошибка процесса локальной реплики"))


class LocalReplicaWorkerTimeout(LocalReplicaWorkerError):
    def __init__(self, timeout_sec: float, *, stage: str = ""):
        self.timeout_sec = float(timeout_sec)
        self.stage = str(stage or "")
        stage_suffix = f" Этап: {self.stage}." if self.stage else ""
        super().__init__(
            f"Обновление локальной реплики превысило безопасный тайм-аут "
            f"{self.timeout_sec:.1f} с.{stage_suffix}"
        )


class LocalReplicaRotationBusy(LocalReplicaWorkerError):
    def __init__(self):
        super().__init__(
            "Обновление локальной реплики отложено: выполняется ротация базы."
        )


class LocalReplicaSnapshotBusy(LocalReplicaWorkerError):
    def __init__(self, gate_diagnostics: dict | None = None):
        self.gate_diagnostics = dict(gate_diagnostics or {})
        super().__init__(
            "Обновление локальной реплики отложено: другой клиент уже "
            "копирует центральную базу."
        )


class LocalReplicaWriterBusy(LocalReplicaWorkerError):
    def __init__(self):
        super().__init__(
            "Обновление локальной реплики отложено: клиническая запись "
            "имеет приоритет."
        )


def replica_snapshot_lease_dir(central_db_path: str) -> str:
    baza_dir = os.path.dirname(os.path.dirname(os.path.abspath(central_db_path)))
    return os.path.join(baza_dir, "locks", "replica_snapshots")


def replica_snapshot_gate_path(central_db_path: str, *, host: str | None = None) -> str:
    baza_dir = os.path.dirname(os.path.dirname(os.path.abspath(central_db_path)))
    return os.path.join(baza_dir, "locks", _snapshot_gate_name(host))


def _snapshot_gate_name(host: str | None = None) -> str:
    # Serialize roles on one workstation only. A dead foreign workstation must
    # not stop every reader. The separate per-worker leases still protect
    # rotation; SQLite and the writer-priority checks protect clinical writes.
    identity = str(host or socket.gethostname()).strip().casefold()
    host_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"replica_snapshot_copy_{host_key}.lock"


def database_write_lock_path(central_db_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(central_db_path)), "db.lock")


def malformed_lock_quarantine_dir(central_db_path: str) -> str:
    baza_dir = os.path.dirname(os.path.dirname(os.path.abspath(central_db_path)))
    return os.path.join(baza_dir, "quarantine", "locks")


def _safe_lease_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "")).strip("._")
    return cleaned[:80] or "client"


def _remove_with_sidecars(db_path: str) -> None:
    for candidate in (
        db_path,
        f"{db_path}-wal",
        f"{db_path}-shm",
        f"{db_path}-journal",
    ):
        try:
            if os.path.isfile(candidate):
                os.remove(candidate)
        except OSError:
            pass


def _read_database_state(conn: sqlite3.Connection) -> dict[str, Any]:
    try:
        cycle_row = conn.execute(
            "SELECT value FROM meta WHERE key = ?",
            (DB_CYCLE_META_KEY,),
        ).fetchone()
    except sqlite3.OperationalError:
        cycle_row = None
    try:
        cursor_row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM change_log"
        ).fetchone()
    except sqlite3.OperationalError:
        cursor_row = None
    try:
        schema_row = conn.execute(
            "SELECT value FROM meta WHERE key = 'unified_schema_fastpath_rev'"
        ).fetchone()
    except sqlite3.OperationalError:
        schema_row = None
    return {
        "db_cycle": str(cycle_row[0] if cycle_row and cycle_row[0] is not None else ""),
        "change_cursor": int(cursor_row[0] or 0) if cursor_row else None,
        "schema_revision": str(
            schema_row[0] if schema_row and schema_row[0] is not None else ""
        ),
    }


def _states_match(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
    if not left or not right:
        return False
    return all(
        left.get(key) == right.get(key)
        for key in ("db_cycle", "change_cursor", "schema_revision")
    )


def _open_central_readonly(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(
        build_sqlite_file_uri(db_path, mode="ro"),
        uri=True,
        check_same_thread=True,
        isolation_level=None,
        timeout=0.25,
    )
    try:
        configure_connection(conn, readonly=True, profile="network")
        conn.execute("PRAGMA busy_timeout = 250")
        return conn
    except Exception:
        conn.close()
        raise


def _open_temp_replica(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path,
        check_same_thread=True,
        isolation_level=None,
        timeout=1.0,
    )
    try:
        configure_connection(conn, profile="local_replica")
        return conn
    except Exception:
        conn.close()
        raise


def _sync_snapshot(
    message: dict[str, Any],
    *,
    before_backup: Callable[[], None] | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    central_db_path = os.path.abspath(str(message.get("central_db_path") or ""))
    temp_db_path = os.path.abspath(str(message.get("temp_db_path") or ""))
    expected_state = dict(message.get("local_state") or {})
    debug_delay_sec = max(0.0, float(message.get("debug_delay_sec") or 0.0))
    writer_lock_path = os.path.abspath(
        str(
            message.get("writer_lock_path")
            or database_write_lock_path(central_db_path)
        )
    )
    if progress_callback is not None:
        progress_callback("central_probe")
    if not os.path.isfile(central_db_path):
        raise FileNotFoundError(f"central DB missing: {central_db_path}")

    central_conn: sqlite3.Connection | None = None
    temp_conn: sqlite3.Connection | None = None
    failed = False
    _remove_with_sidecars(temp_db_path)
    try:
        if progress_callback is not None:
            progress_callback("central_open")
        central_conn = _open_central_readonly(central_db_path)
        bind_connection(central_conn)
        mark_stage("replica_state_read")
        if progress_callback is not None:
            progress_callback("state_read")
        central_state = _read_database_state(central_conn)
        if _states_match(central_state, expected_state):
            return {
                "status": "unchanged",
                "state": central_state,
            }

        central_conn.close()
        central_conn = None
        if before_backup is not None:
            before_backup()
        if debug_delay_sec:
            time.sleep(debug_delay_sec)
        if writer_lock_path and os.path.exists(writer_lock_path):
            raise LocalReplicaWriterBusy()

        if progress_callback is not None:
            progress_callback("central_open")
        central_conn = _open_central_readonly(central_db_path)
        bind_connection(central_conn)
        mark_stage("replica_state_read")
        if progress_callback is not None:
            progress_callback("state_read")
        central_state = _read_database_state(central_conn)
        if _states_match(central_state, expected_state):
            return {
                "status": "unchanged",
                "state": central_state,
            }

        os.makedirs(os.path.dirname(temp_db_path), exist_ok=True)
        temp_conn = _open_temp_replica(temp_db_path)

        last_writer_probe = 0.0

        def yield_to_writer(_status: int, _remaining: int, _total: int) -> None:
            nonlocal last_writer_probe
            now = time.monotonic()
            sqlite_busy = _status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
            if (
                not sqlite_busy
                and now - last_writer_probe < WRITER_LOCK_PROBE_INTERVAL_SEC
            ):
                return
            last_writer_probe = now
            if writer_lock_path and os.path.exists(writer_lock_path):
                raise LocalReplicaWriterBusy()

        mark_stage("replica_backup")
        if progress_callback is not None:
            progress_callback("backup")
        central_conn.backup(
            temp_conn,
            pages=LOCAL_REPLICA_BACKUP_PAGES,
            progress=yield_to_writer,
            sleep=0.01,
        )
        mark_stage("replica_checkpoint")
        if progress_callback is not None:
            progress_callback("checkpoint")
        checkpoint_row = temp_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint_row and int(checkpoint_row[0] or 0) not in (0,):
            raise sqlite3.OperationalError(
                f"local replica WAL checkpoint failed: {tuple(checkpoint_row)}"
            )
        mark_stage("replica_quick_check")
        if progress_callback is not None:
            progress_callback("quick_check")
        quick_row = temp_conn.execute("PRAGMA quick_check").fetchone()
        if not quick_row or str(quick_row[0] or "").lower() != "ok":
            raise sqlite3.DatabaseError(
                f"local replica quick_check failed: {quick_row[0] if quick_row else 'empty'}"
            )
        snapshot_state = _read_database_state(temp_conn)
        temp_conn.close()
        temp_conn = None
        mark_stage("replica_fsync")
        if progress_callback is not None:
            progress_callback("fsync")
        with open(temp_db_path, "r+b") as fh:
            fh.flush()
            os.fsync(fh.fileno())
        return {
            "status": "snapshot_ready",
            "state": snapshot_state,
            "temp_db_path": temp_db_path,
        }
    except Exception:
        failed = True
        raise
    finally:
        if temp_conn is not None:
            try:
                temp_conn.close()
            except Exception:
                pass
        if central_conn is not None:
            try:
                central_conn.close()
            except Exception:
                pass
        if failed:
            _remove_with_sidecars(temp_db_path)


def _sync_snapshot_with_network_lease(
    message: dict[str, Any],
    *,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    with observe("replica_snapshot", resource=message.get("central_db_path"), stage="replica_lease_wait"):
        return _sync_snapshot_with_network_lease_observed(message, progress_callback=progress_callback)


def _sync_snapshot_with_network_lease_observed(
    message: dict[str, Any], *, progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run every potentially blocking network operation inside the worker.

    Windows SMB filesystem calls are not reliably bounded by SQLite timeouts.
    Keeping rotation checks, lease I/O and the database snapshot in this
    process lets the parent enforce one hard deadline for the complete sync.
    """
    central_db_path = os.path.abspath(
        str(message.get("central_db_path") or "")
    )
    raw_rotation_lock_path = str(message.get("rotation_lock_path") or "")
    rotation_lock_path = (
        os.path.abspath(raw_rotation_lock_path)
        if raw_rotation_lock_path
        else ""
    )
    snapshot_lease_path = os.path.abspath(
        str(message.get("snapshot_lease_path") or "")
    )
    snapshot_gate_path = os.path.abspath(
        str(
            message.get("snapshot_gate_path")
            or replica_snapshot_gate_path(central_db_path)
        )
    )
    writer_lock_path = os.path.abspath(
        str(
            message.get("writer_lock_path")
            or database_write_lock_path(central_db_path)
        )
    )
    malformed_quarantine_dir = os.path.abspath(
        str(message.get("malformed_quarantine_dir") or "")
    )
    lease_duration_sec = max(
        15.0,
        float(message.get("lease_duration_sec") or 15.0),
    )
    debug_lease_delay_sec = max(
        0.0,
        float(message.get("debug_lease_delay_sec") or 0.0),
    )
    rotation_gate = (
        FileWriteLock(
            rotation_lock_path,
            stale_timeout_sec=60.0,
            allow_expired_lease_cleanup=True,
            allow_legacy_replica_cleanup=True,
            allow_malformed_cleanup=True,
            malformed_quarantine_dir=malformed_quarantine_dir,
        )
        if rotation_lock_path
        else None
    )
    snapshot_lease = FileWriteLock(
        snapshot_lease_path,
        stale_timeout_sec=60.0,
        lease_duration_sec=lease_duration_sec,
        allow_expired_lease_cleanup=True,
        allow_legacy_replica_cleanup=True,
        allow_malformed_cleanup=True,
        malformed_quarantine_dir=malformed_quarantine_dir,
    )
    snapshot_gate = FileWriteLock(
        snapshot_gate_path,
        stale_timeout_sec=60.0,
        lease_duration_sec=lease_duration_sec,
        # A workstation gate is normally owned by this host, so confirmed dead
        # local PIDs can be recovered. Never reclaim ambiguous/foreign owners
        # merely by age, including after a workstation rename or SMB delay.
        allow_expired_lease_cleanup=False,
        allow_legacy_replica_cleanup=False,
        allow_malformed_cleanup=False,
        malformed_quarantine_dir=malformed_quarantine_dir,
    )
    snapshot_lease_acquired = False
    snapshot_gate_acquired = False
    try:
        if progress_callback is not None:
            progress_callback("lease_acquire")
        if debug_lease_delay_sec:
            time.sleep(debug_lease_delay_sec)
        if writer_lock_path and os.path.exists(writer_lock_path):
            raise LocalReplicaWriterBusy()
        if rotation_gate is not None and os.path.exists(rotation_lock_path):
            rotation_gate.cleanup_abandoned(
                source="local_replica_rotation_gate",
            )
            if os.path.exists(rotation_lock_path):
                raise LocalReplicaRotationBusy()

        owner_id = (
            f"{socket.gethostname()}:{os.getpid()}:local_replica_snapshot"
        )
        if not snapshot_lease.acquire(
            owner_id=owner_id,
            source="local_replica_snapshot",
        ):
            raise LocalReplicaRotationBusy()
        snapshot_lease_acquired = True

        # Close the race where rotation starts after the first check but before
        # this client publishes its reader lease.
        if rotation_gate is not None and os.path.exists(rotation_lock_path):
            raise LocalReplicaRotationBusy()

        def acquire_snapshot_gate() -> None:
            nonlocal snapshot_gate_acquired
            if writer_lock_path and os.path.exists(writer_lock_path):
                raise LocalReplicaWriterBusy()
            if progress_callback is not None:
                progress_callback("snapshot_gate_acquire")
            if not snapshot_gate.acquire(
                owner_id=owner_id,
                source="local_replica_snapshot_gate",
            ):
                raise LocalReplicaSnapshotBusy(describe_sqlite_lock_holder(snapshot_gate_path))
            snapshot_gate_acquired = True
            if writer_lock_path and os.path.exists(writer_lock_path):
                raise LocalReplicaWriterBusy()
            if rotation_gate is not None and os.path.exists(rotation_lock_path):
                raise LocalReplicaRotationBusy()

        if progress_callback is not None:
            progress_callback("snapshot")
        result = _sync_snapshot(
            message,
            before_backup=acquire_snapshot_gate,
            progress_callback=progress_callback,
        )
        return result
    finally:
        try:
            if snapshot_gate_acquired:
                try:
                    if progress_callback is not None:
                        progress_callback("snapshot_gate_release")
                except Exception:
                    pass  # IPC diagnostics must never prevent lock release.
                snapshot_gate.release()
        finally:
            if snapshot_lease_acquired:
                try:
                    if progress_callback is not None:
                        progress_callback("lease_release")
                except Exception:
                    pass
                snapshot_lease.release()


def _send_error(pipe, exc: Exception) -> None:
    try:
        pipe.send(
            {
                "ok": False,
                "error_class": type(exc).__name__,
                "error": str(exc),
                "gate_diagnostics": getattr(exc, "gate_diagnostics", {}),
            }
        )
    except Exception:
        pass


def _cleanup_stopped_worker_entry(
    pipe,
    paths_and_sources: tuple[tuple[str, str], ...],
    worker_pid: int,
) -> None:
    result: dict[str, bool] = {}
    try:
        for path, source in paths_and_sources:
            try:
                result[source] = bool(
                    FileWriteLock(path).cleanup_stopped_local_owner(
                        owner_pid=worker_pid,
                        owner_source=source,
                    )
                )
            except Exception:
                result[source] = False
        pipe.send(result)
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _worker_main(pipe) -> None:
    while True:
        try:
            message = pipe.recv()
        except EOFError:
            return
        command = str(message.get("cmd") or "")
        if command == "shutdown":
            return
        if command != "sync":
            _send_error(pipe, ValueError(f"Unsupported local replica command: {command}"))
            continue
        try:
            pipe.send(
                {
                    "ok": True,
                    **_sync_snapshot_with_network_lease(
                        message,
                        progress_callback=lambda stage: pipe.send(
                            {"progress": str(stage or "")}
                        ),
                    ),
                }
            )
        except Exception as exc:
            _send_error(pipe, exc)


class LocalReplicaWorkerClient:
    def __init__(
        self,
        *,
        central_db_path: str,
        rotation_lock_path: str | None = None,
        snapshot_lease_dir: str | None = None,
        snapshot_lease_id: str | None = None,
        timeout_sec: float = DEFAULT_REPLICA_SYNC_TIMEOUT_SEC,
    ):
        self.central_db_path = os.path.abspath(central_db_path)
        self.rotation_lock_path = (
            os.path.abspath(rotation_lock_path)
            if rotation_lock_path
            else ""
        )
        self.snapshot_lease_dir = os.path.abspath(
            snapshot_lease_dir or replica_snapshot_lease_dir(self.central_db_path)
        )
        lease_id = snapshot_lease_id or (
            f"{socket.gethostname()}_{os.getpid()}_{uuid.uuid4().hex[:12]}"
        )
        self.snapshot_lease_path = os.path.join(
            self.snapshot_lease_dir,
            f"{_safe_lease_component(lease_id)}.lock",
        )
        self.snapshot_gate_path = os.path.join(
            os.path.dirname(self.snapshot_lease_dir),
            _snapshot_gate_name(),
        )
        self.writer_lock_path = database_write_lock_path(self.central_db_path)
        self.malformed_quarantine_dir = malformed_lock_quarantine_dir(
            self.central_db_path
        )
        self.timeout_sec = max(0.2, min(30.0, float(timeout_sec)))
        self._mutex = threading.Lock()
        self._state_lock = threading.RLock()
        self._closing = threading.Event()
        self._process = None
        self._pipe = None
        self._fenced = False
        self._active_temp_path = ""
        self._cleanup_process = None
        self._cleanup_pipe = None
        self._cleanup_required_pid: int | None = None
        self._cleanup_completed_pid: int | None = None

    def _ensure_started(self) -> None:
        with self._state_lock:
            if self._closing.is_set():
                raise LocalReplicaWorkerError("Процесс локальной реплики останавливается")
            if self._fenced:
                if not self._terminate_locked():
                    raise LocalReplicaWorkerError(
                        "Предыдущий процесс локальной реплики не завершён; новый запуск запрещён"
                    )
            if not self._reap_cleanup_locked():
                raise LocalReplicaWorkerError(
                    "Очистка предыдущего процесса локальной реплики не завершена"
                )
            if (
                self._process is not None
                and self._is_alive(self._process)
                and self._pipe is not None
            ):
                return
            if not self._terminate_locked():
                raise LocalReplicaWorkerError(
                    "Предыдущий процесс локальной реплики не удалось завершить"
                )
            context = multiprocessing.get_context("spawn")
            parent_pipe, child_pipe = context.Pipe(duplex=True)
            process = context.Process(
                target=_worker_main,
                args=(child_pipe,),
                name="RemCardLocalReplicaWorker",
                daemon=True,
            )
            try:
                process.start()
            except Exception:
                parent_pipe.close()
                child_pipe.close()
                raise
            child_pipe.close()
            self._process = process
            self._pipe = parent_pipe
            # Process IDs may be reused. A new worker must never inherit the
            # previous owner's completed cleanup marker.
            self._cleanup_completed_pid = None

    @staticmethod
    def _is_alive(process) -> bool:
        try:
            return bool(process is not None and process.is_alive())
        except (OSError, ValueError):
            # A failed liveness probe is not proof of death.  Keep the handle
            # fenced until a later probe can confirm that the process exited.
            return process is not None

    @staticmethod
    def _remaining(deadline: float | None, cap: float) -> float:
        if deadline is None:
            return max(0.0, float(cap))
        return max(0.0, min(float(cap), deadline - time.monotonic()))

    def _terminate(self, *, deadline: float | None = None) -> bool:
        with self._state_lock:
            return self._terminate_locked(deadline=deadline)

    def _terminate_locked(self, *, deadline: float | None = None) -> bool:
        process = self._process
        pipe = self._pipe
        if process is None:
            if pipe is not None:
                try:
                    pipe.close()
                except Exception:
                    pass
            self._pipe = None
            self._fenced = False
            self._active_temp_path = ""
            if self._cleanup_required_pid is not None:
                cleanup_ok = self._bounded_cleanup_stopped_worker_locks(
                    self._cleanup_required_pid,
                    deadline=deadline,
                )
                if cleanup_ok:
                    self._cleanup_required_pid = None
                return cleanup_ok
            return self._reap_cleanup_locked(deadline=deadline)

        worker_pid = process.pid
        forced = self._is_alive(process)
        if forced and worker_pid is not None:
            self._cleanup_required_pid = int(worker_pid)
        try:
            if self._is_alive(process):
                process.terminate()
                process.join(
                    timeout=self._remaining(deadline, WORKER_STOP_WAIT_SEC)
                )
            if self._is_alive(process):
                try:
                    process.kill()
                except Exception:
                    pass
                process.join(
                    timeout=self._remaining(deadline, WORKER_STOP_WAIT_SEC)
                )
        except Exception:
            self._fenced = True
            return False

        if self._is_alive(process):
            # Keep both handles: the child may still write its current temp
            # snapshot or publish a delayed pipe response.  A replacement
            # worker must not start until death is confirmed.
            self._fenced = True
            return False

        if pipe is not None:
            try:
                pipe.close()
            except Exception:
                pass
        cleanup_ok = True
        cleanup_pid = self._cleanup_required_pid
        if cleanup_pid is not None:
            cleanup_ok = self._bounded_cleanup_stopped_worker_locks(
                cleanup_pid,
                deadline=deadline,
            )
        try:
            process.close()
        except Exception:
            pass
        if self._process is process:
            self._process = None
            self._pipe = None
        self._fenced = False
        self._active_temp_path = ""
        if cleanup_ok:
            self._cleanup_required_pid = None
        return cleanup_ok

    def _bounded_cleanup_stopped_worker_locks(
        self,
        worker_pid: int,
        *,
        deadline: float | None = None,
    ) -> bool:
        if not self._reap_cleanup_locked(
            stop_if_alive=False,
            deadline=deadline,
        ):
            return False
        if self._cleanup_completed_pid == worker_pid:
            return True
        if deadline is not None and time.monotonic() >= deadline:
            return False
        if deadline is not None and deadline - time.monotonic() < 0.05:
            return False
        self._cleanup_completed_pid = None
        paths_and_sources = (
            (self.snapshot_gate_path, "local_replica_snapshot_gate"),
            (self.snapshot_lease_path, "local_replica_snapshot"),
        )
        context = multiprocessing.get_context("spawn")
        receiving, sending = context.Pipe(duplex=False)
        cleanup = context.Process(
            target=_cleanup_stopped_worker_entry,
            args=(sending, paths_and_sources, worker_pid),
            name="RemCardReplicaLockCleanup",
            daemon=True,
        )
        try:
            cleanup.start()
        except Exception:
            receiving.close()
            sending.close()
            logging.getLogger(__name__).warning(
                "Failed to start bounded replica lock cleanup (pid=%s)",
                worker_pid,
                exc_info=True,
            )
            return False
        sending.close()
        self._cleanup_process = cleanup
        self._cleanup_pipe = receiving
        try:
            receiving.poll(
                self._remaining(deadline, STOPPED_WORKER_CLEANUP_TIMEOUT_SEC)
            )
        except Exception:
            pass
        reaped = self._reap_cleanup_locked(
            stop_if_alive=True,
            deadline=deadline,
        )
        return reaped and self._cleanup_completed_pid == worker_pid

    def _reap_cleanup_locked(
        self,
        *,
        stop_if_alive: bool = True,
        deadline: float | None = None,
    ) -> bool:
        cleanup = self._cleanup_process
        receiving = self._cleanup_pipe
        if cleanup is None:
            if receiving is not None:
                try:
                    receiving.close()
                except Exception:
                    pass
                self._cleanup_pipe = None
            return True
        if receiving is not None:
            try:
                if receiving.poll(0):
                    receiving.recv()
                    self._cleanup_completed_pid = self._cleanup_required_pid
            except (EOFError, OSError):
                pass
        if self._is_alive(cleanup) and stop_if_alive:
            try:
                cleanup.terminate()
                cleanup.join(timeout=self._remaining(deadline, 0.2))
                if self._is_alive(cleanup):
                    cleanup.kill()
                    cleanup.join(timeout=self._remaining(deadline, 0.2))
            except Exception:
                pass
        if self._is_alive(cleanup):
            # Retain the handle and coalesce future requests into this cleanup
            # attempt.  Subsequent lifecycle calls can reap it once death is
            # observable; they never spawn an overlapping cleanup process.
            return False
        if receiving is not None and self._cleanup_completed_pid is None:
            try:
                if receiving.poll(0):
                    receiving.recv()
                    self._cleanup_completed_pid = self._cleanup_required_pid
            except (EOFError, OSError):
                pass
        if receiving is not None:
            try:
                receiving.close()
            except Exception:
                pass
        try:
            cleanup.close()
        except Exception:
            pass
        if self._cleanup_process is cleanup:
            self._cleanup_process = None
            self._cleanup_pipe = None
        return True

    def can_cleanup_temp_path(self, temp_db_path: str) -> bool:
        candidate = os.path.abspath(temp_db_path)
        with self._state_lock:
            return not (
                self._active_temp_path
                and os.path.abspath(self._active_temp_path) == candidate
                and self._process is not None
                and self._is_alive(self._process)
            )

    @observed_call("replica_worker_mutex_wait")
    def sync(
        self,
        *,
        local_state: dict[str, Any] | None,
        temp_db_path: str,
        timeout_sec: float | None = None,
        debug_delay_sec: float = 0.0,
        debug_lease_delay_sec: float = 0.0,
    ) -> dict[str, Any]:
        effective_timeout = max(
            0.2,
            min(30.0, float(timeout_sec or self.timeout_sec)),
        )
        deadline = time.monotonic() + effective_timeout
        mutex_remaining = deadline - time.monotonic()
        if mutex_remaining <= 0 or not self._mutex.acquire(timeout=mutex_remaining):
            raise LocalReplicaWorkerTimeout(effective_timeout, stage="mutex_wait")
        try:
            last_stage = "worker_start"
            mark_stage(last_stage)
            try:
                self._ensure_started()
                if time.monotonic() >= deadline:
                    raise LocalReplicaWorkerTimeout(effective_timeout, stage=last_stage)
                diagnostic = current_operation()
                if diagnostic is not None:
                    diagnostic.worker(self._process.pid)
                mark_stage("replica_worker_wait")
                temp_db_path = os.path.abspath(temp_db_path)
                with self._state_lock:
                    if self._fenced or self._closing.is_set():
                        raise LocalReplicaWorkerError(
                            "Процесс локальной реплики останавливается"
                        )
                    pipe = self._pipe
                    self._active_temp_path = temp_db_path
                pipe.send(
                    {
                        "cmd": "sync",
                        "central_db_path": self.central_db_path,
                        "temp_db_path": os.path.abspath(temp_db_path),
                        "local_state": dict(local_state or {}),
                        "debug_delay_sec": max(0.0, float(debug_delay_sec)),
                        "debug_lease_delay_sec": max(
                            0.0,
                            float(debug_lease_delay_sec),
                        ),
                        "rotation_lock_path": self.rotation_lock_path,
                        "snapshot_lease_path": self.snapshot_lease_path,
                        "snapshot_gate_path": self.snapshot_gate_path,
                        "writer_lock_path": self.writer_lock_path,
                        "malformed_quarantine_dir": self.malformed_quarantine_dir,
                        "lease_duration_sec": max(
                            15.0,
                            effective_timeout * 2.0 + 3.0,
                        ),
                    }
                )
                while True:
                    if self._closing.is_set():
                        stopped = self._terminate()
                        if stopped:
                            _remove_with_sidecars(temp_db_path)
                        raise LocalReplicaWorkerError(
                            "Обновление локальной реплики остановлено"
                        )
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LocalReplicaWorkerTimeout(
                            effective_timeout,
                            stage=last_stage,
                        )
                    if not pipe.poll(min(remaining, WORKER_POLL_SLICE_SEC)):
                        continue
                    # A stream of progress messages must not move the hard
                    # deadline: validate it again before consuming each one.
                    if time.monotonic() >= deadline:
                        raise LocalReplicaWorkerTimeout(
                            effective_timeout,
                            stage=last_stage,
                        )
                    response = pipe.recv()
                    progress = str(response.get("progress") or "")
                    if progress:
                        last_stage = progress
                        mark_stage(f"replica_{progress}")
                        continue
                    break
            except LocalReplicaWorkerTimeout:
                if self._terminate():
                    _remove_with_sidecars(temp_db_path)
                raise
            except (EOFError, OSError) as exc:
                if self._terminate():
                    _remove_with_sidecars(temp_db_path)
                raise LocalReplicaWorkerError(
                    f"Процесс локальной реплики завершился без результата: {exc}"
                ) from exc

            with self._state_lock:
                self._active_temp_path = ""
            if not response.get("ok"):
                _remove_with_sidecars(temp_db_path)
                remote_error_class = str(response.get("error_class") or "")
                if remote_error_class == LocalReplicaRotationBusy.__name__:
                    raise LocalReplicaRotationBusy()
                if remote_error_class == LocalReplicaSnapshotBusy.__name__:
                    raise LocalReplicaSnapshotBusy(response.get("gate_diagnostics"))
                if remote_error_class == LocalReplicaWriterBusy.__name__:
                    raise LocalReplicaWriterBusy()
                raise LocalReplicaWorkerError(
                    str(response.get("error") or "Ошибка процесса локальной реплики"),
                    remote_error_class=remote_error_class,
                )
            return dict(response)
        finally:
            self._mutex.release()

    def close(self, *, timeout_sec: float = 0.5) -> bool:
        self._closing.set()
        deadline = time.monotonic() + max(0.0, float(timeout_sec))
        acquired = self._mutex.acquire(
            timeout=max(0.0, deadline - time.monotonic())
        )
        if not acquired:
            return False
        try:
            if (
                self._pipe is not None
                and self._process is not None
                and self._is_alive(self._process)
            ):
                try:
                    self._pipe.send({"cmd": "shutdown"})
                    self._process.join(
                        timeout=self._remaining(deadline, WORKER_STOP_WAIT_SEC)
                    )
                except Exception:
                    pass
            return self._terminate(deadline=deadline)
        finally:
            self._mutex.release()
