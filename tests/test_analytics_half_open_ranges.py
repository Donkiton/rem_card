from __future__ import annotations

import sqlite3
import sys
import re
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.services.analytics.detailed_statistics_service import DetailedStatisticsReportBuilder
from rem_card.services.analytics import graphs_service
from rem_card.services.analytics.multi_db_analytics import create_multi_db_analytics_manager
from rem_card.services.analytics.operblock_statistics_service import OperBlockStatisticsReportBuilder
from rem_card.services.analytics.period import normalize_analytics_period, parse_analytics_datetime
from rem_card.services.analytics.recovery_summary import build_recovery_bed_summary
from rem_card.services.analytics.statistics_service import build_statistical_report_html
from rem_card.app.unified_db_schema import ensure_unified_schema, is_unified_schema_ready


class _Manager:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def get_connection(self):
        return self._conn


def _create_remcard_analytics_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE admissions (
            id INTEGER PRIMARY KEY,
            patient_id INTEGER,
            admission_datetime TEXT,
            transfer_datetime TEXT,
            death_datetime TEXT,
            outcome TEXT,
            patient_age REAL,
            patient_age_unit TEXT,
            patient_gender TEXT,
            source_department TEXT,
            diagnosis_code TEXT,
            diagnosis_text TEXT,
            bed_number INTEGER,
            recovery_bed_stay INTEGER DEFAULT 0
        );
        CREATE INDEX idx_admissions_admission_datetime
            ON admissions(admission_datetime);
        CREATE TABLE operations (
            id INTEGER PRIMARY KEY,
            admission_id INTEGER,
            operation_datetime TEXT,
            description TEXT
        );
        CREATE TABLE transfusions (
            id INTEGER PRIMARY KEY,
            admission_id INTEGER,
            datetime TEXT,
            type TEXT,
            volume_ml REAL
        );
        CREATE TABLE ivl_episodes (
            id INTEGER PRIMARY KEY,
            admission_id INTEGER,
            start_time TEXT,
            end_time TEXT
        );
        """
    )


def _seed_remcard_boundaries(conn: sqlite3.Connection) -> None:
    rows = [
        (1, 1, "2026-07-11T23:59:59.999999", 10, 0),
        (2, 2, "2026-07-12 00:00:00", 10, 1),
        (3, 3, "2026-07-12T23:59:59.999999", 11, 1),
        (4, 4, "2026-07-13 00:00:00", 10, 1),
        (5, 5, "2026-07-13T00:00:00", 10, 1),
    ]
    conn.executemany(
        """
        INSERT INTO admissions (
            id, patient_id, admission_datetime, patient_gender,
            source_department, diagnosis_code, diagnosis_text,
            bed_number, recovery_bed_stay
        ) VALUES (?, ?, ?, 'ж', 'ПСО', 'J18', 'Пневмония', ?, ?)
        """,
        rows,
    )
    conn.executemany(
        "INSERT INTO operations (id, admission_id, operation_datetime, description) VALUES (?, ?, ?, 'Операция')",
        [(1, 2, "2026-07-12T23:59:59.900"), (2, 4, "2026-07-13 00:00:00")],
    )
    conn.executemany(
        "INSERT INTO transfusions (id, admission_id, datetime, type, volume_ml) VALUES (?, ?, ?, 'СЗП', 250)",
        [(1, 3, "2026-07-12 23:59:59.999"), (2, 4, "2026-07-13T00:00:00")],
    )
    conn.executemany(
        "INSERT INTO ivl_episodes (id, admission_id, start_time, end_time) VALUES (?, ?, ?, ?)",
        [
            (1, 2, "2026-07-12T23:59:59.900", "2026-07-13T00:00:00"),
            (2, 4, "2026-07-13 00:00:00", None),
        ],
    )


def test_period_normalization_keeps_inclusive_calendar_day_semantics():
    period = normalize_analytics_period(
        "2026-07-12T08:15:00.123",
        "2026-07-12 23:59:59",
    )

    assert period.sql_bounds == ("2026-07-12 00:00:00", "2026-07-13 00:00:00")
    assert period.inclusive_end.isoformat(timespec="microseconds") == "2026-07-12T23:59:59.999999"
    assert parse_analytics_datetime("2026-07-12T23:59:59.999999") == period.inclusive_end
    assert parse_analytics_datetime("2026-07-12 23:59:59.999999") == period.inclusive_end


def test_analytics_sources_do_not_reintroduce_closed_end_of_day_ranges():
    source_files = [
        *sorted((PROJECT_DIR / "services" / "analytics").glob("*.py")),
        *sorted((PROJECT_DIR / "ui" / "analytics").glob("*.py")),
        PROJECT_DIR / "ui" / "shared" / "analytics_integration.py",
    ]

    for source_file in source_files:
        source = source_file.read_text(encoding="utf-8")
        assert re.search(r"(?<![A-Z0-9_])BETWEEN\s+", source.upper()) is None, source_file
        assert "23:59:59" not in source, source_file


def test_raw_half_open_range_supports_space_t_fractional_seconds_and_index():
    conn = sqlite3.connect(":memory:")
    try:
        _create_remcard_analytics_schema(conn)
        _seed_remcard_boundaries(conn)
        bounds = normalize_analytics_period("2026-07-12", "2026-07-12").sql_bounds

        rows = conn.execute(
            """
            SELECT id FROM admissions
            WHERE admission_datetime >= ? AND admission_datetime < ?
            ORDER BY id
            """,
            bounds,
        ).fetchall()
        plan = conn.execute(
            """
            EXPLAIN QUERY PLAN SELECT id FROM admissions
            WHERE admission_datetime >= ? AND admission_datetime < ?
            """,
            bounds,
        ).fetchall()

        assert rows == [(2,), (3,)]
        assert "idx_admissions_admission_datetime" in " ".join(str(row) for row in plan)
    finally:
        conn.close()


def test_unified_schema_provisions_time_range_indexes_for_analytics():
    conn = sqlite3.connect(":memory:")
    try:
        ensure_unified_schema(conn)
        bounds = ("2026-07-12 00:00:00", "2026-07-13 00:00:00")

        expected = {
            ("operations", "operation_datetime"): "idx_operations_operation_datetime",
            ("transfusions", "datetime"): "idx_transfusions_datetime",
            ("ivl_episodes", "start_time"): "idx_ivl_start_time",
        }
        for (table_name, column_name), index_name in expected.items():
            plan = conn.execute(
                f"EXPLAIN QUERY PLAN SELECT id FROM {table_name} "
                f"WHERE {column_name} >= ? AND {column_name} < ?",
                bounds,
            ).fetchall()
            assert index_name in " ".join(str(row) for row in plan)
        assert is_unified_schema_ready(conn)
    finally:
        conn.close()


def test_statistics_and_detailed_context_include_last_fraction_exclude_next_midnight():
    conn = sqlite3.connect(":memory:")
    try:
        _create_remcard_analytics_schema(conn)
        _seed_remcard_boundaries(conn)
        manager = _Manager(conn)

        html = build_statistical_report_html(
            manager,
            "2026-07-12 00:00:00",
            "2026-07-12 23:59:59",
        )
        context = DetailedStatisticsReportBuilder(
            manager,
            "2026-07-12",
            "2026-07-12",
            include_recovery_beds=True,
        )._fetch_context()

        assert 'Поступило пациентов</td><td class="num">2</td>' in html
        assert 'Операций выполнено</td><td class="num">1</td>' in html
        assert [row["admission_id"] for row in context["admissions"]] == [2, 3]
        assert context["operations_adm_ids"] == [2]
        assert [row["admission_id"] for row in context["transfusions"]] == [3]
        assert [row["admission_id"] for row in context["ivl_episodes"]] == [2]
    finally:
        conn.close()


def test_graphs_service_uses_half_open_bounds_end_to_end(monkeypatch):
    conn = sqlite3.connect(":memory:")
    try:
        _create_remcard_analytics_schema(conn)
        _seed_remcard_boundaries(conn)
        captured = {}

        def passthrough(*args, **_kwargs):
            return args[-1]

        def capture_admissions(*args, **_kwargs):
            captured["params"] = args[2]
            captured["admission_ids"] = [int(row["id"]) for row in args[5]]
            captured["selected_dates"] = (args[6], args[7])
            return args[-1]

        generators = [passthrough] * 12
        generators[1] = capture_admissions
        monkeypatch.setattr(graphs_service, "_load_generators", lambda: tuple(generators))
        monkeypatch.setattr(graphs_service, "_configure_plot_style", lambda _colors: None)

        graphs_service.build_graphs_html(
            _Manager(conn),
            "2026-07-12 00:00:00",
            "2026-07-12 23:59:59",
            ["g6"],
            include_recovery_beds=True,
        )

        assert captured == {
            "params": ("2026-07-12 00:00:00", "2026-07-13 00:00:00"),
            "admission_ids": [2, 3],
            "selected_dates": ("2026-07-12", "2026-07-12"),
        }
    finally:
        conn.close()


def test_recovery_summary_uses_same_half_open_boundary():
    conn = sqlite3.connect(":memory:")
    try:
        _create_remcard_analytics_schema(conn)
        _seed_remcard_boundaries(conn)

        summary = build_recovery_bed_summary(conn, "2026-07-12", "2026-07-12")

        assert summary.total_admissions == 2
        assert summary.recovery_admissions == 2
    finally:
        conn.close()


def _create_operblock_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE operation_cases (
            id INTEGER PRIMARY KEY,
            patient_id INTEGER,
            admission_id INTEGER,
            table_code TEXT,
            status TEXT,
            created_at TEXT,
            started_at TEXT,
            ended_at TEXT,
            planned_operation_name TEXT,
            planned_surgeons_json TEXT,
            planned_operating_nurse TEXT,
            planned_anesthesiologist TEXT,
            planned_anesthetist TEXT,
            height_cm REAL,
            weight_kg REAL,
            allergies TEXT,
            blood_group TEXT,
            blood_rh TEXT,
            preop_sys REAL,
            preop_dia REAL,
            preop_pulse REAL,
            preop_spo2 REAL,
            anesthesia_protocol_number TEXT,
            anesthesia_protocol_date TEXT,
            transfer_department TEXT
        );
        CREATE INDEX idx_operation_cases_started_at_id
            ON operation_cases(started_at, id DESC);
        CREATE TABLE operating_tables (code TEXT PRIMARY KEY, display_name TEXT);
        CREATE TABLE patients (id INTEGER PRIMARY KEY, full_name TEXT, birth_date TEXT);
        CREATE TABLE admissions (
            id INTEGER PRIMARY KEY,
            history_number TEXT,
            patient_gender TEXT,
            patient_age REAL,
            patient_months INTEGER,
            patient_age_unit TEXT,
            diagnosis_code TEXT,
            diagnosis_text TEXT,
            department_profile TEXT,
            source_department TEXT
        );
        """
    )


