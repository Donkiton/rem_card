"""Bounded incremental updates for a private SQLite replica candidate.

``change_log`` is only an invalidation index.  The collector therefore reads
the current source rows from the same read transaction as its watermark; it
never treats a log row as replayable data.
"""

from __future__ import annotations

import hashlib
import pickle
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from typing import Any


PROTOCOL_VERSION = 1
_STATE_KEYS = ("db_cycle", "change_cursor", "schema_revision")
_ROWID_SENTINEL = "__replica_delta_rowid__"
_CHANGE_COLUMNS = (
    "id",
    "entity_name",
    "entity_id",
    "admission_id",
    "action",
    "changed_at",
    "changed_by",
    "version",
)
_KNOWN_ACTIONS = frozenset(("insert", "update", "delete"))
# These tables publish a physical rowid as entity_id even though rowid is not
# their logical identity (and may change after VACUUM).  Their journal events
# are useful invalidations only; bounded full reconciliation uses their real PK.
_FORCE_RECONCILE = frozenset(("vital_settings", "operating_tables"))
# Inspected history and changed rows are separate budgets. A large, mostly
# unchanged audit table must not force a network backup on every clinical edit.
# These bounds include BOTH source and local rows/bytes; candidates remain
# unpublished until the entire consistent reconciliation has completed.
MAX_RECONCILIATION_SCAN_ROWS = 250_000
MAX_RECONCILIATION_SCAN_BYTES = 32 * 1024 * 1024


class ReplicaDeltaError(RuntimeError):
    """The package or destination does not satisfy the delta contract."""


class ReplicaDeltaCancelled(ReplicaDeltaError):
    """Collection was cancelled by the caller between bounded reads."""


def _quote(identifier: str) -> str:
    return '"' + str(identifier).replace('"', '""') + '"'


def _cancel(check_cancel: Callable[[], Any] | None) -> None:
    if check_cancel is not None and check_cancel():
        raise ReplicaDeltaCancelled("Сбор дельты отменён")


def _scalar(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def _table_names(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' "
            "ORDER BY name"
        )
    ]


def _master_contract(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\' "
            "AND type IN ('table', 'index', 'trigger', 'view') "
            "ORDER BY type, name"
        )
    ]


def _schema_fingerprint(contract: Iterable[Sequence[Any]]) -> str:
    digest = hashlib.sha256()
    for row in contract:
        digest.update(pickle.dumps(tuple(row), protocol=4))
    return digest.hexdigest()


def _read_state(conn: sqlite3.Connection) -> dict[str, Any] | None:
    tables = set(_table_names(conn))
    if "meta" not in tables or "change_log" not in tables:
        return None
    meta_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info('meta')")}
    change_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info('change_log')")
    }
    if not {"key", "value"}.issubset(meta_columns):
        return None
    if not set(_CHANGE_COLUMNS).issubset(change_columns):
        return None
    cycle = _scalar(
        conn,
        "SELECT value FROM meta WHERE key='db_cycle_started_at'",
    )
    revision = _scalar(
        conn,
        "SELECT value FROM meta WHERE key='unified_schema_fastpath_rev'",
    )
    cursor = _scalar(conn, "SELECT COALESCE(MAX(id), 0) FROM change_log")
    if cycle is None or str(cycle) == "" or revision is None or str(revision) == "":
        return None
    try:
        normalized_cursor = int(cursor or 0)
    except (TypeError, ValueError):
        return None
    return {
        "db_cycle": str(cycle),
        "change_cursor": normalized_cursor,
        "schema_revision": str(revision),
    }


def _full_required(
    reason: str,
    state: dict[str, Any] | None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": "full_required",
        "reason": reason,
        "state": dict(state or {}),
        "metrics": dict(metrics or {}),
        "data": {},
    }


def _table_info(conn: sqlite3.Connection, table: str) -> dict[str, Any]:
    sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    create_sql = str(sql_row[0] or "") if sql_row else ""
    if "CREATE VIRTUAL TABLE" in create_sql.upper():
        raise ReplicaDeltaError(f"virtual_table:{table}")
    rows = [tuple(row) for row in conn.execute(f"PRAGMA table_xinfo({_quote(table)})")]
    if not rows:
        raise ReplicaDeltaError(f"missing_table_info:{table}")
    if any(int(row[6] or 0) not in (0,) for row in rows):
        raise ReplicaDeltaError(f"generated_or_hidden_columns:{table}")
    columns = [str(row[1]) for row in rows]
    pk_columns = [
        str(row[1])
        for row in sorted(rows, key=lambda value: int(value[5] or 0))
        if int(row[5] or 0) > 0
    ]
    without_rowid = "WITHOUT ROWID" in create_sql.upper()
    if pk_columns:
        key_kind = "pk"
        key_columns = pk_columns
    elif not without_rowid and not {"rowid", "_rowid_", "oid"}.intersection(
        column.casefold() for column in columns
    ):
        key_kind = "rowid"
        key_columns = [_ROWID_SENTINEL]
    else:
        raise ReplicaDeltaError(f"table_without_stable_key:{table}")
    return {
        "columns": columns,
        "key_kind": key_kind,
        "key_columns": key_columns,
        "without_rowid": without_rowid,
    }


