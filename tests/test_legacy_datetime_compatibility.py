from __future__ import annotations

import ast
import re
import sqlite3
import warnings
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from rem_card.app.unified_db_schema import ensure_unified_schema
from rem_card.app.durable_sql_outbox import DurableSqlOutbox
from rem_card.app import durable_sql_outbox
from rem_card.data.dao.fluids_dao import FluidsDAO
from rem_card.services.remcard_facade import RemCardService


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPORAL_FIELD = (
    r"(?:datetime|planned_time|actual_time|scheduled_at|start_time|end_time|"
    r"event_time|started_at|ended_at|admission_datetime|transfer_datetime|"
    r"death_datetime|shift_start|operation_datetime|timestamp|created_at|"
    r"updated_at|completed_at|finished_at|removed_at|released_at|assigned_at|"
    r"changed_at|applied_at)"
)
RAW_TEMPORAL_COMPARISON = re.compile(
    rf"(?i)(?<![\w(])(?:\b\w+\.)?[\"\[]?{TEMPORAL_FIELD}[\"\]]?"
    r"\s*(?:>=|<=|>|<|BETWEEN\b)"
)
RAW_TEMPORAL_ORDER = re.compile(
    rf"(?i)(?<![\w.])(?:\w+\.)?[\"\[]?{TEMPORAL_FIELD}[\"\]]?\s+(?:ASC|DESC)(?!\w)"
)
RAW_TEMPORAL_AGGREGATE = re.compile(
    rf"(?i)\b(?:MIN|MAX)\s*\(\s*(?:\w+\.)?[\"\[]?{TEMPORAL_FIELD}[\"\]]?\s*\)"
)


class _MemoryDb:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def fetch_all_remcard(self, query, params=()):
        return self.conn.execute(query, params).fetchall()


def _new_database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_unified_schema(conn)
    conn.execute("INSERT INTO patients (id, full_name) VALUES (1, 'Тест')")
    conn.execute(
        "INSERT INTO admissions (id, patient_id, bed_number, history_number, admission_datetime) VALUES (1, 1, 1, '1', '2026-01-01 08:00:00')"
    )
    return conn


def test_card_queries_accept_space_and_t_datetime_rows_together():
    conn = _new_database()
    try:
        conn.executemany(
            "INSERT INTO fluids (admission_id, datetime, urine) VALUES (1, ?, ?)",
            (
                ("2026-01-02 09:00:00", 10),
                ("2026-01-02T10:00:00", 20),
            ),
        )
        dao = FluidsDAO(_MemoryDb(conn))
        rows = dao.get_fluids(
            1,
            datetime.fromisoformat("2026-01-02T08:00:00"),
            datetime.fromisoformat("2026-01-03T08:00:00"),
        )
        assert [row.urine for row in rows] == [10, 20]

        fake_service = SimpleNamespace(
            orders_dao=SimpleNamespace(db=_MemoryDb(conn)),
            _lab_orders=SimpleNamespace(card_day_id_from_shift_start=lambda _start: "2026-01-02"),
            get_day_period=lambda _date: (
                datetime.fromisoformat("2026-01-02T08:00:00"),
                datetime.fromisoformat("2026-01-03T08:00:00"),
            ),
        )
        assert RemCardService.has_cards_bulk(fake_service, [1], datetime(2026, 1, 2)) == {1: True}
    finally:
        conn.close()


def test_sql_time_ranges_cannot_compare_mixed_text_formats_lexicographically():
    violations: list[str] = []
    for base_name in ("app", "data", "services"):
        for path in (PROJECT_ROOT / base_name).rglob("*.py"):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                sql = node.value
                for pattern in (
                    RAW_TEMPORAL_COMPARISON,
                    RAW_TEMPORAL_ORDER,
                    RAW_TEMPORAL_AGGREGATE,
                ):
                    if pattern is RAW_TEMPORAL_ORDER and "CREATE INDEX" in sql.upper():
                        continue
                    if (
                        pattern is RAW_TEMPORAL_COMPARISON
                        and "REMCARD_MIXED_DATETIME_INDEXED_RANGE" in sql
                    ):
                        continue
                    if "REMCARD_NUMERIC_EPOCH_TIME" in sql:
                        continue
                    for match in pattern.finditer(sql):
                        violations.append(
                            f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}: {match.group(0).strip()}"
                        )
    assert violations == [], "Wrap temporal SQL comparisons with DATETIME/STRFTIME:\n" + "\n".join(violations)


def test_numeric_epoch_outbox_timestamps_keep_numeric_age_semantics(tmp_path, monkeypatch):
    outbox = DurableSqlOutbox(str(tmp_path / "outbox.sqlite"))
    conn = sqlite3.connect(outbox.db_path)
    try:
        conn.execute(
            """
            INSERT INTO outbox_ops (
                op_id, source, payload_json, status, attempts,
                next_retry_at, created_at, updated_at, last_error
            ) VALUES ('op-1', 'test', '[]', 'pending', 0, 0, 1000.25, 1000.25, NULL)
            """
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(durable_sql_outbox.time, "time", lambda: 1120.75)
    snapshot = outbox.get_health_snapshot()
    assert snapshot["oldest_pending_age_sec"] == pytest.approx(120.5)
    assert snapshot["newest_pending_age_sec"] == pytest.approx(120.5)
