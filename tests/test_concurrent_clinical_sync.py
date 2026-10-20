from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from rem_card.app.local_replica_sync import LocalReplicaSync
from rem_card.app.local_replica_worker import database_write_lock_path
from rem_card.app.sqlite_shared import SQLiteWriteController, configure_connection
from rem_card.app.unified_db_schema import ensure_unified_schema
from rem_card.data.dao.orders_dao import OrdersDAO
from rem_card.data.dto.remcard_dto import OrderDTO
from rem_card.services.order_domain_service import OrderDomainService
from rem_card.services.order_service import OrderService


SHIFT = datetime(2026, 10, 20, 8, 0)


class ClinicalConnection:
    """Small DatabaseManager-compatible facade backed by one real connection."""

    def __init__(self, path: Path, *, before_commit=None):
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        configure_connection(self.conn, profile="network")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self._before_commit = before_commit
        lock_path = Path(database_write_lock_path(str(self.path)))
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.controller = SQLiteWriteController(
            str(self.path), str(lock_path), f"clinical-test:{id(self)}", retry_delay_ms=10
        )

    @contextmanager
    def remcard_transaction(self, *, source="clinical-test", **_kwargs):
        with self.controller.transaction(self.conn, source=source) as cursor:
            yield cursor
            if self._before_commit is not None:
                self._before_commit()

    def fetch_one_remcard(self, query, params=(), **_kwargs):
        return self.conn.execute(query, params).fetchone()

    def fetch_all_remcard(self, query, params=(), **_kwargs):
        return self.conn.execute(query, params).fetchall()

    def execute_remcard(self, query, params=(), **_kwargs):
        return self.conn.execute(query, params)

    def run_write_operation(self, operation, *, source="test", **_kwargs):
        with self.remcard_transaction(source=source) as cursor:
            return operation(cursor)

    def close(self):
        self.conn.close()


def _domain(database: ClinicalConnection) -> OrderDomainService:
    service = OrderDomainService(database)
    service._legacy_statuses_sanitized = True
    return service


def _seed_database(path: Path, *, order_count: int = 4) -> list[tuple[int, int]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        ensure_unified_schema(conn, allow_local_schema_upgrades=True)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('db_cycle_started_at', 'synthetic-concurrent')"
        )
        conn.execute("INSERT INTO patients(id, full_name) VALUES(1, 'Синтетический пациент')")
        conn.execute(
            """
            INSERT INTO admissions(
                id, patient_id, bed_number, history_number, admission_datetime
            ) VALUES(1, 1, 1, 'SYNTHETIC-1', ?)
            """,
            (SHIFT.isoformat(),),
        )
        result = []
        for index in range(order_count):
            order_id = int(
                conn.execute(
                    """
                    INSERT INTO orders(
                        admission_id, datetime, text, latin, drug_key, type,
                        status, is_committed, dose_value, dose_unit, frequency,
                        specific_times, revision, comment, last_modified_by
                    ) VALUES(1, ?, ?, ?, ?, 'medication', 'active', 1, ?, 'mg', 1, '[]', 0, '', 'doctor')
                    """,
                    (
                        SHIFT.isoformat(),
                        f"Synthetic order {index}",
                        f"Drug {index}",
                        f"drug-{index}",
                        float(index + 1),
                    ),
                ).lastrowid
            )
            admin_id = int(
                conn.execute(
                    """
                    INSERT INTO administrations(
                        order_id, planned_time, cell_role, status, is_committed,
                        version, volume_ml, comment, last_modified_by
                    ) VALUES(?, ?, 'single', 'planned', 1, 0, 0, '', 'doctor')
                    """,
                    (order_id, (SHIFT + timedelta(hours=index + 1)).isoformat()),
                ).lastrowid
            )
            result.append((order_id, admin_id))
        conn.commit()
    return result


def _parallel_status_write(path: Path, barrier: threading.Barrier, *, role: str, admin_id: int, mark: str):
    database = ClinicalConnection(path)
    try:
        service = _domain(database)
        barrier.wait(timeout=10)
        try:
            if role == "doctor":
                service.set_doctor_status(admin_id, mark, expected_version=0)
            else:
                service.set_nurse_status(admin_id, mark, expected_version=0)
            return "committed", None
        except RuntimeError as exc:
            return "conflict", str(exc)
    finally:
        database.close()


