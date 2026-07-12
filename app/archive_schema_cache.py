from __future__ import annotations

import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping


_MAX_SCHEMA_CACHE_ENTRIES = 64
_SCHEMA_CACHE_LOCK = threading.Lock()
_SCHEMA_CACHE: "OrderedDict[tuple, ArchiveSchemaSnapshot]" = OrderedDict()


@dataclass(frozen=True)
class ArchiveSchemaSnapshot:
    fingerprint: tuple
    tables: frozenset[str]
    columns: Mapping[str, frozenset[str]]


def archive_file_fingerprint(db_path: str) -> tuple:
    absolute_path = os.path.abspath(str(db_path or ""))
    stat_result = os.stat(absolute_path)
    try:
        wal_stat = os.stat(f"{absolute_path}-wal")
        wal_state = (int(wal_stat.st_size), int(wal_stat.st_mtime_ns))
    except OSError:
        wal_state = (-1, -1)
    return (
        os.path.normcase(absolute_path),
        int(stat_result.st_size),
        int(stat_result.st_mtime_ns),
        int(stat_result.st_ctime_ns),
        int(stat_result.st_dev),
        int(stat_result.st_ino),
        *wal_state,
    )


def _quoted_identifier(value: str) -> str:
    return '"' + str(value or "").replace('"', '""') + '"'


def get_archive_schema(
    conn,
    db_path: str,
    *,
    inspect_tables: Iterable[str] = (),
) -> ArchiveSchemaSnapshot:
    requested_tables = tuple(sorted({str(name or "").strip() for name in inspect_tables if str(name or "").strip()}))
    fingerprint = archive_file_fingerprint(db_path)
    cache_key = (fingerprint, requested_tables)
    with _SCHEMA_CACHE_LOCK:
        cached = _SCHEMA_CACHE.get(cache_key)
        if cached is not None:
            _SCHEMA_CACHE.move_to_end(cache_key)
            return cached

    tables = frozenset(
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if row and row[0]
    )
    columns: dict[str, frozenset[str]] = {}
    for table_name in requested_tables:
        if table_name not in tables:
            columns[table_name] = frozenset()
            continue
        rows = conn.execute(f"PRAGMA table_info({_quoted_identifier(table_name)})").fetchall()
        columns[table_name] = frozenset(str(row[1]) for row in rows if row and row[1])

    snapshot = ArchiveSchemaSnapshot(
        fingerprint=fingerprint,
        tables=tables,
        columns=MappingProxyType(columns),
    )
    with _SCHEMA_CACHE_LOCK:
        _SCHEMA_CACHE[cache_key] = snapshot
        _SCHEMA_CACHE.move_to_end(cache_key)
        while len(_SCHEMA_CACHE) > _MAX_SCHEMA_CACHE_ENTRIES:
            _SCHEMA_CACHE.popitem(last=False)
    return snapshot


def clear_archive_schema_cache() -> None:
    with _SCHEMA_CACHE_LOCK:
        _SCHEMA_CACHE.clear()
