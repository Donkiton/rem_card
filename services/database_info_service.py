from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

from rem_card.app.db_cycle_registry import (
    DbCycleInfo,
    discover_db_cycle_paths,
    inspect_db_cycle,
)
from rem_card.app.db_runtime_context import DbRuntimeContext, build_network_runtime_context
from rem_card.app.sqlite_shared import backup_meta_path


@dataclass(frozen=True)
class DatabaseInfo:
    key: str
    title: str
    kind: str
    path: str
    exists: bool
    size_bytes: int
    created_at: datetime | None
    modified_at: datetime | None
    status: str
    schema_version: str
    detail: str


@dataclass(frozen=True)
class BackupInfo:
    scope: str
    kind: str
    path: str
    size_bytes: int
    created_at: datetime | None
    modified_at: datetime | None
    validation_status: str
    source: str
    sha256: str
    metadata_path: str
    metadata_error: str


@dataclass(frozen=True)
class DatabaseHistoryEvent:
    occurred_at: datetime | None
    event_type: str
    title: str
    description: str
    size_bytes: int
    status: str
    path: str


@dataclass(frozen=True)
class DatabaseInfoSnapshot:
    collected_at: datetime
    databases: tuple[DatabaseInfo, ...]
    cycles: tuple[DbCycleInfo, ...]
    backups: tuple[BackupInfo, ...]
    events: tuple[DatabaseHistoryEvent, ...]
    warnings: tuple[str, ...]

    @property
    def total_backup_bytes(self) -> int:
        return sum(item.size_bytes for item in self.backups)

    @property
    def latest_backup_at(self) -> datetime | None:
        dates = [item.created_at or item.modified_at for item in self.backups]
        dates = [value for value in dates if value is not None]
        return max(dates) if dates else None


class DatabaseInfoCollectionCancelled(RuntimeError):
    pass


