from __future__ import annotations

import sqlite3
from datetime import datetime
from types import SimpleNamespace

import pytest

from rem_card.app.unified_db_schema import (
    SCHEMA_FASTPATH_META_KEY,
    SCHEMA_FASTPATH_REV,
    ensure_unified_schema,
    is_unified_schema_ready,
)
from rem_card.services.order_service import OrderService


class _MemoryDb:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.last_query = ""
        self.last_params = ()

    def fetch_all_remcard(self, query, params=(), **_kwargs):
        self.last_query = query
        self.last_params = params
        return self.conn.execute(query, params).fetchall()


def _service_with_schema():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_unified_schema(conn)
    db = _MemoryDb(conn)
    service = OrderService(SimpleNamespace(db=db))
    conn.execute(
        "INSERT INTO orders (id, admission_id, datetime, text, status, is_committed) "
        "VALUES (1, 7, '2026-09-28T08:00:00', 'test', 'active', 1)"
    )
    return conn, db, service


@pytest.mark.parametrize('expression_index', [True, False])
def test_latest_committed_query_preserves_datetime_grouping_and_uses_expression_index(expression_index):
    conn, db, service = _service_with_schema()
    try:
        if not expression_index:
            conn.execute('DROP INDEX idx_admin_committed_planned_expr_order_id')
        conn.executemany(
            """
            INSERT INTO administrations
                (order_id, cell_role, planned_time, status, comment, is_committed)
            VALUES (1, 'single', ?, ?, ?, ?)
            """,
            (
                ("2026-09-28T09:00:00", "planned", "old", 1),
                ("2026-09-28 09:00:00", "planned", "latest committed", 1),
                ("2026-09-28T09:00:00", "deleted", "draft", 0),
                ("2026-09-29T08:00:00", "planned", "upper bound", 1),
            ),
        )

        rows = service.get_latest_admin_rows_for_order_ids(
            start_dt=datetime(2026, 9, 28, 8),
            end_dt=datetime(2026, 9, 29, 8),
            admission_id=7,
            only_committed=True,
        )

        assert [row["comment"] for row in rows] == ["latest committed"]
        assert db.last_query.count("DATETIME(a2.planned_time)") >= 3
        assert "DATETIME(a.planned_time) >=" not in db.last_query

        plan = conn.execute(
            f"EXPLAIN QUERY PLAN {db.last_query}",
            db.last_params,
        ).fetchall()
        details = "\n".join(str(row[3]) for row in plan)
        assert ("idx_admin_committed_planned_expr_order_id" in details) is expression_index

        rows = service.get_latest_admin_rows_for_order_ids(
            start_dt=datetime(2026, 9, 28, 8),
            end_dt=datetime(2026, 9, 29, 8),
            admission_id=7,
            include_deleted=True,
        )
        assert [row["comment"] for row in rows] == ["draft"]
    finally:
        conn.close()


def test_expression_index_migration_is_idempotent_and_does_not_block_writes():
    conn, _db, _service = _service_with_schema()
    try:
        conn.execute("DROP INDEX idx_admin_committed_planned_expr_order_id")
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = ?",
            (str(SCHEMA_FASTPATH_REV - 1), SCHEMA_FASTPATH_META_KEY),
        )
        assert not is_unified_schema_ready(conn)

        ensure_unified_schema(conn)
        ensure_unified_schema(conn)

        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        assert "idx_admin_committed_planned_expr_order_id" in indexes
        conn.execute(
            """
            INSERT INTO administrations
                (order_id, cell_role, planned_time, status, is_committed)
            VALUES (1, 'single', '2026-09-28T10:00:00', 'planned', 1)
            """
        )
        assert conn.execute("SELECT COUNT(*) FROM administrations").fetchone()[0] == 1
        assert is_unified_schema_ready(conn)
    finally:
        conn.close()
