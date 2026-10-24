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
from contextlib import closing, contextmanager
from typing import Any

from rem_card.app.db_lifecycle import DB_CYCLE_META_KEY
from rem_card.app.db_wait_diagnostics import observe, observed_call, mark_stage, current_operation, bind_connection
from rem_card.app.sqlite_shared import FileWriteLock, configure_connection, describe_sqlite_lock_holder
from rem_card.app.sqlite_uri import build_sqlite_file_uri
from rem_card.app.replica_copy_diagnostics import current_copy, observed_copy


DEFAULT_REPLICA_SYNC_TIMEOUT_SEC = 30.0
MAX_REPLICA_SYNC_TIMEOUT_SEC = 90.0
LOCAL_REPLICA_BACKUP_PAGES = 256
WRITER_LOCK_PROBE_INTERVAL_SEC = 0.1
BACKUP_PROGRESS_IPC_INTERVAL_SEC = 0.1
WORKER_POLL_SLICE_SEC = 0.05
WORKER_STOP_WAIT_SEC = 0.5
STOPPED_WORKER_CLEANUP_TIMEOUT_SEC = 0.5


class LocalReplicaWorkerError(RuntimeError):
    def __init__(self, message: str, *, remote_error_class: str = "", stage: str = "",
                 sqlite_errorcode: int | None = None, sqlite_errorname: str = ""):
        self.remote_error_class = str(remote_error_class or "")
        self.stage = str(stage or getattr(self, "stage", ""))
        self.sqlite_errorcode = sqlite_errorcode
        self.sqlite_errorname = str(sqlite_errorname or "")
        super().__init__(str(message or "Ошибка процесса локальной реплики"))


class LocalReplicaWorkerCancelled(LocalReplicaWorkerError):
    """The worker operation was intentionally cancelled by client shutdown."""


class LocalReplicaWorkerTimeout(LocalReplicaWorkerError):
    def __init__(self, timeout_sec: float, *, stage: str = ""):
        self.timeout_sec = float(timeout_sec)
        self.stage = str(stage or "")
        stage_suffix = f" Этап: {self.stage}." if self.stage else ""
        super().__init__(
            f"Обновление локальной реплики превысило безопасный тайм-аут "
            f"{self.timeout_sec:.1f} с.{stage_suffix}"
        )


class LocalReplicaDestinationError(LocalReplicaWorkerError):
    """Failure known to concern only the local snapshot destination."""


def _is_proven_backup_destination_failure(exc: BaseException) -> bool:
    """Identify the one Backup API failure attributable to its destination.

    SQLite attaches Backup API errors to the destination connection.  That is
    not enough to turn arbitrary I/O or locking failures into local-disk
    failures: those can still describe the source path or a concurrent writer.
    ``SQLITE_FULL`` (and CPython's equivalent canonical message) is the
    narrow, actionable exception for a full destination volume.
    """
    if not isinstance(exc, sqlite3.Error):
        return False
    error_code = getattr(exc, "sqlite_errorcode", None)
    sqlite_full = getattr(sqlite3, "SQLITE_FULL", 13)
    if error_code is not None:
        try:
            return int(error_code) & 0xFF == sqlite_full
        except (TypeError, ValueError):
            return False
    return "database or disk is full" in str(exc).casefold()


@contextmanager
def _local_destination():
    try:
        yield
    except (OSError, sqlite3.Error) as exc:
        raise LocalReplicaDestinationError(str(exc)) from exc


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


class LocalReplicaSourceBusy(LocalReplicaWriterBusy):
    """SQLite refused a read while a concurrent source transaction was active."""


class _ReplicaProgress:
    def __init__(self, callback):
        self.callback = callback
        self.stage = "worker_start"

    def __call__(self, stage: str) -> None:
        self.stage = str(stage)
        if self.callback is not None:
            self.callback(stage)

    def preserve_failure(self, exc: Exception) -> None:
        if not getattr(exc, "stage", ""):
            exc.stage = self.stage


def _delta_fallback_details(result: dict) -> tuple[str, dict]:
    return str(result.get("reason") or "delta_unavailable"), dict(result.get("metrics") or {})


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


def _remove_with_sidecars(db_path: str) -> bool:
    removed = True
    for candidate in (
        db_path,
        f"{db_path}-wal",
        f"{db_path}-shm",
        f"{db_path}-journal",
    ):
        try:
            os.remove(candidate)
        except FileNotFoundError:
            pass
        except OSError:
            removed = False
    return removed


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


