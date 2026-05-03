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
import tempfile
import threading
import time
from pathlib import Path


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
    from rem_card.ui.analytics.statistics_dialog import StatisticsDialog

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

    def make_dialog(conn):
        dialog = StatisticsDialog.__new__(StatisticsDialog)
        dialog.db_manager = Manager(conn)
        dialog._start_dt = StatisticsDialog._parse_datetime("2026-04-01")
        dialog._end_dt = StatisticsDialog._parse_datetime("2026-04-30")
        dialog.start_date_str = dialog._start_dt.strftime("%Y-%m-%d 00:00:00")
        dialog.end_date_str = dialog._end_dt.strftime("%Y-%m-%d 23:59:59")
        dialog.section_groups = {
            "Основная деятельность": {
                "s1": "1. Общая деятельность отделения",
                "s2": "2. Использование коечного фонда",
                "s3": "3. Демография",
                "s4": "4. Поток пациентов",
                "s5": "5. Диагностическая структура",
                "s6": "6. Исходы лечения",
                "s7": "7. Время до смерти",
                "s8": "8. Летальность по группам",
            },
            "Интенсивная терапия и вмешательства": {
                "s9": "9. ИВЛ",
                "s10": "10. Операции",
                "s11": "11. Переливания",
                "s16": "16. Индексы интенсивности",
                "s17": "17. Индексы нагрузки",
                "s18": "18. Специальные показатели",
                "s19": "19. Нагрузка персонала",
                "sx": "➕ Дополнительные показатели",
            },
        }
        return dialog

    def make_conn(with_data: bool):
        conn = sqlite3.connect(":memory:")
        init_db(conn)
        if with_data:
            seed(conn)
        return conn

    def snapshot(with_data: bool):
        dialog = make_dialog(make_conn(with_data))
        stats = dialog._calculate_statistics()
        selected = ["s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "s9", "s10", "s11", "s16", "s17", "s18", "s19", "sx"]
        return {
            "stats": stats,
            "rows": {key: dialog._section_rows(key, stats) for key in selected},
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
    if coordinator.get_cached_card(card_key(2)) is not None:
        return False, "oldest patient 2 cache survived after 11th context"

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

    service.versions[1] = 2
    if coordinator.get_current_cached_card(card_key(1)) is not None:
        return False, "stale patient 1 card cache was treated as current"
    if coordinator.get_cached_card(card_key(1)) is None:
        return False, "stale patient 1 card cache was removed instead of preserved for SWR"
    refreshed = coordinator.load_patient_card_snapshot(1, shift_date, role="doctor", force_refresh=False)
    if int(refreshed.get("version") or 0) != 2:
        return False, f"patient 1 card cache did not refresh to version 2: {refreshed.get('version')}"

    return True, "ok"


def _check_visible_section_cache_keys_use_shift_context(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from datetime import datetime

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


def main():
    temp_root = _make_temp_root()
    _prepare_import_environment(temp_root)

    checks = [
        ("lock_read_unavailable_not_stale", _check_lock_read_unavailable_not_stale),
        ("role_lock_read_unavailable_blocks_acquire", _check_role_lock_read_unavailable_blocks_acquire),
        ("local_write_queue_shutdown_drains", _check_local_write_queue_shutdown_drains),
        ("sync_cursor_normalizes_timestamp_formats", _check_sync_cursor_normalizes_timestamp_formats),
        ("transaction_isolation", _check_transaction_isolation),
        ("read_your_writes_inside_tx", _check_read_your_writes_inside_transaction),
        ("central_reads_split_from_write_connection", _check_central_reads_split_from_write_connection),
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
        ("doctor_create_card_avoids_open_snapshot_race", _check_doctor_create_card_avoids_open_snapshot_race),
        (
            "orders_widgets_defer_snapshot_reload_thread_creation",
            _check_orders_widgets_defer_snapshot_reload_thread_creation,
        ),
        ("report_pdf_callbacks_are_qobject_slots", _check_report_pdf_callbacks_are_qobject_slots),
        ("w1_yesterday_card_skips_status_write_and_defers", _check_w1_yesterday_card_skips_status_write_and_defers),
        ("chart_clears_on_card_context_change", _check_chart_clears_on_card_context_change),
        ("journal_prewarm_is_opt_in", _check_journal_prewarm_is_opt_in),
        ("w1_beds_refreshes_on_vitals_change", _check_w1_beds_refreshes_on_vitals_change),
        ("w1_outcome_timer_ticks_without_beds_refresh", _check_w1_outcome_timer_ticks_without_beds_refresh),
        ("build_release_reuses_prepared_version", _check_build_release_reuses_prepared_version),
        ("patient_card_cache_lru_10", _check_patient_card_cache_lru_10),
        ("visible_section_cache_keys_use_shift_context", _check_visible_section_cache_keys_use_shift_context),
        ("balance_loading_state_uses_placeholders", _check_balance_loading_state_uses_placeholders),
        ("lazy_section_snapshot_caches", _check_lazy_section_snapshot_caches),
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
