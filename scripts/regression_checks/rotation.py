"""Safety-сценарии: rotation."""

from __future__ import annotations

from .common import PROJECT_ROOT
from pathlib import Path
import ast
import glob
import json
import os
import socket
import sqlite3
import time


def _create_db_cycle_fixture(path: str, *, admission_dt: str, active_bed: bool = False, cycle_days_old: int = 200) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for candidate in (path, f"{path}-journal", f"{path}-wal", f"{path}-shm"):
        try:
            os.remove(candidate)
        except FileNotFoundError:
            pass
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
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
                outcome TEXT,
                diagnosis_text TEXT,
                patient_age INTEGER,
                patient_months INTEGER,
                patient_age_unit TEXT,
                diagnosis_code TEXT,
                operation_description TEXT,
                emergency_notice_number TEXT,
                emergency_notice_entered_at TEXT
            );
            CREATE TABLE beds (
                id INTEGER PRIMARY KEY,
                number INTEGER,
                status TEXT,
                current_admission_id INTEGER
            );
            """
        )
        cycle_started = int(time.time() - cycle_days_old * 86400)
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('db_cycle_started_at', ?)",
            (str(cycle_started),),
        )
        conn.execute(
            "INSERT INTO patients (id, last_name, first_name, middle_name, full_name) VALUES (1, 'Тестов', 'Пациент', '', 'Тестов Пациент')"
        )
        conn.execute(
            """
            INSERT INTO admissions (
                id, patient_id, history_number, bed_number, admission_datetime,
                transfer_datetime, outcome, diagnosis_text, patient_age, patient_age_unit
            )
            VALUES (1, 1, 'ИБ-1', 1, ?, ?, 'переведен', 'Тест', 40, 'л')
            """,
            (admission_dt, admission_dt),
        )
        conn.execute(
            "INSERT INTO beds (id, number, status, current_admission_id) VALUES (1, 1, ?, ?)",
            ("OCCUPIED" if active_bed else "FREE", 1 if active_bed else None),
        )
        conn.commit()
    finally:
        conn.close()


def _create_operblock_cycle_fixture(
    path: str,
    *,
    started_at: str,
    full_name: str,
    history_number: str,
    table_code: str = "emergency",
) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for candidate in (path, f"{path}-journal", f"{path}-wal", f"{path}-shm"):
        try:
            os.remove(candidate)
        except FileNotFoundError:
            pass
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE operating_tables (
                code TEXT PRIMARY KEY,
                display_name TEXT,
                sort_order INTEGER
            );
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
                unit_scope TEXT,
                patient_gender TEXT,
                patient_age INTEGER,
                patient_months INTEGER,
                patient_age_unit TEXT,
                diagnosis_code TEXT,
                diagnosis_text TEXT,
                department_profile TEXT,
                source_department TEXT
            );
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
                height_cm INTEGER,
                weight_kg REAL,
                allergies TEXT,
                blood_group TEXT,
                blood_rh TEXT,
                preop_sys INTEGER,
                preop_dia INTEGER,
                preop_pulse INTEGER,
                preop_spo2 INTEGER,
                anesthesia_protocol_number INTEGER,
                anesthesia_protocol_date TEXT,
                transfer_department TEXT
            );
            CREATE TABLE operblock_timeline_events (
                id INTEGER PRIMARY KEY,
                operation_case_id INTEGER,
                admission_id INTEGER,
                table_code TEXT,
                event_type TEXT,
                event_time TEXT,
                end_time TEXT,
                drug_label TEXT,
                display_label TEXT,
                raw_text TEXT,
                dose_value TEXT,
                dose_unit TEXT,
                volume_ml TEXT,
                concentration_text TEXT,
                rate_value TEXT,
                rate_unit TEXT,
                route TEXT,
                status TEXT,
                revision INTEGER,
                source_order_id INTEGER,
                parent_event_id INTEGER,
                payload_json TEXT
            );
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                admission_id INTEGER,
                datetime TEXT,
                text TEXT,
                drug_key TEXT,
                status TEXT,
                comment TEXT
            );
            CREATE TABLE vitals (
                id INTEGER PRIMARY KEY,
                admission_id INTEGER,
                datetime TEXT,
                sys INTEGER,
                dia INTEGER,
                pulse INTEGER,
                temp REAL,
                spo2 INTEGER,
                rr INTEGER,
                cvp INTEGER
            );
            """
        )
        conn.execute("INSERT INTO meta (key, value) VALUES ('db_cycle_started_at', ?)", (str(int(time.time())),))
        conn.executemany(
            "INSERT INTO operating_tables (code, display_name, sort_order) VALUES (?, ?, ?)",
            [
                ("emergency", "Экстренная операционная", 1),
                ("planned", "Плановая операционная", 2),
            ],
        )
        conn.execute(
            "INSERT INTO patients (id, full_name, birth_date) VALUES (1, ?, '1980-01-01')",
            (full_name,),
        )
        conn.execute(
            """
            INSERT INTO admissions (
                id, patient_id, history_number, admission_datetime, unit_scope,
                patient_gender, patient_age, patient_months, patient_age_unit,
                diagnosis_code, diagnosis_text, department_profile, source_department
            ) VALUES (1, 1, ?, ?, 'operblock', 'м', 46, 552, 'л', 'K35', 'Аппендицит', 'хирургия', 'приёмное')
            """,
            (history_number, started_at),
        )
        conn.execute(
            """
            INSERT INTO operation_cases (
                id, patient_id, admission_id, table_code, status, created_at, started_at, ended_at,
                planned_operation_name, planned_surgeons_json, planned_operating_nurse,
                planned_anesthesiologist, planned_anesthetist,
                height_cm, weight_kg, allergies, blood_group, blood_rh, preop_sys, preop_dia, preop_pulse,
                preop_spo2, anesthesia_protocol_number, anesthesia_protocol_date, transfer_department
            ) VALUES (
                1, 1, 1, ?, 'closed', ?, ?, DATETIME(?, '+1 hour'),
                'Операция', '["Хирург"]', 'Оперсестра', 'Анестезиолог', 'Анестезистка',
                175, 70, '', 'O(I) первая', 'Rh(+) положительный', 120, 80, 80,
                99, 1, DATE(?), 'РАО'
            )
            """,
            (table_code, started_at, started_at, started_at, started_at),
        )
        conn.execute(
            """
            INSERT INTO operblock_timeline_events (
                id, operation_case_id, admission_id, table_code, event_type, event_time,
                drug_label, display_label, route, status, revision, payload_json
            ) VALUES (1, 1, 1, ?, 'bolus', DATETIME(?, '+10 minutes'), 'Тест препарат', 'Тест препарат', 'iv', 'active', 1, '{}')
            """,
            (table_code, started_at),
        )
        conn.execute(
            """
            INSERT INTO vitals (id, admission_id, datetime, sys, dia, pulse, temp, spo2, rr, cvp)
            VALUES (1, 1, DATETIME(?, '+5 minutes'), 120, 80, 80, 36.6, 99, NULL, NULL)
            """,
            (started_at,),
        )
        conn.commit()
    finally:
        conn.close()


