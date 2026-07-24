from __future__ import annotations

import json
import multiprocessing
import os
import socket
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping
from datetime import date, datetime
from typing import Any, Callable

from rem_card.app.sqlite_shared import FileWriteLock, configure_connection


DEFAULT_NETWORK_WRITE_TIMEOUT_SEC = 8.0
NETWORK_WRITE_CONFIRM_RESERVE_SEC = 1.5
NETWORK_WRITE_RECEIPT_TABLE = "runtime_write_receipts"


class NetworkWriteWorkerError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        operation_id: str,
        source: str,
        remote_error_class: str = "",
    ):
        self.operation_id = str(operation_id or "")
        self.source = str(source or "")
        self.remote_error_class = str(remote_error_class or "")
        super().__init__(str(message or "Ошибка сетевой записи"))


class NetworkWriteWorkerTimeout(NetworkWriteWorkerError):
    def __init__(
        self,
        *,
        operation_id: str,
        source: str,
        timeout_sec: float,
        phase: str,
        outcome_unknown: bool,
    ):
        self.timeout_sec = float(timeout_sec)
        self.phase = str(phase or "")
        self.outcome_unknown = bool(outcome_unknown)
        state = "результат записи не подтверждён" if outcome_unknown else "запись не выполнена"
        super().__init__(
            f"Сетевая операция превысила безопасный тайм-аут {timeout_sec:.1f} с; {state}.",
            operation_id=operation_id,
            source=source,
            remote_error_class="NetworkWriteWorkerTimeout",
        )


class WorkerRow(Mapping[str, Any]):
    def __init__(self, columns: tuple[str, ...], values: tuple[Any, ...]):
        self._columns = tuple(columns)
        self._values = tuple(values)
        self._mapping = dict(zip(self._columns, self._values))

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        return self._mapping[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._columns)

    def __len__(self) -> int:
        return len(self._values)

    def keys(self):
        return list(self._columns)

    def __repr__(self) -> str:
        return f"WorkerRow({self._mapping!r})"


