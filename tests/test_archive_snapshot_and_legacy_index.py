from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import threading
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.app import operblock_archive_index_cache as legacy_index_cache
from rem_card.app.operblock_archive_index_cache import (
    LEGACY_OPERBLOCK_INDEX_ALIAS,
    attach_legacy_operblock_archive_index,
)
from rem_card.app.sqlite_shared import configure_connection
from rem_card.data.dao.db_manager import DatabaseManager
from rem_card.data.dao.patient_dao import PatientDAO
from rem_card.services.operblock_service import OperBlockService


class _ConnectionGuard:
    @contextmanager
    def connection_guard(self, _conn):
        yield


def _create_live_archive_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
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
                bed_number INTEGER,
                admission_datetime TEXT,
                transfer_datetime TEXT,
                death_datetime TEXT,
                diagnosis_text TEXT,
                patient_age INTEGER,
                patient_months INTEGER,
                patient_age_unit TEXT,
                patient_gender TEXT,
                diagnosis_code TEXT,
                operation_description TEXT,
                emergency_notice_number TEXT,
                emergency_notice_entered_at TEXT,
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
            CREATE INDEX idx_operation_cases_started_at_id
            ON operation_cases(started_at, id DESC);
            INSERT INTO operating_tables VALUES ('emergency', 'Экстренная операционная');
            INSERT INTO patients VALUES (1, 'Рао', 'Первый', '', 'Рао Первый', '1980-01-01');
            INSERT INTO admissions (
                id, patient_id, history_number, bed_number, admission_datetime,
                transfer_datetime, diagnosis_text, patient_age, patient_age_unit,
                unit_scope, admission_type
            ) VALUES (
                1, 1, 'РАО-1', 1, '2026-07-12 08:00:00',
                '2026-07-12 10:00:00', 'РАО', 46, 'л', 'rao', 'rao'
            );
            INSERT INTO patients VALUES (10, 'Опер', 'Первый', '', 'Опер Первый', '1980-01-01');
            INSERT INTO admissions (
                id, patient_id, history_number, admission_datetime,
                diagnosis_text, patient_age, patient_age_unit, unit_scope, admission_type
            ) VALUES (
                10, 10, 'ОП-1', '2026-07-12 09:00:00',
                'Операция', 46, 'л', 'operblock', 'operblock'
            );
            INSERT INTO operation_cases
            VALUES (10, 10, 10, 'emergency', 'closed', '2026-07-12 09:00:00', '2026-07-12 10:00:00');
            """
        )
        conn.commit()
    finally:
        conn.close()


def _snapshot_manager(db_path: Path, after_count) -> tuple[DatabaseManager, list[int]]:
    manager = object.__new__(DatabaseManager)
    manager.db_path = os.path.abspath(str(db_path))
    manager._thread_state = threading.local()
    manager.write_controller = _ConnectionGuard()
    manager._in_current_thread_remcard_transaction = lambda: False
    manager._central_io_lock_scope = lambda *_args, **_kwargs: nullcontext()
    open_count = [0]

    def open_readonly():
        open_count[0] += 1
        conn = sqlite3.connect(
            f"file:{manager.db_path}?mode=ro",
            uri=True,
            isolation_level=None,
        )
        configure_connection(conn, readonly=True)
        return conn

    manager._open_readonly_central_connection = open_readonly
    callback = [after_count]

    def fetch_all(query, params=(), **_kwargs):
        conn = manager._scoped_central_read_connection()
        assert conn is not None, "archive reads must stay inside central_read_snapshot_scope"
        rows = conn.execute(query, tuple(params or ())).fetchall()
        if "COUNT(*) AS total_count" in query and callback:
            operation = callback.pop()
            operation()
        return rows

    manager.fetch_all_remcard = fetch_all
    return manager, open_count


def test_live_patient_archive_count_and_rows_share_one_sqlite_snapshot(tmp_path, monkeypatch):
    db_path = tmp_path / "live.db"
    _create_live_archive_db(db_path)

    def concurrent_insert():
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("INSERT INTO patients VALUES (2, 'Рао', 'Второй', '', 'Рао Второй', '1980-01-01')")
            conn.execute(
                """
                INSERT INTO admissions (
                    id, patient_id, history_number, bed_number, admission_datetime,
                    transfer_datetime, diagnosis_text, patient_age, patient_age_unit,
                    unit_scope, admission_type
                ) VALUES (2, 2, 'РАО-2', 2, '2026-07-12 11:00:00',
                          '2026-07-12 12:00:00', 'РАО', 46, 'л', 'rao', 'rao')
                """
            )
            conn.commit()
        finally:
            conn.close()

    manager, open_count = _snapshot_manager(db_path, concurrent_insert)
    dao = PatientDAO(manager)
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

    assert open_count == [1]
    assert payload["total_count"] == len(payload["records"]) == 1
    assert payload["records"][0].history_number == "РАО-1"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM admissions WHERE unit_scope = 'rao'").fetchone()[0] == 2


def test_live_operblock_archive_count_and_rows_share_one_sqlite_snapshot(tmp_path, monkeypatch):
    db_path = tmp_path / "live.db"
    _create_live_archive_db(db_path)

    def concurrent_insert():
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("INSERT INTO patients VALUES (11, 'Опер', 'Второй', '', 'Опер Второй', '1980-01-01')")
            conn.execute(
                """
                INSERT INTO admissions (
                    id, patient_id, history_number, admission_datetime,
                    diagnosis_text, patient_age, patient_age_unit, unit_scope, admission_type
                ) VALUES (11, 11, 'ОП-2', '2026-07-12 11:00:00',
                          'Операция', 46, 'л', 'operblock', 'operblock')
                """
            )
            conn.execute(
                """
                INSERT INTO operation_cases
                VALUES (11, 11, 11, 'emergency', 'closed',
                        '2026-07-12 11:00:00', '2026-07-12 12:00:00')
                """
            )
            conn.commit()
        finally:
            conn.close()

    manager, open_count = _snapshot_manager(db_path, concurrent_insert)
    manager.runtime_context = SimpleNamespace(mode="network")
    service = object.__new__(OperBlockService)
    service.db = manager
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

    assert open_count == [1]
    assert payload["total_count"] == len(payload["records"]) == 1
    assert payload["records"][0]["history_number"] == "ОП-1"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM operation_cases").fetchone()[0] == 2


def _create_legacy_operblock_archive(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE patients (
                id INTEGER PRIMARY KEY,
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
                unit_scope TEXT
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
            CREATE TABLE operating_tables (code TEXT PRIMARY KEY, display_name TEXT);
            INSERT INTO operating_tables VALUES ('emergency', 'Экстренная операционная');
            INSERT INTO patients VALUES (1, 'Пациент Один', '1980-01-01');
            INSERT INTO patients VALUES (2, 'Пациент Два', '1980-01-01');
            INSERT INTO admissions VALUES (1, 1, 'ОП-1', '2026-07-12 08:00:00', 'S01', 'Диагноз 1', 'М', 46, 0, 'л', 'operblock');
            INSERT INTO admissions VALUES (2, 2, 'ОП-2', '2026-07-12 09:00:00', 'S02', 'Диагноз 2', 'Ж', 46, 0, 'л', 'operblock');
            INSERT INTO operation_cases VALUES (1, 1, 1, 'emergency', 'closed', '2026-07-12T08:00:00', '2026-07-12T08:30:00');
            INSERT INTO operation_cases VALUES (2, 2, 2, 'emergency', 'closed', '2026-07-12 09:00:00', '2026-07-12 09:30:00');
            """
        )
        conn.commit()
    finally:
        conn.close()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_legacy_operblock_uses_local_technical_index_without_mutating_source(
    tmp_path,
    monkeypatch,
):
    source_path = tmp_path / "legacy.db"
    cache_dir = tmp_path / "local-cache"
    _create_legacy_operblock_archive(source_path)
    monkeypatch.setattr(
        legacy_index_cache,
        "LEGACY_OPERBLOCK_INDEX_CACHE_DIR",
        cache_dir,
    )
    before_hash = _sha256(source_path)
    before_stat = source_path.stat()

    from rem_card.services import operblock_service as operblock_service_module

    original_connect = sqlite3.connect
    source_open_count = 0

    def counted_connect(*args, **kwargs):
        nonlocal source_open_count
        target = str(args[0]) if args else ""
        if os.path.abspath(str(source_path)).replace("\\", "/") in target.replace("\\", "/") and "mode=ro" in target:
            source_open_count += 1
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(operblock_service_module.sqlite3, "connect", counted_connect)
    total, rows = OperBlockService._fetch_archive_case_page_from_db(
        str(source_path),
        start_dt="2026-07-12 00:00:00",
        end_dt="2026-07-13 00:00:00",
        limit=50,
    )

    assert source_open_count == 1
    assert total == 2
    assert {row["operation_case_id"] for row in rows} == {1, 2}
    assert _sha256(source_path) == before_hash
    after_stat = source_path.stat()
    assert (after_stat.st_size, after_stat.st_mtime_ns) == (
        before_stat.st_size,
        before_stat.st_mtime_ns,
    )

    with sqlite3.connect(source_path) as conn:
        source_indexes = {row[1] for row in conn.execute("PRAGMA index_list('operation_cases')")}
    assert "idx_operation_cases_started_at_id" not in source_indexes

    sidecars = list(cache_dir.glob("operblock_*.sqlite3"))
    assert len(sidecars) == 1
    with sqlite3.connect(sidecars[0]) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info('operation_case_started')")
        }
    assert tables == {"cache_meta", "operation_case_started"}
    assert columns == {"operation_case_id", "started_at"}


