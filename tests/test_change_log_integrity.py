from __future__ import annotations

import sqlite3
from datetime import datetime

from rem_card.app.unified_db_schema import ensure_unified_schema


UPDATED_AT_CHANGE_TABLES = (
    "vitals",
    "vital_settings",
    "fluids",
    "orders",
    "administrations",
    "patient_status_events",
    "diet_templates",
    "diet_plan",
    "oral_intake_events",
    "procedures",
    "lab_orders",
)


def _unified_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    ensure_unified_schema(conn)
    return conn


def test_updated_at_change_triggers_have_single_event_gate():
    conn = _unified_connection()
    try:
        for table_name in UPDATED_AT_CHANGE_TABLES:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                (f"trg_{table_name}_version_upd",),
            ).fetchone()
            assert row is not None, table_name
            assert "WHEN OLD.updated_at IS NOT NEW.updated_at" in str(row[0]), table_name
    finally:
        conn.close()


def test_one_vitals_update_creates_one_change_log_row():
    conn = _unified_connection()
    try:
        conn.execute("INSERT INTO patients (id, full_name) VALUES (1, 'Тест')")
        conn.execute(
            """
            INSERT INTO admissions (
                id,
                patient_id,
                bed_number,
                history_number,
                admission_datetime
            )
            VALUES (1, 1, 1, '1', ?)
            """,
            (datetime.now().isoformat(),),
        )
        conn.execute(
            """
            INSERT INTO vitals (admission_id, datetime, sys, dia, spo2)
            VALUES (1, ?, 120, 80, 98)
            """,
            (datetime.now().isoformat(),),
        )
        conn.execute("DELETE FROM change_log")

        conn.execute("UPDATE vitals SET sys = 121 WHERE admission_id = 1")

        rows = conn.execute(
            """
            SELECT entity_name, entity_id, admission_id, action
            FROM change_log
            ORDER BY id
            """
        ).fetchall()
        assert rows == [("vitals", 1, 1, "update")]

        conn.execute("DELETE FROM change_log")
        conn.execute("UPDATE vitals SET sys = 122 WHERE admission_id = 1")
        assert conn.execute(
            "SELECT COUNT(*) FROM change_log WHERE entity_name = 'vitals'"
        ).fetchone()[0] == 1

        conn.execute("DELETE FROM change_log")
        conn.execute(
            """
            UPDATE vitals
            SET sys = 123,
                updated_at = '2026-08-28T12:00:00.000'
            WHERE admission_id = 1
            """
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM change_log WHERE entity_name = 'vitals'"
        ).fetchone()[0] == 1
    finally:
        conn.close()
