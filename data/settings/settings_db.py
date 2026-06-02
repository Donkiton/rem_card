from __future__ import annotations

import os
import glob
import json
import re
import socket
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Iterator

from rem_card.app.db_runtime_context import DbRuntimeContext
from rem_card.app.local_metrics import record_metric
from rem_card.app.logger import logger
from rem_card.app.settings_db_paths import (
    get_settings_backup_dir,
    get_settings_db_path,
    get_settings_dir,
    get_settings_lock_path,
)
from rem_card.app.sqlite_shared import (
    FileWriteLock,
    SQLiteWriteController,
    backup_connection,
    backup_meta_path,
    configure_connection,
    run_integrity_check,
    run_quick_check,
    validate_sqlite_file,
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


SETTINGS_PRE_WRITE_BACKUP_COALESCE_SEC = max(
    0.0,
    float(os.environ.get("REMCARD_SETTINGS_PRE_WRITE_BACKUP_COALESCE_SEC", "900")),
)
SETTINGS_PRE_WRITE_BACKUP_SCAN_LIMIT = max(
    1,
    int(os.environ.get("REMCARD_SETTINGS_PRE_WRITE_BACKUP_SCAN_LIMIT", "5")),
)
SETTINGS_BACKUP_RETENTION_DAYS = max(
    1,
    int(os.environ.get("REMCARD_SETTINGS_BACKUP_RETENTION_DAYS", "14")),
)
SETTINGS_BACKUP_MAX_COUNT = max(
    5,
    int(os.environ.get("REMCARD_SETTINGS_BACKUP_MAX_COUNT", "30")),
)
SETTINGS_BACKUP_MAX_TOTAL_BYTES = max(
    256 * 1024 * 1024,
    int(float(os.environ.get("REMCARD_SETTINGS_BACKUP_MAX_TOTAL_GB", "1.0")) * 1024 * 1024 * 1024),
)
SETTINGS_BACKUP_MIN_VALID_RECENT = max(
    1,
    int(os.environ.get("REMCARD_SETTINGS_BACKUP_MIN_VALID_RECENT", "3")),
)
SETTINGS_BACKUP_HEALTH_CHECK_SCAN_LIMIT = max(
    SETTINGS_BACKUP_MIN_VALID_RECENT,
    int(os.environ.get("REMCARD_SETTINGS_BACKUP_HEALTH_CHECK_SCAN_LIMIT", "8")),
)
SETTINGS_BACKUP_CLEANUP_MIN_INTERVAL_SEC = max(
    60.0,
    float(os.environ.get("REMCARD_SETTINGS_BACKUP_CLEANUP_MIN_INTERVAL_SEC", "300")),
)
SETTINGS_BACKUP_FORCE_SOURCE_PREFIXES = (
    "settings_schema_",
    "settings_release_snapshot",
    "settings_legacy_import",
    "settings_legacy_prescription_overrides",
)


def _settings_backup_source_tag(source: str) -> str:
    raw = str(source or "settings_write").strip().lower()
    tag = re.sub(r"[^a-z0-9_]+", "_", raw).strip("_")
    tag = re.sub(r"_+", "_", tag)
    return (tag or "settings_write")[:96]


def _settings_source_forces_backup(source: str) -> bool:
    normalized = str(source or "").strip().lower()
    return any(normalized.startswith(prefix) for prefix in SETTINGS_BACKUP_FORCE_SOURCE_PREFIXES)


class SettingsDatabase:
    def __init__(
        self,
        baza_dir: str | None = None,
        *,
        context: DbRuntimeContext | None = None,
        runtime_context: DbRuntimeContext | None = None,
        settings_db_path: str | None = None,
        settings_db_lock_path: str | None = None,
        settings_backups_dir: str | None = None,
        settings_backup_health_dir: str | None = None,
        readonly: bool | None = None,
    ):
        effective_context = context or runtime_context
        if effective_context is not None:
            self.baza_dir = effective_context.baza_dir
            self.db_path = os.path.abspath(os.path.normpath(settings_db_path or effective_context.settings_db_path))
            self.lock_path = os.path.abspath(os.path.normpath(settings_db_lock_path or effective_context.settings_db_lock_path))
            self.backups_dir = os.path.abspath(os.path.normpath(settings_backups_dir or effective_context.settings_backups_dir))
            self.backup_health_dir = os.path.abspath(
                os.path.normpath(settings_backup_health_dir or effective_context.settings_backup_health_dir)
            )
            self.settings_readonly = bool(effective_context.settings_readonly if readonly is None else readonly)
        else:
            self.baza_dir = baza_dir
            self.db_path = os.path.abspath(os.path.normpath(settings_db_path or get_settings_db_path(baza_dir)))
            default_settings_dir = os.path.dirname(self.db_path) if settings_db_path else get_settings_dir(baza_dir)
            default_lock_path = (
                os.path.join(default_settings_dir, "settings.db.lock")
                if settings_db_path
                else get_settings_lock_path(baza_dir)
            )
            default_backups_dir = (
                os.path.join(default_settings_dir, "backups")
                if settings_db_path
                else get_settings_backup_dir(baza_dir)
            )
            self.lock_path = os.path.abspath(
                os.path.normpath(settings_db_lock_path or default_lock_path)
            )
            self.backups_dir = os.path.abspath(
                os.path.normpath(settings_backups_dir or default_backups_dir)
            )
            self.backup_health_dir = os.path.abspath(
                os.path.normpath(settings_backup_health_dir or os.path.join(default_settings_dir, "backup_health"))
            )
            self.settings_readonly = bool(readonly)
        self.settings_dir = os.path.abspath(os.path.normpath(os.path.dirname(self.db_path)))
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
        self._last_settings_backup_cleanup_ts = 0.0

    def ensure_ready(self) -> dict[str, object]:
        if self.settings_readonly:
            return self._ensure_readonly_ready()

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
                    before_begin=lambda: self._backup_before_write(
                        conn,
                        "settings_schema_init",
                        force=True,
                    ),
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

    def _ensure_readonly_ready(self) -> dict[str, object]:
        started = time.perf_counter()
        if not os.path.exists(self.db_path):
            raise SettingsDbError(f"Readonly settings DB snapshot отсутствует: {self.db_path}")
        conn = None
        try:
            conn = self.connect(readonly=True)
            ok, reason = run_quick_check(conn)
            if not ok:
                raise SettingsDbError(f"settings DB quick_check failed: {reason}")
            schema_status = settings_schema.inspect_schema_status(conn)
        except sqlite3.OperationalError as exc:
            if _is_sqlite_busy_error(exc):
                raise SettingsDbError(
                    "БД настроек временно занята другим рабочим местом. Повторите действие позже."
                ) from exc
            raise
        finally:
            if conn is not None:
                conn.close()
        if schema_status.schema_version > settings_schema.SCHEMA_VERSION:
            raise SettingsDbError(
                "Версия БД настроек новее текущей программы. Обновите программу."
            )
        if not schema_status.fastpath_ready:
            raise SettingsDbError(
                f"Readonly settings DB snapshot не готов к запуску: {schema_status.reason}"
            )
        logger.info(
            "settings_db_path=%s settings_db_created=false settings_schema_version=%s "
            "settings_profile=network_safe settings_local_db_used=true "
            "settings_schema_fastpath_used=true settings_readonly=true",
            self.db_path,
            schema_status.schema_version,
        )
        return {
            "settings_db_path": self.db_path,
            "settings_db_created": False,
            "settings_schema_version": schema_status.schema_version,
            "settings_profile": "network_safe",
            "settings_local_db_used": True,
            "settings_schema_fastpath_used": True,
            "settings_readonly": True,
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }

    def connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        if self.settings_readonly and not readonly:
            raise SettingsDbError("БД настроек открыта в режиме только чтения. Изменения запрещены.")
        if readonly:
            uri = f"file:{self.db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, check_same_thread=True, isolation_level=None, timeout=5.0)
            configure_connection(conn, readonly=True, profile="network")
            return conn
        conn = sqlite3.connect(self.db_path, check_same_thread=True, isolation_level=None, timeout=5.0)
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
        if self.settings_readonly:
            raise SettingsDbError("БД настроек открыта в режиме только чтения. Изменения запрещены.")
        started = time.perf_counter()
        status = "error"
        backup_created = False
        conn = self.connect(readonly=False)
        try:
            def before_begin() -> None:
                nonlocal backup_created
                backup_created = self._backup_before_write(conn, source) is not None

            with self.write_controller.transaction(
                conn,
                source=source,
                before_begin=before_begin,
            ) as cursor:
                yield cursor
            status = "ok"
            if backup_created:
                self._maybe_cleanup_settings_backups(conn)
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
        backup_path = os.path.join(self.backups_dir, f"settings_migration_{stamp}.db")
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
        self._annotate_settings_backup_meta(
            backup_path,
            backup_kind="settings_migration",
            source="settings_migration_backup",
        )

    def _backup_before_write(
        self,
        conn: sqlite3.Connection,
        source: str,
        *,
        force: bool = False,
    ) -> str | None:
        if not force and not _settings_source_forces_backup(source):
            recent_backup = self._find_recent_valid_pre_write_backup(source)
            if recent_backup:
                record_metric(
                    "settings_backup_skipped_reason",
                    "coalesced_recent_pre_write",
                    source=source,
                    backup_path=recent_backup,
                )
                logger.info(
                    "Skipping settings pre-write backup for %s: recent validated backup exists: %s",
                    source,
                    recent_backup,
                )
                return None
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_dir = self.backups_dir
        source_tag = _settings_backup_source_tag(source)
        backup_path = os.path.join(backup_dir, f"settings_pre_{source_tag}_{stamp}.db")
        created_path = backup_connection(
            conn,
            backup_path,
            invalid_dir=os.path.join(backup_dir, "invalid"),
            logger=logger,
            validate=True,
            lock_path=None,
            source=f"settings_pre_write_backup:{source or 'settings_write'}",
        )
        self._annotate_settings_backup_meta(
            created_path,
            backup_kind="settings_pre_write",
            source=source or "settings_write",
        )
        return created_path

    def _find_recent_valid_pre_write_backup(self, source: str) -> str | None:
        if SETTINGS_PRE_WRITE_BACKUP_COALESCE_SEC <= 0:
            return None
        source_tag = _settings_backup_source_tag(source)
        pattern = os.path.join(self.backups_dir, f"settings_pre_{source_tag}_*.db")
        candidates = [
            path
            for path in glob.glob(pattern)
            if os.path.isfile(path)
        ]
        if not candidates:
            return None
        candidates.sort(key=os.path.getmtime, reverse=True)
        now = time.time()
        for path in candidates[:SETTINGS_PRE_WRITE_BACKUP_SCAN_LIMIT]:
            try:
                age_sec = now - os.path.getmtime(path)
            except OSError:
                continue
            if age_sec < 0 or age_sec > SETTINGS_PRE_WRITE_BACKUP_COALESCE_SEC:
                continue
            if not self._settings_backup_meta_is_valid(path):
                continue
            ok, reason = validate_sqlite_file(path)
            if ok:
                return path
            logger.warning("Recent settings backup is not valid and will not be reused: %s (%s)", path, reason)
        return None

    @staticmethod
    def _settings_backup_meta_is_valid(db_path: str) -> bool:
        meta_path = backup_meta_path(db_path)
        if not os.path.isfile(meta_path):
            return False
        try:
            with open(meta_path, "r", encoding="utf-8") as handle:
                meta = json.load(handle)
            size_bytes = int(meta.get("size_bytes") or 0)
        except Exception:
            return False
        return (
            meta.get("quick_check") == "ok"
            and meta.get("integrity_check") == "ok"
            and size_bytes > 0
        )

    def _annotate_settings_backup_meta(self, backup_path: str, *, backup_kind: str, source: str) -> None:
        meta_path = backup_meta_path(backup_path)
        if not os.path.isfile(meta_path):
            return
        try:
            with open(meta_path, "r", encoding="utf-8") as handle:
                meta = json.load(handle)
            meta["backup_kind"] = backup_kind
            meta["settings_source"] = source
            meta["settings_source_tag"] = _settings_backup_source_tag(source)
            tmp_path = os.path.join(
                os.path.dirname(meta_path),
                f".settings_meta_{os.getpid()}_{int(time.time() * 1000000)}.tmp",
            )
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(meta, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_path, meta_path)
        except Exception as exc:
            logger.warning("Failed to annotate settings backup metadata %s: %s", meta_path, exc)

    def _maybe_cleanup_settings_backups(self, conn: sqlite3.Connection) -> None:
        now = time.time()
        if now - self._last_settings_backup_cleanup_ts < SETTINGS_BACKUP_CLEANUP_MIN_INTERVAL_SEC:
            return
        self._last_settings_backup_cleanup_ts = now
        stamp_path = os.path.join(self.backup_health_dir, "settings_backup_cleanup.stamp")
        try:
            if os.path.exists(stamp_path):
                stamp_age = now - os.path.getmtime(stamp_path)
                if 0 <= stamp_age < SETTINGS_BACKUP_CLEANUP_MIN_INTERVAL_SEC:
                    return
        except OSError:
            return

        lock_path = os.path.join(self.backup_health_dir, "settings_backup_cleanup.lock")
        cleanup_lock = FileWriteLock(lock_path, stale_timeout_sec=10 * 60, logger=logger)
        owner_id = f"{socket.gethostname()}:{os.getpid()}:settings_backup_cleanup"
        if not cleanup_lock.acquire(owner_id, "settings_backup_cleanup"):
            return
        try:
            self._cleanup_settings_backups(conn)
            os.makedirs(self.backup_health_dir, exist_ok=True)
            with open(stamp_path, "w", encoding="utf-8") as handle:
                handle.write(datetime.now().isoformat(timespec="seconds"))
        except Exception as exc:
            logger.warning("Settings backup cleanup skipped after error: %s", exc, exc_info=True)
        finally:
            cleanup_lock.release()

    def _cleanup_settings_backups(self, conn: sqlite3.Connection) -> None:
        if not os.path.isdir(self.backups_dir):
            return
        ok, reason = run_quick_check(conn)
        if not ok:
            logger.warning("Settings backup cleanup skipped: current settings DB quick_check failed: %s", reason)
            return

        files = self._list_settings_backup_files()
        if not files:
            return
        valid_recent = self._valid_recent_settings_backups(files)
        if len(valid_recent) < SETTINGS_BACKUP_MIN_VALID_RECENT:
            logger.warning(
                "Settings backup cleanup skipped: only %s valid recent backup(s), required %s",
                len(valid_recent),
                SETTINGS_BACKUP_MIN_VALID_RECENT,
            )
            return

        protected = set(valid_recent[:SETTINGS_BACKUP_MIN_VALID_RECENT])
        keep = self._select_settings_backups_to_keep(files, protected)
        to_delete = [path for path in files if path not in keep]

        remaining = [path for path in files if path not in set(to_delete)]
        total_size = self._settings_backup_total_size(remaining)
        if total_size > SETTINGS_BACKUP_MAX_TOTAL_BYTES:
            for old_path in sorted(remaining, key=os.path.getmtime):
                if total_size <= SETTINGS_BACKUP_MAX_TOTAL_BYTES:
                    break
                if old_path in protected:
                    continue
                if old_path in to_delete:
                    continue
                try:
                    total_size -= os.path.getsize(old_path)
                except OSError:
                    pass
                to_delete.append(old_path)

        deleted = 0
        for path in sorted(set(to_delete), key=os.path.getmtime):
            if path in protected:
                continue
            if self._remove_settings_backup_with_meta(path):
                deleted += 1
        if deleted:
            logger.info("Settings backup cleanup removed %s old backup(s) from %s", deleted, self.backups_dir)
            record_metric("settings_backup_cleanup_deleted", deleted, backup_dir=self.backups_dir)

    def _list_settings_backup_files(self) -> list[str]:
        result: list[str] = []
        for name in os.listdir(self.backups_dir):
            if not name.lower().endswith(".db"):
                continue
            if not (name.startswith("settings_pre_") or name.startswith("settings_migration_")):
                continue
            path = os.path.join(self.backups_dir, name)
            if os.path.isfile(path):
                result.append(path)
        result.sort(key=os.path.getmtime, reverse=True)
        return result

    def _valid_recent_settings_backups(self, files: list[str]) -> list[str]:
        valid: list[str] = []
        for path in files[:SETTINGS_BACKUP_HEALTH_CHECK_SCAN_LIMIT]:
            if not self._settings_backup_meta_is_valid(path):
                continue
            ok, reason = validate_sqlite_file(path)
            if ok:
                valid.append(path)
                if len(valid) >= SETTINGS_BACKUP_MIN_VALID_RECENT:
                    break
            else:
                logger.warning("Settings backup failed cleanup validation: %s (%s)", path, reason)
        return valid

    def _select_settings_backups_to_keep(self, files: list[str], protected: set[str]) -> set[str]:
        keep = set(protected)
        pre_write = [path for path in files if os.path.basename(path).startswith("settings_pre_")]
        migrations = [path for path in files if os.path.basename(path).startswith("settings_migration_")]

        pre_write.sort(key=os.path.getmtime, reverse=True)
        keep.update(pre_write[:SETTINGS_BACKUP_MAX_COUNT])

        daily_cutoff = datetime.now() - timedelta(days=SETTINGS_BACKUP_RETENTION_DAYS)
        days_seen: set[str] = set()
        for path in pre_write:
            try:
                modified = datetime.fromtimestamp(os.path.getmtime(path))
            except OSError:
                continue
            if modified < daily_cutoff:
                continue
            day_key = modified.strftime("%Y-%m-%d")
            if day_key in days_seen:
                continue
            days_seen.add(day_key)
            keep.add(path)

        migration_cutoff = datetime.now() - timedelta(days=max(30, SETTINGS_BACKUP_RETENTION_DAYS))
        for path in migrations:
            try:
                if datetime.fromtimestamp(os.path.getmtime(path)) >= migration_cutoff:
                    keep.add(path)
            except OSError:
                continue
        if migrations:
            migrations.sort(key=os.path.getmtime, reverse=True)
            keep.add(migrations[0])
        return keep

    @staticmethod
    def _settings_backup_total_size(files: list[str]) -> int:
        total = 0
        for path in files:
            try:
                total += os.path.getsize(path)
            except OSError:
                continue
        return total

    @staticmethod
    def _remove_settings_backup_with_meta(db_path: str) -> bool:
        removed = False
        for path in (db_path, backup_meta_path(db_path)):
            try:
                os.remove(path)
                removed = True
            except FileNotFoundError:
                continue
            except Exception as exc:
                logger.warning("Failed to delete settings backup file %s: %s", path, exc)
        return removed


_DEFAULT_DB: SettingsDatabase | None = None


def get_settings_database(
    context: DbRuntimeContext | None = None,
    *,
    runtime_context: DbRuntimeContext | None = None,
    settings_db_path: str | None = None,
    settings_db_lock_path: str | None = None,
    settings_backups_dir: str | None = None,
    settings_backup_health_dir: str | None = None,
    readonly: bool | None = None,
) -> SettingsDatabase:
    global _DEFAULT_DB
    if (
        context is not None
        or runtime_context is not None
        or settings_db_path is not None
        or settings_db_lock_path is not None
        or settings_backups_dir is not None
        or settings_backup_health_dir is not None
        or readonly is not None
    ):
        return SettingsDatabase(
            context=context,
            runtime_context=runtime_context,
            settings_db_path=settings_db_path,
            settings_db_lock_path=settings_db_lock_path,
            settings_backups_dir=settings_backups_dir,
            settings_backup_health_dir=settings_backup_health_dir,
            readonly=readonly,
        )
    if _DEFAULT_DB is None:
        _DEFAULT_DB = SettingsDatabase()
    return _DEFAULT_DB


def reset_settings_database() -> None:
    global _DEFAULT_DB
    _DEFAULT_DB = None
