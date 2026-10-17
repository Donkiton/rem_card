"""One consistent network read for startup validation and the first local replica.

Only a live, process-local admission owns these files. A snapshot is never a
permission to write, nor evidence that a replica has caught up with newer writes.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from contextlib import closing
from typing import Callable

from rem_card.app.sqlite_uri import build_sqlite_file_uri

_owned_workspaces: set[str] = set()


def create_workspace() -> str:
    from rem_card.app.runtime_paths import get_local_cache_dir
    root = os.path.abspath(os.path.join(get_local_cache_dir(), "startup_snapshots"))
    os.makedirs(root, exist_ok=True)
    workspace = tempfile.mkdtemp(prefix=f"{os.getpid()}_", dir=root)
    _owned_workspaces.add(workspace)
    return workspace


def discard(snapshot: dict | None) -> None:
    if snapshot and snapshot.get("workspace") in _owned_workspaces:
        workspace = snapshot["workspace"]
        parent = os.path.realpath(os.path.dirname(workspace))
        resolved = os.path.realpath(workspace)
        try:
            contained = resolved != parent and os.path.commonpath((resolved, parent)) == parent
        except ValueError:
            contained = False
        if not contained:
            return
        shutil.rmtree(workspace, ignore_errors=True)
        if not os.path.exists(workspace):
            _owned_workspaces.discard(workspace)


def _fingerprint(path: str) -> list[int]:
    stat = os.stat(path)
    return [int(stat.st_dev), int(stat.st_ino), stat.st_size, stat.st_mtime_ns]


def _digest(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _schema_digest(conn) -> str:
    rows = [tuple(row) for row in conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]
    return hashlib.sha256(json.dumps(rows).encode("utf-8")).hexdigest()


def _source_state(path: str) -> tuple[dict, dict]:
    from rem_card.app.local_replica_worker import _open_central_readonly, _read_database_state
    from rem_card.app.role_admission import read_admission_state
    with closing(_open_central_readonly(path)) as conn:
        conn.execute("BEGIN")
        state = _read_database_state(conn)
        state["schema_digest"] = _schema_digest(conn)
        return read_admission_state(conn, path), state


def _direct_without_seed(path: str, original: dict, progress) -> tuple[bool, str, bool]:
    """Keep baseline availability when another copier/writer preempts backup."""
    from rem_card.app.startup_db_guard import _check_quick_direct
    from rem_card.app.role_admission import file_identity
    from rem_card.app.startup_diagnostics import startup_event
    rotation = os.path.join(os.path.dirname(path), "db_rotation.lock")
    writer = os.path.join(os.path.dirname(path), "db.lock")
    deadline = time.monotonic() + 2.0
    while os.path.exists(writer) and time.monotonic() < deadline:
        time.sleep(0.1)
    current, _ = _source_state(path)
    if current != original or os.path.exists(rotation):
        return False, "database is locked: source changed before direct check", False
    if progress:
        progress({"stage": "direct_check"})
    startup_event("startup_snapshot_fallback", reason="snapshot_busy", check_mode="direct_without_seed")
    outcome = _check_quick_direct(path, capture_admission=True)
    if file_identity(path) != original["identity"] or os.path.exists(rotation):
        return False, "database is locked: source changed during direct check", False
    if not outcome[0]:
        return outcome
    if outcome[1] == "ok":
        return False, "database is locked: direct check has no stable admission state", False
    state = json.loads(outcome[1])
    after, _ = _source_state(path)
    if state != original or state != after:
        return False, "database is locked: source changed during direct check", False
    return True, json.dumps({"admission": state, "snapshot": None}), False


def check_direct_admission(path: str, progress=None) -> tuple[bool, str, bool]:
    """A local cache failure must not make the central database unusable."""
    try:
        original, _ = _source_state(path)
        return _direct_without_seed(path, original, progress)
    except Exception as exc:
        return False, f"direct admission unavailable: {exc}", False


def check_snapshot(path: str, workspace: str, *, lease_duration_sec: float = 183.0,
                   progress: Callable[[dict], None] | None = None) -> tuple[bool, str, bool]:
    """Run in a killable child; never migrate, recover or write clinical data."""
    from rem_card.app.local_replica_worker import (
        _sync_snapshot_with_network_lease, LocalReplicaWriterBusy,
        LocalReplicaRotationBusy, LocalReplicaSnapshotBusy, LocalReplicaDestinationError,
        _local_destination,
    )
    from rem_card.app.startup_db_guard import _check_quick_direct, is_confirmed_db_corruption_reason
    from rem_card.app.startup_diagnostics import startup_span

    def stage(value):
        if progress:
            progress({"stage": value})

    def pages(remaining, total):
        if progress:
            progress({"stage": "backup", "remaining": remaining, "total": total})

    snapshot_path = os.path.join(workspace, "validated.db")
    try:
        stage("source_probe")
        before, original_state = _source_state(path)
        root = os.path.dirname(os.path.dirname(os.path.abspath(path)))
        with startup_span("startup_snapshot_copy", target="central_medical"):
            result = _sync_snapshot_with_network_lease(
                {"central_db_path": path, "temp_db_path": snapshot_path,
                 "rotation_lock_path": os.path.join(os.path.dirname(path), "db_rotation.lock"),
                 "snapshot_lease_path": os.path.join(root, "locks", "replica_snapshots",
                                                      f"startup_{os.getpid()}.lock"),
                 "lease_duration_sec": lease_duration_sec},
                progress_callback=stage, backup_progress_callback=pages,
            )
        # Backup API already quick_checked, checkpointed, closed and fsynced the
        # local snapshot. Verify its schema/cycle against the live source too.
        stage("source_confirmation")
        after, current = _source_state(path)
        if (before != after or original_state["db_cycle"] != current["db_cycle"]
                or original_state["schema_digest"] != current["schema_digest"]
                or result["state"].get("db_cycle") != current.get("db_cycle")):
            return False, "database is locked: database changed during startup snapshot", False
        with closing(sqlite3.connect(build_sqlite_file_uri(snapshot_path, mode="ro"), uri=True)) as conn:
            from rem_card.app.role_admission import read_admission_state
            copied = read_admission_state(conn, path)
            schema_digest = _schema_digest(conn)
        # SQLite backup deliberately changes destination schema_version and the
        # local destination uses WAL. Compare actual schema definitions instead.
        for key in ("schema_version", "journal_mode"):
            copied["schema"][key] = after["schema"][key]
        if copied != after or schema_digest != current["schema_digest"]:
            return False, "database is locked: snapshot schema differs from source", False
        with _local_destination():
            manifest = {"workspace": workspace, "path": snapshot_path, "source": after,
                        "state": result["state"], "fingerprint": _fingerprint(snapshot_path),
                        "sha256": _digest(snapshot_path)}
        return True, json.dumps({"admission": after, "snapshot": manifest}), False
    except (LocalReplicaWriterBusy, LocalReplicaSnapshotBusy, LocalReplicaDestinationError):
        # One bounded attempt at the original direct full check. It can hold a
        # read lock like the old startup path, but avoids starving role entry
        # behind the optional background copier. No stale seed is published.
        try:
            return _direct_without_seed(path, before, progress)
        except Exception as exc:
            return False, f"direct admission unavailable: {exc}", False
    except LocalReplicaRotationBusy as exc:
        return False, f"database is locked: {exc}", False
    except ValueError as exc:
        # Only missing filesystem identity may use the legacy direct check.
        # Active recovery or identity races must never fall through this path.
        if "identity is unavailable" in str(exc):
            return _check_quick_direct(path)
        return False, f"database is locked: {exc}", False
    except Exception as exc:
        if is_confirmed_db_corruption_reason(str(exc)):
            # A bad destination/disk cannot establish corruption of the source.
            # Only an independent direct source check may authorize recovery.
            stage("source_corruption_confirmation")
            outcome = _check_quick_direct(path)
            if not outcome[0]:
                return outcome
        return False, f"startup snapshot unavailable: {exc}", False


def _cleanup_entry(sender, paths, pid):
    from rem_card.app.sqlite_shared import FileWriteLock
    import socket
    result = []
    try:
        for path, source in paths:
            lock = FileWriteLock(path)
            lock.cleanup_stopped_local_owner(owner_pid=pid, owner_source=source)
            current = lock._read_lock_snapshot()
            if not current.exists:
                result.append(True)
            elif current.readable:
                payload = current.payload or {}
                result.append(payload.get("pid") != pid or payload.get("source") != source
                              or str(payload.get("host", "")).casefold() != socket.gethostname().casefold())
            else:
                result.append(False)
        sender.send(all(result))
    finally:
        sender.close()


def cleanup_stopped_worker(path: str, pid: int) -> bool:
    """Bound all SMB cleanup in another killable process after confirmed death."""
    import multiprocessing
    from rem_card.app.local_replica_worker import replica_snapshot_gate_path
    root = os.path.dirname(os.path.dirname(os.path.abspath(path)))
    paths = ((replica_snapshot_gate_path(path), "local_replica_snapshot_gate"),
             (os.path.join(root, "locks", "replica_snapshots", f"startup_{pid}.lock"),
              "local_replica_snapshot"))
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_cleanup_entry,
                              args=(sender, paths, pid), name="RemCardStartupLockCleanup", daemon=True)
    started = False
    confirmed = False
    try:
        process.start()
        started = True
        sender.close()
        if receiver.poll(3.0):
            confirmed = receiver.recv() is True
        process.join(0.1)
        return confirmed
    except (OSError, EOFError):
        return False
    finally:
        sender.close()
        receiver.close()
        if started:
            if process.is_alive():
                process.terminate()
                process.join(0.5)
            if process.is_alive():
                process.kill()
                process.join(0.5)
            if not process.is_alive():
                process.close()


def copy_for_replica(receipt: dict, target_path: str) -> str | None:
    """Prepare a local-only candidate; caller publishes through replica rollback.

    The replica still needs its normal central cursor/cycle confirmation before
    it is marked fresh. Copying these bytes must not advance last_sync_ok_ts.
    """
    from rem_card.app.role_admission import admission_still_valid
    snapshot = receipt.get("snapshot")
    if not snapshot or not admission_still_valid(receipt):
        return None
    source = snapshot["path"]
    if _fingerprint(source) != snapshot["fingerprint"]:
        return None
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    descriptor, candidate = tempfile.mkstemp(prefix=os.path.basename(target_path) + ".sync_tmp.", suffix=".db",
                                            dir=os.path.dirname(target_path))
    os.close(descriptor)
    try:
        shutil.copyfile(source, candidate)
        if (_fingerprint(source) != snapshot["fingerprint"]
                or _digest(candidate) != snapshot["sha256"]
                or not admission_still_valid(receipt)):
            return None
        with closing(sqlite3.connect(build_sqlite_file_uri(candidate, mode="ro"), uri=True)) as conn:
            if conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
                return None
        with open(candidate, "r+b") as handle:
            os.fsync(handle.fileno())
        result, candidate = candidate, ""
        return result
    finally:
        if candidate:
            try:
                os.remove(candidate)
            except FileNotFoundError:
                pass
