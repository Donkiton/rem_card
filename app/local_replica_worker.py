from __future__ import annotations

import multiprocessing
import os
import re
import socket
import sqlite3
import threading
import time
import uuid
from typing import Any

from rem_card.app.db_lifecycle import DB_CYCLE_META_KEY
from rem_card.app.sqlite_shared import FileWriteLock, configure_connection
from rem_card.app.sqlite_uri import build_sqlite_file_uri


DEFAULT_REPLICA_SYNC_TIMEOUT_SEC = 6.0


class LocalReplicaWorkerError(RuntimeError):
    def __init__(self, message: str, *, remote_error_class: str = ""):
        self.remote_error_class = str(remote_error_class or "")
        super().__init__(str(message or "Ошибка процесса локальной реплики"))


class LocalReplicaWorkerTimeout(LocalReplicaWorkerError):
    def __init__(self, timeout_sec: float):
        self.timeout_sec = float(timeout_sec)
        super().__init__(
            f"Обновление локальной реплики превысило безопасный тайм-аут "
            f"{self.timeout_sec:.1f} с."
        )


class LocalReplicaRotationBusy(LocalReplicaWorkerError):
    def __init__(self):
        super().__init__(
            "Обновление локальной реплики отложено: выполняется другая операция "
            "снимка или ротации базы."
        )


def replica_snapshot_lease_dir(central_db_path: str) -> str:
    baza_dir = os.path.dirname(os.path.dirname(os.path.abspath(central_db_path)))
    return os.path.join(baza_dir, "locks", "replica_snapshots")


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
    configure_connection(conn, readonly=True, profile="network")
    conn.execute("PRAGMA busy_timeout = 250")
    return conn


def _open_temp_replica(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path,
        check_same_thread=True,
        isolation_level=None,
        timeout=1.0,
    )
    configure_connection(conn, profile="local_replica")
    return conn


def _sync_snapshot(message: dict[str, Any]) -> dict[str, Any]:
    central_db_path = os.path.abspath(str(message.get("central_db_path") or ""))
    temp_db_path = os.path.abspath(str(message.get("temp_db_path") or ""))
    expected_state = dict(message.get("local_state") or {})
    debug_delay_sec = max(0.0, float(message.get("debug_delay_sec") or 0.0))
    if not os.path.isfile(central_db_path):
        raise FileNotFoundError(f"central DB missing: {central_db_path}")

    central_conn: sqlite3.Connection | None = None
    temp_conn: sqlite3.Connection | None = None
    _remove_with_sidecars(temp_db_path)
    try:
        if debug_delay_sec:
            time.sleep(debug_delay_sec)
        central_conn = _open_central_readonly(central_db_path)
        central_state = _read_database_state(central_conn)
        if _states_match(central_state, expected_state):
            return {
                "status": "unchanged",
                "state": central_state,
            }

        os.makedirs(os.path.dirname(temp_db_path), exist_ok=True)
        temp_conn = _open_temp_replica(temp_db_path)
        central_conn.backup(temp_conn, pages=512, sleep=0.01)
        checkpoint_row = temp_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint_row and int(checkpoint_row[0] or 0) not in (0,):
            raise sqlite3.OperationalError(
                f"local replica WAL checkpoint failed: {tuple(checkpoint_row)}"
            )
        quick_row = temp_conn.execute("PRAGMA quick_check").fetchone()
        if not quick_row or str(quick_row[0] or "").lower() != "ok":
            raise sqlite3.DatabaseError(
                f"local replica quick_check failed: {quick_row[0] if quick_row else 'empty'}"
            )
        snapshot_state = _read_database_state(temp_conn)
        temp_conn.close()
        temp_conn = None
        with open(temp_db_path, "r+b") as fh:
            fh.flush()
            os.fsync(fh.fileno())
        return {
            "status": "snapshot_ready",
            "state": snapshot_state,
            "temp_db_path": temp_db_path,
        }
    except Exception:
        _remove_with_sidecars(temp_db_path)
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


