"""Short, locked change detection for the supported DELETE network profile.

SQLite's rollback-journal file change counter is designed for cache invalidation:
https://www.sqlite.org/fileformat.html#file_change_counter
Only 100 header bytes are inspected; full central copies use SQLite's Backup API.
No SQLite connection or shared-file handle remains open between worker requests.
"""
from __future__ import annotations

import hashlib
import os


def capture_source_stamp(conn, path: str) -> dict | None:
    from rem_card.app.role_admission import file_identity
    owned = not conn.in_transaction
    if owned:
        conn.execute("BEGIN")
    try:
        # Pin SQLite's SHARED read lock before opening the metadata header.
        conn.execute("SELECT rootpage FROM sqlite_master LIMIT 1").fetchone()
        if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "delete":
            return None
        try:
            identity = file_identity(path)
        except ValueError:
            return None
        with open(path, "rb", buffering=0) as handle:
            header = handle.read(100)
            info = os.fstat(handle.fileno())
        if (int(info.st_dev) != identity["device"] or int(info.st_ino) != identity["inode"]
                or (os.name == "nt" and info.st_ctime_ns != identity["created_ns"])):
            return None
        if (len(header) != 100 or header[:16] != b"SQLite format 3\x00"
                or header[18:20] != b"\x01\x01" or header[24:28] != header[92:96]):
            return None
        if file_identity(path) != identity:
            return None
        return {"format": 1, "identity": identity,
                "header_sha256": hashlib.sha256(header).hexdigest(),
                "size": info.st_size, "mtime_ns": info.st_mtime_ns}
    finally:
        if owned and conn.in_transaction:
            conn.execute("ROLLBACK")
