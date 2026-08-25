import sqlite3
from rem_card.services.analytics.multi_db_analytics import AnalyticsConnectionManager
from rem_card.services.analytics.multi_db_analytics import create_multi_db_analytics_manager
from rem_card.services.analytics.platform import AnalyticsPeriod, CohortDefinition, CohortFilter, MetricScope, StatisticsRepository, materialize_cohort_snapshot


def test_repository_keeps_source_identity_and_isolates_populations(tmp_path):
    paths = []
    for name in ("a.db", "b.db"):
        path = tmp_path / name; paths.append(str(path)); conn = sqlite3.connect(path)
        conn.executescript("CREATE TABLE admissions(id INTEGER, admission_datetime TEXT, unit_scope TEXT, admission_type TEXT, merged_into_admission_id INTEGER); CREATE TABLE operation_cases(id INTEGER, started_at TEXT, status TEXT, admission_id INTEGER);")
        conn.execute("INSERT INTO admissions VALUES (1, '2026-01-02 10:00:00', 'rao', 'rao', NULL)")
        conn.execute("INSERT INTO admissions VALUES (2, '2026-01-02 10:00:00', 'operblock', 'operblock', NULL)")
        conn.execute("INSERT INTO operation_cases VALUES (1, '2026-01-03 10:00:00', 'completed', 2)"); conn.commit(); conn.close()
    repo = StatisticsRepository(db_paths=paths); period = AnalyticsPeriod.from_values("2026-01-01", "2026-01-31")
    rao, oper = repo.source_cases(MetricScope.RAO, period), repo.source_cases(MetricScope.OPERBLOCK, period)
    assert len(rao) == len(oper) == 2
    assert len({item.id for item in rao}) == 2 and len({item.id for item in oper}) == 2
    assert len({item.attributes["source_name"] for item in rao}) == 2


