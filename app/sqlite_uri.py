from __future__ import annotations

import os
from typing import Literal
from urllib.parse import quote


SQLiteOpenMode = Literal["ro", "rw"]


def build_sqlite_file_uri(
    path: str | os.PathLike[str],
    *,
    mode: SQLiteOpenMode,
    immutable: bool = False,
) -> str:
    """Build an absolute, percent-encoded SQLite ``file:`` URI.

    Backslashes are intentionally retained for Windows paths.  In particular,
    SQLite accepts UNC paths in the ``file:\\\\server\\share`` form, while a
    standards-style URI authority (``file://server/share``) can be rejected by
    the default Windows VFS.
    """
    if mode not in {"ro", "rw"}:
        raise ValueError("SQLite file URI mode must be 'ro' or 'rw'")

    raw_path = os.fsdecode(os.fspath(path))
    if not raw_path:
        raise ValueError("SQLite file URI path must not be empty")
    if "\x00" in raw_path:
        raise ValueError("SQLite file URI path must not contain NUL")

    normalized = os.path.abspath(os.path.normpath(raw_path))
    # URI query/fragment delimiters and literal percent signs must be escaped.
    # Slashes, drive separators and Windows UNC separators remain path syntax.
    encoded_path = quote(normalized, safe="/:\\")
    query = f"mode={mode}"
    if immutable:
        query += "&immutable=1"
    return f"file:{encoded_path}?{query}"
