from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterator

from rem_card.app.local_metrics import record_metric
from rem_card.app.sqlite_uri import build_sqlite_file_uri
from rem_card.app.sqlite_shared import configure_connection, run_quick_check
from rem_card.data.settings import settings_schema


MEDICAL_REQUIRED_TABLES = (
    "patients",
    "admissions",
    "change_log",
    "vitals",
    "orders",
    "administrations",
    "fluids",
)


@dataclass(frozen=True)
class SnapshotValidationResult:
    ok: bool
    reason: str
    schema_version: int = 0
    last_change_id: int = 0
    file_hash: str = ""
    file_size: int = 0
    file_mtime: float = 0.0
    fingerprint: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _SnapshotFileIdentity:
    normalized_path: str
    device: int
    inode: int
    size_bytes: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class _SuccessfulSnapshotValidation:
    identity: _SnapshotFileIdentity
    schema_version: int
    result: SnapshotValidationResult


@dataclass
class _SnapshotValidationAttempt:
    successful: dict[tuple[str, str], _SuccessfulSnapshotValidation] = field(default_factory=dict)


_SNAPSHOT_VALIDATION_ATTEMPT: ContextVar[_SnapshotValidationAttempt | None] = ContextVar(
    "emergency_snapshot_validation_attempt",
    default=None,
)


@contextmanager
def emergency_snapshot_validation_attempt() -> Iterator[None]:
    """Limit successful immutable-snapshot reuse to one startup admission attempt.

    The caller must wrap the complete admitted startup sequence.  A new scope
    always starts with an empty cache; no validation result survives the scope.
    """

    token = _SNAPSHOT_VALIDATION_ATTEMPT.set(_SnapshotValidationAttempt())
    try:
        yield
    finally:
        _SNAPSHOT_VALIDATION_ATTEMPT.reset(token)


def _normalized_snapshot_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(path)))


def _snapshot_file_identity(path: str) -> _SnapshotFileIdentity | None:
    if not path:
        return None
    try:
        stat_result = os.stat(path)
    except OSError:
        return None
    if not os.path.isfile(path):
        return None
    return _SnapshotFileIdentity(
        normalized_path=_normalized_snapshot_path(path),
        device=int(getattr(stat_result, "st_dev", 0) or 0),
        inode=int(getattr(stat_result, "st_ino", 0) or 0),
        size_bytes=int(stat_result.st_size),
        mtime_ns=int(getattr(stat_result, "st_mtime_ns", int(stat_result.st_mtime * 1_000_000_000))),
        ctime_ns=int(getattr(stat_result, "st_ctime_ns", int(stat_result.st_ctime * 1_000_000_000))),
    )


def _result_matches_identity(result: SnapshotValidationResult, identity: _SnapshotFileIdentity) -> bool:
    if not result.ok or not result.file_hash:
        return False
    if int(result.file_size or 0) != identity.size_bytes:
        return False
    fingerprint = dict(result.fingerprint or {})
    try:
        fingerprint_path = _normalized_snapshot_path(str(fingerprint.get("path") or ""))
    except Exception:
        return False
    return (
        fingerprint_path == identity.normalized_path
        and int(fingerprint.get("size_bytes") or 0) == identity.size_bytes
        and int(fingerprint.get("mtime_ns") or 0) == identity.mtime_ns
    )


def _record_validation_reuse(target: str) -> None:
    try:
        record_metric(
            "emergency_snapshot_validation_reused",
            1,
            target=target,
            reason="unchanged_success_within_startup_attempt",
        )
    except Exception:
        pass


