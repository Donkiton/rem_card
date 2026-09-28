"""Immutable archive-transfer primitives for locally completed OperBlock cases.

The functions in this module deliberately know nothing about UI or network
startup.  They make a completed case portable and provide the receipt/dest-
ination contracts used by the background exporter.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Iterable


_CASE_COLUMNS_TO_SKIP = {
    "id", "patient_id", "admission_id", "migrated_at", "migrated_remote_id",
    "migration_status", "original_local_id", "future_rao_admission_id",
    "offline_session_id",
}
_CHILD_COLUMNS_TO_SKIP = {"id", "operation_case_id", "admission_id", "patient_id"}
_CHILD_TABLES = (
    ("orders", "admission_id"),
    ("vitals", "admission_id"),
    ("patient_status_events", "admission_id"),
    ("operation_table_assignments", "operation_case_id"),
    ("operblock_timeline_events", "operation_case_id"),
    ("operblock_archive_edit_history", "operation_case_id"),
)
_VOLATILE_COLUMNS = {"updated_at", "last_modified_by"}


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).fetchone())


def columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    if not table_exists(conn, table_name):
        return []
    return [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table_name}")')]


def row_dict(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _semantic_row(row: sqlite3.Row | dict[str, Any], *, skip: set[str]) -> dict[str, Any]:
    return {
        key: value
        for key, value in row_dict(row).items()
        if key not in skip and key not in _VOLATILE_COLUMNS
    }


def _sorted_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def build_case_payload(
    conn: sqlite3.Connection,
    case: sqlite3.Row | dict[str, Any],
    *,
    installation_origin_id: str,
) -> dict[str, Any]:
    """Return the complete semantic case content, independent of row ids.

    Primary and child row IDs are database-local.  They are intentionally
    removed, whereas every clinical/content column (including revisions and
    timeline parent/source references) remains part of the receipt.  Parent
    and order references are converted to their source ordinals so equivalent
    copied graphs produce the same payload on a retry.
    """
    source_case = row_dict(case)
    case_id = int(source_case["id"])
    admission_id = int(source_case["admission_id"])
    patient_id = int(source_case["patient_id"])
    patient = conn.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
    admission = conn.execute("SELECT * FROM admissions WHERE id=?", (admission_id,)).fetchone()
    if patient is None or admission is None:
        raise RuntimeError("Повреждён локальный завершённый случай: пациент или госпитализация не найдены.")

    payload: dict[str, Any] = {
        "format": 1,
        "case_uuid": str(source_case.get("offline_case_uuid") or ""),
        "installation_origin_id": str(installation_origin_id or ""),
        "case": _semantic_row(source_case, skip=_CASE_COLUMNS_TO_SKIP),
        "patient": _semantic_row(patient, skip={"id"}),
        "admission": _semantic_row(admission, skip={"id", "patient_id", "merged_into_admission_id", "is_active"}),
        "children": {},
    }
    order_ordinal: dict[int, int] = {}
    event_ordinal: dict[int, int] = {}
    for table_name, relation in _CHILD_TABLES:
        if not table_exists(conn, table_name):
            continue
        owner_id = admission_id if relation == "admission_id" else case_id
        rows = conn.execute(
            f'SELECT * FROM "{table_name}" WHERE "{relation}"=? ORDER BY id', (owner_id,)
        ).fetchall()
        values: list[dict[str, Any]] = []
        for ordinal, row in enumerate(rows, start=1):
            data = _semantic_row(row, skip=_CHILD_COLUMNS_TO_SKIP)
            source_id = int(row["id"])
            if table_name == "orders":
                order_ordinal[source_id] = ordinal
            if table_name == "operblock_timeline_events":
                parent_id = row["parent_event_id"] if "parent_event_id" in row.keys() else None
                source_order_id = row["source_order_id"] if "source_order_id" in row.keys() else None
                data.pop("parent_event_id", None)
                data.pop("source_order_id", None)
                data["parent_event_ordinal"] = event_ordinal.get(int(parent_id or 0)) if parent_id else None
                data["source_order_ordinal"] = order_ordinal.get(int(source_order_id or 0)) if source_order_id else None
                event_ordinal[source_id] = ordinal
            values.append(data)
        # Orders and independent observation rows have no clinical ordering
        # semantics.  Sorting makes a copy with different integer keys stable.
        payload["children"][table_name] = values if table_name == "operblock_timeline_events" else _sorted_rows(values)
    return payload


def payload_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def ensure_transfer_schema(cursor: sqlite3.Cursor) -> None:
    """Install only receipt/destination objects required by archive transfer."""
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS opblock_offline_transfer_destination (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            destination_uuid TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS opblock_offline_case_map (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            offline_case_uuid TEXT NOT NULL UNIQUE,
            offline_session_id TEXT,
            local_operation_case_id INTEGER,
            remote_operation_case_id INTEGER,
            original_protocol_number INTEGER,
            network_protocol_number INTEGER,
            content_hash TEXT,
            source_installation_id TEXT,
            local_revision INTEGER,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now')),
            updated_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
        )
        """
    )
    present = set(columns(cursor.connection, "opblock_offline_case_map"))
    for name, definition in (
        ("source_installation_id", "TEXT"),
        ("local_revision", "INTEGER"),
    ):
        if name not in present:
            cursor.execute(f'ALTER TABLE opblock_offline_case_map ADD COLUMN "{name}" {definition}')
    # Older installations used this visible local number as a global unique
    # key.  Receipt UUIDs now provide identity, so retain only a lookup index.
    cursor.execute("DROP INDEX IF EXISTS idx_operation_cases_protocol_sequence")
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_operation_cases_protocol_sequence
        ON operation_cases(table_code, anesthesia_protocol_date, anesthesia_protocol_number)
        WHERE anesthesia_protocol_number IS NOT NULL AND anesthesia_protocol_date IS NOT NULL
        """
    )


def destination_uuid(cursor: sqlite3.Cursor) -> str:
    ensure_transfer_schema(cursor)
    row = cursor.execute(
        "SELECT destination_uuid FROM opblock_offline_transfer_destination WHERE singleton=1"
    ).fetchone()
    if row:
        return str(row[0])
    value = str(uuid.uuid4())
    cursor.execute(
        "INSERT INTO opblock_offline_transfer_destination (singleton, destination_uuid, created_at) VALUES (1, ?, ?)",
        (value, now_text()),
    )
    return value


def ensure_local_destination_binding(local_conn: sqlite3.Connection, destination_id: str) -> None:
    """Pin one local offline store to exactly one central archive identity."""
    local_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS opblock_offline_destination_binding (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            destination_uuid TEXT NOT NULL,
            bound_at TEXT NOT NULL
        )
        """
    )
    row = local_conn.execute(
        "SELECT destination_uuid FROM opblock_offline_destination_binding WHERE singleton=1"
    ).fetchone()
    if row and str(row[0]) != str(destination_id):
        raise RuntimeError("Целевая центральная база не совпадает с ранее привязанным архивом оперблока.")
    if row is None:
        local_conn.execute(
            "INSERT INTO opblock_offline_destination_binding (singleton, destination_uuid, bound_at) VALUES (1, ?, ?)",
            (str(destination_id), now_text()),
        )


def local_installation_origin_id(local_conn: sqlite3.Connection) -> str:
    """Return the durable origin of this shared local OperBlock installation.

    The offline database lives below ProgramData and can be used by several
    Windows accounts.  A user-profile client id would split one installation
    into several origins, so this UUID is stored in the local database.
    """
    local_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS opblock_offline_installation_identity (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            installation_uuid TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        )
        """
    )
    row = local_conn.execute(
        "SELECT installation_uuid FROM opblock_offline_installation_identity WHERE singleton=1"
    ).fetchone()
    if row:
        return str(row[0])
    value = str(uuid.uuid4())
    local_conn.execute(
        "INSERT INTO opblock_offline_installation_identity (singleton, installation_uuid, created_at) VALUES (1, ?, ?)",
        (value, now_text()),
    )
    return value


def read_local_installation_origin_id(local_conn: sqlite3.Connection) -> str:
    """Read an existing installation UUID without changing the local store."""
    if not table_exists(local_conn, "opblock_offline_installation_identity"):
        return ""
    row = local_conn.execute(
        "SELECT installation_uuid FROM opblock_offline_installation_identity WHERE singleton=1"
    ).fetchone()
    return str(row[0]) if row else ""
