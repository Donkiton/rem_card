from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from rem_card.app.db_runtime_context import DbRuntimeContext, build_operblock_offline_runtime_context
from rem_card.app.logger import logger
from rem_card.app.local_metrics import record_metric
from rem_card.app.sqlite_shared import configure_connection, run_quick_check


OPERBLOCK_OFFLINE_MODE = "opblock_offline"
OPERBLOCK_OFFLINE_WARNING = (
    "Нет связи с сетевой базой. Оперблок открыт в локальном режиме. "
    "Данные будут перенесены после восстановления связи."
)
OPERBLOCK_RUNTIME_DROP_WARNING = "Пропал доступ к сетевой базе. Работа продолжена локально."
OPERBLOCK_MIGRATION_TITLE = "Перенос данных оперблока"
OPERBLOCK_MIGRATION_MESSAGE = "Не выключайте ПК. Идёт перенос данных оперблока."
OPERBLOCK_OFFLINE_ROOT_ENV = "REMCARD_OPERBLOCK_OFFLINE_ROOT"
RETENTION_DAYS = 30
SHADOW_MIRROR_MIGRATION_STATUS = "shadow"
_OPBLOCK_SHADOW_CASE_TABLES = ("operation_table_assignments", "operblock_timeline_events")
_OPBLOCK_SHADOW_ADMISSION_TABLES = ("vitals", "orders", "patient_status_events")


@dataclass
class OperBlockOfflineSessionMetadata:
    offline_session_id: str
    offline_session_uuid: str
    local_db_path: str
    settings_snapshot_path: str
    created_at: str
    last_opened_at: str
    source_workstation_id: str
    source_role: str = "operblock"
    network_db_fingerprint_at_start: dict[str, Any] | None = None
    base_remote_last_change_id: int | None = None
    migration_status: str = "active"
    retain_until: str | None = None
    active_case_uuid: str | None = None


@dataclass
class OperBlockOfflineStartupSession:
    metadata: OperBlockOfflineSessionMetadata
    runtime_context: DbRuntimeContext


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_json_write(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.{int(time.time() * 1000)}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp_path, path)


def _program_data_root() -> str:
    override = str(os.environ.get(OPERBLOCK_OFFLINE_ROOT_ENV) or "").strip()
    if override:
        return os.path.abspath(os.path.normpath(override))
    base = os.environ.get("PROGRAMDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.abspath(os.path.join(base, "RemCard", "operblock_offline"))


def get_operblock_offline_root() -> str:
    return _program_data_root()


def get_operblock_offline_active_dir(root: str | None = None) -> str:
    return os.path.join(os.path.abspath(root or get_operblock_offline_root()), "active")


def get_operblock_offline_metadata_path(root: str | None = None) -> str:
    return os.path.join(get_operblock_offline_active_dir(root), "offline_session.json")


def get_operblock_shadow_journal_path(root: str | None = None) -> str:
    return os.path.join(get_operblock_offline_active_dir(root), "shadow_journal.jsonl")


def ensure_operblock_offline_dirs(root: str | None = None) -> dict[str, str]:
    base = os.path.abspath(root or get_operblock_offline_root())
    paths = {
        "root": base,
        "active": os.path.join(base, "active"),
        "archive": os.path.join(base, "archive"),
        "backups": os.path.join(base, "backups"),
        "logs": os.path.join(base, "logs"),
        "quarantine": os.path.join(base, "quarantine"),
    }
    active_children = (
        "locks",
        "settings",
        "settings/backups",
        "settings/backup_health",
        "backups",
        "backups/valid",
        "backup_health",
        "backup_health/invalid_backups",
        "config",
        "logs",
        "quarantine",
        "snapshots",
    )
    for path in paths.values():
        os.makedirs(path, exist_ok=True)
    for part in active_children:
        os.makedirs(os.path.join(paths["active"], *part.split("/")), exist_ok=True)
    return paths


def _read_metadata(path: str) -> OperBlockOfflineSessionMetadata | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning("Не удалось прочитать metadata локального оперблока %s: %s", path, exc)
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return OperBlockOfflineSessionMetadata(**payload)
    except TypeError:
        payload.setdefault("offline_session_id", Path(path).parent.name or "active")
        payload.setdefault("offline_session_uuid", str(uuid.uuid4()))
        payload.setdefault("created_at", _now_text())
        payload.setdefault("last_opened_at", _now_text())
        payload.setdefault("source_workstation_id", socket.gethostname())
        payload.setdefault("local_db_path", os.path.join(os.path.dirname(path), "operblock_local.db"))
        payload.setdefault(
            "settings_snapshot_path",
            os.path.join(os.path.dirname(path), "settings", "opblock_settings_snapshot.db"),
        )
        return OperBlockOfflineSessionMetadata(**payload)


def write_operblock_offline_metadata(metadata: OperBlockOfflineSessionMetadata, root: str | None = None) -> None:
    _atomic_json_write(get_operblock_offline_metadata_path(root), asdict(metadata))


def read_operblock_offline_metadata(root: str | None = None) -> OperBlockOfflineSessionMetadata | None:
    return _read_metadata(get_operblock_offline_metadata_path(root))


def build_operblock_offline_runtime(root: str | None = None) -> DbRuntimeContext:
    ensure_operblock_offline_dirs(root)
    return build_operblock_offline_runtime_context(get_operblock_offline_active_dir(root))


def _network_fingerprint(db_path: str | None) -> dict[str, Any] | None:
    if not db_path:
        return None
    try:
        stat = os.stat(db_path)
    except Exception:
        return None
    return {
        "db_path": os.path.abspath(db_path),
        "size_bytes": int(stat.st_size),
        "mtime_ns": int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
    }


def _base_remote_last_change_id(db_path: str | None) -> int | None:
    if not db_path or not os.path.isfile(db_path):
        return None
    conn = None
    try:
        conn = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True, isolation_level=None, timeout=2.0)
        configure_connection(conn, readonly=True, profile="network")
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM change_log").fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:
        return None
    finally:
        if conn is not None:
            conn.close()


