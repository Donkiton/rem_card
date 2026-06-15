from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from rem_card.app.logger import logger
from rem_card.app.operblock_offline_store import (
    get_operblock_offline_active_dir,
    get_operblock_offline_root,
    has_active_local_operblock_case,
    mark_operblock_offline_session_verified,
    pending_completed_local_cases_count,
    read_operblock_offline_metadata,
)
from rem_card.app.sqlite_shared import backup_connection, configure_connection, run_integrity_check, run_quick_check


@dataclass
class OperBlockOfflineMigrationResult:
    ok: bool
    attempted: bool = False
    migrated_cases: int = 0
    skipped_cancelled: int = 0
    blocked: bool = False
    reason: str = ""
    user_message: str = ""
    network_backup_path: str = ""
    local_backup_path: str = ""
    migrated_case_uuids: list[str] = field(default_factory=list)


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return bool(row)


def _columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    if not _table_exists(conn, table_name):
        return []
    return [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()]


def _row_dict(row) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _stable_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _create_local_backup(local_conn: sqlite3.Connection, *, root: str | None = None) -> str:
    backup_dir = os.path.join(get_operblock_offline_root() if root is None else root, "backups")
    invalid_dir = os.path.join(get_operblock_offline_root() if root is None else root, "quarantine")
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(backup_dir, f"opblock_offline_pre_migration_{stamp}.db")
    backup_connection(
        local_conn,
        backup_path,
        invalid_dir=invalid_dir,
        logger=logger,
        validate=True,
        source="opblock_offline_local_pre_migration",
        lock_wait_sec=5.0,
    )
    return backup_path


def _insert_row(
    cursor: sqlite3.Cursor,
    table_name: str,
    source: dict[str, Any],
    *,
    overrides: dict[str, Any] | None = None,
    skip_columns: set[str] | None = None,
) -> int:
    conn = cursor.connection
    target_columns = _columns(conn, table_name)
    skip = {"id", *(skip_columns or set())}
    payload = {key: source.get(key) for key in target_columns if key in source and key not in skip}
    payload.update({key: value for key, value in dict(overrides or {}).items() if key in target_columns and key not in skip})
    columns = list(payload)
    placeholders = ", ".join("?" for _ in columns)
    quoted = ", ".join(f'"{key}"' for key in columns)
    cursor.execute(
        f'INSERT INTO "{table_name}" ({quoted}) VALUES ({placeholders})',
        [payload[key] for key in columns],
    )
    return int(cursor.lastrowid)


def _update_row(
    cursor: sqlite3.Cursor,
    table_name: str,
    row_id: int,
    source: dict[str, Any],
    *,
    overrides: dict[str, Any] | None = None,
    skip_columns: set[str] | None = None,
) -> None:
    target_columns = _columns(cursor.connection, table_name)
    skip = {"id", *(skip_columns or set())}
    payload = {key: source.get(key) for key in target_columns if key in source and key not in skip}
    payload.update({key: value for key, value in dict(overrides or {}).items() if key in target_columns and key not in skip})
    if not payload:
        return
    assignments = ", ".join(f'"{key}" = ?' for key in payload)
    cursor.execute(
        f'UPDATE "{table_name}" SET {assignments} WHERE id = ?',
        [payload[key] for key in payload] + [int(row_id)],
    )


def _protocol_for_network(
    cursor: sqlite3.Cursor,
    *,
    table_code: str,
    protocol_date: Any,
    protocol_number: Any,
    target_case_id: int | None,
) -> tuple[Any, Any, Any]:
    if not protocol_number or not protocol_date:
        return protocol_number, protocol_date, None
    conflict = cursor.execute(
        """
        SELECT id
        FROM operation_cases
        WHERE table_code = ?
          AND anesthesia_protocol_date = ?
          AND anesthesia_protocol_number = ?
          AND (? IS NULL OR id <> ?)
        LIMIT 1
        """,
        (table_code, protocol_date, protocol_number, target_case_id, target_case_id),
    ).fetchone()
    if not conflict:
        return protocol_number, protocol_date, None
    row = cursor.execute(
        """
        SELECT MAX(anesthesia_protocol_number)
        FROM operation_cases
        WHERE table_code = ?
          AND anesthesia_protocol_date = ?
        """,
        (table_code, protocol_date),
    ).fetchone()
    next_number = int((row[0] if row else 0) or 0) + 1
    return next_number, protocol_date, int(protocol_number)


def _remote_case_by_uuid(cursor: sqlite3.Cursor, offline_case_uuid: str) -> sqlite3.Row | None:
    if not offline_case_uuid:
        return None
    return cursor.execute(
        "SELECT * FROM operation_cases WHERE offline_case_uuid = ? LIMIT 1",
        (offline_case_uuid,),
    ).fetchone()


