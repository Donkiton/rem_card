from __future__ import annotations

import os
import socket
import sqlite3
import time
import uuid
from typing import Any, Callable
from rem_card.app.db_cycle_registry import discover_db_cycle_paths
from rem_card.app.logger import logger
from rem_card.services.operblock_handoff_service import OperBlockHandoffService
from .common import (
    OPERBLOCK_REPORT_READ_RETRIES,
    OPERBLOCK_REPORT_READ_RETRY_DELAY_SEC,
    _is_sqlite_locked_error,
    _parse_dt,
)


class OperBlockCoreMixin:
    def __init__(self, db_manager):
        self.db = db_manager
        self.client_id = f"{socket.gethostname()}:{os.getpid()}"
        self.handoff_service = OperBlockHandoffService(db_manager, client_id=self.client_id)
        self._operation_case_columns_cache: set[str] | None = None
        self._timeline_events_table_exists_cache: bool | None = None

    def _run_report_read_operation(self, source: str, operation: Callable[[], Any]) -> Any:
        scope = getattr(self.db, "central_read_scope", None)
        attempts = max(1, int(OPERBLOCK_REPORT_READ_RETRIES))
        for attempt in range(1, attempts + 1):
            try:
                if callable(scope):
                    with scope(source):
                        return operation()
                return operation()
            except sqlite3.OperationalError as exc:
                if not _is_sqlite_locked_error(exc) or attempt >= attempts:
                    raise
                delay_sec = OPERBLOCK_REPORT_READ_RETRY_DELAY_SEC * attempt
                logger.warning(
                    "Operblock report read locked source=%s attempt=%s/%s retry_in=%.2fs: %s",
                    source,
                    attempt,
                    attempts,
                    delay_sec,
                    exc,
                )
                time.sleep(delay_sec)

        return operation()

    def _runtime_mode(self) -> str:
        return str(getattr(getattr(self.db, "runtime_context", None), "mode", "") or "")

    def _is_opblock_offline_runtime(self) -> bool:
        return self._runtime_mode() == "opblock_offline"

    def _offline_session_id(self) -> str | None:
        runtime_context = getattr(self.db, "runtime_context", None)
        return str(getattr(runtime_context, "emergency_session_id", "") or "") or None

    def _new_case_uuid(self) -> str:
        return f"opblock:{uuid.uuid4()}"

    def _current_db_path(self) -> str:
        raw = str(getattr(self.db, "db_path", "") or getattr(self.db, "remcard_db_path", "") or "")
        return os.path.abspath(raw) if raw else ""

    def _iter_archive_db_paths(self, *, include_current: bool = True) -> list[str]:
        current_db_path = self._current_db_path()
        if not current_db_path:
            return []
        if self._is_opblock_offline_runtime():
            return [current_db_path] if include_current else []
        return discover_db_cycle_paths(
            current_db_path=current_db_path,
            include_current=include_current,
        )

    def get_archive_db_paths_for_period(self, start_dt: str | None, end_dt: str | None) -> list[str]:
        db_paths = self._iter_archive_db_paths(include_current=True)
        if not start_dt or not end_dt:
            return db_paths

        start = _parse_dt(start_dt)
        end = _parse_dt(end_dt)
        if start is None or end is None:
            return db_paths
        if end < start:
            start, end = end, start

        result = []
        for db_path in db_paths:
            if self._db_has_operblock_cases_in_period(db_path, start, end):
                result.append(db_path)
        return result