class DatabaseInfoService:
    """Собирает read-only снимок существующих БД и резервных копий."""

    def __init__(
        self,
        runtime_context: DbRuntimeContext | None = None,
        *,
        current_db_path: str = "",
        reference_db_path: str | None = None,
    ) -> None:
        self.runtime_context = runtime_context or build_network_runtime_context()
        self.current_db_path = _normalise_path(
            current_db_path or self.runtime_context.medical_db_path
        )
        if reference_db_path is None:
            from rem_card.app.paths import MKB_DB_PATH

            reference_db_path = MKB_DB_PATH
        self.reference_db_path = _normalise_path(reference_db_path) if reference_db_path else ""

    def collect(
        self,
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> DatabaseInfoSnapshot:
        warnings: list[str] = []
        _raise_if_cancelled(cancel_check)
        cycles = self._collect_cycles(warnings, cancel_check)
        _raise_if_cancelled(cancel_check)
        databases = self._collect_databases(cycles)
        _raise_if_cancelled(cancel_check)
        backups = self._collect_backups(warnings, cancel_check)
        _raise_if_cancelled(cancel_check)
        events = _build_events(databases, cycles, backups)
        return DatabaseInfoSnapshot(
            collected_at=datetime.now(),
            databases=tuple(databases),
            cycles=tuple(cycles),
            backups=tuple(backups),
            events=tuple(events),
            warnings=tuple(warnings),
        )

    def _collect_cycles(
        self,
        warnings: list[str],
        cancel_check: Callable[[], bool] | None,
    ) -> list[DbCycleInfo]:
        try:
            paths = discover_db_cycle_paths(
                current_db_path=self.current_db_path,
                archive_dir=os.path.dirname(self.current_db_path),
                include_current=True,
            )
            cycles = []
            for path in paths:
                _raise_if_cancelled(cancel_check)
                cycles.append(
                    inspect_db_cycle(
                        path,
                        current_db_path=self.current_db_path,
                        validate=False,
                    )
                )
            return cycles
        except DatabaseInfoCollectionCancelled:
            raise
        except Exception as exc:
            warnings.append(f"Не удалось прочитать циклы основной БД: {exc}")
            return []

    def _collect_databases(self, cycles: list[DbCycleInfo]) -> list[DatabaseInfo]:
        current_cycle = next((item for item in cycles if item.is_current), None)
        main = _inspect_database(
            key="medical",
            title="Основная медицинская БД",
            kind="Рабочая БД",
            path=self.current_db_path,
            meta_table="meta",
            schema_key="schema_fastpath_revision",
        )
        if current_cycle is not None:
            main = DatabaseInfo(
                key=main.key,
                title=main.title,
                kind=main.kind,
                path=main.path,
                exists=current_cycle.exists,
                size_bytes=current_cycle.size_bytes,
                created_at=current_cycle.cycle_started_at or current_cycle.created_at,
                modified_at=current_cycle.modified_at,
                status="Доступна" if current_cycle.quick_check_ok else "Ошибка чтения",
                schema_version=current_cycle.schema_revision or main.schema_version,
                detail=(
                    f"Текущий цикл, режим: {self.runtime_context.source_label}"
                    if current_cycle.quick_check_ok
                    else current_cycle.validation_message
                ),
            )

        settings = _inspect_database(
            key="settings",
            title="БД настроек",
            kind="Рабочая БД",
            path=self.runtime_context.settings_db_path,
            meta_table="settings_meta",
            schema_key="schema_version",
            created_key="settings_db_created_at",
            readonly=bool(self.runtime_context.settings_readonly),
        )
        databases = [main, settings]
        if self.reference_db_path:
            databases.append(
                _inspect_database(
                    key="reference",
                    title="Справочник МКБ-10",
                    kind="Справочная БД",
                    path=self.reference_db_path,
                    readonly=True,
                )
            )
        return databases

    def _collect_backups(
        self,
        warnings: list[str],
        cancel_check: Callable[[], bool] | None,
    ) -> list[BackupInfo]:
        settings_dir = os.path.dirname(self.runtime_context.settings_db_path)
        roots = [
            (self.runtime_context.settings_backups_dir, "Настройки", False),
            (os.path.join(settings_dir, "migration_backups"), "Настройки", False),
            (self.runtime_context.medical_invalid_backups_dir, "Основная БД", True),
            (self.runtime_context.medical_backups_root_dir, "Основная БД", False),
        ]
        found: list[BackupInfo] = []
        seen: set[str] = set()
        for root, scope, invalid_root in roots:
            _raise_if_cancelled(cancel_check)
            if not root or not os.path.isdir(root):
                continue

            def on_error(exc: OSError, root_path=root) -> None:
                warnings.append(f"Не удалось прочитать каталог бэкапов {root_path}: {exc}")

            for directory, _dir_names, file_names in os.walk(root, onerror=on_error):
                _raise_if_cancelled(cancel_check)
                is_invalid_dir = invalid_root or "invalid" in {
                    part.lower() for part in _path_parts(os.path.relpath(directory, root))
                }
                for file_name in file_names:
                    _raise_if_cancelled(cancel_check)
                    if not file_name.lower().endswith((".db", ".sqlite", ".sqlite3")):
                        continue
                    path = _normalise_path(os.path.join(directory, file_name))
                    key = os.path.normcase(path)
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        found.append(
                            _inspect_backup(
                                path,
                                scope=scope,
                                force_invalid=is_invalid_dir,
                            )
                        )
                    except OSError as exc:
                        warnings.append(f"Не удалось прочитать бэкап {path}: {exc}")
        found.sort(
            key=lambda item: item.created_at or item.modified_at or datetime.min,
            reverse=True,
        )
        return found


def _inspect_database(
    *,
    key: str,
    title: str,
    kind: str,
    path: str,
    meta_table: str = "",
    schema_key: str = "",
    created_key: str = "",
    readonly: bool = False,
) -> DatabaseInfo:
    normalised = _normalise_path(path)
    try:
        stat = os.stat(normalised)
    except OSError as exc:
        return DatabaseInfo(
            key=key,
            title=title,
            kind=kind,
            path=normalised,
            exists=False,
            size_bytes=0,
            created_at=None,
            modified_at=None,
            status="Не найдена",
            schema_version="",
            detail=str(exc),
        )

    created_at = _from_timestamp(stat.st_ctime)
    modified_at = _from_timestamp(stat.st_mtime)
    schema_version = ""
    detail = "Только чтение" if readonly else "Готова к работе"
    status = "Доступна"
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(
            f"file:{normalised}?mode=ro",
            uri=True,
            check_same_thread=False,
            isolation_level=None,
            timeout=2.0,
        )
        conn.execute("PRAGMA query_only = ON")
        conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        if meta_table and schema_key and _safe_table_name(meta_table):
            meta_value = _read_database_meta(conn, meta_table, schema_key)
            if meta_value is not None:
                schema_version = str(meta_value)
        if meta_table and created_key and _safe_table_name(meta_table):
            meta_value = _read_database_meta(conn, meta_table, created_key)
            parsed = _parse_datetime(meta_value)
            if parsed is not None:
                created_at = parsed
    except Exception as exc:
        status = "Ошибка чтения"
        detail = str(exc)
    finally:
        if conn is not None:
            conn.close()

    return DatabaseInfo(
        key=key,
        title=title,
        kind=kind,
        path=normalised,
        exists=True,
        size_bytes=int(stat.st_size),
        created_at=created_at,
        modified_at=modified_at,
        status=status,
        schema_version=schema_version,
        detail=detail,
    )


def _inspect_backup(path: str, *, scope: str, force_invalid: bool) -> BackupInfo:
    stat = os.stat(path)
    metadata, metadata_path, metadata_error = _read_backup_metadata(path)
    created_at = _parse_datetime(metadata.get("created_at")) or _from_timestamp(stat.st_ctime)
    modified_at = _from_timestamp(stat.st_mtime)
    size_bytes = int(stat.st_size)

    quick_check = str(metadata.get("quick_check") or "").strip().lower()
    integrity_check = str(metadata.get("integrity_check") or "").strip().lower()
    recorded_size = _safe_positive_int(metadata.get("size_bytes"))
    size_mismatch = recorded_size is not None and recorded_size != size_bytes
    if force_invalid or (quick_check and quick_check != "ok") or (integrity_check and integrity_check != "ok"):
        validation_status = "Невалиден"
    elif size_mismatch:
        validation_status = "Изменён после проверки"
        mismatch_message = (
            f"Размер в метаданных: {recorded_size} Б; фактический размер: {size_bytes} Б"
        )
        metadata_error = f"{metadata_error}; {mismatch_message}" if metadata_error else mismatch_message
    elif quick_check == "ok" and integrity_check == "ok":
        validation_status = "Проверен"
    elif metadata_error:
        validation_status = "Ошибка метаданных"
    else:
        validation_status = "Без метаданных"

    rotation = metadata.get("rotation")
    rotation_source = rotation.get("source") if isinstance(rotation, dict) else ""
    source = str(
        metadata.get("source")
        or metadata.get("settings_source")
        or rotation_source
        or ""
    )
    return BackupInfo(
        scope=scope,
        kind=_backup_kind(metadata, os.path.basename(path), scope),
        path=path,
        size_bytes=size_bytes,
        created_at=created_at,
        modified_at=modified_at,
        validation_status=validation_status,
        source=source,
        sha256=str(metadata.get("sha256") or ""),
        metadata_path=metadata_path,
        metadata_error=metadata_error,
    )


def _read_backup_metadata(path: str) -> tuple[dict, str, str]:
    candidates = [backup_meta_path(path), f"{path}.meta.json"]
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(candidate))
        if key in seen:
            continue
        seen.add(key)
        if not os.path.isfile(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict):
                return {}, candidate, "Файл метаданных не содержит объект"
            return payload, candidate, ""
        except Exception as exc:
            return {}, candidate, str(exc)
    return {}, "", ""