def test_operblock_statistics_boundary_and_started_at_index():
    conn = sqlite3.connect(":memory:")
    try:
        _create_operblock_schema(conn)
        conn.executemany(
            "INSERT INTO operation_cases (id, status, started_at) VALUES (?, 'completed', ?)",
            [
                (1, "2026-07-12 00:00:00"),
                (2, "2026-07-12T23:59:59.999"),
                (3, "2026-07-13 00:00:00"),
            ],
        )
        builder = OperBlockStatisticsReportBuilder(_Manager(conn), "2026-07-12", "2026-07-12")

        context = builder._fetch_context_from_connection(
            conn,
            builder.start_date_str,
            builder.end_date_str,
        )
        plan = conn.execute(
            """
            EXPLAIN QUERY PLAN SELECT id FROM operation_cases
            WHERE started_at >= ? AND started_at < ?
            """,
            (builder.start_date_str, builder.end_date_str),
        ).fetchall()

        assert [row["operation_case_id"] for row in context["cases"]] == [1, 2]
        assert "idx_operation_cases_started_at_id" in " ".join(str(row) for row in plan)
    finally:
        conn.close()


def test_multi_db_snapshot_filters_with_half_open_boundary(tmp_path: Path):
    db_path = tmp_path / "analytics.sqlite"
    source = sqlite3.connect(db_path)
    try:
        source.execute("CREATE TABLE admissions (id INTEGER PRIMARY KEY, admission_datetime TEXT)")
        source.executemany(
            "INSERT INTO admissions (id, admission_datetime) VALUES (?, ?)",
            [
                (1, "2026-07-12T23:59:59.999"),
                (2, "2026-07-13 00:00:00"),
            ],
        )
        source.commit()
    finally:
        source.close()

    manager = create_multi_db_analytics_manager(
        [str(db_path)],
        start_dt="2026-07-12",
        end_dt="2026-07-12",
    )
    try:
        rows = manager.get_connection().execute("SELECT id FROM admissions").fetchall()
        assert [int(row[0]) for row in rows] == [1]
    finally:
        manager.close_connection()


def test_multi_db_snapshot_excludes_soft_merged_admissions(tmp_path: Path):
    db_path = tmp_path / "analytics_merged.sqlite"
    source = sqlite3.connect(db_path)
    try:
        source.execute(
            """
            CREATE TABLE admissions (
                id INTEGER PRIMARY KEY,
                admission_datetime TEXT,
                merged_into_admission_id INTEGER
            )
            """
        )
        source.executemany(
            """
            INSERT INTO admissions (id, admission_datetime, merged_into_admission_id)
            VALUES (?, ?, ?)
            """,
            [
                (1, "2026-07-12 10:00:00", None),
                (2, "2026-07-12 11:00:00", 1),
            ],
        )
        source.commit()
    finally:
        source.close()

    manager = create_multi_db_analytics_manager(
        [str(db_path)],
        start_dt="2026-07-12",
        end_dt="2026-07-12",
    )
    try:
        rows = manager.get_connection().execute("SELECT id FROM admissions").fetchall()
        assert [int(row[0]) for row in rows] == [1]
    finally:
        manager.close_connection()