def start_or_resume_operblock_offline_session(
    *,
    reason: str = "",
    network_db_path: str | None = None,
    root: str | None = None,
) -> OperBlockOfflineStartupSession:
    paths = ensure_operblock_offline_dirs(root)
    runtime_context = build_operblock_offline_runtime(root)
    metadata = read_operblock_offline_metadata(root)
    now = _now_text()
    if metadata is None:
        metadata = OperBlockOfflineSessionMetadata(
            offline_session_id="active",
            offline_session_uuid=str(uuid.uuid4()),
            local_db_path=runtime_context.medical_db_path,
            settings_snapshot_path=runtime_context.settings_db_path,
            created_at=now,
            last_opened_at=now,
            source_workstation_id=socket.gethostname(),
            network_db_fingerprint_at_start=_network_fingerprint(network_db_path),
            base_remote_last_change_id=_base_remote_last_change_id(network_db_path),
            migration_status="active",
        )
    else:
        metadata.last_opened_at = now
        metadata.local_db_path = runtime_context.medical_db_path
        metadata.settings_snapshot_path = runtime_context.settings_db_path
        if metadata.migration_status in {"verified", "discarded"}:
            metadata.migration_status = "active"
            metadata.retain_until = None
    write_operblock_offline_metadata(metadata, root)
    append_shadow_journal(
        {
            "event": "offline_session_opened",
            "reason": str(reason or ""),
            "runtime_mode": OPERBLOCK_OFFLINE_MODE,
            "session_uuid": metadata.offline_session_uuid,
        },
        root=root,
    )
    logger.warning(
        "OperBlock offline session opened root=%s local_db=%s reason=%s",
        paths["root"],
        runtime_context.medical_db_path,
        reason,
    )
    return OperBlockOfflineStartupSession(metadata=metadata, runtime_context=runtime_context)


def append_shadow_journal(payload: dict[str, Any], root: str | None = None) -> None:
    ensure_operblock_offline_dirs(root)
    event = {
        "ts": _now_text(),
        "host": socket.gethostname(),
        **dict(payload or {}),
    }
    path = get_operblock_shadow_journal_path(root)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def _journal_active_case_snapshot(db_manager) -> list[dict[str, Any]]:
    try:
        rows = db_manager.fetch_all_remcard(
            """
            SELECT id, offline_case_uuid, table_code, status, revision, updated_at
            FROM operation_cases
            WHERE status = 'active'
              AND table_code IN ('emergency', 'planned')
            ORDER BY id
            """
        )
    except Exception:
        return []
    db_path = str(getattr(db_manager, "db_path", "") or "")
    result: list[dict[str, Any]] = []
    for row in rows or []:
        data = dict(row)
        remote_id = int(data.get("id") or 0)
        data["offline_case_uuid"] = _remote_case_uuid(db_path, remote_id, data.get("offline_case_uuid"))
        result.append(data)
    return result


def record_operblock_write_intent(
    db_manager,
    *,
    description: str,
    operation_uuid: str | None = None,
    root: str | None = None,
) -> str:
    operation_uuid = str(operation_uuid or uuid.uuid4())
    runtime_context = getattr(db_manager, "runtime_context", None)
    append_shadow_journal(
        {
            "event": "opblock_write_intent",
            "operation_uuid": operation_uuid,
            "description": str(description or ""),
            "remote_commit_state": "pending",
            "runtime_mode": str(getattr(runtime_context, "mode", "") or ""),
            "remote_db_path": os.path.abspath(str(getattr(db_manager, "db_path", "") or "")) or "",
            "active_cases": _journal_active_case_snapshot(db_manager),
        },
        root=root,
    )
    return operation_uuid


def mark_operblock_write_remote_committed(
    db_manager,
    *,
    operation_uuid: str,
    description: str,
    root: str | None = None,
) -> None:
    runtime_context = getattr(db_manager, "runtime_context", None)
    append_shadow_journal(
        {
            "event": "opblock_write_remote_committed",
            "operation_uuid": str(operation_uuid or ""),
            "description": str(description or ""),
            "remote_commit_state": "remote_committed",
            "runtime_mode": str(getattr(runtime_context, "mode", "") or ""),
            "active_cases": _journal_active_case_snapshot(db_manager),
        },
        root=root,
    )


def mark_operblock_write_failed(
    *,
    operation_uuid: str | None,
    description: str,
    error: Exception | str,
    root: str | None = None,
) -> None:
    if not operation_uuid:
        return
    append_shadow_journal(
        {
            "event": "opblock_write_failed",
            "operation_uuid": str(operation_uuid or ""),
            "description": str(description or ""),
            "remote_commit_state": "failed",
            "error": str(error),
        },
        root=root,
    )


def _connect_local_readonly(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True, isolation_level=None, timeout=2.0)
    configure_connection(conn, readonly=True, profile="network")
    return conn


