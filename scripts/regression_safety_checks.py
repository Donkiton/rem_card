#!/usr/bin/env python
"""
Regression checks for SQLite safety, local replica hygiene and backup cleanup gating.

Usage:
  set PYTHONPATH=C:\Project
  python C:\Project\rem_card\scripts\regression_safety_checks.py
"""

from __future__ import annotations

import ast
import glob
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
try:
    from _local_rem_card_bootstrap import bootstrap_local_rem_card

    bootstrap_local_rem_card()
except Exception:
    pass


def _make_temp_root() -> str:
    return tempfile.mkdtemp(prefix="remcard_regression_checks_")


def _prepare_import_environment(temp_root: str):
    # Isolate LOCALAPPDATA so tests do not touch real user cache.
    os.environ["LOCALAPPDATA"] = os.path.join(temp_root, "localappdata")
    os.environ["REMCARD_BAZA_DIR"] = os.path.join(temp_root, "Baza_rao3_jurnal")
    os.environ["REMCARD_LOCAL_LOGS_DIR"] = os.path.join(temp_root, "logs")
    os.environ["REMCARD_LOCAL_FIRST_SYNC"] = "1"
    os.environ["REMCARD_LOCAL_SYNC_INTERVAL_SEC"] = "999"
    os.environ["REMCARD_LOCAL_OUTBOX_SYNC"] = "0"
    os.environ["REMCARD_LOCAL_CACHE_RETENTION_DAYS"] = "3"
    os.environ["REMCARD_LOCAL_CACHE_MAX_FILES"] = "200"


def _check_dev_baza_dir_prefers_project_baza_name(temp_root: str) -> tuple[bool, str]:
    from rem_card.app import runtime_paths

    saved_env = os.environ.get(runtime_paths.DEV_BAZA_DIR_ENV)
    original_get_project_root = runtime_paths.get_project_root
    try:
        os.environ.pop(runtime_paths.DEV_BAZA_DIR_ENV, None)
        project_root = os.path.join(temp_root, "project_root")
        expected = os.path.join(project_root, runtime_paths.BAZA_DIR_NAME)
        legacy = os.path.join(project_root, "rework_baza")
        os.makedirs(expected, exist_ok=True)
        os.makedirs(legacy, exist_ok=True)
        runtime_paths.get_project_root = lambda: project_root

        resolved = runtime_paths.get_dev_baza_dir()
        if os.path.abspath(resolved) != os.path.abspath(expected):
            return False, f"dev baza dir should use project Baza_rao3_jurnal, got: {resolved}"

        override = os.path.join(temp_root, "explicit_dev_override")
        os.environ[runtime_paths.DEV_BAZA_DIR_ENV] = override
        if os.path.abspath(runtime_paths.get_dev_baza_dir()) != os.path.abspath(override):
            return False, "explicit REMCARD_DEV_BAZA_DIR override was not honored"
        return True, "ok"
    finally:
        runtime_paths.get_project_root = original_get_project_root
        if saved_env is None:
            os.environ.pop(runtime_paths.DEV_BAZA_DIR_ENV, None)
        else:
            os.environ[runtime_paths.DEV_BAZA_DIR_ENV] = saved_env


