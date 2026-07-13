from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from rem_card.app.paths import LOCAL_CACHE_DIR
from rem_card.app.sqlite_uri import build_sqlite_file_uri


LEGACY_OPERBLOCK_INDEX_ALIAS = "operblock_legacy_idx"
LEGACY_OPERBLOCK_INDEX_TABLE = "operation_case_started"
LEGACY_OPERBLOCK_INDEX_NAME = "idx_operation_case_started_at_id"
LEGACY_OPERBLOCK_INDEX_FORMAT = 1
LEGACY_OPERBLOCK_INDEX_CACHE_DIR = Path(LOCAL_CACHE_DIR) / "operblock_archive_indexes"
LEGACY_OPERBLOCK_INDEX_MAX_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
LEGACY_OPERBLOCK_INDEX_MAX_CASES = 1_000_000
LEGACY_OPERBLOCK_INDEX_MAX_CACHE_BYTES = 128 * 1024 * 1024
LEGACY_OPERBLOCK_INDEX_MAX_CACHE_FILES = 64

_BUILD_LOCK = threading.Lock()


@dataclass(frozen=True)
class LegacyOperblockArchiveIndex:
    path: str
    source_fingerprint: tuple
    row_count: int


def _enabled() -> bool:
    return os.environ.get("REMCARD_LEGACY_OPERBLOCK_INDEX_CACHE", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _file_state(path: str) -> tuple[int, int, int, int, int]:
    try:
        value = os.stat(path)
    except OSError:
        return (-1, -1, -1, -1, -1)
    return (
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
        int(value.st_dev),
        int(value.st_ino),
    )


def operblock_archive_source_fingerprint(db_path: str, conn=None) -> tuple:
    """Fingerprint the source and SQLite sidecars without reading medical data."""
    absolute_path = os.path.abspath(str(db_path or ""))
    schema_version = -1
    if conn is not None:
        try:
            row = conn.execute("PRAGMA main.schema_version").fetchone()
            schema_version = int(row[0]) if row else -1
        except sqlite3.Error:
            schema_version = -1
    return (
        os.path.normcase(absolute_path),
        *_file_state(absolute_path),
        *_file_state(f"{absolute_path}-wal"),
        *_file_state(f"{absolute_path}-journal"),
        schema_version,
    )


def _quoted_identifier(value: str) -> str:
    return '"' + str(value or "").replace('"', '""') + '"'


def source_has_started_at_index(conn) -> bool:
    """Return true for any source index whose leading column is started_at."""
    try:
        index_rows = conn.execute("PRAGMA main.index_list('operation_cases')").fetchall()
    except sqlite3.Error:
        return False
    for index_row in index_rows:
        if not index_row or len(index_row) < 2 or not index_row[1]:
            continue
        if len(index_row) > 4 and bool(index_row[4]):
            # A legacy partial index may exclude rows needed by archive filters.
            continue
        try:
            columns = conn.execute(
                f"PRAGMA main.index_info({_quoted_identifier(str(index_row[1]))})"
            ).fetchall()
        except sqlite3.Error:
            continue
        if columns and str(columns[0][2] or "").strip().casefold() == "started_at":
            return True
    return False


def _readonly_uri(path: str) -> str:
    return build_sqlite_file_uri(path, mode="ro", immutable=True)


def _fingerprint_text(fingerprint: tuple) -> str:
    return json.dumps(list(fingerprint), ensure_ascii=False, separators=(",", ":"))


def _cache_path(fingerprint: tuple, cache_dir: Path) -> Path:
    digest = hashlib.sha256(_fingerprint_text(fingerprint).encode("utf-8")).hexdigest()
    return cache_dir / f"operblock_{digest}.sqlite3"


def _read_valid_index(path: Path, fingerprint: tuple) -> LegacyOperblockArchiveIndex | None:
    if not path.is_file():
        return None
    conn = None
    try:
        conn = sqlite3.connect(_readonly_uri(str(path)), uri=True, isolation_level=None, timeout=2.0)
        meta = dict(conn.execute("SELECT key, value FROM cache_meta").fetchall())
        if int(meta.get("format_version") or 0) != LEGACY_OPERBLOCK_INDEX_FORMAT:
            return None
        if str(meta.get("source_fingerprint") or "") != _fingerprint_text(fingerprint):
            return None
        row_count = int(meta.get("row_count") or 0)
        first_row = conn.execute(
            f"SELECT 1 FROM {LEGACY_OPERBLOCK_INDEX_TABLE} LIMIT 1"
        ).fetchone()
        if (row_count > 0) != bool(first_row):
            return None
        indexes = {
            str(row[1])
            for row in conn.execute(
                f"PRAGMA index_list('{LEGACY_OPERBLOCK_INDEX_TABLE}')"
            ).fetchall()
            if row and len(row) > 1
        }
        if LEGACY_OPERBLOCK_INDEX_NAME not in indexes:
            return None
        return LegacyOperblockArchiveIndex(str(path), fingerprint, row_count)
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None
    finally:
        if conn is not None:
            conn.close()


def _prune_cache(cache_dir: Path, *, keep: Path) -> None:
    try:
        files = sorted(
            (path for path in cache_dir.glob("operblock_*.sqlite3") if path != keep),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
    except OSError:
        return
    total_bytes = 0
    retained = 0
    for path in [keep, *files]:
        try:
            size = int(path.stat().st_size)
        except OSError:
            continue
        retained += 1
        total_bytes += size
        if path == keep:
            continue
        if retained <= LEGACY_OPERBLOCK_INDEX_MAX_CACHE_FILES and total_bytes <= LEGACY_OPERBLOCK_INDEX_MAX_CACHE_BYTES:
            continue
        try:
            path.unlink()
        except OSError:
            # Another process may currently have the readonly sidecar attached.
            pass


def _source_is_cacheable(source_conn, absolute_path: str) -> bool:
    try:
        if int(os.path.getsize(absolute_path)) > LEGACY_OPERBLOCK_INDEX_MAX_SOURCE_BYTES:
            return False
        columns = {
            str(row[1])
            for row in source_conn.execute("PRAGMA main.table_info('operation_cases')").fetchall()
            if row and len(row) > 1
        }
    except (OSError, sqlite3.Error):
        return False
    return {"id", "started_at"}.issubset(columns)


def _write_temp_index(
    source_conn,
    temp_path: Path,
    *,
    absolute_path: str,
    fingerprint: tuple,
    row_count: int,
) -> None:
    temp_conn = sqlite3.connect(str(temp_path), isolation_level=None, timeout=5.0)
    try:
        temp_conn.execute("PRAGMA journal_mode=OFF")
        temp_conn.execute("PRAGMA synchronous=FULL")
        temp_conn.executescript(
            f"""
            BEGIN IMMEDIATE;
            CREATE TABLE cache_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE {LEGACY_OPERBLOCK_INDEX_TABLE} (
                operation_case_id INTEGER PRIMARY KEY,
                started_at TEXT
            );
            CREATE INDEX {LEGACY_OPERBLOCK_INDEX_NAME}
            ON {LEGACY_OPERBLOCK_INDEX_TABLE}(started_at, operation_case_id DESC);
            """
        )
        cursor = source_conn.execute(
            "SELECT id, REPLACE(TRIM(CAST(started_at AS TEXT)), 'T', ' ') "
            "FROM operation_cases ORDER BY id"
        )
        inserted = 0
        while batch := cursor.fetchmany(2048):
            temp_conn.executemany(
                f"INSERT INTO {LEGACY_OPERBLOCK_INDEX_TABLE} (operation_case_id, started_at) VALUES (?, ?)",
                [(int(row[0]), row[1]) for row in batch],
            )
            inserted += len(batch)
        if inserted != row_count:
            raise sqlite3.DatabaseError("Legacy archive changed while the technical index was built")
        if operblock_archive_source_fingerprint(absolute_path, source_conn) != fingerprint:
            raise sqlite3.DatabaseError("Legacy archive fingerprint changed while the technical index was built")
        temp_conn.executemany(
            "INSERT INTO cache_meta (key, value) VALUES (?, ?)",
            (
                ("format_version", str(LEGACY_OPERBLOCK_INDEX_FORMAT)),
                ("source_fingerprint", _fingerprint_text(fingerprint)),
                ("row_count", str(row_count)),
            ),
        )
        temp_conn.execute("COMMIT")
    finally:
        temp_conn.close()


def _install_temp_index(
    temp_path: Path,
    target: Path,
    *,
    root: Path,
    fingerprint: tuple,
) -> LegacyOperblockArchiveIndex | None:
    try:
        os.chmod(temp_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    try:
        os.replace(temp_path, target)
    except OSError:
        # On Windows another process may already have installed/opened the
        # equivalent target.  Accept it only after complete validation.
        cached = _read_valid_index(target, fingerprint)
        if cached is not None:
            return cached
        raise
    _prune_cache(root, keep=target)
    return _read_valid_index(target, fingerprint)


def _build_index(
    source_conn,
    *,
    absolute_path: str,
    root: Path,
    target: Path,
    fingerprint: tuple,
) -> LegacyOperblockArchiveIndex | None:
    temp_path = root / f".{target.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    owns_source_transaction = not bool(getattr(source_conn, "in_transaction", False))
    try:
        if owns_source_transaction:
            source_conn.execute("BEGIN")
        count_row = source_conn.execute("SELECT COUNT(*) FROM operation_cases").fetchone()
        row_count = int(count_row[0] or 0) if count_row else 0
        if row_count > LEGACY_OPERBLOCK_INDEX_MAX_CASES:
            return None
        _write_temp_index(
            source_conn,
            temp_path,
            absolute_path=absolute_path,
            fingerprint=fingerprint,
            row_count=row_count,
        )
        return _install_temp_index(
            temp_path,
            target,
            root=root,
            fingerprint=fingerprint,
        )
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None
    finally:
        if owns_source_transaction and bool(getattr(source_conn, "in_transaction", False)):
            try:
                source_conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def ensure_legacy_operblock_archive_index(
    source_conn,
    db_path: str,
    *,
    cache_dir: str | os.PathLike[str] | None = None,
) -> LegacyOperblockArchiveIndex | None:
    """Build a bounded local technical index for an immutable legacy archive.

    The source is always opened by the caller as ``mode=ro`` and is never
    altered.  The cache contains only operation-case ids and start timestamps;
    patient names, history numbers and diagnoses remain solely in the source.
    Any failure or policy-limit hit falls back to the original readonly query.
    """
    if not _enabled() or source_has_started_at_index(source_conn):
        return None
    absolute_path = os.path.abspath(str(db_path or ""))
    if not _source_is_cacheable(source_conn, absolute_path):
        return None

    fingerprint = operblock_archive_source_fingerprint(absolute_path, source_conn)
    root = Path(cache_dir) if cache_dir is not None else LEGACY_OPERBLOCK_INDEX_CACHE_DIR
    target = _cache_path(fingerprint, root)
    cached = _read_valid_index(target, fingerprint)
    if cached is not None:
        return cached

    with _BUILD_LOCK:
        cached = _read_valid_index(target, fingerprint)
        if cached is not None:
            return cached
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        return _build_index(
            source_conn,
            absolute_path=absolute_path,
            root=root,
            target=target,
            fingerprint=fingerprint,
        )


def attach_legacy_operblock_archive_index(
    source_conn,
    db_path: str,
    *,
    cache_dir: str | os.PathLike[str] | None = None,
) -> LegacyOperblockArchiveIndex | None:
    descriptor = ensure_legacy_operblock_archive_index(source_conn, db_path, cache_dir=cache_dir)
    if descriptor is None:
        return None
    try:
        source_conn.execute(
            f"ATTACH DATABASE ? AS {LEGACY_OPERBLOCK_INDEX_ALIAS}",
            (_readonly_uri(descriptor.path),),
        )
        return descriptor
    except sqlite3.Error:
        return None