def _storage_columns(info: dict[str, Any]) -> list[str]:
    columns = list(info["columns"])
    if info["key_kind"] == "rowid":
        return [_ROWID_SENTINEL, *columns]
    return columns


def _select_list(info: dict[str, Any]) -> str:
    columns = [_quote(column) for column in info["columns"]]
    if info["key_kind"] == "rowid":
        columns.insert(0, f"_rowid_ AS {_quote(_ROWID_SENTINEL)}")
    return ", ".join(columns)


def _key_indexes(info: dict[str, Any]) -> tuple[int, ...]:
    columns = _storage_columns(info)
    return tuple(columns.index(column) for column in info["key_columns"])


def _row_key(row: Sequence[Any], info: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(row[index] for index in _key_indexes(info))


def _tracked_tables(
    contract: Sequence[Sequence[Any]], table_names: set[str]
) -> tuple[set[str], bool]:
    trigger_sql = {
        str(row[1]): str(row[3] or "")
        for row in contract
        if row[0] == "trigger"
    }
    tracked: set[str] = set()
    partial = False
    for table in table_names - {"change_log"}:
        names = [f"trg_{table}_version_{suffix}" for suffix in ("ins", "upd", "del")]
        present = [name in trigger_sql for name in names]
        if all(present):
            if all("change_log" in trigger_sql[name].casefold() for name in names):
                tracked.add(table)
            else:
                partial = True
        elif any(present):
            partial = True
    return tracked, partial


def _event_selector(table: str, info: dict[str, Any]) -> str | None:
    if table == "vital_settings":
        return _ROWID_SENTINEL if info["key_kind"] == "pk" else info["key_columns"][0]
    key_columns = list(info["key_columns"])
    if len(key_columns) == 1:
        return key_columns[0]
    return None


def _rows_for_entity_ids(
    conn: sqlite3.Connection,
    table: str,
    info: dict[str, Any],
    selector: str,
    entity_ids: Sequence[Any],
    check_cancel: Callable[[], Any] | None,
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    selector_sql = "_rowid_" if selector == _ROWID_SENTINEL else _quote(selector)
    for offset in range(0, len(entity_ids), 400):
        _cancel(check_cancel)
        batch = entity_ids[offset : offset + 400]
        placeholders = ",".join("?" for _ in batch)
        sql = (
            f"SELECT {_select_list(info)} FROM {_quote(table)} "
            f"WHERE {selector_sql} IN ({placeholders})"
        )
        rows.extend(tuple(row) for row in conn.execute(sql, tuple(batch)))
    return rows


def _all_rows(
    conn: sqlite3.Connection,
    table: str,
    info: dict[str, Any],
    check_cancel: Callable[[], Any] | None,
) -> list[tuple[Any, ...]]:
    result: list[tuple[Any, ...]] = []
    cursor = conn.execute(f"SELECT {_select_list(info)} FROM {_quote(table)}")
    while True:
        _cancel(check_cancel)
        batch = cursor.fetchmany(400)
        if not batch:
            return result
        result.extend(tuple(row) for row in batch)


def _row_size_expression(info: dict[str, Any]) -> str:
    # Account for serialization framing as well as SQLite value bytes.  The
    # final serialized package is measured exactly later; this preflight is a
    # conservative guard against transferring a known oversized BLOB/TEXT row.
    parts = [
        f"COALESCE(length(CAST({_quote(column)} AS BLOB)), 0) + 8"
        for column in info["columns"]
    ]
    return " + ".join(parts) if parts else "1"


def _estimate_source_bytes(
    conn: sqlite3.Connection,
    table: str,
    info: dict[str, Any],
    *,
    selector: str | None = None,
    entity_ids: Sequence[Any] = (),
    check_cancel: Callable[[], Any] | None = None,
) -> int:
    row_size = _row_size_expression(info)
    if selector is None:
        _cancel(check_cancel)
        return int(
            _scalar(
                conn,
                f"SELECT COALESCE(SUM({row_size}), 0) FROM {_quote(table)}",
            )
            or 0
        )
    selector_sql = "_rowid_" if selector == _ROWID_SENTINEL else _quote(selector)
    total = 0
    for offset in range(0, len(entity_ids), 400):
        _cancel(check_cancel)
        batch = entity_ids[offset : offset + 400]
        placeholders = ",".join("?" for _ in batch)
        total += int(
            _scalar(
                conn,
                f"SELECT COALESCE(SUM({row_size}), 0) FROM {_quote(table)} "
                f"WHERE {selector_sql} IN ({placeholders})",
                tuple(batch),
            )
            or 0
        )
    return total


def _estimated_size(value: Any) -> int:
    return len(pickle.dumps(value, protocol=4))


def _database_bytes(conn: sqlite3.Connection) -> int:
    page_count = int(_scalar(conn, "PRAGMA page_count") or 0)
    page_size = int(_scalar(conn, "PRAGMA page_size") or 0)
    return max(1, page_count * page_size)


def _validate_collection_baseline(
    source_conn: sqlite3.Connection,
    local_conn: sqlite3.Connection,
    check_cancel: Callable[[], Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    _cancel(check_cancel)
    source_state = _read_state(source_conn)
    local_state = _read_state(local_conn)
    if source_state is None or local_state is None:
        return None, _full_required("protocol_unavailable", source_state)
    source_contract = _master_contract(source_conn)
    if source_contract != _master_contract(local_conn):
        return None, _full_required("schema_mismatch", source_state)
    if any(
        row[0] == "table" and "CREATE VIRTUAL TABLE" in str(row[3] or "").upper()
        for row in source_contract
    ):
        return None, _full_required("unsupported_virtual_table", source_state)
    if source_state["db_cycle"] != local_state["db_cycle"]:
        return None, _full_required("database_cycle_changed", source_state)
    if source_state["schema_revision"] != local_state["schema_revision"]:
        return None, _full_required("schema_revision_changed", source_state)
    local_cursor = int(local_state["change_cursor"])
    watermark = int(source_state["change_cursor"])
    if local_cursor > watermark:
        return None, _full_required("local_cursor_ahead", source_state)
    return {
        "source_state": source_state,
        "local_state": local_state,
        "source_contract": source_contract,
        "schema_fingerprint": _schema_fingerprint(source_contract),
        "local_cursor": local_cursor,
        "watermark": watermark,
    }, None


def _read_log_prefix(
    source_conn: sqlite3.Connection,
    local_conn: sqlite3.Connection,
    context: dict[str, Any],
) -> tuple[dict[str, int] | None, dict[str, Any] | None]:
    state = context["source_state"]
    cursor = context["local_cursor"]
    watermark = context["watermark"]
    source_min = int(_scalar(source_conn, "SELECT COALESCE(MIN(id), 0) FROM change_log") or 0)
    local_min = int(_scalar(local_conn, "SELECT COALESCE(MIN(id), 0) FROM change_log") or 0)
    if source_min and local_min and local_min > source_min:
        return None, _full_required("change_log_prefix_diverged", state)
    if watermark and source_min > cursor + 1:
        return None, _full_required("change_log_gap", state)
    source_count = int(
        _scalar(source_conn, "SELECT COUNT(*) FROM change_log WHERE id<=?", (cursor,)) or 0
    )
    local_count = int(
        _scalar(local_conn, "SELECT COUNT(*) FROM change_log WHERE id<=?", (cursor,)) or 0
    )
    retained_extra = 0
    if source_min > local_min:
        retained_extra = int(
            _scalar(local_conn, "SELECT COUNT(*) FROM change_log WHERE id<?", (source_min,))
            or 0
        )
    if local_count != source_count + retained_extra:
        return None, _full_required("change_log_prefix_diverged", state)
    return {"source_min": source_min, "local_min": local_min}, None


def _read_events(
    source_conn: sqlite3.Connection,
    context: dict[str, Any],
    max_events: int,
    max_bytes: int,
) -> tuple[list[tuple[Any, ...]] | None, dict[str, Any] | None, int]:
    cursor = context["local_cursor"]
    watermark = context["watermark"]
    state = context["source_state"]
    count = int(
        _scalar(
            source_conn,
            "SELECT COUNT(*) FROM change_log WHERE id>? AND id<=?",
            (cursor, watermark),
        )
        or 0
    )
    if count > max_events:
        return None, _full_required(
            "event_limit", state, {"event_count": count, "max_events": max_events}
        ), 0
    log_info = _table_info(source_conn, "change_log")
    row_size = _row_size_expression(log_info)
    estimated = int(
        _scalar(
            source_conn,
            f"SELECT COALESCE(SUM({row_size}), 0) FROM change_log WHERE id>? AND id<=?",
            (cursor, watermark),
        )
        or 0
    )
    if estimated > max_bytes:
        return None, _full_required("byte_limit", state), estimated
    rows = [
        tuple(row)
        for row in source_conn.execute(
            "SELECT id, entity_name, entity_id, admission_id, action, "
            "changed_at, changed_by, version FROM change_log "
            "WHERE id>? AND id<=? ORDER BY id",
            (cursor, watermark),
        )
    ]
    if len(rows) != count:
        return None, _full_required("change_log_unstable", state), estimated
    if rows and int(rows[0][0]) > cursor + 1:
        return None, _full_required("change_log_gap", state), estimated
    return rows, None, estimated


def _classify_events(
    context: dict[str, Any],
    table_names: set[str],
    event_rows: Sequence[Sequence[Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    tracked, partial = _tracked_tables(context["source_contract"], table_names)
    journalled_reconciled = tracked.intersection(_FORCE_RECONCILE)
    tracked.difference_update(_FORCE_RECONCILE)
    if partial:
        return None, _full_required("unsupported_change_protocol", context["source_state"])
    event_ids: dict[str, set[Any]] = {}
    previous_id = context["local_cursor"]
    for event in event_rows:
        event_id, entity_name, entity_id, _admission, action, *_tail = event
        table = str(entity_name or "")
        invalid = int(event_id) != previous_id + 1 or str(action) not in _KNOWN_ACTIONS
        invalid = invalid or (table not in tracked and table not in journalled_reconciled)
        invalid = invalid or (table in tracked and entity_id is None)
        if invalid:
            return None, _full_required("unsupported_change_protocol", context["source_state"])
        previous_id = int(event_id)
        if table in tracked:
            event_ids.setdefault(table, set()).add(entity_id)
    return {"tracked": tracked, "event_ids": event_ids}, None


def _prepare_collection(
    source_conn: sqlite3.Connection,
    local_conn: sqlite3.Connection,
    max_events: int,
    max_bytes: int,
    check_cancel: Callable[[], Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    context, error = _validate_collection_baseline(source_conn, local_conn, check_cancel)
    if error:
        return None, error
    prefix, error = _read_log_prefix(source_conn, local_conn, context)
    if error:
        return None, error
    events, error, event_bytes = _read_events(
        source_conn, context, max_events, max_bytes
    )
    if error:
        return None, error
    table_names = set(_table_names(source_conn))
    classification, error = _classify_events(context, table_names, events)
    if error:
        return None, error
    context.update(prefix)
    context.update(classification)
    context["event_rows"] = events
    context["event_estimated_bytes"] = event_bytes
    context["table_names"] = table_names
    return context, None


def _tracked_table_change(
    source_conn: sqlite3.Connection,
    table: str,
    info: dict[str, Any],
    ids: Sequence[Any],
    byte_remaining: int,
    check_cancel: Callable[[], Any] | None,
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]], int]:
    selector = _event_selector(table, info)
    if selector is None or len(info["key_columns"]) != 1:
        raise ReplicaDeltaError("unsupported_change_key")
    estimated = _estimate_source_bytes(
        source_conn,
        table,
        info,
        selector=selector,
        entity_ids=ids,
        check_cancel=check_cancel,
    )
    if estimated > byte_remaining:
        raise ReplicaDeltaError("payload_byte_limit")
    rows = _rows_for_entity_ids(source_conn, table, info, selector, ids, check_cancel)
    columns = _storage_columns(info)
    found = {
        row[0] if selector == _ROWID_SENTINEL else row[columns.index(selector)]
        for row in rows
    }
    return rows, [(entity_id,) for entity_id in ids if entity_id not in found], estimated


def _reconciled_table_change(
    source_conn: sqlite3.Connection,
    local_conn: sqlite3.Connection,
    table: str,
    info: dict[str, Any],
    max_rows: int,
    scan_remaining: int,
    byte_remaining: int,
    check_cancel: Callable[[], Any] | None,
    scan_byte_remaining: int,
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]], int, int, int]:
    source_count = int(_scalar(source_conn, f"SELECT COUNT(*) FROM {_quote(table)}") or 0)
    local_count = int(_scalar(local_conn, f"SELECT COUNT(*) FROM {_quote(table)}") or 0)
    scanned = source_count + local_count
    if scanned > scan_remaining:
        raise ReplicaDeltaError(f"reconciliation_scan_limit:{table}:{source_count}:{local_count}")
    if not scanned:
        # The counts come from the same source/local snapshots as collection.
        # Empty tables need neither two byte scans nor two ordered row scans.
        return [], [], 0, 0, 0
    source_bytes = _estimate_source_bytes(
        source_conn, table, info, check_cancel=check_cancel
    )
    if source_bytes > scan_byte_remaining:
        raise ReplicaDeltaError(f"reconciliation_byte_limit:{table}:{source_count}:{local_count}")
    local_bytes = _estimate_source_bytes(local_conn, table, info, check_cancel=check_cancel)
    if source_bytes + local_bytes > scan_byte_remaining:
        raise ReplicaDeltaError(f"reconciliation_byte_limit:{table}:{source_count}:{local_count}")
    upserts, deletes, estimated = _stream_table_difference(
        source_conn, local_conn, table, info, max_rows, byte_remaining, check_cancel
    )
    return upserts, deletes, scanned, estimated, source_bytes + local_bytes


def _ordered_table_rows(conn, table, info, check_cancel):
    """Keep memory bounded while comparing every historical row, not just tails."""
    order = ", ".join(f"{_quote(column)} COLLATE BINARY" for column in info["key_columns"])
    cursor = conn.execute(f"SELECT {_select_list(info)} FROM {_quote(table)} ORDER BY {order}")
    previous_key = None
    try:
        while True:
            _cancel(check_cancel)
            batch = cursor.fetchmany(128)
            if not batch:
                break
            for row in batch:
                row = tuple(row)
                key = _row_key(row, info)
                if any(value is None for value in key):
                    # NULL does not participate in SQLite UNIQUE conflicts:
                    # INSERT OR REPLACE could otherwise duplicate this row.
                    raise ReplicaDeltaError("nullable_replica_key")
                if key == previous_key:
                    raise ReplicaDeltaError("non_unique_replica_key")
                previous_key = key
                yield row
    finally:
        close = getattr(cursor, "close", None)
        if callable(close):
            close()


def _sqlite_key_order(key):
    """SQLite BINARY ordering: NULL, number, text, blob, including mixed keys."""
    result = []
    for value in key:
        if value is None:
            result.append((0, 0))
        elif isinstance(value, (int, float)):
            result.append((1, value))
        elif isinstance(value, str):
            result.append((2, value))
        elif isinstance(value, bytes):
            result.append((3, value))
        else:
            raise ReplicaDeltaError("unsupported_change_key")
    return tuple(result)


def _stream_table_difference(source_conn, local_conn, table, info, max_rows, byte_remaining, check_cancel):
    source_rows = _ordered_table_rows(source_conn, table, info, check_cancel)
    local_rows = _ordered_table_rows(local_conn, table, info, check_cancel)
    upserts, deletes = [], []
    estimated = 0
    try:
        source = next(source_rows, None)
        local = next(local_rows, None)
        while source is not None or local is not None:
            _cancel(check_cancel)
            source_key = _sqlite_key_order(_row_key(source, info)) if source is not None else None
            local_key = _sqlite_key_order(_row_key(local, info)) if local is not None else None
            if source is not None and (local is None or source_key < local_key):
                upserts.append(source)
                estimated += _estimated_size(source)
                source = next(source_rows, None)
            elif local is not None and (source is None or local_key < source_key):
                key = _row_key(local, info)
                deletes.append(key)
                estimated += _estimated_size(key)
                local = next(local_rows, None)
            else:
                if source != local:
                    upserts.append(source)
                    estimated += _estimated_size(source)
                source = next(source_rows, None)
                local = next(local_rows, None)
            if len(upserts) + len(deletes) > max_rows:
                raise ReplicaDeltaError("row_limit")
            if estimated > byte_remaining:
                raise ReplicaDeltaError("payload_byte_limit")
        return upserts, deletes, estimated
    finally:
        source_rows.close()
        local_rows.close()


def _table_error_result(exc: ReplicaDeltaError, state: dict[str, Any]) -> dict[str, Any]:
    detail = str(exc)
    if detail.startswith(("reconciliation_scan_limit:", "reconciliation_byte_limit:")):
        reason, description = detail.split(":", 1)
        table, source_rows, local_rows = description.rsplit(":", 2)
        return _full_required(
            reason,
            state,
            {"table": table, "source_rows": int(source_rows), "local_rows": int(local_rows)},
        )
    reason = detail if detail in {
        "unsupported_change_key", "reconciliation_scan_limit", "non_unique_replica_key",
        "payload_byte_limit", "row_limit",
    } else "unsupported_table_shape"
    return _full_required("byte_limit" if reason == "payload_byte_limit" else reason, state)


def _collect_table_changes(
    source_conn: sqlite3.Connection,
    local_conn: sqlite3.Connection,
    context: dict[str, Any],
    max_rows: int,
    max_bytes: int,
    check_cancel: Callable[[], Any] | None,
    max_scan_rows: int,
    max_scan_bytes: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, int, int]:
    changes: dict[str, Any] = {}
    operations = len(context["event_rows"])
    scanned = 0
    scanned_bytes = 0
    estimated_bytes = int(context["event_estimated_bytes"])
    if operations > max_rows:
        # Journal rows are part of the eventual payload budget. This lower
        # bound already proves full is required, before scanning any history.
        return None, _full_required(
            "row_limit", context["source_state"],
            {"row_count": operations, "max_rows": max_rows},
        ), operations, scanned
    for table in sorted(context["table_names"] - {"change_log"}):
        _cancel(check_cancel)
        try:
            info = _table_info(source_conn, table)
            if table in context["tracked"]:
                ids = sorted(
                    context["event_ids"].get(table, ()),
                    key=lambda item: (str(type(item)), repr(item)),
                )
                if not ids:
                    continue
                upserts, deletes, estimated_now = _tracked_table_change(
                    source_conn, table, info, ids, max_bytes - estimated_bytes,
                    check_cancel,
                )
                scanned_now = 0
                scanned_bytes_now = 0
            else:
                upserts, deletes, scanned_now, estimated_now, scanned_bytes_now = _reconciled_table_change(
                    source_conn, local_conn, table, info, max_rows - operations,
                    max_scan_rows - scanned, max_bytes - estimated_bytes,
                    check_cancel, max_scan_bytes - scanned_bytes,
                )
        except ReplicaDeltaError as exc:
            return None, _table_error_result(exc, context["source_state"]), operations, scanned
        scanned += scanned_now
        scanned_bytes += scanned_bytes_now
        context["reconciliation_bytes_scanned"] = scanned_bytes
        estimated_bytes += estimated_now
        operations += len(upserts) + len(deletes)
        if operations > max_rows:
            return None, _full_required(
                "row_limit", context["source_state"],
                {"row_count": operations, "max_rows": max_rows},
            ), operations, scanned
        if upserts or deletes:
            changes[table] = {
                "columns": _storage_columns(info), "key_kind": info["key_kind"],
                "key_columns": list(info["key_columns"]),
                "upserts": upserts, "deletes": deletes,
            }
    return changes, None, operations, scanned


def _collect_sequence(
    source_conn: sqlite3.Connection,
    local_conn: sqlite3.Connection,
) -> tuple[list[tuple[Any, ...]] | None, int]:
    exists = source_conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
    ).fetchone()
    if not exists:
        return None, 0
    source_rows = [tuple(row) for row in source_conn.execute(
        "SELECT name, seq FROM sqlite_sequence ORDER BY name"
    )]
    local_rows = [tuple(row) for row in local_conn.execute(
        "SELECT name, seq FROM sqlite_sequence ORDER BY name"
    )]
    return (source_rows, len(source_rows) + len(local_rows)) if source_rows != local_rows else (None, 0)


def _build_delta_package(
    source_conn: sqlite3.Connection,
    context: dict[str, Any],
    table_changes: dict[str, Any],
    sequence_rows: list[tuple[Any, ...]] | None,
    row_operations: int,
    scanned_rows: int,
    max_bytes: int,
    max_fraction: float,
) -> dict[str, Any]:
    data = {
        "tables": table_changes,
        "change_log": {"columns": list(_CHANGE_COLUMNS), "events": context["event_rows"],
                       "source_min_id": context["source_min"], "local_min_id": context["local_min"]},
        "sqlite_sequence": sequence_rows,
    }
    package = {
        "protocol_version": PROTOCOL_VERSION, "status": "delta", "reason": "changes",
        "base_state": dict(context["local_state"]), "state": dict(context["source_state"]),
        "schema_fingerprint": context["schema_fingerprint"], "metrics": {}, "data": data,
    }
    db_bytes = _database_bytes(source_conn)
    metrics = {
        "event_count": len(context["event_rows"]), "row_count": row_operations,
        "table_count": len(table_changes), "reconciliation_rows_scanned": scanned_rows,
        "package_bytes": 0, "source_database_bytes": db_bytes, "payload_fraction": 0.0,
        "source_min_cursor": context["source_min"], "local_min_cursor": context["local_min"],
        "reconciliation_bytes_scanned": int(context.get("reconciliation_bytes_scanned", 0)),
    }
    package["metrics"] = metrics
    for _ in range(2):
        metrics["package_bytes"] = _estimated_size(package)
        metrics["payload_fraction"] = metrics["package_bytes"] / db_bytes
    if metrics["package_bytes"] > max_bytes:
        return _full_required("byte_limit", context["source_state"], metrics)
    if metrics["payload_fraction"] > max_fraction:
        return _full_required("fraction_limit", context["source_state"], metrics)
    unchanged = not table_changes and not context["event_rows"] and sequence_rows is None
    unchanged = unchanged and context["source_min"] == context["local_min"]
    if unchanged:
        package.update(status="unchanged", reason="unchanged", data={})
    return package


def collect_delta(
    source_conn: sqlite3.Connection,
    local_conn: sqlite3.Connection,
    *,
    max_events: int = 5000,
    max_rows: int = 5000,
    max_bytes: int = 4 * 1024 * 1024,
    max_fraction: float = 0.30,
    check_cancel: Callable[[], Any] | None = None,
    max_scan_rows: int = MAX_RECONCILIATION_SCAN_ROWS,
    max_scan_bytes: int = MAX_RECONCILIATION_SCAN_BYTES,
) -> dict[str, Any]:
    """Collect a self-contained delta from one consistent source snapshot."""

    limits = (int(max_events), int(max_rows), int(max_bytes), float(max_fraction))
    if limits[0] < 0 or limits[1] < 0 or limits[2] < 0 or not 0 <= limits[3] <= 1:
        raise ValueError("Некорректные ограничения дельты")
    if int(max_scan_rows) < 0 or int(max_scan_bytes) < 0:
        raise ValueError("Некорректные ограничения сверки реплики")

    source_owned = not source_conn.in_transaction
    local_owned = not local_conn.in_transaction
    source_query_only: int | None = None
    if source_owned:
        source_query_only = int(_scalar(source_conn, "PRAGMA query_only") or 0)
        source_conn.execute("PRAGMA query_only=ON")
        source_conn.execute("BEGIN")
    if local_owned:
        local_conn.execute("BEGIN")
    try:
        context, error = _prepare_collection(
            source_conn, local_conn, limits[0], limits[2], check_cancel
        )
        if error:
            return error
        table_changes, error, row_operations, scanned_rows = _collect_table_changes(
            source_conn, local_conn, context, limits[1], limits[2], check_cancel,
            int(max_scan_rows), int(max_scan_bytes),
        )
        if error:
            return error
        sequence_rows, sequence_operations = _collect_sequence(source_conn, local_conn)
        row_operations += sequence_operations
        if row_operations > limits[1]:
            return _full_required("row_limit", context["source_state"])
        return _build_delta_package(
            source_conn, context, table_changes, sequence_rows,
            row_operations, scanned_rows, limits[2], limits[3],
        )
    finally:
        if local_owned and local_conn.in_transaction:
            local_conn.execute("ROLLBACK")
        if source_owned and source_conn.in_transaction:
            source_conn.execute("ROLLBACK")
        if source_owned and source_query_only is not None:
            source_conn.execute(f"PRAGMA query_only={1 if source_query_only else 0}")


def _validate_table_change(
    destination_conn: sqlite3.Connection,
    table: str,
    change: dict[str, Any],
) -> dict[str, Any]:
    if table not in set(_table_names(destination_conn)) or table == "change_log":
        raise ReplicaDeltaError(f"Недопустимая таблица в дельте: {table}")
    info = _table_info(destination_conn, table)
    if list(change.get("columns") or ()) != _storage_columns(info):
        raise ReplicaDeltaError(f"Колонки таблицы {table} не совпадают со схемой")
    if str(change.get("key_kind") or "") != info["key_kind"]:
        raise ReplicaDeltaError(f"Тип ключа таблицы {table} не совпадает со схемой")
    if list(change.get("key_columns") or ()) != list(info["key_columns"]):
        raise ReplicaDeltaError(f"Ключ таблицы {table} не совпадает со схемой")
    width = len(_storage_columns(info))
    key_width = len(info["key_columns"])
    if any(len(tuple(row)) != width for row in change.get("upserts") or ()):
        raise ReplicaDeltaError(f"Некорректная строка таблицы {table}")
    if any(len(tuple(key)) != key_width for key in change.get("deletes") or ()):
        raise ReplicaDeltaError(f"Некорректный ключ таблицы {table}")
    return info


def _prepare_apply(
    destination_conn: sqlite3.Connection,
    package: dict[str, Any],
) -> dict[str, Any]:
    if destination_conn.in_transaction:
        raise ReplicaDeltaError("Для применения дельты требуется отдельная транзакция")
    contract = _master_contract(destination_conn)
    if _schema_fingerprint(contract) != str(package.get("schema_fingerprint") or ""):
        raise ReplicaDeltaError("Схема кандидата не совпадает со схемой пакета")
    state = _read_state(destination_conn)
    base_state = dict(package.get("base_state") or {})
    if state is None or any(state.get(key) != base_state.get(key) for key in _STATE_KEYS):
        raise ReplicaDeltaError("Состояние кандидата не совпадает с базой дельты")
    data = package.get("data")
    if not isinstance(data, dict):
        raise ReplicaDeltaError("В пакете отсутствуют данные дельты")
    table_changes = data.get("tables") or {}
    if not isinstance(table_changes, dict):
        raise ReplicaDeltaError("Некорректный список таблиц дельты")
    validated = {
        str(table): _validate_table_change(destination_conn, str(table), dict(change))
        for table, change in table_changes.items()
    }
    change_log = dict(data.get("change_log") or {})
    if list(change_log.get("columns") or ()) != list(_CHANGE_COLUMNS):
        raise ReplicaDeltaError("Некорректный формат change_log")
    events = [tuple(row) for row in change_log.get("events") or ()]
    if any(len(row) != len(_CHANGE_COLUMNS) for row in events):
        raise ReplicaDeltaError("Некорректная строка change_log")
    triggers = [
        (str(row[1]), str(row[3]))
        for row in contract
        if row[0] == "trigger" and row[3]
    ]
    return {
        "data": data, "table_changes": table_changes, "validated": validated,
        "change_log": change_log, "events": events, "triggers": triggers,
    }


def _write_table_change(
    conn: sqlite3.Connection,
    table: str,
    change: dict[str, Any],
    info: dict[str, Any],
) -> None:
    key_columns = list(info["key_columns"])
    where = " AND ".join(
        ("_rowid_" if column == _ROWID_SENTINEL else _quote(column)) + " IS ?"
        for column in key_columns
    )
    deletes = [tuple(key) for key in change.get("deletes") or ()]
    if deletes:
        conn.executemany(f"DELETE FROM {_quote(table)} WHERE {where}", deletes)
    columns = list(change.get("columns") or ())
    sql_columns = [
        "_rowid_" if column == _ROWID_SENTINEL else _quote(column)
        for column in columns
    ]
    upserts = [tuple(row) for row in change.get("upserts") or ()]
    if upserts:
        placeholders = ",".join("?" for _ in columns)
        conn.executemany(
            f"INSERT OR REPLACE INTO {_quote(table)} "
            f"({', '.join(sql_columns)}) VALUES ({placeholders})",
            upserts,
        )


def _write_change_log(
    conn: sqlite3.Connection,
    change_log: dict[str, Any],
    events: Sequence[Sequence[Any]],
    target_cursor: int,
) -> None:
    source_min = int(change_log.get("source_min_id") or 0)
    if source_min > 0:
        conn.execute("DELETE FROM change_log WHERE id < ?", (source_min,))
    elif target_cursor == 0:
        conn.execute("DELETE FROM change_log")
    if events:
        placeholders = ",".join("?" for _ in _CHANGE_COLUMNS)
        conn.executemany(
            f"INSERT OR REPLACE INTO change_log "
            f"({', '.join(_quote(column) for column in _CHANGE_COLUMNS)}) "
            f"VALUES ({placeholders})",
            events,
        )


def _write_sequence(conn: sqlite3.Connection, sequence_rows: Any) -> None:
    if sequence_rows is None:
        return
    rows = [tuple(row) for row in sequence_rows]
    if any(len(row) != 2 for row in rows):
        raise ReplicaDeltaError("Некорректные данные sqlite_sequence")
    conn.execute("DELETE FROM sqlite_sequence")
    if rows:
        conn.executemany("INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)", rows)


def _validate_applied_state(
    conn: sqlite3.Connection,
    expected_state: dict[str, Any],
) -> None:
    violation = conn.execute("PRAGMA foreign_key_check").fetchone()
    if violation is not None:
        raise ReplicaDeltaError(f"Нарушение внешнего ключа после дельты: {tuple(violation)}")
    state = _read_state(conn)
    if state is None or any(state.get(key) != expected_state.get(key) for key in _STATE_KEYS):
        raise ReplicaDeltaError("Итоговое состояние кандидата не достигло watermark")


def _apply_transaction(
    conn: sqlite3.Connection,
    package: dict[str, Any],
    context: dict[str, Any],
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    for name, _sql in context["triggers"]:
        conn.execute(f"DROP TRIGGER {_quote(name)}")
    for table, change in context["table_changes"].items():
        table = str(table)
        _write_table_change(conn, table, change, context["validated"][table])
    _write_change_log(
        conn, context["change_log"], context["events"],
        int(package["state"]["change_cursor"]),
    )
    _write_sequence(conn, context["data"].get("sqlite_sequence"))
    for _name, sql in context["triggers"]:
        conn.execute(sql)
    _validate_applied_state(conn, dict(package.get("state") or {}))
    conn.execute("COMMIT")


def apply_delta(destination_conn: sqlite3.Connection, package: dict[str, Any]) -> None:
    """Apply a delta atomically to a private candidate cloned from old cache."""

    if not isinstance(package, dict) or package.get("status") == "full_required":
        raise ReplicaDeltaError("Пакет требует полной реплики")
    if int(package.get("protocol_version") or 0) != PROTOCOL_VERSION:
        raise ReplicaDeltaError("Неподдерживаемая версия протокола дельты")
    if package.get("status") == "unchanged":
        return
    if package.get("status") != "delta":
        raise ReplicaDeltaError("Некорректный статус пакета дельты")
    context = _prepare_apply(destination_conn, package)
    foreign_keys = int(_scalar(destination_conn, "PRAGMA foreign_keys") or 0)
    destination_conn.execute("PRAGMA foreign_keys=OFF")
    try:
        _apply_transaction(destination_conn, package, context)
    except Exception:
        if destination_conn.in_transaction:
            destination_conn.execute("ROLLBACK")
        raise
    finally:
        destination_conn.execute(f"PRAGMA foreign_keys={1 if foreign_keys else 0}")


__all__ = [
    "PROTOCOL_VERSION",
    "ReplicaDeltaCancelled",
    "ReplicaDeltaError",
    "apply_delta",
    "collect_delta",
]