def has_active_local_operblock_case(root: str | None = None) -> bool:
    metadata = read_operblock_offline_metadata(root)
    db_path = str(getattr(metadata, "local_db_path", "") or os.path.join(get_operblock_offline_active_dir(root), "operblock_local.db"))
    if not os.path.isfile(db_path):
        return False
    conn = None
    try:
        conn = _connect_local_readonly(db_path)
        rows = conn.execute(
            """
            SELECT id, admission_id, offline_case_uuid, migration_status
            FROM operation_cases
            WHERE status = 'active'
            ORDER BY id
            """
        ).fetchall()
        for row in rows:
            if not _active_case_blocks_network_start(conn, row):
                continue
            if metadata is not None:
                metadata.active_case_uuid = str(row[2] or "") or None
                metadata.migration_status = "active"
                write_operblock_offline_metadata(metadata, root)
            return True
        if rows and metadata is not None:
            metadata.active_case_uuid = None
            if _pending_completed_local_cases_count_on_conn(conn) <= 0:
                metadata.migration_status = SHADOW_MIRROR_MIGRATION_STATUS
            write_operblock_offline_metadata(metadata, root)
        return False
    except Exception:
        return False
    finally:
        if conn is not None:
            conn.close()


def pending_completed_local_cases_count(root: str | None = None) -> int:
    metadata = read_operblock_offline_metadata(root)
    db_path = str(getattr(metadata, "local_db_path", "") or os.path.join(get_operblock_offline_active_dir(root), "operblock_local.db"))
    if not os.path.isfile(db_path):
        return 0
    conn = None
    try:
        conn = _connect_local_readonly(db_path)
        return _pending_completed_local_cases_count_on_conn(conn)
    except Exception:
        return 0
    finally:
        if conn is not None:
            conn.close()


