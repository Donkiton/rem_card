from __future__ import annotations

import os
import socket
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

from rem_card.app.local_metrics import record_metric
from rem_card.app.logger import logger
from rem_card.app.settings_db_paths import (
    get_settings_backup_dir,
    get_settings_db_path,
    get_settings_dir,
    get_settings_lock_path,
)
from rem_card.app.sqlite_shared import (
    SQLiteWriteController,
    backup_connection,
    configure_connection,
    run_integrity_check,
    run_quick_check,
)
from rem_card.data.settings import settings_schema


class SettingsDbError(RuntimeError):
    pass


def _is_sqlite_busy_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return (
        "locked" in message
        or "busy" in message
        or "could not acquire sequential write lock" in message
    )


class SettingsDatabase:
    def __init__(self, baza_dir: str | None = None):
        self.baza_dir = baza_dir
        self.settings_dir = get_settings_dir(baza_dir)
        self.db_path = get_settings_db_path(baza_dir)
        self.lock_path = get_settings_lock_path(baza_dir)
        owner_id = f"{socket.gethostname()}:{os.getpid()}:settings_db"
        self.write_controller = SQLiteWriteController(
            db_path=self.db_path,
            lock_path=self.lock_path,
            owner_id=owner_id,
            logger=logger,
            max_retries=20,
            retry_delay_ms=150,
            stale_timeout_sec=10 * 60,
        )

    def ensure_ready(self) -> dict[str, object]:
        started = time.perf_counter()
        created = False
        try:
            os.makedirs(self.settings_dir, exist_ok=True)
        except Exception as exc:
            raise SettingsDbError(
                f"Не удалось создать папку БД настроек: {self.settings_dir} ({exc})"
            ) from exc

        created = not os.path.exists(self.db_path)
        schema_status = settings_schema.SettingsSchemaStatus(False, "missing_db")
        status_started = time.perf_counter()
        status_metric_status = "skipped"
        if not created:
            status_metric_status = "error"
            status_conn = None
            try:
                status_conn = self.connect(readonly=True)
                schema_status = settings_schema.inspect_schema_status(status_conn)
                status_metric_status = "ok"
            except sqlite3.OperationalError as exc:
                if _is_sqlite_busy_error(exc):
                    record_metric(
                        "settings_schema_status_ms",
                        round((time.perf_counter() - status_started) * 1000.0, 3),
                        status="locked",
                        reason="locked",
                        force_flush=True,
                    )
                    raise SettingsDbError(
                        "БД настроек временно занята другим рабочим местом. Повторите действие позже."
                    ) from exc
                schema_status = settings_schema.SettingsSchemaStatus(
                    False,
                    f"schema_status_unreadable:{exc}",
                )
            finally:
                if status_conn is not None:
                    status_conn.close()

        record_metric(
            "settings_schema_status_ms",
            round((time.perf_counter() - status_started) * 1000.0, 3),
            status=status_metric_status,
            reason=schema_status.reason,
            schema_version=schema_status.schema_version,
            force_flush=True,
        )

        if schema_status.schema_version > settings_schema.SCHEMA_VERSION:
            record_metric(
                "settings_schema_fastpath_used",
                False,
                reason=schema_status.reason,
                force_flush=True,
            )
            raise SettingsDbError(
                "Версия БД настроек новее текущей программы. Обновите программу."
            )

        if schema_status.fastpath_ready:
            record_metric(
                "settings_schema_fastpath_used",
                True,
                reason=schema_status.reason,
                force_flush=True,
            )
            record_metric("settings_schema_init_ms", 0.0, status="skipped", reason="schema_fastpath")
            record_metric("settings_apply_schema_called", False, reason="schema_fastpath")
            record_metric("settings_apply_schema_wrote", False, reason="schema_fastpath")
            record_metric("settings_backup_skipped_reason", "schema_fastpath")
            record_metric("settings_integrity_check_skipped_reason", "schema_fastpath")
            logger.info(
                "settings_db_path=%s settings_db_created=%s settings_schema_version=%s "
                "settings_profile=network_safe settings_local_db_used=false "
                "settings_schema_fastpath_used=true",
                self.db_path,
                bool(created),
                schema_status.schema_version,
            )
            return {
                "settings_db_path": self.db_path,
                "settings_db_created": bool(created),
                "settings_schema_version": schema_status.schema_version,
                "settings_profile": "network_safe",
                "settings_local_db_used": False,
                "settings_schema_fastpath_used": True,
                "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
            }

        record_metric(
            "settings_schema_fastpath_used",
            False,
            reason=schema_status.reason,
            force_flush=True,
        )
        record_metric("settings_backup_skipped_reason", "not_skipped", reason=schema_status.reason)
        record_metric("settings_integrity_check_skipped_reason", "not_skipped", reason=schema_status.reason)

        conn = self.connect(readonly=False)
        try:
            ok, reason = run_quick_check(conn)
            if not ok:
                raise SettingsDbError(f"settings DB quick_check failed: {reason}")
            current_version = settings_schema.get_schema_version(conn)
            if current_version > settings_schema.SCHEMA_VERSION:
                raise SettingsDbError(
                    "Версия БД настроек новее текущей программы. Обновите программу."
                )
            if current_version < settings_schema.SCHEMA_VERSION and not created and current_version > 0:
                self._backup_before_migration(conn)

            schema_init_started = time.perf_counter()
            apply_schema_called = False
            apply_schema_wrote = False
            schema_init_status = "error"
            try:
                with self.write_controller.transaction(
                    conn,
                    source="settings_schema_init",
                    before_begin=lambda: self._backup_before_write(conn, "settings_schema_init"),
                ) as _cursor:
                    apply_schema_called = True
                    settings_schema.apply_schema(conn)
                    apply_schema_wrote = True
                schema_init_status = "ok"
            finally:
                record_metric(
                    "settings_schema_init_ms",
                    round((time.perf_counter() - schema_init_started) * 1000.0, 3),
                    status=schema_init_status,
                    reason=schema_status.reason,
                    force_flush=True,
                )
                record_metric("settings_apply_schema_called", apply_schema_called, reason=schema_status.reason)
                record_metric("settings_apply_schema_wrote", apply_schema_wrote, reason=schema_status.reason)

            ok, reason = run_quick_check(conn)
            if not ok:
                raise SettingsDbError(f"settings DB quick_check after schema failed: {reason}")
            integrity_started = time.perf_counter()
            ok, reason = run_integrity_check(conn)
            record_metric(
                "settings_integrity_check_ms",
                round((time.perf_counter() - integrity_started) * 1000.0, 3),
                status="ok" if ok else "error",
                reason=schema_status.reason,
                force_flush=not ok,
            )
            if not ok:
                raise SettingsDbError(f"settings DB integrity_check after schema failed: {reason}")

            schema_version = settings_schema.get_schema_version(conn)
            logger.info(
                "settings_db_path=%s settings_db_created=%s settings_schema_version=%s "
                "settings_profile=network_safe settings_local_db_used=false "
                "settings_schema_fastpath_used=false settings_schema_status_reason=%s",
                self.db_path,
                bool(created),
                schema_version,
                schema_status.reason,
            )
            return {
                "settings_db_path": self.db_path,
                "settings_db_created": bool(created),
                "settings_schema_version": schema_version,
                "settings_profile": "network_safe",
                "settings_local_db_used": False,
                "settings_schema_fastpath_used": False,
                "settings_schema_status_reason": schema_status.reason,
                "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
            }
        except sqlite3.OperationalError as exc:
            if _is_sqlite_busy_error(exc):
                raise SettingsDbError(
                    "БД настроек временно занята другим рабочим местом. Повторите действие позже."
                ) from exc
            raise
        finally:
            conn.close()

    def connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        if readonly:
            uri = f"file:{self.db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False, isolation_level=None, timeout=5.0)
            configure_connection(conn, readonly=True, profile="network")
            return conn
        conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None, timeout=5.0)
        configure_connection(conn, profile="network")
        return conn

    @contextmanager
    def read_connection(self) -> Iterator[sqlite3.Connection]:
        started = time.perf_counter()
        status = "error"
        conn = self.connect(readonly=True)
        try:
            status = "ok"
            yield conn
        finally:
            try:
                conn.close()
            finally:
                record_metric(
                    "settings_read_ms",
                    round((time.perf_counter() - started) * 1000.0, 3),
                    status=status,
                )

    @contextmanager
    def transaction(self, source: str = "settings_write") -> Iterator[sqlite3.Cursor]:
        started = time.perf_counter()
        status = "error"
        conn = self.connect(readonly=False)
        try:
            with self.write_controller.transaction(
                conn,
                source=source,
                before_begin=lambda: self._backup_before_write(conn, source),
            ) as cursor:
                yield cursor
            status = "ok"
        except sqlite3.OperationalError as exc:
            if _is_sqlite_busy_error(exc):
                raise SettingsDbError(
                    "БД настроек временно занята другим рабочим местом. Данные не сохранены."
                ) from exc
            raise
        finally:
            try:
                conn.close()
            finally:
                record_metric(
                    "settings_write_ms",
                    round((time.perf_counter() - started) * 1000.0, 3),
                    source=source,
                    status=status,
                )

    def _backup_before_migration(self, conn: sqlite3.Connection) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = get_settings_backup_dir(self.baza_dir)
        backup_path = os.path.join(backup_dir, f"settings_migration_{stamp}.db")
        backup_connection(
            conn,
            backup_path,
            invalid_dir=os.path.join(self.settings_dir, "migration_backups", "invalid"),
            logger=logger,
            validate=True,
            lock_path=self.lock_path,
            source="settings_migration_backup",
            lock_wait_sec=20.0,
        )

    def _backup_before_write(self, conn: sqlite3.Connection, source: str) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_dir = get_settings_backup_dir(self.baza_dir)
        backup_path = os.path.join(backup_dir, f"settings_pre_{stamp}.db")
        backup_connection(
            conn,
            backup_path,
            invalid_dir=os.path.join(backup_dir, "invalid"),
            logger=logger,
            validate=True,
            lock_path=None,
            source=f"settings_pre_write_backup:{source or 'settings_write'}",
        )


_DEFAULT_DB: SettingsDatabase | None = None


def get_settings_database() -> SettingsDatabase:
    global _DEFAULT_DB
    if _DEFAULT_DB is None:
        _DEFAULT_DB = SettingsDatabase()
    return _DEFAULT_DB
