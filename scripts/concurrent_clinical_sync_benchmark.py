#!/usr/bin/env python3
"""Synthetic concurrent clinical-write and two-role replica benchmark.

Only temporary databases and an isolated temporary user profile are used.  The
reported application latency is the cost of building a state projection from
replica rows; no GUI widget is created by this benchmark.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from _local_rem_card_bootstrap import bootstrap_local_rem_card  # noqa: E402

bootstrap_local_rem_card()

SHIFT = datetime(2026, 10, 20, 8, 0)


class ClinicalConnection:
    def __init__(self, path: Path):
        from rem_card.app.local_replica_worker import database_write_lock_path
        from rem_card.app.sqlite_shared import SQLiteWriteController, configure_connection

        self.conn = sqlite3.connect(path, timeout=15.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        configure_connection(self.conn, profile="network")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=15000")
        lock_path = Path(database_write_lock_path(str(path)))
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.controller = SQLiteWriteController(
            str(path), str(lock_path), f"benchmark:{id(self)}", retry_delay_ms=10
        )

    @contextmanager
    def remcard_transaction(self, *, source="benchmark", **_kwargs):
        with self.controller.transaction(self.conn, source=source) as cursor:
            yield cursor

    def fetch_one_remcard(self, query, params=(), **_kwargs):
        return self.conn.execute(query, params).fetchone()

    def fetch_all_remcard(self, query, params=(), **_kwargs):
        return self.conn.execute(query, params).fetchall()

    def execute_remcard(self, query, params=(), **_kwargs):
        return self.conn.execute(query, params)

    def run_write_operation(self, operation, *, source="benchmark", **_kwargs):
        with self.remcard_transaction(source=source) as cursor:
            return operation(cursor)

    def close(self):
        self.conn.close()


@contextmanager
def isolated_profile(root: Path):
    profile = root / "profile"
    paths = {
        "USERPROFILE": profile / "user",
        "HOME": profile / "user",
        "APPDATA": profile / "roaming",
        "LOCALAPPDATA": profile / "local",
        "REMCARD_LOCAL_LOGS_DIR": root / "logs",
        "REMCARD_BAZA_DIR": root / "central",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    previous = {key: os.environ.get(key) for key in paths}
    os.environ.update({key: str(value) for key, value in paths.items()})
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def create_synthetic_database(path: Path, history_rows: int) -> tuple[int, int]:
    from rem_card.app.unified_db_schema import ensure_unified_schema

    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=EXTRA")
        ensure_unified_schema(conn, allow_local_schema_upgrades=True)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('db_cycle_started_at', 'synthetic-benchmark')"
        )
        conn.execute("INSERT INTO patients(id, full_name) VALUES(1, 'SYNTHETIC BENCHMARK PATIENT')")
        conn.execute(
            """
            INSERT INTO admissions(
                id, patient_id, bed_number, history_number, admission_datetime
            ) VALUES(1, 1, 1, 'SYNTHETIC-BENCHMARK', ?)
            """,
            (SHIFT.isoformat(),),
        )
        admin_ids = []
        for index, role in enumerate(("doctor", "nurse")):
            order_id = int(
                conn.execute(
                    """
                    INSERT INTO orders(
                        admission_id, datetime, text, latin, drug_key, type,
                        status, is_committed, dose_value, dose_unit, frequency,
                        specific_times, revision, comment, last_modified_by
                    ) VALUES(1, ?, ?, ?, ?, 'medication', 'active', 1, 1, 'mg', 1, '[]', 0, '', ?)
                    """,
                    (
                        SHIFT.isoformat(),
                        f"Synthetic {role} order",
                        f"Synthetic {role}",
                        f"synthetic-{role}",
                        role,
                    ),
                ).lastrowid
            )
            admin_ids.append(
                int(
                    conn.execute(
                        """
                        INSERT INTO administrations(
                            order_id, planned_time, cell_role, status,
                            is_committed, version, volume_ml, comment, last_modified_by
                        ) VALUES(?, ?, 'single', 'planned', 1, 0, 0, '', ?)
                        """,
                        (order_id, (SHIFT + timedelta(hours=index + 1)).isoformat(), role),
                    ).lastrowid
                )
            )
        # No change-log triggers are installed for this synthetic table.  Its
        # size exercises reconciliation/fallback behavior on every delta pass.
        conn.execute(
            "CREATE TABLE synthetic_untracked_history(id INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO synthetic_untracked_history(id, payload) VALUES(?, ?)",
            (
                (index, f"SYNTHETIC-HISTORY-{index:06d}-" + "x" * 96)
                for index in range(1, history_rows + 1)
            ),
        )
        conn.commit()
    return admin_ids[0], admin_ids[1]


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def latency_summary(values: list[float]) -> dict[str, Any]:
    return {
        "samples": len(values),
        "p50_ms": round(percentile(values, 0.50), 3),
        "p95_ms": round(percentile(values, 0.95), 3),
        "max_ms": round(max(values, default=0.0), 3),
        "mean_ms": round(statistics.fmean(values) if values else 0.0, 3),
    }


def central_versions(path: Path, admin_ids: tuple[int, int]) -> tuple[int, int]:
    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute(
            "SELECT id, version FROM administrations WHERE id IN (?, ?) ORDER BY id",
            admin_ids,
        ).fetchall()
    return int(rows[0][1]), int(rows[1][1])


def central_cursor(path: Path) -> int:
    with closing(sqlite3.connect(path)) as conn:
        return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM change_log").fetchone()[0])


def concurrent_role_writes(
    path: Path,
    doctor_admin_id: int,
    nurse_admin_id: int,
    expected_versions: tuple[int, int],
    iteration: int,
) -> dict[str, float]:
    from rem_card.services.order_domain_service import OrderDomainService

    barrier = threading.Barrier(2)

    def write(role: str, admin_id: int, expected_version: int) -> tuple[str, float]:
        database = ClinicalConnection(path)
        try:
            service = OrderDomainService(database)
            service._legacy_statuses_sanitized = True
            barrier.wait(timeout=15)
            mark = (
                "nurse_executed"
                if (iteration + (role == "doctor")) % 2 == 0
                else "nurse_not_executed"
            )
            started = time.perf_counter()
            if role == "doctor":
                service.set_doctor_status(admin_id, mark, expected_version=expected_version)
            else:
                service.set_nurse_status(admin_id, mark, expected_version=expected_version)
            return role, (time.perf_counter() - started) * 1000.0
        finally:
            database.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda args: write(*args),
                (
                    ("doctor", doctor_admin_id, expected_versions[0]),
                    ("nurse", nurse_admin_id, expected_versions[1]),
                ),
            )
        )
    return dict(results)


def wait_for_replicas(
    replicas,
    *,
    target_cursor: int,
    expected_versions: tuple[int, int],
    admin_ids: tuple[int, int],
    timeout_sec: float = 30.0,
    detection_started_at: float | None = None,
) -> dict[str, float]:
    started = detection_started_at or time.perf_counter()
    detected: dict[str, float] = {}
    deadline = time.perf_counter() + timeout_sec
    while time.perf_counter() < deadline and len(detected) < len(replicas):
        for role, replica in replicas.items():
            if role in detected:
                continue
            cursor = int(replica.last_state.get("change_cursor") or 0)
            if cursor < target_cursor:
                continue
            row_versions = tuple(
                int(row[0])
                for row in replica.fetch_all(
                    "SELECT version FROM administrations WHERE id IN (?, ?) ORDER BY id",
                    admin_ids,
                )
            )
            if row_versions == expected_versions:
                detected[role] = (time.perf_counter() - started) * 1000.0
        if len(detected) < len(replicas):
            time.sleep(0.005)
    if len(detected) != len(replicas):
        missing = sorted(set(replicas) - set(detected))
        raise TimeoutError(f"replicas did not reach cursor {target_cursor}: {missing}")
    return detected


def project_replica_state(replica, admin_ids: tuple[int, int]):
    read_started = time.perf_counter()
    rows = replica.fetch_all(
        """
        SELECT a.id, a.comment, a.actual_time, a.version, o.status, o.revision
        FROM administrations a
        JOIN orders o ON o.id = a.order_id
        WHERE a.id IN (?, ?)
        ORDER BY a.id
        """,
        admin_ids,
    )
    read_ms = (time.perf_counter() - read_started) * 1000.0
    application_started = time.perf_counter()
    projection = tuple(
        {
            "admin_id": int(row[0]),
            "mark": str(row[1] or ""),
            "actual_time": str(row[2] or ""),
            "version": int(row[3] or 0),
            "order_status": str(row[4] or ""),
            "order_revision": int(row[5] or 0),
        }
        for row in rows
    )
    # Materialize the same immutable payload shape a view-model consumer sees.
    json.dumps(projection, ensure_ascii=False, sort_keys=True)
    application_ms = (time.perf_counter() - application_started) * 1000.0
    return projection, read_ms, application_ms


def logical_projection(path: Path, admin_ids: tuple[int, int]):
    with closing(sqlite3.connect(path)) as conn:
        return tuple(
            tuple(row)
            for row in conn.execute(
                """
                SELECT a.id, a.comment, a.actual_time, a.version, o.status, o.revision
                FROM administrations a
                JOIN orders o ON o.id = a.order_id
                WHERE a.id IN (?, ?)
                ORDER BY a.id
                """,
                admin_ids,
            ).fetchall()
        )


def run_benchmark(iterations: int, history_rows: int, workstation_layout: str = "separate") -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="remcard-concurrent-sync-") as temp_dir:
        root = Path(temp_dir)
        with isolated_profile(root):
            from rem_card.app.local_replica_sync import LocalReplicaSync
            from rem_card.app.local_replica_worker import (
                LocalReplicaWorkerClient, replica_snapshot_gate_path,
            )

            central_path = root / "central" / "archiv" / "rao_journal.db"
            admin_ids = create_synthetic_database(central_path, history_rows)
            replicas = {}
            for role in ("doctor", "nurse"):
                local_path = root / "replicas" / f"{role}.db"
                worker = LocalReplicaWorkerClient(
                    central_db_path=str(central_path),
                    local_db_path=str(local_path),
                )
                if workstation_layout == "separate":
                    # Production gates are scoped by workstation hostname.
                    # Simulate distinct PCs without changing source I/O or
                    # SQLite locking; both processes still run on this host.
                    worker.snapshot_gate_path = replica_snapshot_gate_path(
                        str(central_path), host=f"synthetic-{role}"
                    )
                replicas[role] = LocalReplicaSync(
                    central_db_path=str(central_path),
                    local_db_path=str(local_path),
                    worker_client=worker,
                    sync_timeout_sec=30.0,
                )
            commit_latencies = {"doctor": [], "nurse": []}
            detection_latencies = {"doctor": [], "nurse": []}
            read_latencies = {"doctor": [], "nurse": []}
            application_latencies = {"doctor": [], "nurse": []}
            final_projections = {}
            try:
                for replica in replicas.values():
                    replica.start()
                baseline_cursor = central_cursor(central_path)
                wait_for_replicas(
                    replicas,
                    target_cursor=baseline_cursor,
                    expected_versions=(0, 0),
                    admin_ids=admin_ids,
                )

                for iteration in range(iterations):
                    expected = central_versions(central_path, admin_ids)
                    iteration_commits = concurrent_role_writes(
                        central_path,
                        admin_ids[0],
                        admin_ids[1],
                        expected,
                        iteration,
                    )
                    for role, latency in iteration_commits.items():
                        commit_latencies[role].append(latency)

                    detection_started_at = time.perf_counter()
                    target_versions = (expected[0] + 1, expected[1] + 1)
                    target_cursor = central_cursor(central_path)
                    detected = wait_for_replicas(
                        replicas,
                        target_cursor=target_cursor,
                        expected_versions=target_versions,
                        admin_ids=admin_ids,
                        detection_started_at=detection_started_at,
                    )
                    for role, latency in detected.items():
                        detection_latencies[role].append(latency)
                    for role, replica in replicas.items():
                        projection, read_ms, application_ms = project_replica_state(replica, admin_ids)
                        final_projections[role] = projection
                        read_latencies[role].append(read_ms)
                        application_latencies[role].append(application_ms)

                source_projection = logical_projection(central_path, admin_ids)
                replica_projection_rows = {
                    role: tuple(
                        (
                            row["admin_id"], row["mark"], row["actual_time"],
                            row["version"], row["order_status"], row["order_revision"],
                        )
                        for row in projection
                    )
                    for role, projection in final_projections.items()
                }
                integrity = {}
                for name, db_path in {
                    "central": central_path,
                    **{
                        role: Path(replica.local_db_path)
                        for role, replica in replicas.items()
                    },
                }.items():
                    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
                        integrity[name] = conn.execute("PRAGMA integrity_check").fetchone()[0]

                equivalence = {
                    role: projection == source_projection
                    for role, projection in replica_projection_rows.items()
                }
                read_only = {
                    role: int(replica.fetch_one("PRAGMA query_only")[0]) == 1
                    for role, replica in replicas.items()
                }
                if not all(value == "ok" for value in integrity.values()):
                    raise RuntimeError(f"integrity verification failed: {integrity}")
                if not all(equivalence.values()) or not all(read_only.values()):
                    raise RuntimeError("replica state or read-only verification failed")

                return {
                    "benchmark": "concurrent_clinical_sync",
                    "python": sys.version,
                    "platform": platform.platform(),
                    "sqlite": sqlite3.sqlite_version,
                    "iterations": iterations,
                    "workstation_layout": workstation_layout,
                    "workstation_simulation": "per-host snapshot gates only; processes and files are local",
                    "replica_poll_interval_sec": {
                        role: replica.sync_interval_sec for role, replica in replicas.items()
                    },
                    "fast_sync_after_commit": False,
                    "write_controller": "SQLiteWriteController + FileWriteLock",
                    "history": {
                        "untracked_rows": history_rows,
                        "payload_bytes_each": 121,
                    },
                    "measurement": {
                        "commit": "domain service call including SQLite lock wait",
                        "detection": "last central commit return to replica cursor and versions visible",
                        "replica_read": "read-only local replica SQL query",
                        "state_projection_application": "immutable state projection only; no GUI widget",
                    },
                    "latency": {
                        role: {
                            "commit": latency_summary(commit_latencies[role]),
                            "detection": latency_summary(detection_latencies[role]),
                            "replica_read": latency_summary(read_latencies[role]),
                            "state_projection_application": latency_summary(
                                application_latencies[role]
                            ),
                        }
                        for role in replicas
                    },
                    "integrity": integrity,
                    "logical_equivalence": equivalence,
                    "replicas_read_only": read_only,
                    "final_change_cursor": central_cursor(central_path),
                }
            finally:
                for replica in replicas.values():
                    replica.stop()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=24)
    parser.add_argument("--history-rows", type=int, default=6000)
    parser.add_argument("--workstation-layout", choices=("separate", "shared"), default="separate")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.iterations < 20:
        parser.error("--iterations must be at least 20")
    if args.history_rows < 6000:
        parser.error("--history-rows must be at least 6000")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    result = run_benchmark(args.iterations, args.history_rows, args.workstation_layout)
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