def mark_operblock_offline_session_verified(root: str | None = None) -> None:
    metadata = read_operblock_offline_metadata(root)
    if metadata is None:
        return
    now = datetime.now().astimezone()
    metadata.migration_status = "verified"
    metadata.retain_until = (now + timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")
    metadata.active_case_uuid = None
    write_operblock_offline_metadata(metadata, root)


def cleanup_verified_operblock_offline_session(network_db_manager, *, root: str | None = None) -> bool:
    metadata = read_operblock_offline_metadata(root)
    if metadata is None or metadata.migration_status != "verified" or not metadata.retain_until:
        return False
    try:
        retain_until = datetime.fromisoformat(str(metadata.retain_until).replace("Z", "+00:00"))
    except Exception:
        return False
    if datetime.now().astimezone() < retain_until:
        return False
    db_path = str(metadata.local_db_path or os.path.join(get_operblock_offline_active_dir(root), "operblock_local.db"))
    if not os.path.isfile(db_path):
        return False
    conn = None
    try:
        conn = _connect_local_readonly(db_path)
        active_or_pending = conn.execute(
            """
            SELECT COUNT(*)
            FROM operation_cases
            WHERE status = 'active'
               OR COALESCE(migration_status, '') NOT IN ('verified', 'discarded')
            """
        ).fetchone()
        if int(active_or_pending[0] or 0) > 0:
            return False
        rows = conn.execute(
            """
            SELECT offline_case_uuid
            FROM operation_cases
            WHERE COALESCE(offline_case_uuid, '') <> ''
              AND migration_status = 'verified'
            """
        ).fetchall()
        uuids = [str(row[0] or "") for row in rows if row and row[0]]
    except Exception:
        return False
    finally:
        if conn is not None:
            conn.close()
    for case_uuid in uuids:
        row = network_db_manager.fetch_one_remcard(
            "SELECT id FROM operation_cases WHERE offline_case_uuid = ? AND status = 'closed' LIMIT 1",
            (case_uuid,),
        )
        if not row:
            return False
    removed = False
    for path in (
        db_path,
        f"{db_path}-wal",
        f"{db_path}-shm",
        f"{db_path}-journal",
        get_operblock_shadow_journal_path(root),
        get_operblock_offline_metadata_path(root),
    ):
        try:
            if os.path.isfile(path):
                os.remove(path)
                removed = True
        except Exception as exc:
            logger.warning("Не удалось удалить verified локальную opblock-сессию %s: %s", path, exc)
            return False
    if removed:
        logger.info("Verified operblock offline session removed after retention root=%s", root or get_operblock_offline_root())
    return removed


def local_quick_check_ok(root: str | None = None) -> bool:
    metadata = read_operblock_offline_metadata(root)
    db_path = str(getattr(metadata, "local_db_path", "") or os.path.join(get_operblock_offline_active_dir(root), "operblock_local.db"))
    if not os.path.isfile(db_path):
        return False
    conn = None
    try:
        conn = sqlite3.connect(db_path, isolation_level=None, timeout=5.0)
        configure_connection(conn, profile="network")
        ok, _reason = run_quick_check(conn)
        return bool(ok)
    except Exception:
        return False
    finally:
        if conn is not None:
            conn.close()


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return bool(row)


def _columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    try:
        return [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()]
    except Exception:
        return []


def _ensure_shadow_map_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS opblock_offline_shadow_map (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            offline_case_uuid TEXT NOT NULL,
            entity_name TEXT NOT NULL,
            remote_id INTEGER NOT NULL,
            local_id INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now')),
            updated_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now')),
            UNIQUE(offline_case_uuid, entity_name, remote_id)
        )
        """
    )


def _ensure_local_runtime_schema_ready(root: str | None = None) -> DbRuntimeContext:
    session = start_or_resume_operblock_offline_session(reason="shadow_schema_ready", root=root)
    db_path = session.runtime_context.medical_db_path
    schema_ready = False
    if os.path.isfile(db_path):
        conn = None
        try:
            conn = sqlite3.connect(db_path, isolation_level=None, timeout=5.0)
            configure_connection(conn, profile="network")
            from rem_card.app.operblock_schema import is_operblock_schema_ready

            schema_ready = bool(is_operblock_schema_ready(conn))
        except Exception:
            schema_ready = False
        finally:
            if conn is not None:
                conn.close()
    if schema_ready:
        return session.runtime_context

    from rem_card.app.operblock_schema import ensure_operblock_schema
    from rem_card.data.dao.db_manager import DatabaseManager

    manager = DatabaseManager(db_path, db_path, runtime_context=session.runtime_context)
    try:
        ensure_operblock_schema(manager)
    finally:
        manager.close(timeout_sec=1.0)
    return session.runtime_context


def _remote_case_uuid(remote_db_path: str, remote_case_id: int, explicit_uuid: Any = None) -> str:
    text = str(explicit_uuid or "").strip()
    if text:
        return text
    key = f"{os.path.normcase(os.path.abspath(remote_db_path))}:{int(remote_case_id)}"
    return f"shadow:{uuid.uuid5(uuid.NAMESPACE_URL, key)}"


def _mapped_local_id(conn: sqlite3.Connection, offline_case_uuid: str, entity_name: str, remote_id: int) -> int | None:
    row = conn.execute(
        """
        SELECT local_id
        FROM opblock_offline_shadow_map
        WHERE offline_case_uuid = ?
          AND entity_name = ?
          AND remote_id = ?
        """,
        (offline_case_uuid, entity_name, int(remote_id)),
    ).fetchone()
    return int(row[0]) if row else None


def _remember_mapping(
    conn: sqlite3.Connection,
    offline_case_uuid: str,
    entity_name: str,
    remote_id: int,
    local_id: int,
) -> None:
    conn.execute(
        """
        INSERT INTO opblock_offline_shadow_map (offline_case_uuid, entity_name, remote_id, local_id)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(offline_case_uuid, entity_name, remote_id) DO UPDATE SET
            local_id = excluded.local_id,
            updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
        """,
        (offline_case_uuid, entity_name, int(remote_id), int(local_id)),
    )


def _forget_assignment_mappings_for_local_id(
    conn: sqlite3.Connection,
    *,
    local_id: int,
    keep_offline_case_uuid: str,
    keep_remote_id: int,
) -> None:
    if not _table_exists(conn, "opblock_offline_shadow_map"):
        return
    conn.execute(
        """
        DELETE FROM opblock_offline_shadow_map
        WHERE entity_name = 'operation_table_assignments'
          AND local_id = ?
          AND NOT (offline_case_uuid = ? AND remote_id = ?)
        """,
        (int(local_id), str(keep_offline_case_uuid or ""), int(keep_remote_id)),
    )


def _operation_case_shadow_mapping(conn: sqlite3.Connection, offline_case_uuid: str, local_case_id: int):
    if not _table_exists(conn, "opblock_offline_shadow_map"):
        return None
    return conn.execute(
        """
        SELECT remote_id, updated_at
        FROM opblock_offline_shadow_map
        WHERE offline_case_uuid = ?
          AND entity_name = 'operation_cases'
          AND local_id = ?
        LIMIT 1
        """,
        (str(offline_case_uuid or ""), int(local_case_id)),
    ).fetchone()


def _has_unmapped_shadow_rows(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    owner_column: str,
    owner_id: int,
    offline_case_uuid: str,
) -> bool:
    if not _table_exists(conn, table_name) or not _table_exists(conn, "opblock_offline_shadow_map"):
        return False
    row = conn.execute(
        f"""
        SELECT 1
        FROM "{table_name}" item
        WHERE item."{owner_column}" = ?
          AND NOT EXISTS (
              SELECT 1
              FROM opblock_offline_shadow_map map
              WHERE map.offline_case_uuid = ?
                AND map.entity_name = ?
                AND map.local_id = item.id
          )
        LIMIT 1
        """,
        (int(owner_id), str(offline_case_uuid or ""), table_name),
    ).fetchone()
    return bool(row)


def _shadow_case_has_local_only_rows(
    conn: sqlite3.Connection,
    *,
    operation_case_id: int,
    admission_id: int,
    offline_case_uuid: str,
) -> bool:
    for table_name in _OPBLOCK_SHADOW_CASE_TABLES:
        if _has_unmapped_shadow_rows(
            conn,
            table_name=table_name,
            owner_column="operation_case_id",
            owner_id=int(operation_case_id),
            offline_case_uuid=offline_case_uuid,
        ):
            return True
    for table_name in _OPBLOCK_SHADOW_ADMISSION_TABLES:
        if _has_unmapped_shadow_rows(
            conn,
            table_name=table_name,
            owner_column="admission_id",
            owner_id=int(admission_id),
            offline_case_uuid=offline_case_uuid,
        ):
            return True
    return False


def _active_case_blocks_network_start(conn: sqlite3.Connection, row) -> bool:
    operation_case_id = int(row[0] or 0)
    admission_id = int(row[1] or 0)
    offline_case_uuid = str(row[2] or "")
    if not operation_case_id:
        return False
    if not offline_case_uuid:
        return True
    if _operation_case_shadow_mapping(conn, offline_case_uuid, operation_case_id) is None:
        return True
    return _shadow_case_has_local_only_rows(
        conn,
        operation_case_id=operation_case_id,
        admission_id=admission_id,
        offline_case_uuid=offline_case_uuid,
    )


def _pending_completed_local_cases_count_on_conn(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM operation_cases
        WHERE status = 'closed'
          AND COALESCE(migration_status, '') NOT IN ('verified', 'discarded', ?)
          AND migrated_at IS NULL
          AND COALESCE(excluded_from_migration, 0) = 0
        """,
        (SHADOW_MIRROR_MIGRATION_STATUS,),
    ).fetchone()
    return int(row[0] or 0) if row else 0