def _active_table_conflict(cursor: sqlite3.Cursor, *, table_code: str, offline_case_uuid: str) -> tuple[bool, int | None]:
    row = cursor.execute(
        """
        SELECT id, offline_case_uuid
        FROM operation_cases
        WHERE table_code = ?
          AND status = 'active'
        LIMIT 1
        """,
        (table_code,),
    ).fetchone()
    if not row:
        return False, None
    if str(row["offline_case_uuid"] or "") == str(offline_case_uuid or ""):
        return False, int(row["id"])
    return True, int(row["id"])


def _copy_admission_rows(
    cursor: sqlite3.Cursor,
    local_conn: sqlite3.Connection,
    *,
    table_name: str,
    local_admission_id: int,
    remote_admission_id: int,
) -> dict[int, int]:
    if not _table_exists(cursor.connection, table_name) or not _table_exists(local_conn, table_name):
        return {}
    result: dict[int, int] = {}
    rows = local_conn.execute(
        f'SELECT * FROM "{table_name}" WHERE admission_id = ? ORDER BY id',
        (int(local_admission_id),),
    ).fetchall()
    for row in rows:
        data = _row_dict(row)
        remote_id = _insert_row(
            cursor,
            table_name,
            data,
            overrides={"admission_id": int(remote_admission_id)},
        )
        result[int(data.get("id") or 0)] = remote_id
    return result


def _copy_case_rows(
    cursor: sqlite3.Cursor,
    local_conn: sqlite3.Connection,
    *,
    table_name: str,
    local_case_id: int,
    remote_case_id: int,
    remote_admission_id: int | None = None,
    order_map: dict[int, int] | None = None,
) -> dict[int, int]:
    if not _table_exists(cursor.connection, table_name) or not _table_exists(local_conn, table_name):
        return {}
    result: dict[int, int] = {}
    rows = local_conn.execute(
        f'SELECT * FROM "{table_name}" WHERE operation_case_id = ? ORDER BY id',
        (int(local_case_id),),
    ).fetchall()
    event_map: dict[int, int] = {}
    for row in rows:
        data = _row_dict(row)
        overrides: dict[str, Any] = {"operation_case_id": int(remote_case_id)}
        if remote_admission_id is not None:
            overrides["admission_id"] = int(remote_admission_id)
        if table_name == "operblock_timeline_events":
            parent_id = data.get("parent_event_id")
            source_order_id = data.get("source_order_id")
            overrides["parent_event_id"] = event_map.get(int(parent_id or 0)) if parent_id else None
            overrides["source_order_id"] = (order_map or {}).get(int(source_order_id or 0)) if source_order_id else None
        remote_id = _insert_row(cursor, table_name, data, overrides=overrides)
        local_id = int(data.get("id") or 0)
        result[local_id] = remote_id
        if table_name == "operblock_timeline_events":
            event_map[local_id] = remote_id
    return result


def _clear_existing_remote_case_details(cursor: sqlite3.Cursor, *, remote_case_id: int, remote_admission_id: int) -> None:
    for table_name in ("operblock_timeline_events", "operation_table_assignments"):
        if _table_exists(cursor.connection, table_name):
            cursor.execute(f'DELETE FROM "{table_name}" WHERE operation_case_id = ?', (int(remote_case_id),))
    for table_name in ("vitals", "orders", "patient_status_events"):
        if _table_exists(cursor.connection, table_name):
            cursor.execute(f'DELETE FROM "{table_name}" WHERE admission_id = ?', (int(remote_admission_id),))


