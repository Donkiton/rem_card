"""Build a replacement replica using bounded network changes and local disk I/O.

The published replica is never edited here. This module runs in the existing
killable worker, under its reader lease and workstation snapshot gate.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing

from rem_card.app.sqlite_uri import build_sqlite_file_uri


def incremental_enabled() -> bool:
    return os.environ.get("REMCARD_INCREMENTAL_REPLICA", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def try_incremental_snapshot(message: dict, *, check_cancel, progress, check_boundary=None) -> dict:
    """Return a worker result, or full_required without touching the source.

    A full_required result MUST force a backup even when change_log's maximum
    ID is unchanged: untracked tables may have changed independently.
    """
    from rem_card.app.replica_delta import collect_delta, apply_delta, ReplicaDeltaError
    from rem_card.app.role_admission import read_admission_state
    from rem_card.app.replica_provenance import read_baseline
    from rem_card.app.replica_source_stamp import capture_source_stamp
    from rem_card.app.local_replica_worker import (
        _open_central_readonly, _open_temp_replica, _read_database_state,
        _remove_with_sidecars, _local_destination, LOCAL_REPLICA_BACKUP_PAGES,
    )

    # Row-level checks may amortize network metadata probes. At phase boundaries
    # and before handing out a candidate, the writer/rotation fence is always
    # fresh. Other callers retain their existing cancellation callback.
    def check_fence():
        check_cancel()
        if check_boundary is not None:
            check_boundary()

    source_path = os.path.abspath(message["central_db_path"])
    local_path = os.path.abspath(message["local_db_path"])
    temp_path = os.path.abspath(message["temp_db_path"])
    if (local_path in {source_path, temp_path} or not os.path.isfile(local_path)
            or os.path.realpath(local_path) in {os.path.realpath(source_path), os.path.realpath(temp_path)}):
        return {"status": "full_required", "reason": "local_baseline_unavailable"}
    check_cancel()
    progress("delta_collect")
    # The package's source transaction ends before any local cloning/applying,
    # so local disk work cannot prolong the central clinical writer's wait.
    with closing(_open_central_readonly(source_path)) as source:
        try:
            before = read_admission_state(source, source_path)
        except ValueError as exc:
            if "identity is unavailable" in str(exc):
                return {"status": "full_required", "reason": "source_identity_unavailable"}
            raise
        if str(before["schema"]["journal_mode"]).lower() != "delete":
            return {"status": "full_required", "reason": "unsupported_network_profile"}
        baseline = read_baseline(local_path, before["identity"])
        if (message.get("baseline_source_identity") != before["identity"]
                and baseline is None):
            return {"status": "full_required", "reason": "baseline_source_identity_unknown"}
        stamp = capture_source_stamp(source, source_path)
        try:
            local = sqlite3.connect(build_sqlite_file_uri(local_path, mode="ro"),
                                    uri=True, isolation_level=None, timeout=0.25)
        except sqlite3.Error:
            return {"status": "full_required", "reason": "local_baseline_unreadable"}
        with closing(local):
            base_stamp = (message.get("baseline_source_stamp") if message.get("baseline_source_identity") == before["identity"]
                          else (baseline or {}).get("source_stamp"))
            base_state = (message.get("baseline_state") if message.get("baseline_source_identity") == before["identity"]
                          else (baseline or {}).get("state"))
            if stamp and stamp == base_stamp and base_state and _read_database_state(local) == base_state:
                package = {"status": "unchanged", "state": base_state, "metrics": {"source_unchanged": True}}
            else:
                package = collect_delta(source, local, check_cancel=check_cancel)
            check_fence()
            if package["status"] == "full_required":
                return package
            after = read_admission_state(source, source_path)
            if before != after:
                raise sqlite3.OperationalError("database is locked: source changed during delta")
            if package["status"] == "unchanged" and not message.get("require_snapshot"):
                final_stamp = stamp if capture_source_stamp(source, source_path) == stamp else None
                check_fence()
                return {"status": "unchanged", "state": package["state"],
                        "source_identity": before["identity"],
                        "source_stamp": final_stamp,
                        "sync_mode": "unchanged", "delta_metrics": package.get("metrics", {})}
            _remove_with_sidecars(temp_path)
            try:
                progress("delta_local_clone")
                with _local_destination():
                    os.makedirs(os.path.dirname(temp_path), exist_ok=True)
                    candidate = _open_temp_replica(temp_path)
                with closing(candidate):
                    with _local_destination():
                        local.backup(candidate, pages=LOCAL_REPLICA_BACKUP_PAGES,
                                     progress=lambda *_: check_cancel(), sleep=0.01)
                    check_fence()
                    if package["status"] == "delta":
                        progress("delta_apply")
                        # Constraints/unsupported local state require a fresh
                        # source backup, never publication of a partial delta.
                        try:
                            apply_delta(candidate, package)
                        except (sqlite3.Error, ValueError, ReplicaDeltaError):
                            return {"status": "full_required", "reason": "delta_apply_rejected"}
                    progress("quick_check")
                    with _local_destination():
                        checkpoint = candidate.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                        if checkpoint and int(checkpoint[0]):
                            raise sqlite3.OperationalError("local delta checkpoint failed")
                        rows = candidate.execute("PRAGMA quick_check").fetchall()
                        if len(rows) != 1 or tuple(rows[0]) != ("ok",):
                            raise sqlite3.DatabaseError("local delta quick_check failed")
                        state = _read_database_state(candidate)
                        if any(state.get(k) != package["state"].get(k)
                               for k in ("db_cycle", "change_cursor", "schema_revision")):
                            raise sqlite3.DatabaseError("local delta watermark mismatch")
                check_fence()
                if read_admission_state(source, source_path) != before:
                    raise sqlite3.OperationalError("database is locked: source changed before delta publication")
                progress("fsync")
                with _local_destination(), open(temp_path, "r+b") as handle:
                    handle.flush()
                    os.fsync(handle.fileno())
                final_stamp = stamp if capture_source_stamp(source, source_path) == stamp else None
                check_fence()
                return {"status": "snapshot_ready", "state": state, "temp_db_path": temp_path,
                        "source_identity": before["identity"],
                        "source_stamp": final_stamp,
                        "sync_mode": "delta" if package["status"] == "delta" else "cache",
                        "delta_metrics": package.get("metrics", {})}
            except BaseException:
                _remove_with_sidecars(temp_path)
                raise