def _validate_snapshot_with_attempt_cache(
    target: str,
    path: str,
    validator: Callable[[str], SnapshotValidationResult],
) -> SnapshotValidationResult:
    attempt = _SNAPSHOT_VALIDATION_ATTEMPT.get()
    if attempt is None:
        return validator(path)

    before = _snapshot_file_identity(path)
    before_hash = _snapshot_hash_or_empty(path) if before is not None else ""
    normalized_path = _normalized_snapshot_path(path) if path else ""
    cache_key = (target, normalized_path)
    cached = attempt.successful.get(cache_key)
    if cached is not None and before == cached.identity:
        # Windows timestamps can coincide for rapid same-size writes. Verify
        # bytes as well, saving the repeated SQLite/schema validation only.
        if (before_hash and before_hash == cached.result.file_hash
                and _snapshot_file_identity(path) == before
                and cached.schema_version == int(cached.result.schema_version or 0)):
            _record_validation_reuse(target)
            return cached.result

    # Missing, replaced or mutated files must not leave a reusable success.
    attempt.successful.pop(cache_key, None)
    result = validator(path)
    after_hash = _snapshot_hash_or_empty(path) if result.ok else ""
    after = _snapshot_file_identity(path)
    if result.ok and (before is None or after is None or before != after
                      or not before_hash or before_hash != after_hash
                      or after_hash != result.file_hash):
        return SnapshotValidationResult(
            ok=False,
            reason="snapshot changed during validation",
            details={"validation_target": target},
        )
    if result.ok and after is not None and _result_matches_identity(result, after):
        attempt.successful[cache_key] = _SuccessfulSnapshotValidation(
            identity=after,
            schema_version=int(result.schema_version or 0),
            result=result,
        )
    return result


def _snapshot_hash_or_empty(path: str) -> str:
    try:
        return compute_file_hash(path)
    except OSError:
        return ""


def compute_file_hash(path: str) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _readonly_uri(path: str) -> str:
    return build_sqlite_file_uri(path, mode="ro")