def _discard_stale_shadow_active_cases(conn: sqlite3.Connection, active_case_uuids: set[str]) -> int:
    if not _table_exists(conn, "operation_cases") or not _table_exists(conn, "opblock_offline_shadow_map"):
        return 0
    rows = conn.execute(
        """
        SELECT id, admission_id, offline_case_uuid
        FROM operation_cases
        WHERE status = 'active'
          AND COALESCE(migration_status, '') IN (?, 'active')
          AND COALESCE(offline_case_uuid, '') <> ''
        """,
        (SHADOW_MIRROR_MIGRATION_STATUS,),
    ).fetchall()
    discarded = 0
    for row in rows:
        operation_case_id = int(row[0] or 0)
        admission_id = int(row[1] or 0)
        offline_case_uuid = str(row[2] or "")
        if not offline_case_uuid or offline_case_uuid in active_case_uuids:
            continue
        if _operation_case_shadow_mapping(conn, offline_case_uuid, operation_case_id) is None:
            continue
        if _shadow_case_has_local_only_rows(
            conn,
            operation_case_id=operation_case_id,
            admission_id=admission_id,
            offline_case_uuid=offline_case_uuid,
        ):
            continue
        cursor = conn.execute(
            """
            UPDATE operation_cases
            SET status = 'cancelled',
                migration_status = 'discarded',
                excluded_from_migration = 1,
                last_modified_by = 'operblock'
            WHERE id = ?
              AND status = 'active'
              AND COALESCE(migration_status, '') IN (?, 'active')
            """,
            (operation_case_id, SHADOW_MIRROR_MIGRATION_STATUS),
        )
        if cursor.rowcount == 1:
            discarded += 1
            assignment_rows = []
            assignment_columns = set(_columns(conn, "operation_table_assignments"))
            if {"id", "operation_case_id"}.issubset(assignment_columns):
                assignment_filters = ["operation_case_id = ?"]
                assignment_params: list[Any] = [operation_case_id]
                if "status" in assignment_columns:
                    assignment_filters.append("status = 'active'")
                if "released_at" in assignment_columns:
                    assignment_filters.append("released_at IS NULL")
                assignment_rows = conn.execute(
                    f"""
                    SELECT id
                    FROM operation_table_assignments
                    WHERE {" AND ".join(assignment_filters)}
                    """,
                    tuple(assignment_params),
                ).fetchall()
            for assignment_row in assignment_rows:
                _deactivate_assignment_row(
                    conn,
                    local_id=int(assignment_row[0]),
                    reason="missing_from_active_set",
                    source="shadow_discard_stale_case",
                )
    return discarded


def _discard_stale_shadow_active_cases_if_possible(root: str | None, active_case_uuids: set[str]) -> int:
    metadata = read_operblock_offline_metadata(root)
    db_path = str(getattr(metadata, "local_db_path", "") or os.path.join(get_operblock_offline_active_dir(root), "operblock_local.db"))
    if not os.path.isfile(db_path):
        return 0
    conn = None
    try:
        conn = sqlite3.connect(db_path, isolation_level=None, timeout=5.0)
        configure_connection(conn, profile="network")
        conn.execute("BEGIN IMMEDIATE")
        discarded = _discard_stale_shadow_active_cases(conn, active_case_uuids)
        conn.execute("COMMIT")
        if discarded and metadata is not None:
            metadata.active_case_uuid = None
            if _pending_completed_local_cases_count_on_conn(conn) <= 0:
                metadata.migration_status = SHADOW_MIRROR_MIGRATION_STATUS
            write_operblock_offline_metadata(metadata, root)
        return discarded
    except Exception:
        try:
            if conn is not None and conn.in_transaction:
                conn.execute("ROLLBACK")
        except Exception:
            pass
        return 0
    finally:
        if conn is not None:
            conn.close()


def _insert_or_update_mapped_row(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    source: dict[str, Any],
    overrides: dict[str, Any] | None,
    offline_case_uuid: str,
    entity_name: str,
    remote_id: int,
) -> int:
    columns = _columns(conn, table_name)
    if not columns:
        raise RuntimeError(f"Локальная таблица не готова: {table_name}")
    local_id = _mapped_local_id(conn, offline_case_uuid, entity_name, remote_id)
    payload = {key: source.get(key) for key in columns if key != "id" and key in source}
    payload.update({key: value for key, value in dict(overrides or {}).items() if key in columns and key != "id"})
    if local_id:
        assignments = [f'"{key}" = ?' for key in payload]
        if assignments:
            conn.execute(
                f'UPDATE "{table_name}" SET {", ".join(assignments)} WHERE id = ?',
                [payload[key] for key in payload] + [local_id],
            )
        _remember_mapping(conn, offline_case_uuid, entity_name, remote_id, int(local_id))
        return int(local_id)
    insert_columns = list(payload)
    placeholders = ", ".join("?" for _ in insert_columns)
    quoted_columns = ", ".join(f'"{key}"' for key in insert_columns)
    conn.execute(
        f'INSERT INTO "{table_name}" ({quoted_columns}) VALUES ({placeholders})',
        [payload[key] for key in insert_columns],
    )
    local_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    _remember_mapping(conn, offline_case_uuid, entity_name, remote_id, local_id)
    return local_id


def _assignment_payload(
    conn: sqlite3.Connection,
    *,
    source: dict[str, Any],
    overrides: dict[str, Any],
) -> dict[str, Any]:
    columns = _columns(conn, "operation_table_assignments")
    if not columns:
        raise RuntimeError("Локальная таблица не готова: operation_table_assignments")
    payload = {key: source.get(key) for key in columns if key != "id" and key in source}
    payload.update({key: value for key, value in dict(overrides or {}).items() if key in columns and key != "id"})
    return payload


def _active_assignment_for_table(conn: sqlite3.Connection, table_code: str):
    return conn.execute(
        """
        SELECT *
        FROM operation_table_assignments
        WHERE table_code = ?
          AND status = 'active'
          AND released_at IS NULL
        ORDER BY id
        LIMIT 1
        """,
        (str(table_code or ""),),
    ).fetchone()


