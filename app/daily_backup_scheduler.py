"""Bounded once-per-day backup of the central SQLite database.

The scheduler itself never opens the clinical database or touches the network
backup directories.  Those operations run in a spawned child process so a
blocked SMB/SQLite call can be terminated after a hard deadline.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import re
import socket
import sqlite3
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from rem_card.app.local_metrics import record_metric
from rem_card.app.logger import logger


DAILY_BACKUP_ENV = "REMCARD_DAILY_CENTRAL_BACKUP_ENABLED"
DAILY_BACKUP_SOURCE = "daily_central_backup_reservation"
DAILY_DATABASE_LOCK_SOURCE = "daily_central_backup_copy"
DAILY_SNAPSHOT_LEASE_SOURCE = "daily_central_backup_snapshot"
DAILY_BACKUP_KIND = "daily_central"
_DAILY_NAME_RE = re.compile(r"^daily_.+_(\d{4}-\d{2}-\d{2})\.db$")


class DailyBackupDeferred(RuntimeError):
    """Expected coordination refusal; keep the short retry cadence."""


class DailyBackupCopyBudgetExceeded(DailyBackupDeferred):
    """The bounded copy yielded its shared writer gate for a later attempt."""


def _process_state(process: Any) -> str:
    """A failed liveness probe is not evidence that an owned child stopped."""
    try:
        return "alive" if process.is_alive() else "stopped"
    except Exception:
        return "unknown"


def _stop_process_bounded(process: Any, *, timeout_sec: float = 2.0) -> tuple[str, tuple[str, ...]]:
    """Recheck death after termination errors, including Windows exit races."""
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    errors: list[str] = []
    state = _process_state(process)
    if state == "stopped":
        return state, ()
    for index, action in enumerate(("terminate", "kill")):
        try:
            method = getattr(process, action, None)
            if method is not None:
                method()
        except Exception as exc:
            errors.append(f"{action}:{type(exc).__name__}")
        try:
            remaining = max(0.0, deadline - time.monotonic())
            process.join(timeout=remaining / (2 - index))
        except Exception as exc:
            errors.append(f"join:{type(exc).__name__}")
        state = _process_state(process)
        if state == "stopped" or time.monotonic() >= deadline:
            break
    return state, tuple(errors)


def daily_backup_enabled_from_environment() -> bool:
    """Default on; only an explicit false-like value disables the backup."""
    value = os.environ.get(DAILY_BACKUP_ENV)
    if value is None or not value.strip():
        return True
    return value.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def normalize_backup_role(role: str | None) -> str:
    normalized = str(role or "").strip().lower()
    if normalized in {"doctor", "врач"} or "doctor" in normalized or "врач" in normalized:
        return "doctor"
    if normalized in {"nurse", "медсестра"} or "nurse" in normalized or "мед" in normalized:
        return "nurse"
    return normalized


def _atomic_json(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = os.path.join(
        os.path.dirname(path),
        f".{os.path.basename(path)}.{socket.gethostname()}_{os.getpid()}_{uuid.uuid4().hex}.tmp",
    )
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(temporary, path)
    finally:
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _daily_paths(config: dict[str, Any]) -> dict[str, str]:
    db_stem = os.path.splitext(os.path.basename(str(config["database_path"])))[0]
    safe_stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in db_stem) or "database"
    backup_date = str(config["backup_date"])
    backup_name = f"daily_{safe_stem}_{backup_date}.db"
    backup_path = os.path.join(str(config["backup_dir"]), backup_name)
    health_dir = str(config["health_dir"])
    local_staging_dir = str(
        config.get("local_staging_dir")
        or os.path.join(tempfile.gettempdir(), "remcard_daily_backup")
    )
    source_key = hashlib.sha256(
        os.path.normcase(os.path.abspath(str(config["database_path"]))).encode("utf-8")
    ).hexdigest()[:16]
    local_staging = os.path.join(
        local_staging_dir,
        f"daily_{source_key}_{backup_date}_{config['job_token']}.db",
    )
    publication_staging = os.path.join(
        str(config["backup_dir"]),
        f".{backup_name}.{config['job_token']}.publishing",
    )
    snapshot_lease = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(str(config["database_path"])))),
        "locks",
        "replica_snapshots",
        f"daily_{config['job_token']}.lock",
    )
    return {
        "backup": backup_path,
        "done": os.path.join(health_dir, f"daily_central_backup_{backup_date}.done.json"),
        "reservation": os.path.join(health_dir, f"daily_central_backup_{backup_date}.lock"),
        # Keep the legacy key for cleanup callers while making its local-disk
        # ownership explicit. Publication staging always lives beside backup.
        "staging": local_staging,
        "local_staging": local_staging,
        "publication_staging": publication_staging,
        "snapshot_lease": snapshot_lease,
    }


def _emit_progress(
    progress_callback: Callable[[dict[str, Any]], None] | None,
    stage: str,
    state: str,
    **fields: Any,
) -> None:
    """Report bounded, non-clinical stage telemetry without affecting backup."""
    payload = {"stage": stage, "state": state, **fields}
    try:
        record_metric("daily_backup_stage", 1, **payload)
    except Exception:
        pass
    if progress_callback is not None:
        try:
            progress_callback(payload)
        except Exception:
            pass


def _validated_daily_backup(path: str, *, backup_date: str, source_path: str) -> tuple[bool, str, dict[str, Any]]:
    from rem_card.app.sqlite_shared import backup_meta_path, validate_sqlite_file

    if not os.path.isfile(path):
        return False, "missing", {}
    meta_path = backup_meta_path(path)
    try:
        with open(meta_path, "r", encoding="utf-8") as handle:
            metadata = json.load(handle)
    except FileNotFoundError:
        return False, "metadata_missing", {}
    except Exception as exc:
        return False, f"metadata_unreadable:{type(exc).__name__}", {}
    if not isinstance(metadata, dict):
        return False, "metadata_not_object", {}
    if metadata.get("backup_kind") != DAILY_BACKUP_KIND:
        return False, "metadata_kind", metadata
    if str(metadata.get("backup_date") or "") != backup_date:
        return False, "metadata_date", metadata
    if os.path.normcase(os.path.abspath(str(metadata.get("source_db_path") or ""))) != os.path.normcase(
        os.path.abspath(source_path)
    ):
        return False, "metadata_source", metadata
    try:
        if int(metadata.get("size_bytes") or -1) != os.path.getsize(path):
            return False, "metadata_size", metadata
    except (OSError, TypeError, ValueError):
        return False, "metadata_size", metadata
    expected_sha = str(metadata.get("sha256") or "")
    if len(expected_sha) != 64 or _sha256(path) != expected_sha:
        return False, "metadata_sha256", metadata
    ok, reason = validate_sqlite_file(path)
    if not ok:
        return False, f"sqlite_validation:{reason}", metadata
    return True, "ok", metadata


def _write_daily_metadata(path: str, *, config: dict[str, Any], sha256: str) -> dict[str, Any]:
    from rem_card.app.sqlite_shared import NETWORK_SAFE_DB_PROFILE, backup_meta_path

    metadata = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "backup_path": path,
        "size_bytes": os.path.getsize(path),
        "sha256": sha256,
        "quick_check": "ok",
        "integrity_check": "ok",
        "db_profile": NETWORK_SAFE_DB_PROFILE,
        "backup_kind": DAILY_BACKUP_KIND,
        "backup_date": str(config["backup_date"]),
        "source_db_path": os.path.abspath(str(config["database_path"])),
        "role": str(config.get("role") or ""),
        "job_token": str(config["job_token"]),
    }
    meta_path = backup_meta_path(path)
    _atomic_json(meta_path, metadata)
    return metadata


def _bounded_sqlite_copy(
    config: dict[str, Any],
    staging_path: str,
    *,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Copy to local disk under the shared DB lock, then release it promptly."""
    from rem_card.app.sqlite_shared import FileWriteLock, configure_connection
    from rem_card.app.sqlite_uri import build_sqlite_file_uri

    database_path = str(config["database_path"])
    owner_id = f"daily-backup-job:{config['job_token']}"
    uri = build_sqlite_file_uri(database_path, mode="ro")
    source = None
    target = None
    database_lock = FileWriteLock(
        str(config["database_lock_path"]),
        stale_timeout_sec=10 * 60,
        logger=logger,
    )
    acquired = False
    read_started = time.monotonic()
    _emit_progress(progress_callback, "read_wait", "started")
    try:
        source = sqlite3.connect(
            uri,
            uri=True,
            check_same_thread=False,
            isolation_level=None,
            timeout=max(0.1, float(config.get("sqlite_timeout_sec", 1.0))),
        )
        configure_connection(source, readonly=True)
        lock_deadline = time.monotonic() + max(0.1, float(config.get("database_lock_wait_sec", 1.5)))
        while not database_lock.acquire(owner_id, DAILY_DATABASE_LOCK_SOURCE):
            if time.monotonic() >= lock_deadline:
                _emit_progress(
                    progress_callback,
                    "read_wait",
                    "deferred",
                    elapsed_ms=round((time.monotonic() - read_started) * 1000, 3),
                )
                raise DailyBackupDeferred("central_database_lock_busy")
            time.sleep(0.05)
        acquired = True
        _emit_progress(
            progress_callback,
            "read_wait",
            "completed",
            elapsed_ms=round((time.monotonic() - read_started) * 1000, 3),
        )
        os.makedirs(os.path.dirname(staging_path), exist_ok=True)
        try:
            os.remove(staging_path)
        except FileNotFoundError:
            pass
        target = sqlite3.connect(staging_path, check_same_thread=False, isolation_level=None, timeout=1.0)
        copy_deadline = time.monotonic() + max(0.25, float(config.get("copy_budget_sec", 8.0)))
        copy_started = time.monotonic()
        last_progress = copy_started
        _emit_progress(progress_callback, "copy", "started")

        def _progress(_status: int, remaining: int, total: int) -> None:
            nonlocal last_progress
            now = time.monotonic()
            if now >= copy_deadline:
                raise DailyBackupCopyBudgetExceeded("copy_budget_exhausted")
            if now - last_progress >= 0.5 or remaining == 0:
                last_progress = now
                _emit_progress(
                    progress_callback,
                    "copy",
                    "progress",
                    elapsed_ms=round((now - copy_started) * 1000, 3),
                    pages_remaining=max(0, int(remaining)),
                    pages_total=max(0, int(total)),
                )

        source.backup(
            target,
            pages=max(16, int(config.get("copy_pages", 256))),
            progress=_progress,
            sleep=0.01,
        )
        _emit_progress(
            progress_callback,
            "copy",
            "completed",
            elapsed_ms=round((time.monotonic() - copy_started) * 1000, 3),
            size_bytes=os.path.getsize(staging_path),
        )
    finally:
        if target is not None:
            try:
                target.close()
            except Exception:
                pass
        if acquired:
            database_lock.release()
        if source is not None:
            source.close()