def _open_readonly(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(
        _readonly_uri(path),
        uri=True,
        check_same_thread=False,
        isolation_level=None,
        timeout=5.0,
    )
    configure_connection(conn, readonly=True, profile="network")
    return conn


def _base_file_checks(path: str) -> tuple[bool, str]:
    if not path:
        return False, "empty file path"
    if not os.path.exists(path):
        return False, "file does not exist"
    if not os.path.isfile(path):
        return False, "path is not a file"
    if os.path.getsize(path) <= 0:
        return False, "file size is zero"
    return True, "ok"


def _cheap_file_fingerprint(path: str) -> dict[str, Any]:
    stat_result = os.stat(path)
    return {
        "path": os.path.abspath(path),
        "size_bytes": int(stat_result.st_size),
        "mtime_ns": int(getattr(stat_result, "st_mtime_ns", int(stat_result.st_mtime * 1_000_000_000))),
    }


def probe_medical_db_snapshot(path: str) -> SnapshotValidationResult:
    """Читает только идентификатор изменений и версию схемы, без полного сканирования БД."""
    started = time.perf_counter()
    base_ok, base_reason = _base_file_checks(path)
    conn = None
    probe_ok = False
    try:
        if not base_ok:
            return SnapshotValidationResult(ok=False, reason=base_reason)
        conn = _open_readonly(path)
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM change_log").fetchone()
        last_change_id = int(row[0] or 0) if row else 0
        try:
            row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
            schema_version = int(row[0] or 0) if row else 0
        except sqlite3.DatabaseError:
            schema_version = 0
        stat_result = os.stat(path)
        probe_ok = True
        return SnapshotValidationResult(
            ok=True,
            reason="probe_ok",
            schema_version=schema_version,
            last_change_id=last_change_id,
            file_size=int(stat_result.st_size),
            file_mtime=float(stat_result.st_mtime),
            fingerprint=_cheap_file_fingerprint(path),
        )
    except Exception as exc:
        return SnapshotValidationResult(ok=False, reason=str(exc))
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        record_metric(
            "emergency_medical_source_probe_ms",
            round((time.perf_counter() - started) * 1000.0, 3),
            status="ok" if probe_ok else "error",
        )


def probe_settings_db_snapshot(path: str) -> SnapshotValidationResult:
    """Проверяет доступность и stat файла настроек без открытия SQLite и вычисления SHA-256."""
    started = time.perf_counter()
    base_ok, base_reason = _base_file_checks(path)
    probe_ok = False
    try:
        if not base_ok:
            return SnapshotValidationResult(ok=False, reason=base_reason)
        stat_result = os.stat(path)
        probe_ok = True
        return SnapshotValidationResult(
            ok=True,
            reason="probe_ok",
            file_size=int(stat_result.st_size),
            file_mtime=float(stat_result.st_mtime),
            fingerprint=_cheap_file_fingerprint(path),
        )
    except Exception as exc:
        return SnapshotValidationResult(ok=False, reason=str(exc))
    finally:
        record_metric(
            "emergency_settings_source_probe_ms",
            round((time.perf_counter() - started) * 1000.0, 3),
            status="ok" if probe_ok else "error",
        )


def validate_sqlite_file_quick_check(path: str) -> tuple[bool, str]:
    ok, reason = _base_file_checks(path)
    if not ok:
        return ok, reason
    conn = None
    try:
        conn = _open_readonly(path)
        return run_quick_check(conn)
    except Exception as exc:
        return False, str(exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def get_db_schema_version(path: str) -> int:
    conn = None
    try:
        conn = _open_readonly(path)
        if "schema_migrations" not in _table_names(conn):
            return 0
        row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
        return int(row[0] or 0) if row else 0
    finally:
        if conn is not None:
            conn.close()


def get_db_last_change_id(path: str) -> int:
    conn = None
    try:
        conn = _open_readonly(path)
        if "change_log" not in _table_names(conn):
            raise RuntimeError("missing change_log table")
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM change_log").fetchone()
        return int(row[0] or 0) if row else 0
    finally:
        if conn is not None:
            conn.close()


def get_db_fingerprint(path: str, *, file_hash: str | None = None) -> dict[str, Any]:
    stat_result = os.stat(path)
    fingerprint = {
        "path": os.path.abspath(path),
        "size_bytes": int(stat_result.st_size),
        "mtime_ns": int(getattr(stat_result, "st_mtime_ns", int(stat_result.st_mtime * 1_000_000_000))),
        "sha256": file_hash or compute_file_hash(path),
    }
    try:
        fingerprint["schema_version"] = get_db_schema_version(path)
    except Exception as exc:
        fingerprint["schema_version_error"] = str(exc)
    try:
        fingerprint["last_change_id"] = get_db_last_change_id(path)
    except Exception as exc:
        fingerprint["last_change_id_error"] = str(exc)
    return fingerprint


def get_settings_db_fingerprint(path: str, *, file_hash: str | None = None) -> dict[str, Any]:
    stat_result = os.stat(path)
    fingerprint = {
        "path": os.path.abspath(path),
        "size_bytes": int(stat_result.st_size),
        "mtime_ns": int(getattr(stat_result, "st_mtime_ns", int(stat_result.st_mtime * 1_000_000_000))),
        "sha256": file_hash or compute_file_hash(path),
    }
    conn = None
    try:
        conn = _open_readonly(path)
        fingerprint["settings_schema_version"] = settings_schema.get_schema_version(conn)
        fingerprint["settings_required_table_count"] = len(settings_schema.REQUIRED_TABLES)
    except Exception as exc:
        fingerprint["settings_schema_error"] = str(exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return fingerprint


def _result_from_path(
    path: str,
    *,
    ok: bool,
    reason: str,
    schema_version: int = 0,
    last_change_id: int = 0,
    details: dict[str, Any] | None = None,
) -> SnapshotValidationResult:
    if not path or not os.path.exists(path):
        return SnapshotValidationResult(
            ok=ok,
            reason=reason,
            schema_version=schema_version,
            last_change_id=last_change_id,
            details=dict(details or {}),
        )
    stat_result = os.stat(path)
    file_hash = compute_file_hash(path) if ok else ""
    return SnapshotValidationResult(
        ok=ok,
        reason=reason,
        schema_version=schema_version,
        last_change_id=last_change_id,
        file_hash=file_hash,
        file_size=int(stat_result.st_size),
        file_mtime=float(stat_result.st_mtime),
        fingerprint=get_db_fingerprint(path, file_hash=file_hash) if ok else {},
        details=dict(details or {}),
    )


def _settings_result_from_path(
    path: str,
    *,
    ok: bool,
    reason: str,
    schema_version: int = 0,
    details: dict[str, Any] | None = None,
) -> SnapshotValidationResult:
    if not path or not os.path.exists(path):
        return SnapshotValidationResult(ok=ok, reason=reason, schema_version=schema_version, details=dict(details or {}))
    stat_result = os.stat(path)
    file_hash = compute_file_hash(path) if ok else ""
    fingerprint = get_settings_db_fingerprint(path, file_hash=file_hash) if ok else {}
    return SnapshotValidationResult(
        ok=ok,
        reason=reason,
        schema_version=schema_version,
        last_change_id=0,
        file_hash=file_hash,
        file_size=int(stat_result.st_size),
        file_mtime=float(stat_result.st_mtime),
        fingerprint=fingerprint,
        details=dict(details or {}),
    )


def _record_settings_snapshot_schema_drift(
    path: str,
    *,
    reason: str,
    missing_tables: list[str] | tuple[str, ...] = (),
    actual_schema_version: int = 0,
) -> None:
    try:
        stat_result = os.stat(path)
        snapshot_created_at = datetime.fromtimestamp(stat_result.st_mtime).isoformat()
    except Exception:
        snapshot_created_at = ""
    missing_table = ",".join(str(item) for item in missing_tables)
    try:
        record_metric(
            "emergency_settings_snapshot_schema_drift",
            1,
            reason=str(reason or "schema_drift"),
            missing_table=missing_table,
            expected_schema_version=settings_schema.SCHEMA_VERSION,
            actual_schema_version=int(actual_schema_version or 0),
            snapshot_created_at=snapshot_created_at,
            settings_db_path=os.path.abspath(path) if path else "",
        )
    except Exception:
        pass


def _validate_medical_db_snapshot_uncached(path: str) -> SnapshotValidationResult:
    base_ok, base_reason = _base_file_checks(path)
    if not base_ok:
        return SnapshotValidationResult(ok=False, reason=base_reason)
    conn = None
    try:
        conn = _open_readonly(path)
        ok, reason = run_quick_check(conn)
        if not ok:
            return _result_from_path(path, ok=False, reason=f"quick_check failed: {reason}")
        tables = _table_names(conn)
        missing = sorted(set(MEDICAL_REQUIRED_TABLES) - tables)
        if missing:
            return _result_from_path(path, ok=False, reason=f"missing tables: {', '.join(missing)}")
        schema_version = 0
        if "schema_migrations" in tables:
            row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
            schema_version = int(row[0] or 0) if row else 0
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM change_log").fetchone()
        last_change_id = int(row[0] or 0) if row else 0
        return _result_from_path(
            path,
            ok=True,
            reason="ok",
            schema_version=schema_version,
            last_change_id=last_change_id,
        )
    except Exception as exc:
        return _result_from_path(path, ok=False, reason=str(exc))
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def validate_medical_db_snapshot(path: str) -> SnapshotValidationResult:
    from rem_card.app.startup_diagnostics import startup_span
    with startup_span("snapshot_validation", target="snapshot_medical"):
        return _validate_snapshot_with_attempt_cache(
            "medical", path, _validate_medical_db_snapshot_uncached,
        )


def _validate_settings_db_snapshot_uncached(path: str) -> SnapshotValidationResult:
    base_ok, base_reason = _base_file_checks(path)
    if not base_ok:
        return SnapshotValidationResult(ok=False, reason=base_reason)
    conn = None
    try:
        conn = _open_readonly(path)
        ok, reason = run_quick_check(conn)
        if not ok:
            return _settings_result_from_path(path, ok=False, reason=f"quick_check failed: {reason}")
        tables = _table_names(conn)
        missing = sorted(set(settings_schema.REQUIRED_TABLES) - tables)
        if missing:
            actual_schema_version = settings_schema.get_schema_version(conn)
            details = {
                "missing_tables": missing,
                "expected_schema_version": settings_schema.SCHEMA_VERSION,
                "actual_schema_version": actual_schema_version,
            }
            _record_settings_snapshot_schema_drift(
                path,
                reason="missing_settings_tables",
                missing_tables=missing,
                actual_schema_version=actual_schema_version,
            )
            return _settings_result_from_path(
                path,
                ok=False,
                reason=f"invalid_snapshot_schema_drift: missing settings tables: {', '.join(missing)}",
                schema_version=actual_schema_version,
                details=details,
            )
        schema_status = settings_schema.inspect_schema_status(conn)
        if schema_status.schema_version > settings_schema.SCHEMA_VERSION:
            _record_settings_snapshot_schema_drift(
                path,
                reason="settings_schema_version_newer",
                actual_schema_version=schema_status.schema_version,
            )
            return _settings_result_from_path(
                path,
                ok=False,
                reason="invalid_snapshot_schema_drift: settings schema version is newer",
                schema_version=schema_status.schema_version,
                details={
                    "expected_schema_version": settings_schema.SCHEMA_VERSION,
                    "actual_schema_version": schema_status.schema_version,
                },
            )
        if not schema_status.fastpath_ready:
            if schema_status.reason in {
                "missing_schema_version",
                "invalid_schema_version",
                "schema_version_outdated",
                "missing_indexes",
                "missing_unique_constraints",
                "missing_catalog_versions",
                "active_migration_marker",
            }:
                _record_settings_snapshot_schema_drift(
                    path,
                    reason=schema_status.reason,
                    missing_tables=schema_status.missing_tables,
                    actual_schema_version=schema_status.schema_version,
                )
                reason_prefix = "invalid_snapshot_schema_drift"
            else:
                reason_prefix = "settings schema not ready"
            return _settings_result_from_path(
                path,
                ok=False,
                reason=f"{reason_prefix}: settings schema not ready: {schema_status.reason}",
                schema_version=schema_status.schema_version,
                details={
                    "schema_status": schema_status.reason,
                    "missing_tables": list(schema_status.missing_tables),
                    "missing_indexes": list(schema_status.missing_indexes),
                    "missing_unique_constraints": list(schema_status.missing_unique_constraints),
                    "missing_catalog_keys": list(schema_status.missing_catalog_keys),
                    "expected_schema_version": settings_schema.SCHEMA_VERSION,
                    "actual_schema_version": schema_status.schema_version,
                },
            )
        file_hash = compute_file_hash(path)
        stat_result = os.stat(path)
        return SnapshotValidationResult(
            ok=True,
            reason="ok",
            schema_version=schema_status.schema_version,
            last_change_id=0,
            file_hash=file_hash,
            file_size=int(stat_result.st_size),
            file_mtime=float(stat_result.st_mtime),
            fingerprint=get_settings_db_fingerprint(path, file_hash=file_hash),
            details={
                "expected_schema_version": settings_schema.SCHEMA_VERSION,
                "actual_schema_version": schema_status.schema_version,
            },
        )
    except Exception as exc:
        return _settings_result_from_path(path, ok=False, reason=str(exc))
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def validate_settings_db_snapshot(path: str) -> SnapshotValidationResult:
    from rem_card.app.startup_diagnostics import startup_span
    with startup_span("snapshot_validation", target="snapshot_settings"):
        return _validate_snapshot_with_attempt_cache(
            "settings", path, _validate_settings_db_snapshot_uncached,
        )
