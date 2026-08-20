from __future__ import annotations

import sqlite3
import unittest

from rem_card.app.unified_db_schema import (
    SCHEMA_FASTPATH_META_KEY,
    SCHEMA_FASTPATH_REV,
    SCHEMA_MIN_MIGRATION_VERSION,
    ensure_unified_schema,
    is_unified_schema_ready,
)
from rem_card.data.dao.vitals_dao import VitalsDAO


class _MemoryDb:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def fetch_all_remcard(self, query, params=()):
        return self.conn.execute(query, params).fetchall()


class VitalsQueryIndexTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_unified_schema(self.conn)
        self.dao = VitalsDAO(_MemoryDb(self.conn))

    def tearDown(self):
        self.conn.close()

    def test_bulk_latest_values_keep_epoch_and_id_ordering(self):
        self.conn.executemany(
            """
            INSERT INTO vitals (
                admission_id, datetime, sys, dia, pulse, temp, spo2, rr, cvp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (1, "2025-01-01T10:00:00", 100, 60, 70, None, None, None, None),
                (1, "2025-01-01T11:00:00", None, None, 75, 36.5, 97, 18, None),
                # Space and T formats must be compared by time, not lexicographically.
                (1, "2025-01-01 12:00:00", 110, 65, None, None, None, None, 5),
                (1, "2025-01-01T12:00:00", 120, 70, None, None, None, None, 6),
                (2, "2025-01-02T09:00:00", 130, 80, 90, 37.1, 96, 20, 7),
            ),
        )

        values = self.dao.get_latest_vital_values_bulk([1, 2, 3, 1])

        self.assertEqual(
            values[1],
            {
                "sys": 120,
                "dia": 70,
                "pulse": 75,
                "temp": 36.5,
                "spo2": 97,
                "rr": 18,
                "cvp": 6,
            },
        )
        self.assertEqual(values[2]["sys"], 130)
        self.assertTrue(all(value is None for value in values[3].values()))

    def test_expression_index_removes_latest_value_sort(self):
        plan = self.conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT sys
            FROM vitals
            WHERE admission_id = ? AND sys IS NOT NULL
            ORDER BY CAST(STRFTIME('%s', datetime) AS INTEGER) DESC, id DESC
            LIMIT 1
            """,
            (1,),
        ).fetchall()
        details = "\n".join(str(row[3]) for row in plan)

        self.assertIn("idx_vitals_admission_epoch_id", details)
        self.assertNotIn("TEMP B-TREE FOR ORDER BY", details)

    def test_schema_upgrade_restores_required_query_indexes(self):
        for index_name in (
            "idx_vitals_admission_epoch_id",
            "idx_resp_support_admission_time",
            "idx_lab_data_admission_time",
            "idx_devices_admission",
        ):
            self.conn.execute(f"DROP INDEX {index_name}")
        self.conn.execute(
            "DELETE FROM schema_migrations WHERE version = ?",
            (SCHEMA_MIN_MIGRATION_VERSION,),
        )
        self.conn.execute(
            "UPDATE meta SET value = ? WHERE key = ?",
            (str(SCHEMA_FASTPATH_REV - 1), SCHEMA_FASTPATH_META_KEY),
        )
        self.assertFalse(is_unified_schema_ready(self.conn))

        ensure_unified_schema(self.conn)
        ensure_unified_schema(self.conn)

        indexes = {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        self.assertTrue(
            {
                "idx_vitals_admission_epoch_id",
                "idx_resp_support_admission_time",
                "idx_lab_data_admission_time",
                "idx_devices_admission",
            }.issubset(indexes)
        )
        migration = self.conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (SCHEMA_MIN_MIGRATION_VERSION,),
        ).fetchone()
        self.assertIsNotNone(migration)
        self.assertTrue(is_unified_schema_ready(self.conn))


if __name__ == "__main__":
    unittest.main()
