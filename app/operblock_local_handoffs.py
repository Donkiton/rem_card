"""Durable invitations for a locally completed OperBlock case returning to RAO.

The import worker calls :func:`publish_imported_handoff` inside the same central
SQLite transaction that imports a final local case.  This module deliberately
does not open a database or contact the network: a failed invitation write must
roll back the imported case as well.
"""
from __future__ import annotations

import json
import sqlite3
import os
import uuid
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


RAO_TRANSFER_WINDOW = timedelta(minutes=30)
INVITATION_PENDING = "pending"
INVITATION_DISMISSED = "dismissed"
INVITATION_ACCEPTED = "accepted"
INVITATION_EXPIRED = "expired"


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone().replace(tzinfo=None) if value.tzinfo is not None else value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo is not None else parsed
    except ValueError:
        return None


def _is_rao(value: Any) -> bool:
    return str(value or "").strip().casefold().replace("ё", "е") in {"рао", "оитар", "реанимация"}


def _transfer_datetime(payload: dict[str, Any]) -> datetime | None:
    for key in ("transfer_datetime", "clinical_transfer_at", "anesthesia_end_at", "event_time"):
        value = _parse_datetime(payload.get(key))
        if value is not None:
            return value
    return None


def _case_payload(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("case")
    return dict(value) if isinstance(value, dict) else {}


def _timeline_transfer_datetime(payload: dict[str, Any]) -> datetime | None:
    children = payload.get("children")
    events = children.get("operblock_timeline_events") if isinstance(children, dict) else []
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        if event.get("deleted_at") not in (None, ""):
            continue
        if str(event.get("status") or "").strip().casefold() in {"cancelled", "canceled", "deleted"}:
            continue
        raw = event.get("payload_json")
        try:
            stage = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        except (TypeError, ValueError):
            stage = {}
        if str(stage.get("stage_kind") or "") != "anesthesia_end":
            continue
        event_dt = _parse_datetime(event.get("event_time"))
        if event_dt is not None:
            candidates.append((event_dt, stage))
    return max(candidates, key=lambda item: item[0])[0] if candidates else None


def ensure_operblock_rao_invitation_schema(cursor: sqlite3.Cursor) -> None:
    """Create only the additive coordination table needed by the local importer."""
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS operblock_rao_handoff_invitations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            remote_operation_case_id INTEGER NOT NULL,
            remote_patient_id INTEGER NOT NULL,
            remote_admission_id INTEGER NOT NULL,
            source_rao_admission_id INTEGER,
            transfer_datetime TEXT NOT NULL,
            transfer_department TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',
            notification_seen_at TEXT,
            notification_snoozed_until TEXT,
            accepted_admission_id INTEGER,
            accepted_bed_number INTEGER,
            accepted_at TEXT,
            accepted_by TEXT,
            dismissed_at TEXT,
            dismissed_by TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 0,
            UNIQUE(remote_operation_case_id),
            CHECK (status IN ('pending', 'dismissed', 'accepted', 'expired'))
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_operblock_rao_handoff_invitation_pending
        ON operblock_rao_handoff_invitations(status, notification_snoozed_until, transfer_datetime)
        """
    )


def publish_imported_handoff(
    cursor: sqlite3.Cursor,
    *,
    local_case: dict[str, Any],
    remote_case_id: int,
    remote_patient_id: int,
    remote_admission_id: int,
    source_payload: dict[str, Any],
) -> None:
    """Publish one RAO invitation only for a fresh, final transfer to RAO.

    The unique remote case key makes repeated exports and receipt reconciliation
    idempotent.  A stale final case remains imported and archived, but never
    creates a clinical admission workflow.
    """
    payload = dict(source_payload or {})
    case_payload = _case_payload(payload)
    department = (
        payload.get("transfer_department")
        or case_payload.get("transfer_department")
        or local_case.get("transfer_department")
    )
    transferred_at = _transfer_datetime(payload) or _timeline_transfer_datetime(payload)
    if not _is_rao(department) or transferred_at is None:
        return
    now = datetime.now().replace(microsecond=0)
    if now - transferred_at > RAO_TRANSFER_WINDOW or transferred_at > now + timedelta(minutes=5):
        return

    ensure_operblock_rao_invitation_schema(cursor)
    now_text = now.isoformat(timespec="seconds")
    transfer_text = transferred_at.isoformat(timespec="seconds")
    source_rao_admission_id = (
        payload.get("source_rao_admission_id")
        or case_payload.get("source_rao_admission_id")
        or local_case.get("source_rao_admission_id")
    )
    try:
        source_rao_admission_id = int(source_rao_admission_id) if source_rao_admission_id not in (None, "") else None
    except (TypeError, ValueError):
        source_rao_admission_id = None
    presentation_payload = dict(payload)
    patient = payload.get("patient")
    if isinstance(patient, dict):
        for key in ("full_name", "birth_date"):
            presentation_payload.setdefault(key, patient.get(key))
    presentation_payload.update(
        transfer_datetime=transfer_text,
        transfer_department="РАО",
        source_rao_admission_id=source_rao_admission_id,
    )
    cursor.execute(
        """
        INSERT INTO operblock_rao_handoff_invitations (
            remote_operation_case_id, remote_patient_id, remote_admission_id,
            source_rao_admission_id, transfer_datetime, transfer_department,
            payload_json, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        ON CONFLICT(remote_operation_case_id) DO NOTHING
        """,
        (
            int(remote_case_id), int(remote_patient_id), int(remote_admission_id),
            source_rao_admission_id, transfer_text, "РАО",
            json.dumps(presentation_payload, ensure_ascii=False, sort_keys=True, default=str),
            now_text, now_text,
        ),
    )


def _ensure_remote_claim_columns(cursor: sqlite3.Cursor) -> None:
    columns = {str(row[1]) for row in cursor.execute("PRAGMA table_info(operblock_handoffs)")}
    for name, definition in (
        ("local_claim_uuid", "TEXT"),
        ("local_claim_workstation", "TEXT"),
        ("local_claimed_at", "TEXT"),
    ):
        if name not in columns:
            cursor.execute(f"ALTER TABLE operblock_handoffs ADD COLUMN {name} {definition}")
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_operblock_handoffs_local_claim_uuid "
        "ON operblock_handoffs(local_claim_uuid) WHERE local_claim_uuid IS NOT NULL"
    )


def list_remote_rao_handoffs(central_db, *, workstation_id: str | None = None) -> list[dict[str, Any]]:
    """Read the central queue only when an OpBlock user explicitly opens it."""
    workstation = str(workstation_id or os.environ.get("COMPUTERNAME") or "operblock")
    # Older central databases receive the claim columns at the first explicit
    # queue read.  This is an additive, small transaction, never startup I/O.
    central_db.run_write_operation(_ensure_remote_claim_columns, source="operblock_local_rao_queue_schema")
    rows = central_db.fetch_all_remcard(
        """
        SELECT h.* FROM operblock_handoffs h
        JOIN admissions a ON a.id = h.source_admission_id
        WHERE (h.status = 'waiting' OR (h.status = 'accepted' AND h.local_claim_workstation = ?))
          AND a.merged_into_admission_id IS NULL
        ORDER BY DATETIME(h.dispatched_at), h.id
        """, (workstation,)
    )
    result = []
    for row in rows or []:
        data = dict(row)
        data["patient_snapshot"] = _json_dict(data.pop("patient_snapshot_json", None))
        data["vitals_snapshot"] = _json_dict(data.pop("vitals_snapshot_json", None))
        result.append(data)
    return result


def local_rao_claim_workstation_id(db_path: str) -> str:
    """Return this local installation's durable queue-claim identity.

    The queue must not use a hostname: cloned workstations can retain it, and
    several Windows accounts share one ProgramData store.  The identity table
    is created only in an existing local database, before a central claim.
    """
    local_path = Path(str(db_path))
    if not local_path.is_file():
        raise RuntimeError("Локальная база оперблока недоступна для идентификации рабочего места.")
    from rem_card.app.operblock_offline_transfer import local_installation_origin_id

    with closing(sqlite3.connect(str(local_path), timeout=5.0)) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            origin = local_installation_origin_id(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return str(origin)


def _ensure_local_claim_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS operblock_local_rao_claim_snapshots (
            claim_uuid TEXT PRIMARY KEY,
            remote_handoff_id INTEGER NOT NULL UNIQUE,
            remote_source_admission_id INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            claimed_at TEXT NOT NULL,
            local_operation_case_id INTEGER,
            consumed_at TEXT
        )
        """
    )


def read_local_rao_claim_queue(db_path: str) -> tuple[set[str], list[dict[str, Any]]]:
    """Return consumed claim tokens and durable unconsumed queue snapshots.

    This is deliberately a read-only helper for the UI worker.  A missing or
    pre-schema local database is an empty queue, never a reason to create a
    database while the user merely opens the central queue.
    """
    try:
        uri = Path(str(db_path)).absolute().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=1.0)) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'operblock_local_rao_claim_snapshots'"
            ).fetchone()
            if not exists:
                return set(), []
            consumed = {
                str(row[0])
                for row in conn.execute(
                    "SELECT claim_uuid FROM operblock_local_rao_claim_snapshots "
                    "WHERE local_operation_case_id IS NOT NULL"
                )
            }
            snapshots = conn.execute(
                "SELECT claim_uuid, payload_json "
                "FROM operblock_local_rao_claim_snapshots "
                "WHERE local_operation_case_id IS NULL"
            ).fetchall()
    except (OSError, sqlite3.Error, ValueError):
        return set(), []

    rows: list[dict[str, Any]] = []
    for token, raw_payload in snapshots:
        payload = _json_dict(raw_payload)
        if payload:
            rows.append({
                **payload,
                "local_claim_uuid": str(token),
                "_local_only_claim": True,
            })
    return consumed, rows


