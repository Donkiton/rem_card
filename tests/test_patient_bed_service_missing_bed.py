from __future__ import annotations

import sqlite3
import sys
import threading
import time
import unittest
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.services.patient_bed_management.service import PatientBedManagementService  # noqa: E402


class SQLiteWriteDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(
            """
            CREATE TABLE patients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                admission_uid TEXT,
                birth_date TEXT
            );
            CREATE TABLE admissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id INTEGER NOT NULL,
                bed_number INTEGER NOT NULL,
                history_number TEXT NOT NULL,
                admission_datetime TEXT NOT NULL,
                patient_age INTEGER,
                patient_months INTEGER,
                patient_age_unit TEXT,
                patient_gender TEXT,
                diagnosis_code TEXT,
                diagnosis_text TEXT,
                department_profile TEXT,
                source_department TEXT,
                recovery_bed_stay INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT,
                revision INTEGER DEFAULT 0,
                FOREIGN KEY (patient_id) REFERENCES patients(id)
            );
            CREATE TABLE beds (
                bed_number INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                current_admission_id INTEGER,
                revision INTEGER DEFAULT 0,
                FOREIGN KEY (current_admission_id) REFERENCES admissions(id)
            );
            """
        )

    def run_write_operation(self, operation, source: str = ""):
        del source
        cursor = self.conn.cursor()
        try:
            result = operation(cursor)
            self.conn.commit()
            return result
        except Exception:
            self.conn.rollback()
            raise


def admission_data(bed_number: int) -> dict:
    return {
        "bed_number": bed_number,
        "history_number": "ИБ-1",
        "admission_datetime": datetime(2026, 7, 13, 8, 30),
        "patient_age": 40,
        "patient_months": None,
        "patient_age_unit": "years",
        "patient_gender": "male",
        "diagnosis_code": "A00",
        "diagnosis_text": "Холера",
        "department_profile": "ОАР",
        "source_department": "Приёмное отделение",
    }


class PatientBedServiceMissingBedTest(unittest.TestCase):
    def setUp(self):
        self.db = SQLiteWriteDB()
        self.service = PatientBedManagementService(self.db)

    def tearDown(self):
        self.db.conn.close()

    def test_create_admission_initializes_missing_bed(self):
        admission_id = self.service.create_patient_and_admission(
            {"full_name": "Новый пациент", "birth_date": date(1986, 1, 1)},
            admission_data(2),
        )

        bed = self.db.conn.execute(
            "SELECT status, current_admission_id, revision FROM beds WHERE bed_number = 2"
        ).fetchone()

        self.assertIsNotNone(bed)
        self.assertEqual(bed["status"], "OCCUPIED")
        self.assertEqual(bed["current_admission_id"], admission_id)
        self.assertEqual(bed["revision"], 1)

    def test_move_admission_initializes_missing_target_bed(self):
        admission_id = self.service.create_patient_and_admission(
            {"full_name": "Перемещаемый пациент", "birth_date": date(1986, 1, 1)},
            admission_data(1),
        )

        moved = self.service.move_patient(
            1,
            2,
            expected_source_bed_revision=1,
            expected_target_bed_revision=0,
            expected_source_admission_revision=0,
        )

        source_bed = self.db.conn.execute(
            "SELECT status, current_admission_id FROM beds WHERE bed_number = 1"
        ).fetchone()
        target_bed = self.db.conn.execute(
            "SELECT status, current_admission_id FROM beds WHERE bed_number = 2"
        ).fetchone()
        admission = self.db.conn.execute(
            "SELECT bed_number FROM admissions WHERE id = ?",
            (admission_id,),
        ).fetchone()

        self.assertTrue(moved)
        self.assertEqual(dict(source_bed), {"status": "FREE", "current_admission_id": None})
        self.assertEqual(dict(target_bed), {"status": "OCCUPIED", "current_admission_id": admission_id})
        self.assertEqual(admission["bed_number"], 2)

    def test_create_admission_still_rejects_occupied_bed(self):
        patient_id = self.db.conn.execute(
            "INSERT INTO patients (full_name) VALUES ('Текущий пациент')"
        ).lastrowid
        current_admission_id = self.db.conn.execute(
            """
            INSERT INTO admissions (
                patient_id, bed_number, history_number, admission_datetime
            ) VALUES (?, 2, 'ИБ-0', '2026-07-13 08:00:00')
            """,
            (patient_id,),
        ).lastrowid
        self.db.conn.execute(
            """
            INSERT INTO beds (bed_number, status, current_admission_id, revision)
            VALUES (2, 'OCCUPIED', ?, 4)
            """,
            (current_admission_id,),
        )
        self.db.conn.commit()

        with self.assertRaisesRegex(RuntimeError, "Койка 2 уже занята"):
            self.service.create_patient_and_admission(
                {"full_name": "Другой пациент", "birth_date": date(1990, 1, 1)},
                admission_data(2),
            )

        counts = {
            table: self.db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("patients", "admissions", "beds")
        }
        self.assertEqual(counts, {"patients": 1, "admissions": 1, "beds": 1})