def _check_arbitrary_baza_dir_name_allowed(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.runtime_paths import (
        create_baza_structure_and_db,
        read_configured_baza_dir,
        validate_baza_dir_for_runtime,
        write_configured_baza_dir,
    )
    from rem_card.app.startup_db_guard import run_startup_db_guard

    saved_env = {
        key: os.environ.get(key)
        for key in ("REMCARD_BAZA_DIR", "REMCARD_DATA_PATH_CONFIG")
    }
    arbitrary_dir = os.path.join(temp_root, "custom_db_folder")
    config_path = os.path.join(temp_root, "runtime_config", "remcard_data_path.json")
    try:
        os.environ.pop("REMCARD_BAZA_DIR", None)
        os.environ["REMCARD_DATA_PATH_CONFIG"] = config_path

        ok, reason = create_baza_structure_and_db(arbitrary_dir)
        if not ok:
            return False, f"arbitrary folder create failed: {reason}"

        stored_config_path = write_configured_baza_dir(arbitrary_dir)
        if os.path.abspath(stored_config_path) != os.path.abspath(config_path):
            return False, f"unexpected config path: {stored_config_path}"
        if read_configured_baza_dir() != os.path.abspath(arbitrary_dir):
            return False, "configured arbitrary folder was not read back"

        valid, message = validate_baza_dir_for_runtime(arbitrary_dir)
        if not valid:
            return False, f"runtime validation rejected arbitrary folder: {message}"

        os.environ["REMCARD_BAZA_DIR"] = arbitrary_dir
        guard_result = run_startup_db_guard(role=None)
        if not guard_result.ok:
            return False, f"startup guard rejected arbitrary folder: {guard_result.user_message}"

        env = os.environ.copy()
        env.pop("REMCARD_BAZA_DIR", None)
        env["REMCARD_DATA_PATH_CONFIG"] = config_path
        env["PYTHONPATH"] = str(PROJECT_ROOT)
        fake_exe_dir = os.path.join(temp_root, "compiled_probe", "Prog")
        os.makedirs(fake_exe_dir, exist_ok=True)
        env["REMCARD_FAKE_EXE_DIR"] = fake_exe_dir
        script = r"""
from _local_rem_card_bootstrap import bootstrap_local_rem_card
bootstrap_local_rem_card()
import os
import sys
sys.frozen = True
sys.executable = os.path.join(os.environ["REMCARD_FAKE_EXE_DIR"], "RemCardDoctor.exe")
from rem_card.app.runtime_paths import resolve_baza_dir
from rem_card.app import paths
print(resolve_baza_dir())
print(paths.BAZA_DIR)
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(PROJECT_ROOT),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        if result.returncode != 0:
            return False, f"compiled path probe failed: {result.stderr[-500:]}"
        lines = [line.strip() for line in str(result.stdout or "").splitlines() if line.strip()]
        expected = os.path.abspath(arbitrary_dir)
        if lines[-2:] != [expected, expected]:
            return False, f"compiled path probe mismatch: {lines[-2:]}"
        return True, "ok"
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _check_lock_read_unavailable_not_stale(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.sqlite_shared import FileWriteLock, _LOCK_READ_UNAVAILABLE

    lock_path = os.path.join(temp_root, "db.lock")
    lock1 = FileWriteLock(lock_path, stale_timeout_sec=60.0)
    if not lock1.acquire(owner_id="owner_1", source="check_1"):
        return False, "owner_1 failed to acquire initial lock"

    lock2 = FileWriteLock(lock_path, stale_timeout_sec=60.0)
    lock2._try_read_payload = lambda: _LOCK_READ_UNAVAILABLE  # type: ignore[attr-defined]
    acquired_2 = lock2.acquire(owner_id="owner_2", source="check_2")

    try:
        if acquired_2:
            return False, "owner_2 should not acquire lock when lock payload is unreadable"
        if not os.path.exists(lock_path):
            return False, "lock file unexpectedly removed on unreadable payload"
        return True, "ok"
    finally:
        lock2.release()
        lock1.release()


def _check_role_lock_read_unavailable_blocks_acquire(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.role_session_lock import RoleSessionLock, _ROLE_LOCK_READ_UNAVAILABLE

    lock_path = os.path.join(temp_root, "role.lock")
    lock1 = RoleSessionLock(lock_path, role="doctor", owner_id="owner_1", stale_timeout_sec=60.0)
    if not lock1.acquire():
        return False, "owner_1 failed to acquire initial role lock"

    lock2 = RoleSessionLock(lock_path, role="doctor", owner_id="owner_2", stale_timeout_sec=60.0)
    lock2._read_payload = lambda: _ROLE_LOCK_READ_UNAVAILABLE  # type: ignore[method-assign]
    acquired_2 = lock2.acquire()

    try:
        if acquired_2:
            return False, "owner_2 should not acquire role lock when payload is unreadable"
        if not os.path.exists(lock_path):
            return False, "role lock file unexpectedly removed on unreadable payload"
        if "недоступен" not in lock2.describe_holder():
            return False, "role lock holder description did not report unreadable lock"
        return True, "ok"
    finally:
        lock2.release()
        lock1.release()


def _check_local_write_queue_shutdown_drains(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from rem_card.app.sqlite_shared import LocalWriteQueue

    queue = LocalWriteQueue()
    completed: list[int] = []
    lock = threading.Lock()

    for idx in range(8):
        def task(value=idx):
            time.sleep(0.01)
            with lock:
                completed.append(value)

        queue.submit(task, description=f"queue_drain_{idx}")

    queue.shutdown(timeout=2.0)

    if sorted(completed) != list(range(8)):
        return False, f"queued writes were not drained before shutdown: {completed}"

    try:
        queue.submit(lambda: None, description="after_shutdown")
    except RuntimeError:
        return True, "ok"
    return False, "queue accepted a write after shutdown"


def _check_sync_cursor_normalizes_timestamp_formats(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from rem_card.data.dao.sync_cursor import is_cursor_newer, make_sync_cursor, normalize_sync_cursor

    ts, row_id = normalize_sync_cursor({"updated_at": "2026-05-01T08:00:00", "id": 7})
    if (ts, row_id) != ("2026-05-01 08:00:00.000", 7):
        return False, f"unexpected normalized cursor: {(ts, row_id)}"
    if not is_cursor_newer("2026-05-01 09:00:00.000", 1, "2026-05-01T08:00:00", 999):
        return False, "space-separated newer timestamp did not beat T-separated older timestamp"
    cursor = make_sync_cursor("2026-05-01T08:00:00.123", 3)
    if cursor != {"updated_at": "2026-05-01 08:00:00.123", "id": 3}:
        return False, f"make_sync_cursor did not canonicalize timestamp: {cursor}"

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, updated_at TEXT)")
        conn.execute("INSERT INTO items(id, updated_at) VALUES (1, '2026-05-01T08:00:00')")
        conn.execute("INSERT INTO items(id, updated_at) VALUES (2, '2026-05-01 09:00:00.000')")
        last_sync_ts, last_sync_id = normalize_sync_cursor({"updated_at": "2026-05-01T08:00:00", "id": 1})
        rows = conn.execute(
            """
            SELECT id FROM items
            WHERE COALESCE(STRFTIME('%Y-%m-%d %H:%M:%f', updated_at), '') > ?
               OR (
                   COALESCE(STRFTIME('%Y-%m-%d %H:%M:%f', updated_at), '') = ?
                   AND id > ?
               )
            ORDER BY COALESCE(STRFTIME('%Y-%m-%d %H:%M:%f', updated_at), '') ASC, id ASC
            """,
            (last_sync_ts, last_sync_ts, last_sync_id),
        ).fetchall()
    finally:
        conn.close()

    if [row[0] for row in rows] != [2]:
        return False, f"SQLite normalized timestamp query returned unexpected rows: {rows}"
    return True, "ok"


def _check_change_log_lag_uses_utc_for_sqlite_timestamp(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from datetime import datetime, timezone

    import rem_card.services.data_update_monitor as monitor_module
    from rem_card.services.data_update_monitor import DataUpdateMonitor

    original_time = monitor_module.time.time
    try:
        monitor_module.time.time = lambda: datetime(2026, 5, 3, 8, 0, 1, tzinfo=timezone.utc).timestamp()
        lag_ms = DataUpdateMonitor._change_log_lag_ms(
            [{"changed_at": "2026-05-03 08:00:00"}]
        )
    finally:
        monitor_module.time.time = original_time

    if lag_ms is None or not (900 <= lag_ms <= 1100):
        return False, f"SQLite UTC timestamp lag was misread: {lag_ms}"
    return True, "ok"


def _check_startup_lock_timeout_messages(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from rem_card.app.startup_db_guard import _lock_timeout_user_message

    recovery = _lock_timeout_user_message("Could not acquire recovery lock: recovery.lock")
    if "восстанавливается" not in recovery:
        return False, f"unexpected recovery lock message: {recovery}"
    db_busy = _lock_timeout_user_message("Could not acquire db_profile lock: archiv/db.lock")
    if "занята" not in db_busy or "восстанавливается" in db_busy:
        return False, f"unexpected db lock message: {db_busy}"
    return True, "ok"


def _check_transaction_isolation(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.sqlite_shared import SQLiteWriteController, configure_connection

    db_path = os.path.join(temp_root, "tx_isolation.db")
    lock_path = os.path.join(temp_root, "tx_isolation.lock")

    conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None, timeout=5.0)
    configure_connection(conn, profile="network")
    conn.execute("CREATE TABLE test_rows(id INTEGER PRIMARY KEY AUTOINCREMENT, who TEXT)")
    controller = SQLiteWriteController(db_path=db_path, lock_path=lock_path, owner_id="regression_tx")

    start_evt = threading.Event()
    results: dict[str, str] = {}

    def writer_a():
        try:
            with controller.transaction(conn, source="writer_a") as cursor:
                cursor.execute("INSERT INTO test_rows(who) VALUES (?)", ("A1",))
                start_evt.set()
                time.sleep(0.45)
                raise RuntimeError("writer_a_forced_rollback")
        except Exception as exc:  # noqa: BLE001
            results["writer_a"] = str(exc)

    def writer_b():
        start_evt.wait(timeout=2.0)
        controller.execute(conn, "INSERT INTO test_rows(who) VALUES (?)", ("B1",), source="writer_b")
        results["writer_b"] = "ok"

    ta = threading.Thread(target=writer_a, daemon=True)
    tb = threading.Thread(target=writer_b, daemon=True)
    ta.start()
    tb.start()
    ta.join(timeout=5.0)
    tb.join(timeout=5.0)

    rows = [tuple(row) for row in conn.execute("SELECT who FROM test_rows ORDER BY id").fetchall()]
    conn.close()

    if rows != [("B1",)]:
        return False, f"unexpected rows after concurrent writes: {rows}"
    if results.get("writer_b") != "ok":
        return False, "writer_b did not complete successfully"
    if "writer_a_forced_rollback" not in results.get("writer_a", ""):
        return False, "writer_a rollback path was not triggered"
    return True, "ok"


def _check_read_your_writes_inside_transaction(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.dao.db_manager import DatabaseManager

    db_path = os.path.join(temp_root, "read_your_writes.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        manager.execute_remcard(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('tx_probe', 1)",
            source="regression_init",
        )
        # Let local-read grace expire to make sure test hits local-first branch without fix.
        time.sleep(2.1)

        with manager.remcard_transaction(source="regression_tx"):
            manager.execute_remcard(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('tx_probe', 2)",
                source="regression_update_inside_tx",
            )
            row = manager.fetch_one_remcard("SELECT value FROM meta WHERE key='tx_probe'")
            inside_value = int(row[0]) if row and row[0] is not None else None

        if inside_value != 2:
            return False, f"stale read inside transaction: expected 2, got {inside_value}"
        return True, "ok"
    finally:
        manager.close()


def _check_central_reads_split_from_write_connection(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.dao.db_manager import DatabaseManager

    db_path = os.path.join(temp_root, "central_read_split.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        # Force central path; this check is specifically about the central read connection.
        manager._local_replica = None
        manager.execute_remcard(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('read_split_probe', 1)",
            source="regression_read_split_init",
        )

        readonly_open_count = 0
        original_open = manager._open_readonly_central_connection

        def counted_open():
            nonlocal readonly_open_count
            readonly_open_count += 1
            return original_open()

        manager._open_readonly_central_connection = counted_open  # type: ignore[method-assign]

        outside_row = manager.fetch_one_remcard("SELECT value FROM meta WHERE key='read_split_probe'")
        if not outside_row or int(outside_row[0]) != 1:
            return False, "outside transaction read returned wrong value"
        if readonly_open_count != 1:
            return False, f"outside transaction did not use readonly central connection: {readonly_open_count}"

        with manager.remcard_transaction(source="regression_read_split_tx"):
            manager.execute_remcard(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('read_split_probe', 2)",
                source="regression_read_split_update_inside_tx",
            )
            inside_row = manager.fetch_one_remcard("SELECT value FROM meta WHERE key='read_split_probe'")
            if not inside_row or int(inside_row[0]) != 2:
                return False, "inside transaction did not see uncommitted write"

        if readonly_open_count != 1:
            return False, "inside transaction unexpectedly opened readonly central connection"

        read_started = threading.Event()
        read_finished = threading.Event()
        read_errors: list[str] = []

        def background_read():
            read_started.set()
            try:
                row = manager.fetch_one_remcard("SELECT value FROM meta WHERE key='read_split_probe'")
                if not row or int(row[0]) != 2:
                    read_errors.append("background read returned wrong value")
            except Exception as exc:
                read_errors.append(str(exc))
            finally:
                read_finished.set()

        manager._central_io_lock.acquire()
        try:
            thread = threading.Thread(target=background_read, daemon=True)
            thread.start()
            if not read_started.wait(1.0):
                return False, "background read did not start"
            if read_finished.wait(0.15):
                return False, "central read did not wait for central IO gate"
        finally:
            manager._central_io_lock.release()

        thread.join(timeout=2.0)
        if thread.is_alive():
            return False, "background read stayed blocked after central IO gate released"
        if read_errors:
            return False, read_errors[0]
        return True, "ok"
    finally:
        manager.close()


def _check_blood_plasma_key_ru_prescription_parse(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.dto.remcard_dto import OrderType
    from rem_card.ui.doctor_view.components.order_input_handler import OrderInputHandler

    cases = [
        (
            "blood",
            "Эр. масса [DOSE:350] [UNIT:мл] [ROUTE:инфузия] [KEY:blood] [RU]",
            350,
            60,
        ),
        (
            "plasma",
            "СЗП [DOSE:450] [UNIT:мл] [ROUTE:инфузия] [KEY:plasma] [RU]",
            450,
            0,
        ),
    ]
    for expected_key, text, expected_dose, expected_duration in cases:
        dto = OrderInputHandler.parse_input_to_dto(text, admission_id=3)
        if dto.drug_key != expected_key:
            return False, f"{expected_key}: wrong drug_key: {dto.drug_key}"
        if dto.dose_value != expected_dose:
            return False, f"{expected_key}: wrong dose_value: {dto.dose_value}"
        if dto.duration_min != expected_duration:
            return False, f"{expected_key}: duration lost: {dto.duration_min}"
        if dto.type != OrderType.INFUSION_CONTINUOUS:
            return False, f"{expected_key}: infusion type lost: {dto.type}"
        if not dto.specific_times:
            return False, f"{expected_key}: prescription did not get generated schedule times"
    return True, "ok"


def _check_order_input_real_examples(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.dto.remcard_dto import OrderType
    from rem_card.ui.doctor_view.components.order_input_handler import OrderInputHandler

    cases = [
        (
            "standard_infusion_with_route_duration",
            "цефтриаксон 1 + NaCl 0,9% 100 мл [ROUTE:инфузия] [DUR:60]",
            {
                "drug_key": "ceftriaxone",
                "latin": "Ceftriaxoni",
                "type": OrderType.INFUSION_CONTINUOUS,
                "dose_value": 1.0,
                "dose_unit": "g",
                "is_per_kg": False,
                "frequency": 1,
                "specific_times": ["08:00"],
                "duration_min": 60,
                "comment": "NaCl 0,9% 100 мл",
            },
        ),
        (
            "latin_prefix_kept_compatible",
            "S. Ceftriaxoni 1 + NaCl 0,9% 100 мл [DUR:60]",
            {
                "drug_key": "ceftriaxone",
                "latin": "Ceftriaxoni",
                "type": OrderType.INFUSION_CONTINUOUS,
                "dose_value": 1.0,
                "dose_unit": "g",
                "is_per_kg": False,
                "frequency": 1,
                "specific_times": ["08:00"],
                "duration_min": 60,
                "comment": "NaCl 0,9% 100 мл",
            },
        ),
        (
            "per_kg_unknown_drug",
            "норэпинефрин 0.2 мкг/кг/мин [DUR:-1]",
            {
                "drug_key": None,
                "latin": "Норэпинефрин",
                "type": OrderType.MEDICATION,
                "dose_value": 0.2,
                "dose_unit": "g",
                "is_per_kg": True,
                "frequency": 1,
                "specific_times": ["08:00"],
                "duration_min": -1,
                "comment": "",
            },
        ),
        (
            "manual_ru_with_route_duration",
            "ruki Контроль дренажа [ROUTE:процедура] [DUR:30] [RU]",
            {
                "drug_key": "ruchnoivvod",
                "latin": "ruki Контроль дренажа",
                "type": OrderType.INFUSION_CONTINUOUS,
                "dose_value": 0.0,
                "dose_unit": "",
                "is_per_kg": False,
                "frequency": 1,
                "specific_times": [],
                "duration_min": 30,
                "comment": "[ROUTE:процедура] [DUR:30]",
            },
        ),
        (
            "explicit_key_with_diluent",
            "Meropenemi 1 [KEY:meropenem] + NaCl 0,9% 100 мл [DUR:180]",
            {
                "drug_key": "meropenem",
                "latin": "Meropenemi",
                "type": OrderType.INFUSION_CONTINUOUS,
                "dose_value": 1.0,
                "dose_unit": "g",
                "is_per_kg": False,
                "frequency": 1,
                "specific_times": ["08:00"],
                "duration_min": 180,
                "comment": "NaCl 0,9% 100 мл",
            },
        ),
        (
            "end_of_day_legacy_text",
            "Пиперациллин 4 + NaCl 0,9% 100 мл до конца суток",
            {
                "drug_key": None,
                "latin": "Пиперациллин",
                "type": OrderType.MEDICATION,
                "dose_value": 4.0,
                "dose_unit": "g",
                "is_per_kg": False,
                "frequency": 1,
                "specific_times": ["08:00"],
                "duration_min": -1,
                "comment": "NaCl 0,9% 100 мл до конца суток",
            },
        ),
    ]

    for name, text, expected in cases:
        dto = OrderInputHandler.parse_input_to_dto(text, admission_id=3)
        actual = {
            "drug_key": dto.drug_key,
            "latin": dto.latin,
            "type": dto.type,
            "dose_value": dto.dose_value,
            "dose_unit": dto.dose_unit,
            "is_per_kg": dto.is_per_kg,
            "frequency": dto.frequency,
            "specific_times": dto.specific_times,
            "duration_min": dto.duration_min,
            "comment": dto.comment,
        }
        for key, expected_value in expected.items():
            if actual[key] != expected_value:
                return False, f"{name}: {key}={actual[key]!r}, expected {expected_value!r}"

    return True, "ok"


def _create_sqlite_file(path: str):
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS t(id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT OR REPLACE INTO t(id, v) VALUES (1, 'ok')")
        conn.commit()
    finally:
        conn.close()


def _connect_network_db(path: str):
    from rem_card.app.sqlite_shared import configure_connection

    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=5.0)
    configure_connection(conn, profile="network")
    return conn


def _schema_guard_paths(temp_root: str) -> dict[str, str]:
    return {
        "backup_dir": os.path.join(temp_root, "backups", "valid"),
        "invalid_dir": os.path.join(temp_root, "backup_health", "invalid_backups"),
        "policy_path": os.path.join(temp_root, "Baza_rao3_jurnal", "config", "client_policy.json"),
        "lock_path": os.path.join(temp_root, "Baza_rao3_jurnal", "archiv", "db.lock"),
        "baza_dir": os.path.join(temp_root, "Baza_rao3_jurnal"),
    }


def _seed_legacy_patients_table(conn):
    conn.execute(
        """
        CREATE TABLE patients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL
        )
        """
    )
    conn.execute("INSERT INTO patients(full_name) VALUES ('Legacy Patient')")


def _check_schema_migration_backup_fastpath_policy(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.schema_migration_guard import ensure_unified_schema_with_migration_backup
    from rem_card.app.sqlite_shared import SQLiteWriteController, validate_sqlite_file
    from rem_card.app.unified_db_schema import SCHEMA_MIN_MIGRATION_VERSION
    from rem_card.app.version import APP_VERSION

    db_path = os.path.join(temp_root, "legacy_schema.db")
    paths = _schema_guard_paths(temp_root)
    conn = _connect_network_db(db_path)
    try:
        _seed_legacy_patients_table(conn)
        controller = SQLiteWriteController(db_path=db_path, lock_path=paths["lock_path"], owner_id="schema_regression")
        result = ensure_unified_schema_with_migration_backup(
            conn,
            db_path=db_path,
            backup_dir=paths["backup_dir"],
            invalid_dir=paths["invalid_dir"],
            policy_path=paths["policy_path"],
            baza_dir=paths["baza_dir"],
            controller=controller,
            source="regression_schema_migration",
        )
        if not result.migrated or not result.backup_path:
            return False, f"migration did not report validated backup: {result}"
        ok, reason = validate_sqlite_file(result.backup_path)
        if not ok:
            return False, f"pre-migration backup is invalid: {reason}"

        backup_conn = sqlite3.connect(result.backup_path)
        try:
            backup_columns = {row[1] for row in backup_conn.execute("PRAGMA table_info(patients)").fetchall()}
        finally:
            backup_conn.close()
        if "admission_uid" in backup_columns:
            return False, "backup was created after ALTER TABLE patients.admission_uid"

        main_columns = {row[1] for row in conn.execute("PRAGMA table_info(patients)").fetchall()}
        if "admission_uid" not in main_columns:
            return False, "migration did not add patients.admission_uid"

        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        if not row or int(row[0] or 0) < SCHEMA_MIN_MIGRATION_VERSION:
            return False, f"schema_migrations did not reach {SCHEMA_MIN_MIGRATION_VERSION}: {row}"

        with open(paths["policy_path"], "r", encoding="utf-8") as fh:
            policy = json.load(fh)
        if str(policy.get("min_client_version")) != APP_VERSION:
            return False, f"client policy min version not raised to APP_VERSION: {policy}"
        policy["min_client_version"] = "1.5.2"
        with open(paths["policy_path"], "w", encoding="utf-8") as fh:
            json.dump(policy, fh, ensure_ascii=False, indent=2)

        import rem_card.app.schema_migration_guard as guard

        original_backup = guard.backup_connection
        guard.backup_connection = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("fastpath called backup"))
        try:
            second = ensure_unified_schema_with_migration_backup(
                conn,
                db_path=db_path,
                backup_dir=paths["backup_dir"],
                invalid_dir=paths["invalid_dir"],
                policy_path=paths["policy_path"],
                baza_dir=paths["baza_dir"],
                controller=controller,
                source="regression_schema_fastpath",
            )
        finally:
            guard.backup_connection = original_backup
        if second.migrated:
            return False, "fastpath-ready schema was migrated again"
        if not second.policy_updated:
            return False, "fastpath-ready schema did not repair stale min_client_version policy"
        with open(paths["policy_path"], "r", encoding="utf-8") as fh:
            repaired_policy = json.load(fh)
        if str(repaired_policy.get("min_client_version")) != APP_VERSION:
            return False, f"stale client policy was not repaired to APP_VERSION: {repaired_policy}"
        return True, "ok"
    finally:
        conn.close()


def _check_schema_migration_invalid_backup_blocks_ddl(temp_root: str) -> tuple[bool, str]:
    import rem_card.app.schema_migration_guard as guard

    from rem_card.app.sqlite_shared import SQLiteWriteController

    db_path = os.path.join(temp_root, "invalid_backup_blocks.db")
    paths = _schema_guard_paths(temp_root)
    conn = _connect_network_db(db_path)
    original_backup = guard.backup_connection
    try:
        _seed_legacy_patients_table(conn)
        controller = SQLiteWriteController(db_path=db_path, lock_path=paths["lock_path"], owner_id="invalid_backup")

        def fail_backup(*args, **kwargs):
            raise sqlite3.DatabaseError("backup validation failed: regression")

        guard.backup_connection = fail_backup
        try:
            guard.ensure_unified_schema_with_migration_backup(
                conn,
                db_path=db_path,
                backup_dir=paths["backup_dir"],
                invalid_dir=paths["invalid_dir"],
                policy_path=paths["policy_path"],
                baza_dir=paths["baza_dir"],
                controller=controller,
                source="regression_invalid_backup",
            )
        except sqlite3.DatabaseError:
            pass
        else:
            return False, "migration continued after invalid backup"

        columns = {row[1] for row in conn.execute("PRAGMA table_info(patients)").fetchall()}
        if "admission_uid" in columns:
            return False, "DDL ran despite failed pre-migration backup"
        return True, "ok"
    finally:
        guard.backup_connection = original_backup
        conn.close()


def _check_schema_migration_failure_rolls_back(temp_root: str) -> tuple[bool, str]:
    import rem_card.app.schema_migration_guard as guard

    from rem_card.app.sqlite_shared import SQLiteWriteController

    db_path = os.path.join(temp_root, "migration_failure.db")
    paths = _schema_guard_paths(temp_root)
    conn = _connect_network_db(db_path)
    original_ensure = guard.ensure_unified_schema
    try:
        _seed_legacy_patients_table(conn)
        controller = SQLiteWriteController(db_path=db_path, lock_path=paths["lock_path"], owner_id="migration_failure")

        def broken_migration(target_conn, logger=None):
            target_conn.execute("CREATE TABLE should_rollback(id INTEGER PRIMARY KEY)")
            raise RuntimeError("forced migration failure")

        guard.ensure_unified_schema = broken_migration
        try:
            guard.ensure_unified_schema_with_migration_backup(
                conn,
                db_path=db_path,
                backup_dir=paths["backup_dir"],
                invalid_dir=paths["invalid_dir"],
                policy_path=paths["policy_path"],
                baza_dir=paths["baza_dir"],
                controller=controller,
                source="regression_failed_migration",
            )
        except RuntimeError as exc:
            if "forced migration failure" not in str(exc):
                return False, f"unexpected migration failure: {exc}"
        else:
            return False, "broken migration unexpectedly succeeded"

        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='should_rollback'"
        ).fetchone()
        if row:
            return False, "DDL from failed migration was not rolled back"
        backups = [name for name in os.listdir(paths["backup_dir"]) if name.endswith(".db")]
        if not backups:
            return False, "failed migration did not create pre-migration backup"
        return True, "ok"
    finally:
        guard.ensure_unified_schema = original_ensure
        conn.close()


def _check_schema_migration_parallel_start(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.schema_migration_guard import ensure_unified_schema_with_migration_backup
    from rem_card.app.sqlite_shared import SQLiteWriteController

    db_path = os.path.join(temp_root, "parallel_schema.db")
    paths = _schema_guard_paths(temp_root)
    errors: list[str] = []
    results: list[bool] = []
    lock = threading.Lock()

    seed_conn = _connect_network_db(db_path)
    try:
        _seed_legacy_patients_table(seed_conn)
    finally:
        seed_conn.close()

    def worker(owner_id: str):
        conn = _connect_network_db(db_path)
        try:
            controller = SQLiteWriteController(db_path=db_path, lock_path=paths["lock_path"], owner_id=owner_id)
            result = ensure_unified_schema_with_migration_backup(
                conn,
                db_path=db_path,
                backup_dir=paths["backup_dir"],
                invalid_dir=paths["invalid_dir"],
                policy_path=paths["policy_path"],
                baza_dir=paths["baza_dir"],
                controller=controller,
                source="regression_parallel_schema",
            )
            with lock:
                results.append(bool(result.migrated))
        except Exception as exc:
            with lock:
                errors.append(str(exc))
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(f"parallel_{idx}",), daemon=True) for idx in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20.0)
    if any(thread.is_alive() for thread in threads):
        return False, "parallel schema migration threads did not finish"
    if errors:
        return False, f"parallel migration errors: {errors}"
    if sorted(results) != [False, True]:
        return False, f"expected exactly one migration and one fastpath skip, got {results}"
    return True, "ok"


def _check_old_client_blocked_by_policy(temp_root: str) -> tuple[bool, str]:
    import rem_card.app.startup_db_guard as guard

    paths = _schema_guard_paths(temp_root)
    os.makedirs(os.path.dirname(paths["policy_path"]), exist_ok=True)
    guard.update_client_policy_min_version(
        paths["policy_path"],
        "9.9.9",
        baza_dir=paths["baza_dir"],
        reason="regression_new_schema",
    )

    original_app_version = guard.APP_VERSION
    original_required = guard.REQUIRED_CLIENT_POLICY_VERSION
    try:
        guard.APP_VERSION = "1.0.0"
        guard.REQUIRED_CLIENT_POLICY_VERSION = "1.0.0"
        try:
            guard._load_or_create_client_policy(paths["baza_dir"], role="doctor")
        except guard.StartupPolicyError:
            return True, "ok"
        return False, "old client was not blocked by min_client_version"
    finally:
        guard.APP_VERSION = original_app_version
        guard.REQUIRED_CLIENT_POLICY_VERSION = original_required


def _prepare_recovery_baza(temp_root: str) -> dict[str, str]:
    baza_dir = os.path.join(temp_root, "Baza_rao3_jurnal")
    paths = {
        "baza_dir": baza_dir,
        "db_path": os.path.join(baza_dir, "archiv", "rao_journal.db"),
        "backup_dir": os.path.join(baza_dir, "backups", "valid"),
        "locks_dir": os.path.join(baza_dir, "locks"),
        "session_locks_dir": os.path.join(baza_dir, "session_locks"),
        "db_lock": os.path.join(baza_dir, "archiv", "db.lock"),
        "recovery_lock": os.path.join(baza_dir, "locks", "recovery.lock"),
    }
    for path in (
        os.path.dirname(paths["db_path"]),
        paths["backup_dir"],
        paths["locks_dir"],
        paths["session_locks_dir"],
        os.path.join(baza_dir, "backup_health", "invalid_backups"),
        os.path.join(baza_dir, "quarantine", "shared_db"),
        os.path.join(baza_dir, "logs"),
    ):
        os.makedirs(path, exist_ok=True)
    return paths


def _write_corrupt_file(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"not a sqlite database")


def _write_lock_payload(path: str, *, source: str, role: str | None = None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "timestamp": time.time(),
        "pid": 999999,
        "host": "other-host",
        "role": role,
        "source": source,
        "user_id": "regression_other",
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def _check_recovery_blocks_active_second_client(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.startup_db_guard import recover_shared_db_with_locks
    from rem_card.app.sqlite_shared import validate_sqlite_file

    paths = _prepare_recovery_baza(temp_root)
    healthy_backup = os.path.join(paths["backup_dir"], "healthy.db")
    _create_sqlite_file(healthy_backup)
    _write_corrupt_file(paths["db_path"])
    _write_lock_payload(os.path.join(paths["session_locks_dir"], "doctor.lock"), source="role", role="doctor")

    result = recover_shared_db_with_locks(
        baza_dir=paths["baza_dir"],
        db_path=paths["db_path"],
        role="nurse",
        failure_reason="quick_check failed: database disk image is malformed",
    )
    if result.ok:
        return False, "recovery succeeded despite active second client lock"
    ok, _reason = validate_sqlite_file(paths["db_path"])
    if ok:
        return False, "corrupt primary DB was replaced while second client was active"
    return True, "ok"


def _check_recovery_db_lock_busy_blocks_restore(temp_root: str) -> tuple[bool, str]:
    import rem_card.app.startup_db_guard as guard

    paths = _prepare_recovery_baza(temp_root)
    _create_sqlite_file(os.path.join(paths["backup_dir"], "healthy.db"))
    _write_corrupt_file(paths["db_path"])
    _write_lock_payload(paths["db_lock"], source="db_write")

    original_wait = guard.DB_LOCK_WAIT_SEC
    try:
        guard.DB_LOCK_WAIT_SEC = 0.1
        result = guard.recover_shared_db_with_locks(
            baza_dir=paths["baza_dir"],
            db_path=paths["db_path"],
            role="doctor",
            failure_reason="quick_check failed: malformed",
        )
    finally:
        guard.DB_LOCK_WAIT_SEC = original_wait
    if result.ok:
        return False, "recovery succeeded while db.lock was busy"
    return True, "ok"


def _check_recovery_lock_busy_blocks_restore(temp_root: str) -> tuple[bool, str]:
    import rem_card.app.startup_db_guard as guard

    paths = _prepare_recovery_baza(temp_root)
    _create_sqlite_file(os.path.join(paths["backup_dir"], "healthy.db"))
    _write_corrupt_file(paths["db_path"])
    _write_lock_payload(paths["recovery_lock"], source="recovery")

    original_wait = guard.RECOVERY_LOCK_WAIT_SEC
    try:
        guard.RECOVERY_LOCK_WAIT_SEC = 0.1
        result = guard.recover_shared_db_with_locks(
            baza_dir=paths["baza_dir"],
            db_path=paths["db_path"],
            role="doctor",
            failure_reason="quick_check failed: malformed",
        )
    finally:
        guard.RECOVERY_LOCK_WAIT_SEC = original_wait
    if result.ok:
        return False, "recovery succeeded while recovery.lock was busy"
    return True, "ok"


def _check_dbmanager_locked_quickcheck_does_not_restore(temp_root: str) -> tuple[bool, str]:
    import rem_card.data.dao.db_manager as dbm

    manager = dbm.DatabaseManager.__new__(dbm.DatabaseManager)
    manager.db_path = os.path.join(temp_root, "locked_not_corrupt.db")
    manager._remcard_conn = object()
    manager._should_run_startup_quickcheck = lambda: (True, None)
    manager._write_startup_quickcheck_ts = lambda *args, **kwargs: None
    manager._close_connections_for_restore = lambda: None
    manager._init_connections = lambda: None

    original_quick = dbm.run_quick_check
    original_recover = dbm.recover_shared_db_with_locks
    dbm.run_quick_check = lambda conn: (False, "database is locked")
    dbm.recover_shared_db_with_locks = lambda **kwargs: (_ for _ in ()).throw(AssertionError("restore called"))
    try:
        try:
            manager._verify_quick_integrity_or_restore()
        except RuntimeError as exc:
            if "confirmed corruption" not in str(exc):
                return False, f"unexpected locked-db error: {exc}"
            return True, "ok"
        return False, "locked quick_check did not fail"
    finally:
        dbm.run_quick_check = original_quick
        dbm.recover_shared_db_with_locks = original_recover


def _check_recovery_selects_next_valid_backup(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.sqlite_shared import validate_sqlite_file
    from rem_card.app.startup_db_guard import recover_shared_db_with_locks

    paths = _prepare_recovery_baza(temp_root)
    good_backup = os.path.join(paths["backup_dir"], "backup_older_good.db")
    bad_backup = os.path.join(paths["backup_dir"], "backup_latest_bad.db")
    _create_sqlite_file(good_backup)
    _write_corrupt_file(bad_backup)
    now = time.time()
    os.utime(good_backup, (now - 10, now - 10))
    os.utime(bad_backup, (now, now))
    _write_corrupt_file(paths["db_path"])

    result = recover_shared_db_with_locks(
        baza_dir=paths["baza_dir"],
        db_path=paths["db_path"],
        role="doctor",
        failure_reason="quick_check failed: database disk image is malformed",
    )
    if not result.ok:
        return False, f"recovery failed despite next valid backup: {result.technical_reason}"
    if os.path.normcase(os.path.abspath(result.restored_from)) != os.path.normcase(os.path.abspath(good_backup)):
        return False, f"wrong backup selected: {result.restored_from}"
    ok, reason = validate_sqlite_file(paths["db_path"])
    if not ok:
        return False, f"restored DB is invalid: {reason}"
    if os.path.exists(bad_backup):
        return False, "corrupt latest backup was not quarantined"
    return True, "ok"


def _check_local_metrics_written_locally(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.local_metrics import flush_metrics, record_metric
    from rem_card.app.runtime_paths import get_local_logs_dir

    _ = temp_root
    record_metric("regression_metric_probe", 1, component="regression")
    flush_metrics(timeout=1.0)
    metrics_dir = get_local_logs_dir()
    files = [
        os.path.join(metrics_dir, name)
        for name in os.listdir(metrics_dir)
        if name.startswith("metrics_") and name.endswith(".jsonl")
    ]
    if not files:
        return False, "local metrics file was not created"
    newest = max(files, key=os.path.getmtime)
    with open(newest, "r", encoding="utf-8") as fh:
        content = fh.read()
    if "regression_metric_probe" not in content:
        return False, "metric probe was not written to local metrics log"
    baza_dir = os.environ.get("REMCARD_BAZA_DIR") or ""
    if baza_dir and os.path.normcase(os.path.abspath(newest)).startswith(os.path.normcase(os.path.abspath(baza_dir))):
        return False, f"metrics file was written inside shared baza dir: {newest}"
    return True, "ok"


def _check_local_metrics_are_buffered(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    root = Path(__file__).resolve().parents[1]
    source_path = root / "app/local_metrics.py"
    source_text = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text)
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    required = {"record_metric", "flush_metrics", "shutdown_metrics", "_metrics_worker", "_write_payloads"}
    missing = sorted(required - set(functions))
    if missing:
        return False, f"local_metrics missing buffered helpers: {missing}"

    record_source = ast.get_source_segment(source_text, functions["record_metric"]) or ""
    if "put_nowait" not in record_source:
        return False, "record_metric must enqueue without blocking the read path"
    if "_write_payloads([payload])" not in record_source:
        return False, "record_metric must keep a sync/forced-flush escape hatch"
    if "open(" in record_source or "_metrics_path()" in record_source:
        return False, "record_metric hot path must not open metrics files directly"
    if "REMCARD_LOCAL_METRICS_SYNC" not in source_text:
        return False, "local metrics sync fallback env flag is missing"
    if "RemCardLocalMetricsWriter" not in source_text:
        return False, "local metrics background writer thread is missing"

    return True, "ok"


def _check_sector_ivl_enqueue_error_refreshes(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime
    from types import SimpleNamespace

    from PySide6.QtWidgets import QApplication

    from rem_card.ui.rem_card_sectors import sector_ivl

    _ = temp_root
    app = QApplication.instance() or QApplication([])

    class FakeIvlService:
        def __init__(self):
            self.enqueue_called = False
            self.summary_reads = 0

        def enqueue_write(self, description, operation, on_success=None, on_error=None):
            self.enqueue_called = True
            if on_error:
                on_error(RuntimeError("forced ivl write failure"))

        def get_ventilation_summary(self, admission_id):
            self.summary_reads += 1
            return {"active_case": None, "total_duration_seconds": 0}

        def get_ventilation_timeline(self, admission_id):
            return []

        def get_latest_ventilation_case(self, admission_id):
            return None

        def get_latest_change_id(self, admission_id=None, include_global=True):
            return self.summary_reads

        def get_patient(self, admission_id):
            return SimpleNamespace(admission_datetime=datetime(2026, 5, 3, 8, 0))

    warnings: list[str] = []
    original_warning = sector_ivl.CustomMessageBox.warning
    sector_ivl.CustomMessageBox.warning = lambda parent, title, message: warnings.append(f"{title}: {message}")
    widget = sector_ivl.SectorIvl()
    service = FakeIvlService()
    try:
        widget.set_runtime_context(service, 1)
        widget._enqueue_ivl_write(
            "regression_ivl_error",
            lambda: None,
            pending_text="Случай: сохранение...",
            error_title="Ошибка ИВЛ",
        )
        app.processEvents()
        if not service.enqueue_called:
            return False, "SectorIvl did not use enqueue_write"
        if widget._ivl_write_pending:
            return False, "SectorIvl kept pending state after write error"
        if not warnings or "forced ivl write failure" not in warnings[-1]:
            return False, f"SectorIvl did not show write error warning: {warnings}"
        if service.summary_reads < 2:
            return False, "SectorIvl did not refresh from DB/service after write error"
        return True, "ok"
    finally:
        sector_ivl.CustomMessageBox.warning = original_warning
        widget.close()


def _check_balance_controller_enqueue_error_refreshes(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    from PySide6.QtWidgets import QApplication

    from rem_card.ui.shared.components import balance_controller as balance_module
    from rem_card.ui.shared.components.balance_controller import BalanceController

    _ = temp_root
    app = QApplication.instance() or QApplication([])

    class FakeButton:
        def __init__(self):
            self.enabled = True

        def setEnabled(self, enabled):
            self.enabled = bool(enabled)

        def isEnabled(self):
            return self.enabled

    class FakeLabel:
        def __init__(self):
            self.text = ""

        def setText(self, text):
            self.text = text

    class FakePanel:
        def __init__(self):
            self.edit_input = FakeButton()
            self.btn_save = FakeButton()
            self.btn_delete = FakeButton()
            self.btn_undo = FakeButton()
            self.status_lbl = FakeLabel()

        def set_selection(self, label_text, current_val=None, keep_focus=True):
            self.last_selection = (label_text, current_val, keep_focus)

        def set_undo_active(self, active):
            self.btn_undo.setEnabled(active)

    class FakeGrid:
        def __init__(self):
            self.enabled = True
            self.rows_map = ["urine", "drain_output", "ng_output", "stool", "other_output"]
            self.row_labels = ["Диурез", "Дренажи", "ЖКТ (зонд)", "Рвота", "Другое"]

        def setEnabled(self, enabled):
            self.enabled = bool(enabled)

        def update_data(self, hourly_data):
            self.hourly_data = hourly_data

        def currentRow(self):
            return 0

        def currentColumn(self):
            return 0

        def get_selected_info(self):
            return "urine", 8, 0

    class FakeVitalService:
        def get_effective_bounds(self, admission_id, shift_date):
            return shift_date - timedelta(hours=1), shift_date + timedelta(hours=23)

    class FakeFluidService:
        def __init__(self):
            self.vital_service = FakeVitalService()
            self.enqueue_called = False
            self.refresh_reads = 0
            self.on_error = None

        def enqueue_write(self, description, operation, on_success=None, on_error=None):
            self.enqueue_called = True
            self.description = description
            self.operation = operation
            self.on_error = on_error

        def upsert_hourly_output(self, **kwargs):
            raise AssertionError("queued write operation should not run in UI thread")

        def get_fluids(self, admission_id, shift_date):
            self.refresh_reads += 1
            return []

    critical_messages: list[str] = []
    original_critical = balance_module.CustomMessageBox.critical
    balance_module.CustomMessageBox.critical = lambda parent, title, message: critical_messages.append(f"{title}: {message}")
    try:
        shift_date = datetime(2026, 5, 3, 8, 0)
        service = FakeFluidService()
        controller = BalanceController(service, admission_id=1, shift_date=shift_date)
        controller.grid = FakeGrid()
        controller.panel_2d = FakePanel()
        controller._effective_bounds_cache = (shift_date - timedelta(hours=1), shift_date + timedelta(hours=23))

        controller._process_update("urine", 8, 100, is_sum=False)
        app.processEvents()
        if not service.enqueue_called:
            return False, "BalanceController did not use enqueue_write"
        if not controller._write_pending:
            return False, "BalanceController did not enter pending state"
        if controller.grid.enabled or controller.panel_2d.btn_save.enabled:
            return False, "BalanceController did not disable write UI while pending"
        if not service.on_error:
            return False, "BalanceController did not register error callback"

        service.on_error(RuntimeError("forced balance write failure"))
        app.processEvents()
        if controller._write_pending:
            return False, "BalanceController kept pending state after write error"
        if not controller.grid.enabled:
            return False, "BalanceController did not re-enable UI after write error"
        if controller._undo_stack:
            return False, f"BalanceController added undo state after failed write: {controller._undo_stack}"
        if service.refresh_reads < 1:
            return False, "BalanceController did not refresh from DB/service after write error"
        if not critical_messages or "forced balance write failure" not in critical_messages[-1]:
            return False, f"BalanceController did not show write error: {critical_messages}"
        return True, "ok"
    finally:
        balance_module.CustomMessageBox.critical = original_critical


def _check_diet_intake_enqueue_error_refreshes(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    from PySide6.QtWidgets import QApplication

    from rem_card.ui.shared.components import diet_intake_widget as diet_module
    from rem_card.ui.shared.components.diet_intake_widget import DietIntakeWidget

    _ = temp_root
    app = QApplication.instance() or QApplication([])

    class FakeDietService:
        def __init__(self):
            self.enqueue_called = False
            self.on_error = None
            self.refresh_reads = 0

        def enqueue_write(self, description, operation, on_success=None, on_error=None):
            self.enqueue_called = True
            self.description = description
            self.operation = operation
            self.on_error = on_error

        def list_diet_templates(self):
            self.refresh_reads += 1
            return []

        def get_diet_plan(self, admission_id, shift_date):
            return None

        def get_oral_intake_events(self, admission_id, shift_date):
            return []

        def get_latest_change_id(self, admission_id=None, include_global=True):
            return self.refresh_reads

        def current_shift_time(self, shift_date):
            return "08:00"

    warnings: list[str] = []
    original_warning = diet_module.CustomMessageBox.warning
    diet_module.CustomMessageBox.warning = lambda parent, title, message: warnings.append(f"{title}: {message}")
    try:
        service = FakeDietService()
        widget = DietIntakeWidget(service=service, role="nurse")
        widget.admission_id = 1
        widget.shift_date = datetime(2026, 5, 3, 8, 0)
        widget.btn_save.setEnabled(True)
        widget.btn_cancel.setEnabled(True)

        widget._enqueue_write("regression_diet_error", lambda: None)
        app.processEvents()
        if not service.enqueue_called:
            return False, "DietIntakeWidget did not use enqueue_write"
        if not widget._write_pending:
            return False, "DietIntakeWidget did not enter pending state"
        if widget.btn_save.isEnabled():
            return False, "DietIntakeWidget did not disable save while pending"
        if not service.on_error:
            return False, "DietIntakeWidget did not register error callback"

        service.on_error(RuntimeError("forced diet write failure"))
        app.processEvents()
        if widget._write_pending:
            return False, "DietIntakeWidget kept pending state after write error"
        if not widget.btn_save.isEnabled():
            return False, "DietIntakeWidget did not re-enable save after write error"
        if service.refresh_reads < 1:
            return False, "DietIntakeWidget did not refresh from service after write error"
        if not warnings or "forced diet write failure" not in warnings[-1]:
            return False, f"DietIntakeWidget did not show write error: {warnings}"
        return True, "ok"
    finally:
        diet_module.CustomMessageBox.warning = original_warning
        try:
            widget.close()
        except Exception:
            pass


def _check_oral_intake_batch_rolls_back(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.data.dao.diet_dao import OralIntakeDAO
    from rem_card.data.dao.exceptions import OptimisticLockError
    from rem_card.data.dao.patient_dao import PatientDAO
    from rem_card.services.diet_service import OralIntakeService
    from rem_card.services.vital_service import VitalService

    db_path = os.path.join(temp_root, "oral_intake_batch.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        admission_dt = datetime(2026, 4, 24, 8, 0, 0)
        existing_dt = datetime(2026, 4, 24, 10, 0, 0)
        new_dt = datetime(2026, 4, 24, 9, 0, 0)
        with manager.remcard_transaction(source="regression_seed_oral_batch") as cursor:
            cursor.execute(
                """
                INSERT INTO patients (full_name, last_name, first_name, middle_name)
                VALUES (?, ?, ?, ?)
                """,
                ("Петров Петр", "Петров", "Петр", None),
            )
            patient_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO admissions (
                    patient_id,
                    bed_number,
                    history_number,
                    admission_datetime,
                    is_active
                )
                VALUES (?, ?, ?, ?, 1)
                """,
                (patient_id, 1, "REG-DIET-001", admission_dt.isoformat()),
            )
            admission_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO oral_intake_events (
                    admission_id, shift_start, event_time, amount_ml, version, last_modified_by, updated_at
                )
                VALUES (?, ?, ?, 50, 1, 'nurse', STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
                """,
                (
                    admission_id,
                    admission_dt.strftime("%Y-%m-%d %H:%M"),
                    existing_dt.strftime("%Y-%m-%d %H:%M"),
                ),
            )

        vital_service = VitalService(vitals_dao=None, patient_dao=PatientDAO(manager), status_service=None)
        oral_service = OralIntakeService(OralIntakeDAO(manager), vital_service)

        try:
            oral_service.apply_changes(
                admission_id,
                [
                    {"event_dt": new_dt, "amount": 100, "expected_version": None},
                    {"event_dt": existing_dt, "amount": 250, "expected_version": 999},
                ],
            )
        except OptimisticLockError:
            pass
        else:
            return False, "batch did not raise optimistic lock conflict"

        inserted = oral_service.dao.get_event_at(admission_id, new_dt)
        if inserted is not None:
            return False, "first batch change was committed despite later failure"
        existing = oral_service.dao.get_event_at(admission_id, existing_dt)
        if existing is None or int(existing.amount_ml) != 50 or int(existing.version) != 1:
            return False, f"existing oral event changed despite rollback: {existing}"
        return True, "ok"
    finally:
        manager.close()


def _check_patient_form_enqueue_error_keeps_dialog(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    from PySide6.QtWidgets import QApplication

    from rem_card.ui.patient_bed_management import patient_form as patient_form_module
    from rem_card.ui.patient_bed_management.patient_form import PatientForm

    _ = temp_root
    app = QApplication.instance() or QApplication([])

    class FakePatientBedService:
        def __init__(self):
            self.enqueue_called = False
            self.on_error = None

        def enqueue_write(self, description, operation, on_success=None, on_error=None):
            self.enqueue_called = True
            self.description = description
            self.operation = operation
            self.on_error = on_error

    class FakeGeneralTab:
        def get_data(self):
            return {
                "history_number": "REG-PAT-001",
                "full_name": "Иванов Иван",
                "admission_datetime": datetime(2026, 5, 3, 8, 0),
                "age_value": 40,
                "months": None,
                "age_unit": "лет",
                "gender": "М",
                "department_profile": "ОАР",
                "source_department": "Приемное",
            }

    class FakeDiagnosisTab:
        def get_data(self):
            return {"diagnosis_code": "A00", "diagnosis_text": "Тестовый диагноз"}

    warnings: list[str] = []
    original_warning = patient_form_module.CustomMessageBox.warning
    patient_form_module.CustomMessageBox.warning = lambda parent, title, message: warnings.append(f"{title}: {message}")
    form = None
    try:
        service = FakePatientBedService()
        form = PatientForm(service, 1)
        form.general_tab = FakeGeneralTab()
        form.diagnosis_tab = FakeDiagnosisTab()
        form._save_data()
        app.processEvents()
        if not service.enqueue_called:
            return False, "PatientForm did not use enqueue_write"
        if not form._write_pending:
            return False, "PatientForm did not enter pending state"
        if form.save_button.isEnabled() or form.save_button.text() != "СОХРАНЕНИЕ...":
            return False, "PatientForm did not show pending save state"
        if not service.on_error:
            return False, "PatientForm did not register error callback"
        service.on_error(RuntimeError("forced patient form failure"))
        app.processEvents()
        if form._write_pending:
            return False, "PatientForm kept pending state after error"
        if not form.save_button.isEnabled() or form.save_button.text() != "СОХРАНИТЬ КАРТОЧКУ":
            return False, "PatientForm did not restore save button after error"
        if not warnings or "forced patient form failure" not in warnings[-1]:
            return False, f"PatientForm did not show write error: {warnings}"
        return True, "ok"
    finally:
        patient_form_module.CustomMessageBox.warning = original_warning
        if form is not None:
            form.close()


def _check_patient_bed_move_enqueue_error_refreshes(temp_root: str) -> tuple[bool, str]:
    from PySide6.QtWidgets import QApplication

    from rem_card.ui.patient_bed_management import management_widget as management_module
    from rem_card.ui.patient_bed_management.management_widget import NUM_BEDS, PatientBedManagementWidget

    _ = temp_root
    app = QApplication.instance() or QApplication([])

    class FakePatientBedService:
        def __init__(self):
            self.enqueue_called = False
            self.on_error = None
            self.refresh_reads = 0

        def get_bed_by_number(self, bed_number):
            if int(bed_number) == 1:
                return {"bed_number": 1, "status": "OCCUPIED", "current_admission_id": 10}
            return {"bed_number": int(bed_number), "status": "FREE", "current_admission_id": None}

        def enqueue_write(self, description, operation, on_success=None, on_error=None):
            self.enqueue_called = True
            self.description = description
            self.operation = operation
            self.on_error = on_error

        def get_beds_snapshot(self):
            self.refresh_reads += 1
            return [
                {
                    "bed_number": idx,
                    "status": "FREE",
                    "current_admission_id": None,
                    "full_name": "",
                    "history_number": "",
                    "diagnosis_text": "",
                }
                for idx in range(1, NUM_BEDS + 1)
            ]

        def get_patient_with_current_admission(self, bed_number):
            return None, None

    warnings: list[str] = []
    original_question = management_module.CustomMessageBox.question
    original_warning = management_module.CustomMessageBox.warning
    management_module.CustomMessageBox.question = lambda *args, **kwargs: management_module.CustomMessageBox.Yes
    management_module.CustomMessageBox.warning = lambda parent, title, message: warnings.append(f"{title}: {message}")
    widget = None
    try:
        widget = PatientBedManagementWidget(db_manager=object())
        service = FakePatientBedService()
        widget.patient_bed_service = service
        widget.move_patient(1, 2)
        app.processEvents()
        if not service.enqueue_called:
            return False, "PatientBedManagementWidget did not use enqueue_write"
        if not widget._move_pending:
            return False, "PatientBedManagementWidget did not enter move pending state"
        if any(bed.isEnabled() for bed in widget.bed_widgets):
            return False, "PatientBedManagementWidget did not disable bed widgets while pending"
        if not service.on_error:
            return False, "PatientBedManagementWidget did not register error callback"
        service.on_error(RuntimeError("forced bed move failure"))
        app.processEvents()
        if widget._move_pending:
            return False, "PatientBedManagementWidget kept pending state after error"
        if not all(bed.isEnabled() for bed in widget.bed_widgets):
            return False, "PatientBedManagementWidget did not re-enable bed widgets after error"
        if service.refresh_reads < 1:
            return False, "PatientBedManagementWidget did not refresh beds after error"
        if not warnings or "forced bed move failure" not in warnings[-1]:
            return False, f"PatientBedManagementWidget did not show move error: {warnings}"
        return True, "ok"
    finally:
        management_module.CustomMessageBox.question = original_question
        management_module.CustomMessageBox.warning = original_warning
        if widget is not None:
            widget.close()


def _check_archive_delete_enqueue_error_refreshes(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime
    from types import SimpleNamespace

    from PySide6.QtWidgets import QApplication

    from rem_card.ui.doctor_view import archive_widget as archive_module
    from rem_card.ui.doctor_view.archive_widget import ArchiveWidget

    _ = temp_root
    app = QApplication.instance() or QApplication([])

    class FakeArchivePatient(SimpleNamespace):
        def get_display_name(self):
            return self.full_name

    class FakeWriteService:
        def __init__(self, result=None):
            self.result = result
            self.enqueue_called = False
            self.on_error = None
            self.on_success = None

        def enqueue_write(self, description, operation, on_success=None, on_error=None):
            self.enqueue_called = True
            self.description = description
            self.operation = operation
            self.on_success = on_success
            self.on_error = on_error

        def get_archived_patients(self):
            return []

        def delete_admission(self, admission_id):
            return self.result

        def delete_last_card(self, admission_id):
            return self.result

    warnings: list[str] = []
    original_question = archive_module.CustomMessageBox.question
    original_warning = archive_module.CustomMessageBox.warning
    archive_module.CustomMessageBox.question = lambda *args, **kwargs: archive_module.CustomMessageBox.Yes
    archive_module.CustomMessageBox.warning = lambda parent, title, message: warnings.append(f"{title}: {message}")
    widget = None
    try:
        patient_service = FakeWriteService()
        remcard_service = FakeWriteService(result=(True, None, "ok"))
        widget = ArchiveWidget(patient_service, remcard_service=remcard_service)
        load_calls = []
        widget.load_data = lambda: load_calls.append(True)
        patient = FakeArchivePatient(
            id=1,
            full_name="Иванов Иван",
            history_number="REG-ARCH-001",
            diagnosis_text="Тест",
            admission_datetime=datetime(2026, 5, 3, 8, 0),
            transfer_datetime=datetime(2026, 5, 4, 8, 0),
            is_external_archive=False,
            source_db_path=None,
            source_admission_id=None,
        )
        widget.all_archived_patients = [patient]
        widget.filter_data()
        widget.table.selectRow(0)

        widget.on_delete_clicked()
        app.processEvents()
        if not patient_service.enqueue_called:
            return False, "ArchiveWidget delete-all did not use enqueue_write"
        if not widget._delete_pending or widget.table.isEnabled():
            return False, "ArchiveWidget did not enter pending state for delete-all"
        if not patient_service.on_error:
            return False, "ArchiveWidget did not register delete-all error callback"
        patient_service.on_error(RuntimeError("forced archive delete failure"))
        app.processEvents()
        if widget._delete_pending or not widget.table.isEnabled():
            return False, "ArchiveWidget did not restore UI after delete-all error"
        if not load_calls:
            return False, "ArchiveWidget did not refresh after delete-all error"
        if not warnings or "forced archive delete failure" not in warnings[-1]:
            return False, f"ArchiveWidget did not show delete-all error: {warnings}"

        warnings.clear()
        load_calls.clear()
        widget.table.selectRow(0)
        widget.on_delete_last_clicked()
        app.processEvents()
        if not remcard_service.enqueue_called:
            return False, "ArchiveWidget delete-last did not use enqueue_write"
        if not widget._delete_pending or widget.table.isEnabled():
            return False, "ArchiveWidget did not enter pending state for delete-last"
        if not remcard_service.on_error:
            return False, "ArchiveWidget did not register delete-last error callback"
        remcard_service.on_error(RuntimeError("forced archive last-card failure"))
        app.processEvents()
        if widget._delete_pending or not widget.table.isEnabled():
            return False, "ArchiveWidget did not restore UI after delete-last error"
        if not load_calls:
            return False, "ArchiveWidget did not refresh after delete-last error"
        if not warnings or "forced archive last-card failure" not in warnings[-1]:
            return False, f"ArchiveWidget did not show delete-last error: {warnings}"
        return True, "ok"
    finally:
        archive_module.CustomMessageBox.question = original_question
        archive_module.CustomMessageBox.warning = original_warning
        if widget is not None:
            widget.close()


def _check_doctor_create_card_enqueue_error_refreshes(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime, timedelta
    from types import MethodType, SimpleNamespace

    from PySide6.QtWidgets import QApplication

    from rem_card.ui.doctor_view import doctor_remcard_widget as doctor_module
    from rem_card.ui.doctor_view.doctor_remcard_widget import DoctorRemCardWidget

    _ = temp_root
    app = QApplication.instance() or QApplication([])

    class FakeButton:
        def __init__(self):
            self.enabled = True

        def setEnabled(self, enabled):
            self.enabled = bool(enabled)

    class FakeStatusService:
        def __init__(self):
            self.ensure_calls = 0

        def ensure_initial_status(self, admission_id, start, admission_datetime):
            self.ensure_calls += 1

    class FakeService:
        def __init__(self):
            self.status_service = FakeStatusService()
            self.enqueue_called = False
            self.operation = None
            self.on_error = None

        def get_day_period(self, now):
            start = now.replace(hour=8, minute=0, second=0, microsecond=0)
            return start, start + timedelta(days=1)

        def get_patient(self, admission_id):
            return SimpleNamespace(admission_datetime=datetime(2026, 5, 3, 8, 0))

        def enqueue_write(self, description, operation, on_success=None, on_error=None):
            self.enqueue_called = True
            self.description = description
            self.operation = operation
            self.on_error = on_error

        def add_vital(self, dto, shift_date=None, force=False):
            raise AssertionError("queued create-card write should not run in UI thread")

    class FakeLayoutManager:
        def __init__(self):
            self.sector_4v = SimpleNamespace(btn_new_card=FakeButton())
            self.status_refreshes = 0

        def refresh_current_status(self):
            self.status_refreshes += 1

    warnings: list[str] = []
    original_warning = doctor_module.CustomMessageBox.warning
    original_information = doctor_module.CustomMessageBox.information
    doctor_module.CustomMessageBox.warning = lambda parent, title, message: warnings.append(f"{title}: {message}")
    doctor_module.CustomMessageBox.information = lambda *args, **kwargs: None
    try:
        service = FakeService()
        layout_manager = FakeLayoutManager()
        widget = SimpleNamespace(
            _archive_read_only_mode=False,
            _create_card_write_pending=False,
            _snapshot_worker=None,
            _create_card_after_snapshot=False,
            _snapshot_pending=None,
            admission_id=1,
            service=service,
            layout_manager=layout_manager,
            refresh_calls=0,
        )
        widget._begin_create_card_pending = MethodType(DoctorRemCardWidget._begin_create_card_pending, widget)
        widget._finish_create_card_pending = MethodType(DoctorRemCardWidget._finish_create_card_pending, widget)
        widget._set_create_card_controls_enabled = MethodType(
            DoctorRemCardWidget._set_create_card_controls_enabled,
            widget,
        )
        widget.force_reload_all = lambda: setattr(widget, "refresh_calls", widget.refresh_calls + 1)
        widget.update_patient_info = lambda: None
        widget._show_read_only_hint = lambda: None

        DoctorRemCardWidget.on_create_card_clicked(widget)
        app.processEvents()
        if not service.enqueue_called:
            return False, "DoctorRemCardWidget did not use enqueue_write for create-card"
        if not widget._create_card_write_pending:
            return False, "DoctorRemCardWidget did not enter create-card pending state"
        if layout_manager.sector_4v.btn_new_card.enabled:
            return False, "DoctorRemCardWidget did not disable create-card button while pending"
        if service.status_service.ensure_calls:
            return False, "create-card write operation ran before queued worker callback"
        if not service.on_error:
            return False, "DoctorRemCardWidget did not register create-card error callback"

        service.on_error(RuntimeError("forced create-card failure"))
        app.processEvents()
        if widget._create_card_write_pending:
            return False, "DoctorRemCardWidget kept create-card pending state after error"
        if not layout_manager.sector_4v.btn_new_card.enabled:
            return False, "DoctorRemCardWidget did not re-enable create-card button after error"
        if widget.refresh_calls != 1:
            return False, "DoctorRemCardWidget did not refresh after create-card error"
        if not warnings or "forced create-card failure" not in warnings[-1]:
            return False, f"DoctorRemCardWidget did not show create-card error: {warnings}"
        return True, "ok"
    finally:
        doctor_module.CustomMessageBox.warning = original_warning
        doctor_module.CustomMessageBox.information = original_information


def _check_patient_status_error_refreshes_checked_state(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    from PySide6.QtWidgets import QApplication

    from rem_card.data.dto.remcard_dto import PatientStatus, PatientStatusEventDTO
    from rem_card.ui.rem_card_sectors import sector_events as events_module
    from rem_card.ui.rem_card_sectors.sector_events import SectorEvents

    _ = temp_root
    app = QApplication.instance() or QApplication([])

    class FakeStatusService:
        def __init__(self):
            self.enqueue_called = False
            self.on_error = None
            self.reads = 0

        def get_events(self, admission_id):
            self.reads += 1
            return [
                PatientStatusEventDTO(
                    id=1,
                    admission_id=admission_id,
                    status=PatientStatus.ACTIVE,
                    start_time=datetime(2026, 5, 3, 8, 0),
                    created_by="USER",
                )
            ]

        def get_latest_change_id(self, admission_id=None, include_global=True):
            return self.reads

        def enqueue_change_status(
            self,
            admission_id,
            new_status,
            reason_type=None,
            reason_text=None,
            user_id=None,
            on_success=None,
            on_error=None,
        ):
            self.enqueue_called = True
            self.on_error = on_error

    warnings: list[str] = []
    original_warning = events_module.CustomMessageBox.warning
    events_module.CustomMessageBox.warning = lambda parent, title, message: warnings.append(f"{title}: {message}")
    widget = SectorEvents()
    service = FakeStatusService()
    try:
        widget.set_patient(1, service)
        app.processEvents()
        if not widget.btn_active.isChecked() or widget.btn_out.isChecked():
            return False, "initial status buttons did not reflect active DB status"

        widget.btn_out.setChecked(True)
        widget.on_status_btn_clicked(PatientStatus.OUT)
        app.processEvents()
        if not service.enqueue_called or not service.on_error:
            return False, "SectorEvents did not enqueue status change"
        if widget.btn_out.isChecked() or not widget.btn_active.isChecked():
            return False, "SectorEvents showed final status before commit"
        if widget.content_area.isEnabled():
            return False, "SectorEvents did not enter pending disabled state"

        service.on_error(RuntimeError("forced status failure"))
        app.processEvents()
        if not widget.content_area.isEnabled():
            return False, "SectorEvents did not re-enable after status write error"
        if not widget.btn_active.isChecked() or widget.btn_out.isChecked():
            return False, "SectorEvents did not refresh/rollback checked state after error"
        if service.reads < 2:
            return False, "SectorEvents did not refresh from DB/service after status write error"
        if not warnings or "forced status failure" not in warnings[-1]:
            return False, f"SectorEvents did not show status write error: {warnings}"
        return True, "ok"
    finally:
        events_module.CustomMessageBox.warning = original_warning
        widget.close()


def _assert_stale_snapshot_preserves_cell_delete_draft(doctor_widget, doctor_model, index, shift, doctor_order):
    from PySide6.QtCore import Qt

    stale_snapshot = {
        "admission_id": 1,
        "shift_date": shift,
        "only_committed": False,
        "orders": [doctor_order],
        "admin_rows": [
            {
                "id": 10,
                "order_id": 1,
                "big_chain_id": None,
                "cell_role": "single",
                "planned_time": doctor_model.time_slots[0].isoformat(),
                "actual_time": None,
                "performer_id": None,
                "status": "planned",
                "is_committed": 1,
                "comment": "",
                "volume_ml": 0.0,
                "updated_at": "2026-05-03 08:00:00.000",
                "last_modified_by": "doctor",
            }
        ],
        "has_any_draft": False,
        "has_any_administrations": True,
        "has_any_orders": True,
        "change_id": 1,
        "source": "refresh",
        "load_trace_id": "regression-stale-no-draft",
    }
    if not doctor_widget._apply_snapshot_data(snapshot=stale_snapshot, admission_id=1, shift_date=shift):
        return False, "doctor committed cell delete stale snapshot was rejected instead of guarded"
    guarded_admin = doctor_model.data(index, Qt.UserRole)
    if guarded_admin is None or guarded_admin.status != "deleted" or not doctor_widget.has_drafts():
        return False, "stale no-draft snapshot cleared committed cell delete draft state"
    return True, "ok"


def _assert_committed_long_infusion_delete_marks_draft(doctor_widget, doctor_model, long_index, long_order):
    from rem_card.data.dto.remcard_dto import AdministrationDTO

    long_chain_id = "long-committed-chain"
    committed_chain = []
    for offset, role in enumerate(("start", "body", "end")):
        slot = doctor_model.time_slots[offset]
        admin_row = AdministrationDTO(
            id=30 + offset,
            order_id=3,
            planned_time=slot,
            status="planned",
            cell_role=role,
            big_chain_id=long_chain_id,
            is_committed=1,
            comment="",
        )
        doctor_model.admin_map[(3, slot.isoformat())] = admin_row
        committed_chain.append(admin_row)

    doctor_model.has_any_draft = False
    doctor_widget._cached_has_drafts = False
    committed_long_delete = doctor_widget._apply_optimistic_cell(
        long_index,
        long_order,
        committed_chain[0],
        doctor_model.time_slots[0],
        "orders_left_click",
    )
    deleted_roles = [
        getattr(doctor_model.admin_map.get((3, doctor_model.time_slots[offset].isoformat())), "status", None)
        for offset in range(3)
    ]
    if deleted_roles != ["deleted", "deleted", "deleted"]:
        return False, f"committed long infusion delete did not tombstone all cells: {deleted_roles}"
    if not doctor_widget.has_drafts():
        return False, "committed long infusion delete did not activate save draft state"

    doctor_widget._restore_admin_cells(committed_long_delete)
    if any(
        getattr(doctor_model.admin_map.get((3, doctor_model.time_slots[offset].isoformat())), "status", None) != "planned"
        for offset in range(3)
    ):
        return False, "committed long infusion tombstones were not restored on error"
    return True, "ok"


def _check_orders_pending_states_before_commit(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime, timedelta

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from rem_card.data.dto.remcard_dto import AdministrationDTO, OrderDTO
    from rem_card.ui.doctor_view.orders_widget import OrdersWidget
    from rem_card.ui.nurse_view.components.nurse_orders_widget import NurseOrdersWidget
    from rem_card.ui.shared.orders_model import OrdersModel
    from rem_card.services.order_domain_service import NURSE_MARK_EXECUTED

    _ = temp_root
    app = QApplication.instance() or QApplication([])

    class FakeOrdersService:
        def get_day_period(self, shift_date):
            start = shift_date.replace(hour=8, minute=0, second=0, microsecond=0)
            return start, start + timedelta(days=1)

    shift = datetime(2026, 5, 3, 8, 0)
    service = FakeOrdersService()
    doctor_widget = OrdersWidget(service=service, admission_id=1, shift_date=shift, defer_ui=True)
    nurse_widget = NurseOrdersWidget(service=service, admission_id=1, shift_date=shift, defer_ui=True)
    try:
        doctor_model = OrdersModel(service, admission_id=1, shift_date=shift)
        doctor_order = OrderDTO(id=1, admission_id=1, latin="Test", is_committed=1)
        doctor_model.orders = [doctor_order]
        doctor_widget.model = doctor_model
        doctor_widget._mark_local_order_row_deleted(0, doctor_order, was_committed=True)
        app.processEvents()
        if len(doctor_model.orders) != 1:
            return False, "doctor order row was removed before delete commit"
        if not getattr(doctor_model.orders[0], "_pending_delete", False):
            return False, "doctor order row was not marked as pending-delete"
        doctor_widget._clear_local_order_row_pending_delete(1)
        if getattr(doctor_model.orders[0], "_pending_delete", False):
            return False, "doctor order row pending-delete marker was not cleared on error"

        index = doctor_model.index(0, 1)
        pending = doctor_widget._apply_pending_cell(index, doctor_order, None, doctor_model.time_slots[0], "orders_left_click")
        admin = doctor_model.data(index, Qt.UserRole)
        if not pending:
            return False, "doctor order cell did not capture previous state"
        if admin is None or not getattr(admin, "_pending_cell_action", None):
            return False, "doctor order cell did not show pending state before commit"
        doctor_widget._restore_admin_cells(pending)
        if doctor_model.data(index, Qt.UserRole) is not None:
            return False, "doctor order cell pending state was not restored on error"

        optimistic = doctor_widget._apply_optimistic_cell(
            index,
            doctor_order,
            None,
            doctor_model.time_slots[0],
            "orders_left_click",
        )
        admin = doctor_model.data(index, Qt.UserRole)
        if not optimistic:
            return False, "doctor order cell did not capture optimistic previous state"
        if admin is None or admin.status != "planned" or admin.cell_role != "single":
            return False, "doctor order cell did not show final mark immediately"
        if not getattr(admin, "_pending_cell_action", None):
            return False, "doctor order optimistic mark did not keep pending marker"
        doctor_widget._restore_admin_cells(optimistic)
        if doctor_model.data(index, Qt.UserRole) is not None:
            return False, "doctor order optimistic state was not restored on error"

        committed_admin = AdministrationDTO(
            id=10,
            order_id=1,
            planned_time=doctor_model.time_slots[0],
            status="planned",
            cell_role="single",
            is_committed=1,
            comment="",
        )
        doctor_model.admin_map[(1, doctor_model.time_slots[0].isoformat())] = committed_admin
        doctor_model.has_any_draft = False
        doctor_widget._cached_has_drafts = False
        committed_delete = doctor_widget._apply_optimistic_cell(
            index,
            doctor_order,
            committed_admin,
            doctor_model.time_slots[0],
            "orders_left_click",
        )
        deleted_admin = doctor_model.data(index, Qt.UserRole)
        if not committed_delete:
            return False, "doctor committed cell delete did not capture previous state"
        if deleted_admin is None or deleted_admin.status != "deleted" or int(deleted_admin.is_committed or 0) != 0:
            return False, f"doctor committed cell delete did not create draft tombstone: {deleted_admin}"
        if not doctor_widget.has_drafts():
            return False, "doctor committed cell delete did not activate save draft state"
        ok, details = _assert_stale_snapshot_preserves_cell_delete_draft(
            doctor_widget,
            doctor_model,
            index,
            shift,
            doctor_order,
        )
        if not ok:
            return False, details
        doctor_widget._restore_admin_cells(committed_delete)
        if doctor_model.data(index, Qt.UserRole) != committed_admin:
            return False, "doctor committed cell tombstone was not restored on error"

        long_order = OrderDTO(id=3, admission_id=1, latin="Long", is_committed=1, duration_min=180)
        doctor_model.orders.append(long_order)
        long_index = doctor_model.index(1, 1)
        long_previous = doctor_widget._apply_optimistic_cell(
            long_index,
            long_order,
            None,
            doctor_model.time_slots[0],
            "orders_left_click",
        )
        expected_roles = ["start", "body", "end"]
        actual_roles = [
            getattr(doctor_model.admin_map.get((3, doctor_model.time_slots[offset].isoformat())), "cell_role", None)
            for offset in range(3)
        ]
        if actual_roles != expected_roles:
            return False, f"long infusion optimistic roles mismatch: {actual_roles}"
        if not all(
            getattr(doctor_model.admin_map[(3, doctor_model.time_slots[offset].isoformat())], "_pending_cell_action", None)
            for offset in range(3)
        ):
            return False, "long infusion optimistic cells did not keep pending markers"
        doctor_widget._restore_admin_cells(long_previous)
        if any(key[0] == 3 for key in doctor_model.admin_map):
            return False, "long infusion optimistic state was not restored on error"
        ok, details = _assert_committed_long_infusion_delete_marks_draft(
            doctor_widget,
            doctor_model,
            long_index,
            long_order,
        )
        if not ok:
            return False, details
        doctor_model.orders.pop()

        nurse_model = OrdersModel(service, admission_id=1, shift_date=shift)
        nurse_order = OrderDTO(id=2, admission_id=1, latin="Nurse")
        nurse_model.orders = [nurse_order]
        nurse_slot = nurse_model.time_slots[0]
        nurse_admin = AdministrationDTO(
            id=20,
            order_id=2,
            planned_time=nurse_slot,
            status="planned",
            cell_role="single",
            comment="",
        )
        nurse_model.admin_map[(2, nurse_slot.isoformat())] = nurse_admin
        nurse_widget.model = nurse_model
        nurse_index = nurse_model.index(0, 1)

        nurse_widget._apply_pending_nurse_mark(nurse_index, nurse_admin, NURSE_MARK_EXECUTED)
        pending_admin = nurse_model.data(nurse_index, Qt.UserRole)
        if getattr(pending_admin, "comment", ""):
            return False, "nurse mark became final before commit"
        if not hasattr(pending_admin, "_pending_mark"):
            return False, "nurse mark did not enter pending state"

        nurse_widget._apply_committed_nurse_mark(nurse_index, nurse_admin, NURSE_MARK_EXECUTED)
        committed_admin = nurse_model.data(nurse_index, Qt.UserRole)
        if getattr(committed_admin, "comment", "") != NURSE_MARK_EXECUTED:
            return False, "nurse mark did not become final after success"
        if hasattr(committed_admin, "_pending_mark"):
            return False, "nurse pending marker remained after success"
        return True, "ok"
    finally:
        doctor_widget.close()
        nurse_widget.close()


def _check_local_replica_tmp_cleanup(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.local_replica_sync import LocalReplicaSync

    central_path = os.path.join(temp_root, "central_replica_source.db")
    local_path = os.path.join(temp_root, "local_replica_target.db")
    _create_sqlite_file(central_path)

    replica = LocalReplicaSync(
        central_db_path=central_path,
        local_db_path=local_path,
        sync_interval_sec=60.0,
    )
    ok = replica.sync_once()
    replica.stop()

    leftovers = glob.glob(f"{local_path}.sync_tmp.*")
    if leftovers:
        return False, f"temporary replica files were not cleaned: {leftovers[:3]}"
    if not ok:
        return False, "sync_once returned False"
    return True, "ok"


def _check_backup_cleanup_gating(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.backup_and_cleanup import _can_cleanup_old_backups

    db_path = os.path.join(temp_root, "cleanup_gate_primary.db")
    backup_dir = os.path.join(temp_root, "cleanup_gate_backups")
    os.makedirs(backup_dir, exist_ok=True)
    _create_sqlite_file(db_path)

    # No backups -> cleanup must be blocked.
    if _can_cleanup_old_backups(db_path, backup_dir):
        return False, "cleanup gate passed unexpectedly without healthy backups"

    healthy_backup = os.path.join(backup_dir, "healthy_backup.db")
    shutil.copy2(db_path, healthy_backup)
    if not _can_cleanup_old_backups(db_path, backup_dir):
        return False, "cleanup gate failed despite healthy backup"

    # Corrupt backup only -> cleanup must be blocked.
    os.remove(healthy_backup)
    corrupt_backup = os.path.join(backup_dir, "corrupt_backup.db")
    with open(corrupt_backup, "wb") as fh:
        fh.write(b"not_sqlite")
    if _can_cleanup_old_backups(db_path, backup_dir):
        return False, "cleanup gate passed unexpectedly with only corrupt backup"

    return True, "ok"


def _check_backup_count_limit_enforcement(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.backup_and_cleanup import BACKUP_MAX_COUNT, _enforce_backup_limits

    backup_dir = os.path.join(temp_root, "count_limit_backups")
    os.makedirs(backup_dir, exist_ok=True)

    files_to_create = int(BACKUP_MAX_COUNT) + 9
    now = time.time()
    for idx in range(files_to_create):
        path = os.path.join(backup_dir, f"backup_{idx:03d}.db")
        with open(path, "wb") as fh:
            fh.write(b"sqlite-mock")
        # Чем меньше idx, тем старше файл.
        file_age_sec = float(files_to_create - idx) * 10.0
        ts = now - file_age_sec
        os.utime(path, (ts, ts))

    _enforce_backup_limits(backup_dir)

    remaining = [
        os.path.join(backup_dir, name)
        for name in os.listdir(backup_dir)
        if name.lower().endswith(".db")
    ]
    if len(remaining) > int(BACKUP_MAX_COUNT):
        return False, f"backup count cap not enforced: {len(remaining)} > {BACKUP_MAX_COUNT}"

    newest_name = f"backup_{files_to_create - 1:03d}.db"
    if not os.path.exists(os.path.join(backup_dir, newest_name)):
        return False, f"newest backup was removed unexpectedly: {newest_name}"

    oldest_name = "backup_000.db"
    if os.path.exists(os.path.join(backup_dir, oldest_name)):
        return False, f"oldest backup was not removed: {oldest_name}"

    return True, "ok"


def _check_runtime_backup_rotation_scans_valid_dir(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.paths import BACKUPS_RC_DIR, BACKUPS_VALID_DIR
    from rem_card.data.dao import db_manager as rem_db_manager

    valid_root = os.path.normcase(os.path.abspath(BACKUPS_VALID_DIR))
    isolated_baza_dir = os.environ.get("REMCARD_BAZA_DIR") or temp_root
    isolated_root = os.path.normcase(os.path.abspath(isolated_baza_dir))
    if not valid_root.startswith(isolated_root):
        return False, f"backup test path is not isolated: {BACKUPS_VALID_DIR}"

    def prepare_files(prefix: str, count: int):
        shutil.rmtree(BACKUPS_RC_DIR, ignore_errors=True)
        os.makedirs(BACKUPS_VALID_DIR, exist_ok=True)
        now = time.time()
        for idx in range(count):
            path = os.path.join(BACKUPS_VALID_DIR, f"{prefix}_{idx:03d}.db")
            with open(path, "wb") as fh:
                fh.write(b"sqlite-mock")
            with open(f"{path}.meta.json", "w", encoding="utf-8") as fh:
                json.dump({"idx": idx}, fh)
            ts = now - float(count - idx)
            os.utime(path, (ts, ts))
            os.utime(f"{path}.meta.json", (ts, ts))

    rem_limit = int(rem_db_manager.MAX_RUNTIME_BACKUPS)
    prepare_files("shutdown_remcard_regression", rem_limit + 2)
    rem_instance = rem_db_manager.DatabaseManager.__new__(rem_db_manager.DatabaseManager)
    rem_db_manager.DatabaseManager._rotate_backups(rem_instance)
    rem_remaining = sorted(
        name for name in os.listdir(BACKUPS_VALID_DIR) if name.endswith(".db")
    )
    if len(rem_remaining) > rem_limit:
        return False, f"remcard runtime backup cap not enforced in valid dir: {len(rem_remaining)} > {rem_limit}"
    if os.path.exists(os.path.join(BACKUPS_VALID_DIR, "shutdown_remcard_regression_000.db")):
        return False, "oldest remcard runtime backup was not removed from valid dir"
    if os.path.exists(os.path.join(BACKUPS_VALID_DIR, "shutdown_remcard_regression_000.db.meta.json")):
        return False, "oldest remcard runtime backup metadata was not removed"
    if not os.path.exists(os.path.join(BACKUPS_VALID_DIR, f"shutdown_remcard_regression_{rem_limit + 1:03d}.db")):
        return False, "newest remcard runtime backup was removed unexpectedly"

    return True, "ok"


def _check_balance_admission_hour_visibility(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.data.dao.fluids_dao import FluidsDAO
    from rem_card.data.dao.patient_dao import PatientDAO
    from rem_card.services.fluid_service import FluidService
    from rem_card.services.vital_service import VitalService

    db_path = os.path.join(temp_root, "balance_admission_hour.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        admission_dt = datetime(2026, 4, 23, 11, 1, 41, 123456)
        with manager.remcard_transaction(source="regression_seed_balance_hour") as cursor:
            cursor.execute(
                """
                INSERT INTO patients (full_name, last_name, first_name, middle_name)
                VALUES (?, ?, ?, ?)
                """,
                ("Иванов Иван", "Иванов", "Иван", None),
            )
            patient_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO admissions (
                    patient_id,
                    bed_number,
                    history_number,
                    admission_datetime,
                    is_active
                )
                VALUES (?, ?, ?, ?, 1)
                """,
                (patient_id, 1, "REG-FLUID-001", admission_dt.isoformat()),
            )
            admission_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT OR REPLACE INTO beds (bed_number, status, current_admission_id)
                VALUES (?, 'OCCUPIED', ?)
                """,
                (1, admission_id),
            )

        patient_dao = PatientDAO(manager)
        fluids_dao = FluidsDAO(manager)
        vital_service = VitalService(vitals_dao=None, patient_dao=patient_dao, status_service=None)
        fluid_service = FluidService(fluids_dao, vital_service)

        fluid_service.upsert_hourly_output(
            admission_id=admission_id,
            shift_date=admission_dt,
            hour=admission_dt.hour,
            row_key="urine",
            value=250,
            is_sum=False,
        )

        fluids = fluid_service.get_fluids(admission_id, admission_dt)
        if len(fluids) != 1:
            return False, f"expected exactly 1 visible fluid row, got {len(fluids)}"

        fluid = fluids[0]
        if int(fluid.urine or 0) != 250:
            return False, f"unexpected urine value: {fluid.urine}"
        if fluid.timestamp != admission_dt:
            return False, f"admission-hour timestamp drifted: expected {admission_dt.isoformat()}, got {fluid.timestamp.isoformat()}"

        return True, "ok"
    finally:
        manager.close()


def _check_print_hourly_input_planned_time(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime, timedelta

    from rem_card.data.dto.remcard_dto import AdministrationDTO, OrderDTO, OrderStatus, OrderType
    from rem_card.services.balance_calculator import BalanceCalculator
    from rem_card.services.report_balance import build_print_balance_final

    start = datetime(2026, 4, 24, 8, 0, 0)
    end = start + timedelta(hours=24)

    def executed_admin(order_id: int, planned_hour: int, actual_hour: int, actual_minute: int = 0, *, role: str = "single", chain_id: str | None = None):
        return AdministrationDTO(
            id=order_id * 100 + planned_hour,
            order_id=order_id,
            big_chain_id=chain_id,
            cell_role=role,
            planned_time=start + timedelta(hours=planned_hour),
            actual_time=start + timedelta(hours=actual_hour, minutes=actual_minute),
            status="planned",
            is_committed=1,
            comment="nurse_executed",
        )

    mixed_input = OrderDTO(
        id=1,
        admission_id=1,
        drug_key="ruchnoivvod",
        latin="Manual infusion",
        type=OrderType.MEDICATION,
        status=OrderStatus.ACTIVE,
        dose_value=20,
        dose_unit="ml",
        duration_min=0,
        is_committed=1,
        comment="S. NaCl - 400 ml",
        administrations=[executed_admin(1, planned_hour=11, actual_hour=15, actual_minute=0)],
    )
    mixed_hourly = BalanceCalculator.calculate_hourly_actual_input([mixed_input], start, end, end)
    if mixed_hourly[11]["infusion"] != 400.0 or mixed_hourly[11]["preparats"] != 20.0:
        return False, f"mixed input did not land in planned hour: {mixed_hourly[11]}"
    if mixed_hourly[15]["infusion"] != 0.0 or mixed_hourly[15]["preparats"] != 0.0:
        return False, f"mixed input incorrectly used actual mark hour: {mixed_hourly[15]}"

    future_21 = OrderDTO(
        id=7,
        admission_id=1,
        drug_key="furosemide",
        latin="Furosemidi",
        type=OrderType.MEDICATION,
        status=OrderStatus.ACTIVE,
        dose_value=21,
        dose_unit="ml",
        duration_min=0,
        is_committed=1,
        comment="",
        administrations=[executed_admin(7, planned_hour=13, actual_hour=12, actual_minute=0)],
    )
    future_22 = OrderDTO(
        id=8,
        admission_id=1,
        drug_key="furosemide",
        latin="Furosemidi",
        type=OrderType.MEDICATION,
        status=OrderStatus.ACTIVE,
        dose_value=22,
        dose_unit="ml",
        duration_min=0,
        is_committed=1,
        comment="",
        administrations=[executed_admin(8, planned_hour=14, actual_hour=12, actual_minute=0)],
    )
    unmarked_future_21 = OrderDTO(
        id=9,
        admission_id=1,
        drug_key="furosemide",
        latin="Furosemidi",
        type=OrderType.MEDICATION,
        status=OrderStatus.ACTIVE,
        dose_value=31,
        dose_unit="ml",
        duration_min=0,
        is_committed=1,
        comment="",
        administrations=[
            AdministrationDTO(
                id=913,
                order_id=9,
                cell_role="single",
                planned_time=start + timedelta(hours=13),
                status="planned",
                is_committed=1,
                comment="",
            )
        ],
    )
    print_balance = build_print_balance_final(
        orders=[future_21, future_22, unmarked_future_21],
        fluids=[],
        remcard_service=object(),
        config={"balance": True},
        admission_id=1,
        start_dt=start,
        current_time=start + timedelta(hours=12),
        end_dt=end,
    )
    if print_balance["in_hourly"][13]["preparats"] != 21.0:
        return False, "print input did not include exactly the one-hour future executed appointment"
    if print_balance["in_hourly"][14]["preparats"] != 0.0:
        return False, "print input included appointment more than one hour in the future"

    timed_infusion = OrderDTO(
        id=5,
        admission_id=1,
        drug_key="ceftriaxone",
        latin="Ceftriaxoni",
        type=OrderType.MEDICATION,
        status=OrderStatus.ACTIVE,
        dose_value=1,
        dose_unit="g",
        duration_min=120,
        is_committed=1,
        comment="S. NaCl - 240 ml",
        administrations=[executed_admin(5, planned_hour=1, actual_hour=2, actual_minute=30)],
    )
    timed_hourly = BalanceCalculator.calculate_hourly_actual_input([timed_infusion], start, start + timedelta(hours=4), end)
    if (timed_hourly[1]["infusion"], timed_hourly[2]["infusion"], timed_hourly[3]["infusion"]) != (120.0, 120.0, 0.0):
        return False, f"timed infusion used actual mark time instead of planned time: {[timed_hourly[i]['infusion'] for i in (1, 2, 3)]}"

    preparat = OrderDTO(
        id=2,
        admission_id=1,
        drug_key="furosemide",
        latin="Furosemidi",
        type=OrderType.MEDICATION,
        status=OrderStatus.ACTIVE,
        dose_value=20,
        dose_unit="ml",
        duration_min=0,
        is_committed=1,
        comment="",
        administrations=[executed_admin(2, planned_hour=2, actual_hour=3, actual_minute=5)],
    )
    preparat_hourly = BalanceCalculator.calculate_hourly_actual_input([preparat], start, start + timedelta(hours=5), end)
    if preparat_hourly[2]["preparats"] != 20.0 or preparat_hourly[3]["preparats"] != 0.0:
        return False, f"bolus preparat used actual hour instead of planned hour: {[preparat_hourly[i]['preparats'] for i in (2, 3)]}"

    not_done = OrderDTO(
        id=6,
        admission_id=1,
        drug_key="furosemide",
        latin="Furosemidi",
        type=OrderType.MEDICATION,
        status=OrderStatus.ACTIVE,
        dose_value=30,
        dose_unit="ml",
        duration_min=0,
        is_committed=1,
        comment="",
        administrations=[
            AdministrationDTO(
                id=606,
                order_id=6,
                cell_role="single",
                planned_time=start + timedelta(hours=6),
                actual_time=start + timedelta(hours=7),
                status="planned",
                is_committed=1,
                comment="nurse_not_executed",
            )
        ],
    )
    not_done_hourly = BalanceCalculator.calculate_hourly_actual_input([not_done], start, end, end)
    if not_done_hourly[6]["preparats"] != 0.0:
        return False, "not executed preparat was included in print hourly input"

    late_documented = OrderDTO(
        id=4,
        admission_id=1,
        drug_key="furosemide",
        latin="Furosemidi",
        type=OrderType.MEDICATION,
        status=OrderStatus.ACTIVE,
        dose_value=10,
        dose_unit="ml",
        duration_min=0,
        is_committed=1,
        comment="",
        administrations=[
            AdministrationDTO(
                id=404,
                order_id=4,
                cell_role="single",
                planned_time=start + timedelta(hours=4),
                actual_time=end + timedelta(hours=1),
                status="planned",
                is_committed=1,
                comment="nurse_executed",
            )
        ],
    )
    late_hourly = BalanceCalculator.calculate_hourly_actual_input([late_documented], start, end, end)
    if late_hourly[4]["preparats"] != 10.0:
        return False, "past card late-documented preparat was not kept in its planned hour"

    chain = OrderDTO(
        id=3,
        admission_id=1,
        drug_key="ceftriaxone",
        latin="Ceftriaxoni",
        type=OrderType.MEDICATION,
        status=OrderStatus.ACTIVE,
        dose_value=1,
        dose_unit="g",
        duration_min=120,
        is_committed=1,
        comment="S. NaCl - 240 ml",
        administrations=[
            executed_admin(3, planned_hour=1, actual_hour=1, actual_minute=30, role="start", chain_id="chain-1"),
            AdministrationDTO(
                id=302,
                order_id=3,
                big_chain_id="chain-1",
                cell_role="end",
                planned_time=start + timedelta(hours=2),
                status="planned",
                is_committed=1,
                comment="",
            ),
        ],
    )
    chain_hourly = BalanceCalculator.calculate_hourly_actual_input([chain], start, start + timedelta(hours=4), end)
    if (chain_hourly[1]["infusion"], chain_hourly[2]["infusion"], chain_hourly[3]["infusion"]) != (120.0, 120.0, 0.0):
        return False, f"chain infusion used actual start instead of planned start: {[chain_hourly[i]['infusion'] for i in (1, 2, 3)]}"

    return True, "ok"


def _check_sector_print_transform_snapshot(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    from rem_card.data.dto.remcard_dto import (
        FluidDTO,
        OrderDTO,
        OrderStatus,
        OrderType,
        PatientStatus,
        PatientStatusEventDTO,
        VentilationEventDTO,
        VentilationEventType,
        VentilationMode,
        VitalDTO,
    )
    from rem_card.ui.rem_card_sectors import sector_print
    from rem_card.ui.rem_card_sectors.sector_print import DataCollectorWorker

    real_datetime = datetime
    fixed_now = real_datetime(2026, 4, 24, 14, 30)

    class FixedDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return fixed_now.replace(tzinfo=tz)
            return fixed_now

    class FakeStatusService:
        def get_admission_outcome_context(self, admission_id):
            return {
                "outcome": "dead",
                "death_datetime": "2026-04-24T13:10:00",
                "clinical_death_datetime": "2026-04-24T13:00:00",
                "cardiac_arrest_cause": "Асистолия",
                "cardiac_arrest_measures_json": json.dumps(
                    {
                        "comment": "Реанимационные мероприятия без эффекта",
                        "measures": [{"name": "СЛР", "value": "30 мин"}],
                    },
                    ensure_ascii=False,
                ),
            }

    class FakeService:
        status_service = FakeStatusService()

        def get_vital_settings_cached(self, admission_id, start_dt):
            return {"ad": 1, "pulse": 1, "temp": 1, "spo2": 1, "rr": 1, "cvp": 1}

        def get_latest_administrations_for_order_ids(self, **kwargs):
            start = kwargs["start_dt"]
            return [
                {
                    "id": 101,
                    "order_id": 1,
                    "chain_id": "c1",
                    "big_chain_id": None,
                    "cell_role": "single",
                    "planned_time": (start + timedelta(hours=0)).isoformat(sep=" "),
                    "actual_time": (start + timedelta(minutes=5)).isoformat(sep=" "),
                    "status": "planned",
                    "volume_ml": 100.0,
                    "comment": "nurse_executed",
                },
                {
                    "id": 201,
                    "order_id": 2,
                    "chain_id": "c2",
                    "big_chain_id": "bc2",
                    "cell_role": "start",
                    "planned_time": start + timedelta(hours=2),
                    "actual_time": None,
                    "status": "planned",
                    "volume_ml": 0.0,
                    "comment": "",
                },
                {
                    "id": 202,
                    "order_id": 2,
                    "chain_id": "c2",
                    "big_chain_id": "bc2",
                    "cell_role": "end",
                    "planned_time": start + timedelta(hours=3),
                    "actual_time": None,
                    "status": "planned",
                    "volume_ml": 0.0,
                    "comment": "nurse_not_executed",
                },
                {
                    "id": 401,
                    "order_id": 4,
                    "chain_id": None,
                    "big_chain_id": None,
                    "cell_role": "single",
                    "planned_time": start + timedelta(hours=4),
                    "actual_time": None,
                    "status": "planned",
                    "volume_ml": 0.0,
                    "comment": "",
                },
            ]

        def get_oral_intake_totals(self, admission_id, start_dt, current_time=None):
            return {"current": 150, "daily": 300}

        def get_oral_intake_events(self, admission_id, start_dt):
            return [SimpleNamespace(event_time=start_dt + timedelta(hours=1), amount_ml=50)]

    start = real_datetime(2026, 4, 24, 8, 0)
    end = start + timedelta(hours=24)
    data = {
        "admission_id": 7,
        "patient_name": "Тест Пациент",
        "diagnosis": "Тестовый диагноз",
        "icu_day": "2",
        "start_dt": start,
        "end_dt": end,
        "vitals": [
            VitalDTO(id=1, admission_id=7, timestamp=start + timedelta(minutes=20), sys=120, dia=70, pulse=80, temp=36.6, spo2=98, rr=16, cvp=-1),
            VitalDTO(id=2, admission_id=7, timestamp=start + timedelta(hours=1, minutes=20), sys=125, dia=75, pulse=82, temp=None, spo2=97, rr=18, cvp=4),
        ],
        "prescriptions": [
            OrderDTO(id=1, admission_id=7, drug_key="ceftriaxone", latin="Ceftriaxoni", type=OrderType.MEDICATION, status=OrderStatus.ACTIVE, dose_value=1, dose_unit="g", duration_min=60, is_committed=1, created_at=start, comment="S. NaCl 0,9% 100 мл [DUR:60]"),
            OrderDTO(id=2, admission_id=7, drug_key="mix", latin="DrugA + DrugB", type=OrderType.INFUSION_CONTINUOUS, status=OrderStatus.ACTIVE, dose_value=2.5, dose_unit="mg", is_per_kg=True, duration_min=120, is_committed=1, created_at=start, comment="[DIL:S. Glucose 5% 200 мл] [ROUTE:инфузия] [DUR:120]"),
            OrderDTO(id=3, admission_id=7, drug_key="old", latin="Deleted", type=OrderType.MEDICATION, status=OrderStatus.DELETED, dose_value=1, dose_unit="mg", is_committed=1, created_at=start, comment=""),
            OrderDTO(id=4, admission_id=7, drug_key="draft", latin="Draft cancelled", type=OrderType.MEDICATION, status=OrderStatus.CANCELLED, dose_value=5, dose_unit="ml", is_committed=0, created_at=start, comment=""),
        ],
        "events": [
            PatientStatusEventDTO(id=1, admission_id=7, status=PatientStatus.OR, reason_text="Операция", start_time=start + timedelta(hours=2), end_time=start + timedelta(hours=3)),
            PatientStatusEventDTO(id=2, admission_id=7, status=PatientStatus.DEAD, reason_text="Биологическая смерть: подтверждена", start_time=start + timedelta(hours=5, minutes=10), end_time=None),
            PatientStatusEventDTO(id=3, admission_id=7, status=PatientStatus.OUT, reason_text=None, start_time=start + timedelta(hours=15, minutes=50), end_time=start + timedelta(hours=16, minutes=10)),
        ],
        "fluids_raw": [
            FluidDTO(id=1, admission_id=7, timestamp=start + timedelta(hours=1), urine=200, drain_output=15),
        ],
        "ventilation_events": [
            VentilationEventDTO(id=1, admission_id=7, timestamp=start + timedelta(hours=2), event_type=VentilationEventType.MODE_CHANGE, mode=VentilationMode.PSV, parameters={"PEEP": 5, "FiO2": 40}, o2_flow=3),
        ],
    }

    old_datetime = sector_print.datetime.datetime
    sector_print.datetime.datetime = FixedDateTime
    try:
        result = DataCollectorWorker.transform_data_static(data, FakeService(), {"balance": True, "death_outcome": True})
    finally:
        sector_print.datetime.datetime = old_datetime

    expected_keys = [
        "admission_id",
        "patient_name",
        "diagnosis",
        "icu_day",
        "start_dt",
        "end_dt",
        "vitals",
        "prescriptions",
        "events",
        "fluids_raw",
        "ventilation_events",
        "vitals_matrix",
        "vital_settings",
        "prescriptions_matrix",
        "balance_final",
        "events_struct",
        "death_outcome",
        "ventilation_struct",
    ]
    if list(result.keys()) != expected_keys:
        return False, f"unexpected print data key order: {list(result.keys())}"

    if result["vitals_matrix"].get(0, {}).get("hr") != 80 or result["vitals_matrix"].get(1, {}).get("sys") != 125:
        return False, f"unexpected vitals matrix: {result['vitals_matrix']}"

    prescriptions = result["prescriptions_matrix"]
    if len(prescriptions) != 3:
        return False, f"expected 3 prescription rows, got {len(prescriptions)}"
    expected_names = [
        ["Ceftriaxoni 1 g", "S. NaCl 0,9% 100 мл"],
        ["DrugA", "DrugB 2.5 mg/кг", "S. Glucose 5% 200 мл"],
        ["Draft cancelled 5 мл"],
    ]
    actual_names = [row["name"] for row in prescriptions]
    if actual_names != expected_names:
        return False, f"unexpected prescription names: {actual_names}"
    if prescriptions[0]["marks"][0]["nurse_mark"] != "nurse_executed":
        return False, "single administration mark was not preserved"
    if prescriptions[1]["marks"][2]["role"] != "start" or prescriptions[1]["marks"][3]["role"] != "end":
        return False, "chain administration roles were not preserved"
    if prescriptions[1]["marks"][3]["nurse_mark"] != "nurse_not_executed":
        return False, "not-executed chain mark was not preserved"

    expected_events = [
        {"time": "24.04.2026 10:00 - 11:00", "status": "Оперблок", "desc": "Операция"},
        {"time": "24.04.2026 13:10", "status": "Умер", "desc": "—"},
        {"time": "24.04 23:50 - 25.04 00:10", "status": "Вне отд.", "desc": "—"},
    ]
    if result["events_struct"] != expected_events:
        return False, f"unexpected events struct: {result['events_struct']}"

    death = result["death_outcome"]
    if death.get("clinical_time") != "24.04.2026 13:00" or death.get("biological_time") != "24.04.2026 13:10":
        return False, f"unexpected death outcome times: {death}"
    if death.get("cause") != "Асистолия" or death.get("measures") != [{"name": "СЛР", "value": "30 мин"}]:
        return False, f"unexpected death outcome details: {death}"

    ventilation = result["ventilation_struct"]
    if len(ventilation) != 1 or ventilation[0]["event"] != "Смена режима" or ventilation[0]["mode"] != "PSV":
        return False, f"unexpected ventilation struct: {ventilation}"
    if set(result["balance_final"].keys()) != {"current", "full", "out_cur", "out_full", "out_hourly", "in_hourly", "in_cur"}:
        return False, f"unexpected balance keys: {result['balance_final'].keys()}"

    return True, "ok"


def _check_sector_events_refresh_snapshot(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime, timedelta

    from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton, QDateTimeEdit, QFrame, QWidget

    from rem_card.data.dto.remcard_dto import PatientStatus, PatientStatusEventDTO
    from rem_card.ui.rem_card_sectors import sector_events
    from rem_card.ui.rem_card_sectors.sector_events import SectorEvents

    fixed_now = datetime(2026, 4, 24, 12, 0)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return fixed_now.replace(tzinfo=tz)
            return fixed_now

    class FakeStatusService:
        def __init__(self, events):
            self.events = events
            self.calls = []

        def get_events_in_range(self, admission_id, shift_start, shift_end):
            self.calls.append(("range", admission_id, shift_start.isoformat(), shift_end.isoformat()))
            return list(self.events)

        def get_events(self, admission_id):
            self.calls.append(("all", admission_id))
            return list(self.events)

    def make_events(start):
        return [
            PatientStatusEventDTO(id=1, admission_id=7, status=PatientStatus.ACTIVE, reason_text="Начало смены", start_time=start - timedelta(hours=2), end_time=start + timedelta(hours=1), created_by="SYSTEM"),
            PatientStatusEventDTO(id=2, admission_id=7, status=PatientStatus.OR, reason_text="Операционная", start_time=start + timedelta(hours=1, minutes=30), end_time=start + timedelta(hours=2, minutes=45), created_by="USER"),
            PatientStatusEventDTO(id=3, admission_id=7, status=PatientStatus.OUT, reason_text="КТ", start_time=start + timedelta(hours=3), end_time=start + timedelta(hours=8), created_by="ADMIN"),
            PatientStatusEventDTO(id=4, admission_id=7, status=PatientStatus.DEAD, reason_text="Биологическая смерть: подтверждена", start_time=start + timedelta(hours=4), end_time=None, created_by="doctor42"),
        ]

    def row_parts(row):
        parts = []
        layout = row.layout()
        for i in range(layout.count()):
            widget = layout.itemAt(i).widget()
            if isinstance(widget, QLabel):
                parts.append(("label", widget.text(), widget.width(), widget.styleSheet(), widget.toolTip()))
            elif isinstance(widget, QLineEdit):
                parts.append(("edit", widget.text(), widget.isReadOnly(), widget.styleSheet()))
            elif isinstance(widget, QDateTimeEdit):
                parts.append(("dt", widget.dateTime().toPython().strftime("%H:%M"), widget.isEnabled(), widget.styleSheet()))
            elif isinstance(widget, QWidget) and widget.layout() is not None:
                nested = []
                for j in range(widget.layout().count()):
                    child = widget.layout().itemAt(j).widget()
                    if isinstance(child, QPushButton):
                        nested.append(("button", child.text(), child.isEnabled(), child.toolTip(), child.styleSheet()))
                parts.append(("container", widget.width(), nested))
            elif isinstance(widget, QWidget):
                parts.append(("spacer", widget.width()))
            else:
                parts.append((type(widget).__name__,))
        return parts

    def capture(*, archive=False, empty=False, no_admission=False):
        shift_start = datetime(2026, 4, 24, 8, 0)
        shift_end = shift_start + (timedelta(hours=2) if archive else timedelta(hours=4))
        service = FakeStatusService([] if empty else make_events(shift_start))
        widget = SectorEvents()
        widget.role = "Врач"
        widget.admission_id = None if no_admission else 7
        widget.status_service = service
        widget.shift_start = shift_start
        widget.shift_end = shift_end
        widget.refresh(force=True)
        rows = []
        for i in range(widget.history_list_layout.count() - 1):
            row = widget.history_list_layout.itemAt(i).widget()
            if isinstance(row, QFrame):
                rows.append(row_parts(row))
        return {
            "calls": service.calls,
            "rows": rows,
            "rollback": widget.btn_rollback.isEnabled(),
            "buttons": {
                "active": (widget.btn_active.isChecked(), widget.btn_active.isEnabled()),
                "out": (widget.btn_out.isChecked(), widget.btn_out.isEnabled()),
                "or": (widget.btn_or.isChecked(), widget.btn_or.isEnabled()),
                "trans": (widget.btn_trans.isChecked(), widget.btn_trans.isEnabled()),
                "dead": (widget.btn_dead.isChecked(), widget.btn_dead.isEnabled()),
            },
        }

    app = QApplication.instance() or QApplication([])
    _ = app, temp_root
    old_datetime = sector_events.datetime
    sector_events.datetime = FixedDateTime
    try:
        live = capture()
        archive = capture(archive=True)
        empty = capture(empty=True)
        no_admission = capture(no_admission=True)
    finally:
        sector_events.datetime = old_datetime

    if live["calls"] != [("range", 7, "2026-04-24T08:00:00", "2026-04-24T12:00:00")]:
        return False, f"unexpected live service calls: {live['calls']}"
    if len(live["rows"]) != 4 or live["rollback"] is not True:
        return False, f"unexpected live rows/rollback: rows={len(live['rows'])}, rollback={live['rollback']}"
    if live["buttons"]["dead"] != (True, False):
        return False, f"unexpected live current-status buttons: {live['buttons']}"

    live_statuses = [row[-3][1] for row in live["rows"]]
    if live_statuses != ["В отделении", "Операционная", "Вне отд.", "Умер"]:
        return False, f"unexpected event order/status labels: {live_statuses}"
    live_comments = [row[-2][1] for row in live["rows"]]
    if live_comments != ["Начало смены", "Операционная", "КТ", ""]:
        return False, f"unexpected event comments: {live_comments}"
    live_creators = [row[-1][1] for row in live["rows"]]
    if live_creators != ["[Система]", "[Врач]", "[Админ]", "[DOCTOR42]"]:
        return False, f"unexpected creator labels: {live_creators}"

    if live["rows"][0][0][0:3] != ("label", "...", 60) or live["rows"][0][0][4] != "24.04.26 06:00":
        return False, f"start-outside marker changed: {live['rows'][0][0]}"
    if live["rows"][2][2][0:3] != ("label", "...", 60):
        return False, f"end-outside marker changed: {live['rows'][2][2]}"
    if any(part[0] == "container" for part in live["rows"][2]):
        return False, "end-outside row unexpectedly has save button container"
    if not any(part[0] == "container" for part in live["rows"][3]):
        return False, "open live row lost comment save button"

    if archive["rollback"] is not False or archive["buttons"]["dead"] != (True, False):
        return False, f"unexpected archive controls: rollback={archive['rollback']}, buttons={archive['buttons']}"
    if not all(row[-2][2] for row in archive["rows"]):
        return False, "archive comments must be read-only"
    if any(part[0] == "container" for row in archive["rows"][1:] for part in row):
        return False, "archive outside rows unexpectedly have save button containers"

    if len(empty["rows"]) != 0 or empty["rollback"] is not False:
        return False, f"empty events state changed: rows={len(empty['rows'])}, rollback={empty['rollback']}"
    if empty["buttons"] != {
        "active": (False, True),
        "out": (False, True),
        "or": (False, True),
        "trans": (False, True),
        "dead": (False, True),
    }:
        return False, f"empty buttons changed: {empty['buttons']}"
    if no_admission["calls"] != [] or len(no_admission["rows"]) != 0:
        return False, f"no-admission guard changed: calls={no_admission['calls']}, rows={len(no_admission['rows'])}"

    return True, "ok"


def _check_statistics_dialog_snapshot(temp_root: str) -> tuple[bool, str]:
    from rem_card.services.analytics.multi_db_analytics import FALLBACK_DDL
    from rem_card.services.analytics.detailed_statistics_service import DetailedStatisticsReportBuilder

    class Manager:
        def __init__(self, conn):
            self.conn = conn

        def get_connection(self):
            return self.conn

    def init_db(conn):
        for ddl in FALLBACK_DDL.values():
            conn.execute(ddl)

    def seed(conn):
        conn.executemany(
            "INSERT INTO admissions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, 101, "2026-04-01 08:00:00", "2026-04-05 10:00:00", None, "переведен", 70, "л", "М", "СМП", "I21", "Инфаркт", 1),
                (2, 102, "2026-04-02 11:00:00", None, "2026-04-03 05:00:00", "умер", 6, "месяцев", "Ж", "Приемное", "J96", "ДН", 2),
                (3, 103, "2026-04-10 13:30:00", None, None, "в отделении", 45, "л", "М", "Перевод", "K35", "Аппендицит", 3),
                (4, 101, "2026-04-15 09:00:00", "2026-04-18 09:00:00", None, "переведен", None, "л", "", "", "", "", 4),
                (5, 104, "2026-03-30 10:00:00", "2026-04-02 10:00:00", None, "переведен", 80, "л", "М", "До периода", "Z00", "Вне периода", 5),
            ],
        )
        conn.executemany(
            "INSERT INTO operations VALUES (?, ?, ?, ?)",
            [
                (1, 1, "2026-04-02 12:00:00", "Операция A"),
                (2, 2, "2026-04-02 13:00:00", "Операция B"),
                (3, 99, "2026-04-02 14:00:00", "Вне admissions"),
            ],
        )
        conn.executemany(
            "INSERT INTO transfusions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, 2, "2026-04-02 14:00:00", "Плазма", 300, "journal", None, None),
                (2, 2, "2026-04-02 15:00:00", "Эритроциты", 250, "journal", None, None),
                (3, 3, "2026-04-11 10:00:00", "Плазма", 200, "journal", None, None),
                (4, 99, "2026-04-11 10:00:00", "Плазма", 999, "journal", None, None),
            ],
        )
        conn.executemany(
            "INSERT INTO ivl_episodes VALUES (?, ?, ?, ?)",
            [
                (1, 2, "2026-04-02 12:00:00", "2026-04-03 06:00:00"),
                (2, 3, "2026-04-11 00:00:00", "2026-04-12 12:00:00"),
                (3, 1, "2026-05-01 00:00:00", "2026-05-02 00:00:00"),
            ],
        )

    def make_builder(conn):
        return DetailedStatisticsReportBuilder(Manager(conn), "2026-04-01", "2026-04-30")

    def make_conn(with_data: bool):
        conn = sqlite3.connect(":memory:")
        init_db(conn)
        if with_data:
            seed(conn)
        return conn

    def snapshot(with_data: bool):
        builder = make_builder(make_conn(with_data))
        stats = builder._calculate_statistics()
        selected = ["s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "s9", "s10", "s11", "s16", "s17", "s18", "s19", "sx"]
        return {
            "stats": stats,
            "rows": {key: builder._section_rows(key, stats) for key in selected},
        }

    result = {"filled": snapshot(True), "empty": snapshot(False)}
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    expected_digest = "8926eca22b054d6c2a0d4d0212da6fc2eafbb0b7ee94b33afe1427dcc348795f"
    if digest != expected_digest:
        return False, f"statistics snapshot changed: {digest}"
    if result["filled"]["stats"]["N"] != 4 or result["filled"]["stats"]["deaths"] != 1:
        return False, f"unexpected filled core stats: {result['filled']['stats']}"
    if result["empty"]["stats"]["N"] != 0 or result["empty"]["stats"]["bed_days"] != 0:
        return False, f"unexpected empty stats: {result['empty']['stats']}"
    return True, "ok"


def _check_vitals_boundary_minutes(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.data.dao.patient_dao import PatientDAO
    from rem_card.data.dao.patient_status_dao import PatientStatusDAO
    from rem_card.data.dao.vitals_dao import VitalsDAO
    from rem_card.data.dto.remcard_dto import PatientStatus, VitalDTO
    from rem_card.services.patient_status_service import PatientStatusService
    from rem_card.services.vital_service import VitalService

    db_path = os.path.join(temp_root, "vitals_boundary_minutes.db")
    manager = DatabaseManager(db_path, db_path)

    def seed_patient(
        *,
        history_number: str,
        admission_dt: datetime,
        terminal_dt: datetime | None = None,
        terminal_status: PatientStatus | None = None,
    ) -> int:
        with manager.remcard_transaction(source=f"regression_seed_{history_number}") as cursor:
            cursor.execute("INSERT INTO patients(full_name) VALUES (?)", (f"Boundary {history_number}",))
            patient_id = int(cursor.lastrowid)

            transfer_dt = terminal_dt if terminal_status == PatientStatus.TRANSFERRED else None
            death_dt = terminal_dt if terminal_status == PatientStatus.DEAD else None
            cursor.execute(
                """
                INSERT INTO admissions(
                    patient_id,
                    bed_number,
                    history_number,
                    admission_datetime,
                    transfer_datetime,
                    death_datetime,
                    is_active
                )
                VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    patient_id,
                    patient_id,
                    history_number,
                    admission_dt.isoformat(),
                    transfer_dt.isoformat() if transfer_dt else None,
                    death_dt.isoformat() if death_dt else None,
                ),
            )
            admission_id = int(cursor.lastrowid)

            active_end = terminal_dt.isoformat() if terminal_dt else None
            cursor.execute(
                """
                INSERT INTO patient_status_events(
                    admission_id,
                    status,
                    start_time,
                    end_time,
                    created_by,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, 'REGRESSION', ?, ?)
                """,
                (
                    admission_id,
                    PatientStatus.ACTIVE.value,
                    admission_dt.isoformat(),
                    active_end,
                    admission_dt.isoformat(),
                    admission_dt.isoformat(),
                ),
            )

            if terminal_status and terminal_dt:
                cursor.execute(
                    """
                    INSERT INTO patient_status_events(
                        admission_id,
                        status,
                        start_time,
                        created_by,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, 'REGRESSION', ?, ?)
                    """,
                    (
                        admission_id,
                        terminal_status.value,
                        terminal_dt.isoformat(),
                        terminal_dt.isoformat(),
                        terminal_dt.isoformat(),
                    ),
                )
            return admission_id

    try:
        patient_dao = PatientDAO(manager)
        status_service = PatientStatusService(PatientStatusDAO(manager))
        vital_service = VitalService(VitalsDAO(manager), patient_dao, status_service)

        admission_dt = datetime(2026, 4, 24, 20, 0, 41, 123456)
        admission_id = seed_patient(history_number="REG-VITAL-ADMIT", admission_dt=admission_dt)

        before_ok, _ = vital_service.validate_timestamp(
            admission_id,
            datetime(2026, 4, 24, 19, 59),
            admission_dt,
        )
        at_ok, at_msg = vital_service.validate_timestamp(
            admission_id,
            datetime(2026, 4, 24, 20, 0),
            admission_dt,
        )
        if before_ok:
            return False, "19:59 was accepted for a 20:00 admission"
        if not at_ok:
            return False, f"20:00 was rejected for a 20:00 admission: {at_msg}"

        vital_service.add_vital(
            VitalDTO(
                id=None,
                admission_id=admission_id,
                timestamp=datetime(2026, 4, 24, 20, 0),
                pulse=80,
            ),
            shift_date=admission_dt,
        )
        visible_vitals = vital_service.get_vitals(admission_id, admission_dt)
        if len(visible_vitals) != 1:
            return False, f"20:00 vital was saved but not visible, count={len(visible_vitals)}"

        terminal_dt = datetime(2026, 4, 24, 23, 0, 37)
        for status in (PatientStatus.OUT, PatientStatus.OR, PatientStatus.TRANSFERRED, PatientStatus.DEAD):
            terminal_admission_id = seed_patient(
                history_number=f"REG-VITAL-{status.value}",
                admission_dt=datetime(2026, 4, 24, 20, 0),
                terminal_dt=terminal_dt,
                terminal_status=status,
            )
            terminal_ok, terminal_msg = vital_service.validate_timestamp(
                terminal_admission_id,
                datetime(2026, 4, 24, 23, 0),
                terminal_dt,
            )
            after_ok, _ = vital_service.validate_timestamp(
                terminal_admission_id,
                datetime(2026, 4, 24, 23, 1),
                terminal_dt,
            )
            if not terminal_ok:
                return False, f"23:00 was rejected for {status.value}: {terminal_msg}"
            if after_ok:
                return False, f"23:01 was accepted after {status.value} at 23:00"

        return True, "ok"
    finally:
        manager.close()