def _backup_kind(metadata: dict, file_name: str, scope: str) -> str:
    raw_kind = str(metadata.get("backup_kind") or "").strip().lower()
    labels = {
        "primary_manual": "Ручной",
        "settings_manual": "Ручной",
        "settings_pre_write": "Перед изменением",
        "settings_migration": "Перед миграцией",
        "pre_rotation": "Перед ротацией",
        "daily": "Ежедневный",
    }
    if raw_kind in labels:
        return labels[raw_kind]

    lower = file_name.lower()
    patterns = (
        ("pre_rotation_", "Перед ротацией"),
        ("pre_migration_", "Перед миграцией"),
        ("settings_migration_", "Перед миграцией"),
        ("settings_manual_", "Ручной"),
        ("settings_pre_", "Перед изменением"),
        ("shutdown_", "При завершении"),
        ("startup_", "При запуске"),
        ("periodic_", "Периодический"),
    )
    for prefix, label in patterns:
        if lower.startswith(prefix):
            return label
    if "manual" in lower:
        return "Ручной"
    if re.match(r"^rao_journal_\d{4}-\d{2}-\d{2}\.db$", lower):
        return "Ежедневный"
    return "Настройки" if scope == "Настройки" else "Автоматический"


def _build_events(
    databases: Iterable[DatabaseInfo],
    cycles: Iterable[DbCycleInfo],
    backups: Iterable[BackupInfo],
) -> list[DatabaseHistoryEvent]:
    events: list[DatabaseHistoryEvent] = []
    for database in databases:
        if not database.exists:
            continue
        events.append(
            DatabaseHistoryEvent(
                occurred_at=database.created_at,
                event_type="База данных",
                title=f"Создана: {database.title}",
                description=database.kind,
                size_bytes=database.size_bytes,
                status=database.status,
                path=database.path,
            )
        )
    for cycle in cycles:
        if cycle.is_current:
            continue
        cycle_started = cycle.cycle_started_at or cycle.created_at
        events.append(
            DatabaseHistoryEvent(
                occurred_at=_archive_timestamp(cycle.path) or cycle.created_at or cycle.modified_at,
                event_type="Ротация",
                title="БД перенесена в архив",
                description=(
                    f"Цикл начат {_format_event_datetime(cycle_started)}"
                    if cycle_started is not None
                    else cycle.display_name
                ),
                size_bytes=cycle.size_bytes,
                status="Доступен" if cycle.quick_check_ok else "Ошибка чтения",
                path=cycle.path,
            )
        )
    for backup in backups:
        events.append(
            DatabaseHistoryEvent(
                occurred_at=backup.created_at or backup.modified_at,
                event_type="Бэкап",
                title=f"{backup.kind}: {backup.scope}",
                description=backup.source or os.path.basename(backup.path),
                size_bytes=backup.size_bytes,
                status=backup.validation_status,
                path=backup.path,
            )
        )
    events.sort(key=lambda item: item.occurred_at or datetime.min, reverse=True)
    return events