class _BudgetedPatientDatabase:
    """Real SQLite + production DBManager/controller, without application boot."""

    def __init__(self, path, *, retry_delay_ms=10):
        from rem_card.app.sqlite_shared import SQLiteWriteController, configure_connection
        from rem_card.data.dao.db_manager import DatabaseManager

        seed = SQLiteWriteDB()
        try:
            schema = "\n".join(seed.conn.iterdump())
        finally:
            seed.conn.close()
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(schema)
        configure_connection(self.conn, profile="network")
        self.statements, self.attempt_options = [], []
        self.conn.set_trace_callback(self.statements.append)
        self.manager = object.__new__(DatabaseManager)
        manager = self.manager
        manager.db_path = str(path)
        manager._thread_state = threading.local()
        manager._central_io_lock = threading.RLock()
        manager._closed, manager._remcard_conn = False, self.conn
        manager.runtime_context = SimpleNamespace(mode="")
        manager._request_periodic_backup = lambda **_kwargs: None
        manager._after_write_committed = lambda: None
        manager._journal_conn = self.conn
        manager.write_controller = SQLiteWriteController(
            str(path), str(path) + ".lock", "patient-test", retry_delay_ms=retry_delay_ms,
            on_connection_quarantined=manager._on_write_connection_quarantined,
        )
        transaction = manager.write_controller.transaction

        def observed(conn, *args, **kwargs):
            self.attempt_options.append(dict(kwargs.get("write_options") or {}))
            return transaction(conn, *args, **kwargs)

        manager.write_controller.transaction = observed

    def close(self):
        for conn in {self.conn, self.manager._remcard_conn}:
            conn.close()


@pytest.fixture
def budgeted_patient_db(tmp_path):
    db = _BudgetedPatientDatabase(tmp_path / "patient.db")
    try:
        yield db
    finally:
        db.close()


def _create_fixture_patient(service):
    return service.create_patient_and_admission(
        {"full_name": "Synthetic fixture", "birth_date": date(1986, 1, 1)}, admission_data(2),
    )


