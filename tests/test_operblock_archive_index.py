from __future__ import annotations

import sqlite3

from rem_card.app.operblock_schema import (
    _apply_operblock_schema,
    is_operblock_schema_ready,
)
from rem_card.app.unified_db_schema import ensure_unified_schema
from rem_card.app.unified_db_schema import is_unified_schema_ready


def test_operblock_schema_creates_required_started_at_archive_index():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        ensure_unified_schema(conn)
        _apply_operblock_schema(conn.cursor())
        conn.commit()

        index_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name = ?",
            ("idx_operation_cases_started_at_id",),
        ).fetchone()
        assert index_row is not None
        assert "started_at" in str(index_row["sql"])
        assert is_operblock_schema_ready(conn)

        conn.execute("DROP INDEX idx_operation_cases_started_at_id")
        assert not is_unified_schema_ready(conn)
        assert not is_operblock_schema_ready(conn)
        ensure_unified_schema(conn)
        assert is_unified_schema_ready(conn)
        assert is_operblock_schema_ready(conn)
    finally:
        conn.close()