def test_repository_enriches_rao_and_operblock_source_cases_with_patient_name(tmp_path):
    path = tmp_path / "patient-names.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE patients(
            id INTEGER, full_name TEXT, last_name TEXT, first_name TEXT, middle_name TEXT
        );
        CREATE TABLE admissions(
            id INTEGER, patient_id INTEGER, history_number TEXT,
            admission_datetime TEXT, unit_scope TEXT
        );
        CREATE TABLE operation_cases(
            id INTEGER, patient_id INTEGER, admission_id INTEGER,
            started_at TEXT, status TEXT
        );
    """)
    conn.execute("INSERT INTO patients VALUES (1, '', 'Иванов', 'Иван', 'Иванович')")
    conn.execute("INSERT INTO admissions VALUES (7, 1, 'ИБ-7', '2026-01-02 08:00:00', 'rao')")
    conn.execute("INSERT INTO operation_cases VALUES (9, 1, 7, '2026-01-02 09:00:00', 'completed')")
    conn.commit(); conn.close()

    repository = StatisticsRepository(db_paths=(str(path),))
    period = AnalyticsPeriod.from_values("2026-01-01", "2026-01-03")
    rao_case = repository.source_cases(MetricScope.RAO, period)[0]
    operblock_case = repository.source_cases(MetricScope.OPERBLOCK, period)[0]

    assert rao_case.attributes["full_name"] == "Иванов Иван Иванович"
    assert operblock_case.attributes["full_name"] == "Иванов Иван Иванович"
    assert rao_case.attributes["history_number"] == "ИБ-7"
    assert operblock_case.attributes["history_number"] == "ИБ-7"


def test_memory_cohort_snapshot_filters_admissions_without_writing_source(tmp_path):
    path = tmp_path / "cohort.db"; conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE admissions(id INTEGER, admission_datetime TEXT, patient_gender TEXT, source_department TEXT); CREATE TABLE orders(id INTEGER, admission_id INTEGER);")
    conn.executemany("INSERT INTO admissions VALUES (?, '2026-01-02', ?, 'РАО')", [(1, 'ж'), (2, 'м')]); conn.executemany("INSERT INTO orders VALUES (?, ?)", [(1, 1), (2, 2)]); conn.commit()
    period = AnalyticsPeriod.from_values("2026-01-01", "2026-01-03")
    snapshot, cases = materialize_cohort_snapshot(AnalyticsConnectionManager(conn, db_path=str(path)), MetricScope.RAO, period, CohortDefinition(scope=MetricScope.RAO, filters=(CohortFilter("sex", "equals", "ж"),)))
    try:
        assert [case.local_id for case in cases] == ["1"]
        assert snapshot.get_connection().execute("SELECT id FROM admissions").fetchall() == [(1,)]
        assert snapshot.get_connection().execute("SELECT admission_id FROM orders").fetchall() == [(1,)]
        assert conn.execute("SELECT count(*) FROM admissions").fetchone()[0] == 2
    finally:
        snapshot.close_connection(); conn.close()


def test_legacy_schema_rao_to_or_to_rao_stays_one_rao_admission(tmp_path):
    path = tmp_path / "handoff.db"; conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE admissions(id INTEGER, admission_datetime TEXT); CREATE TABLE operation_cases(id INTEGER, admission_id INTEGER, started_at TEXT, status TEXT, source_rao_admission_id INTEGER);")
    conn.execute("INSERT INTO admissions VALUES (7, '2026-01-02 08:00:00')")
    conn.execute("INSERT INTO operation_cases VALUES (9, 7, '2026-01-03 09:00:00', 'completed', 7)"); conn.commit(); conn.close()
    repo = StatisticsRepository(db_paths=(str(path),)); period = AnalyticsPeriod.from_values("2026-01-01", "2026-01-04")
    assert [item.local_id for item in repo.source_cases(MetricScope.RAO, period)] == ["7"]
    assert [item.local_id for item in repo.source_cases(MetricScope.OPERBLOCK, period)] == ["9"]


def test_legacy_standalone_operblock_admission_without_scope_is_not_rao(tmp_path):
    path = tmp_path / "standalone-operblock.db"; conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE admissions(id INTEGER, admission_datetime TEXT); CREATE TABLE operation_cases(id INTEGER, admission_id INTEGER, started_at TEXT, status TEXT);")
    conn.execute("INSERT INTO admissions VALUES (1, '2026-01-02 08:00:00')")
    conn.execute("INSERT INTO operation_cases VALUES (1, 1, '2026-01-02 09:00:00', 'completed')")
    conn.commit(); conn.close()
    repo = StatisticsRepository(db_paths=(str(path),)); period = AnalyticsPeriod.from_values("2026-01-01", "2026-01-04")
    assert repo.source_cases(MetricScope.RAO, period) == ()
    assert [item.local_id for item in repo.source_cases(MetricScope.OPERBLOCK, period)] == ["1"]


def test_explicit_rao_admission_with_operation_remains_rao(tmp_path):
    path = tmp_path / "explicit-rao.db"; conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE admissions(id INTEGER, admission_datetime TEXT, unit_scope TEXT, admission_type TEXT); CREATE TABLE operation_cases(id INTEGER, admission_id INTEGER, started_at TEXT, status TEXT);")
    conn.execute("INSERT INTO admissions VALUES (1, '2026-01-02 08:00:00', 'rao', 'rao')")
    conn.execute("INSERT INTO operation_cases VALUES (3, 1, '2026-01-02 09:00:00', 'completed')")
    conn.commit(); conn.close()
    repo = StatisticsRepository(db_paths=(str(path),)); period = AnalyticsPeriod.from_values("2026-01-01", "2026-01-04")
    assert [item.local_id for item in repo.source_cases(MetricScope.RAO, period)] == ["1"]
    assert [item.local_id for item in repo.source_cases(MetricScope.OPERBLOCK, period)] == ["3"]


def test_source_overlap_uses_earliest_of_transfer_and_death(tmp_path):
    path = tmp_path / "terminals.db"; conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE admissions(id INTEGER, admission_datetime TEXT, transfer_datetime TEXT, death_datetime TEXT, unit_scope TEXT);")
    conn.executemany("INSERT INTO admissions VALUES (?, '2026-01-01', ?, ?, 'rao')", [
        (1, "2026-01-10", "2026-01-04"),
        (2, "2026-01-04", "2026-01-10"),
        (3, "2026-01-10", "2026-01-06"),
    ]); conn.commit(); conn.close()
    cases = StatisticsRepository(db_paths=(str(path),)).source_cases(MetricScope.RAO, AnalyticsPeriod.from_values("2026-01-05", "2026-01-06"))
    assert [case.local_id for case in cases] == ["3"]


def test_legacy_multi_db_snapshot_remaps_colliding_ids_and_relations(tmp_path):
    paths = []
    for index in (0, 1):
        path = tmp_path / f"source-{index}.db"; paths.append(str(path)); conn = sqlite3.connect(path)
        conn.executescript("CREATE TABLE admissions(id INTEGER, admission_datetime TEXT); CREATE TABLE operations(id INTEGER, admission_id INTEGER, operation_datetime TEXT, description TEXT);")
        conn.execute("INSERT INTO admissions VALUES (1, '2026-01-02')"); conn.execute("INSERT INTO operations VALUES (1, 1, '2026-01-02', 'op')"); conn.commit(); conn.close()
    manager = create_multi_db_analytics_manager(paths, start_dt="2026-01-01", end_dt="2026-01-03")
    try:
        conn = manager.get_connection(); admissions = {row[0] for row in conn.execute("SELECT id FROM admissions")}; linked = {row[0] for row in conn.execute("SELECT admission_id FROM operations")}
        assert len(admissions) == 2 and linked == admissions
    finally: manager.close_connection()