def _assignment_values_equal(row, payload: dict[str, Any]) -> bool:
    if row is None:
        return False
    for key, value in payload.items():
        if str(row[key] if row[key] is not None else "") != str(value if value is not None else ""):
            return False
    return True


def _row_value(row, key: str, index: int, default: Any = None) -> Any:
    try:
        return row[key]
    except Exception:
        try:
            return row[index]
        except Exception:
            return default


def _update_assignment_row(conn: sqlite3.Connection, *, local_id: int, payload: dict[str, Any]) -> None:
    assignments = [f'"{key}" = ?' for key in payload]
    if not assignments:
        return
    conn.execute(
        f'UPDATE operation_table_assignments SET {", ".join(assignments)} WHERE id = ?',
        [payload[key] for key in payload] + [int(local_id)],
    )


def _record_assignment_upsert_metric(
    *,
    table_code: str,
    operation_case_id: int,
    admission_id: int | None,
    previous_operation_case_id: int | None,
    action: str,
    source: str,
    duration_ms: float,
) -> None:
    record_metric(
        "opblock_shadow_mirror_assignment_upserted",
        1,
        table_code=str(table_code or ""),
        operation_case_id=int(operation_case_id),
        admission_id=admission_id,
        previous_operation_case_id=previous_operation_case_id,
        action=str(action or ""),
        source=str(source or ""),
        duration_ms=round(float(duration_ms or 0.0), 3),
    )


def _record_assignment_duplicate_resolved(
    *,
    table_code: str,
    old_operation_case_id: int | None,
    new_operation_case_id: int,
    source: str,
) -> None:
    record_metric(
        "opblock_shadow_mirror_duplicate_assignment_resolved",
        1,
        table_code=str(table_code or ""),
        old_operation_case_id=old_operation_case_id,
        new_operation_case_id=int(new_operation_case_id),
        source=str(source or ""),
        reason="upsert_conflict",
    )


def _deactivate_assignment_row(
    conn: sqlite3.Connection,
    *,
    local_id: int,
    reason: str,
    source: str,
) -> bool:
    columns = set(_columns(conn, "operation_table_assignments"))
    now = _now_text()
    updates: dict[str, Any] = {}
    if "status" in columns:
        updates["status"] = "released"
    if "released_at" in columns:
        updates["released_at"] = now
    if "last_modified_by" in columns:
        updates["last_modified_by"] = "operblock"
    if "updated_at" in columns:
        updates["updated_at"] = now
    if not updates:
        return False
    select_columns = ["id"]
    select_columns.append("table_code" if "table_code" in columns else "'' AS table_code")
    select_columns.append("operation_case_id" if "operation_case_id" in columns else "0 AS operation_case_id")
    filters = ["id = ?"]
    if "status" in columns:
        filters.append("status = 'active'")
    if "released_at" in columns:
        filters.append("released_at IS NULL")
    row = conn.execute(
        f"""
        SELECT {", ".join(select_columns)}
        FROM operation_table_assignments
        WHERE {" AND ".join(filters)}
        """,
        (int(local_id),),
    ).fetchone()
    if not row:
        return False
    assignments = [f'"{key}" = ?' for key in updates]
    cursor = conn.execute(
        f'UPDATE operation_table_assignments SET {", ".join(assignments)} WHERE id = ?',
        [updates[key] for key in updates] + [int(local_id)],
    )
    if cursor.rowcount == 1:
        record_metric(
            "opblock_shadow_mirror_assignment_stale_deactivated",
            1,
            table_code=str(_row_value(row, "table_code", 1, "") or ""),
            old_operation_case_id=int(_row_value(row, "operation_case_id", 2, 0) or 0),
            old_admission_id=None,
            reason=str(reason or ""),
            source=str(source or ""),
        )
        return True
    return False


def _deactivate_stale_shadow_assignments_for_case(
    conn: sqlite3.Connection,
    *,
    local_case_id: int,
    offline_case_uuid: str,
    active_remote_assignment_ids: set[int],
    source: str,
) -> int:
    if not _table_exists(conn, "operation_table_assignments") or not _table_exists(conn, "opblock_offline_shadow_map"):
        return 0
    columns = set(_columns(conn, "operation_table_assignments"))
    if not {"id", "operation_case_id"}.issubset(columns):
        return 0
    filters = [
        "item.operation_case_id = ?",
        "map.offline_case_uuid = ?",
    ]
    if "status" in columns:
        filters.append("item.status = 'active'")
    if "released_at" in columns:
        filters.append("item.released_at IS NULL")
    rows = conn.execute(
        f"""
        SELECT item.id, map.remote_id
        FROM operation_table_assignments item
        JOIN opblock_offline_shadow_map map
          ON map.entity_name = 'operation_table_assignments'
         AND map.local_id = item.id
        WHERE {" AND ".join(filters)}
        """,
        (int(local_case_id), str(offline_case_uuid or "")),
    ).fetchall()
    deactivated = 0
    for row in rows:
        remote_id = int(row["remote_id"] or 0)
        if remote_id in active_remote_assignment_ids:
            continue
        if _deactivate_assignment_row(
            conn,
            local_id=int(row["id"]),
            reason="missing_from_active_set",
            source=source,
        ):
            deactivated += 1
    return deactivated