def _send_error(pipe, exc: Exception) -> None:
    try:
        pipe.send(
            {
                "ok": False,
                "error_class": type(exc).__name__,
                "error": str(exc),
            }
        )
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
            pipe.send({"ok": True, **_sync_snapshot(message)})
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
        self.malformed_quarantine_dir = malformed_lock_quarantine_dir(
            self.central_db_path
        )
        self.timeout_sec = max(0.2, min(30.0, float(timeout_sec)))
        self._mutex = threading.Lock()
        self._process = None
        self._pipe = None

    def _ensure_started(self) -> None:
        if (
            self._process is not None
            and self._process.is_alive()
            and self._pipe is not None
        ):
            return
        self._terminate()
        context = multiprocessing.get_context("spawn")
        parent_pipe, child_pipe = context.Pipe(duplex=True)
        process = context.Process(
            target=_worker_main,
            args=(child_pipe,),
            name="RemCardLocalReplicaWorker",
            daemon=True,
        )
        process.start()
        child_pipe.close()
        self._process = process
        self._pipe = parent_pipe

    def _terminate(self) -> None:
        process = self._process
        pipe = self._pipe
        self._process = None
        self._pipe = None
        if pipe is not None:
            try:
                pipe.close()
            except Exception:
                pass
        if process is None:
            return
        if process.is_alive():
            process.terminate()
            process.join(timeout=0.5)
        if process.is_alive():
            try:
                process.kill()
            except Exception:
                pass
            process.join(timeout=0.5)
        try:
            process.close()
        except Exception:
            pass

    def sync(
        self,
        *,
        local_state: dict[str, Any] | None,
        temp_db_path: str,
        timeout_sec: float | None = None,
        debug_delay_sec: float = 0.0,
    ) -> dict[str, Any]:
        effective_timeout = max(
            0.2,
            min(30.0, float(timeout_sec or self.timeout_sec)),
        )
        deadline = time.monotonic() + effective_timeout
        with self._mutex:
            rotation_gate = (
                FileWriteLock(
                    self.rotation_lock_path,
                    stale_timeout_sec=60.0,
                    allow_expired_lease_cleanup=True,
                    allow_legacy_replica_cleanup=True,
                    allow_malformed_cleanup=True,
                    malformed_quarantine_dir=self.malformed_quarantine_dir,
                )
                if self.rotation_lock_path
                else None
            )
            snapshot_lease = FileWriteLock(
                self.snapshot_lease_path,
                stale_timeout_sec=60.0,
                lease_duration_sec=max(15.0, effective_timeout * 2.0 + 3.0),
                allow_expired_lease_cleanup=True,
                allow_legacy_replica_cleanup=True,
                allow_malformed_cleanup=True,
                malformed_quarantine_dir=self.malformed_quarantine_dir,
            )
            snapshot_lease_acquired = False
            try:
                if rotation_gate is not None and os.path.exists(
                    self.rotation_lock_path
                ):
                    rotation_gate.cleanup_abandoned(
                        source="local_replica_rotation_gate",
                    )
                    if os.path.exists(self.rotation_lock_path):
                        raise LocalReplicaRotationBusy()

                owner_id = (
                    f"{socket.gethostname()}:{os.getpid()}:"
                    "local_replica_snapshot"
                )
                if not snapshot_lease.acquire(
                    owner_id=owner_id,
                    source="local_replica_snapshot",
                ):
                    raise LocalReplicaRotationBusy()
                snapshot_lease_acquired = True

                # Close the race where rotation starts after the first check but
                # before this client publishes its reader lease.
                if rotation_gate is not None and os.path.exists(
                    self.rotation_lock_path
                ):
                    raise LocalReplicaRotationBusy()
                self._ensure_started()
                self._pipe.send(
                    {
                        "cmd": "sync",
                        "central_db_path": self.central_db_path,
                        "temp_db_path": os.path.abspath(temp_db_path),
                        "local_state": dict(local_state or {}),
                        "debug_delay_sec": max(0.0, float(debug_delay_sec)),
                    }
                )
                remaining = max(0.0, deadline - time.monotonic())
                if not self._pipe.poll(remaining):
                    raise LocalReplicaWorkerTimeout(effective_timeout)
                response = self._pipe.recv()
            except LocalReplicaWorkerTimeout:
                self._terminate()
                _remove_with_sidecars(temp_db_path)
                raise
            except (EOFError, OSError) as exc:
                self._terminate()
                _remove_with_sidecars(temp_db_path)
                raise LocalReplicaWorkerError(
                    f"Процесс локальной реплики завершился без результата: {exc}"
                ) from exc
            finally:
                if snapshot_lease_acquired:
                    snapshot_lease.release()

            if not response.get("ok"):
                _remove_with_sidecars(temp_db_path)
                raise LocalReplicaWorkerError(
                    str(response.get("error") or "Ошибка процесса локальной реплики"),
                    remote_error_class=str(response.get("error_class") or ""),
                )
            return dict(response)

    def close(self, *, timeout_sec: float = 0.5) -> None:
        acquired = self._mutex.acquire(timeout=max(0.0, float(timeout_sec)))
        if not acquired:
            self._terminate()
            return
        try:
            if (
                self._pipe is not None
                and self._process is not None
                and self._process.is_alive()
            ):
                try:
                    self._pipe.send({"cmd": "shutdown"})
                    self._process.join(timeout=0.5)
                except Exception:
                    pass
            self._terminate()
        finally:
            self._mutex.release()
