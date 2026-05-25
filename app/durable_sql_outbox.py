import json
import logging
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from rem_card.app.sqlite_shared import configure_connection


CORRUPTION_WRITE_ERROR_MARKERS = (
    "database disk image is malformed",
    "file is not a database",
    "not a database",
    "malformed database schema",
    "database corruption",
    "database corrupt",
)

RETRYABLE_WRITE_ERROR_MARKERS = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
    "cannot operate on a closed database",
    "unable to open database file",
    "disk i/o error",
    "readonly database",
    "i/o error",
)

APPEND_ONLY_INSERT_TABLES = {
    "patient_status_events",
    "order_audit_log",
    "operations",
    "ivl_episodes",
    "transfusions",
    "clinical_events",
    "devices",
    "respiratory_support",
    "lab_data",
    "lab_orders",
    "administrations",
}


def _sql_head(sql: str) -> str:
    return (sql or "").strip().upper()


def is_write_statement(sql: str) -> bool:
    head = _sql_head(sql)
    return head.startswith("INSERT") or head.startswith("UPDATE") or head.startswith("DELETE") or head.startswith("REPLACE")


def _extract_insert_table(sql: str) -> Optional[str]:
    match = re.match(r"^\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)", sql or "", flags=re.IGNORECASE)
    return match.group(1).lower() if match else None


def classify_statement_policy(sql: str) -> str:
    head = _sql_head(sql)
    if head.startswith("UPDATE") or head.startswith("DELETE"):
        return "lww"
    if head.startswith("REPLACE"):
        return "lww"
    if head.startswith("INSERT"):
        table_name = _extract_insert_table(sql)
        if table_name in APPEND_ONLY_INSERT_TABLES:
            return "append_only"
        if "ON CONFLICT" in head:
            return "lww"
    return "unsafe"


def is_outbox_safe_statement(sql: str) -> bool:
    return classify_statement_policy(sql) != "unsafe"


def is_retryable_write_error(exc: Exception) -> bool:
    text = str(exc).lower()
    if any(marker in text for marker in CORRUPTION_WRITE_ERROR_MARKERS):
        return False
    return any(marker in text for marker in RETRYABLE_WRITE_ERROR_MARKERS)


def is_corruption_write_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in CORRUPTION_WRITE_ERROR_MARKERS)


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    return str(value)


def _normalize_params(params: Any) -> list[Any]:
    if params is None:
        return []
    if isinstance(params, (list, tuple)):
        return [_json_value(v) for v in params]
    return [_json_value(params)]


@dataclass
class SqlStatement:
    sql: str
    params: list[Any]
    policy: str


@dataclass
class OutboxOperation:
    row_id: int
    op_id: str
    source: str
    statements: list[SqlStatement]
    attempts: int


class DeferredWriteCursor:
    def __init__(self, op_id: str):
        self.op_id = op_id
        self.rowcount = 0
        self.lastrowid = None


class RecordingCursor:
    def __init__(self, cursor: sqlite3.Cursor, statement_sink: list[tuple[str, tuple]]):
        self._cursor = cursor
        self._sink = statement_sink

    def execute(self, sql: str, parameters: Any = ()):
        if is_write_statement(sql):
            self._sink.append((sql, tuple(parameters or ())))
        return self._cursor.execute(sql, parameters)

    def executemany(self, sql: str, seq_of_parameters: Iterable[Sequence[Any]]):
        batch = list(seq_of_parameters)
        if is_write_statement(sql):
            for params in batch:
                self._sink.append((sql, tuple(params or ())))
        return self._cursor.executemany(sql, batch)

    def __getattr__(self, item):
        return getattr(self._cursor, item)