def _mirror_operation_table_assignment(
    conn: sqlite3.Connection,
    *,
    source: dict[str, Any],
    overrides: dict[str, Any],
    offline_case_uuid: str,
    remote_id: int,
    reason: str,
) -> int:
    started = time.perf_counter()
    payload = _assignment_payload(conn, source=source, overrides=overrides)
    table_code = str(payload.get("table_code") or "")
    local_case_id = int(payload.get("operation_case_id") or 0)
    admission_id = payload.get("admission_id")
    mapped_id = _mapped_local_id(conn, offline_case_uuid, "operation_table_assignments", remote_id)
    active_conflict = _active_assignment_for_table(conn, table_code) if table_code else None
    payload_is_active = str(payload.get("status") or "") == "active" and not payload.get("released_at")
    target_id = int(
        (active_conflict["id"] if active_conflict is not None and payload_is_active else None)
        or mapped_id
        or 0
    )
    previous_case_id = int(active_conflict["operation_case_id"] or 0) if active_conflict else None
    action = "insert"
    if target_id:
        if mapped_id and int(mapped_id) != target_id:
            _deactivate_assignment_row(
                conn,
                local_id=int(mapped_id),
                reason="superseded_by_table_code_upsert",
                source=reason,
            )
        current = conn.execute("SELECT * FROM operation_table_assignments WHERE id = ?", (target_id,)).fetchone()
        previous_case_id = int(current["operation_case_id"] or 0) if current else previous_case_id
        if current is not None and _assignment_values_equal(current, payload):
            action = "noop"
        else:
            _update_assignment_row(conn, local_id=target_id, payload=payload)
            action = "update"
        _forget_assignment_mappings_for_local_id(
            conn,
            local_id=target_id,
            keep_offline_case_uuid=offline_case_uuid,
            keep_remote_id=remote_id,
        )
        _remember_mapping(conn, offline_case_uuid, "operation_table_assignments", remote_id, target_id)
    else:
        insert_columns = list(payload)
        placeholders = ", ".join("?" for _ in insert_columns)
        quoted_columns = ", ".join(f'"{key}"' for key in insert_columns)
        conn.execute(
            f'INSERT INTO operation_table_assignments ({quoted_columns}) VALUES ({placeholders})',
            [payload[key] for key in insert_columns],
        )
        target_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        _remember_mapping(conn, offline_case_uuid, "operation_table_assignments", remote_id, target_id)
    if active_conflict is not None and int(active_conflict["id"] or 0) == target_id:
        old_case_id = int(active_conflict["operation_case_id"] or 0)
        if old_case_id != local_case_id or not mapped_id:
            _record_assignment_duplicate_resolved(
                table_code=table_code,
                old_operation_case_id=old_case_id,
                new_operation_case_id=local_case_id,
                source=reason,
            )
    _record_assignment_upsert_metric(
        table_code=table_code,
        operation_case_id=local_case_id,
        admission_id=int(admission_id) if admission_id is not None else None,
        previous_operation_case_id=previous_case_id,
        action=action,
        source=reason,
        duration_ms=(time.perf_counter() - started) * 1000.0,
    )
    return int(target_id)


def _fetch_remote_rows(db_manager, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in db_manager.fetch_all_remcard(query, params)]