def test_legacy_operblock_query_plan_uses_sidecar_index(tmp_path):
    source_path = tmp_path / "legacy.db"
    cache_dir = tmp_path / "local-cache"
    _create_legacy_operblock_archive(source_path)
    conn = sqlite3.connect(
        f"file:{source_path}?mode=ro",
        uri=True,
        isolation_level=None,
    )
    try:
        configure_connection(conn, readonly=True)
        descriptor = attach_legacy_operblock_archive_index(
            conn,
            str(source_path),
            cache_dir=cache_dir,
        )
        assert descriptor is not None
        query, params = OperBlockService._build_archive_cases_query(
            tables={"operation_cases", "admissions", "patients", "operating_tables"},
            admission_columns={"unit_scope"},
            start_dt="2026-07-12 00:00:00",
            end_dt="2026-07-13 00:00:00",
            count_only=True,
            end_exclusive=True,
            legacy_index_attached=True,
        )
        plan = conn.execute(f"EXPLAIN QUERY PLAN {query}", params).fetchall()
    finally:
        conn.close()

    details = "\n".join(str(row[3]) for row in plan)
    assert "idx_operation_case_started_at_id" in details
    assert "SCAN legacy_idx" not in details
    assert LEGACY_OPERBLOCK_INDEX_ALIAS in query


