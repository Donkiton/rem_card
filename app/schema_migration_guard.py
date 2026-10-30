import logging
import json
import os
import socket
import sqlite3
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator, Optional

from rem_card.app.sqlite_shared import FileWriteLock, backup_connection
from rem_card.app.startup_db_guard import _compare_client_versions, update_client_policy_min_version
from rem_card.app.unified_db_schema import (
    SCHEMA_REQUIRED_CLIENT_VERSION,
    ensure_unified_schema,
    is_unified_schema_ready,
)
from rem_card.app.version import APP_VERSION
from rem_card.app.client_build_profile import (
    database_upgrades_disabled, local_operblock_schema_upgrades_allowed, require_compatible_schema,
)


SCHEMA_MIGRATION_LOCK_TIMEOUT_SEC = 60.0


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


def _wait_for_migration_lock(lock: FileWriteLock, owner_id: str, source: str, retry_delay: float = 0.2):
    # Миграция с резервной копией может длиться дольше обычной записи в БД.
    deadline = time.monotonic() + SCHEMA_MIGRATION_LOCK_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if lock.acquire(owner_id=owner_id, source=source):
            return
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(retry_delay, remaining))
    raise sqlite3.OperationalError("Could not acquire db lock for schema migration")


def _acquire_controller_file_lock(controller: Any, source: str):
    retry_delay = max(0.05, float(getattr(controller, "retry_delay_sec", 0.2)))
    _wait_for_migration_lock(controller.lock, _controller_owner_id(controller), source, retry_delay)


def _acquire_file_lock(lock_path: str, owner_id: str, source: str, logger: logging.Logger):
    lock = FileWriteLock(lock_path, stale_timeout_sec=10 * 60, logger=logger)
    try:
        _wait_for_migration_lock(lock, owner_id, source)
    except sqlite3.OperationalError as exc:
        raise sqlite3.OperationalError(f"Could not acquire db lock for schema migration: {lock_path}") from exc
    return lock


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


def _run_schema_migration(
    conn: sqlite3.Connection, logger: logging.Logger, controller: Any, source: str,
    allow_local_schema_upgrades: bool = False,
):
    def apply_and_validate():
        ensure_unified_schema(conn, logger=logger, allow_local_schema_upgrades=allow_local_schema_upgrades)
        if not is_unified_schema_ready(conn):
            raise RuntimeError("Unified schema migration finished but fastpath contract is not satisfied")

    if controller is not None:
        with controller.transaction(conn, source=source):
            apply_and_validate()
        return
    with _plain_write_transaction(conn):
        apply_and_validate()


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


def _require_existing_policy_floor(policy_path: Optional[str]) -> None:
    if policy_path is None:
        return
    try:
        with open(policy_path, "r", encoding="utf-8") as fh:
            policy = json.load(fh)
    except (FileNotFoundError, ValueError) as exc:
        raise RuntimeError("schema incompatible: обязательная политика доступа к базе недоступна") from exc
    floor = str(policy.get("min_client_version") or "") if isinstance(policy, dict) else ""
    if not floor or _compare_client_versions(floor, SCHEMA_REQUIRED_CLIENT_VERSION) < 0:
        raise RuntimeError(
            "schema incompatible: обязательное обновление политики базы требует сборки с миграциями"
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
    allow_local_schema_upgrades: bool = False,
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
    _run_schema_migration(conn, logger, controller, source, allow_local_schema_upgrades)
    return SchemaMigrationResult(migrated=True, backup_path=backup_path, policy_updated=policy_updated)


def ensure_unified_schema_with_migration_backup(
    conn: sqlite3.Connection,
    *,
    db_path: str,
    backup_dir: str,
    invalid_dir: Optional[str] = None,
    policy_path: Optional[str] = None,
    min_client_version: Optional[str] = None,
    role: Optional[str] = None,
    baza_dir: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
    controller: Any = None,
    lock_path: Optional[str] = None,
    source: str = "schema_migration",
    runtime_context: Any = None,
) -> SchemaMigrationResult:
    logger = logger or logging.getLogger(__name__)
    if getattr(runtime_context, "is_network", False) and not policy_path:
        raise RuntimeError("schema incompatible: для сетевой базы не задана политика доступа")
    with controller.connection_guard(conn) if controller is not None else nullcontext():
        allow_local_schema_upgrades = local_operblock_schema_upgrades_allowed(conn, runtime_context)
        if database_upgrades_disabled() and not allow_local_schema_upgrades:
            require_compatible_schema(is_unified_schema_ready(conn))
            # A mistakenly optional mandatory-release build cannot leave an
            # already upgraded shared database accessible to old clients.
            _require_existing_policy_floor(policy_path)
            return SchemaMigrationResult(migrated=False)
    _ = db_path
    effective_min_client_version = SCHEMA_REQUIRED_CLIENT_VERSION
    for candidate in (APP_VERSION, min_client_version):
        if candidate and _compare_client_versions(str(candidate), effective_min_client_version) > 0:
            effective_min_client_version = str(candidate)

    if controller is not None:
        with controller.connection_guard(conn):
            _acquire_controller_file_lock(controller, source)
            try:
                return _ensure_after_lock(
                    conn,
                    backup_dir=backup_dir,
                    invalid_dir=invalid_dir,
                    policy_path=policy_path,
                    min_client_version=effective_min_client_version,
                    role=role,
                    baza_dir=baza_dir,
                    logger=logger,
                    controller=controller,
                    source=source,
                    allow_local_schema_upgrades=allow_local_schema_upgrades,
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
            min_client_version=effective_min_client_version,
            role=role,
            baza_dir=baza_dir,
            logger=logger,
            controller=None,
            source=source,
            allow_local_schema_upgrades=allow_local_schema_upgrades,
        )
    finally:
        if lock:
            lock.release()