def _check_orders_force_refresh_accepts_unchanged_version(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    from rem_card.services.read_coordinator import ReadCoordinator

    class StaticOrdersService:
        def __init__(self):
            self.calls = 0

        def build_orders_snapshot(self, admission_id, shift_date, *, only_committed=False, include_change_cursor=False):
            self.calls += 1
            snapshot = {
                "admission_id": admission_id,
                "shift_date": shift_date,
                "only_committed": bool(only_committed),
                "orders": [],
                "admin_rows": [],
                "has_any_draft": False,
                "has_any_administrations": False,
                "has_any_orders": False,
            }
            if include_change_cursor:
                snapshot["change_id"] = 42
            return snapshot

        def get_latest_change_id(self, admission_id=None, include_global=True):
            return 42

    service = StaticOrdersService()
    coordinator = ReadCoordinator(service)
    shift_date = datetime(2026, 4, 24, 12, 0, 0)
    context = coordinator.make_orders_context(
        source_db="live",
        admission_id=1,
        shift_date=shift_date,
        role="doctor",
        mode="live",
        variant="full",
    )

    first = coordinator.load_orders_tab(context, source="user", priority="HIGH")
    coordinator.invalidate_tab(context, reason="regression_force_refresh")
    second = coordinator.load_orders_tab(context, source="refresh", priority="HIGH", force_refresh=True)

    if int(first.get("version") or 0) != 42:
        return False, f"unexpected first version: {first.get('version')}"
    if int(second.get("version") or 0) != 42:
        return False, f"unexpected second version: {second.get('version')}"
    if service.calls < 2:
        return False, f"force refresh did not rebuild snapshot, calls={service.calls}"
    return True, "ok"


def _check_doctor_orders_late_model_binding(temp_root: str) -> tuple[bool, str]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from datetime import datetime, timedelta

    from PySide6.QtCore import QObject
    from PySide6.QtWidgets import QApplication

    from rem_card.data.dto.remcard_dto import OrderDTO, OrderStatus, OrderType
    from rem_card.ui.doctor_view.orders_widget import OrdersWidget

    class DummyOrdersService(QObject):
        def get_day_period(self, shift_date):
            start = shift_date.replace(hour=8, minute=0, second=0, microsecond=0)
            return start, start + timedelta(hours=24)

    app = QApplication.instance() or QApplication([])
    service = DummyOrdersService()
    widget = OrdersWidget(service=service, admission_id=1, shift_date=datetime(2026, 4, 24, 12), defer_ui=True)
    try:
        widget._ensure_model_initialized()
        if widget.model is None:
            return False, "model was not initialized before UI setup"
        widget.model.orders = [object()]

        widget.setup_ui()
        widget.show()
        app.processEvents()

        if widget.table_view.model() is not widget.model:
            return False, "late-created table did not bind existing orders model"
        if widget.table_view.verticalHeader().count() != 1:
            return False, f"table header row count mismatch: {widget.table_view.verticalHeader().count()}"
        if widget.table_view.rowHeight(0) <= 0:
            return False, f"first row is collapsed: height={widget.table_view.rowHeight(0)}"

        draft_events = []
        widget.draftStatusChanged.connect(lambda active: draft_events.append(bool(active)))
        order = OrderDTO(
            id=10,
            admission_id=1,
            drug_key="local_delete_probe",
            latin="Local Delete Probe",
            type=OrderType.MEDICATION,
            status=OrderStatus.ACTIVE,
            is_committed=1,
            created_at=datetime(2026, 4, 24, 9),
        )
        widget.model.orders = [order]
        widget.model.admin_map = {}
        widget.model.has_any_draft = False
        widget._cached_has_drafts = False
        widget._mark_local_order_row_deleted(0, order, was_committed=True)
        if not widget.has_drafts() or not draft_events or draft_events[-1] is not True:
            return False, "local row delete did not emit active draft state"
        return True, "ok"
    finally:
        widget.close()


def _check_orders_widget_skips_duplicate_snapshot(temp_root: str) -> tuple[bool, str]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from datetime import datetime, timedelta

    from PySide6.QtCore import QObject
    from PySide6.QtWidgets import QApplication

    from rem_card.data.dto.remcard_dto import OrderDTO, OrderStatus, OrderType
    from rem_card.services.read_coordinator import ReadCoordinator
    from rem_card.ui.doctor_view.orders_widget import OrdersWidget

    class DummyOrdersService(QObject):
        def get_day_period(self, shift_date):
            start = shift_date.replace(hour=8, minute=0, second=0, microsecond=0)
            return start, start + timedelta(hours=24)

    app = QApplication.instance() or QApplication([])
    shift_date = datetime(2026, 4, 24, 12)
    service = DummyOrdersService()
    service.read_coordinator = ReadCoordinator(service)
    widget = OrdersWidget(service=service, admission_id=1, shift_date=shift_date, defer_ui=True)
    try:
        widget._ensure_model_initialized()
        if widget.model is None:
            return False, "model was not initialized"
        context = service.read_coordinator.make_orders_context(
            source_db="live",
            admission_id=1,
            shift_date=shift_date,
            role="doctor",
            mode="live",
            variant="full",
        )
        context_key = context.cache_key()
        context_hash = context.hash()

        original_apply_snapshot = widget.model.apply_snapshot
        apply_count = 0

        def counted_apply_snapshot(snapshot):
            nonlocal apply_count
            apply_count += 1
            return original_apply_snapshot(snapshot)

        widget.model.apply_snapshot = counted_apply_snapshot
        snapshot = {
            "admission_id": 1,
            "shift_date": shift_date,
            "only_committed": False,
            "orders": [
                OrderDTO(
                    id=10,
                    admission_id=1,
                    drug_key="duplicate_snapshot_probe",
                    latin="Duplicate Snapshot Probe",
                    type=OrderType.MEDICATION,
                    status=OrderStatus.ACTIVE,
                    is_committed=1,
                    created_at=datetime(2026, 4, 24, 9),
                )
            ],
            "admin_rows": [],
            "has_any_draft": False,
            "has_any_administrations": False,
            "has_any_orders": True,
            "change_id": 7,
            "version": 7,
            "context_hash": context_hash,
            "load_trace_id": "orders-duplicate-000001",
            "source": "refresh",
        }

        first_ok = widget._apply_snapshot_data(
            snapshot=snapshot,
            admission_id=1,
            shift_date=shift_date,
            context_key=context_key,
        )
        second_ok = widget._apply_snapshot_data(
            snapshot=snapshot,
            admission_id=1,
            shift_date=shift_date,
            context_key=context_key,
        )
        app.processEvents()

        if not first_ok or not second_ok:
            return False, f"snapshot apply returned first={first_ok} second={second_ok}"
        if apply_count != 1:
            return False, f"duplicate snapshot reset was not skipped, apply_count={apply_count}"
        if len(widget.model.orders) != 1:
            return False, f"unexpected model rows after duplicate skip: {len(widget.model.orders)}"

        previous_context = service.read_coordinator.make_orders_context(
            source_db="live",
            admission_id=7,
            shift_date=shift_date,
            role="doctor",
            mode="live",
            variant="full",
        )
        current_context = service.read_coordinator.make_orders_context(
            source_db="live",
            admission_id=5,
            shift_date=shift_date,
            role="doctor",
            mode="live",
            variant="full",
        )
        widget.admission_id = 5
        widget.shift_date = shift_date
        widget._last_polled_change_id = 49793
        widget._last_polled_context_key = previous_context.cache_key()
        widget._last_applied_snapshot_signature = None
        drift_snapshot = {
            "admission_id": 5,
            "shift_date": shift_date,
            "only_committed": False,
            "orders": [],
            "admin_rows": [],
            "has_any_draft": False,
            "has_any_administrations": False,
            "has_any_orders": False,
            "change_id": 49781,
            "version": 49781,
            "context_hash": current_context.hash(),
            "load_trace_id": "orders-context-drift",
            "source": "refresh",
        }
        drift_ok = widget._apply_snapshot_data(
            snapshot=drift_snapshot,
            admission_id=5,
            shift_date=shift_date,
            context_key=current_context.cache_key(),
        )
        if not drift_ok or widget._snapshot_stale:
            return False, "context-drift cursor caused stale snapshot loop"
        if int(widget._last_polled_change_id or 0) != 49781:
            return False, f"context-drift cursor was not reset: {widget._last_polled_change_id}"
        return True, "ok"
    finally:
        widget.close()


def _check_order_row_delete_without_times_marks_draft(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.data.dao.remcard_dao import FluidsDAO, OrdersDAO, PatientDAO, VentilationDAO, VitalsDAO
    from rem_card.data.dto.remcard_dto import OrderDTO, OrderStatus, OrderType
    from rem_card.services.remcard_service import RemCardService

    db_path = os.path.join(temp_root, "orders_no_times_delete.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        with manager.remcard_transaction(source="regression_seed_patient") as cursor:
            cursor.execute("INSERT INTO patients(full_name) VALUES (?)", ("Regression Patient",))
            patient_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO admissions(patient_id, bed_number, history_number, admission_datetime)
                VALUES (?, ?, ?, ?)
                """,
                (patient_id, 1, "REG-1", "2026-04-24T08:00:00"),
            )
            admission_id = int(cursor.lastrowid)

        service = RemCardService(
            VitalsDAO(manager),
            FluidsDAO(manager),
            OrdersDAO(manager),
            VentilationDAO(manager),
            PatientDAO(manager),
        )
        shift_date = datetime(2026, 4, 24, 12, 0, 0)
        order = OrderDTO(
            admission_id=admission_id,
            drug_key="regression_empty_schedule",
            latin="Regression Empty Schedule",
            type=OrderType.MEDICATION,
            status=OrderStatus.ACTIVE,
            dose_value=1.0,
            dose_unit="mg",
            is_per_kg=False,
            frequency=1,
            specific_times=[],
            duration_min=0,
            is_committed=0,
            created_at=datetime(2026, 4, 24, 9, 0, 0),
            comment="",
            last_modified_by="doctor",
        )

        service.add_order(order)
        if order.id is None:
            return False, "order insert did not return id"
        service.finalize_order_card(admission_id, shift_date=shift_date)

        saved_snapshot = service.build_orders_snapshot(admission_id, shift_date, only_committed=False)
        if len(saved_snapshot["orders"]) != 1 or saved_snapshot["has_any_draft"]:
            return False, f"unexpected saved snapshot: orders={len(saved_snapshot['orders'])}, draft={saved_snapshot['has_any_draft']}"
        if len(service.get_orders(admission_id, shift_date, only_committed=True)) != 1:
            return False, "saved no-time order is not visible to committed reader"

        service.soft_delete_order_row(order.id, True)
        deleted_snapshot = service.build_orders_snapshot(admission_id, shift_date, only_committed=False)
        if deleted_snapshot["orders"]:
            return False, "deleted no-time order is still visible in doctor snapshot"
        if not deleted_snapshot["has_any_draft"]:
            return False, "deleted no-time order did not mark doctor snapshot as draft"
        if not service.has_order_drafts(admission_id, shift_date):
            return False, "shift-scoped draft query missed deleted no-time order"
        if service.get_orders(admission_id, shift_date, only_committed=True):
            return False, "deleted no-time order is still visible to committed reader before save"

        service.finalize_order_card(admission_id, shift_date=shift_date)
        if service.has_order_drafts(admission_id, shift_date):
            return False, "draft flag remained after finalizing deleted no-time order"
        if service.get_orders(admission_id, shift_date, only_committed=False):
            return False, "deleted no-time order is visible to doctor after final save"
        if service.get_orders(admission_id, shift_date, only_committed=True):
            return False, "deleted no-time order is visible to committed reader after final save"
        return True, "ok"
    finally:
        manager.close()


def _check_orders_cell_delete_draft_and_noop_toggle(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    from PySide6.QtCore import Qt

    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.data.dao.remcard_dao import FluidsDAO, OrdersDAO, PatientDAO, VentilationDAO, VitalsDAO
    from rem_card.data.dto.remcard_dto import OrderDTO, OrderStatus, OrderType
    from rem_card.services.order_domain_service import NURSE_MARK_EXECUTED
    from rem_card.services.read_coordinator import ReadCoordinator
    from rem_card.services.remcard_service import RemCardService
    from rem_card.ui.shared.orders_model import OrdersModel

    db_path = os.path.join(temp_root, "orders_cell_delete_draft.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        with manager.remcard_transaction(source="regression_seed_patient") as cursor:
            cursor.execute("INSERT INTO patients(full_name) VALUES (?)", ("Regression Patient",))
            patient_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO admissions(patient_id, bed_number, history_number, admission_datetime)
                VALUES (?, ?, ?, ?)
                """,
                (patient_id, 1, "REG-CELL", "2026-04-24T08:00:00"),
            )
            admission_id = int(cursor.lastrowid)

        service = RemCardService(
            VitalsDAO(manager),
            FluidsDAO(manager),
            OrdersDAO(manager),
            VentilationDAO(manager),
            PatientDAO(manager),
        )
        shift_date = datetime(2026, 4, 24, 12, 0, 0)
        order = OrderDTO(
            admission_id=admission_id,
            drug_key="regression_cell",
            latin="Regression Cell",
            type=OrderType.MEDICATION,
            status=OrderStatus.ACTIVE,
            dose_value=1.0,
            dose_unit="mg",
            is_per_kg=False,
            frequency=1,
            specific_times=[],
            duration_min=0,
            is_committed=0,
            created_at=datetime(2026, 4, 24, 9, 0, 0),
            comment="",
            last_modified_by="doctor",
        )
        service.add_order(order)
        service.finalize_order_card(admission_id, shift_date=shift_date)

        saved_slot = datetime(2026, 4, 24, 10, 0, 0)
        empty_slot = datetime(2026, 4, 24, 11, 0, 0)
        service.apply_order_left_click(order, None, saved_slot)
        service.finalize_order_card(admission_id, shift_date=shift_date)
        saved_snapshot = service.build_orders_snapshot(admission_id, shift_date, only_committed=False)
        if saved_snapshot["has_any_draft"]:
            return False, "saved baseline unexpectedly has drafts"
        baseline_rows = [
            dict(row)
            for row in saved_snapshot["admin_rows"]
            if int(dict(row).get("order_id") or 0) == int(order.id)
            and str(dict(row).get("planned_time") or "") == saved_slot.isoformat()
        ]
        if not baseline_rows:
            return False, "saved baseline committed cell row is missing"
        baseline_admin_id = int(baseline_rows[-1]["id"])

        service.apply_order_left_click(order, None, saved_slot)
        deleted_snapshot = service.build_orders_snapshot(admission_id, shift_date, only_committed=False)
        if not deleted_snapshot["has_any_draft"]:
            return False, "deleted saved cell did not keep draft flag"
        latest_deleted = [
            dict(row)
            for row in deleted_snapshot["admin_rows"]
            if int(dict(row).get("order_id") or 0) == int(order.id)
            and str(dict(row).get("planned_time") or "") == saved_slot.isoformat()
        ][-1]
        if latest_deleted.get("status") != "deleted" or int(latest_deleted.get("is_committed") or 0) != 0:
            return False, f"saved-cell delete did not produce uncommitted tombstone: {latest_deleted}"

        model = OrdersModel(service, admission_id=admission_id, shift_date=shift_date)
        model.apply_snapshot(deleted_snapshot)
        deleted_admin = model.data(model.index(0, 3), Qt.UserRole)
        if deleted_admin is None or deleted_admin.status != "deleted" or not model.has_any_draft:
            return False, "OrdersModel dropped deleted draft tombstone"

        try:
            service.set_nurse_order_mark(baseline_admin_id, NURSE_MARK_EXECUTED)
        except RuntimeError as exc:
            return False, f"nurse mark was blocked by unsaved doctor cell draft: {exc}"
        nurse_rows = service.get_nurse_orders_data(admission_id, shift_date)
        nurse_row = next((dict(row) for row in nurse_rows if int(dict(row).get("id") or 0) == baseline_admin_id), None)
        if nurse_row is None or nurse_row.get("comment") != NURSE_MARK_EXECUTED:
            return False, f"nurse mark did not apply to committed baseline during doctor draft: {nurse_rows}"

        coordinator = ReadCoordinator(service)
        context = coordinator.make_orders_context(
            source_db="live",
            admission_id=admission_id,
            shift_date=shift_date,
            role="doctor",
            mode="live",
            variant="full",
        )
        delta_snapshot = coordinator._change_log_applier.apply_orders_delta(
            context=context,
            base_snapshot=saved_snapshot,
            latest_change_id=service.get_latest_change_id(admission_id),
        )
        if not delta_snapshot.get("has_any_draft"):
            return False, "ReadCoordinator delta lost deleted draft flag"
        if not any(str(dict(row).get("status") or "") == "deleted" for row in delta_snapshot.get("admin_rows") or []):
            return False, "ReadCoordinator delta removed deleted tombstone row"

        service.apply_order_left_click(order, None, saved_slot)
        restored_snapshot = service.build_orders_snapshot(admission_id, shift_date, only_committed=False)
        if restored_snapshot["has_any_draft"]:
            return False, "delete-then-restore saved cell left a no-op draft"

        service.apply_order_left_click(order, None, empty_slot)
        if not service.has_order_drafts(admission_id, shift_date):
            return False, "new draft cell did not mark card dirty"
        service.apply_order_left_click(order, None, empty_slot)
        if service.has_order_drafts(admission_id, shift_date):
            return False, "quick add-then-remove empty cell left a no-op draft"
        empty_rows = [
            dict(row)
            for row in service.get_latest_administrations(
                admission_id=admission_id,
                shift_date=shift_date,
                only_committed=False,
                include_deleted=True,
                include_cancelled=True,
                include_deleted_orders=True,
            )
            if int(dict(row).get("order_id") or 0) == int(order.id)
            and str(dict(row).get("planned_time") or "") == empty_slot.isoformat()
        ]
        if empty_rows:
            return False, f"quick add-then-remove left effective rows: {empty_rows}"

        return True, "ok"
    finally:
        manager.close()


def _check_orders_optimistic_lock_conflicts(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.data.dao.remcard_dao import FluidsDAO, OrdersDAO, PatientDAO, VentilationDAO, VitalsDAO
    from rem_card.data.dto.remcard_dto import OrderDTO, OrderStatus, OrderType
    from rem_card.services.order_service import ORDER_CONFLICT_MESSAGE, OrderConflictError
    from rem_card.services.remcard_service import RemCardService

    db_path = os.path.join(temp_root, "orders_optimistic_lock.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        with manager.remcard_transaction(source="regression_seed_patient") as cursor:
            cursor.execute("INSERT INTO patients(full_name) VALUES (?)", ("Regression Patient",))
            patient_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO admissions(patient_id, bed_number, history_number, admission_datetime)
                VALUES (?, ?, ?, ?)
                """,
                (patient_id, 1, "REG-LOCK", "2026-04-24T08:00:00"),
            )
            admission_id = int(cursor.lastrowid)

        service = RemCardService(
            VitalsDAO(manager),
            FluidsDAO(manager),
            OrdersDAO(manager),
            VentilationDAO(manager),
            PatientDAO(manager),
        )
        shift_date = datetime(2026, 4, 24, 12, 0, 0)

        def new_order(name: str) -> OrderDTO:
            return OrderDTO(
                admission_id=admission_id,
                drug_key=name.lower(),
                latin=name,
                type=OrderType.MEDICATION,
                status=OrderStatus.ACTIVE,
                dose_value=1.0,
                dose_unit="mg",
                is_per_kg=False,
                frequency=1,
                specific_times=[],
                duration_min=0,
                is_committed=0,
                created_at=datetime(2026, 4, 24, 9, 0, 0),
                comment="",
                last_modified_by="doctor",
            )

        first = new_order("Lock One")
        second = new_order("Lock Two")
        service.add_order(first)
        service.add_order(second)
        if first.id is None or second.id is None:
            return False, "order insert did not return ids"

        initial = {order.id: order.revision for order in service.get_orders(admission_id, shift_date)}
        if initial.get(first.id) != 0 or initial.get(second.id) != 0:
            return False, f"unexpected initial revisions: {initial}"

        service.update_order_status(first.id, "held", expected_revision=initial[first.id])
        changed_first = next(order for order in service.get_orders(admission_id, shift_date) if order.id == first.id)
        if int(changed_first.revision or 0) != 1:
            return False, f"order revision did not increment after update: {changed_first.revision}"

        try:
            service.update_order_status(first.id, "active", expected_revision=initial[first.id])
            return False, "stale order update did not raise conflict"
        except OrderConflictError as exc:
            if ORDER_CONFLICT_MESSAGE not in str(exc):
                return False, f"unexpected conflict message: {exc}"

        try:
            service.save_order_draft_sort(admission_id, shift_date, [first.id, second.id], expected_revisions=initial)
            return False, "stale order sort did not raise conflict"
        except OrderConflictError:
            pass

        latest = {order.id: order.revision for order in service.get_orders(admission_id, shift_date)}
        service.save_order_draft_sort(admission_id, shift_date, [second.id, first.id], expected_revisions=latest)
        after_sort = {order.id: order.revision for order in service.get_orders(admission_id, shift_date)}
        if int(after_sort.get(second.id, 0)) <= int(latest.get(second.id, 0)):
            return False, "order sort did not increment revision"

        try:
            service.finalize_order_card(admission_id, shift_date=shift_date, expected_revisions=latest)
            return False, "stale order finalize did not raise conflict"
        except OrderConflictError:
            pass

        latest = {order.id: order.revision for order in service.get_orders(admission_id, shift_date)}
        service.soft_delete_order_row(second.id, False, expected_revision=latest[second.id])
        try:
            service.soft_delete_order_row(first.id, False, expected_revision=initial[first.id])
            return False, "stale order soft-delete did not raise conflict"
        except OrderConflictError:
            pass

        return True, "ok"
    finally:
        manager.close()


def _check_remaining_clinical_optimistic_lock_conflicts(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime, timedelta

    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.data.dao.fluids_dao import FluidsDAO
    from rem_card.data.dao.patient_dao import PatientDAO
    from rem_card.data.dao.patient_status_dao import PatientStatusDAO
    from rem_card.data.dao.ventilation_dao import VentilationDAO
    from rem_card.data.dao.vitals_dao import VitalsDAO
    from rem_card.data.dto.remcard_dto import PatientStatus, VentilationEventType, VentilationMode, VitalDTO
    from rem_card.services.concurrency import DATA_CONFLICT_MESSAGE, DataConflictError
    from rem_card.services.fluid_service import FluidService
    from rem_card.services.patient_bed_management.service import PatientBedManagementService
    from rem_card.services.patient_status_service import PatientStatusService
    from rem_card.services.ventilation_service import VentilationService
    from rem_card.services.vital_service import VitalService

    saved_local_first = os.environ.get("REMCARD_LOCAL_FIRST_SYNC")
    os.environ["REMCARD_LOCAL_FIRST_SYNC"] = "0"
    db_path = os.path.join(temp_root, "remaining_optimistic_lock.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        with manager.remcard_transaction(source="regression_seed_remaining_locks") as cursor:
            cursor.execute("INSERT INTO beds(bed_number, status, current_admission_id) VALUES (1, 'FREE', NULL)")
            cursor.execute("INSERT INTO beds(bed_number, status, current_admission_id) VALUES (2, 'FREE', NULL)")
            cursor.execute("INSERT INTO patients(full_name) VALUES (?)", ("Clinical Lock Patient",))
            patient_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO admissions(patient_id, bed_number, history_number, admission_datetime)
                VALUES (?, ?, ?, ?)
                """,
                (patient_id, 1, "REG-CLIN-LOCK", "2026-04-24T08:00:00"),
            )
            admission_id = int(cursor.lastrowid)
            cursor.execute(
                """
                UPDATE beds
                SET status = 'OCCUPIED',
                    current_admission_id = ?,
                    revision = COALESCE(revision, 0) + 1
                WHERE bed_number = 1
                """,
                (admission_id,),
            )
            cursor.execute(
                """
                INSERT INTO patient_status_events(admission_id, status, start_time, created_by, created_at, updated_at)
                VALUES (?, ?, ?, 'test', ?, ?)
                """,
                (admission_id, PatientStatus.ACTIVE.value, "2026-04-24T08:00:00", "2026-04-24T08:00:00", "2026-04-24T08:00:00"),
            )

        patient_dao = PatientDAO(manager)
        vitals_dao = VitalsDAO(manager)
        vital_service = VitalService(vitals_dao, patient_dao)
        fluid_service = FluidService(FluidsDAO(manager), vital_service)
        shift_date = datetime(2026, 4, 24, 12, 0, 0)

        fluid_service.upsert_hourly_output(admission_id, shift_date, 10, "urine", 100)
        fluid = fluid_service.get_fluids(admission_id, shift_date)[0]
        fluid_service.upsert_hourly_output(admission_id, shift_date, 10, "urine", 120, expected_revision=fluid.revision)
        try:
            fluid_service.upsert_hourly_output(admission_id, shift_date, 10, "urine", 140, expected_revision=fluid.revision)
            return False, "stale fluids update did not raise conflict"
        except DataConflictError as exc:
            if DATA_CONFLICT_MESSAGE not in str(exc):
                return False, f"unexpected fluids conflict message: {exc}"

        vital_time = datetime(2026, 4, 24, 10, 30, 0)
        vital_service.add_vital(
            VitalDTO(id=None, admission_id=admission_id, timestamp=vital_time, sys=120, dia=70, pulse=80),
            shift_date=shift_date,
            force=True,
        )
        vital = vital_service.get_vitals(admission_id, shift_date)[0]
        vital_service.add_vital(
            VitalDTO(id=None, admission_id=admission_id, timestamp=vital_time, sys=121),
            shift_date=shift_date,
            force=True,
            expected_revision=vital.revision,
        )
        try:
            vital_service.add_vital(
                VitalDTO(id=None, admission_id=admission_id, timestamp=vital_time, sys=122),
                shift_date=shift_date,
                force=True,
                expected_revision=vital.revision,
            )
            return False, "stale vitals update did not raise conflict"
        except DataConflictError:
            pass

        bed_service = PatientBedManagementService(manager)
        patient, admission = bed_service.get_patient_with_current_admission(1)
        if not patient or not admission:
            return False, "seeded bed/admission was not visible"
        bed_service.update_patient_and_admission(
            patient.id,
            admission.id,
            {"full_name": "Clinical Lock Patient"},
            {
                "bed_number": 1,
                "history_number": "REG-CLIN-LOCK-2",
                "admission_datetime": admission.admission_datetime,
            },
            expected_admission_revision=admission.revision,
        )
        try:
            bed_service.update_patient_and_admission(
                patient.id,
                admission.id,
                {"full_name": "Clinical Lock Patient"},
                {
                    "bed_number": 1,
                    "history_number": "REG-CLIN-LOCK-3",
                    "admission_datetime": admission.admission_datetime,
                },
                expected_admission_revision=admission.revision,
            )
            return False, "stale admission update did not raise conflict"
        except DataConflictError:
            pass

        source_bed = bed_service.get_bed_by_number(1)
        target_bed = bed_service.get_bed_by_number(2)
        _patient, latest_admission = bed_service.get_patient_with_current_admission(1)
        bed_service.move_patient(
            1,
            2,
            expected_source_bed_revision=int(source_bed["revision"] or 0),
            expected_target_bed_revision=int(target_bed["revision"] or 0),
            expected_source_admission_revision=latest_admission.revision,
        )
        try:
            bed_service.move_patient(2, 1, expected_source_bed_revision=0)
            return False, "stale bed move did not raise conflict"
        except DataConflictError:
            pass

        status_service = PatientStatusService(PatientStatusDAO(manager))
        current = status_service.get_current_status(admission_id)
        status_service.change_status(
            admission_id,
            PatientStatus.OUT,
            reason_text="test",
            user_id="test",
            expected_active_event_id=current.id,
            expected_active_revision=current.revision,
        )
        try:
            status_service.change_status(
                admission_id,
                PatientStatus.OR,
                reason_text="stale",
                user_id="test",
                expected_active_event_id=current.id,
                expected_active_revision=current.revision,
            )
            return False, "stale status change did not raise conflict"
        except DataConflictError:
            pass

        vent_service = VentilationService(VentilationDAO(manager))
        start_time = datetime(2026, 4, 24, 9, 0, 0)
        case = vent_service.create_case(
            admission_id,
            start_time=start_time,
            initial_mode=VentilationMode.CONTROLLED_VCV,
            initial_parameters={"RR": 12, "TV": 500, "PEEP": 5, "FiO2": 50, "Flow": 40},
        )
        vent_service.add_event(
            case.id,
            event_time=start_time + timedelta(minutes=10),
            event_type=VentilationEventType.MODE_CHANGE,
            mode=VentilationMode.CONTROLLED_VCV,
            parameters={"RR": 13, "TV": 500, "PEEP": 5, "FiO2": 50, "Flow": 40},
            expected_case_revision=case.revision,
        )
        try:
            vent_service.add_event(
                case.id,
                event_time=start_time + timedelta(minutes=20),
                event_type=VentilationEventType.MODE_CHANGE,
                mode=VentilationMode.CONTROLLED_VCV,
                parameters={"RR": 14, "TV": 500, "PEEP": 5, "FiO2": 50, "Flow": 40},
                expected_case_revision=case.revision,
            )
            return False, "stale ventilation event did not raise conflict"
        except DataConflictError:
            pass

        quick = manager.fetch_one_remcard("PRAGMA quick_check")
        if not quick or str(quick[0]).lower() != "ok":
            return False, f"quick_check failed after optimistic lock checks: {quick}"
        return True, "ok"
    finally:
        manager.close()
        if saved_local_first is None:
            os.environ.pop("REMCARD_LOCAL_FIRST_SYNC", None)
        else:
            os.environ["REMCARD_LOCAL_FIRST_SYNC"] = saved_local_first


def _check_analytics_runs_outside_ui_callbacks(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    graphs_source = (PROJECT_ROOT / "ui" / "analytics" / "graphs_dialog.py").read_text(encoding="utf-8")
    report_source = (PROJECT_ROOT / "ui" / "analytics" / "report_dialog.py").read_text(encoding="utf-8")
    detailed_report_source = (PROJECT_ROOT / "ui" / "analytics" / "statistics_dialog.py").read_text(encoding="utf-8")
    worker_source = (PROJECT_ROOT / "ui" / "shared" / "analytics_worker.py").read_text(encoding="utf-8")
    pdf_worker_source = (PROJECT_ROOT / "ui" / "shared" / "html_pdf_worker.py").read_text(encoding="utf-8")
    graph_service_source = (PROJECT_ROOT / "services" / "analytics" / "graphs_service.py").read_text(encoding="utf-8")
    statistics_service_source = (PROJECT_ROOT / "services" / "analytics" / "statistics_service.py").read_text(encoding="utf-8")
    detailed_statistics_service_source = (
        PROJECT_ROOT / "services" / "analytics" / "detailed_statistics_service.py"
    ).read_text(encoding="utf-8")

    forbidden_ui_tokens = ("cursor.execute", "pd.read_sql", "matplotlib", "QPdfWriter", "QTextDocument", "generate_g")
    for label, source in (
        ("graphs_dialog", graphs_source),
        ("report_dialog", report_source),
        ("statistics_dialog", detailed_report_source),
    ):
        for token in forbidden_ui_tokens:
            if token in source:
                return False, f"{label} still contains heavy analytics token: {token}"

    if "class AnalyticsWorker(QThread)" not in worker_source or "self._operation()" not in worker_source:
        return False, "AnalyticsWorker does not own callable execution"
    if "class HtmlPdfWorker(QThread)" not in pdf_worker_source or "QPdfWriter" not in pdf_worker_source:
        return False, "HtmlPdfWorker does not own HTML PDF generation"
    for label, source in (
        ("graphs dialog", graphs_source),
        ("report dialog", report_source),
        ("statistics dialog", detailed_report_source),
    ):
        if "def reject(self):" not in source or "def closeEvent(self, event):" not in source:
            return False, f"{label} must cancel/ignore worker callbacks on reject and closeEvent"
        if "self._closing = True" not in source:
            return False, f"{label} must ignore worker callbacks after close/reject"
    if "build_graphs_html" not in graph_service_source or "generate_g1_g5" not in graph_service_source:
        return False, "graphs service does not own graph generation"
    if "build_statistical_report_html" not in statistics_service_source or "cursor.execute" not in statistics_service_source:
        return False, "statistics service does not own SQL report generation"
    if (
        "build_detailed_statistics_report_html" not in detailed_statistics_service_source
        or "cursor.execute" not in detailed_statistics_service_source
    ):
        return False, "detailed statistics service does not own detailed SQL report generation"
    return True, "ok"


def _check_medical_audit_log_triggers(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.app.unified_db_schema import SCHEMA_MIN_MIGRATION_VERSION

    db_path = os.path.join(temp_root, "medical_audit_log.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        with manager.remcard_transaction(source="regression_seed_medical_audit") as cursor:
            cursor.execute("INSERT INTO patients(full_name) VALUES (?)", ("Audit Patient",))
            patient_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO admissions(patient_id, bed_number, history_number, admission_datetime)
                VALUES (?, ?, ?, ?)
                """,
                (patient_id, 1, "REG-AUDIT-001", "2026-05-03 08:00:00"),
            )
            admission_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO orders(
                    admission_id, datetime, text, drug_key, latin, type, status,
                    is_committed, revision, last_modified_by, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, 'doctor', STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
                """,
                (
                    admission_id,
                    "2026-05-03 08:00:00",
                    "Audit Drug",
                    "audit_drug",
                    "Audit Drug",
                    "medication",
                    "active",
                ),
            )
            order_id = int(cursor.lastrowid)
            cursor.execute("UPDATE orders SET status = 'held' WHERE id = ?", (order_id,))

        row = manager.fetch_one_remcard(
            """
            SELECT MAX(version) AS version
            FROM schema_migrations
            """
        )
        if not row or int(row["version"] or 0) < SCHEMA_MIN_MIGRATION_VERSION:
            return False, "medical audit migration did not advance schema_migrations"

        audit_rows = manager.fetch_all_remcard(
            """
            SELECT table_name, row_id, admission_id, action_type, changed_by, operation_id, before_json, after_json
            FROM medical_audit_log
            WHERE table_name = 'orders' AND row_id = ?
            ORDER BY id
            """,
            (order_id,),
        )
        actions = [dict(row)["action_type"] for row in audit_rows]
        if actions != ["insert", "update"]:
            return False, f"unexpected order audit actions: {actions}"

        update_row = dict(audit_rows[-1])
        if update_row.get("changed_by") != "doctor":
            return False, f"unexpected audit changed_by: {update_row.get('changed_by')}"
        if not update_row.get("operation_id"):
            return False, "medical audit operation_id is empty"
        if int(update_row.get("admission_id") or 0) != admission_id:
            return False, "medical audit admission_id mismatch"

        before_payload = json.loads(update_row["before_json"])
        after_payload = json.loads(update_row["after_json"])
        if before_payload.get("status") != "active" or after_payload.get("status") != "held":
            return False, f"medical audit before/after payload mismatch: {before_payload} -> {after_payload}"

        quick = manager.fetch_one_remcard("PRAGMA quick_check")
        if not quick or str(quick[0]).lower() != "ok":
            return False, f"quick_check failed after audit trigger writes: {quick}"
        return True, "ok"
    finally:
        manager.close()


def _check_doctor_create_card_avoids_open_snapshot_race(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    source_path = Path(__file__).resolve().parents[1] / "ui" / "doctor_view" / "doctor_remcard_widget.py"
    source_text = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text)

    class_defs = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DoctorRemCardWidget"]
    if not class_defs:
        return False, "DoctorRemCardWidget class not found"
    methods = {node.name: node for node in class_defs[0].body if isinstance(node, ast.FunctionDef)}

    load_method = methods.get("load_patient_card")
    if load_method is None:
        return False, "load_patient_card not found"
    load_source = ast.get_source_segment(source_text, load_method) or ""
    if "ow.set_context" not in load_source:
        return False, "load_patient_card must update OrdersWidget through set_context"
    request_snapshot_kw = [
        (arg, default)
        for arg, default in zip(load_method.args.kwonlyargs, load_method.args.kw_defaults)
        if arg.arg == "request_snapshot"
    ]
    if (
        not request_snapshot_kw
        or not isinstance(request_snapshot_kw[0][1], ast.Constant)
        or request_snapshot_kw[0][1].value is not True
    ):
        return False, "load_patient_card must accept request_snapshot=True keyword"

    select_method = methods.get("on_patient_selected_from_list")
    if select_method is None:
        return False, "on_patient_selected_from_list not found"
    create_branch_uses_deferred_snapshot = False
    for node in ast.walk(select_method):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "load_patient_card":
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == "request_snapshot"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is False
            ):
                create_branch_uses_deferred_snapshot = True
                break
    if not create_branch_uses_deferred_snapshot:
        return False, "create action should load patient card with request_snapshot=False"

    create_method = methods.get("on_create_card_clicked")
    if create_method is None:
        return False, "on_create_card_clicked not found"
    create_source = ast.get_source_segment(source_text, create_method) or ""
    if "_create_card_after_snapshot" not in create_source or ".isRunning()" not in create_source:
        return False, "create-card write is not deferred while snapshot worker is running"

    return True, "ok"


def _check_orders_widgets_defer_snapshot_reload_thread_creation(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    cases = [
        ("doctor", "ui/doctor_view/orders_widget.py", "OrdersWidget"),
        ("nurse", "ui/nurse_view/components/nurse_orders_widget.py", "NurseOrdersWidget"),
    ]
    root = Path(__file__).resolve().parents[1]
    for role, relative_path, class_name in cases:
        source_path = root / relative_path
        source_text = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source_text)
        class_defs = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name]
        if not class_defs:
            return False, f"{role}: {class_name} class not found"
        methods = {node.name: node for node in class_defs[0].body if isinstance(node, ast.FunctionDef)}
        for method_name in (
            "_request_snapshot",
            "_queue_forced_reload_after_stale_snapshot",
            "_on_snapshot_finished",
            "_defer_snapshot_request",
        ):
            if method_name not in methods:
                return False, f"{role}: {method_name} not found"

        request_source = ast.get_source_segment(source_text, methods["_request_snapshot"]) or ""
        if "self._snapshot_worker is not None" not in request_source:
            return False, f"{role}: snapshot worker must stay busy until finished signal"

        stale_source = ast.get_source_segment(source_text, methods["_queue_forced_reload_after_stale_snapshot"]) or ""
        if "_defer_snapshot_request" not in stale_source:
            return False, f"{role}: stale snapshot reload must be deferred"

        finished_source = ast.get_source_segment(source_text, methods["_on_snapshot_finished"]) or ""
        if "_defer_snapshot_request" not in finished_source:
            return False, f"{role}: pending reload after worker finish must be deferred"

        defer_source = ast.get_source_segment(source_text, methods["_defer_snapshot_request"]) or ""
        if "QTimer.singleShot" not in defer_source:
            return False, f"{role}: deferred reload helper must use QTimer.singleShot"

    return True, "ok"


def _check_targeted_async_workers_are_parentless_and_guarded(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    cases = [
        (
            "doctor_card",
            PROJECT_ROOT / "ui" / "doctor_view" / "doctor_remcard_widget.py",
            "DoctorRemCardWidget",
            "_request_card_snapshot",
            ("_apply_card_snapshot", "_on_card_snapshot_failed", "_on_card_snapshot_finished", "shutdown"),
        ),
        (
            "nurse_card",
            PROJECT_ROOT / "ui" / "nurse_view" / "nurse_main_widget.py",
            "NurseMainWidget",
            "_request_card_snapshot",
            ("_apply_card_snapshot", "_on_card_snapshot_failed", "_on_card_snapshot_finished", "shutdown"),
        ),
        (
            "doctor_orders",
            PROJECT_ROOT / "ui" / "doctor_view" / "orders_widget.py",
            "OrdersWidget",
            "_request_snapshot",
            ("_apply_snapshot", "_apply_snapshot_data", "_on_snapshot_failed", "_on_snapshot_finished", "shutdown"),
        ),
        (
            "nurse_orders",
            PROJECT_ROOT / "ui" / "nurse_view" / "components" / "nurse_orders_widget.py",
            "NurseOrdersWidget",
            "_request_snapshot",
            ("_apply_snapshot", "_apply_snapshot_data", "_on_snapshot_failed", "_on_snapshot_finished", "shutdown"),
        ),
        (
            "doctor_beds",
            PROJECT_ROOT / "ui" / "doctor_view" / "components" / "beds_selection_widget.py",
            "BedsSelectionWidget",
            "refresh",
            ("_apply_beds_snapshot", "_on_refresh_failed", "_on_refresh_finished", "shutdown"),
        ),
        (
            "doctor_bars_auth",
            PROJECT_ROOT / "ui" / "doctor_view" / "doctor_remcard_widget.py",
            "DoctorRemCardWidget",
            "_check_bars_auth_async",
            ("_on_bars_auth_check_succeeded", "_on_bars_auth_check_failed", "_on_bars_auth_check_finished", "shutdown"),
        ),
        (
            "nurse_beds",
            PROJECT_ROOT / "ui" / "nurse_view" / "components" / "nurse_beds_selection_widget.py",
            "NurseBedsSelectionWidget",
            "refresh",
            ("_apply_beds_snapshot", "_on_refresh_failed", "_on_refresh_finished", "shutdown"),
        ),
    ]

    def _async_call_uses_parent_self(method: ast.FunctionDef) -> bool:
        for node in ast.walk(method):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            func_name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if func_name != "AsyncCallThread":
                continue
            for keyword in node.keywords:
                if keyword.arg == "parent" and isinstance(keyword.value, ast.Name) and keyword.value.id == "self":
                    return True
        return False

    for role, path, class_name, request_method_name, guarded_method_names in cases:
        source_text = path.read_text(encoding="utf-8")
        tree = ast.parse(source_text)
        class_defs = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name]
        if not class_defs:
            return False, f"{role}: {class_name} class not found"
        methods = {node.name: node for node in class_defs[0].body if isinstance(node, ast.FunctionDef)}
        request_method = methods.get(request_method_name)
        if request_method is None:
            return False, f"{role}: {request_method_name} not found"
        request_source = ast.get_source_segment(source_text, request_method) or ""
        if "AsyncCallThread" not in request_source:
            return False, f"{role}: request method does not start AsyncCallThread"
        if _async_call_uses_parent_self(request_method):
            return False, f"{role}: snapshot worker still uses Qt parent=self"
        if "_is_closing" not in request_source:
            return False, f"{role}: request method must guard _is_closing"

        for method_name in guarded_method_names:
            method = methods.get(method_name)
            if method is None:
                return False, f"{role}: {method_name} not found"
            method_source = ast.get_source_segment(source_text, method) or ""
            if "_is_closing" not in method_source:
                return False, f"{role}: {method_name} must guard _is_closing"

        shutdown_source = ast.get_source_segment(source_text, methods["shutdown"]) or ""
        helper_source = ""
        helper = methods.get("_shutdown_snapshot_worker")
        if helper is not None:
            helper_source = ast.get_source_segment(source_text, helper) or ""
        lifecycle_source = shutdown_source + "\n" + helper_source
        if "disconnect" not in lifecycle_source or ".wait(" not in lifecycle_source:
            return False, f"{role}: shutdown must disconnect and wait active snapshot workers"
        if role.endswith("_card") and "clear_drafts()" in shutdown_source:
            return False, f"{role}: shutdown must not enqueue clear_drafts during app close"

    return True, "ok"


def _check_patient_form_open_is_deferred_from_callback(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    path = PROJECT_ROOT / "ui" / "patient_bed_management" / "management_widget.py"
    source_text = path.read_text(encoding="utf-8")
    tree = ast.parse(source_text)
    class_defs = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PatientBedManagementWidget"
    ]
    if not class_defs:
        return False, "PatientBedManagementWidget class not found"
    methods = {node.name: node for node in class_defs[0].body if isinstance(node, ast.FunctionDef)}
    open_method = methods.get("_open_patient_card_by_number")
    safe_method = methods.get("_open_patient_form_safe")
    if open_method is None or safe_method is None:
        return False, "deferred patient form helpers not found"
    open_source = ast.get_source_segment(source_text, open_method) or ""
    safe_source = ast.get_source_segment(source_text, safe_method) or ""
    if "QTimer.singleShot" not in open_source:
        return False, "PatientForm opening must be deferred with QTimer.singleShot"
    if "dialog.exec" in open_source:
        return False, "PatientForm.dialog.exec must not run in the original callback"
    if "dialog.exec" not in safe_source:
        return False, "deferred helper must still open PatientForm"
    for guard in ("_opening_patient_form", "_is_closing"):
        if guard not in open_source + safe_source:
            return False, f"PatientForm deferred open missing {guard} guard"
    return True, "ok"


def _check_shutdown_queue_db_ordering_guards(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    data_service_text = (PROJECT_ROOT / "services" / "data_service.py").read_text(encoding="utf-8")
    for marker in (
        "_shutting_down",
        "set_shutting_down",
        "Queued write rejected during shutdown",
        "return False",
    ):
        if marker not in data_service_text:
            return False, f"DataService missing shutdown guard marker: {marker}"

    main_window_text = (PROJECT_ROOT / "ui" / "main_window.py").read_text(encoding="utf-8")
    main_tree = ast.parse(main_window_text)
    main_classes = [node for node in main_tree.body if isinstance(node, ast.ClassDef) and node.name == "MainWindow"]
    if not main_classes:
        return False, "MainWindow class not found"
    main_methods = {node.name: node for node in main_classes[0].body if isinstance(node, ast.FunctionDef)}
    close_method = main_methods.get("closeEvent")
    if close_method is None:
        return False, "MainWindow.closeEvent not found"
    close_source = ast.get_source_segment(main_window_text, close_method) or ""
    if "set_shutting_down" not in close_source:
        return False, "MainWindow.closeEvent must mark DataService shutting down before UI shutdown"
    if "queue_drained" not in close_source or "db_manager.close()" not in close_source:
        return False, "MainWindow.closeEvent must gate DB close on queue drain"
    if "clear_drafts()" in close_source:
        return False, "MainWindow.closeEvent must not enqueue clear_drafts during shutdown"

    sqlite_text = (PROJECT_ROOT / "app" / "sqlite_shared.py").read_text(encoding="utf-8")
    for marker in ("DatabaseClosedError", "conn is None", "def shutdown(self, timeout: float = 1.0) -> bool"):
        if marker not in sqlite_text:
            return False, f"sqlite_shared missing controlled shutdown marker: {marker}"

    db_text = (PROJECT_ROOT / "data" / "dao" / "db_manager.py").read_text(encoding="utf-8")
    if "DatabaseClosedError" not in db_text or "self._closed or self._remcard_conn is None" not in db_text:
        return False, "DatabaseManager must raise controlled DatabaseClosedError after close"
    return True, "ok"


def _check_orders_fast_click_path_stays_local(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    root = Path(__file__).resolve().parents[1]
    source_path = root / "ui/doctor_view/orders_widget.py"
    source_text = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text)
    class_defs = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "OrdersWidget"]
    if not class_defs:
        return False, "doctor: OrdersWidget class not found"

    methods = {node.name: node for node in class_defs[0].body if isinstance(node, ast.FunctionDef)}
    for method_name in ("_enqueue_cell_write", "_emit_admin_cell_changes"):
        if method_name not in methods:
            return False, f"doctor: {method_name} not found"

    enqueue_source = ast.get_source_segment(source_text, methods["_enqueue_cell_write"]) or ""
    if "_defer_snapshot_request" in enqueue_source or "_request_snapshot" in enqueue_source:
        return False, "doctor: cell write success must not start an immediate orders snapshot"
    if "_schedule_fast_sync" not in enqueue_source:
        return False, "doctor: cell write success must debounce the quiet orders sync"
    if "_schedule_state_sync" not in enqueue_source:
        return False, "doctor: cell write success must keep state buttons in sync"

    emit_source = ast.get_source_segment(source_text, methods["_emit_admin_cell_changes"]) or ""
    if ".viewport().update(" in emit_source or "viewport().update()" in emit_source:
        return False, "doctor: local cell changes must not repaint the whole orders viewport"

    delegate_path = root / "ui/shared/orders_delegate.py"
    delegate_text = delegate_path.read_text(encoding="utf-8")
    delegate_tree = ast.parse(delegate_text)
    delegate_classes = [
        node
        for node in delegate_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "OrdersDelegate"
    ]
    if not delegate_classes:
        return False, "shared: OrdersDelegate class not found"
    delegate_methods = {node.name: node for node in delegate_classes[0].body if isinstance(node, ast.FunctionDef)}
    if "_is_admin_pending" not in delegate_methods:
        return False, "shared: OrdersDelegate._is_admin_pending not found"
    pending_source = ast.get_source_segment(delegate_text, delegate_methods["_is_admin_pending"]) or ""
    if "_pending_cell_action" in pending_source:
        return False, "shared: ordinary planned X must not be drawn as pending"

    return True, "ok"


def _check_performance_a_guards_present(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    root = Path(__file__).resolve().parents[1]

    doctor_path = root / "ui/doctor_view/doctor_remcard_widget.py"
    doctor_text = doctor_path.read_text(encoding="utf-8")
    doctor_tree = ast.parse(doctor_text)
    doctor_classes = [
        node
        for node in doctor_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "DoctorRemCardWidget"
    ]
    if not doctor_classes:
        return False, "DoctorRemCardWidget class not found"
    doctor_methods = {node.name: node for node in doctor_classes[0].body if isinstance(node, ast.FunctionDef)}
    readonly_method = doctor_methods.get("_apply_archive_read_only_state")
    if readonly_method is None:
        return False, "DoctorRemCardWidget._apply_archive_read_only_state not found"
    readonly_source = ast.get_source_segment(doctor_text, readonly_method) or ""
    if "_read_only_widget_signature" not in doctor_text or "apply_widget_state" not in readonly_source:
        return False, "doctor read-only state must be idempotent for child widgets"
    if "self.controls" not in readonly_source or "set_save_active" not in readonly_source:
        return False, "doctor read-only guard must keep controls refresh outside the child-widget skip"

    orders_path = root / "ui/doctor_view/orders_widget.py"
    orders_text = orders_path.read_text(encoding="utf-8")
    orders_tree = ast.parse(orders_text)
    orders_classes = [
        node
        for node in orders_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "OrdersWidget"
    ]
    if not orders_classes:
        return False, "OrdersWidget class not found"
    orders_methods = {node.name: node for node in orders_classes[0].body if isinstance(node, ast.FunctionDef)}
    guard_method = orders_methods.get("_known_current_context_without_drafts")
    source_probe_method = orders_methods.get("_source_has_order_drafts")
    clear_method = orders_methods.get("clear_drafts")
    if guard_method is None or source_probe_method is None or clear_method is None:
        return False, "orders clear-drafts guard methods are missing"
    guard_source = ast.get_source_segment(orders_text, guard_method) or ""
    source_probe_source = ast.get_source_segment(orders_text, source_probe_method) or ""
    clear_source = ast.get_source_segment(orders_text, clear_method) or ""
    if "self.model.admission_id != self.admission_id" not in guard_source:
        return False, "clear-drafts guard must not skip when model context is different"
    if "_last_applied_snapshot_signature" not in guard_source:
        return False, "clear-drafts guard must require a known loaded snapshot/local model"
    if "has_order_drafts" not in source_probe_source:
        return False, "clear-drafts guard must use a cheap source draft probe when local state is unknown"
    if "_known_current_context_without_drafts" not in clear_source:
        return False, "clear_drafts must skip known no-op clears"
    if "_source_has_order_drafts" not in clear_source:
        return False, "clear_drafts must skip source-confirmed no-op clears"

    diet_path = root / "ui/shared/components/diet_intake_widget.py"
    diet_text = diet_path.read_text(encoding="utf-8")
    diet_tree = ast.parse(diet_text)
    diet_classes = [
        node
        for node in diet_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "DietIntakeWidget"
    ]
    if not diet_classes:
        return False, "DietIntakeWidget class not found"
    diet_methods = {node.name: node for node in diet_classes[0].body if isinstance(node, ast.FunctionDef)}
    set_read_only = ast.get_source_segment(diet_text, diet_methods.get("set_read_only")) if diet_methods.get("set_read_only") else ""
    if "self.read_only == bool(read_only)" not in (set_read_only or ""):
        return False, "DietIntakeWidget.set_read_only must skip unchanged state"

    return True, "ok"


def _check_report_pdf_callbacks_are_qobject_slots(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    cases = [
        ("doctor", "ui/doctor_view/components/beds_selection_widget.py", "BedsSelectionWidget"),
        ("nurse", "ui/nurse_view/components/nurse_beds_selection_widget.py", "NurseBedsSelectionWidget"),
        ("shared", "ui/shared/report_controller.py", "RemCardReportController"),
    ]
    root = Path(__file__).resolve().parents[1]
    for role, relative_path, class_name in cases:
        source_path = root / relative_path
        source_text = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source_text)
        class_defs = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name]
        if not class_defs:
            return False, f"{role}: {class_name} class not found"
        if class_name == "RemCardReportController":
            base_names = [
                base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
                for base in class_defs[0].bases
            ]
            if "QObject" not in base_names:
                return False, "shared: RemCardReportController must inherit QObject for queued report callbacks"
        methods = {node.name: node for node in class_defs[0].body if isinstance(node, ast.FunctionDef)}

        required_slots = {
            "_on_daily_report_collected": "dict",
            "_on_daily_report_error": "str",
            "_on_full_report_collected": "list",
            "_on_full_report_error": "str",
        }
        def has_slot_decorator(method: ast.FunctionDef, slot_arg: str) -> bool:
            for decorator in method.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                if not isinstance(decorator.func, ast.Name) or decorator.func.id != "Slot":
                    continue
                if not decorator.args:
                    continue
                arg = decorator.args[0]
                if isinstance(arg, ast.Name) and arg.id == slot_arg:
                    return True
            return False

        for method_name, slot_arg in required_slots.items():
            method = methods.get(method_name)
            if method is None:
                return False, f"{role}: {method_name} not found"
            if not has_slot_decorator(method, slot_arg):
                return False, f"{role}: {method_name} must be a Qt Slot({slot_arg})"

        daily_method_name = "run_daily_report" if class_name == "RemCardReportController" else "on_daily_report_requested"
        full_method_name = "run_full_report" if class_name == "RemCardReportController" else "on_full_report_requested"
        daily_method = methods.get(daily_method_name)
        full_method = methods.get(full_method_name)
        if daily_method is None or full_method is None:
            return False, f"{role}: report request methods not found"
        daily_source = ast.get_source_segment(source_text, daily_method) or ""
        full_source = ast.get_source_segment(source_text, full_method) or ""
        if "def on_finished" in daily_source or "def on_error" in daily_source:
            return False, f"{role}: daily report must not use nested callbacks"
        if "def on_finished" in full_source or "def on_error" in full_source:
            return False, f"{role}: full report must not use nested callbacks"
        if "finished.connect(self._on_daily_report_collected)" not in daily_source:
            return False, f"{role}: daily report must connect to QObject slot"
        if "error.connect(self._on_daily_report_error)" not in daily_source:
            return False, f"{role}: daily report error must connect to QObject slot"
        if "finished.connect(self._on_full_report_collected)" not in full_source:
            return False, f"{role}: full report must connect to QObject slot"
        if "error.connect(self._on_full_report_error)" not in full_source:
            return False, f"{role}: full report error must connect to QObject slot"

    return True, "ok"


def _check_pdf_build_runs_in_worker(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    worker_source = (PROJECT_ROOT / "ui" / "shared" / "pdf_build_worker.py").read_text(encoding="utf-8")
    if "class PdfBuildWorker(QThread)" not in worker_source or "ReportBuilder.build_pdf" not in worker_source:
        return False, "PdfBuildWorker must own ReportBuilder.build_pdf"

    checked_methods = {
        "ui/shared/report_controller.py": [
            "_on_daily_report_collected",
            "_on_full_report_collected",
        ],
        "ui/doctor_view/components/beds_selection_widget.py": [
            "_on_daily_report_collected",
            "_on_full_report_collected",
        ],
        "ui/nurse_view/components/nurse_beds_selection_widget.py": [
            "_on_daily_report_collected",
            "_on_full_report_collected",
        ],
        "ui/rem_card_sectors/sector_print.py": [
            "on_data_collected",
            "on_full_data_collected",
        ],
        "ui/nurse_view/sectors/nurse_sector_print.py": [
            "on_data",
            "on_full",
        ],
    }
    for relative_path, method_names in checked_methods.items():
        source_path = PROJECT_ROOT / relative_path
        source_text = source_path.read_text(encoding="utf-8")
        if "PdfBuildWorker" not in source_text or "pdf_worker" not in source_text:
            return False, f"{relative_path}: PdfBuildWorker is not retained by the widget"
        tree = ast.parse(source_text)
        methods = {
            node.name: ast.get_source_segment(source_text, node) or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        for method_name in method_names:
            method_source = methods.get(method_name, "")
            if not method_source:
                return False, f"{relative_path}: {method_name} not found"
            if "ReportBuilder.build_pdf" in method_source:
                return False, f"{relative_path}: {method_name} still builds PDF in UI callback"
            if "_start" not in method_source:
                return False, f"{relative_path}: {method_name} does not delegate PDF build"
    return True, "ok"


def _check_w1_yesterday_card_skips_status_write_and_defers(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    source_path = Path(__file__).resolve().parents[1] / "ui" / "doctor_view" / "doctor_remcard_widget.py"
    source_text = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text)

    class_defs = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DoctorRemCardWidget"]
    if not class_defs:
        return False, "DoctorRemCardWidget class not found"
    methods = {node.name: node for node in class_defs[0].body if isinstance(node, ast.FunctionDef)}

    load_method = methods.get("load_patient_card")
    if load_method is None:
        return False, "load_patient_card not found"
    ensure_kw = [
        (arg, default)
        for arg, default in zip(load_method.args.kwonlyargs, load_method.args.kw_defaults)
        if arg.arg == "ensure_initial_status"
    ]
    if not ensure_kw or not isinstance(ensure_kw[0][1], ast.Constant) or ensure_kw[0][1].value is not None:
        return False, "load_patient_card must accept ensure_initial_status=None keyword"

    yest_clicked_source = ast.get_source_segment(source_text, methods.get("on_yest_card_clicked")) or ""
    if "QTimer.singleShot" not in yest_clicked_source or "safe_load_archived_card" not in yest_clicked_source:
        return False, "open-card yesterday action must defer archive loading through QTimer.singleShot"

    select_source = ast.get_source_segment(source_text, methods.get("on_patient_selected_from_list")) or ""
    if "QTimer.singleShot" not in select_source or "_open_w1_yesterday_card" not in select_source:
        return False, "W1 yesterday action must defer loading through QTimer.singleShot"

    open_w1_source = ast.get_source_segment(source_text, methods.get("_open_w1_yesterday_card")) or ""
    if "ensure_initial_status=False" not in open_w1_source:
        return False, "W1 yesterday card must skip initial status writes"

    archive_source = ast.get_source_segment(source_text, methods.get("safe_load_archived_card")) or ""
    if "current_start <= selected_date < current_end" not in archive_source:
        return False, "safe_load_archived_card must only write initial status for the current card day"
    if "skip initial status write for historical card" not in archive_source:
        return False, "safe_load_archived_card must log skipped historical status writes"

    return True, "ok"


def _check_chart_clears_on_card_context_change(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    root = Path(__file__).resolve().parents[1]

    chart_source = (root / "ui" / "shared" / "chart_widget.py").read_text(encoding="utf-8")
    if "def clear_for_context" not in chart_source:
        return False, "ChartWidget.clear_for_context not found"
    if "self.scatter_vitals.setData([])" not in chart_source:
        return False, "ChartWidget.clear_for_context must clear previous vital markers"

    cases = [
        ("doctor", "ui/doctor_view/doctor_remcard_widget.py", "DoctorRemCardWidget"),
        ("nurse", "ui/nurse_view/nurse_main_widget.py", "NurseMainWidget"),
    ]
    for role, relative_path, class_name in cases:
        source_path = root / relative_path
        source_text = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source_text)
        class_defs = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name]
        if not class_defs:
            return False, f"{role}: {class_name} class not found"
        methods = {node.name: node for node in class_defs[0].body if isinstance(node, ast.FunctionDef)}
        load_method = methods.get("load_patient_card")
        if load_method is None:
            return False, f"{role}: load_patient_card not found"
        load_source = ast.get_source_segment(source_text, load_method) or ""
        if "clear_for_context" not in load_source:
            return False, f"{role}: chart must be cleared immediately on patient card switch"
        if role == "doctor":
            match_pos = load_source.find("chart_matches_target = self._chart_matches_context")
            assign_pos = load_source.find("self.chart.admission_id = admission_id")
            if match_pos < 0 or assign_pos < 0 or match_pos > assign_pos:
                return False, "doctor: chart context must be checked before assigning the new admission_id"

    return True, "ok"


def _check_journal_prewarm_is_opt_in(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    root = Path(__file__).resolve().parents[1]
    cases = [
        ("doctor", root / "ui" / "doctor_view" / "doctor_remcard_widget.py"),
        ("nurse", root / "ui" / "nurse_view" / "nurse_main_widget.py"),
    ]
    for role, source_path in cases:
        source = source_path.read_text(encoding="utf-8")
        if 'JOURNAL_PREWARM_ENABLED = os.environ.get("REMCARD_JOURNAL_PREWARM", "0") == "1"' not in source:
            return False, f"{role}: journal prewarm must be disabled by default"
        if 'JOURNAL_WIDGET_PREWARM_ENABLED = os.environ.get("REMCARD_JOURNAL_WIDGET_PREWARM", "0") == "1"' not in source:
            return False, f"{role}: journal widget prewarm must be disabled by default"
        if "if JOURNAL_PREWARM_ENABLED:" not in source:
            return False, f"{role}: startup journal prewarm timer must be gated"

    return True, "ok"


def _check_w1_beds_refreshes_on_vitals_change(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    root = Path(__file__).resolve().parents[1]
    cases = [
        ("doctor", root / "ui" / "doctor_view" / "doctor_main_widget.py"),
        ("nurse", root / "ui" / "nurse_view" / "nurse_main_widget.py"),
    ]
    required_entities = {
        "vitals",
        "vital_settings",
        "patient_status_events",
        "fluids",
        "orders",
        "administrations",
    }
    for role, source_path in cases:
        source = source_path.read_text(encoding="utf-8")
        if "W1_REFRESH_ENTITIES" not in source:
            return False, f"{role}: W1 refresh entity set not found"
        if "queue_if_running=False" not in source:
            return False, f"{role}: startup W1 refresh should not queue a duplicate refresh"
        missing = [entity for entity in required_entities if f'"{entity}"' not in source]
        if missing:
            return False, f"{role}: W1 refresh entities missing {missing}"
        tree = ast.parse(source)
        methods = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_on_data_changes"
        ]
        if not methods:
            return False, f"{role}: _on_data_changes not found"
        method_source = ast.get_source_segment(source, methods[0]) or ""
        if "W1_REFRESH_ENTITIES" not in method_source:
            return False, f"{role}: W1 beds refresh must use W1_REFRESH_ENTITIES"

    widget_cases = [
        ("doctor", root / "ui" / "doctor_view" / "components" / "beds_selection_widget.py"),
        ("nurse", root / "ui" / "nurse_view" / "components" / "nurse_beds_selection_widget.py"),
    ]
    for role, source_path in widget_cases:
        source = source_path.read_text(encoding="utf-8")
        if "def refresh(self, *, queue_if_running: bool = True)" not in source:
            return False, f"{role}: W1 refresh must support non-queued startup refresh"
        if "if queue_if_running:" not in source:
            return False, f"{role}: W1 refresh must respect queue_if_running"

    return True, "ok"


def _check_w1_outcome_timer_ticks_without_beds_refresh(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from datetime import datetime

    from PySide6.QtWidgets import QApplication

    from rem_card.data.dto.remcard_dto import PatientStatus, PatientStatusEventDTO
    from rem_card.ui.rem_card_sectors.sector_4_sub import Sector4b

    app = QApplication.instance() or QApplication([])
    widget = Sector4b()
    status = PatientStatusEventDTO(
        admission_id=1,
        status=PatientStatus.TRANSFERRED,
        start_time=datetime.now(),
    )
    try:
        widget.show()
        app.processEvents()
        widget.update_status(status)
        widget.update_outcome_timer(status, delay_minutes=1)
        first_text = widget.lbl_outcome_timer.text()
        if widget.lbl_outcome_timer.isHidden():
            return False, "outcome timer label is hidden"
        if not widget._outcome_tick_timer.isActive():
            return False, "outcome timer QTimer is not active"

        deadline = time.monotonic() + 1.6
        changed = False
        while time.monotonic() < deadline:
            app.processEvents()
            if widget.lbl_outcome_timer.text() != first_text:
                changed = True
                break
            time.sleep(0.05)
        if not changed:
            return False, "outcome timer text did not tick without beds refresh"
        return True, "ok"
    finally:
        widget.close()


def _check_w1_outcome_release_runs_from_change_monitor(temp_root: str) -> tuple[bool, str]:
    _ = temp_root

    from rem_card.services.data_update_monitor import DataUpdateMonitor

    class FakeDataService:
        def __init__(self):
            self.calls = []
            self.current_change_id = 1

        def run_poll_maintenance_tasks(self):
            self.calls.append("maintenance")
            self.current_change_id = 2

        def get_latest_change_id(self):
            self.calls.append("latest")
            return self.current_change_id

        def fetch_changes_since(self, last_change_id):
            self.calls.append(("fetch", int(last_change_id)))
            return [
                {
                    "id": 2,
                    "entity_name": "beds",
                    "entity_id": 1,
                    "admission_id": 7,
                    "action": "update",
                    "changed_at": "2026-05-05 08:04:00.000",
                    "changed_by": "journal",
                    "version": 2,
                }
            ]

    service = FakeDataService()
    monitor = DataUpdateMonitor(service)
    monitor._last_seen_id = 1
    monitor._poll_once(force_emit=False, force_sources=[])
    if service.calls[:2] != ["maintenance", "latest"]:
        return False, f"maintenance must run before change cursor read, calls={service.calls}"
    if ("fetch", 1) not in service.calls:
        return False, f"change monitor did not fetch release changes after maintenance, calls={service.calls}"

    root = Path(__file__).resolve().parents[1]
    bootstrap_source = (root / "app" / "bootstrap.py").read_text(encoding="utf-8")
    if "add_poll_maintenance_task(self.remcard_service.maybe_release_due_outcome_beds)" not in bootstrap_source:
        return False, "bootstrap must register outcome auto-release as a data monitor maintenance task"
    facade_source = (root / "services" / "remcard_facade.py").read_text(encoding="utf-8")
    if "PatientService(patient_dao, data_service=data_service)" not in facade_source:
        return False, "RemCardService patient helper must receive DataService for coordinated releases"

    return True, "ok"


def _check_build_release_reuses_prepared_version(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    root = Path(__file__).resolve().parents[1]
    source = (root / "scripts" / "build_release.py").read_text(encoding="utf-8")
    required = [
        "release_files_already_prepared",
        "previous_release_commit == current_head",
        "версия уже подготовлена",
        "собираю текущий релиз без поднятия версии",
        "push_current_branch(root)",
    ]
    missing = [item for item in required if item not in source]
    if missing:
        return False, f"build_release prepared-version flow missing {missing}"
    return True, "ok"


def _check_patient_card_cache_lru_10(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from datetime import datetime

    from rem_card.services.read_coordinator import READ_CACHE_MAX_PATIENTS, ReadCoordinator

    if READ_CACHE_MAX_PATIENTS != 10:
        return False, f"expected default card cache size 10, got {READ_CACHE_MAX_PATIENTS}"

    class FakeRemCardService:
        def __init__(self):
            self.build_calls = 0
            self.versions = {}

        def get_latest_change_id(self, admission_id=None, include_global=True):
            _ = include_global
            return int(self.versions.get(int(admission_id or 0), 1))

        def build_full_card_snapshot(self, admission_id, shift_date, **kwargs):
            self.build_calls += 1
            _ = kwargs
            return {
                "admission_id": int(admission_id),
                "shift_date": shift_date,
                "start_dt": shift_date,
                "end_dt": shift_date,
                "vitals": [],
                "vitals_extended": [],
                "fluids": [],
                "effective_bounds": (shift_date, shift_date),
                "balance_runtime": {"orders": [], "start_dt": shift_date, "end_dt": shift_date},
                "change_id": int(self.versions.get(int(admission_id), 1)),
            }

        def build_vitals_snapshot(self, admission_id, shift_date, **kwargs):
            self.build_calls += 1
            _ = kwargs
            return {
                "admission_id": int(admission_id),
                "shift_date": shift_date,
                "start_dt": shift_date,
                "end_dt": shift_date,
                "vitals": [{"pulse": int(admission_id)}],
                "vitals_extended": [],
                "latest_values": {"pulse": int(admission_id)},
                "effective_bounds": (shift_date, shift_date),
                "change_id": int(self.versions.get(int(admission_id), 1)),
            }

    service = FakeRemCardService()
    coordinator = ReadCoordinator(service)
    shift_date = datetime(2026, 5, 3, 8, 0, 0)

    def card_key(admission_id: int):
        context = coordinator.make_patient_snapshot_context(
            source_db="live",
            admission_id=admission_id,
            shift_date=shift_date,
            role="doctor",
            mode="live",
            variant="card_full",
        )
        return context.cache_key()

    def card_key_at(admission_id: int, dt: datetime):
        context = coordinator.make_patient_snapshot_context(
            source_db="live",
            admission_id=admission_id,
            shift_date=dt,
            role="doctor",
            mode="live",
            variant="card_full",
        )
        return context.cache_key()

    def vitals_key(admission_id: int):
        context = coordinator.make_patient_snapshot_context(
            source_db="live",
            admission_id=admission_id,
            shift_date=shift_date,
            role="doctor",
            mode="live",
            variant="vitals",
        )
        return context.cache_key()

    for admission_id in range(1, 11):
        coordinator.load_patient_card_snapshot(
            admission_id,
            shift_date,
            role="doctor",
            force_refresh=False,
        )

    if len(coordinator._patient_card_cache) != 10:
        return False, f"card cache should hold 10 entries, got {len(coordinator._patient_card_cache)}"
    if coordinator.get_cached_card(card_key(1)) is None:
        return False, "patient 1 card cache missing before LRU overflow"

    coordinator.load_patient_card_snapshot(11, shift_date, role="doctor", force_refresh=False)
    if coordinator.get_cached_card(card_key(1)) is None:
        return False, "recently used patient 1 was evicted instead of oldest entry"
    if card_key(2) in coordinator._patient_card_cache:
        return False, "oldest patient 2 memory cache survived after 11th context"

    same_shift_times = [
        datetime(2026, 5, 3, 8, 0, 0),
        datetime(2026, 5, 3, 9, 15, 30),
        datetime(2026, 5, 3, 13, 40, 10),
        datetime(2026, 5, 3, 23, 59, 59),
        datetime(2026, 5, 4, 2, 30, 0),
        datetime(2026, 5, 4, 7, 59, 59),
    ]
    same_shift_keys = {card_key_at(1, dt) for dt in same_shift_times}
    if len(same_shift_keys) != 1:
        return False, f"same medical shift produced time-dependent card cache keys: {same_shift_keys}"
    if card_key_at(1, datetime(2026, 5, 4, 8, 0, 0)) in same_shift_keys:
        return False, "next medical shift reused previous card cache key"

    restarted_coordinator = ReadCoordinator(service)
    persisted_after_restart = restarted_coordinator.get_cached_card(card_key(2))
    if persisted_after_restart is None:
        return False, "patient card persistent cache was not restored after coordinator restart"
    if int(persisted_after_restart.get("admission_id") or 0) != 2:
        return False, f"unexpected restored admission_id: {persisted_after_restart.get('admission_id')}"

    coordinator.load_patient_vitals_snapshot(3, shift_date, role="doctor", force_refresh=False)
    restarted_vitals = ReadCoordinator(service)
    persisted_vitals = restarted_vitals.get_cached_vitals(vitals_key(3))
    if persisted_vitals is None:
        return False, "patient vitals persistent cache was not restored after coordinator restart"
    if persisted_vitals.get("latest_values", {}).get("pulse") != 3:
        return False, f"unexpected restored vitals snapshot: {persisted_vitals}"

    service.versions[1] = 2
    if coordinator.get_current_cached_card(card_key(1)) is not None:
        return False, "stale patient 1 card cache was treated as current"
    if coordinator.get_cached_card(card_key(1)) is None:
        return False, "stale patient 1 card cache was removed instead of preserved for SWR"
    refreshed = coordinator.load_patient_card_snapshot(1, shift_date, role="doctor", force_refresh=False)
    if int(refreshed.get("version") or 0) != 2:
        return False, f"patient 1 card cache did not refresh to version 2: {refreshed.get('version')}"

    return True, "ok"


def _check_read_coordinator_partial_snapshots(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from datetime import datetime

    from rem_card.services.read_coordinator import ReadCoordinator

    class FakeRemCardService:
        def __init__(self):
            self.versions = {}
            self.calls = []
            self.full_calls = 0

        def get_latest_change_id(self, admission_id=None, include_global=True):
            _ = include_global
            return int(self.versions.get(int(admission_id or 0), 1))

        def build_full_card_snapshot(self, *args, **kwargs):
            _ = args, kwargs
            self.full_calls += 1
            raise AssertionError("partial snapshots must not call full card snapshot")

        def _base(self, scope, admission_id, shift_date):
            self.calls.append(scope)
            return {
                "admission_id": int(admission_id),
                "shift_date": shift_date,
                "change_id": int(self.versions.get(int(admission_id or 0), 1)),
            }

        def build_balance_snapshot(self, admission_id, shift_date, **kwargs):
            _ = kwargs
            snapshot = self._base("balance", admission_id, shift_date)
            snapshot["fluids"] = [{"amount": 10}]
            snapshot["balance_runtime"] = {"orders": [], "start_dt": shift_date, "end_dt": shift_date}
            return snapshot

        def build_diet_snapshot(self, admission_id, shift_date, **kwargs):
            _ = kwargs
            snapshot = self._base("diet", admission_id, shift_date)
            snapshot["events"] = [{"amount_ml": 150}]
            snapshot["totals"] = {"daily": 150}
            return snapshot

        def build_patient_header_snapshot(self, admission_id, shift_date, **kwargs):
            _ = kwargs
            snapshot = self._base("patient_header", admission_id, shift_date)
            snapshot["patient"] = {"name": "test"}
            snapshot["status"] = {"status": "active"}
            return snapshot

        def build_status_snapshot(self, admission_id, shift_date, **kwargs):
            _ = kwargs
            snapshot = self._base("status", admission_id, shift_date)
            snapshot["status"] = {"status": "active"}
            snapshot["active_intervals"] = []
            return snapshot

        def build_ivl_snapshot(self, admission_id, shift_date, **kwargs):
            _ = kwargs
            snapshot = self._base("ivl", admission_id, shift_date)
            snapshot["summary"] = {"active_case": None}
            snapshot["timeline"] = []
            return snapshot

        def build_beds_snapshot(self, reference_dt=None, **kwargs):
            _ = kwargs
            dt = reference_dt or datetime(2026, 5, 3, 8)
            snapshot = self._base("beds", 0, dt)
            snapshot["patients"] = [{"id": 1}]
            snapshot["runtime_snapshot"] = {1: {"card_exists": True}}
            return snapshot

    service = FakeRemCardService()
    coordinator = ReadCoordinator(service)
    shift_date = datetime(2026, 5, 3, 9, 15)

    balance = coordinator.load_balance_snapshot(1, shift_date, role="doctor", force_refresh=True)
    if balance.get("scope") != "balance" or balance.get("tab_name") != "balance":
        return False, f"balance snapshot scope mismatch: {balance}"
    if balance.get("dedup_signature", (None,))[0:3] != (1, "balance", 1):
        return False, f"balance dedup signature mismatch: {balance.get('dedup_signature')}"
    if not balance.get("content_hash") or balance.get("dedup_signature")[3] != balance.get("content_hash"):
        return False, "balance snapshot content_hash is not part of dedup signature"

    same_balance = coordinator.load_balance_snapshot(1, shift_date, role="doctor", force_refresh=True)
    if same_balance.get("dedup_signature") != balance.get("dedup_signature"):
        return False, "same partial content produced different dedup signature"
    if same_balance.get("load_trace_id") == balance.get("load_trace_id"):
        return False, "trace ids should stay diagnostic, not dedup keys"

    balance_context = coordinator.make_patient_snapshot_context(
        source_db="live",
        admission_id=1,
        shift_date=shift_date,
        role="doctor",
        mode="live",
        variant="balance_full",
    )
    if coordinator.get_current_cached_patient_scope(balance_context.cache_key()) is None:
        return False, "fresh patient scope cache was not treated as current"
    service.versions[1] = 2
    if coordinator.get_current_cached_patient_scope(balance_context.cache_key()) is not None:
        return False, "stale patient scope cache was treated as current"
    if coordinator.get_cached_patient_scope(balance_context.cache_key()) is None:
        return False, "stale patient scope cache was not preserved for SWR"
    refreshed = coordinator.load_balance_snapshot(1, shift_date, role="doctor", force_refresh=False)
    if int(refreshed.get("version") or 0) != 2:
        return False, f"stale partial snapshot did not refresh to version 2: {refreshed.get('version')}"

    coordinator.load_diet_snapshot(1, shift_date, role="doctor", force_refresh=True)
    coordinator.load_patient_header_snapshot(1, shift_date, role="doctor", force_refresh=True)
    coordinator.load_status_snapshot(1, shift_date, role="doctor", force_refresh=True)
    coordinator.load_ivl_snapshot(1, shift_date, role="doctor", force_refresh=True)
    beds = coordinator.load_beds_snapshot(shift_date, role="nurse", force_refresh=True)
    required_calls = {"balance", "diet", "patient_header", "status", "ivl", "beds"}
    if not required_calls.issubset(set(service.calls)):
        return False, f"missing partial snapshot builders: calls={service.calls}"
    if service.full_calls:
        return False, f"partial snapshots called full snapshot {service.full_calls} times"
    if beds.get("dedup_signature", (None,))[0:3] != (0, "beds", 1):
        return False, f"beds dedup signature mismatch: {beds.get('dedup_signature')}"

    source = (PROJECT_ROOT / "services" / "read_coordinator.py").read_text(encoding="utf-8")
    if "id(snapshot)" in source:
        return False, "snapshot identity must not be used for dedup"
    if "dedup_signature" not in source or "content_hash" not in source:
        return False, "read coordinator missing content-based dedup fields"

    for widget_path in (
        PROJECT_ROOT / "ui" / "doctor_view" / "doctor_remcard_widget.py",
        PROJECT_ROOT / "ui" / "nurse_view" / "nurse_main_widget.py",
    ):
        widget_source = widget_path.read_text(encoding="utf-8")
        tree = ast.parse(widget_source)
        apply_snapshot = ""
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_apply_card_snapshot":
                apply_snapshot = ast.get_source_segment(widget_source, node) or ""
                break
        if "context_key" not in apply_snapshot or "_current_snapshot_context_key" not in apply_snapshot:
            return False, f"{widget_path.name}: snapshot stale guard does not use context key"

    return True, "ok"


def _check_visible_section_cache_keys_use_shift_context(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from datetime import datetime
    from collections import OrderedDict

    from rem_card.services import persistent_snapshot_cache
    from rem_card.ui.shared.components.current_orders_widget import CurrentNurseOrdersWidget
    from rem_card.ui.shared.components.diet_intake_widget import DietIntakeWidget

    same_shift_times = [
        datetime(2026, 5, 3, 8, 0, 0),
        datetime(2026, 5, 3, 9, 15, 30),
        datetime(2026, 5, 3, 13, 40, 10),
        datetime(2026, 5, 3, 23, 59, 59),
        datetime(2026, 5, 4, 2, 30, 0),
        datetime(2026, 5, 4, 7, 59, 59),
    ]
    next_shift = datetime(2026, 5, 4, 8, 0, 0)

    orders_keys = {CurrentNurseOrdersWidget._cache_key_for(7, dt) for dt in same_shift_times}
    orders_key_next = CurrentNurseOrdersWidget._cache_key_for(7, next_shift)
    if len(orders_keys) != 1:
        return False, f"orders visible cache key still depends on open time: {orders_keys}"
    if orders_key_next in orders_keys:
        return False, "orders visible cache key does not separate different medical shifts"

    diet = DietIntakeWidget.__new__(DietIntakeWidget)
    diet.admission_id = 7
    diet.role = "doctor"
    diet.read_only = False
    diet_keys = set()
    for dt in same_shift_times:
        diet.shift_date = dt
        diet_keys.add(diet._cache_key())
    diet.shift_date = next_shift
    diet_key_next = diet._cache_key()
    if len(diet_keys) != 1:
        return False, f"diet cache key still depends on open time: {diet_keys}"
    if diet_key_next in diet_keys:
        return False, "diet cache key does not separate different medical shifts"

    class FakeService:
        def get_latest_change_id(self, admission_id=None, include_global=True):
            _ = admission_id, include_global
            return 5

    orders_widget = CurrentNurseOrdersWidget.__new__(CurrentNurseOrdersWidget)
    orders_widget.service = FakeService()
    orders_widget.admission_id = 7
    orders_widget.shift_date = same_shift_times[0]
    orders_widget._snapshot_cache = OrderedDict()
    orders_widget._store_snapshot_cache([{"id": 1, "planned_time": "2026-05-03T09:00:00"}])
    orders_persisted = persistent_snapshot_cache.load_snapshot("current_orders", orders_widget._cache_key())
    if not orders_persisted or orders_persisted.get("data", [{}])[0].get("id") != 1:
        return False, f"current orders persistent cache was not stored: {orders_persisted}"

    diet.service = FakeService()
    diet._snapshot_cache = OrderedDict()
    diet._templates = []
    diet._plan = None
    diet._events = []
    diet.shift_date = same_shift_times[0]
    diet._store_snapshot_cache()
    diet_persisted = persistent_snapshot_cache.load_snapshot("diet", diet._cache_key())
    if diet_persisted is None or "events" not in diet_persisted:
        return False, f"diet persistent cache was not stored: {diet_persisted}"

    return True, "ok"


def _check_balance_loading_state_uses_placeholders(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication

    from rem_card.ui.rem_card_sectors.balance.sector_2b_g import Sector2b_g
    from rem_card.ui.rem_card_sectors.balance.sector_2b_v import Sector2b_v
    from rem_card.ui.rem_card_sectors.sector_3a import Sector3a
    from rem_card.ui.rem_card_sectors.sector_3b import Sector3b
    from rem_card.ui.rem_card_sectors.sector_4a import Sector4a

    app = QApplication.instance() or QApplication([])
    _ = app

    widgets = [Sector2b_g(), Sector2b_v(), Sector3a(), Sector3b(), Sector4a()]
    try:
        for widget in widgets:
            if not hasattr(widget, "set_loading_state"):
                return False, f"{widget.__class__.__name__} has no set_loading_state"
            widget.set_loading_state()

        checks = [
            widgets[0].total_in_val.text(),
            widgets[1].total_out_val.text(),
            widgets[1].balance_val.text(),
            widgets[2].total_in_val.text(),
            widgets[3].total_out_val.text(),
            widgets[4].balance_val.text(),
        ]
        bad = [text for text in checks if text.strip().startswith("0")]
        if bad:
            return False, f"loading state still shows zero-like values: {bad}"
        if not all("—" in text for text in checks):
            return False, f"loading state should use placeholders, got {checks}"
        return True, "ok"
    finally:
        for widget in widgets:
            widget.close()


def _check_lazy_section_snapshot_caches(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from datetime import datetime, timedelta

    from PySide6.QtWidgets import QApplication

    from rem_card.ui.rem_card_sectors.sector_events import SectorEvents
    from rem_card.ui.rem_card_sectors.sector_ivl import SectorIvl

    app = QApplication.instance() or QApplication([])
    _ = app

    class FakeStatusService:
        def __init__(self):
            self.version = {}
            self.calls = []

        def get_latest_change_id(self, admission_id=None, include_global=False):
            _ = include_global
            return int(self.version.get(int(admission_id or 0), 1))

        def get_events_in_range(self, admission_id, shift_start, shift_end):
            self.calls.append(("range", int(admission_id), shift_start.isoformat(), shift_end.isoformat()))
            return []

        def get_events(self, admission_id):
            self.calls.append(("all", int(admission_id)))
            return []

    class FakeRemCardService:
        def __init__(self):
            self.version = {}
            self.calls = []

        def get_latest_change_id(self, admission_id=None, include_global=False):
            _ = include_global
            return int(self.version.get(int(admission_id or 0), 1))

        def get_ventilation_summary(self, admission_id):
            self.calls.append(("summary", int(admission_id)))
            return {"active_case": None, "total_duration_seconds": 0.0}

        def get_ventilation_timeline(self, admission_id):
            self.calls.append(("timeline", int(admission_id)))
            return []

        def get_latest_ventilation_case(self, admission_id):
            self.calls.append(("latest", int(admission_id)))
            return None

        def get_patient(self, admission_id):
            _ = admission_id
            return None

    shift_start = datetime(2026, 5, 3, 8, 0)
    shift_end = shift_start + timedelta(hours=12)

    events_service = FakeStatusService()
    events_widget = SectorEvents()
    ivl_service = FakeRemCardService()
    ivl_widget = SectorIvl()
    try:
        events_widget.role = "Врач"
        events_widget.shift_start = shift_start
        events_widget.shift_end = shift_end
        events_widget.set_patient(1, events_service)
        events_widget.set_patient(2, events_service)
        events_widget.set_patient(1, events_service)
        event_patient_calls = [call[1] for call in events_service.calls]
        if event_patient_calls != [1, 2]:
            return False, f"events hot-cache should avoid repeated DB load, calls={events_service.calls}"

        for admission_id in range(3, 11):
            events_widget.set_patient(admission_id, events_service)
        events_widget.set_patient(1, events_service)
        events_widget.set_patient(11, events_service)
        event_keys = list(events_widget._snapshot_cache.keys())
        if len(event_keys) != 10 or not any(key[0] == 1 for key in event_keys) or any(key[0] == 2 for key in event_keys):
            return False, f"events LRU cache mismatch: {event_keys}"

        ivl_widget.set_runtime_context(ivl_service, 1)
        ivl_widget.set_runtime_context(ivl_service, 2)
        ivl_widget.set_runtime_context(ivl_service, 1)
        ivl_patient_calls = [call[1] for call in ivl_service.calls if call[0] == "summary"]
        if ivl_patient_calls != [1, 2]:
            return False, f"ivl hot-cache should avoid repeated DB load, calls={ivl_service.calls}"

        for admission_id in range(3, 11):
            ivl_widget.set_runtime_context(ivl_service, admission_id)
        ivl_widget.set_runtime_context(ivl_service, 1)
        ivl_widget.set_runtime_context(ivl_service, 11)
        ivl_keys = list(ivl_widget._snapshot_cache.keys())
        if len(ivl_keys) != 10 or (1, "ivl") not in ivl_keys or (2, "ivl") in ivl_keys:
            return False, f"ivl LRU cache mismatch: {ivl_keys}"
        return True, "ok"
    finally:
        events_widget.close()
        ivl_widget.close()


def _check_sync_coordinator_classifies_targeted_refresh(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from rem_card.services.sync_coordinator import SyncCoordinator

    def actions(payload):
        return SyncCoordinator.classify(payload)["sync_actions"]

    orders = actions({
        "changed_entities": ["orders"],
        "changes": [{"entity_name": "orders", "admission_id": 1}],
    })
    if orders["full_refresh_required"] or orders["card_snapshot_required"]:
        return False, f"orders should not require full card snapshot: {orders}"
    if not orders["orders_refresh"] or orders["vitals_snapshot_required"]:
        return False, f"orders classification mismatch: {orders}"
    if not orders["balance_refresh"]:
        return False, f"orders should refresh balance sectors: {orders}"

    administrations = actions({
        "changed_entities": ["administrations"],
        "changes": [{"entity_name": "administrations", "admission_id": 1}],
    })
    if administrations["full_refresh_required"] or administrations["card_snapshot_required"]:
        return False, f"administrations should not require full card snapshot: {administrations}"
    if not (administrations["orders_refresh"] and administrations["balance_refresh"]):
        return False, f"administrations should refresh orders and balance: {administrations}"

    vitals = actions({
        "changed_entities": ["vitals"],
        "changes": [{"entity_name": "vitals", "admission_id": 1}],
    })
    if vitals["full_refresh_required"] or vitals["card_snapshot_required"]:
        return False, f"vitals should be partial snapshot only: {vitals}"
    if not vitals["vitals_snapshot_required"]:
        return False, f"vitals snapshot was not requested: {vitals}"

    fluids = actions({
        "changed_entities": ["fluids"],
        "changes": [{"entity_name": "fluids", "admission_id": 1}],
    })
    if fluids["full_refresh_required"] or fluids["card_snapshot_required"] or fluids["vitals_snapshot_required"]:
        return False, f"fluids should stay balance-only: {fluids}"
    if not fluids["balance_refresh"]:
        return False, f"balance refresh was not requested: {fluids}"

    diet = actions({
        "changed_entities": ["diet_plan", "oral_intake_events"],
        "changes": [
            {"entity_name": "diet_plan", "admission_id": 1},
            {"entity_name": "oral_intake_events", "admission_id": 1},
        ],
    })
    if diet["full_refresh_required"] or diet["card_snapshot_required"]:
        return False, f"diet/oral changes should not require full card snapshot: {diet}"
    if not diet["diet_refresh"] or not diet["balance_refresh"]:
        return False, f"diet/oral classification mismatch: {diet}"

    status = actions({
        "changed_entities": ["patient_status_events"],
        "changes": [{"entity_name": "patient_status_events", "admission_id": 1}],
    })
    if status["full_refresh_required"] or status["card_snapshot_required"]:
        return False, f"status should not require full card snapshot: {status}"
    if not (status["status_refresh"] and status["vitals_snapshot_required"] and status["balance_refresh"]):
        return False, f"status classification mismatch: {status}"

    local_force = actions({
        "forced": True,
        "force_source": "orders_left_click:1",
        "changed_entities": ["orders"],
    })
    if local_force["full_refresh_required"] or local_force["card_snapshot_required"]:
        return False, f"local orders force should not require full refresh: {local_force}"
    if not local_force["balance_refresh"]:
        return False, f"local orders force should refresh balance: {local_force}"

    nurse_panel_force = actions({
        "forced": True,
        "force_source": "nurse_order_panel_mark:5",
        "changed_entities": ["administrations"],
    })
    if nurse_panel_force["full_refresh_required"] or nurse_panel_force["card_snapshot_required"]:
        return False, f"nurse panel mark should not require full refresh: {nurse_panel_force}"
    if not (nurse_panel_force["orders_refresh"] and nurse_panel_force["balance_refresh"]):
        return False, f"nurse panel mark should refresh orders and balance: {nurse_panel_force}"

    gap = actions({
        "gap_detected": True,
        "reason": "gap_detected",
        "changed_entities": ["orders"],
    })
    if not (gap["full_refresh_required"] and gap["card_snapshot_required"]):
        return False, f"gap must require full refresh: {gap}"

    empty_forced = actions({"forced": True, "force_source": "unknown_source"})
    if not (empty_forced["full_refresh_required"] and empty_forced["card_snapshot_required"]):
        return False, f"unknown forced refresh must be conservative: {empty_forced}"

    return True, "ok"


def _check_orders_balance_adapter_uses_local_state(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from datetime import datetime, timedelta

    from rem_card.data.dto.remcard_dto import AdministrationDTO, OrderDTO, OrderStatus, OrderType
    from rem_card.services.balance_calculator import BalanceCalculator
    from rem_card.ui.shared.orders_balance_adapter import (
        apply_current_order_mark_overrides,
        build_balance_orders_from_orders_widget,
        oral_totals_from_runtime,
    )

    shift_start = datetime(2026, 5, 3, 8, 0)

    class FakeService:
        def get_day_period(self, value):
            _ = value
            return shift_start, shift_start + timedelta(days=1)

    class FakeModel:
        def __init__(self, *, pending_mark: bool):
            self.service = FakeService()
            self.admission_id = 7
            self.shift_date = shift_start
            self.orders = [
                OrderDTO(
                    id=11,
                    admission_id=7,
                    latin="NaCl",
                    type=OrderType.INFUSION_INTERMITTENT,
                    status=OrderStatus.ACTIVE,
                    volume_total=100.0,
                    duration_min=60,
                    is_committed=1,
                )
            ]
            admin = AdministrationDTO(
                id=21,
                order_id=11,
                planned_time=shift_start + timedelta(hours=1),
                status="planned",
                is_committed=1,
                comment="",
                volume_ml=100.0,
            )
            if pending_mark:
                setattr(admin, "_pending_mark", "nurse_executed")
            self.admin_map = {(11, admin.planned_time.isoformat()): admin}

    class FakeWidget:
        def __init__(self, pending: int = 0, has_drafts: bool = False, pending_mark: bool = False):
            self.model = FakeModel(pending_mark=pending_mark)
            self._pending_admin_write_count = pending
            self._has_drafts = has_drafts

        def has_drafts(self):
            return self._has_drafts

    inactive_widget = FakeWidget()
    if build_balance_orders_from_orders_widget(inactive_widget, 7, shift_start) is not None:
        return False, "inactive orders widget without local state should not override balance runtime"

    active_widget = FakeWidget()
    active_widget.model.admin_map[(11, (shift_start + timedelta(hours=1)).isoformat())].comment = "nurse_not_executed"
    active_orders = build_balance_orders_from_orders_widget(active_widget, 7, shift_start, tab_active=True)
    if not active_orders:
        return False, "active orders tab should use visible local model for balance"
    active_admins = getattr(active_orders[0], "administrations", None) or []
    if active_admins[0].comment != "nurse_not_executed":
        return False, "active orders tab lost committed nurse mark from local model"

    widget = FakeWidget(pending=1, pending_mark=True)
    balance_orders = build_balance_orders_from_orders_widget(widget, 7, shift_start)
    if not balance_orders or balance_orders[0] is widget.model.orders[0]:
        return False, "local balance adapter did not return copied orders"
    admins = getattr(balance_orders[0], "administrations", None) or []
    if len(admins) != 1:
        return False, f"local balance adapter did not attach administrations: {admins}"
    if admins[0].comment != "nurse_executed" or admins[0].actual_time is None:
        return False, "pending nurse mark was not applied to local balance administration"

    setattr(widget.model.orders[0], "_pending_delete", True)
    deleted_orders = build_balance_orders_from_orders_widget(widget, 7, shift_start)
    if deleted_orders != []:
        return False, f"pending deleted order should be excluded from local balance: {deleted_orders}"

    if build_balance_orders_from_orders_widget(widget, 8, shift_start) is not None:
        return False, "different admission should not use local orders"

    runtime_orders = []
    for order_id, admin_id, hour in ((101, 201, 1), (102, 202, 2)):
        order = OrderDTO(
            id=order_id,
            admission_id=7,
            drug_key="manual_balance_test",
            latin="Manual balance test",
            type=OrderType.INFUSION_INTERMITTENT,
            status=OrderStatus.ACTIVE,
            dose_value=0,
            dose_unit="ml",
            duration_min=0,
            is_committed=1,
            comment="S. NaCl - 250 ml",
        )
        order.administrations = [
            AdministrationDTO(
                id=admin_id,
                order_id=order_id,
                planned_time=shift_start + timedelta(hours=hour),
                status="planned",
                is_committed=1,
                comment="",
            )
        ]
        runtime_orders.append(order)

    class FakeCurrentOrders:
        def __init__(self, mark: str):
            self.service = FakeService()
            self.admission_id = 7
            self.shift_date = shift_start
            self._pending_marks = {
                201: {
                    "mark": mark,
                    "actual_time": (shift_start + timedelta(hours=1)).isoformat(),
                    "started_mono": 0.0,
                }
            }

        def _get_pending_mark(self, admin_id: int):
            return self._pending_marks.get(int(admin_id))

    patched_not_done = apply_current_order_mark_overrides(
        runtime_orders,
        FakeCurrentOrders("nurse_not_executed"),
        7,
        shift_start,
    )
    if patched_not_done is None or patched_not_done[0].administrations[0].comment != "nurse_not_executed":
        return False, "sector 1a pending not-done mark was not applied to balance orders"
    if runtime_orders[0].administrations[0].comment:
        return False, "sector 1a balance override mutated runtime orders"
    base_calc = BalanceCalculator.calculate(runtime_orders, shift_start + timedelta(hours=3), shift_start + timedelta(days=1))
    not_done_calc = BalanceCalculator.calculate(patched_not_done, shift_start + timedelta(hours=3), shift_start + timedelta(days=1))
    if base_calc["daily"]["total"] != 500 or not_done_calc["daily"]["total"] != 250:
        return False, f"sector 1a not-done daily balance mismatch: base={base_calc} not_done={not_done_calc}"

    patched_done = apply_current_order_mark_overrides(
        runtime_orders,
        FakeCurrentOrders("nurse_executed"),
        7,
        shift_start,
    )
    done_calc = BalanceCalculator.calculate(patched_done, shift_start + timedelta(hours=3), shift_start + timedelta(days=1))
    if done_calc["current"]["total"] != 250 or done_calc["daily"]["total"] != 500:
        return False, f"sector 1a executed balance mismatch: {done_calc}"

    patched_cancel = apply_current_order_mark_overrides(
        patched_not_done,
        FakeCurrentOrders(""),
        7,
        shift_start,
    )
    if patched_cancel is None or patched_cancel[0].administrations[0].comment:
        return False, "sector 1a pending cancel mark did not clear balance order mark"
    cancel_calc = BalanceCalculator.calculate(patched_cancel, shift_start + timedelta(hours=3), shift_start + timedelta(days=1))
    if cancel_calc["daily"]["total"] != 500 or cancel_calc["current"]["total"] != 0:
        return False, f"sector 1a cancel balance mismatch: {cancel_calc}"

    class FakeOralEvent:
        def __init__(self, event_time, amount_ml):
            self.event_time = event_time
            self.amount_ml = amount_ml

    oral_current, oral_daily = oral_totals_from_runtime(
        {
            "oral_events": [
                FakeOralEvent(shift_start + timedelta(hours=1), 100),
                FakeOralEvent(shift_start + timedelta(hours=5), 200),
            ]
        },
        shift_start + timedelta(hours=2),
    )
    if (oral_current, oral_daily) != (100.0, 300.0):
        return False, f"cached oral totals mismatch: {(oral_current, oral_daily)}"

    fallback_current, fallback_daily = oral_totals_from_runtime(
        {"oral_totals": {"current": 10, "daily": 20}},
        shift_start + timedelta(hours=2),
    )
    if (fallback_current, fallback_daily) != (10.0, 20.0):
        return False, f"fallback oral totals mismatch: {(fallback_current, fallback_daily)}"

    return True, "ok"


def _check_card_widgets_use_sync_actions_for_partial_refresh(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    widget_paths = [
        PROJECT_ROOT / "ui" / "doctor_view" / "doctor_remcard_widget.py",
        PROJECT_ROOT / "ui" / "nurse_view" / "nurse_main_widget.py",
    ]
    for path in widget_paths:
        source_text = path.read_text(encoding="utf-8")
        tree = ast.parse(source_text)
        methods = {
            node.name: ast.get_source_segment(source_text, node) or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        on_changes = methods.get("_on_data_changes", "")
        if not on_changes:
            return False, f"{path.name}: _on_data_changes not found"
        if "sync_actions" not in on_changes or "vitals_snapshot_required" not in on_changes:
            return False, f"{path.name}: SyncCoordinator actions are not used"
        if "if card_snapshot_required:" in on_changes:
            return False, f"{path.name}: card_snapshot_required must not be an unconditional full-card path"
        if 'load_scope="patient_open_vitals"' not in on_changes:
            return False, f"{path.name}: vitals changes must use partial vitals snapshot"
        local_force_pos = on_changes.find("_is_local_orders_force_payload")
        diet_pos = on_changes.find("_handle_diet_sync", local_force_pos)
        if local_force_pos < 0 or diet_pos < 0:
            return False, f"{path.name}: local orders force branch not found"
        local_force_block = on_changes[local_force_pos:diet_pos]
        if "_refresh_balance_from_db()" in local_force_block:
            return False, f"{path.name}: local order force must not synchronously reload balance from DB"
        if "_schedule_balance_update()" not in local_force_block:
            return False, f"{path.name}: local order force must schedule local balance update"
        partial_actions = methods.get("_apply_partial_sync_actions", "")
        if "_apply_partial_sync_actions(" not in on_changes or not partial_actions:
            return False, f"{path.name}: partial sync action dispatcher missing"
        for helper in (
            "_refresh_balance_from_db",
            "_refresh_status_from_db",
            "_refresh_ivl_from_db",
        ):
            if helper not in methods:
                return False, f"{path.name}: {helper} helper missing"
            if f"{helper}()" not in partial_actions:
                return False, f"{path.name}: {helper} is not called from partial sync dispatcher"
        balance_method = methods.get("update_balance_data") or methods.get("_update_balance_calculations") or ""
        if "get_oral_intake_totals" in balance_method:
            return False, f"{path.name}: balance UI update must not synchronously read oral totals from DB"
        if "oral_totals_from_runtime" not in balance_method:
            return False, f"{path.name}: balance UI update must use cached oral runtime"
    return True, "ok"


def main():
    temp_root = _make_temp_root()
    _prepare_import_environment(temp_root)

    checks = [
        ("lock_read_unavailable_not_stale", _check_lock_read_unavailable_not_stale),
        ("role_lock_read_unavailable_blocks_acquire", _check_role_lock_read_unavailable_blocks_acquire),
        ("local_write_queue_shutdown_drains", _check_local_write_queue_shutdown_drains),
        ("sync_cursor_normalizes_timestamp_formats", _check_sync_cursor_normalizes_timestamp_formats),
        ("change_log_lag_uses_utc_for_sqlite_timestamp", _check_change_log_lag_uses_utc_for_sqlite_timestamp),
        ("startup_lock_timeout_messages", _check_startup_lock_timeout_messages),
        ("transaction_isolation", _check_transaction_isolation),
        ("read_your_writes_inside_tx", _check_read_your_writes_inside_transaction),
        ("central_reads_split_from_write_connection", _check_central_reads_split_from_write_connection),
        ("dev_baza_dir_prefers_project_baza_name", _check_dev_baza_dir_prefers_project_baza_name),
        ("arbitrary_baza_dir_name_allowed", _check_arbitrary_baza_dir_name_allowed),
        ("schema_migration_backup_fastpath_policy", _check_schema_migration_backup_fastpath_policy),
        ("schema_migration_invalid_backup_blocks_ddl", _check_schema_migration_invalid_backup_blocks_ddl),
        ("schema_migration_failure_rolls_back", _check_schema_migration_failure_rolls_back),
        ("schema_migration_parallel_start", _check_schema_migration_parallel_start),
        ("old_client_blocked_by_policy", _check_old_client_blocked_by_policy),
        ("recovery_blocks_active_second_client", _check_recovery_blocks_active_second_client),
        ("recovery_db_lock_busy_blocks_restore", _check_recovery_db_lock_busy_blocks_restore),
        ("recovery_lock_busy_blocks_restore", _check_recovery_lock_busy_blocks_restore),
        ("dbmanager_locked_quickcheck_does_not_restore", _check_dbmanager_locked_quickcheck_does_not_restore),
        ("recovery_selects_next_valid_backup", _check_recovery_selects_next_valid_backup),
        ("local_metrics_written_locally", _check_local_metrics_written_locally),
        ("local_metrics_are_buffered", _check_local_metrics_are_buffered),
        ("sector_ivl_enqueue_error_refreshes", _check_sector_ivl_enqueue_error_refreshes),
        ("balance_controller_enqueue_error_refreshes", _check_balance_controller_enqueue_error_refreshes),
        ("diet_intake_enqueue_error_refreshes", _check_diet_intake_enqueue_error_refreshes),
        ("oral_intake_batch_rolls_back", _check_oral_intake_batch_rolls_back),
        ("patient_form_enqueue_error_keeps_dialog", _check_patient_form_enqueue_error_keeps_dialog),
        ("patient_bed_move_enqueue_error_refreshes", _check_patient_bed_move_enqueue_error_refreshes),
        ("archive_delete_enqueue_error_refreshes", _check_archive_delete_enqueue_error_refreshes),
        ("doctor_create_card_enqueue_error_refreshes", _check_doctor_create_card_enqueue_error_refreshes),
        ("patient_status_error_refreshes_checked_state", _check_patient_status_error_refreshes_checked_state),
        ("orders_pending_states_before_commit", _check_orders_pending_states_before_commit),
        ("blood_plasma_key_ru_prescription_parse", _check_blood_plasma_key_ru_prescription_parse),
        ("order_input_real_examples", _check_order_input_real_examples),
        ("local_replica_tmp_cleanup", _check_local_replica_tmp_cleanup),
        ("backup_cleanup_gating", _check_backup_cleanup_gating),
        ("backup_count_limit_enforcement", _check_backup_count_limit_enforcement),
        ("runtime_backup_rotation_scans_valid_dir", _check_runtime_backup_rotation_scans_valid_dir),
        ("balance_admission_hour_visibility", _check_balance_admission_hour_visibility),
        ("print_hourly_input_planned_time", _check_print_hourly_input_planned_time),
        ("sector_print_transform_snapshot", _check_sector_print_transform_snapshot),
        ("sector_events_refresh_snapshot", _check_sector_events_refresh_snapshot),
        ("statistics_dialog_snapshot", _check_statistics_dialog_snapshot),
        ("vitals_boundary_minutes", _check_vitals_boundary_minutes),
        ("orders_force_refresh_accepts_unchanged_version", _check_orders_force_refresh_accepts_unchanged_version),
        ("doctor_orders_late_model_binding", _check_doctor_orders_late_model_binding),
        ("orders_widget_skips_duplicate_snapshot", _check_orders_widget_skips_duplicate_snapshot),
        ("order_row_delete_without_times_marks_draft", _check_order_row_delete_without_times_marks_draft),
        ("orders_cell_delete_draft_and_noop_toggle", _check_orders_cell_delete_draft_and_noop_toggle),
        ("orders_optimistic_lock_conflicts", _check_orders_optimistic_lock_conflicts),
        ("remaining_clinical_optimistic_lock_conflicts", _check_remaining_clinical_optimistic_lock_conflicts),
        ("medical_audit_log_triggers", _check_medical_audit_log_triggers),
        ("doctor_create_card_avoids_open_snapshot_race", _check_doctor_create_card_avoids_open_snapshot_race),
        (
            "orders_widgets_defer_snapshot_reload_thread_creation",
            _check_orders_widgets_defer_snapshot_reload_thread_creation,
        ),
        ("targeted_async_workers_are_parentless_and_guarded", _check_targeted_async_workers_are_parentless_and_guarded),
        ("patient_form_open_is_deferred_from_callback", _check_patient_form_open_is_deferred_from_callback),
        ("shutdown_queue_db_ordering_guards", _check_shutdown_queue_db_ordering_guards),
        ("orders_fast_click_path_stays_local", _check_orders_fast_click_path_stays_local),
        ("performance_a_guards_present", _check_performance_a_guards_present),
        ("report_pdf_callbacks_are_qobject_slots", _check_report_pdf_callbacks_are_qobject_slots),
        ("pdf_build_runs_in_worker", _check_pdf_build_runs_in_worker),
        ("analytics_runs_outside_ui_callbacks", _check_analytics_runs_outside_ui_callbacks),
        ("w1_yesterday_card_skips_status_write_and_defers", _check_w1_yesterday_card_skips_status_write_and_defers),
        ("chart_clears_on_card_context_change", _check_chart_clears_on_card_context_change),
        ("journal_prewarm_is_opt_in", _check_journal_prewarm_is_opt_in),
        ("w1_beds_refreshes_on_vitals_change", _check_w1_beds_refreshes_on_vitals_change),
        ("w1_outcome_timer_ticks_without_beds_refresh", _check_w1_outcome_timer_ticks_without_beds_refresh),
        ("w1_outcome_release_runs_from_change_monitor", _check_w1_outcome_release_runs_from_change_monitor),
        ("build_release_reuses_prepared_version", _check_build_release_reuses_prepared_version),
        ("patient_card_cache_lru_10", _check_patient_card_cache_lru_10),
        ("read_coordinator_partial_snapshots", _check_read_coordinator_partial_snapshots),
        ("visible_section_cache_keys_use_shift_context", _check_visible_section_cache_keys_use_shift_context),
        ("balance_loading_state_uses_placeholders", _check_balance_loading_state_uses_placeholders),
        ("lazy_section_snapshot_caches", _check_lazy_section_snapshot_caches),
        ("sync_coordinator_classifies_targeted_refresh", _check_sync_coordinator_classifies_targeted_refresh),
        ("orders_balance_adapter_uses_local_state", _check_orders_balance_adapter_uses_local_state),
        ("card_widgets_use_sync_actions_for_partial_refresh", _check_card_widgets_use_sync_actions_for_partial_refresh),
    ]

    result_items = []
    failures = 0
    started = time.time()
    try:
        for name, fn in checks:
            check_root = os.path.join(temp_root, name)
            Path(check_root).mkdir(parents=True, exist_ok=True)
            ok, details = fn(check_root)
            result_items.append({"check": name, "ok": bool(ok), "details": str(details)})
            if not ok:
                failures += 1
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    report = {
        "total": len(checks),
        "failed": failures,
        "passed": len(checks) - failures,
        "duration_sec": round(time.time() - started, 3),
        "checks": result_items,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