def test_legacy_operblock_period_preselector_keeps_fractional_last_second(
    tmp_path,
    monkeypatch,
):
    source_path = tmp_path / "legacy-fractional.db"
    cache_dir = tmp_path / "local-cache"
    with sqlite3.connect(source_path) as conn:
        conn.executescript(
            """
            CREATE TABLE operation_cases (
                id INTEGER PRIMARY KEY,
                started_at TEXT,
                status TEXT
            );
            INSERT INTO operation_cases
            VALUES (1, '2026-07-12T23:59:59.900', 'closed');
            """
        )
    monkeypatch.setattr(
        legacy_index_cache,
        "LEGACY_OPERBLOCK_INDEX_CACHE_DIR",
        cache_dir,
    )

    assert OperBlockService._db_has_operblock_cases_in_period(
        str(source_path),
        datetime(2026, 7, 12, 0, 0, 0),
        datetime(2026, 7, 12, 23, 59, 59),
    )
    assert list(cache_dir.glob("operblock_*.sqlite3")), "test must exercise the legacy sidecar path"


def test_modern_operblock_period_preselector_uses_native_started_at_index(
    tmp_path,
    monkeypatch,
):
    source_path = tmp_path / "modern-indexed.db"
    cache_dir = tmp_path / "local-cache"
    with sqlite3.connect(source_path) as conn:
        conn.executescript(
            """
            CREATE TABLE operation_cases (
                id INTEGER PRIMARY KEY,
                started_at TEXT,
                status TEXT
            );
            CREATE INDEX idx_operation_cases_started_at_id
            ON operation_cases(started_at, id DESC);
            INSERT INTO operation_cases
            VALUES (1, '2026-07-12T23:59:59.900', 'closed');
            """
        )
    monkeypatch.setattr(
        legacy_index_cache,
        "LEGACY_OPERBLOCK_INDEX_CACHE_DIR",
        cache_dir,
    )

    from rem_card.services import operblock_service as operblock_service_module

    traced_statements: list[str] = []
    original_configure = operblock_service_module.configure_connection

    def configure_with_trace(conn, *, readonly):
        original_configure(conn, readonly=readonly)
        conn.set_trace_callback(traced_statements.append)

    monkeypatch.setattr(
        operblock_service_module,
        "configure_connection",
        configure_with_trace,
    )

    assert OperBlockService._db_has_operblock_cases_in_period(
        str(source_path),
        datetime(2026, 7, 12, 0, 0, 0),
        datetime(2026, 7, 12, 23, 59, 59),
    )

    period_query = next(
        statement
        for statement in traced_statements
        if "FROM operation_cases oc" in statement and "LIMIT 1" in statement
    )
    assert "DATETIME(oc.started_at)" not in period_query
    assert "oc.started_at >= '2026-07-12'" in period_query
    assert "oc.started_at < '2026-07-13'" in period_query
    with sqlite3.connect(source_path) as conn:
        plan = conn.execute(f"EXPLAIN QUERY PLAN {period_query}").fetchall()
    assert "idx_operation_cases_started_at_id" in " ".join(str(row) for row in plan)
    assert not list(cache_dir.glob("operblock_*.sqlite3"))