def _encode_result(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date)):
        return {"__remcard_type__": type(value).__name__, "value": value.isoformat()}
    if isinstance(value, Mapping):
        return {str(key): _encode_result(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return {
            "__remcard_type__": "tuple",
            "items": [_encode_result(item) for item in value],
        }
    if isinstance(value, list):
        return [_encode_result(item) for item in value]
    return {"__remcard_type__": "text", "value": str(value)}


def _decode_result(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode_result(item) for item in value]
    if not isinstance(value, dict):
        return value
    marker = str(value.get("__remcard_type__") or "")
    if marker == "tuple":
        return tuple(_decode_result(item) for item in value.get("items") or [])
    if marker == "datetime":
        return datetime.fromisoformat(str(value.get("value") or ""))
    if marker == "date":
        return date.fromisoformat(str(value.get("value") or ""))
    if marker == "text":
        return str(value.get("value") or "")
    return {str(key): _decode_result(item) for key, item in value.items()}


def _receipt_result(payload: str | None) -> Any:
    if not payload:
        return None
    return _decode_result(json.loads(payload))


def _send_error(pipe, exc: Exception, *, phase: str) -> None:
    try:
        pipe.send(
            {
                "ok": False,
                "phase": str(phase or ""),
                "error_class": type(exc).__name__,
                "error": str(exc),
            }
        )
    except Exception:
        pass


def _open_network_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path,
        check_same_thread=True,
        isolation_level=None,
        timeout=0.25,
    )
    conn.row_factory = sqlite3.Row
    configure_connection(conn, profile="network")
    conn.execute("PRAGMA busy_timeout = 250")
    return conn


def _lookup_receipt(conn: sqlite3.Connection, operation_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"""
        SELECT operation_id, result_json, affected_rows_json, committed_at
        FROM {NETWORK_WRITE_RECEIPT_TABLE}
        WHERE operation_id = ?
        """,
        (operation_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "operation_id": str(row["operation_id"] or ""),
        "result": _receipt_result(row["result_json"]),
        "affected_rows": json.loads(row["affected_rows_json"] or "[]"),
        "committed_at": str(row["committed_at"] or ""),
    }


def _change_cursor(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM change_log").fetchone()
    return int(row[0] or 0) if row else 0


def _affected_rows(conn: sqlite3.Connection, after_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, entity_name, entity_id, admission_id, action
        FROM change_log
        WHERE id > ?
        ORDER BY id
        """,
        (int(after_id),),
    ).fetchall()
    return [
        {
            "change_id": int(row["id"]),
            "entity_name": str(row["entity_name"] or ""),
            "entity_id": row["entity_id"],
            "admission_id": row["admission_id"],
            "action": str(row["action"] or ""),
        }
        for row in rows
    ]


def _execute_sql(conn: sqlite3.Connection, message: dict[str, Any]) -> dict[str, Any]:
    command = str(message.get("cmd") or "")
    cursor = conn.cursor()
    try:
        if command == "execute":
            cursor.execute(str(message.get("sql") or ""), tuple(message.get("params") or ()))
        elif command == "executemany":
            cursor.executemany(
                str(message.get("sql") or ""),
                [tuple(params or ()) for params in (message.get("batch") or [])],
            )
        elif command == "executescript":
            cursor.executescript(str(message.get("sql") or ""))
        else:
            raise ValueError(f"Unsupported worker cursor command: {command}")
        description = tuple(item[0] for item in (cursor.description or ()))
        rows = [tuple(row) for row in cursor.fetchall()] if cursor.description else []
        return {
            "ok": True,
            "columns": description,
            "rows": rows,
            "rowcount": int(cursor.rowcount),
            "lastrowid": cursor.lastrowid,
        }
    finally:
        cursor.close()


def _worker_write(pipe, message: dict[str, Any]) -> None:
    db_path = os.path.abspath(str(message.get("db_path") or ""))
    lock_path = os.path.abspath(str(message.get("lock_path") or ""))
    operation_id = str(message.get("operation_id") or "")
    source = str(message.get("source") or "network_write_worker")
    node_id = str(message.get("node_id") or "")
    metadata = dict(message.get("metadata") or {})
    conn: sqlite3.Connection | None = None
    lock = FileWriteLock(lock_path, stale_timeout_sec=60.0)
    lock_acquired = False
    transaction_started = False
    try:
        conn = _open_network_connection(db_path)
        receipt = _lookup_receipt(conn, operation_id)
        if receipt is not None:
            pipe.send({"ok": True, "status": "already_committed", **receipt})
            return

        while not lock.acquire(node_id or f"{socket.gethostname()}:{os.getpid()}", source):
            time.sleep(0.05)
        lock_acquired = True
        while True:
            try:
                conn.execute("BEGIN IMMEDIATE")
                transaction_started = True
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                time.sleep(0.05)

        receipt = _lookup_receipt(conn, operation_id)
        if receipt is not None:
            conn.rollback()
            transaction_started = False
            pipe.send({"ok": True, "status": "already_committed", **receipt})
            return
        before_change_id = _change_cursor(conn)
        pipe.send({"ok": True, "status": "ready"})

        while True:
            command = pipe.recv()
            name = str(command.get("cmd") or "")
            if name in {"execute", "executemany", "executescript"}:
                try:
                    pipe.send(_execute_sql(conn, command))
                except Exception as exc:
                    _send_error(pipe, exc, phase="execute")
                continue
            if name == "abort":
                conn.rollback()
                transaction_started = False
                pipe.send({"ok": True, "status": "rolled_back"})
                return
            if name != "finish":
                raise ValueError(f"Unsupported worker command: {name}")

            encoded_result = command.get("result")
            affected_rows = _affected_rows(conn, before_change_id)
            result_json = json.dumps(encoded_result, ensure_ascii=False, separators=(",", ":"))
            affected_rows_json = json.dumps(
                affected_rows,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            conn.execute(
                f"""
                INSERT INTO {NETWORK_WRITE_RECEIPT_TABLE} (
                    operation_id, request_id, source, node_id, role,
                    admission_id, operation_case_id, result_json, affected_rows_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    str(metadata.get("request_id") or ""),
                    source,
                    node_id,
                    str(metadata.get("role") or ""),
                    metadata.get("admission_id"),
                    metadata.get("operation_case_id"),
                    result_json,
                    affected_rows_json,
                ),
            )
            conn.commit()
            transaction_started = False
            pipe.send(
                {
                    "ok": True,
                    "status": "committed",
                    "operation_id": operation_id,
                    "result": _decode_result(encoded_result),
                    "affected_rows": affected_rows,
                }
            )
            return
    except EOFError:
        pass
    except Exception as exc:
        if transaction_started and conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        _send_error(pipe, exc, phase="worker")
    finally:
        if lock_acquired:
            lock.release()
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _worker_main(pipe) -> None:
    while True:
        try:
            message = pipe.recv()
        except EOFError:
            return
        command = str(message.get("cmd") or "")
        if command == "shutdown":
            return
        if command == "lookup":
            conn = None
            try:
                conn = _open_network_connection(os.path.abspath(str(message.get("db_path") or "")))
                receipt = _lookup_receipt(conn, str(message.get("operation_id") or ""))
                pipe.send({"ok": True, "status": "found" if receipt else "missing", "receipt": receipt})
            except Exception as exc:
                _send_error(pipe, exc, phase="lookup")
            finally:
                if conn is not None:
                    conn.close()
            continue
        if command != "write":
            _send_error(pipe, ValueError(f"Unsupported worker request: {command}"), phase="dispatch")
            continue
        _worker_write(pipe, message)


class _RemoteConnection:
    def __init__(self, client: "NetworkWriteWorkerClient", deadline: float, operation_id: str, source: str):
        self._client = client
        self._deadline = deadline
        self._operation_id = operation_id
        self._source = source

    def execute(self, sql: str, params=()):
        cursor = _RemoteCursor(self._client, self._deadline, self._operation_id, self._source)
        return cursor.execute(sql, params)

    def executemany(self, sql: str, batch):
        cursor = _RemoteCursor(self._client, self._deadline, self._operation_id, self._source)
        return cursor.executemany(sql, batch)

    def executescript(self, sql: str):
        cursor = _RemoteCursor(self._client, self._deadline, self._operation_id, self._source)
        return cursor.executescript(sql)


class _RemoteCursor:
    def __init__(self, client: "NetworkWriteWorkerClient", deadline: float, operation_id: str, source: str):
        self._client = client
        self._deadline = deadline
        self._operation_id = operation_id
        self._source = source
        self._rows: list[WorkerRow] = []
        self._position = 0
        self.rowcount = -1
        self.lastrowid = None
        self.description = None
        self.connection = _RemoteConnection(client, deadline, operation_id, source)

    def _apply_response(self, response: dict[str, Any]):
        columns = tuple(str(item) for item in (response.get("columns") or ()))
        self._rows = [
            WorkerRow(columns, tuple(values or ()))
            for values in (response.get("rows") or [])
        ]
        self._position = 0
        self.rowcount = int(response.get("rowcount", -1))
        self.lastrowid = response.get("lastrowid")
        self.description = tuple((column, None, None, None, None, None, None) for column in columns) or None
        return self

    def execute(self, sql: str, params=()):
        return self._apply_response(
            self._client._request(
                {"cmd": "execute", "sql": str(sql), "params": tuple(params or ())},
                deadline=self._deadline,
                operation_id=self._operation_id,
                source=self._source,
                phase="execute",
            )
        )

    def executemany(self, sql: str, batch):
        return self._apply_response(
            self._client._request(
                {
                    "cmd": "executemany",
                    "sql": str(sql),
                    "batch": [tuple(params or ()) for params in batch],
                },
                deadline=self._deadline,
                operation_id=self._operation_id,
                source=self._source,
                phase="executemany",
            )
        )

    def executescript(self, sql: str):
        return self._apply_response(
            self._client._request(
                {"cmd": "executescript", "sql": str(sql)},
                deadline=self._deadline,
                operation_id=self._operation_id,
                source=self._source,
                phase="executescript",
            )
        )

    def fetchone(self):
        if self._position >= len(self._rows):
            return None
        row = self._rows[self._position]
        self._position += 1
        return row

    def fetchall(self):
        rows = self._rows[self._position :]
        self._position = len(self._rows)
        return rows

    def fetchmany(self, size: int | None = None):
        limit = max(1, int(size or 1))
        rows = self._rows[self._position : self._position + limit]
        self._position += len(rows)
        return rows

    def close(self):
        self._rows = []
        self._position = 0

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row


class NetworkWriteWorkerClient:
    def __init__(
        self,
        *,
        db_path: str,
        lock_path: str,
        node_id: str,
        timeout_sec: float = DEFAULT_NETWORK_WRITE_TIMEOUT_SEC,
    ):
        self.db_path = os.path.abspath(db_path)
        self.lock_path = os.path.abspath(lock_path)
        self.node_id = str(node_id or "")
        self.timeout_sec = max(0.2, min(30.0, float(timeout_sec)))
        self._mutex = threading.Lock()
        self._process = None
        self._pipe = None

    def _ensure_started(self) -> None:
        if self._process is not None and self._process.is_alive() and self._pipe is not None:
            return
        self._terminate()
        context = multiprocessing.get_context("spawn")
        parent_pipe, child_pipe = context.Pipe(duplex=True)
        process = context.Process(
            target=_worker_main,
            args=(child_pipe,),
            name="RemCardNetworkWriteWorker",
            daemon=True,
        )
        process.start()
        child_pipe.close()
        self._process = process
        self._pipe = parent_pipe

    def _remaining(self, deadline: float) -> float:
        return max(0.0, deadline - time.monotonic())

    def _recv(
        self,
        *,
        deadline: float,
        operation_id: str,
        source: str,
        phase: str,
    ) -> dict[str, Any]:
        if self._pipe is None or not self._pipe.poll(self._remaining(deadline)):
            raise NetworkWriteWorkerTimeout(
                operation_id=operation_id,
                source=source,
                timeout_sec=self.timeout_sec,
                phase=phase,
                outcome_unknown=True,
            )
        try:
            response = self._pipe.recv()
        except (EOFError, OSError) as exc:
            raise NetworkWriteWorkerError(
                f"Процесс сетевой записи завершился до получения результата: {exc}",
                operation_id=operation_id,
                source=source,
                remote_error_class=type(exc).__name__,
            ) from exc
        if not response.get("ok"):
            raise NetworkWriteWorkerError(
                str(response.get("error") or "Ошибка процесса сетевой записи"),
                operation_id=operation_id,
                source=source,
                remote_error_class=str(response.get("error_class") or ""),
            )
        return dict(response)

    def _request(
        self,
        message: dict[str, Any],
        *,
        deadline: float,
        operation_id: str,
        source: str,
        phase: str,
    ) -> dict[str, Any]:
        if self._pipe is None:
            raise NetworkWriteWorkerError(
                "Процесс сетевой записи не запущен.",
                operation_id=operation_id,
                source=source,
            )
        self._pipe.send(message)
        return self._recv(
            deadline=deadline,
            operation_id=operation_id,
            source=source,
            phase=phase,
        )

    def _terminate(self) -> None:
        process = self._process
        pipe = self._pipe
        self._process = None
        self._pipe = None
        if pipe is not None:
            try:
                pipe.close()
            except Exception:
                pass
        if process is None:
            return
        if process.is_alive():
            process.terminate()
            process.join(timeout=0.5)
        if process.is_alive():
            try:
                process.kill()
            except Exception:
                pass
            process.join(timeout=0.5)
        try:
            process.close()
        except Exception:
            pass

    def _confirm_after_timeout(
        self,
        *,
        operation_id: str,
        source: str,
        deadline: float,
    ) -> dict[str, Any] | None:
        self._terminate()
        try:
            self._ensure_started()
            response = self._request(
                {
                    "cmd": "lookup",
                    "db_path": self.db_path,
                    "operation_id": operation_id,
                },
                deadline=deadline,
                operation_id=operation_id,
                source=source,
                phase="confirm",
            )
        except Exception:
            self._terminate()
            return None
        if response.get("status") == "found":
            return dict(response.get("receipt") or {})
        return {"missing": True}

    def execute(
        self,
        operation: Callable[[Any], Any],
        *,
        operation_id: str,
        source: str,
        metadata: dict[str, Any] | None = None,
        timeout_sec: float | None = None,
    ) -> Any:
        operation_id = str(operation_id or "").strip()
        if not operation_id:
            raise ValueError("operation_id is required for isolated network writes")
        effective_timeout = max(0.2, min(30.0, float(timeout_sec or self.timeout_sec)))
        started = time.monotonic()
        total_deadline = started + effective_timeout
        work_deadline = total_deadline - min(
            NETWORK_WRITE_CONFIRM_RESERVE_SEC,
            effective_timeout / 3.0,
        )
        with self._mutex:
            try:
                self._ensure_started()
                begin = self._request(
                    {
                        "cmd": "write",
                        "db_path": self.db_path,
                        "lock_path": self.lock_path,
                        "operation_id": operation_id,
                        "source": str(source or "network_write"),
                        "node_id": self.node_id,
                        "metadata": dict(metadata or {}),
                    },
                    deadline=work_deadline,
                    operation_id=operation_id,
                    source=source,
                    phase="begin",
                )
                if begin.get("status") == "already_committed":
                    return begin.get("result")
                cursor = _RemoteCursor(self, work_deadline, operation_id, source)
                try:
                    result = operation(cursor)
                except Exception:
                    try:
                        self._request(
                            {"cmd": "abort"},
                            deadline=work_deadline,
                            operation_id=operation_id,
                            source=source,
                            phase="rollback",
                        )
                    except Exception:
                        self._terminate()
                    raise
                finish = self._request(
                    {"cmd": "finish", "result": _encode_result(result)},
                    deadline=work_deadline,
                    operation_id=operation_id,
                    source=source,
                    phase="commit",
                )
                return result if finish.get("status") == "committed" else finish.get("result")
            except NetworkWriteWorkerTimeout as exc:
                receipt = self._confirm_after_timeout(
                    operation_id=operation_id,
                    source=source,
                    deadline=total_deadline,
                )
                if receipt and not receipt.get("missing"):
                    return receipt.get("result")
                raise NetworkWriteWorkerTimeout(
                    operation_id=operation_id,
                    source=source,
                    timeout_sec=effective_timeout,
                    phase=exc.phase,
                    outcome_unknown=receipt is None,
                ) from exc
            except NetworkWriteWorkerError:
                self._terminate()
                raise

    def close(self, *, timeout_sec: float = 0.5) -> None:
        acquired = self._mutex.acquire(timeout=max(0.0, float(timeout_sec)))
        if not acquired:
            self._terminate()
            return
        try:
            if self._pipe is not None and self._process is not None and self._process.is_alive():
                try:
                    self._pipe.send({"cmd": "shutdown"})
                    self._process.join(timeout=0.5)
                except Exception:
                    pass
            self._terminate()
        finally:
            self._mutex.release()
