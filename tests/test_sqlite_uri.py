from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import unquote

import pytest


try:
    from _local_rem_card_bootstrap import bootstrap_local_rem_card

    bootstrap_local_rem_card()
except ImportError:
    pass

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.app.sqlite_uri import build_sqlite_file_uri  # noqa: E402


@pytest.mark.parametrize(
    ("literal", "encoded"),
    (("#", "%23"), ("%", "%25"), ("?", "%3F")),
)
def test_builder_percent_encodes_sqlite_uri_delimiters(
    tmp_path: Path,
    literal: str,
    encoded: str,
) -> None:
    db_path = tmp_path / f"literal{literal}name.db"
    can_exist_on_host = not (os.name == "nt" and literal == "?")
    if can_exist_on_host:
        sqlite3.connect(db_path).close()

    uri = build_sqlite_file_uri(db_path, mode="ro")

    assert encoded in uri
    assert unquote(uri.removeprefix("file:").split("?mode=", 1)[0]) == os.path.abspath(db_path)
    if can_exist_on_host:
        conn = sqlite3.connect(uri, uri=True)
        try:
            assert conn.execute("PRAGMA query_only").fetchone() is not None
        finally:
            conn.close()


def test_builder_preserves_unc_form_and_encodes_filename() -> None:
    uri = build_sqlite_file_uri(r"\\server\share\folder\a#%?.db", mode="ro", immutable=True)

    assert uri.startswith("file:\\\\server\\share\\folder\\")
    assert "a%23%25%3F.db" in uri
    assert uri.endswith("?mode=ro&immutable=1")


def test_mode_rw_refuses_to_create_missing_database(tmp_path: Path) -> None:
    db_path = tmp_path / "missing#database.db"

    with pytest.raises(sqlite3.OperationalError):
        sqlite3.connect(build_sqlite_file_uri(db_path, mode="rw"), uri=True)

    assert not db_path.exists()


def test_builder_rejects_unsupported_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mode"):
        build_sqlite_file_uri(tmp_path / "db.sqlite", mode="rwc")  # type: ignore[arg-type]