def _flush_snapshot_file(path):
    with open(path, "r+b") as handle:
        handle.flush()
        os.fsync(handle.fileno())


@observed_copy("direct_snapshot")
def _sync_snapshot(
    message: dict[str, Any],
    *,
    before_backup: Callable[[], None] | None = None,
    progress_callback: Callable[[str], None] | None = None,
    backup_progress_callback: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    diagnostics = current_copy()
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
        central_conn = diagnostics.call("source_open", _open_central_readonly, central_db_path)
        bind_connection(central_conn)
        mark_stage("replica_state_read")
        if progress_callback is not None:
            progress_callback("state_read")
        central_state = diagnostics.call("source_state", _read_database_state, central_conn)
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
        if writer_lock_path and diagnostics.call("writer_probe", os.path.exists, writer_lock_path):
            raise LocalReplicaWriterBusy()

        if progress_callback is not None:
            progress_callback("central_open")
        central_conn = diagnostics.call("source_open", _open_central_readonly, central_db_path)
        bind_connection(central_conn)
        mark_stage("replica_state_read")
        if progress_callback is not None:
            progress_callback("state_read")
        central_state = diagnostics.call("source_state", _read_database_state, central_conn)
        if _states_match(central_state, expected_state):
            return {
                "status": "unchanged",
                "state": central_state,
            }

        with _local_destination():
            os.makedirs(os.path.dirname(temp_db_path), exist_ok=True)
            temp_conn = diagnostics.call("local_open", _open_temp_replica, temp_db_path)

        last_writer_probe = 0.0
        last_backup_progress_emit = 0.0
        last_backup_progress: tuple[int, int] | None = None
        backup_reported_full = False

        def report_backup_progress(remaining: int, total: int) -> None:
            """Emit bounded, content-free SQLite backup progress diagnostics."""
            nonlocal last_backup_progress_emit, last_backup_progress
            remaining = max(0, int(remaining))
            total = max(0, int(total))
            current = (remaining, total)
            now = time.monotonic()
            is_first = last_backup_progress is None
            is_complete = remaining == 0
            if (
                backup_progress_callback is not None
                and (
                    is_first
                    or is_complete
                    or now - last_backup_progress_emit
                    >= BACKUP_PROGRESS_IPC_INTERVAL_SEC
                )
            ):
                try:
                    backup_progress_callback(remaining, total)
                except Exception:
                    # UI/IPC diagnostics must not affect a validated snapshot.
                    pass
                last_backup_progress_emit = now
            last_backup_progress = current

        def yield_to_writer(status: int, remaining: int, total: int) -> None:
            nonlocal backup_reported_full, last_writer_probe
            now = time.monotonic()
            observed_at = diagnostics.callback_begin(status, remaining, total)
            try:
                if int(status) & 0xFF == getattr(sqlite3, "SQLITE_FULL", 13):
                    backup_reported_full = True
                sqlite_busy = status in (
                    getattr(sqlite3, "SQLITE_BUSY", 5),
                    getattr(sqlite3, "SQLITE_LOCKED", 6),
                )
                if (
                    not sqlite_busy
                    and now - last_writer_probe < WRITER_LOCK_PROBE_INTERVAL_SEC
                ):
                    report_backup_progress(remaining, total)
                    return
                last_writer_probe = now
                if writer_lock_path and diagnostics.call("writer_probe", os.path.exists, writer_lock_path):
                    raise LocalReplicaWriterBusy()
                report_backup_progress(remaining, total)
            finally:
                diagnostics.callback_end(observed_at)

        mark_stage("replica_backup")
        if progress_callback is not None:
            progress_callback("backup")
        try:
            diagnostics.call("full_backup", central_conn.backup,
                temp_conn,
                pages=LOCAL_REPLICA_BACKUP_PAGES,
                progress=yield_to_writer,
                sleep=0.01,
            )
        except Exception as exc:
            if backup_reported_full or _is_proven_backup_destination_failure(exc):
                raise LocalReplicaDestinationError(str(exc)) from exc
            raise
        mark_stage("replica_checkpoint")
        if progress_callback is not None:
            progress_callback("checkpoint")
        with _local_destination():
            checkpoint_row = diagnostics.call("checkpoint", lambda:
                temp_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
            if checkpoint_row and int(checkpoint_row[0] or 0) not in (0,):
                raise sqlite3.OperationalError(
                    f"local replica WAL checkpoint failed: {tuple(checkpoint_row)}"
                )
        mark_stage("replica_quick_check")
        if progress_callback is not None:
            progress_callback("quick_check")
        with _local_destination():
            quick_row = diagnostics.call("quick_check", lambda:
                temp_conn.execute("PRAGMA quick_check").fetchone())
            if not quick_row or str(quick_row[0] or "").lower() != "ok":
                raise sqlite3.DatabaseError(
                    f"local replica quick_check failed: {quick_row[0] if quick_row else 'empty'}"
                )
            snapshot_state = diagnostics.call("local_state", _read_database_state, temp_conn)
            temp_conn.close()
        temp_conn = None
        mark_stage("replica_fsync")
        if progress_callback is not None:
            progress_callback("fsync")
        with _local_destination():
            diagnostics.call("fsync", _flush_snapshot_file, temp_db_path)
        return {
            "status": "snapshot_ready",
            "state": snapshot_state,
            "temp_db_path": temp_db_path,
            "backup_pages_remaining": (
                last_backup_progress[0] if last_backup_progress is not None else 0
            ),
            "backup_pages_total": (
                last_backup_progress[1] if last_backup_progress is not None else 0
            ),
        }
    except Exception:
        diagnostics.note_error()
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
    backup_progress_callback: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    with observe("replica_snapshot", resource=message.get("central_db_path"), stage="replica_lease_wait"):
        return _sync_snapshot_with_network_lease_observed(
            message,
            progress_callback=progress_callback,
            backup_progress_callback=backup_progress_callback,
        )


@observed_copy("network_lease")
def _sync_snapshot_with_network_lease_observed(
    message: dict[str, Any],
    *,
    progress_callback: Callable[[str], None] | None = None,
    backup_progress_callback: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Run every potentially blocking network operation inside the worker.

    Windows SMB filesystem calls are not reliably bounded by SQLite timeouts.
    Keeping rotation checks, lease I/O and the database snapshot in this
    process lets the parent enforce one hard deadline for the complete sync.
    """
    diagnostics = current_copy()
    progress_reporter = _ReplicaProgress(progress_callback)
    progress_callback = progress_reporter
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
        if writer_lock_path and diagnostics.call("writer_probe", os.path.exists, writer_lock_path):
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
        if not diagnostics.call("lease_acquire", snapshot_lease.acquire,
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
            if writer_lock_path and diagnostics.call("writer_probe", os.path.exists, writer_lock_path):
                raise LocalReplicaWriterBusy()
            if snapshot_gate_acquired:
                return
            if progress_callback is not None:
                progress_callback("snapshot_gate_acquire")
            if not diagnostics.call("gate_acquire", snapshot_gate.acquire,
                owner_id=owner_id,
                source="local_replica_snapshot_gate",
            ):
                raise LocalReplicaSnapshotBusy(describe_sqlite_lock_holder(snapshot_gate_path))
            snapshot_gate_acquired = True
            if writer_lock_path and diagnostics.call("writer_probe", os.path.exists, writer_lock_path):
                raise LocalReplicaWriterBusy()
            if rotation_gate is not None and os.path.exists(rotation_lock_path):
                raise LocalReplicaRotationBusy()

        if progress_callback is not None:
            progress_callback("snapshot")
        from rem_card.app.replica_refresh import incremental_enabled, try_incremental_snapshot
        fallback_reason = ""
        fallback_metrics = {}
        if message.get("local_db_path") and incremental_enabled():
            acquire_snapshot_gate()

            def check_delta_cancel() -> None:
                if writer_lock_path and diagnostics.call("writer_probe", os.path.exists, writer_lock_path):
                    raise LocalReplicaWriterBusy()
                if rotation_gate is not None and os.path.exists(rotation_lock_path):
                    raise LocalReplicaRotationBusy()

            result = try_incremental_snapshot(
                message, check_cancel=check_delta_cancel,
                progress=progress_callback or (lambda _: None),
            )
            if result["status"] != "full_required":
                return result
            fallback_reason, fallback_metrics = _delta_fallback_details(result)
            # An untracked table can change without advancing the notification
            # cursor. A rejected delta therefore cannot use the old fast skip.
            message = {**message, "local_state": {}}
        elif message.get("require_snapshot"):
            # Startup needs actual bytes even when the optional delta path is
            # disabled. The legacy cursor shortcut cannot materialize a file.
            message = {**message, "local_state": {}}
        from rem_card.app.role_admission import file_identity
        from rem_card.app.replica_source_stamp import capture_source_stamp
        with closing(diagnostics.call("source_open", _open_central_readonly, central_db_path)) as stamp_conn:
            source_stamp = diagnostics.call("source_stamp", capture_source_stamp, stamp_conn, central_db_path)
            diagnostics.source_stamp("before", source_stamp)
        try:
            source_identity = file_identity(central_db_path)
        except ValueError:
            source_identity = {}
        result = _sync_snapshot(
            message,
            before_backup=acquire_snapshot_gate,
            progress_callback=progress_callback,
            backup_progress_callback=backup_progress_callback,
        )
        if source_identity and file_identity(central_db_path) != source_identity:
            _remove_with_sidecars(str(message["temp_db_path"]))
            raise LocalReplicaRotationBusy()
        result["source_identity"] = source_identity
        with closing(diagnostics.call("source_open", _open_central_readonly, central_db_path)) as stamp_conn:
            final_stamp = diagnostics.call("source_stamp", capture_source_stamp, stamp_conn, central_db_path)
            diagnostics.source_stamp("after", final_stamp)
            result["source_stamp"] = source_stamp if final_stamp == source_stamp else None
        result["sync_mode"] = "full" if result["status"] == "snapshot_ready" else "unchanged"
        if fallback_reason:
            result["full_reason"] = fallback_reason
            result["delta_metrics"] = fallback_metrics
        return result
    except Exception as exc:
        diagnostics.note_error()
        # Cleanup emits its own progress; preserve the operation that failed.
        progress_reporter.preserve_failure(exc)
        raise
    finally:
        try:
            if snapshot_gate_acquired:
                try:
                    if progress_callback is not None:
                        progress_callback("snapshot_gate_release")
                except Exception:
                    pass  # IPC diagnostics must never prevent lock release.
                diagnostics.call("gate_release", snapshot_gate.release)
        finally:
            if snapshot_lease_acquired:
                try:
                    if progress_callback is not None:
                        progress_callback("lease_release")
                except Exception:
                    pass
                diagnostics.call("lease_release", snapshot_lease.release)


def _send_error(pipe, exc: Exception, *, stage: str = "") -> None:
    sqlite_error = exc
    seen = set()
    while not isinstance(sqlite_error, sqlite3.Error) and id(sqlite_error) not in seen:
        seen.add(id(sqlite_error))
        cause = sqlite_error.__cause__ or sqlite_error.__context__
        if cause is None:
            break
        sqlite_error = cause
    try:
        pipe.send(
            {
                "ok": False,
                "error_class": type(exc).__name__,
                "error": str(exc),
                "gate_diagnostics": getattr(exc, "gate_diagnostics", {}),
                "failure_stage": str(getattr(exc, "stage", "") or stage),
                "sqlite_errorcode": getattr(sqlite_error, "sqlite_errorcode", None),
                "sqlite_errorname": str(getattr(sqlite_error, "sqlite_errorname", "") or ""),
            }
        )
    except Exception:
        pass


def _worker_response_error(response: dict, last_stage: str) -> LocalReplicaWorkerError:
    remote_class = str(response.get("error_class") or "")
    if remote_class == LocalReplicaRotationBusy.__name__:
        return LocalReplicaRotationBusy()
    if remote_class == LocalReplicaSnapshotBusy.__name__:
        return LocalReplicaSnapshotBusy(response.get("gate_diagnostics"))
    if remote_class == LocalReplicaWriterBusy.__name__:
        return LocalReplicaWriterBusy()
    code = response.get("sqlite_errorcode")
    message = str(response.get("error") or "Ошибка процесса локальной реплики")
    stage = str(response.get("failure_stage") or last_stage)
    primary_code = (code & 0xFF) if isinstance(code, int) else None
    busy = primary_code in (5, 6) or (
        remote_class == "OperationalError"
        and message.casefold().startswith(("database is locked", "database table is locked"))
    )
    source_read_stage = stage in {"central_probe", "central_open", "state_read", "delta_collect", "backup", "snapshot"}
    if busy and remote_class == "OperationalError" and source_read_stage:
        error = LocalReplicaSourceBusy()
        error.stage = stage
        error.sqlite_errorcode = code
        error.sqlite_errorname = str(response.get("sqlite_errorname") or "")
        return error
    return LocalReplicaWorkerError(
        message, remote_error_class=remote_class,
        stage=stage,
        sqlite_errorcode=code if isinstance(code, int) else None,
        sqlite_errorname=str(response.get("sqlite_errorname") or ""),
    )


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
        last_stage = "worker_start"

        def send_progress(stage):
            nonlocal last_stage
            last_stage = str(stage or "")
            pipe.send({"progress": last_stage})

        try:
            pipe.send(
                {
                    "ok": True,
                    **_sync_snapshot_with_network_lease(
                        message,
                        progress_callback=send_progress,
                        backup_progress_callback=lambda remaining, total: pipe.send(
                            {
                                "progress": "backup",
                                "backup_remaining": int(remaining),
                                "backup_total": int(total),
                            }
                        ),
                    ),
                }
            )
        except Exception as exc:
            _send_error(pipe, exc, stage=last_stage)


class LocalReplicaWorkerClient:
    def __init__(
        self,
        *,
        central_db_path: str,
        local_db_path: str | None = None,
        rotation_lock_path: str | None = None,
        snapshot_lease_dir: str | None = None,
        snapshot_lease_id: str | None = None,
        timeout_sec: float = DEFAULT_REPLICA_SYNC_TIMEOUT_SEC,
    ):
        self.central_db_path = os.path.abspath(central_db_path)
        self.local_db_path = os.path.abspath(local_db_path) if local_db_path else ""
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
        self.timeout_sec = max(
            0.2,
            min(MAX_REPLICA_SYNC_TIMEOUT_SEC, float(timeout_sec)),
        )
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
                raise LocalReplicaWorkerCancelled(
                    "Процесс локальной реплики останавливается"
                )
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

    def _discard_stopped_temp_locked(self) -> bool:
        if not self._active_temp_path:
            return True
        if not _remove_with_sidecars(self._active_temp_path):
            # The child is dead, but Windows/AV may still deny unlink. Retain
            # the only reference and retry before allowing a replacement worker.
            return False
        self._active_temp_path = ""
        return True

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
            temp_removed = self._discard_stopped_temp_locked()
            if self._cleanup_required_pid is not None:
                cleanup_ok = self._bounded_cleanup_stopped_worker_locks(
                    self._cleanup_required_pid,
                    deadline=deadline,
                )
                if cleanup_ok:
                    self._cleanup_required_pid = None
                return cleanup_ok and temp_removed
            return self._reap_cleanup_locked(deadline=deadline) and temp_removed

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
        temp_removed = self._discard_stopped_temp_locked()
        if cleanup_ok:
            self._cleanup_required_pid = None
        return cleanup_ok and temp_removed

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
        progress_callback: Callable[[str], None] | None = None,
        backup_progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        effective_timeout = max(
            0.2,
            min(
                MAX_REPLICA_SYNC_TIMEOUT_SEC,
                float(timeout_sec or self.timeout_sec),
            ),
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
                    if self._closing.is_set():
                        raise LocalReplicaWorkerCancelled(
                            "Процесс локальной реплики останавливается"
                        )
                    if self._fenced:
                        raise LocalReplicaWorkerError(
                            "Процесс локальной реплики останавливается"
                        )
                    pipe = self._pipe
                    self._active_temp_path = temp_db_path
                pipe.send(
                    {
                        "cmd": "sync",
                        "central_db_path": self.central_db_path,
                        "local_db_path": self.local_db_path,
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
                        raise LocalReplicaWorkerCancelled(
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
                        if progress_callback is not None:
                            try:
                                progress_callback(progress)
                            except Exception:
                                pass
                        if "backup_remaining" in response or "backup_total" in response:
                            try:
                                remaining = max(
                                    0,
                                    int(response.get("backup_remaining") or 0),
                                )
                                total = max(0, int(response.get("backup_total") or 0))
                                if backup_progress_callback is not None:
                                    backup_progress_callback(remaining, total)
                            except Exception:
                                pass
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
                raise _worker_response_error(response, last_stage)
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
                        # Reserve part of the same hard budget for termination
                        # and bounded lease cleanup if Python shutdown stalls.
                        timeout=min(WORKER_STOP_WAIT_SEC, max(0.0, deadline - time.monotonic()) / 2)
                    )
                except Exception:
                    pass
            return self._terminate(deadline=deadline)
        finally:
            self._mutex.release()
