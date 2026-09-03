from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from rem_card.data.dao.patient_status_dao import PatientStatusDAO
from rem_card.data.dto.remcard_dto import PatientStatus
from rem_card.services.patient_status_service import PatientStatusService
from rem_card.services.shift_service import ShiftService
from rem_card.ui.rem_card_sectors.sector_events import SectorEvents


class _MemoryDb:
    def __init__(self, source_shift_start: datetime):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE admissions (
                id INTEGER PRIMARY KEY,
                patient_id INTEGER,
                admission_datetime TEXT,
                bed_number INTEGER,
                is_active INTEGER DEFAULT 1,
                outcome TEXT,
                transfer_datetime TEXT,
                transfer_department TEXT,
                transfer_lpu TEXT,
                transfer_lpu_other TEXT,
                death_datetime TEXT,
                clinical_death_datetime TEXT,
                cardiac_arrest_cause TEXT,
                cardiac_arrest_measures_json TEXT,
                updated_at TEXT,
                revision INTEGER DEFAULT 0
            );
            CREATE TABLE patient_status_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admission_id INTEGER,
                status TEXT,
                reason_type TEXT,
                reason_text TEXT,
                start_time TEXT,
                end_time TEXT,
                created_by TEXT,
                created_at TEXT,
                updated_at TEXT,
                last_modified_by TEXT,
                revision INTEGER DEFAULT 0
            );
            CREATE TABLE beds (
                bed_number INTEGER PRIMARY KEY,
                status TEXT,
                current_admission_id INTEGER,
                revision INTEGER DEFAULT 0
            );
            CREATE TABLE vitals (id INTEGER PRIMARY KEY, admission_id INTEGER, datetime TEXT);
            CREATE TABLE fluids (id INTEGER PRIMARY KEY, admission_id INTEGER, datetime TEXT);
            CREATE TABLE orders (id INTEGER PRIMARY KEY, admission_id INTEGER, datetime TEXT);
            CREATE TABLE diet_plan (id INTEGER PRIMARY KEY, admission_id INTEGER, shift_start TEXT);
            CREATE TABLE oral_intake_events (id INTEGER PRIMARY KEY, admission_id INTEGER, event_time TEXT);
            CREATE TABLE lab_orders (
                id INTEGER PRIMARY KEY,
                admission_id INTEGER,
                card_day_id TEXT,
                scheduled_at TEXT,
                created_at TEXT,
                completed_at TEXT
            );
            CREATE TABLE administrations (
                id INTEGER PRIMARY KEY, order_id INTEGER, actual_time TEXT,
                is_committed INTEGER, status TEXT
            );
            CREATE TABLE transfusions (id INTEGER PRIMARY KEY, admission_id INTEGER, datetime TEXT);
            CREATE TABLE clinical_events (
                id INTEGER PRIMARY KEY, admission_id INTEGER, ivl_episode_id INTEGER,
                timestamp TEXT, event_type TEXT, mode TEXT, parameters_json TEXT, data TEXT
            );
            CREATE TABLE respiratory_support (
                id INTEGER PRIMARY KEY, admission_id INTEGER, ivl_episode_id INTEGER,
                datetime TEXT, mode TEXT, parameters_json TEXT, fio2 REAL, peep REAL, tv REAL, rr INTEGER
            );
            CREATE TABLE lab_data (id INTEGER PRIMARY KEY, admission_id INTEGER, datetime TEXT);
            CREATE TABLE ivl_episodes (
                id INTEGER PRIMARY KEY, admission_id INTEGER, episode_number INTEGER,
                start_time TEXT, end_time TEXT, is_active INTEGER
            );
            CREATE TABLE devices (
                id INTEGER PRIMARY KEY, admission_id INTEGER, insertion_date TEXT, removal_date TEXT
            );
            CREATE TABLE procedures (
                id INTEGER PRIMARY KEY, admission_id INTEGER, procedure_type TEXT,
                status TEXT, is_deleted INTEGER, started_at TEXT, finished_at TEXT,
                duration_minutes INTEGER, updated_by TEXT, updated_at TEXT, revision INTEGER DEFAULT 0
            );
            CREATE TABLE procedure_cvc (
                procedure_id INTEGER PRIMARY KEY, catheter_status TEXT,
                removed_or_replaced TEXT, removed_at TEXT, revision INTEGER DEFAULT 0
            );
            """
        )
        stamp = source_shift_start.isoformat()
        self.conn.execute(
            "INSERT INTO admissions(id, patient_id, admission_datetime, bed_number, revision) VALUES (1, 1, ?, 1, 0)",
            (stamp,),
        )
        self.conn.execute(
            "INSERT INTO beds(bed_number, status, current_admission_id) VALUES (1, 'OCCUPIED', 1)"
        )
        self.conn.execute(
            """
            INSERT INTO patient_status_events(
                admission_id, status, start_time, created_by, created_at, updated_at, revision
            ) VALUES (1, 'ACTIVE', ?, 'SYSTEM', ?, ?, 0)
            """,
            (stamp, stamp, stamp),
        )
        self.conn.execute(
            "INSERT INTO vitals(id, admission_id, datetime) VALUES (1, 1, ?)",
            (stamp,),
        )
        self.conn.commit()

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

    def fetch_all_remcard(self, query, params=()):
        return self.conn.execute(query, params).fetchall()

    def close(self):
        self.conn.close()


def _late_window():
    reference = datetime.now().replace(second=0, microsecond=0)
    current_shift_start, _current_shift_end = ShiftService.get_day_period(reference)
    return current_shift_start - timedelta(days=1), reference


def _transfer_details():
    return {
        "transfer_department": "Хирургия",
        "transfer_lpu": None,
        "transfer_lpu_other": None,
    }


def test_late_time_after_0800_is_resolved_into_extension_of_previous_card():
    shift_date = datetime(2026, 8, 26, 8, 0)
    reference = datetime(2026, 8, 27, 8, 10)

    assert ShiftService.resolve_late_card_outcome_datetime(
        "09:00",
        shift_date,
        reference_dt=reference,
        not_before=datetime(2026, 8, 27, 7, 30),
    ) == datetime(2026, 8, 27, 9, 0)
    assert ShiftService.resolve_late_card_outcome_datetime(
        "10:11",
        shift_date,
        reference_dt=reference,
        not_before=datetime(2026, 8, 27, 7, 30),
    ) == datetime(2026, 8, 27, 10, 11)


def test_last_finished_card_is_eligible_only_until_next_card_is_created():
    source_start, reference = _late_window()
    db = _MemoryDb(source_start)
    try:
        dao = PatientStatusDAO(db)
        state = dao.get_late_outcome_card_state(1, source_start, reference_dt=reference)

        assert state["eligible"] is True
        assert state["deadline"] == reference + timedelta(hours=2)

        db.conn.execute(
            "INSERT INTO vitals(id, admission_id, datetime) VALUES (2, 1, ?)",
            ((source_start + timedelta(days=1)).isoformat(),),
        )
        db.conn.commit()

        state = dao.get_late_outcome_card_state(1, source_start, reference_dt=reference)
        assert state["eligible"] is False
        assert state["later_card_exists"] is True
    finally:
        db.close()


def test_transfer_up_to_two_hours_ahead_is_saved_in_existing_admission():
    source_start, reference = _late_window()
    event_time = reference + timedelta(hours=1, minutes=55)
    db = _MemoryDb(source_start)
    try:
        service = PatientStatusService(PatientStatusDAO(db))

        assert service.change_status_with_outcome_details(
            1,
            PatientStatus.TRANSFERRED,
            event_time,
            reason_text="Куда переведен: Хирургия",
            user_id="USER",
            admission_details=_transfer_details(),
            late_card_shift_start=source_start,
            late_outcome_reference_at=reference,
        ) is True

        admission = db.conn.execute(
            "SELECT outcome, transfer_datetime FROM admissions WHERE id = 1"
        ).fetchone()
        assert admission["outcome"] == "переведен"
        assert datetime.fromisoformat(admission["transfer_datetime"]) == event_time
        assert db.conn.execute("SELECT COUNT(*) FROM vitals").fetchone()[0] == 1

        assert service.rollback_last_status(1) is True
        restored = db.conn.execute(
            "SELECT outcome, transfer_datetime, is_active FROM admissions WHERE id = 1"
        ).fetchone()
        assert restored["outcome"] is None
        assert restored["transfer_datetime"] is None
        assert restored["is_active"] == 1
        assert service.get_current_status(1).status == PatientStatus.ACTIVE
    finally:
        db.close()


def test_late_outcome_is_rejected_after_deadline_or_if_next_card_appeared():
    source_start, reference = _late_window()
    db = _MemoryDb(source_start)
    try:
        service = PatientStatusService(PatientStatusDAO(db))
        assert service.change_status_with_outcome_details(
            1,
            PatientStatus.TRANSFERRED,
            reference + timedelta(hours=2, minutes=1),
            admission_details=_transfer_details(),
            late_card_shift_start=source_start,
            late_outcome_reference_at=reference,
        ) is False

        db.conn.execute(
            "INSERT INTO vitals(id, admission_id, datetime) VALUES (2, 1, ?)",
            ((source_start + timedelta(days=1)).isoformat(),),
        )
        db.conn.commit()
        assert service.change_status_with_outcome_details(
            1,
            PatientStatus.TRANSFERRED,
            reference + timedelta(minutes=5),
            admission_details=_transfer_details(),
            late_card_shift_start=source_start,
            late_outcome_reference_at=reference,
        ) is False

        admission = db.conn.execute(
            "SELECT outcome, transfer_datetime FROM admissions WHERE id = 1"
        ).fetchone()
        assert admission["outcome"] is None
        assert admission["transfer_datetime"] is None
    finally:
        db.close()


def test_late_terminal_event_remains_visible_in_source_card_timeline():
    shift_end = datetime(2026, 8, 27, 8, 0)
    sector = SectorEvents.__new__(SectorEvents)
    sector.shift_start = shift_end - timedelta(days=1)
    sector.shift_end = shift_end
    event = SimpleNamespace(
        status=PatientStatus.TRANSFERRED,
        start_time=shift_end + timedelta(hours=1),
        created_at=shift_end + timedelta(minutes=10),
    )

    assert sector._belongs_to_late_outcome_extension(event) is True
    event.status = PatientStatus.OUT
    assert sector._belongs_to_late_outcome_extension(event) is False


def test_archive_card_enables_only_terminal_outcome_buttons():
    app = QApplication.instance() or QApplication([])
    sector = SectorEvents()
    try:
        shift_end = datetime(2026, 8, 27, 8, 0)
        sector.shift_start = shift_end - timedelta(days=1)
        sector.shift_end = shift_end
        sector._late_outcome_card_state = lambda reference_dt=None: {"eligible": True}
        sector._update_buttons_state(PatientStatus.ACTIVE, is_archive=True, late_allowed=True)

        assert sector.btn_trans.isEnabled() is True
        assert sector.btn_dead.isEnabled() is True
        assert sector.btn_active.isEnabled() is False
        assert sector.btn_out.isEnabled() is False
        assert sector.btn_or.isEnabled() is False

        late_outcome = SimpleNamespace(
            status=PatientStatus.TRANSFERRED,
            start_time=shift_end + timedelta(hours=1),
            created_at=shift_end + timedelta(minutes=10),
        )
        sector._update_refresh_controls(
            late_outcome, [object(), late_outcome], is_archive=True,
            snapshot={"late_state": {"eligible": True}},
        )
        assert sector.btn_rollback.isEnabled() is True
    finally:
        sector.close()
        sector.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()
