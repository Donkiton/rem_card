from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtCore import QDate  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from rem_card.app.archive_schema_cache import (  # noqa: E402
    clear_archive_schema_cache,
    get_archive_schema,
)
from rem_card.app.sqlite_shared import configure_connection  # noqa: E402
from rem_card.data.dao.patient_dao import PatientDAO  # noqa: E402
from rem_card.services.operblock_service import OperBlockService  # noqa: E402
from rem_card.ui.doctor_view import archive_widget as archive_widget_module  # noqa: E402


def _create_mixed_archive(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE patients (
                id INTEGER PRIMARY KEY,
                last_name TEXT,
                first_name TEXT,
                middle_name TEXT,
                full_name TEXT,
                birth_date TEXT
            );
            CREATE TABLE admissions (
                id INTEGER PRIMARY KEY,
                patient_id INTEGER,
                history_number TEXT,
                admission_datetime TEXT,
                diagnosis_code TEXT,
                diagnosis_text TEXT,
                patient_gender TEXT,
                patient_age INTEGER,
                patient_months INTEGER,
                patient_age_unit TEXT,
                unit_scope TEXT,
                admission_type TEXT
            );
            CREATE TABLE operating_tables (
                code TEXT PRIMARY KEY,
                display_name TEXT
            );
            CREATE TABLE operation_cases (
                id INTEGER PRIMARY KEY,
                patient_id INTEGER,
                admission_id INTEGER,
                table_code TEXT,
                status TEXT,
                started_at TEXT,
                ended_at TEXT
            );
            CREATE INDEX idx_admissions_admission_datetime
            ON admissions(admission_datetime);
            CREATE INDEX idx_operation_cases_started_at_id
            ON operation_cases(started_at, id DESC);
            INSERT INTO operating_tables (code, display_name)
            VALUES ('emergency', 'Экстренная операционная');
            """
        )
        conn.executemany(
            """
            INSERT INTO patients (
                id, last_name, first_name, middle_name, full_name, birth_date
            ) VALUES (?, ?, ?, '', ?, '1980-01-01')
            """,
            [
                (1, "ИВАНОВ", "Илья", "ИВАНОВ Илья"),
                (2, "Следующий", "День", "Следующий День"),
                (3, "ПЕТРОВА", "Анна", "ПЕТРОВА Анна"),
                (4, "Следующий", "Опер", "Следующий Опер"),
                (5, "СИДОРОВ", "Семён", "СИДОРОВ Семён"),
            ],
        )
        conn.executemany(
            """
            INSERT INTO admissions (
                id, patient_id, history_number, admission_datetime,
                diagnosis_code, diagnosis_text, patient_gender, patient_age,
                patient_months, patient_age_unit, unit_scope, admission_type
            ) VALUES (?, ?, ?, ?, ?, ?, 'Женский', 46, 0, 'л', ?, ?)
            """,
            [
                (1, 1, "РАО-1", "2026-07-12T23:59:59.900", "J18", "ПНЕВМОНИЯ", "rao", "rao"),
                (2, 2, "РАО-2", "2026-07-13 00:00:00", "J18", "Пневмония", "rao", "rao"),
                (3, 3, "ОП-1", "2026-07-12T23:59:59.999", "S82", "ПЕРЕЛОМ", "operblock", "operblock"),
                (4, 4, "ОП-2", "2026-07-13 00:00:00", "S82", "Перелом", "operblock", "operblock"),
                (5, 5, "РАО-5", "2026-07-12T23:59:59.900", "J45", "Астма", "rao", "rao"),
            ],
        )
        conn.executemany(
            """
            INSERT INTO operation_cases (
                id, patient_id, admission_id, table_code, status, started_at, ended_at
            ) VALUES (?, ?, ?, 'emergency', 'closed', ?, ?)
            """,
            [
                (3, 3, 3, "2026-07-12T23:59:59.999", "2026-07-12T23:59:59.999"),
                (4, 4, 4, "2026-07-13 00:00:00", "2026-07-13 00:00:00"),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_patient_archive_page_uses_one_connection_casefold_and_half_open_end(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_mixed_archive(db_path)
    clear_archive_schema_cache()

    from rem_card.data.dao import patient_dao as patient_dao_module

    original_connect = sqlite3.connect
    connect_calls = 0

    def counted_connect(*args, **kwargs):
        nonlocal connect_calls
        connect_calls += 1
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(patient_dao_module.sqlite3, "connect", counted_connect)
    dao = object.__new__(PatientDAO)
    total, rows = dao._fetch_archived_page_from_db(
        str(db_path),
        start_dt="2026-07-12 00:00:00",
        end_dt="2026-07-13 00:00:00",
        search_name="иванов",
        search_diag="пневмония",
        limit=50,
    )

    assert connect_calls == 1
    assert total == 1
    assert [row["admission_id"] for row in rows] == [1]


def test_patient_archive_tie_order_is_stable_and_half_open_filter_uses_index(tmp_path):
    db_path = tmp_path / "archive.db"
    _create_mixed_archive(db_path)
    clear_archive_schema_cache()
    dao = object.__new__(PatientDAO)

    total_1, page_1 = dao._fetch_archived_page_from_db(
        str(db_path),
        start_dt="2026-07-12 00:00:00",
        end_dt="2026-07-13 00:00:00",
        limit=1,
        offset=0,
    )
    total_2, page_2 = dao._fetch_archived_page_from_db(
        str(db_path),
        start_dt="2026-07-12 00:00:00",
        end_dt="2026-07-13 00:00:00",
        limit=1,
        offset=1,
    )

    assert total_1 == total_2 == 2
    assert [page_1[0]["admission_id"], page_2[0]["admission_id"]] == [5, 1]

    conn = sqlite3.connect(db_path)
    configure_connection(conn)
    patient_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(patients)").fetchall()
    }
    admission_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(admissions)").fetchall()
    }
    query, params = dao._build_archived_patients_query(
        patient_columns=patient_columns,
        admission_columns=admission_columns,
        has_operations_table=False,
        has_operation_cases_table=True,
        start_dt="2026-07-12 00:00:00",
        end_dt="2026-07-13 00:00:00",
        count_only=True,
        end_exclusive=True,
    )
    plan = conn.execute(f"EXPLAIN QUERY PLAN {query}", params).fetchall()
    conn.close()
    details = "\n".join(str(row[3]) for row in plan)
    assert "idx_admissions_admission_datetime" in details
    assert "SCAN a" not in details


def test_patient_archive_page_orders_mixed_space_and_t_timestamps_chronologically(tmp_path):
    db_path = tmp_path / "archive.db"
    _create_mixed_archive(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE admissions SET admission_datetime = ? WHERE id = 1",
        ("2026-07-12T01:00:00",),
    )
    conn.execute(
        "UPDATE admissions SET admission_datetime = ? WHERE id = 5",
        ("2026-07-12 23:00:00",),
    )
    conn.commit()
    conn.close()
    clear_archive_schema_cache()

    dao = object.__new__(PatientDAO)
    total, rows = dao._fetch_archived_page_from_db(
        str(db_path),
        start_dt="2026-07-12 00:00:00",
        end_dt="2026-07-13 00:00:00",
        limit=1,
        offset=0,
    )

    assert total == 2
    assert [row["admission_id"] for row in rows] == [5]


def test_public_patient_page_opens_external_archive_once(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_mixed_archive(db_path)
    current_path = tmp_path / "current.db"
    current_path.touch()
    clear_archive_schema_cache()

    from rem_card.data.dao import patient_dao as patient_dao_module

    original_connect = sqlite3.connect
    connect_calls = 0

    def counted_connect(*args, **kwargs):
        nonlocal connect_calls
        connect_calls += 1
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(patient_dao_module.sqlite3, "connect", counted_connect)
    dao = object.__new__(PatientDAO)
    dao.db = SimpleNamespace(db_path=str(current_path))
    monkeypatch.setattr(
        dao,
        "_iter_archived_db_paths",
        lambda _current, *, include_current: [str(db_path)],
    )

    payload = dao.get_archived_patients_page(
        start_dt="2026-07-12 00:00:00",
        end_dt="2026-07-13 00:00:00",
        page=1,
        page_size=50,
    )

    assert connect_calls == 1
    assert payload["total_count"] == 2


def test_operblock_archive_page_uses_one_connection_casefold_and_half_open_end(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_mixed_archive(db_path)
    clear_archive_schema_cache()

    from rem_card.services import operblock_service as operblock_service_module

    original_connect = sqlite3.connect
    connect_calls = 0

    def counted_connect(*args, **kwargs):
        nonlocal connect_calls
        connect_calls += 1
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(operblock_service_module.sqlite3, "connect", counted_connect)
    total, rows = OperBlockService._fetch_archive_case_page_from_db(
        str(db_path),
        start_dt="2026-07-12 00:00:00",
        end_dt="2026-07-13 00:00:00",
        search_name="петрова",
        search_diag="перелом",
        limit=50,
    )

    assert connect_calls == 1
    assert total == 1
    assert [row["operation_case_id"] for row in rows] == [3]

    conn = sqlite3.connect(db_path)
    configure_connection(conn)
    query, params = OperBlockService._build_archive_cases_query(
        tables={"operation_cases", "admissions", "patients", "operating_tables"},
        admission_columns={"unit_scope"},
        start_dt="2026-07-12 00:00:00",
        end_dt="2026-07-13 00:00:00",
        count_only=True,
        end_exclusive=True,
    )
    plan = conn.execute(f"EXPLAIN QUERY PLAN {query}", params).fetchall()
    conn.close()
    details = "\n".join(str(row[3]) for row in plan)
    assert "idx_operation_cases_started_at_id" in details


def test_public_operblock_page_opens_external_archive_once(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_mixed_archive(db_path)
    current_path = tmp_path / "current.db"
    current_path.touch()
    clear_archive_schema_cache()

    from rem_card.services import operblock_service as operblock_service_module

    original_connect = sqlite3.connect
    connect_calls = 0

    def counted_connect(*args, **kwargs):
        nonlocal connect_calls
        connect_calls += 1
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(operblock_service_module.sqlite3, "connect", counted_connect)
    service = object.__new__(OperBlockService)
    service.db = SimpleNamespace(
        db_path=str(current_path),
        runtime_context=SimpleNamespace(mode="network"),
    )
    monkeypatch.setattr(
        service,
        "_iter_archive_db_paths",
        lambda *, include_current: [str(db_path)],
    )

    payload = service.list_archived_operation_cases_page(
        start_dt="2026-07-12 00:00:00",
        end_dt="2026-07-13 00:00:00",
        page=1,
        page_size=50,
    )

    assert connect_calls == 1
    assert payload["total_count"] == 1
    assert [row["operation_case_id"] for row in payload["records"]] == [3]


def test_archive_schema_cache_invalidates_after_file_schema_change(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE admissions (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    clear_archive_schema_cache()

    conn = sqlite3.connect(db_path)
    configure_connection(conn)
    first = get_archive_schema(conn, str(db_path), inspect_tables=("admissions",))
    conn.close()
    assert first.columns["admissions"] == frozenset({"id"})

    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE admissions ADD COLUMN unit_scope TEXT")
    conn.commit()
    conn.close()
    stat_result = db_path.stat()
    os.utime(
        db_path,
        ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 1_000_000),
    )

    conn = sqlite3.connect(db_path)
    configure_connection(conn)
    second = get_archive_schema(conn, str(db_path), inspect_tables=("admissions",))
    conn.close()
    assert second.columns["admissions"] == frozenset({"id", "unit_scope"})
    assert second.fingerprint != first.fingerprint


def test_casefold_sql_function_preserves_numeric_zero():
    conn = sqlite3.connect(":memory:")
    configure_connection(conn)
    try:
        row = conn.execute("SELECT CASEFOLD(0), CASEFOLD(NULL)").fetchone()
        assert tuple(row) == ("0", "")
    finally:
        conn.close()


class _SignalStub:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)


class _DeferredWorker:
    instances = []

    def __init__(self, loader, parent=None):
        self.loader = loader
        self.parent = parent
        self.succeeded = _SignalStub()
        self.failed = _SignalStub()
        self.finished = _SignalStub()
        self.started = False
        self.__class__.instances.append(self)

    def isRunning(self):
        return False

    def start(self):
        self.started = True


class _PatientServiceSpy:
    def __init__(self):
        self.calls = []

    def get_archived_patients_page(self, **kwargs):
        self.calls.append(kwargs)
        return {"records": [], "total_count": 0, "page": kwargs["page"], "page_size": kwargs["page_size"]}


@pytest.mark.usefixtures("monkeypatch")
def test_archive_widget_snapshots_qt_filters_before_worker_start(monkeypatch):
    app = QApplication.instance() or QApplication([])
    _DeferredWorker.instances.clear()
    monkeypatch.setattr(archive_widget_module, "AsyncCallThread", _DeferredWorker)
    service = _PatientServiceSpy()
    widget = archive_widget_module.ArchiveWidget(service)
    try:
        widget.date_from.setDate(QDate(2026, 7, 12))
        widget.date_to.setDate(QDate(2026, 7, 12))
        widget.search_name.setText("Иванов")
        widget.search_ib.setText("РАО-1")
        widget.search_diag.setText("Пневмония")
        widget.load_data(page=2)
        worker = _DeferredWorker.instances[-1]

        widget.search_name.setText("Изменено")
        widget.search_ib.setText("Изменено")
        widget.search_diag.setText("Изменено")
        worker.loader()

        assert worker.started is True
        assert service.calls == [
            {
                "start_dt": "2026-07-12 00:00:00",
                "end_dt": "2026-07-13 00:00:00",
                "page": 2,
                "page_size": 50,
                "search_name": "Иванов",
                "search_ib": "РАО-1",
                "search_diag": "Пневмония",
            }
        ]
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()
