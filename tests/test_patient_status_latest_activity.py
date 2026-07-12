from __future__ import annotations

import sqlite3
import unittest
from contextlib import contextmanager
from datetime import datetime

from rem_card.data.dao.patient_status_dao import PatientStatusDAO


class _MemoryDb:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.fetch_queries: list[str] = []
        self.scope_entries = 0
        self.conn.executescript(
            """
            CREATE TABLE patients (
                id INTEGER PRIMARY KEY,
                birth_date TEXT,
                last_name TEXT,
                first_name TEXT,
                middle_name TEXT,
                full_name TEXT
            );
            CREATE TABLE admissions (
                id INTEGER PRIMARY KEY,
                patient_id INTEGER,
                admission_datetime TEXT,
                department_profile TEXT,
                source_department TEXT,
                history_number TEXT,
                patient_age INTEGER,
                patient_months INTEGER,
                patient_age_unit TEXT,
                patient_gender TEXT,
                transfer_datetime TEXT,
                transfer_department TEXT,
                transfer_lpu TEXT,
                transfer_lpu_other TEXT,
                death_datetime TEXT,
                clinical_death_datetime TEXT,
                cardiac_arrest_cause TEXT,
                cardiac_arrest_measures_json TEXT,
                outcome TEXT,
                revision INTEGER DEFAULT 0
            );
            CREATE TABLE patient_status_events (
                id INTEGER PRIMARY KEY,
                admission_id INTEGER,
                status TEXT,
                start_time TEXT,
                end_time TEXT,
                revision INTEGER DEFAULT 0
            );
            CREATE TABLE vitals (id INTEGER PRIMARY KEY, admission_id INTEGER, datetime TEXT);
            CREATE TABLE fluids (id INTEGER PRIMARY KEY, admission_id INTEGER, datetime TEXT);
            CREATE TABLE oral_intake_events (id INTEGER PRIMARY KEY, admission_id INTEGER, event_time TEXT);
            CREATE TABLE orders (id INTEGER PRIMARY KEY, admission_id INTEGER);
            CREATE TABLE administrations (
                id INTEGER PRIMARY KEY,
                order_id INTEGER,
                actual_time TEXT,
                is_committed INTEGER,
                status TEXT
            );
            CREATE TABLE transfusions (id INTEGER PRIMARY KEY, admission_id INTEGER, datetime TEXT);
            CREATE TABLE clinical_events (
                id INTEGER PRIMARY KEY,
                admission_id INTEGER,
                ivl_episode_id INTEGER,
                timestamp TEXT,
                event_type TEXT,
                mode TEXT,
                parameters_json TEXT,
                data TEXT
            );
            CREATE TABLE respiratory_support (
                id INTEGER PRIMARY KEY,
                admission_id INTEGER,
                ivl_episode_id INTEGER,
                datetime TEXT,
                mode TEXT,
                parameters_json TEXT,
                fio2 REAL,
                peep REAL,
                tv REAL,
                rr INTEGER
            );
            CREATE TABLE lab_data (id INTEGER PRIMARY KEY, admission_id INTEGER, datetime TEXT);
            CREATE TABLE ivl_episodes (
                id INTEGER PRIMARY KEY,
                admission_id INTEGER,
                episode_number INTEGER,
                start_time TEXT,
                end_time TEXT,
                is_active INTEGER
            );
            CREATE TABLE devices (
                id INTEGER PRIMARY KEY,
                admission_id INTEGER,
                insertion_date TEXT,
                removal_date TEXT
            );

            INSERT INTO patients VALUES (1, '1980-01-01', 'Иванов', 'Иван', '', 'Иванов Иван');
            INSERT INTO admissions (
                id, patient_id, admission_datetime, history_number, revision
            ) VALUES (1, 1, '2025-01-01T08:00:00', '42', 3);
            INSERT INTO patient_status_events VALUES (
                1, 1, 'ACTIVE', '2025-01-01T08:00:00', NULL, 2
            );

            INSERT INTO vitals VALUES (1, 1, '2025-01-01T09:00:00');
            INSERT INTO fluids VALUES (1, 1, '2025-01-01 10:00:00');
            INSERT INTO oral_intake_events VALUES (1, 1, '2025-01-01T11:00:00');
            INSERT INTO orders VALUES (1, 1);
            INSERT INTO administrations VALUES (1, 1, '2025-01-01T12:00:00', 1, 'done');
            INSERT INTO administrations VALUES (2, 1, '2025-01-01T23:00:00', 0, 'done');
            INSERT INTO administrations VALUES (3, 1, '2025-01-01T22:00:00', 1, 'deleted');
            INSERT INTO transfusions VALUES (1, 1, '2025-01-01T13:00:00');
            INSERT INTO clinical_events VALUES (
                1, 1, NULL, '2025-01-01T14:00:00', 'NOTE', NULL, NULL, NULL
            );
            INSERT INTO respiratory_support VALUES (
                1, 1, NULL, '2025-01-01T15:00:00', NULL, NULL, NULL, NULL, NULL, NULL
            );
            INSERT INTO lab_data VALUES (1, 1, '2025-01-01T16:00:00');
            INSERT INTO ivl_episodes VALUES (
                1, 1, 1, '2025-01-01T17:00:00', '2025-01-01T18:00:00', 0
            );
            INSERT INTO devices VALUES (
                1, 1, '2025-01-01T19:00:00', '2025-01-01T20:00:00'
            );
            """
        )

    @contextmanager
    def central_read_scope(self, _source="snapshot"):
        self.scope_entries += 1
        yield self

    def fetch_one_remcard(self, query, params=()):
        self.fetch_queries.append(query)
        return self.conn.execute(query, params).fetchone()

    def close(self):
        self.conn.close()


class _FailingActivityDb(_MemoryDb):
    def fetch_one_remcard(self, query, params=()):
        if "AS activity" in query:
            raise sqlite3.OperationalError("simulated read failure")
        return super().fetch_one_remcard(query, params)


class PatientStatusLatestActivityTest(unittest.TestCase):
    def setUp(self):
        self.db = _MemoryDb()
        self.dao = PatientStatusDAO(self.db)

    def tearDown(self):
        self.db.close()

    def test_all_sources_are_reduced_by_one_query(self):
        latest = self.dao.get_latest_patient_activity_datetime(1)

        self.assertEqual(latest, datetime(2025, 1, 1, 20, 0))
        self.assertEqual(len(self.db.fetch_queries), 1)
        self.assertGreaterEqual(self.db.fetch_queries[0].count("UNION ALL"), 11)

    def test_transaction_cursor_uses_the_same_query_without_db_fetch(self):
        latest = self.dao.get_latest_patient_activity_datetime(1, cursor=self.db.conn.cursor())

        self.assertEqual(latest, datetime(2025, 1, 1, 20, 0))
        self.assertEqual(self.db.fetch_queries, [])

    def test_outcome_context_reuses_one_central_read_scope(self):
        context = self.dao.get_admission_outcome_context(1)

        self.assertEqual(self.db.scope_entries, 1)
        self.assertEqual(context["latest_activity_datetime"], "2025-01-01T20:00:00")
        self.assertEqual(context["current_status_id"], 1)
        self.assertEqual(sum("AS activity" in query for query in self.db.fetch_queries), 1)

    def test_latest_activity_read_failure_is_not_silenced(self):
        failing_db = _FailingActivityDb()
        try:
            dao = PatientStatusDAO(failing_db)
            with self.assertRaises(sqlite3.OperationalError):
                dao.get_latest_patient_activity_datetime(1)
        finally:
            failing_db.close()


if __name__ == "__main__":
    unittest.main()
