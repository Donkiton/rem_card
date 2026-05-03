import logging
import os
import socket
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator, Optional

from rem_card.app.sqlite_shared import FileWriteLock, backup_connection
from rem_card.app.startup_db_guard import update_client_policy_min_version
from rem_card.app.unified_db_schema import (
    SCHEMA_REQUIRED_CLIENT_VERSION,
    ensure_unified_schema,
    is_unified_schema_ready,
)


@dataclass(frozen=True)
class SchemaMigrationResult:
    migrated: bool
    backup_path: str = ""
    policy_updated: bool = False


def _backup_name(prefix: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return f"{prefix}_{stamp}_{os.getpid()}.db"


def _build_backup_path(backup_dir: str, prefix: str) -> str:
    os.makedirs(backup_dir, exist_ok=True)
    return os.path.join(backup_dir, _backup_name(prefix))


def _controller_owner_id(controller: Any) -> str:
    return str(getattr(controller, "owner_id", "") or f"{socket.gethostname()}:{os.getpid()}:schema_migration")


def _acquire_controller_file_lock(controller: Any, source: str):
    attempts = max(1, int(getattr(controller, "max_retries", 20)))
    retry_delay = max(0.05, float(getattr(controller, "retry_delay_sec", 0.2)))
    lock = getattr(controller, "lock")
    owner_id = _controller_owner_id(controller)
    for _attempt in range(attempts):
        if lock.acquire(owner_id, source):
            return
        time.sleep(retry_delay)
    raise sqlite3.OperationalError("Could not acquire db lock for schema migration")


def _acquire_file_lock(lock_path: str, owner_id: str, source: str, logger: logging.Logger):
    lock = FileWriteLock(lock_path, stale_timeout_sec=10 * 60, logger=logger)
    deadline = time.time() + 60.0
    while time.time() < deadline:
        if lock.acquire(owner_id=owner_id, source=source):
            return lock
        time.sleep(0.2)
    raise sqlite3.OperationalError(f"Could not acquire db lock for schema migration: {lock_path}")


@contextmanager
def _plain_write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _run_schema_migration(conn: sqlite3.Connection, logger: logging.Logger, controller: Any, source: str):
    if controller is not None:
        with controller.transaction(conn, source=source):
            ensure_unified_schema(conn, logger=logger)
        return
    with _plain_write_transaction(conn):
        ensure_unified_schema(conn, logger=logger)


def _create_validated_backup(
    conn: sqlite3.Connection,
    *,
    backup_dir: str,
    invalid_dir: Optional[str],
    logger: logging.Logger,
    source: str,
) -> str:
    backup_path = _build_backup_path(backup_dir, "pre_migration")
    return backup_connection(
        conn,
        backup_path,
        invalid_dir=invalid_dir,
        logger=logger,
        validate=True,
        source=source,
    )


def _update_policy(
    policy_path: Optional[str],
    *,
    min_client_version: str,
    role: Optional[str],
    baza_dir: Optional[str],
    source: str,
) -> bool:
    if not policy_path:
        return False
    return update_client_policy_min_version(
        policy_path,
        min_client_version,
        role=role,
        baza_dir=baza_dir,
        reason=source,
    )


def _ensure_after_lock(
    conn: sqlite3.Connection,
    *,
    backup_dir: str,
    invalid_dir: Optional[str],
    policy_path: Optional[str],
    min_client_version: str,
    role: Optional[str],
    baza_dir: Optional[str],
    logger: logging.Logger,
    controller: Any,
    source: str,
) -> SchemaMigrationResult:
    if is_unified_schema_ready(conn):
        policy_updated = _update_policy(
            policy_path,
            min_client_version=min_client_version,
            role=role,
            baza_dir=baza_dir,
            source=f"{source}_schema_policy_sync",
        )
        return SchemaMigrationResult(migrated=False, policy_updated=policy_updated)

    backup_path = _create_validated_backup(
        conn,
        backup_dir=backup_dir,
        invalid_dir=invalid_dir,
        logger=logger,
        source=source,
    )
    policy_updated = _update_policy(
        policy_path,
        min_client_version=min_client_version,
        role=role,
        baza_dir=baza_dir,
        source=source,
    )
    _run_schema_migration(conn, logger, controller, source)
    if not is_unified_schema_ready(conn):
        raise RuntimeError("Unified schema migration finished but fastpath contract is not satisfied")
    return SchemaMigrationResult(migrated=True, backup_path=backup_path, policy_updated=policy_updated)


def ensure_unified_schema_with_migration_backup(
    conn: sqlite3.Connection,
    *,
    db_path: str,
    backup_dir: str,
    invalid_dir: Optional[str] = None,
    policy_path: Optional[str] = None,
    min_client_version: str = SCHEMA_REQUIRED_CLIENT_VERSION,
    role: Optional[str] = None,
    baza_dir: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
    controller: Any = None,
    lock_path: Optional[str] = None,
    source: str = "schema_migration",
) -> SchemaMigrationResult:
    logger = logger or logging.getLogger(__name__)
    _ = db_path

    if controller is not None:
        with controller.connection_guard(conn):
            _acquire_controller_file_lock(controller, source)
            try:
                return _ensure_after_lock(
                    conn,
                    backup_dir=backup_dir,
                    invalid_dir=invalid_dir,
                    policy_path=policy_path,
                    min_client_version=min_client_version,
                    role=role,
                    baza_dir=baza_dir,
                    logger=logger,
                    controller=controller,
                    source=source,
                )
            finally:
                controller.lock.release()

    owner_id = f"{socket.gethostname()}:{os.getpid()}:schema_migration"
    lock = _acquire_file_lock(lock_path, owner_id, source, logger) if lock_path else None
    try:
        return _ensure_after_lock(
            conn,
            backup_dir=backup_dir,
            invalid_dir=invalid_dir,
            policy_path=policy_path,
            min_client_version=min_client_version,
            role=role,
            baza_dir=baza_dir,
            logger=logger,
            controller=None,
            source=source,
        )
    finally:
        if lock:
            lock.release()
