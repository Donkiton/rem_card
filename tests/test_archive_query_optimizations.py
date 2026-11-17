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
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionButton  # noqa: E402

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


def test_archive_25_row_pages_keep_search_across_unloaded_records(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_mixed_archive(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO patients (id, full_name) VALUES (?, ?)",
            [(i, "Искомый пациент" if i == 100 else f"Тестовый пациент {i}") for i in range(100, 160)],
        )
        conn.executemany(
            """INSERT INTO admissions (id, patient_id, history_number, admission_datetime, unit_scope, admission_type)
               VALUES (?, ?, ?, '2026-07-12 12:00:00', 'rao', 'rao')""",
            [(i, i, f"ТЕСТ-{i}") for i in range(100, 160)],
        )
    dao = object.__new__(PatientDAO)
    dao.db = SimpleNamespace(db_path=str(tmp_path / "current.db"))
    monkeypatch.setattr(dao, "_iter_archived_db_paths", lambda *args, **kwargs: [str(db_path)])
    pages = [dao.get_archived_patients_page(page=p, page_size=25, search_ib="ТЕСТ-") for p in (1, 2, 3)]
    assert [len(p["records"]) for p in pages] == [25, 25, 10]
    assert all(p["total_count"] == 60 for p in pages)
    assert len({row.id for page in pages for row in page["records"]}) == 60
    assert 100 not in {row.id for row in pages[0]["records"]}

    found = dao.get_archived_patients_page(page=1, page_size=25, search_name="искомый")
    assert found["total_count"] == 1
    assert [row.id for row in found["records"]] == [100]


def test_operblock_archive_merges_current_and_legacy_rotated_db_without_transfer_columns(tmp_path):
    archiv = tmp_path / "archiv"
    archiv.mkdir()
    current_path = archiv / "rao_journal.db"
    old_path = archiv / "rao_journal_archived_20260101_000000.db"
    _create_mixed_archive(current_path)
    _create_mixed_archive(old_path)
    for path, history_number in ((current_path, "CUR"), (old_path, "OLD")):
        conn = sqlite3.connect(path)
        try:
            conn.execute("UPDATE admissions SET history_number=? WHERE id=3", (history_number,))
            conn.commit()
        finally:
            conn.close()

    class Manager:
        def __init__(self, db_path):
            self.db_path = str(db_path)
            self.runtime_context = SimpleNamespace(mode="network")
            self._conn = sqlite3.connect(db_path)
            self._conn.row_factory = sqlite3.Row

        def fetch_all_remcard(self, query, params=()):
            return self._conn.execute(query, params).fetchall()

        def close(self):
            self._conn.close()

    manager = Manager(current_path)
    try:
        records = OperBlockService(manager).list_archived_operation_cases()
    finally:
        manager.close()
    histories = {item["history_number"]: item for item in records}
    assert {"CUR", "OLD"}.issubset(histories)
    assert not histories["CUR"]["is_external_archive"]
    assert histories["OLD"]["is_external_archive"]


def test_operblock_archive_page_uses_one_connection_casefold_and_half_open_end(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_mixed_archive(db_path)
    clear_archive_schema_cache()

    from rem_card.services.operblock import archive_sources as operblock_archive_module

    original_connect = sqlite3.connect
    connect_calls = 0

    def counted_connect(*args, **kwargs):
        nonlocal connect_calls
        connect_calls += 1
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(operblock_archive_module.sqlite3, "connect", counted_connect)
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

    from rem_card.services.operblock import archive_sources as operblock_archive_module

    original_connect = sqlite3.connect
    connect_calls = 0

    def counted_connect(*args, **kwargs):
        nonlocal connect_calls
        connect_calls += 1
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(operblock_archive_module.sqlite3, "connect", counted_connect)
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
        self.running = False
        self.__class__.instances.append(self)

    def isRunning(self):
        return self.running

    def start(self):
        self.started = True
        self.running = True


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
                "page_size": 25,
                "search_name": "Иванов",
                "search_ib": "РАО-1",
                "search_diag": "Пневмония",
            }
        ]
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def _finish_archive_request(worker, payload):
    for callback in worker.succeeded.callbacks:
        callback(payload)
    worker.running = False
    for callback in worker.finished.callbacks:
        callback()