def _fetch_remote_one(db_manager, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    row = db_manager.fetch_one_remcard(query, params)
    return dict(row) if row else None


def _copy_simple_admission_table(
    local_conn: sqlite3.Connection,
    db_manager,
    *,
    table_name: str,
    remote_admission_id: int,
    local_admission_id: int,
    offline_case_uuid: str,
    extra_overrides: dict[str, Any] | None = None,
) -> dict[int, int]:
    if not _table_exists(local_conn, table_name):
        return {}
    rows = _fetch_remote_rows(
        db_manager,
        f'SELECT * FROM "{table_name}" WHERE admission_id = ? ORDER BY id',
        (int(remote_admission_id),),
    )
    mapping: dict[int, int] = {}
    for row in rows:
        remote_id = int(row.get("id") or 0)
        overrides = {"admission_id": int(local_admission_id), **dict(extra_overrides or {})}
        local_id = _insert_or_update_mapped_row(
            local_conn,
            table_name=table_name,
            source=row,
            overrides=overrides,
            offline_case_uuid=offline_case_uuid,
            entity_name=table_name,
            remote_id=remote_id,
        )
        mapping[remote_id] = local_id
    return mapping


def _mirror_active_case(
    local_conn: sqlite3.Connection,
    db_manager,
    *,
    remote_db_path: str,
    case: dict[str, Any],
    offline_session_id: str | None,
    reason: str,
) -> str:
    remote_case_id = int(case["id"])
    remote_patient_id = int(case["patient_id"])
    remote_admission_id = int(case["admission_id"])
    offline_case_uuid = _remote_case_uuid(remote_db_path, remote_case_id, case.get("offline_case_uuid"))

    patient = _fetch_remote_one(db_manager, "SELECT * FROM patients WHERE id = ?", (remote_patient_id,))
    admission = _fetch_remote_one(db_manager, "SELECT * FROM admissions WHERE id = ?", (remote_admission_id,))
    if not patient or not admission:
        raise RuntimeError(f"Не удалось скопировать активный случай оперблока #{remote_case_id}: пациент не найден.")

    local_patient_id = _insert_or_update_mapped_row(
        local_conn,
        table_name="patients",
        source=patient,
        overrides={},
        offline_case_uuid=offline_case_uuid,
        entity_name="patients",
        remote_id=remote_patient_id,
    )
    local_admission_id = _insert_or_update_mapped_row(
        local_conn,
        table_name="admissions",
        source=admission,
        overrides={"patient_id": local_patient_id, "unit_scope": "operblock", "admission_type": "operblock"},
        offline_case_uuid=offline_case_uuid,
        entity_name="admissions",
        remote_id=remote_admission_id,
    )
    local_case_id = _insert_or_update_mapped_row(
        local_conn,
        table_name="operation_cases",
        source=case,
        overrides={
            "patient_id": local_patient_id,
            "admission_id": local_admission_id,
            "offline_case_uuid": offline_case_uuid,
            "offline_session_id": offline_session_id,
            "migration_status": SHADOW_MIRROR_MIGRATION_STATUS,
            "original_local_id": None,
            "migrated_at": None,
            "migrated_remote_id": None,
            "excluded_from_migration": 0,
        },
        offline_case_uuid=offline_case_uuid,
        entity_name="operation_cases",
        remote_id=remote_case_id,
    )
    local_conn.execute(
        "UPDATE operation_cases SET original_local_id = COALESCE(original_local_id, ?) WHERE id = ?",
        (local_case_id, local_case_id),
    )

    _copy_simple_admission_table(
        local_conn,
        db_manager,
        table_name="vitals",
        remote_admission_id=remote_admission_id,
        local_admission_id=local_admission_id,
        offline_case_uuid=offline_case_uuid,
    )
    order_map = _copy_simple_admission_table(
        local_conn,
        db_manager,
        table_name="orders",
        remote_admission_id=remote_admission_id,
        local_admission_id=local_admission_id,
        offline_case_uuid=offline_case_uuid,
    )
    _copy_simple_admission_table(
        local_conn,
        db_manager,
        table_name="patient_status_events",
        remote_admission_id=remote_admission_id,
        local_admission_id=local_admission_id,
        offline_case_uuid=offline_case_uuid,
    )

    assignment_rows = _fetch_remote_rows(
        db_manager,
        "SELECT * FROM operation_table_assignments WHERE operation_case_id = ? ORDER BY id",
        (remote_case_id,),
    )
    active_remote_assignment_ids: set[int] = set()
    for row in assignment_rows:
        remote_assignment_id = int(row.get("id") or 0)
        if str(row.get("status") or "") == "active" and not row.get("released_at"):
            active_remote_assignment_ids.add(remote_assignment_id)
        _mirror_operation_table_assignment(
            local_conn,
            source=row,
            overrides={"operation_case_id": local_case_id},
            offline_case_uuid=offline_case_uuid,
            remote_id=remote_assignment_id,
            reason=reason,
        )
    _deactivate_stale_shadow_assignments_for_case(
        local_conn,
        local_case_id=local_case_id,
        offline_case_uuid=offline_case_uuid,
        active_remote_assignment_ids=active_remote_assignment_ids,
        source=reason,
    )

    event_map: dict[int, int] = {}
    event_rows = _fetch_remote_rows(
        db_manager,
        "SELECT * FROM operblock_timeline_events WHERE operation_case_id = ? ORDER BY id",
        (remote_case_id,),
    )
    for row in event_rows:
        remote_event_id = int(row.get("id") or 0)
        remote_parent_id = row.get("parent_event_id")
        remote_order_id = row.get("source_order_id")
        local_event_id = _insert_or_update_mapped_row(
            local_conn,
            table_name="operblock_timeline_events",
            source=row,
            overrides={
                "operation_case_id": local_case_id,
                "admission_id": local_admission_id,
                "parent_event_id": event_map.get(int(remote_parent_id or 0)) if remote_parent_id else None,
                "source_order_id": order_map.get(int(remote_order_id or 0)) if remote_order_id else None,
            },
            offline_case_uuid=offline_case_uuid,
            entity_name="operblock_timeline_events",
            remote_id=remote_event_id,
        )
        event_map[remote_event_id] = local_event_id
    return offline_case_uuid


def mirror_active_operblock_cases_from_network_db(db_manager, *, reason: str = "", root: str | None = None) -> int:
    runtime_context = getattr(db_manager, "runtime_context", None)
    if str(getattr(runtime_context, "mode", "") or "") != "network":
        return 0
    remote_db_path = str(getattr(db_manager, "db_path", "") or "")
    if not remote_db_path:
        return 0
    active_cases = _fetch_remote_rows(
        db_manager,
        """
        SELECT *
        FROM operation_cases
        WHERE status = 'active'
          AND table_code IN ('emergency', 'planned')
        ORDER BY id
        """,
    )
    if not active_cases:
        _discard_stale_shadow_active_cases_if_possible(root, set())
        return 0

    local_context = _ensure_local_runtime_schema_ready(root)
    local_conn = sqlite3.connect(local_context.medical_db_path, isolation_level=None, timeout=5.0)
    local_conn.row_factory = sqlite3.Row
    configure_connection(local_conn, profile="network")
    metadata = read_operblock_offline_metadata(root)
    mirrored = 0
    try:
        _ensure_shadow_map_table(local_conn)
        local_conn.execute("BEGIN IMMEDIATE")
        active_case_uuids: set[str] = set()
        for case in active_cases:
            case_uuid = _mirror_active_case(
                local_conn,
                db_manager,
                remote_db_path=remote_db_path,
                case=case,
                offline_session_id=getattr(metadata, "offline_session_id", "active") if metadata else "active",
                reason=str(reason or ""),
            )
            active_case_uuids.add(case_uuid)
            if metadata is not None:
                metadata.active_case_uuid = case_uuid
                if _pending_completed_local_cases_count_on_conn(local_conn) <= 0:
                    metadata.migration_status = SHADOW_MIRROR_MIGRATION_STATUS
            mirrored += 1
        discarded = _discard_stale_shadow_active_cases(local_conn, active_case_uuids)
        local_conn.execute("COMMIT")
        if metadata is not None:
            write_operblock_offline_metadata(metadata, root)
        append_shadow_journal(
            {
                "event": "active_cases_mirrored",
                "reason": str(reason or ""),
                "count": mirrored,
                "discarded_stale_shadow_count": discarded,
                "remote_db_path": os.path.abspath(remote_db_path),
            },
            root=root,
        )
        return mirrored
    except Exception:
        try:
            if local_conn.in_transaction:
                local_conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        local_conn.close()