def _upload_local_staging(
    local_path: str,
    publication_path: str,
    *,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Upload a completed local snapshot to a same-volume publication file."""
    os.makedirs(os.path.dirname(publication_path), exist_ok=True)
    try:
        os.remove(publication_path)
    except FileNotFoundError:
        pass
    started = time.monotonic()
    last_progress = started
    copied = 0
    total = os.path.getsize(local_path)
    _emit_progress(progress_callback, "upload", "started", size_bytes=total)
    try:
        with open(local_path, "rb") as source, open(publication_path, "xb") as target:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                target.write(chunk)
                copied += len(chunk)
                now = time.monotonic()
                if now - last_progress >= 0.5 or copied == total:
                    last_progress = now
                    _emit_progress(
                        progress_callback,
                        "upload",
                        "progress",
                        elapsed_ms=round((now - started) * 1000, 3),
                        bytes_copied=copied,
                        size_bytes=total,
                    )
            target.flush()
            try:
                os.fsync(target.fileno())
            except OSError:
                pass
    except Exception:
        try:
            os.remove(publication_path)
        except OSError:
            pass
        raise
    _emit_progress(
        progress_callback,
        "upload",
        "completed",
        elapsed_ms=round((time.monotonic() - started) * 1000, 3),
        size_bytes=copied,
    )


def _quarantine_failed_staging(staging_path: str, invalid_dir: str, reason: str) -> str:
    if not os.path.exists(staging_path):
        return ""
    os.makedirs(invalid_dir, exist_ok=True)
    reason_token = "".join(ch if ch.isalnum() else "_" for ch in reason)[:40] or "invalid"
    target = os.path.join(
        invalid_dir,
        f"daily_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{reason_token}.db",
    )
    try:
        os.replace(staging_path, target)
        return target
    except OSError:
        return ""


def _lightweight_daily_metadata(path: str, *, backup_date: str, source_path: str) -> tuple[bool, dict[str, Any]]:
    from rem_card.app.sqlite_shared import backup_meta_path

    try:
        with open(backup_meta_path(path), "r", encoding="utf-8") as handle:
            metadata = json.load(handle)
        if not isinstance(metadata, dict):
            return False, {}
        if metadata.get("backup_kind") != DAILY_BACKUP_KIND:
            return False, metadata
        if str(metadata.get("backup_date") or "") != backup_date:
            return False, metadata
        if os.path.normcase(os.path.abspath(str(metadata.get("source_db_path") or ""))) != os.path.normcase(
            os.path.abspath(source_path)
        ):
            return False, metadata
        if int(metadata.get("size_bytes") or -1) != os.path.getsize(path):
            return False, metadata
        if len(str(metadata.get("sha256") or "")) != 64:
            return False, metadata
        if metadata.get("quick_check") != "ok" or metadata.get("integrity_check") != "ok":
            return False, metadata
        return True, metadata
    except Exception:
        return False, {}


def _daily_digest_matches(path: str, *, backup_date: str, source_path: str) -> tuple[bool, dict[str, Any]]:
    """Verify today's immutable validated metadata and SHA without another integrity scan."""
    valid, metadata = _lightweight_daily_metadata(
        path,
        backup_date=backup_date,
        source_path=source_path,
    )
    if not valid:
        return False, metadata
    try:
        return _sha256(path) == str(metadata.get("sha256") or ""), metadata
    except OSError:
        return False, metadata


def _prune_validated_daily_backups(config: dict[str, Any], protected_path: str) -> dict[str, int]:
    """Bounded pruning using already validated metadata; never rescan every DB."""
    backup_dir = str(config["backup_dir"])
    retention_days = max(1, int(config.get("retention_days", 35)))
    max_count = max(2, int(config.get("max_count", 35)))
    cutoff = datetime.now() - timedelta(days=retention_days)
    validated: list[tuple[float, str]] = []
    try:
        names = os.listdir(backup_dir)
    except OSError:
        return {"deleted": 0, "validated": 0}

    for name in names:
        match = _DAILY_NAME_RE.match(name)
        if not match:
            continue
        path = os.path.join(backup_dir, name)
        ok, metadata = _lightweight_daily_metadata(
            path,
            backup_date=match.group(1),
            source_path=str(config["database_path"]),
        )
        if not ok or metadata.get("backup_kind") != DAILY_BACKUP_KIND:
            continue
        try:
            validated.append((os.path.getmtime(path), path))
        except OSError:
            continue

    validated.sort(reverse=True)
    if not validated:
        return {"deleted": 0, "validated": 0}
    # The just-created protected copy has already passed full SHA, quick_check,
    # and integrity_check.  Older copies are trusted only for pruning eligibility
    # when their immutable validation metadata and current size still agree.
    keep = {os.path.abspath(protected_path), os.path.abspath(validated[0][1])}
    deleted = 0
    for index, (mtime, path) in enumerate(validated):
        if os.path.abspath(path) in keep:
            continue
        too_many = index >= max_count
        too_old = datetime.fromtimestamp(mtime) < cutoff
        if not (too_many or too_old):
            continue
        from rem_card.app.sqlite_shared import backup_meta_path

        try:
            os.remove(path)
            try:
                os.remove(backup_meta_path(path))
            except FileNotFoundError:
                pass
            deleted += 1
        except OSError:
            continue
        if deleted >= 2:
            break
    return {"deleted": deleted, "validated": len(validated)}


def _lock_heartbeat(lock: Any, stop_event: threading.Event, interval_sec: float, job_token: str) -> None:
    while not stop_event.wait(max(0.25, interval_sec)):
        if not lock.refresh(metadata={"heartbeat_job_token": job_token}):
            return


def _perform_daily_backup(
    config: dict[str, Any],
    *,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    from rem_card.app.sqlite_shared import FileWriteLock, validate_sqlite_file

    paths = _daily_paths(config)
    backup_path = paths["backup"]
    os.makedirs(str(config["backup_dir"]), exist_ok=True)
    os.makedirs(str(config["health_dir"]), exist_ok=True)

    valid, _metadata = _daily_digest_matches(
        backup_path,
        backup_date=str(config["backup_date"]),
        source_path=str(config["database_path"]),
    )
    if valid:
        return {"status": "success", "reason": "already_done", "backup_path": backup_path}

    owner_id = f"daily-backup-job:{config['job_token']}"
    reservation = FileWriteLock(paths["reservation"], stale_timeout_sec=24 * 60 * 60, logger=logger)
    if not reservation.acquire(owner_id, DAILY_BACKUP_SOURCE):
        return {"status": "deferred", "reason": "reservation_busy", "backup_path": backup_path}

    heartbeat_stop = threading.Event()
    heartbeat = threading.Thread(
        target=_lock_heartbeat,
        args=(reservation, heartbeat_stop, float(config.get("heartbeat_sec", 2.0)), str(config["job_token"])),
        name="RemCardDailyBackupHeartbeat",
        daemon=True,
    )
    heartbeat.start()
    try:
        # Recheck after acquiring the cross-client reservation.
        valid, _metadata = _daily_digest_matches(
            backup_path,
            backup_date=str(config["backup_date"]),
            source_path=str(config["database_path"]),
        )
        if valid:
            return {"status": "success", "reason": "already_done", "backup_path": backup_path}

        database_path = str(config["database_path"])
        if not os.path.isfile(database_path):
            return {"status": "failure", "reason": "source_missing", "backup_path": backup_path}
        local_staging_path = paths["local_staging"]
        publication_staging_path = paths["publication_staging"]
        snapshot_lease = FileWriteLock(
            paths["snapshot_lease"],
            stale_timeout_sec=60.0,
            logger=logger,
        )
        snapshot_lease_acquired = False
        try:
            os.makedirs(os.path.dirname(paths["snapshot_lease"]), exist_ok=True)
            if not snapshot_lease.acquire(owner_id, DAILY_SNAPSHOT_LEASE_SOURCE):
                raise DailyBackupDeferred("snapshot_lease_busy")
            snapshot_lease_acquired = True
            _bounded_sqlite_copy(
                config,
                local_staging_path,
                progress_callback=progress_callback,
            )
        finally:
            if snapshot_lease_acquired:
                snapshot_lease.release()

        verify_started = time.monotonic()
        _emit_progress(progress_callback, "verify_local", "started")
        valid, reason = validate_sqlite_file(local_staging_path)
        if not valid:
            invalid_path = _quarantine_failed_staging(
                local_staging_path,
                str(config["invalid_dir"]),
                reason,
            )
            return {
                "status": "failure",
                "reason": "staging_validation_failed",
                "detail": reason,
                "invalid_path": invalid_path,
                "backup_path": backup_path,
            }
        sha256 = _sha256(local_staging_path)
        _emit_progress(
            progress_callback,
            "verify_local",
            "completed",
            elapsed_ms=round((time.monotonic() - verify_started) * 1000, 3),
            size_bytes=os.path.getsize(local_staging_path),
        )

        _upload_local_staging(
            local_staging_path,
            publication_staging_path,
            progress_callback=progress_callback,
        )
        verify_upload_started = time.monotonic()
        _emit_progress(progress_callback, "verify_upload", "started")
        uploaded_sha256 = _sha256(publication_staging_path)
        if uploaded_sha256 != sha256:
            raise OSError("daily backup upload digest mismatch")
        _emit_progress(
            progress_callback,
            "verify_upload",
            "completed",
            elapsed_ms=round((time.monotonic() - verify_upload_started) * 1000, 3),
            size_bytes=os.path.getsize(publication_staging_path),
        )
        _emit_progress(progress_callback, "publish", "started")
        os.replace(publication_staging_path, backup_path)
        metadata = _write_daily_metadata(backup_path, config=config, sha256=sha256)
        valid, metadata = _lightweight_daily_metadata(
            backup_path,
            backup_date=str(config["backup_date"]),
            source_path=database_path,
        )
        if not valid:
            return {
                "status": "failure",
                "reason": "published_validation_failed",
                "detail": "published metadata or size mismatch",
                "backup_path": backup_path,
            }
        _emit_progress(
            progress_callback,
            "publish",
            "completed",
            size_bytes=int(metadata.get("size_bytes") or 0),
        )
        _atomic_json(
            paths["done"],
            {
                "status": "success",
                "backup_date": str(config["backup_date"]),
                "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "backup_path": backup_path,
                "sha256": metadata.get("sha256"),
                "size_bytes": metadata.get("size_bytes"),
                "host": socket.gethostname(),
                "pid": os.getpid(),
            },
        )
        prune = _prune_validated_daily_backups(config, backup_path)
        return {
            "status": "success",
            "reason": "created",
            "backup_path": backup_path,
            "pruned": prune.get("deleted", 0),
        }
    finally:
        for owned_staging in (
            paths.get("local_staging", ""),
            paths.get("publication_staging", ""),
        ):
            if not owned_staging:
                continue
            try:
                os.remove(owned_staging)
            except OSError:
                pass
        heartbeat_stop.set()
        heartbeat.join(timeout=1.0)
        reservation.release()


def _daily_backup_worker(config: dict[str, Any], result_connection: Any) -> None:
    def report_progress(payload: dict[str, Any]) -> None:
        if config.get("emit_progress"):
            result_connection.send({"message_type": "progress", **payload})

    try:
        result = _perform_daily_backup(config, progress_callback=report_progress)
    except DailyBackupDeferred as exc:
        result = {"status": "deferred", "reason": str(exc)}
    except BaseException as exc:
        result = {
            "status": "failure",
            "reason": "worker_exception",
            "error_class": type(exc).__name__,
            "error": str(exc)[:500],
        }
    try:
        result_connection.send(result)
    except Exception:
        pass
    finally:
        try:
            result_connection.close()
        except Exception:
            pass


def _read_lock_payload(path: str) -> tuple[bytes, dict[str, Any]] | None:
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            return None
        return raw, payload
    except Exception:
        return None


def cleanup_confirmed_dead_worker_reservation(
    lock_path: str,
    *,
    worker_pid: int,
    job_token: str,
    owner_source: str = DAILY_BACKUP_SOURCE,
    owner_user_id: str | None = None,
) -> bool:
    """Remove only this job's local dead-child lock; never clean by age."""
    from rem_card.app.sqlite_shared import is_local_pid_alive

    if worker_pid <= 0 or is_local_pid_alive(worker_pid) is not False:
        return False
    first = _read_lock_payload(lock_path)
    if first is None:
        return False
    raw, payload = first
    if str(payload.get("host") or "").casefold() != socket.gethostname().casefold():
        return False
    try:
        if int(payload.get("pid")) != int(worker_pid):
            return False
    except (TypeError, ValueError):
        return False
    if str(payload.get("source") or "") != owner_source:
        return False
    if str(payload.get("user_id") or "") != (owner_user_id or f"daily-backup-job:{job_token}"):
        return False
    if not str(payload.get("lock_token") or ""):
        return False
    second = _read_lock_payload(lock_path)
    if second is None or second[0] != raw:
        return False
    try:
        os.remove(lock_path)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _cleanup_dead_worker_artifacts(
    *,
    reservation_path: str,
    database_lock_path: str,
    staging_path: str,
    worker_pid: int,
    job_token: str,
    publication_staging_path: str = "",
    snapshot_lease_path: str = "",
) -> dict[str, bool]:
    from rem_card.app.sqlite_shared import is_local_pid_alive

    results = {
        "reservation": False,
        "database_lock": False,
        "staging": False,
    }
    # Staging belongs to the same child job, but deleting it is still unsafe
    # while that process may be publishing or validating it.
    if worker_pid <= 0 or is_local_pid_alive(worker_pid) is not False:
        return results
    results.update(
        reservation=cleanup_confirmed_dead_worker_reservation(
            reservation_path,
            worker_pid=worker_pid,
            job_token=job_token,
            owner_source=DAILY_BACKUP_SOURCE,
        ),
        database_lock=cleanup_confirmed_dead_worker_reservation(
            database_lock_path,
            worker_pid=worker_pid,
            job_token=job_token,
            owner_source=DAILY_DATABASE_LOCK_SOURCE,
        ),
    )
    if snapshot_lease_path:
        cleanup_confirmed_dead_worker_reservation(
            snapshot_lease_path,
            worker_pid=worker_pid,
            job_token=job_token,
            owner_source=DAILY_SNAPSHOT_LEASE_SOURCE,
        )
    for owned_staging in (staging_path, publication_staging_path):
        if not owned_staging:
            continue
        try:
            os.remove(owned_staging)
            results["staging"] = True
        except FileNotFoundError:
            pass
        except OSError:
            pass
    # A cleanup process can itself hit a transient SMB failure. Keep the job
    # owned until its files are gone or demonstrably belong to another owner.
    results["completed"] = _dead_worker_artifacts_released(
        reservation_path=reservation_path,
        database_lock_path=database_lock_path,
        snapshot_lease_path=snapshot_lease_path,
        staging_paths=(staging_path, publication_staging_path),
        worker_pid=worker_pid,
        job_token=job_token,
    )
    return results


def _dead_worker_artifacts_released(
    *, reservation_path: str, database_lock_path: str, snapshot_lease_path: str,
    staging_paths: tuple[str, ...], worker_pid: int, job_token: str,
) -> bool:
    for path, source in (
        (reservation_path, DAILY_BACKUP_SOURCE),
        (database_lock_path, DAILY_DATABASE_LOCK_SOURCE),
        (snapshot_lease_path, DAILY_SNAPSHOT_LEASE_SOURCE),
    ):
        if not path:
            continue
        if not _owned_lock_released(
            path, worker_pid=worker_pid, owner_source=source,
            owner_user_id=f"daily-backup-job:{job_token}",
        ):
            return False
    for path in staging_paths:
        if not path:
            continue
        try:
            os.stat(path)
        except FileNotFoundError:
            continue
        except OSError:
            return False
        return False
    return True


def _owned_lock_released(path: str, *, worker_pid: int, owner_source: str, owner_user_id: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return True
    except Exception:
        return False
    if not isinstance(payload, dict) or not all(
        payload.get(key) for key in ("host", "pid", "source", "user_id", "lock_token")
    ):
        return False
    return not (
        str(payload["host"]).casefold() == socket.gethostname().casefold()
        and str(payload["pid"]) == str(worker_pid)
        and str(payload["source"]) == owner_source
        and str(payload["user_id"]) == owner_user_id
    )


def _cleanup_dead_worker_entry(arguments: dict[str, Any], result_connection: Any) -> None:
    try:
        result_connection.send(_cleanup_dead_worker_artifacts(**arguments))
    except Exception:
        pass
    finally:
        try:
            result_connection.close()
        except Exception:
            pass


class DailyBackupScheduler:
    """Small coordinator around one bounded spawned worker at a time."""

    def __init__(
        self,
        *,
        database_path: str,
        backup_dir: str,
        health_dir: str,
        invalid_dir: str,
        database_lock_path: str,
        role: str,
        idle_probe: Callable[[], Any] | None = None,
        target_hour: int = 3,
        window_hours: float = 4.0,
        startup_delay_sec: float = 20.0,
        check_interval_sec: float = 60.0,
        deferred_retry_sec: float = 300.0,
        failure_retry_sec: float = 1800.0,
        worker_timeout_sec: float = 15.0,
        retention_days: int = 35,
        max_count: int = 35,
    ) -> None:
        self.database_path = os.path.abspath(str(database_path))
        self.backup_dir = os.path.abspath(str(backup_dir))
        self.health_dir = os.path.abspath(str(health_dir))
        self.invalid_dir = os.path.abspath(str(invalid_dir))
        self.database_lock_path = os.path.abspath(str(database_lock_path))
        self.role = normalize_backup_role(role)
        self.idle_probe = idle_probe
        self.target_hour = min(23, max(0, int(target_hour)))
        self.window_hours = max(0.25, float(window_hours))
        self.startup_delay_sec = max(0.0, float(startup_delay_sec))
        self.check_interval_sec = max(0.05, float(check_interval_sec))
        self.deferred_retry_sec = max(0.05, float(deferred_retry_sec))
        self.failure_retry_sec = max(0.05, float(failure_retry_sec))
        self.worker_timeout_sec = max(0.25, float(worker_timeout_sec))
        self.retention_days = max(1, int(retention_days))
        self.max_count = max(2, int(max_count))
        self._stop_event = threading.Event()
        self._wakeup = threading.Event()
        self._thread: threading.Thread | None = None
        self._process_lock = threading.Lock()
        self._process_stop_lock = threading.RLock()
        self._process: Any = None
        self._process_job: dict[str, Any] | None = None
        self._cleanup_process: Any = None
        self._cleanup_receiver: Any = None
        self._cleanup_result: dict[str, bool] = {}
        self._reap_retry_sec = min(5.0, self.check_interval_sec, self.deferred_retry_sec, self.failure_retry_sec)
        self._state_lock = threading.Lock()
        self._probe: dict[str, Any] = {
            "enabled": True,
            "running": False,
            "status": "not_started",
            "role": self.role,
        }
        self._completed_date = ""

    def start(self) -> bool:
        if self.role not in {"doctor", "nurse"}:
            self._update_probe(status="disabled_role", running=False)
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        # A dead scheduler thread may still own a live/unknown child. The new
        # coordinator must reap that owner before it is allowed to create work.
        self._stop_event.clear()
        self._wakeup.clear()
        self._thread = threading.Thread(target=self._run, name="RemCardDailyBackupScheduler", daemon=True)
        self._thread.start()
        self._update_probe(status="scheduled", running=True)
        return True

    def stop(self, timeout_sec: float = 2.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout_sec))
        self._stop_event.set()
        self._wakeup.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                self._signal_current_process_stop(timeout_sec=max(0.0, deadline - time.monotonic()))
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
        thread_stopped = thread is None or not thread.is_alive()
        process_stopped = self._terminate_current_process(
            timeout_sec=max(0.0, deadline - time.monotonic()),
        ) if thread_stopped else False
        stopped = thread_stopped and process_stopped
        if stopped:
            self._thread = None
        self._update_probe(status="stopped" if stopped else "stop_timeout", running=not stopped)
        return stopped

    def get_probe(self) -> dict[str, Any]:
        with self._state_lock:
            probe = dict(self._probe)
        thread = self._thread
        probe["thread_alive"] = bool(thread and thread.is_alive())
        with self._process_lock:
            process = self._process
            state = _process_state(process) if process is not None else "stopped"
            probe["worker_alive"] = state == "alive"
            probe["worker_state"] = state
            probe["worker_pid"] = self._owned_process_pid(process)
            cleanup = self._cleanup_process
            cleanup_state = _process_state(cleanup) if cleanup is not None else "stopped"
            probe["cleanup_worker_state"] = cleanup_state
            probe["cleanup_worker_pid"] = self._owned_process_pid(cleanup)
            probe["cleanup_pending"] = self._process_job is not None
        return probe

    @staticmethod
    def _owned_process_pid(process: Any) -> int | None:
        try:
            return int(process.pid) if process is not None and process.pid else None
        except Exception:
            return None

    def _has_owned_resources(self) -> bool:
        with self._process_lock:
            return self._process is not None or self._cleanup_process is not None or self._process_job is not None

    def _update_probe(self, **values: Any) -> None:
        with self._state_lock:
            self._probe.update(values)
            self._probe["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")

    def _due(self, now: datetime) -> tuple[str, str]:
        target = now.replace(hour=self.target_hour, minute=0, second=0, microsecond=0)
        if now < target:
            target -= timedelta(days=1)
        window_end = target + timedelta(hours=self.window_hours)
        reason = "night_window" if now < window_end else "idle_catchup"
        return target.date().isoformat(), reason

    def _idle(self) -> tuple[bool, str]:
        if self.idle_probe is None:
            return True, "idle"
        try:
            result = self.idle_probe()
        except Exception as exc:
            return False, f"idle_probe_error:{type(exc).__name__}"
        if isinstance(result, tuple):
            return bool(result[0]), str(result[1] if len(result) > 1 else "")
        return bool(result), "idle" if result else "busy"

    def _run(self) -> None:
        wait_sec = self.startup_delay_sec
        try:
            while not self._stop_event.wait(wait_sec):
                backup_date, schedule_reason = self._due(datetime.now())
                if self._has_owned_resources() and not self._terminate_current_process():
                    self._record_result(
                        {"status": "deferred", "reason": "worker_cleanup_pending", "backup_date": backup_date},
                        schedule_reason=schedule_reason,
                    )
                    wait_sec = self._reap_retry_sec
                    continue
                if backup_date == self._completed_date:
                    wait_sec = self.check_interval_sec
                    continue
                idle, idle_reason = self._idle()
                if not idle:
                    result = {"status": "deferred", "reason": idle_reason, "backup_date": backup_date}
                    self._record_result(result, schedule_reason=schedule_reason)
                    wait_sec = self.deferred_retry_sec
                    continue
                try:
                    result = self._run_worker(backup_date)
                except Exception as exc:
                    # Coordination/pipe/termination failures are job outcomes,
                    # not a reason to silently lose the scheduler thread.
                    logger.warning("Daily backup coordinator failed: %s", type(exc).__name__)
                    result = {
                        "status": "failure", "reason": "coordinator_exception",
                        "error_class": type(exc).__name__, "backup_date": backup_date,
                    }
                self._record_result(result, schedule_reason=schedule_reason)
                if self._has_owned_resources():
                    wait_sec = self._reap_retry_sec
                elif result.get("status") == "success":
                    self._completed_date = backup_date
                    wait_sec = self.check_interval_sec
                elif result.get("status") == "deferred":
                    wait_sec = self.deferred_retry_sec
                else:
                    wait_sec = self.failure_retry_sec
        finally:
            stopped = self._terminate_current_process()
            self._update_probe(running=False, status="stopped" if stopped else "cleanup_pending")

    def _worker_config(self, backup_date: str, job_token: str) -> dict[str, Any]:
        return {
            "database_path": self.database_path,
            "backup_dir": self.backup_dir,
            "health_dir": self.health_dir,
            "invalid_dir": self.invalid_dir,
            "database_lock_path": self.database_lock_path,
            "backup_date": backup_date,
            "role": self.role,
            "job_token": job_token,
            "retention_days": self.retention_days,
            "max_count": self.max_count,
            "emit_progress": True,
        }

    def _run_worker(self, backup_date: str) -> dict[str, Any]:
        if self._has_owned_resources() and not self._terminate_current_process():
            return {"status": "deferred", "reason": "worker_cleanup_pending", "backup_date": backup_date}
        job_token = uuid.uuid4().hex
        config = self._worker_config(backup_date, job_token)
        paths = _daily_paths(config)
        reservation_path = paths["reservation"]
        context = multiprocessing.get_context("spawn")
        receiving, sending = context.Pipe(duplex=False)
        process = context.Process(
            target=_daily_backup_worker,
            args=(config, sending),
            name="RemCardDailyBackupWorker",
            daemon=True,
        )
        try:
            process.start()
        except Exception as exc:
            receiving.close()
            sending.close()
            return {
                "status": "failure",
                "reason": "worker_start_failed",
                "error_class": type(exc).__name__,
            }
        sending.close()
        with self._process_lock:
            self._process = process
            self._process_job = {
                "reservation_path": reservation_path,
                "database_lock_path": self.database_lock_path,
                "staging_path": paths["staging"],
                "publication_staging_path": paths["publication_staging"],
                "snapshot_lease_path": paths["snapshot_lease"],
                "worker_pid": int(process.pid or 0),
                "job_token": job_token,
            }
        self._update_probe(status="worker_running", backup_date=backup_date, worker_pid=process.pid)
        deadline = time.monotonic() + self.worker_timeout_sec
        result: dict[str, Any] | None = None
        current_stage = "worker_start"
        stage_state = "started"
        stage_elapsed_ms: Any = None
        try:
            while time.monotonic() < deadline and not self._stop_event.is_set():
                if receiving.poll(0.1):
                    try:
                        message = receiving.recv()
                    except EOFError:
                        result = None
                        break
                    if isinstance(message, dict) and message.get("message_type") == "progress":
                        current_stage = str(message.get("stage") or current_stage)
                        stage_state = str(message.get("state") or stage_state)
                        stage_elapsed_ms = message.get("elapsed_ms")
                        self._update_probe(
                            status="worker_running",
                            backup_date=backup_date,
                            worker_pid=process.pid,
                            current_stage=current_stage,
                            stage_state=stage_state,
                            stage_elapsed_ms=stage_elapsed_ms,
                        )
                        continue
                    result = dict(message) if isinstance(message, dict) else None
                    break
                if not process.is_alive():
                    break
            if result is None and self._stop_event.is_set():
                result = {
                    "status": "deferred",
                    "reason": "stopping",
                    "stage": current_stage,
                    "stage_state": stage_state,
                }
            elif result is None and time.monotonic() >= deadline:
                result = {
                    "status": "failure",
                    "reason": "worker_timeout",
                    "stage": current_stage,
                    "stage_state": stage_state,
                    "stage_elapsed_ms": stage_elapsed_ms,
                }
            elif result is None:
                result = {
                    "status": "failure",
                    "reason": "worker_exited_without_result",
                    "exitcode": process.exitcode,
                }
        finally:
            try:
                receiving.close()
            except (OSError, ValueError):
                pass
            if not self._terminate_current_process():
                result = {
                    "status": "deferred", "reason": "worker_cleanup_pending",
                    "stage": current_stage, "stage_state": stage_state,
                }
        result["backup_date"] = backup_date
        return result

    def _terminate_current_process(self, timeout_sec: float = 4.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout_sec))
        if not self._process_stop_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            return False
        try:
            with self._process_lock:
                process = self._process
                job = self._process_job
            if process is not None:
                state, errors = _stop_process_bounded(
                    process, timeout_sec=min(2.0, max(0.0, deadline - time.monotonic())),
                )
                if errors:
                    logger.warning("Daily backup worker stop: state=%s errors=%s", state, errors)
                if state != "stopped":
                    self._update_probe(status="worker_stop_pending", worker_state=state)
                    return False
            if job:
                if time.monotonic() >= deadline:
                    return False
                cleanup = self._bounded_dead_worker_cleanup(
                    job, timeout_sec=max(0.0, deadline - time.monotonic()),
                )
                if not cleanup.get("completed"):
                    self._update_probe(status="cleanup_pending")
                    return False
            elif self._cleanup_process is not None:
                # A retained cleanup child must also be reaped before another
                # job can own these paths.
                if not self._finish_cleanup_process(timeout_sec=max(0.0, deadline - time.monotonic())):
                    return False
            if process is not None:
                try:
                    process.close()
                except Exception:
                    pass
            with self._process_lock:
                if self._process is process:
                    self._process = None
                    self._process_job = None
            return True
        except Exception as exc:
            # Keep every owned handle/job available to the next reap pass even
            # when creating a cleanup pipe/process fails transiently.
            logger.warning("Daily backup reap remains pending: %s", type(exc).__name__)
            self._update_probe(status="cleanup_pending")
            return False
        finally:
            self._process_stop_lock.release()

    def _signal_current_process_stop(self, timeout_sec: float = 1.0) -> bool:
        """Signal only; the scheduler retains exclusive cleanup ownership."""
        deadline = time.monotonic() + max(0.0, float(timeout_sec))
        if not self._process_stop_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            return False
        try:
            with self._process_lock:
                process = self._process
            if process is None:
                return True
            state, _errors = _stop_process_bounded(process, timeout_sec=max(0.0, deadline - time.monotonic()))
            return state == "stopped"
        finally:
            self._process_stop_lock.release()

    def _receive_cleanup_result(self) -> None:
        receiver = self._cleanup_receiver
        if receiver is None:
            return
        try:
            if receiver.poll(0):
                self._cleanup_result = dict(receiver.recv() or {})
        except (EOFError, OSError, ValueError):
            pass

    def _finish_cleanup_process(self, timeout_sec: float = 1.0) -> bool:
        cleanup = self._cleanup_process
        if cleanup is None:
            return True
        self._receive_cleanup_result()
        state, errors = _stop_process_bounded(cleanup, timeout_sec=timeout_sec)
        if errors:
            logger.warning("Daily backup cleanup stop: state=%s errors=%s", state, errors)
        self._receive_cleanup_result()
        if state != "stopped":
            return False
        if self._cleanup_receiver is not None:
            try:
                self._cleanup_receiver.close()
            except (OSError, ValueError):
                pass
        try:
            cleanup.close()
        except Exception:
            pass
        with self._process_lock:
            if self._cleanup_process is cleanup:
                self._cleanup_process = None
                self._cleanup_receiver = None
        return True

    def _bounded_dead_worker_cleanup(self, arguments: dict[str, Any], timeout_sec: float = 2.0) -> dict[str, bool]:
        """Keep a blocked cleanup child owned; never replace it with another."""
        deadline = time.monotonic() + max(0.0, float(timeout_sec))
        with self._process_stop_lock:
            if self._cleanup_process is not None:
                if not self._finish_cleanup_process(timeout_sec=max(0.0, deadline - time.monotonic())):
                    return {}
                previous = dict(self._cleanup_result)
                if previous.get("completed"):
                    self._cleanup_result = {}
                    return previous
            if time.monotonic() >= deadline:
                return {}
            context = multiprocessing.get_context("spawn")
            receiving, sending = context.Pipe(duplex=False)
            cleanup = context.Process(
                target=_cleanup_dead_worker_entry,
                args=(dict(arguments), sending),
                name="RemCardDailyBackupCleanup",
                daemon=True,
            )
            try:
                cleanup.start()
            except Exception as exc:
                receiving.close()
                sending.close()
                logger.warning("Daily backup dead-worker cleanup could not start: %s", type(exc).__name__)
                return {}
            sending.close()
            with self._process_lock:
                self._cleanup_process = cleanup
                self._cleanup_receiver = receiving
                self._cleanup_result = {}
            try:
                if receiving.poll(max(0.0, deadline - time.monotonic())):
                    self._receive_cleanup_result()
            except (EOFError, OSError, ValueError):
                pass
            if not self._finish_cleanup_process(timeout_sec=max(0.0, deadline - time.monotonic())):
                return {}
            result = dict(self._cleanup_result)
            self._cleanup_result = {}
            if not result.get("completed"):
                logger.warning("Daily backup dead-worker cleanup remains pending")
            return result

    def _record_result(self, result: dict[str, Any], *, schedule_reason: str) -> None:
        status = str(result.get("status") or "failure")
        reason = str(result.get("reason") or "unknown")
        fields = {
            "status": status,
            "reason": reason,
            "role": self.role,
            "backup_date": str(result.get("backup_date") or ""),
            "schedule_reason": schedule_reason,
            "backup_path": str(result.get("backup_path") or ""),
            "stage": str(result.get("stage") or ""),
            "stage_state": str(result.get("stage_state") or ""),
            "stage_elapsed_ms": result.get("stage_elapsed_ms"),
        }
        record_metric("daily_central_backup_result", 1, **fields)
        self._update_probe(**fields)
        if status == "success":
            logger.info("Daily central backup result: %s", fields)
        elif status == "deferred":
            logger.info("Daily central backup deferred: %s", fields)
        else:
            logger.warning("Daily central backup failed: %s", {**fields, "error": result.get("error", "")})
