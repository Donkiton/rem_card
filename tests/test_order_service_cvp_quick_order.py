from __future__ import annotations

import sqlite3
import sys
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.data.dao.orders_dao import OrdersDAO  # noqa: E402
from rem_card.services.order_service import (  # noqa: E402
    CVP_QUICK_ORDER_KEY,
    CVP_QUICK_ORDER_TEXT,
    OrderService,
)


class _MemoryDb:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admission_id INTEGER NOT NULL,
                datetime TEXT NOT NULL,
                text TEXT NOT NULL,
                drug_key TEXT,
                latin TEXT,
                type TEXT,
                status TEXT DEFAULT 'active',
                dose_value REAL,
                dose_unit TEXT,
                is_per_kg INTEGER,
                frequency INTEGER,
                specific_times TEXT,
                rate_ml_h REAL,
                volume_total REAL,
                duration_min INTEGER,
                sort_order INTEGER DEFAULT 0,
                draft_sort_order INTEGER,
                is_finalized INTEGER DEFAULT 0,
                is_committed INTEGER DEFAULT 0,
                revision INTEGER DEFAULT 0,
                created_at TEXT,
                comment TEXT,
                last_modified_by TEXT,
                updated_at TEXT
            );
            CREATE TABLE administrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                planned_time TEXT NOT NULL,
                cell_role TEXT NOT NULL,
                status TEXT NOT NULL,
                is_committed INTEGER DEFAULT 0
            );
            """
        )
        self.conn.commit()

    def close(self):
        self.conn.close()

    @contextmanager
    def remcard_transaction(self, source="test"):
        cursor = self.conn.cursor()
        try:
            yield cursor
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def fetch_one_remcard(self, query, params=()):
        return self.conn.execute(query, params).fetchone()

    def fetch_all_remcard(self, query, params=(), *, cancel_check=None):
        return self.conn.execute(query, params).fetchall()


class CvpQuickOrderTest(unittest.TestCase):
    def setUp(self):
        self.db = _MemoryDb()
        self.service = OrderService(OrdersDAO(self.db))

    def tearDown(self):
        self.db.close()

    def test_add_cvp_order_creates_draft_without_times_and_without_duplicates(self):
        shift_date = datetime(2025, 1, 1, 12, 0)

        self.assertFalse(self.service.has_cvp_order(1, shift_date))

        order, created = self.service.add_cvp_order_if_missing(1, shift_date)

        self.assertTrue(created)
        self.assertIsNotNone(order)
        self.assertEqual(order.latin, CVP_QUICK_ORDER_TEXT)
        self.assertEqual(order.drug_key, CVP_QUICK_ORDER_KEY)
        self.assertEqual(order.specific_times, [])
        self.assertEqual(order.frequency, 1)
        self.assertEqual(order.dose_value, 0)
        self.assertEqual(order.dose_unit, "")
        self.assertEqual(order.is_committed, 0)

        row = self.db.fetch_one_remcard("SELECT COUNT(*) AS count FROM orders")
        self.assertEqual(row["count"], 1)
        self.assertTrue(self.service.has_cvp_order(1, shift_date))

    def test_existing_manual_cvp_order_is_reused(self):
        shift_date = datetime(2025, 1, 1, 12, 0)
        self.db.conn.execute(
            """
            INSERT INTO orders (
                admission_id, datetime, text, drug_key, latin, type, status,
                dose_value, dose_unit, is_per_kg, frequency, specific_times,
                sort_order, is_committed, created_at, comment, last_modified_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "2025-01-01T08:00:00",
                f"{CVP_QUICK_ORDER_TEXT} 0",
                "ruchnoivvod",
                CVP_QUICK_ORDER_TEXT,
                "medication",
                "active",
                0.0,
                "",
                0,
                1,
                "[]",
                0,
                1,
                "2025-01-01T08:00:00",
                "",
                "doctor",
            ),
        )
        self.db.conn.commit()

        self.assertTrue(self.service.has_cvp_order(1, shift_date))

        order, created = self.service.add_cvp_order_if_missing(1, shift_date)

        self.assertFalse(created)
        self.assertIsNotNone(order)
        self.assertEqual(order.latin, CVP_QUICK_ORDER_TEXT)
        row = self.db.fetch_one_remcard("SELECT COUNT(*) AS count FROM orders")
        self.assertEqual(row["count"], 1)
        row = self.db.fetch_one_remcard("SELECT specific_times FROM orders WHERE id = ?", (order.id,))
        self.assertEqual(row["specific_times"], "[]")
        row = self.db.fetch_one_remcard("SELECT COUNT(*) AS count FROM administrations")
        self.assertEqual(row["count"], 0)

        same_order, created_again = self.service.add_cvp_order_if_missing(1, shift_date)

        self.assertFalse(created_again)
        self.assertEqual(same_order.id, order.id)
        row = self.db.fetch_one_remcard("SELECT COUNT(*) AS count FROM orders")
        self.assertEqual(row["count"], 1)


if __name__ == "__main__":
    unittest.main()