def _migrate_one_case(
    cursor: sqlite3.Cursor,
    local_conn: sqlite3.Connection,
    *,
    local_case: dict[str, Any],
    session_id: str | None,
) -> tuple[str, int]:
    offline_case_uuid = str(local_case.get("offline_case_uuid") or "").strip()
    if not offline_case_uuid:
        offline_case_uuid = f"opblock:{local_case.get('id')}:{int(time.time())}"
    table_code = str(local_case.get("table_code") or "")
    conflict, same_case_remote_id = _active_table_conflict(
        cursor,
        table_code=table_code,
        offline_case_uuid=offline_case_uuid,
    )
    if conflict:
        raise RuntimeError(f"Операционный стол {table_code} занят другим активным случаем.")

    existing = _remote_case_by_uuid(cursor, offline_case_uuid)
    if existing and str(existing["status"] or "") == "closed":
        return offline_case_uuid, int(existing["id"])

    local_patient = local_conn.execute("SELECT * FROM patients WHERE id = ?", (int(local_case["patient_id"]),)).fetchone()
    local_admission = local_conn.execute("SELECT * FROM admissions WHERE id = ?", (int(local_case["admission_id"]),)).fetchone()
    if not local_patient or not local_admission:
        raise RuntimeError(f"Локальный случай {offline_case_uuid} повреждён: пациент или госпитализация не найдены.")

    target_case_id = same_case_remote_id or (int(existing["id"]) if existing else None)
    protocol_number, protocol_date, original_protocol = _protocol_for_network(
        cursor,
        table_code=table_code,
        protocol_date=local_case.get("anesthesia_protocol_date"),
        protocol_number=local_case.get("anesthesia_protocol_number"),
        target_case_id=target_case_id,
    )

    if target_case_id:
        remote_case = cursor.execute("SELECT * FROM operation_cases WHERE id = ?", (target_case_id,)).fetchone()
        remote_patient_id = int(remote_case["patient_id"])
        remote_admission_id = int(remote_case["admission_id"])
        _update_row(cursor, "patients", remote_patient_id, _row_dict(local_patient))
        _update_row(
            cursor,
            "admissions",
            remote_admission_id,
            _row_dict(local_admission),
            overrides={"patient_id": remote_patient_id, "unit_scope": "operblock", "admission_type": "operblock", "is_active": 0},
        )
        _clear_existing_remote_case_details(cursor, remote_case_id=target_case_id, remote_admission_id=remote_admission_id)
        remote_case_id = target_case_id
        _update_row(
            cursor,
            "operation_cases",
            remote_case_id,
            local_case,
            overrides={
                "patient_id": remote_patient_id,
                "admission_id": remote_admission_id,
                "status": "closed",
                "offline_case_uuid": offline_case_uuid,
                "offline_session_id": session_id,
                "anesthesia_protocol_number": protocol_number,
                "anesthesia_protocol_date": protocol_date,
                "original_protocol_number": original_protocol,
                "future_rao_admission_id": None,
                "migration_status": "verified",
                "migrated_at": _now_text(),
            },
        )
    else:
        remote_patient_id = _insert_row(cursor, "patients", _row_dict(local_patient))
        remote_admission_id = _insert_row(
            cursor,
            "admissions",
            _row_dict(local_admission),
            overrides={"patient_id": remote_patient_id, "unit_scope": "operblock", "admission_type": "operblock", "is_active": 0},
        )
        remote_case_id = _insert_row(
            cursor,
            "operation_cases",
            local_case,
            overrides={
                "patient_id": remote_patient_id,
                "admission_id": remote_admission_id,
                "status": "closed",
                "offline_case_uuid": offline_case_uuid,
                "offline_session_id": session_id,
                "anesthesia_protocol_number": protocol_number,
                "anesthesia_protocol_date": protocol_date,
                "original_protocol_number": original_protocol,
                "future_rao_admission_id": None,
                "migration_status": "verified",
                "migrated_at": _now_text(),
            },
        )

    order_map = _copy_admission_rows(
        cursor,
        local_conn,
        table_name="orders",
        local_admission_id=int(local_case["admission_id"]),
        remote_admission_id=remote_admission_id,
    )
    _copy_admission_rows(
        cursor,
        local_conn,
        table_name="vitals",
        local_admission_id=int(local_case["admission_id"]),
        remote_admission_id=remote_admission_id,
    )
    _copy_admission_rows(
        cursor,
        local_conn,
        table_name="patient_status_events",
        local_admission_id=int(local_case["admission_id"]),
        remote_admission_id=remote_admission_id,
    )
    _copy_case_rows(
        cursor,
        local_conn,
        table_name="operation_table_assignments",
        local_case_id=int(local_case["id"]),
        remote_case_id=remote_case_id,
    )
    _copy_case_rows(
        cursor,
        local_conn,
        table_name="operblock_timeline_events",
        local_case_id=int(local_case["id"]),
        remote_case_id=remote_case_id,
        remote_admission_id=remote_admission_id,
        order_map=order_map,
    )
    cursor.execute(
        """
        INSERT INTO opblock_offline_case_map (
            offline_case_uuid, offline_session_id, local_operation_case_id,
            remote_operation_case_id, original_protocol_number, network_protocol_number,
            content_hash, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'verified')
        ON CONFLICT(offline_case_uuid) DO UPDATE SET
            remote_operation_case_id = excluded.remote_operation_case_id,
            original_protocol_number = excluded.original_protocol_number,
            network_protocol_number = excluded.network_protocol_number,
            content_hash = excluded.content_hash,
            status = 'verified',
            updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
        """,
        (
            offline_case_uuid,
            session_id,
            int(local_case["id"]),
            remote_case_id,
            original_protocol,
            protocol_number,
            _stable_hash(local_case),
        ),
    )
    return offline_case_uuid, remote_case_id


