from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from rem_card.services.remcard_facade import RemCardService
from rem_card.services.shift_service import ShiftService


class _MemoryDb:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.fetch_count = 0
        self.conn.executescript(
            """
            CREATE TABLE vitals (id INTEGER PRIMARY KEY, admission_id INTEGER, datetime TEXT);
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                admission_id INTEGER,
                datetime TEXT,
                status TEXT
            );
            CREATE TABLE fluids (id INTEGER PRIMARY KEY, admission_id INTEGER, datetime TEXT);
            CREATE TABLE diet_plan (id INTEGER PRIMARY KEY, admission_id INTEGER, shift_start TEXT);
            CREATE TABLE oral_intake_events (
                id INTEGER PRIMARY KEY,
                admission_id INTEGER,
                event_time TEXT
            );
            CREATE TABLE lab_orders (
                id INTEGER PRIMARY KEY,
                admission_id INTEGER,
                scheduled_at TEXT,
                created_at TEXT,
                completed_at TEXT
            );
            """
        )

    def fetch_all_remcard(self, query, params=()):
        self.fetch_count += 1
        return self.conn.execute(query, params).fetchall()

    def close(self):
        self.conn.close()


class _RemCardHarness:
    get_all_card_dates = RemCardService.get_all_card_dates

    def __init__(self, db: _MemoryDb):
        self.orders_dao = SimpleNamespace(db=db)
        self._shifts = ShiftService()


class CardDatesQueryTest(unittest.TestCase):
    def setUp(self):
        self.db = _MemoryDb()
        self.service = _RemCardHarness(self.db)

    def tearDown(self):
        self.db.close()

    def test_all_sources_are_grouped_in_one_query_at_shift_boundaries(self):
        self.db.conn.executescript(
            """
            INSERT INTO vitals VALUES (1, 1, '2025-01-01T07:59:59.900000');
            INSERT INTO fluids VALUES (1, 1, '2025-01-01 08:00:00');
            INSERT INTO orders VALUES (1, 1, '2025-01-02T07:59:00', 'active');
            INSERT INTO orders VALUES (2, 1, '2025-01-05T08:00:00', 'deleted');
            INSERT INTO diet_plan VALUES (1, 1, '2025-01-02 08:00:00');
            INSERT INTO oral_intake_events VALUES (1, 1, '2025-01-03 07:00:00');
            INSERT INTO lab_orders VALUES (
                1, 1, NULL, '2025-01-03T08:00:00', '2025-01-04T08:00:00'
            );
            INSERT INTO vitals VALUES (2, 1, '2025-01-04T08:00:00.123456');
            """
        )

        dates = self.service.get_all_card_dates(1)

        self.assertEqual(
            dates,
            [
                datetime(2024, 12, 31, 8, 0),
                datetime(2025, 1, 1, 8, 0),
                datetime(2025, 1, 2, 8, 0),
                datetime(2025, 1, 3, 8, 0),
                datetime(2025, 1, 4, 8, 0),
            ],
        )
        self.assertEqual(self.db.fetch_count, 1)

    def test_timezone_iso_is_routed_through_python_compatibility_parser(self):
        self.db.conn.execute(
            "INSERT INTO vitals VALUES (?, ?, ?)",
            (1, 1, "2025-01-01T07:59:00+10:00"),
        )

        dates = self.service.get_all_card_dates(1)

        self.assertEqual(
            dates,
            [datetime(2024, 12, 31, 8, 0, tzinfo=timezone(timedelta(hours=10)))],
        )

    def test_python_only_basic_iso_format_is_preserved(self):
        self.db.conn.execute(
            "INSERT INTO fluids VALUES (?, ?, ?)",
            (1, 1, "20250101T075900"),
        )

        dates = self.service.get_all_card_dates(1)

        self.assertEqual(dates, [datetime(2024, 12, 31, 8, 0)])

    def test_invalid_optional_sources_are_ignored(self):
        self.db.conn.execute(
            "INSERT INTO diet_plan VALUES (?, ?, ?)",
            (1, 1, "not-a-date"),
        )
        self.db.conn.execute(
            "INSERT INTO lab_orders VALUES (?, ?, ?, ?, ?)",
            (1, 1, "also-not-a-date", None, None),
        )

        self.assertEqual(self.service.get_all_card_dates(1), [])
        self.assertEqual(self.db.fetch_count, 1)

    def test_invalid_strict_source_still_fails_fast(self):
        self.db.conn.execute(
            "INSERT INTO vitals VALUES (?, ?, ?)",
            (1, 1, "not-a-date"),
        )

        with self.assertRaises(ValueError):
            self.service.get_all_card_dates(1)
        self.assertEqual(self.db.fetch_count, 1)


if __name__ == "__main__":
    unittest.main()