def test_legacy_operblock_sidecar_is_invalidated_when_source_is_replaced(tmp_path):
    source_path = tmp_path / "legacy.db"
    cache_dir = tmp_path / "local-cache"
    _create_legacy_operblock_archive(source_path)

    first_conn = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, isolation_level=None)
    try:
        configure_connection(first_conn, readonly=True)
        first = attach_legacy_operblock_archive_index(
            first_conn,
            str(source_path),
            cache_dir=cache_dir,
        )
    finally:
        first_conn.close()
    assert first is not None
    assert first.row_count == 2

    with sqlite3.connect(source_path) as conn:
        conn.execute("INSERT INTO patients VALUES (3, 'Пациент Три', '1980-01-01')")
        conn.execute(
            "INSERT INTO admissions VALUES (3, 3, 'ОП-3', '2026-07-12 10:00:00', "
            "'S03', 'Диагноз 3', 'М', 46, 0, 'л', 'operblock')"
        )
        conn.execute(
            "INSERT INTO operation_cases VALUES (3, 3, 3, 'emergency', 'closed', "
            "'2026-07-12 10:00:00', '2026-07-12 10:30:00')"
        )
        conn.commit()

    second_conn = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, isolation_level=None)
    try:
        configure_connection(second_conn, readonly=True)
        second = attach_legacy_operblock_archive_index(
            second_conn,
            str(source_path),
            cache_dir=cache_dir,
        )
    finally:
        second_conn.close()

    assert second is not None
    assert second.row_count == 3
    assert second.path != first.path
    assert second.source_fingerprint != first.source_fingerprint
