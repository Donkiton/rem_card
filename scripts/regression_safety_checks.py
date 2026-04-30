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
                "patient_context": None,
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

    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QApplication

    from rem_card.data.dto.remcard_dto import OrderDTO, OrderStatus, OrderType
    from rem_card.ui.doctor_view.orders_widget import OrdersWidget

    class DummyOrdersService(QObject):
        patient_context_changed = Signal(int)

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

    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QApplication

    from rem_card.data.dto.remcard_dto import OrderDTO, OrderStatus, OrderType
    from rem_card.services.read_coordinator import ReadCoordinator
    from rem_card.ui.doctor_view.orders_widget import OrdersWidget

    class DummyOrdersService(QObject):
        patient_context_changed = Signal(int)

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
            "patient_context": None,
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
            "patient_context": None,
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


def _check_chart_clears_on_patient_context_change(temp_root: str) -> tuple[bool, str]:
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


def main():
    temp_root = _make_temp_root()
    _prepare_import_environment(temp_root)

    checks = [
        ("lock_read_unavailable_not_stale", _check_lock_read_unavailable_not_stale),
        ("transaction_isolation", _check_transaction_isolation),
        ("read_your_writes_inside_tx", _check_read_your_writes_inside_transaction),
        ("central_reads_split_from_write_connection", _check_central_reads_split_from_write_connection),
        ("blood_plasma_key_ru_prescription_parse", _check_blood_plasma_key_ru_prescription_parse),
        ("local_replica_tmp_cleanup", _check_local_replica_tmp_cleanup),
        ("backup_cleanup_gating", _check_backup_cleanup_gating),
        ("backup_count_limit_enforcement", _check_backup_count_limit_enforcement),
        ("balance_admission_hour_visibility", _check_balance_admission_hour_visibility),
        ("print_hourly_input_planned_time", _check_print_hourly_input_planned_time),
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
        ("chart_clears_on_patient_context_change", _check_chart_clears_on_patient_context_change),
        ("journal_prewarm_is_opt_in", _check_journal_prewarm_is_opt_in),
        ("w1_beds_refreshes_on_vitals_change", _check_w1_beds_refreshes_on_vitals_change),
        ("w1_outcome_timer_ticks_without_beds_refresh", _check_w1_outcome_timer_ticks_without_beds_refresh),
        ("build_release_reuses_prepared_version", _check_build_release_reuses_prepared_version),
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