class DurableSqlOutbox:
    def __init__(self, db_path: str, logger: Optional[logging.Logger] = None):
        self.db_path = db_path
        self.logger = logger or logging.getLogger(__name__)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            isolation_level=None,
            timeout=5.0,
        )
        configure_connection(conn, profile="local_outbox")
        return conn

    def _init_schema(self):
        conn = None
        try:
            conn = self._connect()
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox_ops (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    op_id TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_error TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_status_retry ON outbox_ops(status, next_retry_at, id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_created_at ON outbox_ops(created_at)")
        finally:
            if conn:
                conn.close()

    def enqueue(self, statements: Sequence[tuple[str, tuple]], source: str) -> Optional[str]:
        prepared: list[SqlStatement] = []
        for sql, params in statements:
            if not is_write_statement(sql):
                continue
            policy = classify_statement_policy(sql)
            if policy == "unsafe":
                return None
            prepared.append(SqlStatement(sql=sql, params=_normalize_params(params), policy=policy))

        if not prepared:
            return None

        op_id = str(uuid.uuid4())
        now = time.time()
        payload = json.dumps(
            [{"sql": item.sql, "params": item.params, "policy": item.policy} for item in prepared],
            ensure_ascii=False,
            separators=(",", ":"),
        )

        conn = None
        try:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO outbox_ops (op_id, source, payload_json, status, attempts, next_retry_at, created_at, updated_at, last_error)
                VALUES (?, ?, ?, 'pending', 0, 0, ?, ?, NULL)
                """,
                (op_id, source, payload, now, now),
            )
            return op_id
        except Exception as exc:
            self.logger.error("Failed to enqueue outbox operation for %s: %s", source, exc)
            return None
        finally:
            if conn:
                conn.close()

    def fetch_ready(self, limit: int = 20) -> list[OutboxOperation]:
        now = time.time()
        conn = None
        try:
            conn = self._connect()
            rows = conn.execute(
                """
                SELECT id, op_id, source, payload_json, attempts
                FROM outbox_ops
                WHERE status = 'pending' AND next_retry_at <= ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
            result: list[OutboxOperation] = []
            for row in rows:
                statements_raw = json.loads(row["payload_json"])
                statements = [
                    SqlStatement(
                        sql=item.get("sql", ""),
                        params=list(item.get("params", [])),
                        policy=item.get("policy", "unsafe"),
                    )
                    for item in statements_raw
                ]
                result.append(
                    OutboxOperation(
                        row_id=int(row["id"]),
                        op_id=row["op_id"],
                        source=row["source"],
                        statements=statements,
                        attempts=int(row["attempts"]),
                    )
                )
            return result
        finally:
            if conn:
                conn.close()

    def mark_applied(self, row_id: int):
        now = time.time()
        conn = None
        try:
            conn = self._connect()
            conn.execute(
                """
                UPDATE outbox_ops
                SET status = 'applied',
                    updated_at = ?,
                    last_error = NULL
                WHERE id = ?
                """,
                (now, row_id),
            )
        finally:
            if conn:
                conn.close()

    def mark_retry(self, row_id: int, attempts: int, error_text: str, delay_sec: float):
        now = time.time()
        next_retry = now + max(0.5, delay_sec)
        conn = None
        try:
            conn = self._connect()
            conn.execute(
                """
                UPDATE outbox_ops
                SET attempts = ?,
                    next_retry_at = ?,
                    updated_at = ?,
                    last_error = ?
                WHERE id = ?
                """,
                (attempts, next_retry, now, error_text[:2000], row_id),
            )
        finally:
            if conn:
                conn.close()

    def mark_failed(self, row_id: int, error_text: str):
        now = time.time()
        conn = None
        try:
            conn = self._connect()
            conn.execute(
                """
                UPDATE outbox_ops
                SET status = 'failed',
                    updated_at = ?,
                    last_error = ?
                WHERE id = ?
                """,
                (now, error_text[:2000], row_id),
            )
        finally:
            if conn:
                conn.close()

    def apply_replay_outcomes(
        self,
        *,
        applied_row_ids: Sequence[int] = (),
        retry_rows: Sequence[tuple[int, int, str, float]] = (),
        failed_rows: Sequence[tuple[int, str]] = (),
    ):
        if not applied_row_ids and not retry_rows and not failed_rows:
            return

        now = time.time()
        conn = None
        try:
            conn = self._connect()

            if applied_row_ids:
                conn.executemany(
                    """
                    UPDATE outbox_ops
                    SET status = 'applied',
                        updated_at = ?,
                        last_error = NULL
                    WHERE id = ?
                    """,
                    [(now, int(row_id)) for row_id in applied_row_ids],
                )

            if retry_rows:
                retry_payload = [
                    (
                        int(attempts),
                        now + max(0.5, float(delay_sec)),
                        now,
                        str(error_text)[:2000],
                        int(row_id),
                    )
                    for row_id, attempts, error_text, delay_sec in retry_rows
                ]
                conn.executemany(
                    """
                    UPDATE outbox_ops
                    SET attempts = ?,
                        next_retry_at = ?,
                        updated_at = ?,
                        last_error = ?
                    WHERE id = ?
                    """,
                    retry_payload,
                )

            if failed_rows:
                conn.executemany(
                    """
                    UPDATE outbox_ops
                    SET status = 'failed',
                        updated_at = ?,
                        last_error = ?
                    WHERE id = ?
                    """,
                    [(now, str(error_text)[:2000], int(row_id)) for row_id, error_text in failed_rows],
                )
        finally:
            if conn:
                conn.close()

    def pending_count(self) -> int:
        conn = None
        try:
            conn = self._connect()
            row = conn.execute("SELECT COUNT(*) FROM outbox_ops WHERE status = 'pending'").fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        finally:
            if conn:
                conn.close()

    def get_health_snapshot(self) -> dict[str, Any]:
        now = time.time()
        conn = None
        try:
            conn = self._connect()
            rows = conn.execute(
                """
                SELECT
                    status,
                    COUNT(*) AS cnt,
                    MIN(created_at) AS min_created_at,
                    MAX(created_at) AS max_created_at
                FROM outbox_ops
                GROUP BY status
                """
            ).fetchall()

            snapshot: dict[str, Any] = {
                "total": 0,
                "pending": 0,
                "pending_retry": 0,
                "failed": 0,
                "applied": 0,
                "oldest_pending_age_sec": 0.0,
                "newest_pending_age_sec": 0.0,
                "next_retry_in_sec": 0.0,
            }

            for row in rows:
                status = str(row["status"] or "")
                cnt = int(row["cnt"] or 0)
                snapshot["total"] += cnt
                if status in snapshot:
                    snapshot[status] = cnt

                if status == "pending":
                    min_created_at = float(row["min_created_at"] or now)
                    max_created_at = float(row["max_created_at"] or now)
                    snapshot["oldest_pending_age_sec"] = max(0.0, now - min_created_at)
                    snapshot["newest_pending_age_sec"] = max(0.0, now - max_created_at)

            retry_row = conn.execute(
                """
                SELECT COUNT(*), MIN(next_retry_at)
                FROM outbox_ops
                WHERE status = 'pending' AND attempts > 0
                """
            ).fetchone()
            if retry_row:
                snapshot["pending_retry"] = int(retry_row[0] or 0)
                next_retry_at = float(retry_row[1] or now)
                snapshot["next_retry_in_sec"] = max(0.0, next_retry_at - now)

            return snapshot
        finally:
            if conn:
                conn.close()

    def prune_applied(self, keep_last: int = 2000):
        conn = None
        try:
            conn = self._connect()
            row = conn.execute("SELECT COUNT(*) FROM outbox_ops WHERE status = 'applied'").fetchone()
            applied_count = int(row[0]) if row and row[0] is not None else 0
            if applied_count <= keep_last:
                return
            to_delete = applied_count - keep_last
            conn.execute(
                """
                DELETE FROM outbox_ops
                WHERE id IN (
                    SELECT id FROM outbox_ops
                    WHERE status = 'applied'
                    ORDER BY id ASC
                    LIMIT ?
                )
                """,
                (to_delete,),
            )
        finally:
            if conn:
                conn.close()