class _SimpleDbManager:
    def __init__(self, db_path: str):
        from rem_card.app.sqlite_shared import configure_connection

        self.db_path = os.path.abspath(db_path)
        self.conn = sqlite3.connect(self.db_path)
        configure_connection(self.conn)

    def fetch_all_remcard(self, query, params=()):
        return self.conn.execute(query, tuple(params or ())).fetchall()

    def fetch_one_remcard(self, query, params=()):
        return self.conn.execute(query, tuple(params or ())).fetchone()

    def close(self):
        self.conn.close()


def _check_db_rotation_forbids_emergency_runtime(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.db_lifecycle import maybe_rotate_database_if_due

    baza = os.path.join(temp_root, "Baza_rotation_emergency")
    db_path = os.path.join(baza, "archiv", "rao_journal.db")
    _create_db_cycle_fixture(db_path, admission_dt="2026-01-10 08:00:00", active_bed=False)
    result = maybe_rotate_database_if_due(
        db_path=db_path,
        archive_dir=os.path.dirname(db_path),
        rotation_lock_path=os.path.join(baza, "archiv", "db_rotation.lock"),
        db_lock_path=os.path.join(baza, "archiv", "db.lock"),
        backup_dir=os.path.join(baza, "backups", "valid"),
        invalid_dir=os.path.join(baza, "backup_health", "invalid_backups"),
        runtime_mode="emergency",
        max_age_days=180,
    )
    if result.get("status") != "rotation_forbidden_runtime":
        return False, f"unexpected status: {result}"
    if glob.glob(os.path.join(baza, "archiv", "rao_journal_archived_*.db")):
        return False, "emergency runtime rotated DB"
    if glob.glob(os.path.join(baza, "backups", "valid", "*.db")):
        return False, "emergency runtime created pre-rotation backup"
    return True, "ok"


def _check_db_rotation_creates_validated_backup(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.db_lifecycle import maybe_rotate_database_if_due
    from rem_card.app.sqlite_shared import validate_sqlite_file

    baza = os.path.join(temp_root, "Baza_rotation_backup")
    db_path = os.path.join(baza, "archiv", "rao_journal.db")
    backup_dir = os.path.join(baza, "backups", "valid")
    _create_db_cycle_fixture(db_path, admission_dt="2026-01-10 08:00:00", active_bed=False)
    result = maybe_rotate_database_if_due(
        db_path=db_path,
        archive_dir=os.path.dirname(db_path),
        rotation_lock_path=os.path.join(baza, "archiv", "db_rotation.lock"),
        db_lock_path=os.path.join(baza, "archiv", "db.lock"),
        backup_dir=backup_dir,
        invalid_dir=os.path.join(baza, "backup_health", "invalid_backups"),
        runtime_mode="network",
        max_age_days=180,
        source="regression_rotation",
    )
    if result.get("status") != "rotated":
        return False, f"rotation failed: {result}"
    backup_path = result.get("backup_path")
    archived_path = result.get("archived_path")
    if not backup_path or not os.path.isfile(backup_path):
        return False, f"pre-rotation backup missing: {result}"
    ok, reason = validate_sqlite_file(backup_path)
    if not ok:
        return False, f"pre-rotation backup invalid: {reason}"
    if not archived_path or not os.path.isfile(archived_path):
        return False, f"archived db missing: {result}"
    if not os.path.isfile(db_path):
        return False, "fresh current DB was not created"
    meta_path = f"{backup_path}.meta.json"
    meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
    if meta.get("rotation", {}).get("source") != "regression_rotation":
        return False, f"rotation context missing in backup meta: {meta}"
    return True, "ok"


def _check_db_rotation_blocks_active_beds(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.db_lifecycle import maybe_rotate_database_if_due

    baza = os.path.join(temp_root, "Baza_rotation_active_beds")
    db_path = os.path.join(baza, "archiv", "rao_journal.db")
    backup_dir = os.path.join(baza, "backups", "valid")
    _create_db_cycle_fixture(db_path, admission_dt="2026-01-10 08:00:00", active_bed=True)
    result = maybe_rotate_database_if_due(
        db_path=db_path,
        archive_dir=os.path.dirname(db_path),
        rotation_lock_path=os.path.join(baza, "archiv", "db_rotation.lock"),
        db_lock_path=os.path.join(baza, "archiv", "db.lock"),
        backup_dir=backup_dir,
        invalid_dir=os.path.join(baza, "backup_health", "invalid_backups"),
        runtime_mode="network",
        max_age_days=180,
    )
    if result.get("status") != "deferred_active_beds":
        return False, f"active beds did not block rotation: {result}"
    if glob.glob(os.path.join(baza, "archiv", "rao_journal_archived_*.db")):
        return False, "active-bed rotation created archive"
    if glob.glob(os.path.join(backup_dir, "*.db")):
        return False, "active-bed rotation created backup before blocking"
    return True, "ok"


def _check_db_rotation_blocks_active_nurse_role(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.db_lifecycle import maybe_rotate_database_if_due
    from rem_card.app.role_session_lock import RoleSessionLock

    baza = os.path.join(temp_root, "Baza_rotation_active_nurse")
    db_path = os.path.join(baza, "archiv", "rao_journal.db")
    backup_dir = os.path.join(baza, "backups", "valid")
    nurse_lock_path = os.path.join(baza, "session_locks", "nurse.lock")
    _create_db_cycle_fixture(db_path, admission_dt="2026-01-10 08:00:00", active_bed=False)
    nurse_lock = RoleSessionLock(
        lock_path=nurse_lock_path,
        role="nurse",
        owner_id=f"{socket.gethostname()}:{os.getpid()}:regression_nurse",
        stale_timeout_sec=75.0,
        heartbeat_sec=60.0,
    )
    if not nurse_lock.acquire():
        return False, "failed to acquire nurse role lock"
    try:
        result = maybe_rotate_database_if_due(
            db_path=db_path,
            archive_dir=os.path.dirname(db_path),
            rotation_lock_path=os.path.join(baza, "archiv", "db_rotation.lock"),
            db_lock_path=os.path.join(baza, "archiv", "db.lock"),
            backup_dir=backup_dir,
            invalid_dir=os.path.join(baza, "backup_health", "invalid_backups"),
            runtime_mode="network",
            max_age_days=180,
            blocked_role_lock_paths={"nurse": nurse_lock_path},
        )
    finally:
        nurse_lock.release()
    if result.get("status") != "deferred_active_role_lock":
        return False, f"active nurse role did not block rotation: {result}"
    blocked_roles = result.get("blocked_roles") or []
    if not any((item or {}).get("role") == "nurse" for item in blocked_roles):
        return False, f"nurse role not reported in blockers: {result}"
    if glob.glob(os.path.join(baza, "archiv", "rao_journal_archived_*.db")):
        return False, "active-nurse rotation created archive"
    if glob.glob(os.path.join(backup_dir, "*.db")):
        return False, "active-nurse rotation created backup before blocking"
    return True, "ok"


def _check_db_rotation_blocks_network_emergency_nurse_marker(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.db_lifecycle import maybe_rotate_database_if_due
    from rem_card.app.role_session_lock import RoleSessionLock

    baza = os.path.join(temp_root, "Baza_rotation_network_emergency_nurse")
    db_path = os.path.join(baza, "archiv", "rao_journal.db")
    backup_dir = os.path.join(baza, "backups", "valid")
    marker_path = os.path.join(baza, "session_locks", "nurse_emergency.lock")
    _create_db_cycle_fixture(db_path, admission_dt="2026-01-10 08:00:00", active_bed=False)
    marker_lock = RoleSessionLock(
        lock_path=marker_path,
        role="nurse_emergency",
        owner_id=f"{socket.gethostname()}:{os.getpid()}:regression_nurse_emergency",
        stale_timeout_sec=75.0,
        heartbeat_sec=60.0,
    )
    if not marker_lock.acquire():
        return False, "failed to acquire emergency nurse role marker"
    try:
        result = maybe_rotate_database_if_due(
            db_path=db_path,
            archive_dir=os.path.dirname(db_path),
            rotation_lock_path=os.path.join(baza, "archiv", "db_rotation.lock"),
            db_lock_path=os.path.join(baza, "archiv", "db.lock"),
            backup_dir=backup_dir,
            invalid_dir=os.path.join(baza, "backup_health", "invalid_backups"),
            runtime_mode="network",
            max_age_days=180,
            blocked_role_lock_paths={"nurse_emergency": marker_path},
        )
    finally:
        marker_lock.release()
    if result.get("status") != "deferred_active_role_lock":
        return False, f"network emergency nurse marker did not block rotation: {result}"
    blocked_roles = result.get("blocked_roles") or []
    if not any((item or {}).get("role") == "nurse_emergency" for item in blocked_roles):
        return False, f"emergency nurse marker not reported in blockers: {result}"
    if glob.glob(os.path.join(baza, "archiv", "rao_journal_archived_*.db")):
        return False, "network emergency nurse marker rotation created archive"
    if glob.glob(os.path.join(backup_dir, "*.db")):
        return False, "network emergency nurse marker rotation created backup before blocking"
    return True, "ok"


def _check_db_rotation_blocks_active_emergency_nurse_session(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.db_lifecycle import maybe_rotate_database_if_due

    baza = os.path.join(temp_root, "Baza_rotation_active_emergency_nurse")
    emergency_root = os.path.join(temp_root, "emergency_root")
    db_path = os.path.join(baza, "archiv", "rao_journal.db")
    backup_dir = os.path.join(baza, "backups", "valid")
    _create_db_cycle_fixture(db_path, admission_dt="2026-01-10 08:00:00", active_bed=False)

    session_id = "session_nurse_active"
    session_dir = os.path.join(emergency_root, "active", session_id)
    os.makedirs(session_dir, exist_ok=True)
    metadata_path = os.path.join(session_dir, "emergency_session.json")
    Path(metadata_path).write_text(
        json.dumps(
            {
                "emergency_session_id": session_id,
                "status": "active",
                "source_role": "nurse",
                "source_machine": "nurse-pc",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = maybe_rotate_database_if_due(
        db_path=db_path,
        archive_dir=os.path.dirname(db_path),
        rotation_lock_path=os.path.join(baza, "archiv", "db_rotation.lock"),
        db_lock_path=os.path.join(baza, "archiv", "db.lock"),
        backup_dir=backup_dir,
        invalid_dir=os.path.join(baza, "backup_health", "invalid_backups"),
        runtime_mode="network",
        max_age_days=180,
        blocked_emergency_roots=[emergency_root],
    )
    if result.get("status") != "deferred_active_emergency_session":
        return False, f"active emergency nurse did not block rotation: {result}"
    blocked = result.get("blocked_emergency_sessions") or []
    if not any((item or {}).get("session_id") == session_id for item in blocked):
        return False, f"emergency session not reported in blockers: {result}"
    if glob.glob(os.path.join(baza, "archiv", "rao_journal_archived_*.db")):
        return False, "active emergency nurse rotation created archive"
    if glob.glob(os.path.join(backup_dir, "*.db")):
        return False, "active emergency nurse rotation created backup before blocking"
    return True, "ok"


def _check_db_rotation_new_db_failure_preserves_current(temp_root: str) -> tuple[bool, str]:
    import rem_card.app.db_lifecycle as lifecycle

    baza = os.path.join(temp_root, "Baza_rotation_new_db_failure")
    db_path = os.path.join(baza, "archiv", "rao_journal.db")
    backup_dir = os.path.join(baza, "backups", "valid")
    _create_db_cycle_fixture(db_path, admission_dt="2026-01-10 08:00:00", active_bed=False)
    original = Path(db_path).read_bytes()

    original_schema_init = lifecycle.ensure_unified_schema_with_migration_backup

    def fail_schema_init(*_args, **_kwargs):
        raise RuntimeError("forced fresh db init failure")

    lifecycle.ensure_unified_schema_with_migration_backup = fail_schema_init
    try:
        result = lifecycle.maybe_rotate_database_if_due(
            db_path=db_path,
            archive_dir=os.path.dirname(db_path),
            rotation_lock_path=os.path.join(baza, "archiv", "db_rotation.lock"),
            db_lock_path=os.path.join(baza, "archiv", "db.lock"),
            backup_dir=backup_dir,
            invalid_dir=os.path.join(baza, "backup_health", "invalid_backups"),
            runtime_mode="network",
            max_age_days=180,
        )
    finally:
        lifecycle.ensure_unified_schema_with_migration_backup = original_schema_init

    if result.get("status") != "new_db_failed":
        return False, f"unexpected status after forced new DB failure: {result}"
    if not result.get("current_preserved"):
        return False, f"result does not report preserved current DB: {result}"
    if not os.path.isfile(db_path):
        return False, "current DB disappeared after fresh DB init failure"
    if Path(db_path).read_bytes() != original:
        return False, "current DB changed after fresh DB init failure"
    if glob.glob(os.path.join(baza, "archiv", "rao_journal_archived_*.db")):
        return False, "fresh DB init failure created archive"
    if not glob.glob(os.path.join(backup_dir, "pre_rotation_*.db")):
        return False, "pre-rotation backup was not created before fresh DB init failure"
    return True, "ok"


def _check_archive_patients_sql_period_filter(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.dao.patient_dao import PatientDAO

    db_path = os.path.join(temp_root, "archive_period_filter.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
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
                bed_number INTEGER,
                admission_datetime TEXT,
                transfer_datetime TEXT,
                death_datetime TEXT,
                outcome TEXT,
                diagnosis_text TEXT,
                patient_age INTEGER,
                patient_months INTEGER,
                patient_age_unit TEXT,
                diagnosis_code TEXT,
                operation_description TEXT,
                emergency_notice_number TEXT,
                emergency_notice_entered_at TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO patients (id, last_name, first_name, middle_name, full_name) VALUES (1, 'Архивов', 'Январь', '', 'Архивов Январь')"
        )
        conn.execute(
            "INSERT INTO patients (id, last_name, first_name, middle_name, full_name) VALUES (2, 'Архивов', 'Февраль', '', 'Архивов Февраль')"
        )
        conn.execute(
            """
            INSERT INTO admissions (id, patient_id, history_number, bed_number, admission_datetime, transfer_datetime, outcome)
            VALUES (1, 1, 'JAN', 1, '2026-01-10 08:00:00', '2026-01-11 08:00:00', 'переведен')
            """
        )
        conn.execute(
            """
            INSERT INTO admissions (id, patient_id, history_number, bed_number, admission_datetime, transfer_datetime, outcome)
            VALUES (2, 2, 'FEB', 2, '2026-02-10 08:00:00', '2026-02-11 08:00:00', 'переведен')
            """
        )
        conn.commit()
    finally:
        conn.close()

    dao = object.__new__(PatientDAO)
    rows = dao._fetch_archived_rows_from_db(
        db_path,
        start_dt="2026-02-01 00:00:00",
        end_dt="2026-02-28 23:59:59",
    )
    history_numbers = {row.get("history_number") for row in rows}
    if history_numbers != {"FEB"}:
        return False, f"SQL period filter returned unexpected rows: {history_numbers}"
    return True, "ok"


def _check_auto_rotation_after_doctor_exit_only(temp_root: str) -> tuple[bool, str]:
    db_manager_path = PROJECT_ROOT / "data" / "dao" / "db_manager.py"
    main_path = PROJECT_ROOT / "app" / "main.py"
    db_manager_source = db_manager_path.read_text(encoding="utf-8")
    tree = ast.parse(db_manager_source)
    init_calls_rotation = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "DatabaseManager":
            continue
        for item in node.body:
            if not isinstance(item, ast.FunctionDef) or item.name != "__init__":
                continue
            for subnode in ast.walk(item):
                if isinstance(subnode, ast.Call):
                    func = subnode.func
                    if (
                        isinstance(func, ast.Attribute)
                        and func.attr == "_maybe_rotate_db_lifecycle"
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "self"
                    ):
                        init_calls_rotation = True
    if init_calls_rotation:
        return False, "DatabaseManager.__init__ still triggers automatic rotation"

    main_source = main_path.read_text(encoding="utf-8")
    required_markers = (
        "_run_doctor_exit_db_rotation",
        "maybe_rotate_database_after_doctor_exit",
        "exit_code == 0",
        "resources_shutdown_ok",
    )
    missing = [marker for marker in required_markers if marker not in main_source]
    if missing:
        return False, f"doctor-exit auto-rotation markers missing in main.py: {missing}"
    return True, "ok"


def _check_db_cycle_period_selection(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.db_cycle_registry import select_db_paths_for_period

    archiv = os.path.join(temp_root, "Baza_cycle_period", "archiv")
    current = os.path.join(archiv, "rao_journal.db")
    old = os.path.join(archiv, "rao_journal_archived_20240101_000000.db")
    newer = os.path.join(archiv, "rao_journal_archived_20250101_000000.db")
    _create_db_cycle_fixture(current, admission_dt="2026-05-10 08:00:00")
    _create_db_cycle_fixture(old, admission_dt="2024-03-10 08:00:00")
    _create_db_cycle_fixture(newer, admission_dt="2025-03-10 08:00:00")
    selected = select_db_paths_for_period(
        current_db_path=current,
        start_dt="2025-01-01 00:00:00",
        end_dt="2025-12-31 23:59:59",
    )
    selected_keys = {os.path.normcase(path) for path in selected}
    if os.path.normcase(newer) not in selected_keys:
        return False, f"period selection missed 2025 archive: {selected}"
    if os.path.normcase(old) in selected_keys or os.path.normcase(current) in selected_keys:
        return False, f"period selection included out-of-period DB: {selected}"
    return True, "ok"


def _check_operblock_archive_reads_rotated_db(temp_root: str) -> tuple[bool, str]:
    from rem_card.services.operblock_service import OperBlockService

    archiv = os.path.join(temp_root, "Baza_operblock_archive", "archiv")
    current = os.path.join(archiv, "rao_journal.db")
    old = os.path.join(archiv, "rao_journal_archived_20250101_000000.db")
    _create_operblock_cycle_fixture(
        current,
        started_at="2026-06-10 08:00:00",
        full_name="Текущий Пациент",
        history_number="CUR",
    )
    _create_operblock_cycle_fixture(
        old,
        started_at="2025-03-10 08:00:00",
        full_name="Архивный Пациент",
        history_number="OLD",
    )

    manager = _SimpleDbManager(current)
    try:
        service = OperBlockService(manager)
        cases = service.list_archived_operation_cases()
        by_history = {case.get("history_number"): case for case in cases}
        if {"CUR", "OLD"} - set(by_history):
            return False, f"operblock archive missed DB cycle cases: {by_history}"
        if not by_history["OLD"].get("is_external_archive"):
            return False, f"rotated operblock case is not marked external: {by_history['OLD']}"
        if by_history["CUR"].get("is_external_archive"):
            return False, f"current operblock case is incorrectly marked external: {by_history['CUR']}"

        selected = service.get_archive_db_paths_for_period(
            "2025-01-01 00:00:00",
            "2025-12-31 23:59:59",
        )
        selected_keys = {os.path.normcase(path) for path in selected}
        if os.path.normcase(old) not in selected_keys or os.path.normcase(current) in selected_keys:
            return False, f"operblock period DB selection is wrong: {selected}"

        period_cases = service.list_archived_operation_cases(
            start_dt="2025-01-01 00:00:00",
            end_dt="2025-12-31 23:59:59",
        )
        period_histories = {case.get("history_number") for case in period_cases}
        if period_histories != {"OLD"}:
            return False, f"operblock period archive returned unexpected cases: {period_histories}"
    finally:
        manager.close()
    return True, "ok"


def _check_operblock_statistics_reads_rotated_db_without_id_collision(temp_root: str) -> tuple[bool, str]:
    from rem_card.services.analytics.operblock_statistics_service import OperBlockStatisticsReportBuilder

    archiv = os.path.join(temp_root, "Baza_operblock_stats", "archiv")
    current = os.path.join(archiv, "rao_journal.db")
    old = os.path.join(archiv, "rao_journal_archived_20250101_000000.db")
    _create_operblock_cycle_fixture(
        current,
        started_at="2026-06-10 08:00:00",
        full_name="Текущий Пациент",
        history_number="CUR",
    )
    _create_operblock_cycle_fixture(
        old,
        started_at="2025-03-10 08:00:00",
        full_name="Архивный Пациент",
        history_number="OLD",
    )

    manager = _SimpleDbManager(current)
    try:
        builder = OperBlockStatisticsReportBuilder(
            manager,
            "2025-01-01 00:00:00",
            "2026-12-31 23:59:59",
            db_paths=[old, current],
        )
        context = builder._fetch_multi_db_context([old, current], builder.start_date_str, builder.end_date_str)
        case_ids = [int(row.get("operation_case_id") or 0) for row in context["cases"]]
        source_case_ids = [int(row.get("source_operation_case_id") or 0) for row in context["cases"]]
        if sorted(case_ids) != [1, 2]:
            return False, f"operblock multi-DB context did not remap case ids: {case_ids}"
        if source_case_ids != [1, 1]:
            return False, f"operblock source ids should preserve per-DB ids: {source_case_ids}"
        timeline_case_ids = {int(row.get("operation_case_id") or 0) for row in context["timeline"]}
        if timeline_case_ids != {1, 2}:
            return False, f"operblock timeline rows not mapped to both cases: {timeline_case_ids}"

        stats = builder._calculate_statistics()
        if int(stats.get("total") or 0) != 2:
            return False, f"operblock stats missed rotated DB cases: total={stats.get('total')}"
        if int(stats.get("bolus_count") or 0) != 2:
            return False, f"operblock stats lost timeline rows across rotated DBs: bolus_count={stats.get('bolus_count')}"
    finally:
        manager.close()
    return True, "ok"