def claim_remote_rao_handoff(
    central_db,
    *,
    local_db_path: str,
    handoff_id: int,
    workstation_id: str | None = None,
    claim_uuid: str | None = None,
) -> dict[str, Any]:
    """Atomically reserve central work, then persist a retryable local snapshot.

    If the process dies after the central COMMIT, the same workstation retries
    with the durable claim UUID and receives the same source record; a second
    workstation can never take the waiting handoff.
    """
    token = str(claim_uuid or uuid.uuid4())
    workstation = str(workstation_id or local_rao_claim_workstation_id(local_db_path))
    now = datetime.now().replace(microsecond=0).isoformat(timespec="seconds")

    def claim(cursor: sqlite3.Cursor):
        _ensure_remote_claim_columns(cursor)
        row = cursor.execute("SELECT * FROM operblock_handoffs WHERE id = ? LIMIT 1", (int(handoff_id),)).fetchone()
        if not row:
            raise RuntimeError("Пациент больше не ожидает операционную.")
        item = dict(row)
        existing_token = str(item.get("local_claim_uuid") or "")
        if existing_token:
            if existing_token != token or str(item.get("local_claim_workstation") or "") != workstation:
                raise RuntimeError("Пациент уже выбран другим рабочим местом.")
        else:
            cursor.execute(
                """
                UPDATE operblock_handoffs
                SET local_claim_uuid = ?, local_claim_workstation = ?, local_claimed_at = ?,
                    status = 'accepted', last_modified_by = ?, revision = COALESCE(revision, 0) + 1
                WHERE id = ? AND status = 'waiting' AND local_claim_uuid IS NULL
                """,
                (token, workstation, now, workstation, int(handoff_id)),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Пациент уже выбран другим рабочим местом.")
            item.update(local_claim_uuid=token, local_claim_workstation=workstation, local_claimed_at=now, status="accepted")
        return item

    remote = dict(central_db.run_write_operation(claim, source="operblock_local_rao_claim"))
    patient = _json_dict(remote.get("patient_snapshot_json"))
    vitals = _json_dict(remote.get("vitals_snapshot_json"))
    payload = {**remote, "patient_snapshot": patient, "vitals_snapshot": vitals}
    conn = sqlite3.connect(str(local_db_path), timeout=5.0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_local_claim_schema(conn)
        conn.execute(
            """
            INSERT INTO operblock_local_rao_claim_snapshots (
                claim_uuid, remote_handoff_id, remote_source_admission_id, payload_json, claimed_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(claim_uuid) DO NOTHING
            """,
            (token, int(handoff_id), int(remote["source_admission_id"]),
             json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str), now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"claim_uuid": token, **payload}


def attach_claimed_local_case(local_db_manager, *, claim_uuid: str, operation_case_id: int) -> None:
    """Persist the original central RAO admission on the newly created local case."""
    def operation(cursor: sqlite3.Cursor):
        row = cursor.execute(
            "SELECT remote_source_admission_id FROM operblock_local_rao_claim_snapshots WHERE claim_uuid = ?",
            (str(claim_uuid),),
        ).fetchone()
        if not row:
            raise RuntimeError("Локальный снимок очереди не найден; случай не связан с исходной картой РАО.")
        cursor.execute(
            """
            UPDATE operation_cases
            SET source_rao_admission_id = COALESCE(source_rao_admission_id, ?),
                last_modified_by = 'operblock', revision = COALESCE(revision, 0) + 1
            WHERE id = ?
            """,
            (int(row["remote_source_admission_id"]), int(operation_case_id)),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Локальный операционный случай не найден.")
        cursor.execute(
            "UPDATE operblock_local_rao_claim_snapshots SET local_operation_case_id = ?, consumed_at = ? WHERE claim_uuid = ?",
            (int(operation_case_id), datetime.now().replace(microsecond=0).isoformat(timespec="seconds"), str(claim_uuid)),
        )
    local_db_manager.run_write_operation(operation, source="operblock_local_rao_claim_attach")