def test_archive_load_renders_without_period_scan_and_reports_resolve_on_demand(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(archive_widget_module, "AsyncCallThread", _DeferredWorker)
    service = _PatientServiceSpy()
    period_calls = []
    service.get_archive_db_paths_for_period = lambda *args: period_calls.append(args) or []
    widget = archive_widget_module.ArchiveWidget(service)
    try:
        widget.load_data()
        assert widget.records_stack.currentWidget() is widget.load_status_page
        assert widget.load_status_label.text() == "Загрузка пациентов..."
        assert widget._load_in_progress
        assert not widget.btn_open.isEnabled()

        payload = widget._load_worker.loader()
        _finish_archive_request(widget._load_worker, payload)
        assert widget.records_stack.currentWidget() is widget.table
        assert not widget._load_in_progress
        assert period_calls == []

        # Reports cover the whole period, even when a patient search is empty.
        assert widget.btn_report_stats.isEnabled()
        monkeypatch.setattr(archive_widget_module.CustomMessageBox, "information", lambda *args: None)
        widget.on_report_stats_clicked()
        assert len(period_calls) == 1
    finally:
        widget.deleteLater()
        app.processEvents()


def test_archive_loading_stays_visible_for_pending_page_and_ignores_stale_result(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(archive_widget_module, "AsyncCallThread", _DeferredWorker)
    widget = archive_widget_module.ArchiveWidget(_PatientServiceSpy())
    try:
        widget.load_data(page=1)
        first_worker = widget._load_worker
        widget.load_data(page=2)
        _finish_archive_request(first_worker, {"records": [], "total_count": 100, "page": 1})
        assert widget._load_in_progress
        assert widget.records_stack.currentWidget() is widget.load_status_page
        app.processEvents()
        next_worker = widget._load_worker
        assert next_worker is not first_worker
        payload = next_worker.loader()
        payload["total_count"] = 100
        _finish_archive_request(next_worker, payload)
        assert widget.current_page == 2
        assert widget.records_stack.currentWidget() is widget.table
        assert not widget._load_in_progress
    finally:
        widget.deleteLater()
        app.processEvents()


def test_archive_load_error_stops_spinner_and_retry_shows_table(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(archive_widget_module, "AsyncCallThread", _DeferredWorker)
    widget = archive_widget_module.ArchiveWidget(_PatientServiceSpy())
    try:
        widget.load_data()
        worker = widget._load_worker
        for callback in worker.failed.callbacks:
            callback(RuntimeError("test read failure"))
        worker.running = False
        for callback in worker.finished.callbacks:
            callback()
        assert not widget._load_in_progress
        assert widget.load_spinner.isHidden()
        assert "Не удалось загрузить архив" in widget.load_status_label.text()

        widget.load_data()
        worker = widget._load_worker
        _finish_archive_request(worker, worker.loader())
        assert widget.records_stack.currentWidget() is widget.table
    finally:
        widget.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("total_pages,current_page", [(1, 1), (3, 2), (5, 5), (12, 6), (100, 100)])
def test_archive_quick_pages_stay_at_first_five_and_all_pages_remain_reachable(total_pages, current_page):
    app = QApplication.instance() or QApplication([])
    widget = archive_widget_module.ArchiveWidget(_PatientServiceSpy())
    try:
        widget.total_pages = total_pages
        widget.current_page = current_page
        widget._refresh_pagination_ui()
        buttons = [widget.page_buttons_layout.itemAt(i).widget() for i in range(widget.page_buttons_layout.count())]
        assert [button.text() for button in buttons] == [str(p) for p in range(1, min(5, total_pages) + 1)]
        assert [button.text() for button in buttons if button.isChecked()] == ([str(current_page)] if current_page <= 5 else [])
        assert widget.page_info.text() == f"Страница {current_page} из {total_pages}"
        assert widget.btn_prev_page.isEnabled() == (current_page > 1)
        assert widget.btn_next_page.isEnabled() == (current_page < total_pages)

        requested_pages = []
        widget.load_data = lambda *, page: requested_pages.append(page)
        if current_page > 1:
            widget.btn_prev_page.click()
            assert requested_pages[-1] == current_page - 1
        if current_page < total_pages:
            widget.btn_next_page.click()
            assert requested_pages[-1] == current_page + 1
        widget.page_jump_input.setText(str(total_pages))
        widget.btn_page_jump.click()
        assert requested_pages[-1] == total_pages
    finally:
        widget.deleteLater()
        app.processEvents()


def test_archive_page_buttons_fit_text_with_center_style():
    from rem_card.ui.styles.archive_center_styles import build_archive_center_style
    from rem_card.ui.styles.theme_runtime import style_tokens

    app = QApplication.instance() or QApplication([])
    widget = archive_widget_module.ArchiveWidget(_PatientServiceSpy(), embedded=True)
    widget.setStyleSheet(build_archive_center_style(style_tokens()))
    try:
        widget.total_pages = 12
        widget._refresh_pagination_ui()
        widget.resize(900, 650)
        widget.show()
        app.processEvents()
        for index in range(widget.page_buttons_layout.count()):
            button = widget.page_buttons_layout.itemAt(index).widget()
            option = QStyleOptionButton()
            option.initFrom(button)
            content = button.style().subElementRect(QStyle.SE_PushButtonContents, option, button)
            assert content.width() >= button.fontMetrics().horizontalAdvance(button.text())
            assert content.height() >= button.fontMetrics().height()
            assert button.height() >= 32
    finally:
        widget.deleteLater()
        app.processEvents()