def test_same_administration_same_expected_version_has_one_winner(tmp_path):
    path = tmp_path / "central" / "archiv" / "rao_journal.db"
    _, admin_id = _seed_database(path, order_count=1)[0]
    barrier = threading.Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                _parallel_status_write,
                path,
                barrier,
                role="doctor",
                admin_id=admin_id,
                mark="nurse_not_executed",
            ),
            pool.submit(
                _parallel_status_write,
                path,
                barrier,
                role="nurse",
                admin_id=admin_id,
                mark="nurse_executed",
            ),
        ]
        results = [future.result(timeout=20) for future in futures]

    assert sorted(result[0] for result in results) == ["committed", "conflict"]
    conflict = next(result[1] for result in results if result[0] == "conflict")
    assert "изменено" in conflict.lower()
    with closing(sqlite3.connect(path)) as conn:
        row = conn.execute(
            "SELECT comment, version, actual_time FROM administrations WHERE id=?",
            (admin_id,),
        ).fetchone()
        assert row[0] in {"nurse_executed", "nurse_not_executed"}
        assert row[1] == 1
        assert row[2]
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_distinct_administrations_survive_simultaneous_role_writes(tmp_path):
    path = tmp_path / "central" / "archiv" / "rao_journal.db"
    seeded = _seed_database(path, order_count=2)
    first_admin, second_admin = seeded[0][1], seeded[1][1]
    barrier = threading.Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                _parallel_status_write,
                path,
                barrier,
                role="doctor",
                admin_id=first_admin,
                mark="nurse_not_executed",
            ),
            pool.submit(
                _parallel_status_write,
                path,
                barrier,
                role="nurse",
                admin_id=second_admin,
                mark="nurse_executed",
            ),
        ]
        assert [future.result(timeout=20)[0] for future in futures] == ["committed", "committed"]

    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute(
            "SELECT id, comment, version FROM administrations ORDER BY id"
        ).fetchall()
        assert rows == [
            (first_admin, "nurse_not_executed", 1),
            (second_admin, "nurse_executed", 1),
        ]
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("doctor_action", ["edit_finalize", "delete_finalize"])
def test_doctor_commit_does_not_erase_concurrent_nurse_execution(tmp_path, doctor_action):
    path = tmp_path / "central" / "archiv" / "rao_journal.db"
    order_id, admin_id = _seed_database(path, order_count=1)[0]
    nurse_updated = threading.Event()
    doctor_attempting = threading.Event()

    def hold_nurse_commit_until_doctor_attempts():
        nurse_updated.set()
        assert doctor_attempting.wait(timeout=10)
        # Give the doctor connection time to enter BEGIN IMMEDIATE and wait.
        time.sleep(0.03)

    def nurse_write():
        database = ClinicalConnection(path, before_commit=hold_nurse_commit_until_doctor_attempts)
        try:
            _domain(database).set_nurse_status(
                admin_id,
                "nurse_executed",
                expected_version=0,
            )
        finally:
            database.close()

    def doctor_write():
        assert nurse_updated.wait(timeout=10)
        database = ClinicalConnection(path)
        try:
            doctor_attempting.set()
            service = OrderService(OrdersDAO(database))
            service._domain_service._legacy_statuses_sanitized = True
            if doctor_action == "edit_finalize":
                dto = OrderDTO(
                    id=order_id,
                    admission_id=1,
                    drug_key="drug-0",
                    latin="Edited drug",
                    dose_value=7,
                    dose_unit="mg",
                    frequency=1,
                    specific_times=[],
                    comment="doctor edit",
                    created_at=SHIFT,
                )
                updated = service.update_order(order_id, dto, expected_revision=0)
                expected_revision = updated.revision
            else:
                service.soft_delete_order_row(order_id, True, expected_revision=0)
                expected_revision = 1
            service.finalize_card(
                1,
                shift_date=SHIFT,
                expected_revisions={order_id: expected_revision},
            )
        finally:
            database.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        nurse_future = pool.submit(nurse_write)
        doctor_future = pool.submit(doctor_write)
        nurse_future.result(timeout=20)
        doctor_future.result(timeout=20)

    with closing(sqlite3.connect(path)) as conn:
        original = conn.execute(
            "SELECT comment, actual_time, version, is_committed FROM administrations WHERE id=?",
            (admin_id,),
        ).fetchone()
        assert original[0] == "nurse_executed"
        assert original[1]
        assert original[2:] == (1, 1)
        order = conn.execute(
            "SELECT latin, status, is_committed, revision FROM orders WHERE id=?",
            (order_id,),
        ).fetchone()
        if doctor_action == "edit_finalize":
            assert order == ("Edited drug", "active", 1, 2)
        else:
            assert order[1:] == ("deleted", 1, 2)
            assert conn.execute(
                "SELECT COUNT(*) FROM administrations WHERE order_id=? AND status='deleted' AND is_committed=1",
                (order_id,),
            ).fetchone()[0] == 1
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_doctor_and_nurse_replicas_catch_central_commits_read_only(tmp_path):
    path = tmp_path / "central" / "archiv" / "rao_journal.db"
    seeded = _seed_database(path, order_count=2)
    replicas = {
        role: LocalReplicaSync(
            central_db_path=str(path),
            local_db_path=str(tmp_path / "replicas" / f"{role}.db"),
        )
        for role in ("doctor", "nurse")
    }
    try:
        writer = ClinicalConnection(path)
        try:
            assert all(
                writer.controller.lock_path == replica._worker_client.writer_lock_path
                for replica in replicas.values()
            )
        finally:
            writer.close()
        for replica in replicas.values():
            assert replica.sync_once()

        barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda args: _parallel_status_write(path, barrier, **args),
                    (
                        {
                            "role": "doctor",
                            "admin_id": seeded[0][1],
                            "mark": "nurse_not_executed",
                        },
                        {
                            "role": "nurse",
                            "admin_id": seeded[1][1],
                            "mark": "nurse_executed",
                        },
                    ),
                )
            )
        assert [result[0] for result in results] == ["committed", "committed"]

        central_digest_before_sync = hashlib.sha256(path.read_bytes()).hexdigest()
        with closing(sqlite3.connect(path)) as conn:
            central_projection = conn.execute(
                "SELECT id, comment, version FROM administrations ORDER BY id"
            ).fetchall()
        for replica in replicas.values():
            assert replica.sync_once()
            assert replica.is_ready(max_stale_sec=60)
            assert replica.fetch_one("PRAGMA query_only")[0] == 1
            projection = [
                tuple(row)
                for row in replica.fetch_all(
                    "SELECT id, comment, version FROM administrations ORDER BY id"
                )
            ]
            assert projection == central_projection
        assert hashlib.sha256(path.read_bytes()).hexdigest() == central_digest_before_sync
        assert replicas["doctor"].last_state == replicas["nurse"].last_state
    finally:
        for replica in replicas.values():
            assert replica.stop()