def _parse_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y%m%d_%H%M%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _from_timestamp(value: float) -> datetime:
    return datetime.fromtimestamp(float(value))


def _read_database_meta(
    conn: sqlite3.Connection,
    table_name: str,
    key: str,
) -> object | None:
    try:
        row = conn.execute(
            f'SELECT value FROM "{table_name}" WHERE key = ?',
            (key,),
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    return row[0] if row else None


def _archive_timestamp(path: str) -> datetime | None:
    file_name = os.path.basename(path)
    timestamp_patterns = (
        r"_(?:archived|cycle)_(\d{8}_\d{6})(?:_\d+)?\.db$",
        r"_(?:archived|cycle)_(\d{8})\.db$",
        r"^rao-(\d{4}-\d{2}-\d{2})\.db$",
    )
    for pattern in timestamp_patterns:
        match = re.search(pattern, file_name, re.IGNORECASE)
        if match:
            return _parse_datetime(match.group(1))
    return None


def _format_event_datetime(value: datetime) -> str:
    return value.strftime("%d.%m.%Y %H:%M")


def _safe_positive_int(value: object) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _raise_if_cancelled(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise DatabaseInfoCollectionCancelled("Сбор сведений отменён")


def _normalise_path(path: str) -> str:
    return os.path.abspath(os.path.normpath(str(path or "")))


def _safe_table_name(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value))


def _path_parts(path: str) -> tuple[str, ...]:
    return tuple(part for part in re.split(r"[\\/]", path) if part and part != ".")
