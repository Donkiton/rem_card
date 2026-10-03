"""Bounded, process-local reuse of a successful central database scan."""
from __future__ import annotations

import json
import os
import threading
import time

from rem_card.app.sqlite_shared import NETWORK_SAFE_DB_PROFILE


ADMISSION_TTL_SEC = 30 * 60
HANDOFF_ENV = "REMCARD_STARTUP_GUARD_QUICKCHECK_OK"
_lock = threading.Lock()
_generation = 0
_receipt = None


def invalidate_role_admission() -> None:
    global _receipt, _generation
    with _lock:
        _generation += 1
        _receipt = None
        os.environ.pop(HANDOFF_ENV, None)


def admission_generation() -> int:
    with _lock:
        return _generation


def file_identity(path: str) -> dict:
    stat = os.stat(path)
    if not stat.st_ino:
        raise ValueError("database file identity is unavailable")
    identity = {"path": os.path.normcase(os.path.abspath(path)),
                "device": int(stat.st_dev), "inode": int(stat.st_ino)}
    if os.name == "nt":
        identity["created_ns"] = stat.st_ctime_ns
    return identity


def read_admission_state(conn, path: str) -> dict:
    """Read schema metadata only; clinical writes do not expire admission."""
    from rem_card.app.unified_db_schema import SCHEMA_FASTPATH_META_KEY
    identity = file_identity(path)
    root = os.path.dirname(os.path.dirname(path))
    if os.path.exists(os.path.join(root, "locks", "recovery.lock")):
        raise ValueError("database recovery is active")
    markers = []
    for relative in ("quarantine", "quarantine/shared_db", "backup_health/invalid_backups"):
        marker = os.path.join(root, relative)
        try:
            stat = os.stat(marker)
            markers.append([relative, int(stat.st_ino), stat.st_mtime_ns])
        except FileNotFoundError:
            markers.append([relative, None, None])
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('meta','schema_migrations')")}
    migration = None
    revision = None
    if "schema_migrations" in tables:
        migration = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    if "meta" in tables:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (SCHEMA_FASTPATH_META_KEY,)).fetchone()
        revision = row[0] if row else None
    schema = {name: conn.execute(f"PRAGMA {name}").fetchone()[0]
              for name in ("schema_version", "user_version", "application_id", "journal_mode")}
    if file_identity(path) != identity:
        raise ValueError("database identity changed during admission probe")
    return {"identity": identity, "schema": schema, "migration": migration,
            "revision": revision, "markers": markers,
            "db_profile": NETWORK_SAFE_DB_PROFILE}


def remember_full_scan(state: dict, *, generation: int, checked_monotonic: float,
                       checked_at_epoch: float) -> None:
    global _receipt
    # A profile conversion needs another full check before it can be reused.
    if not state or state.get("schema", {}).get("journal_mode") != "delete":
        return
    with _lock:
        if generation == _generation:
            _receipt = {"state": state, "generation": generation, "pid": os.getpid(),
                        "checked_monotonic": checked_monotonic, "checked_at_epoch": checked_at_epoch}


def recent_admission(path: str) -> tuple[dict | None, str]:
    global _receipt
    with _lock:
        receipt = _receipt
        if receipt is None:
            return None, "no_full_scan"
        age = time.monotonic() - receipt["checked_monotonic"]
        if receipt["pid"] != os.getpid() or not 0 <= age < ADMISSION_TTL_SEC:
            _receipt = None
            return None, "expired"
        if receipt["state"]["identity"]["path"] != os.path.normcase(os.path.abspath(path)):
            _receipt = None
            return None, "database_path_changed"
        return receipt, "recent_full_scan"


def admission_still_valid(receipt: dict) -> bool:
    with _lock:
        return (receipt is _receipt and receipt["generation"] == _generation
                and receipt["pid"] == os.getpid()
                and 0 <= time.monotonic() - receipt["checked_monotonic"] < ADMISSION_TTL_SEC)


def read_only_admission_probe(path: str) -> tuple[bool, str, bool]:
    import sqlite3
    from rem_card.app.sqlite_shared import configure_connection
    from rem_card.app.sqlite_uri import build_sqlite_file_uri
    from rem_card.app.startup_diagnostics import startup_span
    try:
        with startup_span("startup_admission_probe", target="central_medical"):
            conn = sqlite3.connect(build_sqlite_file_uri(path, mode="ro"), uri=True,
                                   isolation_level=None, timeout=5.0)
            try:
                configure_connection(conn, readonly=True, profile="network")
                conn.execute("BEGIN")
                return True, json.dumps(read_admission_state(conn, path)), False
            finally:
                conn.close()
    except Exception as exc:
        # A failed cheap probe only requests the normal full check; it never
        # authorizes recovery or a local emergency database by itself.
        return False, str(exc), False
