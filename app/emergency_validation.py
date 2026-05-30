from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any

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


def compute_file_hash(path: str) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _readonly_uri(path: str) -> str:
    return f"file:{os.path.abspath(path)}?mode=ro"


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


def get_db_fingerprint(path: str) -> dict[str, Any]:
    stat_result = os.stat(path)
    fingerprint = {
        "path": os.path.abspath(path),
        "size_bytes": int(stat_result.st_size),
        "mtime_ns": int(getattr(stat_result, "st_mtime_ns", int(stat_result.st_mtime * 1_000_000_000))),
        "sha256": compute_file_hash(path),
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


def _result_from_path(
    path: str,
    *,
    ok: bool,
    reason: str,
    schema_version: int = 0,
    last_change_id: int = 0,
) -> SnapshotValidationResult:
    if not path or not os.path.exists(path):
        return SnapshotValidationResult(ok=ok, reason=reason, schema_version=schema_version, last_change_id=last_change_id)
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
        fingerprint=get_db_fingerprint(path) if ok else {},
    )


def validate_medical_db_snapshot(path: str) -> SnapshotValidationResult:
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


def validate_settings_db_snapshot(path: str) -> SnapshotValidationResult:
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
        missing = sorted(set(settings_schema.REQUIRED_TABLES) - tables)
        if missing:
            return _result_from_path(path, ok=False, reason=f"missing settings tables: {', '.join(missing)}")
        schema_status = settings_schema.inspect_schema_status(conn)
        if schema_status.schema_version > settings_schema.SCHEMA_VERSION:
            return _result_from_path(
                path,
                ok=False,
                reason="settings schema version is newer",
                schema_version=schema_status.schema_version,
            )
        if not schema_status.fastpath_ready:
            return _result_from_path(
                path,
                ok=False,
                reason=f"settings schema not ready: {schema_status.reason}",
                schema_version=schema_status.schema_version,
            )
        return _result_from_path(
            path,
            ok=True,
            reason="ok",
            schema_version=schema_status.schema_version,
            last_change_id=0,
        )
    except Exception as exc:
        return _result_from_path(path, ok=False, reason=str(exc))
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