@pytest.mark.parametrize("kind", ["create", "update"])
@pytest.mark.parametrize("block", ["begin", "commit"])
def test_patient_save_real_busy_has_one_bounded_transaction_and_restores_connection(budgeted_patient_db, kind, block):
    from rem_card.app.sqlite_shared import QueuedWriteDeadlineExceeded

    db = budgeted_patient_db
    service = PatientBedManagementService(db.manager)
    admission_id = _create_fixture_patient(service) if kind == "update" else None
    service.PATIENT_FORM_WRITE_BUDGET_MS = 120
    previous = [tuple(row) for row in db.conn.execute("SELECT * FROM patients")]
    holder = sqlite3.connect(db.manager.db_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE" if block == "begin" else "BEGIN")
    holder.execute("SELECT * FROM patients").fetchall()
    db.statements.clear()
    db.attempt_options.clear()
    try:
        with pytest.raises(QueuedWriteDeadlineExceeded):
            if kind == "create":
                _create_fixture_patient(service)
            else:
                service.update_patient_and_admission(
                    1, admission_id, {"full_name": "Changed fixture", "birth_date": date(1986, 1, 1)},
                    admission_data(2), expected_admission_revision=0,
                )
        assert len(db.attempt_options) == 1
        assert db.attempt_options[0]["write_budget_ms"] == 120
        assert not Path(db.manager.write_controller.lock_path).exists()
        if block == "commit":
            assert not db.conn.in_transaction
            assert db.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
            assert "BEGIN IMMEDIATE" in db.statements and "COMMIT" in db.statements and "ROLLBACK" in db.statements
        else:
            assert db.manager._quarantined_write_conn is db.conn and db.manager._quarantined_write_closed
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                _ = db.conn.in_transaction
            assert not any(sql.startswith("INSERT") or sql.startswith("UPDATE") for sql in db.statements)
        with closing(sqlite3.connect(db.manager.db_path)) as check:
            assert check.execute("SELECT * FROM patients").fetchall() == previous
    finally:
        holder.rollback()
        holder.close()
    # The same connection remains usable after a confirmed failure; no replay
    # happened before this explicit second user action.
    if kind == "create":
        assert _create_fixture_patient(service) == 1
    else:
        assert service.update_patient_and_admission(
            1, admission_id, {"full_name": "Changed fixture"}, admission_data(2), expected_admission_revision=0,
        )


def test_patient_save_shares_queue_wait_and_native_budget(budgeted_patient_db):
    from rem_card.app.sqlite_shared import QueuedWriteDeadlineExceeded
    from rem_card.services.data_service import DataService
    from test_unified_write_outcomes import _service_harness

    db = budgeted_patient_db
    data = _service_harness()
    data.db = db.manager
    data._write_metadata_context = MethodType(DataService._write_metadata_context, data)
    service = PatientBedManagementService(db.manager, data)
    service.PATIENT_FORM_WRITE_BUDGET_MS = 200
    blocker_started, release, done = threading.Event(), threading.Event(), threading.Event()
    result, remaining_at_start = [], []
    holder = sqlite3.connect(db.manager.db_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")

    def blocker():
        blocker_started.set()
        assert release.wait(2)

    def save():
        metadata = db.manager._current_thread_write_metadata()
        remaining_at_start.append(metadata["write_deadline_monotonic"] - time.perf_counter())
        return _create_fixture_patient(service)

    try:
        data._queue.submit(blocker, "synthetic queue delay", retryable=False)
        assert blocker_started.wait(1)
        service.enqueue_write("patient_bed_create_admission:2", save,
                              on_settled=lambda state, payload: (result.append((state, payload)), done.set()))
        time.sleep(0.10)
        release.set()
        assert done.wait(2)
        assert data._queue.shutdown(2)
        assert result[0][0] == "failed" and isinstance(result[0][1], QueuedWriteDeadlineExceeded)
        assert 0 < remaining_at_start[0] < 0.15
        assert len(db.attempt_options) == 1
        assert db.attempt_options[0]["write_budget_ms"] == 200
        assert db.manager._quarantined_write_conn is db.conn and db.manager._quarantined_write_closed
        with closing(sqlite3.connect(db.manager.db_path)) as check:
            assert check.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 0
    finally:
        release.set()
        holder.rollback()
        holder.close()
        data.shutdown(timeout=1, application_exit=True)


def test_expired_patient_queue_budget_skips_operation_entirely(budgeted_patient_db):
    from test_unified_write_outcomes import _service_harness

    db = budgeted_patient_db
    data = _service_harness()
    service = PatientBedManagementService(db.manager, data)
    service.PATIENT_FORM_WRITE_BUDGET_MS = 50
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    calls, terminal = [], []

    def blocker():
        entered.set()
        assert release.wait(2)
    try:
        data._queue.submit(blocker, "synthetic delay", retryable=False)
        assert entered.wait(1)
        service.enqueue_write("patient_bed_create_admission:2", lambda: calls.append(1),
                              on_settled=lambda state, payload: (terminal.append(state), done.set()))
        # The second task must settle while the first is still blocked; its
        # expiration callback owns a queued->expired CAS, never an active SQL.
        assert done.wait(1)
        assert calls == [] and terminal == ["failed"]
        assert not data._queue.is_idle()
        release.set()
        assert data._queue.shutdown(2)
        assert calls == [] and terminal == ["failed"] and db.attempt_options == []
    finally:
        release.set()
        data.shutdown(timeout=1, application_exit=True)


def test_direct_patient_scope_preserves_earlier_deadline_and_correlation(budgeted_patient_db):
    from rem_card.app.sqlite_shared import QueuedWriteDeadlineExceeded

    db = budgeted_patient_db
    options = {"write_deadline_monotonic": time.perf_counter() - 1,
               "accepted_write_id": "a" * 32, "write_budget_ms": 40}
    with db.manager.write_metadata_context(options):
        with pytest.raises(QueuedWriteDeadlineExceeded):
            _create_fixture_patient(PatientBedManagementService(db.manager))
        assert db.manager._current_thread_write_metadata() == options
    assert db.manager._current_thread_write_metadata() == {}
    assert db.conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 0


if __name__ == "__main__":
    unittest.main()