def run_pending_operblock_offline_migration(
    network_db_manager,
    *,
    root: str | None = None,
) -> OperBlockOfflineMigrationResult:
    if has_active_local_operblock_case(root):
        return OperBlockOfflineMigrationResult(
            ok=True,
            attempted=False,
            blocked=True,
            reason="active_local_case",
            user_message="Есть незавершённый локальный случай оперблока. Перенос не выполнялся.",
        )
    if pending_completed_local_cases_count(root) <= 0:
        return OperBlockOfflineMigrationResult(ok=True, attempted=False, reason="no_pending_cases")

    metadata = read_operblock_offline_metadata(root)
    local_db_path = str(getattr(metadata, "local_db_path", "") or os.path.join(get_operblock_offline_active_dir(root), "operblock_local.db"))
    if not os.path.isfile(local_db_path):
        return OperBlockOfflineMigrationResult(ok=True, attempted=False, reason="local_db_missing")

    network_backup_path = ""
    local_backup_path = ""
    local_conn = sqlite3.connect(local_db_path, isolation_level=None, timeout=5.0)
    local_conn.row_factory = sqlite3.Row
    configure_connection(local_conn, profile="network")
    try:
        ok, reason = run_quick_check(local_conn)
        if not ok:
            return OperBlockOfflineMigrationResult(ok=False, attempted=False, reason=f"local_quick_check_failed:{reason}")
        network_backup_path = str(
            network_db_manager.create_validated_backup(
                prefix="opblock_offline_pre_migration",
                source="opblock_offline_migration",
            )
            or ""
        )
        if not network_backup_path:
            return OperBlockOfflineMigrationResult(ok=False, attempted=False, reason="network_backup_failed")
        local_backup_path = _create_local_backup(local_conn, root=root)

        local_cases = [
            _row_dict(row)
            for row in local_conn.execute(
                """
                SELECT *
                FROM operation_cases
                WHERE status = 'closed'
                  AND COALESCE(migration_status, '') NOT IN ('verified', 'discarded')
                  AND migrated_at IS NULL
                  AND COALESCE(excluded_from_migration, 0) = 0
                ORDER BY ended_at, id
                """
            ).fetchall()
        ]
        skipped_cancelled = int(
            local_conn.execute(
                "SELECT COUNT(*) FROM operation_cases WHERE status = 'cancelled' AND COALESCE(migration_status, '') <> 'verified'"
            ).fetchone()[0]
            or 0
        )
        migrated: list[tuple[str, int, int]] = []

        def operation(cursor: sqlite3.Cursor):
            for local_case in local_cases:
                case_uuid, remote_case_id = _migrate_one_case(
                    cursor,
                    local_conn,
                    local_case=local_case,
                    session_id=getattr(metadata, "offline_session_id", "active") if metadata else "active",
                )
                migrated.append((case_uuid, int(local_case["id"]), remote_case_id))

        network_db_manager.run_write_operation(operation, source="opblock_offline_migration")
        conn = getattr(network_db_manager, "_remcard_conn", None)
        if conn is not None:
            quick_ok, quick_result = run_quick_check(conn)
            if not quick_ok:
                return OperBlockOfflineMigrationResult(ok=False, attempted=True, reason=f"network_quick_check_failed:{quick_result}")
            integrity_ok, integrity_result = run_integrity_check(conn)
            if not integrity_ok:
                return OperBlockOfflineMigrationResult(ok=False, attempted=True, reason=f"network_integrity_check_failed:{integrity_result}")

        local_conn.execute("BEGIN IMMEDIATE")
        try:
            for case_uuid, local_case_id, remote_case_id in migrated:
                local_conn.execute(
                    """
                    UPDATE operation_cases
                    SET migration_status = 'verified',
                        migrated_at = ?,
                        migrated_remote_id = ?
                    WHERE id = ?
                    """,
                    (_now_text(), remote_case_id, local_case_id),
                )
            local_conn.execute("COMMIT")
        except Exception:
            if local_conn.in_transaction:
                local_conn.execute("ROLLBACK")
            raise

        if migrated:
            mark_operblock_offline_session_verified(root)
        return OperBlockOfflineMigrationResult(
            ok=True,
            attempted=True,
            migrated_cases=len(migrated),
            skipped_cancelled=skipped_cancelled,
            network_backup_path=network_backup_path,
            local_backup_path=local_backup_path,
            migrated_case_uuids=[item[0] for item in migrated],
        )
    except Exception as exc:
        logger.warning("Operblock offline migration failed: %s", exc, exc_info=True)
        return OperBlockOfflineMigrationResult(
            ok=False,
            attempted=True,
            blocked="занят другим активным случаем" in str(exc).lower(),
            reason=str(exc),
            user_message="Перенос не выполнен. Локальные данные сохранены. Повторите позже.",
            network_backup_path=network_backup_path,
            local_backup_path=local_backup_path,
        )
    finally:
        local_conn.close()
