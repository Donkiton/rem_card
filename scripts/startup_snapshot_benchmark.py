#!/usr/bin/env python3
"""Benchmark one startup snapshot against the previous two-read sequence.

This is an algorithm-level benchmark.  It executes the real RemCard startup
guard and snapshot worker functions with scoped APSW connection adapters so a
custom SQLite VFS can inject and count source-read latency.  No production
module is modified or permanently monkeypatched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sqlite3
import statistics
import struct
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEPENDENCY_DIR = PROJECT_ROOT / "tmp" / "startup-benchmark-deps"
DEFAULT_OUTPUT = (
    PROJECT_ROOT / "tmp" / "bench-results" / "startup_snapshot_benchmark.json"
)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from _local_rem_card_bootstrap import bootstrap_local_rem_card  # noqa: E402

bootstrap_local_rem_card()


def _load_apsw(dependency_dir: Path):
    if str(dependency_dir) not in sys.path:
        sys.path.insert(0, str(dependency_dir))
    try:
        import apsw  # type: ignore
    except ImportError as exc:
        raise SystemExit(
            "APSW is required only for this benchmark. Install the SQLite "
            "3.40 build with:\n"
            f'  {sys.executable} -m pip install --target "{dependency_dir}" '
            "apsw==3.40.1.0"
        ) from exc
    return apsw


class ReadStats:
    def __init__(self, delay_seconds: float):
        self.delay_seconds = delay_seconds
        self.calls = 0
        self.requested_bytes = 0
        self.injected_delay_seconds = 0.0
        self.actual_wait_seconds = 0.0

    def record(self, amount: int) -> None:
        self.calls += 1
        self.requested_bytes += int(amount)
        self.injected_delay_seconds += self.delay_seconds

    def wait(self) -> None:
        if not self.delay_seconds:
            return
        started = time.perf_counter()
        # Sub-millisecond sleep resolution varies substantially on Windows.
        # A bounded spin keeps the configured per-read latency reproducible.
        if self.delay_seconds < 0.010:
            deadline = started + self.delay_seconds
            while time.perf_counter() < deadline:
                pass
        else:
            time.sleep(self.delay_seconds)
        self.actual_wait_seconds += time.perf_counter() - started

    def as_dict(self) -> dict[str, Any]:
        return {
            "xread_calls": self.calls,
            "requested_bytes": self.requested_bytes,
            "injected_delay_seconds": round(self.injected_delay_seconds, 6),
            "actual_wait_seconds": round(self.actual_wait_seconds, 6),
        }


def _make_latency_vfs(apsw, stats: ReadStats, label: str):
    vfs_name = f"remcard_latency_{label}_{uuid.uuid4().hex}"

    class LatencyFile(apsw.VFSFile):
        def __init__(self, base_vfs: str, filename, flags):
            super().__init__(base_vfs, filename, flags)

        def xRead(self, amount: int, offset: int):
            stats.record(amount)
            stats.wait()
            return super().xRead(amount, offset)

    class LatencyVFS(apsw.VFS):
        def __init__(self):
            self.base_vfs = ""
            super().__init__(vfs_name, self.base_vfs)

        def xOpen(self, name, flags):
            return LatencyFile(self.base_vfs, name, flags)

    return LatencyVFS(), vfs_name


class ApswConnectionAdapter:
    """sqlite3-compatible surface used by the two production functions."""

    def __init__(self, connection, apsw):
        self._connection = connection
        self._apsw = apsw
        self._closed = False

    def execute(self, sql: str, parameters=()):
        return self._connection.execute(sql, parameters)

    def backup(self, target, *, pages: int, progress, sleep: float) -> None:
        with target._connection.backup(
            "main", self._connection, "main"
        ) as backup:
            while not backup.done:
                try:
                    done = backup.step(pages)
                except (self._apsw.BusyError, self._apsw.LockedError):
                    time.sleep(sleep)
                    continue
                progress(
                    self._apsw.SQLITE_DONE if done else self._apsw.SQLITE_OK,
                    backup.remaining,
                    backup.pagecount,
                )

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True


@contextmanager
def _source_factory(apsw, source_path: Path, delay_seconds: float, label: str):
    stats = ReadStats(delay_seconds)
    vfs, vfs_name = _make_latency_vfs(apsw, stats, label)

    def open_source(_path: str, *args, **kwargs):
        connection = apsw.Connection(
            str(source_path),
            flags=apsw.SQLITE_OPEN_READONLY,
            vfs=vfs_name,
        )
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA temp_store = MEMORY")
        connection.execute("PRAGMA cache_size = -8192")
        connection.execute("PRAGMA mmap_size = 0")
        connection.execute("PRAGMA busy_timeout = 250")
        return ApswConnectionAdapter(connection, apsw)

    try:
        yield open_source, stats
    finally:
        vfs.unregister()


def _open_local_adapter(apsw, path: str, *args, **kwargs):
    connection = apsw.Connection(str(path))
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA temp_store = MEMORY")
    connection.execute("PRAGMA cache_size = -32768")
    connection.execute("PRAGMA mmap_size = 134217728")
    connection.execute("PRAGMA busy_timeout = 5000")
    return ApswConnectionAdapter(connection, apsw)


def _deterministic_payload(index: int, size: int) -> bytes:
    seed = hashlib.sha256(
        f"remcard-startup-benchmark:{index}".encode("ascii")
    ).digest()
    return (seed * math.ceil(size / len(seed)))[:size]


def _create_database(path: Path, target_mib: float) -> dict[str, Any]:
    for candidate in (
        path,
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
        Path(f"{path}-journal"),
    ):
        if candidate.exists():
            raise FileExistsError(
                "benchmark refuses to overwrite existing database: "
                f"{candidate}"
            )
    payload_size = 32 * 1024
    target_bytes = int(target_mib * 1024 * 1024)
    row_count = max(1, math.ceil(target_bytes / payload_size))
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA page_size = 4096")
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = OFF")
        connection.execute("PRAGMA application_id = 1380270916")
        connection.execute("PRAGMA user_version = 1")
        connection.executescript(
            """
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY);
            CREATE TABLE change_log(
                id INTEGER PRIMARY KEY,
                changed_at TEXT NOT NULL
            );
            CREATE TABLE benchmark_payload(
                id INTEGER PRIMARY KEY,
                label TEXT NOT NULL,
                payload BLOB NOT NULL
            );
            CREATE INDEX benchmark_payload_label_idx
                ON benchmark_payload(label);
            """
        )
        connection.executemany(
            "INSERT INTO meta(key, value) VALUES(?, ?)",
            (
                ("db_cycle_started_at", "benchmark-cycle-1"),
                ("unified_schema_fastpath_rev", "benchmark-r1"),
            ),
        )
        connection.execute("INSERT INTO schema_migrations(version) VALUES(1)")
        connection.execute(
            "INSERT INTO change_log(id, changed_at) "
            "VALUES(1, '2026-01-01T00:00:00Z')"
        )
        rows = (
            (
                index,
                f"row-{index:06d}",
                _deterministic_payload(index, payload_size),
            )
            for index in range(1, row_count + 1)
        )
        connection.executemany(
            "INSERT INTO benchmark_payload(id, label, payload) "
            "VALUES(?, ?, ?)",
            rows,
        )
        connection.commit()
        connection.execute("VACUUM")
        connection.execute("PRAGMA journal_mode = DELETE")
    finally:
        connection.close()
    return {
        "requested_mib": target_mib,
        "actual_bytes": path.stat().st_size,
        "actual_mib": round(path.stat().st_size / 1024 / 1024, 3),
        "page_size": 4096,
        "payload_rows": row_count,
        "payload_bytes_per_row": payload_size,
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _database_validation(path: Path) -> dict[str, Any]:
    logical = hashlib.sha256()
    schema = hashlib.sha256()
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        quick_check = str(
            connection.execute("PRAGMA quick_check").fetchone()[0]
        )
        schema_rows = list(
            connection.execute(
                "SELECT type, name, tbl_name, COALESCE(sql, '') "
                "FROM sqlite_master ORDER BY type, name"
            )
        )
        for row in schema_rows:
            schema.update(
                json.dumps(tuple(row), separators=(",", ":")).encode("utf-8")
            )
        row_count = 0
        payload_bytes = 0
        for row_id, label, payload in connection.execute(
            "SELECT id, label, payload FROM benchmark_payload ORDER BY id"
        ):
            encoded_label = label.encode("utf-8")
            logical.update(
                struct.pack(
                    ">QII", int(row_id), len(encoded_label), len(payload)
                )
            )
            logical.update(encoded_label)
            logical.update(payload)
            row_count += 1
            payload_bytes += len(payload)
    finally:
        connection.close()
    return {
        "quick_check": quick_check,
        "file_sha256": _file_sha256(path),
        "schema_sha256": schema.hexdigest(),
        "logical_sha256": logical.hexdigest(),
        "payload_rows": row_count,
        "payload_bytes": payload_bytes,
    }


class StageTimeline:
    def __init__(self):
        self.started = time.perf_counter()
        self.events: list[dict[str, Any]] = []

    def callback(self, value) -> None:
        stage = value.get("stage") if isinstance(value, dict) else value
        self.events.append(
            {
                "stage": str(stage),
                "elapsed_seconds": round(
                    time.perf_counter() - self.started, 6
                ),
            }
        )

    def duration(self, start_stage: str, end_stage: str) -> float | None:
        start = next(
            (
                event["elapsed_seconds"]
                for event in self.events
                if event["stage"] == start_stage
            ),
            None,
        )
        if start is None:
            return None
        end = next(
            (
                event["elapsed_seconds"]
                for event in self.events
                if event["stage"] == end_stage
                and event["elapsed_seconds"] >= start
            ),
            None,
        )
        return None if end is None else round(end - start, 6)


class InProcessWorkerClient:
    """LocalReplicaSync client running the real worker function in-process."""

    def __init__(
        self, worker, source: Path, lock_root: Path, timeline: StageTimeline
    ):
        self._worker = worker
        self._source = source
        self._lock_root = lock_root
        self._timeline = timeline

    def sync(self, *, local_state, temp_db_path, **_kwargs):
        return self._worker._sync_snapshot_with_network_lease(
            {
                "central_db_path": str(self._source),
                "temp_db_path": str(temp_db_path),
                "local_state": dict(local_state or {}),
                "snapshot_lease_path": str(self._lock_root / "snapshot.lock"),
                "snapshot_gate_path": str(
                    self._lock_root / "snapshot_gate.lock"
                ),
                "rotation_lock_path": str(self._lock_root / "rotation.lock"),
                "writer_lock_path": str(self._lock_root / "writer.lock"),
                "lease_duration_sec": 60.0,
            },
            progress_callback=self._timeline.callback,
        )

    def can_cleanup_temp_path(self, _temp_db_path: str) -> bool:
        return True

    def close(self, **_kwargs) -> bool:
        return True


def _stats_delta(
    after: dict[str, Any], before: dict[str, Any]
) -> dict[str, Any]:
    return {
        "xread_calls": after["xread_calls"] - before["xread_calls"],
        "requested_bytes": after["requested_bytes"]
        - before["requested_bytes"],
        "injected_delay_seconds": round(
            after["injected_delay_seconds"] - before["injected_delay_seconds"],
            6,
        ),
        "actual_wait_seconds": round(
            after["actual_wait_seconds"] - before["actual_wait_seconds"], 6
        ),
    }


def _require_owned_child(path: Path, owned_root: Path) -> Path:
    resolved = path.resolve()
    root = owned_root.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(
            f"benchmark cleanup target is outside owned root: {resolved}"
        ) from exc
    if not relative.parts:
        raise RuntimeError(
            "benchmark refuses to recursively delete its owned root"
        )
    return resolved


def _remove_owned_tree(path: Path, owned_root: Path) -> None:
    target = _require_owned_child(path, owned_root)
    if target.exists():
        shutil.rmtree(target)


def _run_old_path(
    apsw,
    source: Path,
    destination: Path,
    delay_seconds: float,
    run_number: int,
):
    from rem_card.app import local_replica_worker as worker
    from rem_card.app import startup_db_guard as guard
    from rem_card.app.local_replica_sync import LocalReplicaSync

    with _source_factory(
        apsw, source, delay_seconds, f"old_guard_{run_number}"
    ) as (open_source, guard_stats):
        started = time.perf_counter()
        # Scope the adapter to this call. sqlite3.connect is restored on exit.
        with (
            patch.object(guard.sqlite3, "connect", side_effect=open_source),
            patch.object(
                guard,
                "configure_connection",
                side_effect=lambda *args, **kwargs: None,
            ),
        ):
            guard_result = guard._check_quick_direct(
                str(source), capture_admission=True
            )
        admission_seconds = time.perf_counter() - started
    if not guard_result[0] or guard_result[2]:
        raise RuntimeError(f"old startup guard failed: {guard_result}")

    timeline = StageTimeline()
    with _source_factory(
        apsw, source, delay_seconds, f"old_replica_{run_number}"
    ) as (
        open_source,
        replica_stats,
    ):
        replica_started = time.perf_counter()
        worker_client = InProcessWorkerClient(
            worker, source, destination.parent / "old_worker_locks", timeline
        )
        replica = LocalReplicaSync(
            central_db_path=str(source),
            local_db_path=str(destination),
            sync_interval_sec=60.0,
            worker_client=worker_client,
        )
        with (
            patch.object(
                worker, "_open_central_readonly", side_effect=open_source
            ),
            patch.object(
                worker,
                "_open_temp_replica",
                side_effect=lambda path: _open_local_adapter(apsw, path),
            ),
        ):
            synced = replica.sync_once()
            ready = replica.is_ready(max_stale_sec=10.0)
        replica_seconds = time.perf_counter() - replica_started
    health = replica.health_snapshot()
    replica.stop()
    if not synced or not ready:
        raise RuntimeError(f"old replica was not ready: {health}")
    return {
        "admission_seconds": round(admission_seconds, 6),
        "independent_replica_seconds": round(replica_seconds, 6),
        "admission_and_initial_replica_ready_seconds": round(
            admission_seconds + replica_seconds, 6
        ),
        "backup_seconds": timeline.duration("backup", "checkpoint"),
        "source_reads": {
            "admission_quick_check": guard_stats.as_dict(),
            "independent_replica": replica_stats.as_dict(),
            "combined": {
                "xread_calls": guard_stats.calls + replica_stats.calls,
                "requested_bytes": guard_stats.requested_bytes
                + replica_stats.requested_bytes,
                "injected_delay_seconds": round(
                    guard_stats.injected_delay_seconds
                    + replica_stats.injected_delay_seconds,
                    6,
                ),
                "actual_wait_seconds": round(
                    guard_stats.actual_wait_seconds
                    + replica_stats.actual_wait_seconds,
                    6,
                ),
            },
        },
        "stages": timeline.events,
        "replica_ready": ready,
        "replica_state": health["state"],
        "validation": _database_validation(destination),
    }


def _run_new_path(
    apsw, source: Path, workspace: Path, delay_seconds: float, run_number: int
):
    from rem_card.app import local_replica_worker as worker
    from rem_card.app import role_admission, startup_snapshot
    from rem_card.app.local_replica_sync import LocalReplicaSync

    workspace.mkdir(parents=True, exist_ok=False)
    timeline = StageTimeline()
    role_admission.invalidate_role_admission()
    with _source_factory(
        apsw, source, delay_seconds, f"new_snapshot_{run_number}"
    ) as (
        open_source,
        stats,
    ):
        started = time.perf_counter()
        try:
            with (
                patch.object(
                    worker, "_open_central_readonly", side_effect=open_source
                ),
                patch.object(
                    worker,
                    "_open_temp_replica",
                    side_effect=lambda path: _open_local_adapter(apsw, path),
                ),
            ):
                ok, payload, corrupt = startup_snapshot.check_snapshot(
                    str(source),
                    str(workspace),
                    lease_duration_sec=60.0,
                    progress=timeline.callback,
                )
                admission_seconds = time.perf_counter() - started
                admission_reads = stats.as_dict()
                if not ok or corrupt:
                    raise RuntimeError(
                        "new startup snapshot failed: "
                        f"{(ok, payload, corrupt)}"
                    )
                payload_receipt = json.loads(payload)
                snapshot_path = Path(payload_receipt["snapshot"]["path"])
                role_admission.remember_full_scan(
                    payload_receipt["admission"],
                    generation=role_admission.admission_generation(),
                    checked_monotonic=time.monotonic(),
                    checked_at_epoch=time.time(),
                    snapshot=payload_receipt["snapshot"],
                )
                remembered, reason = role_admission.recent_admission(
                    str(source)
                )
                if remembered is None:
                    raise RuntimeError(
                        f"startup admission was not retained: {reason}"
                    )

                replica_started = time.perf_counter()
                replica_path = workspace / "initial_replica.db"
                candidate = startup_snapshot.copy_for_replica(
                    remembered, str(replica_path)
                )
                if not candidate:
                    raise RuntimeError(
                        "copy_for_replica rejected the validated "
                        "startup snapshot"
                    )
                worker_client = InProcessWorkerClient(
                    worker, source, workspace / "confirmation_locks", timeline
                )
                replica = LocalReplicaSync(
                    central_db_path=str(source),
                    local_db_path=str(replica_path),
                    sync_interval_sec=60.0,
                    worker_client=worker_client,
                )
                replica.install_startup_snapshot(candidate)
                synced = replica.sync_once()
                ready = replica.is_ready(max_stale_sec=10.0)
                replica_pipeline_seconds = (
                    time.perf_counter() - replica_started
                )
                ready_total_seconds = time.perf_counter() - started
                health = replica.health_snapshot()
                total_reads = stats.as_dict()
                replica.stop()
        finally:
            role_admission.invalidate_role_admission()
    if not synced or not ready:
        raise RuntimeError(
            f"new replica freshness confirmation failed: {health}"
        )
    replica_validation = _database_validation(replica_path)
    return {
        "admission_seconds": round(admission_seconds, 6),
        "local_initial_replica_publish_and_confirmation_seconds": round(
            replica_pipeline_seconds, 6
        ),
        "admission_and_initial_replica_ready_seconds": round(
            ready_total_seconds, 6
        ),
        "backup_seconds": timeline.duration("backup", "checkpoint"),
        "source_reads": {
            "admission_snapshot": admission_reads,
            "freshness_confirmation": _stats_delta(
                total_reads, admission_reads
            ),
            "combined": total_reads,
        },
        "stages": timeline.events,
        "replica_ready": ready,
        "replica_state": health["state"],
        "validation": _database_validation(snapshot_path),
        "initial_replica_validation": replica_validation,
        "manifest_sha256": payload_receipt["snapshot"]["sha256"],
    }


def _median(runs: list[dict[str, Any]], path: tuple[str, ...]) -> float | int:
    values = []
    for run in runs:
        value: Any = run
        for key in path:
            value = value[key]
        values.append(value)
    median = statistics.median(values)
    if all(
        isinstance(value, int) and not isinstance(value, bool)
        for value in values
    ):
        return int(median)
    return round(float(median), 6)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--database-mib", type=float, default=15.0)
    parser.add_argument("--read-delay-ms", type=float, default=1.75)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--dependency-dir", type=Path, default=DEFAULT_DEPENDENCY_DIR
    )
    parser.add_argument("--keep-copies", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.runs < 1 or args.runs > 9:
        raise SystemExit("--runs must be between 1 and 9")
    if not 1.0 <= args.database_mib <= 256.0:
        raise SystemExit("--database-mib must be between 1 and 256")
    if not 0.0 <= args.read_delay_ms <= 25.0:
        raise SystemExit("--read-delay-ms must be between 0 and 25")

    apsw = _load_apsw(args.dependency_dir.resolve())
    if apsw.sqlitelibversion() != sqlite3.sqlite_version:
        raise SystemExit(
            "Benchmark APSW must use the same SQLite version as Python "
            "sqlite3: "
            f"APSW={apsw.sqlitelibversion()} sqlite3={sqlite3.sqlite_version}"
        )

    output_path = args.output.resolve()
    output_root = output_path.parent
    output_root.mkdir(parents=True, exist_ok=True)
    owned_root = Path(
        tempfile.mkdtemp(prefix="remcard_startup_benchmark_", dir=output_root)
    ).resolve()
    source = owned_root / "synthetic_medical.db"
    database = _create_database(source, args.database_mib)
    source_before = _database_validation(source)
    source_file_before = source_before["file_sha256"]
    delay_seconds = args.read_delay_ms / 1000.0

    runs: list[dict[str, Any]] = []
    for run_number in range(1, args.runs + 1):
        run_root = owned_root / f"run-{run_number}"
        run_root.mkdir(parents=True, exist_ok=False)
        old_destination = run_root / "old_initial_replica.db"
        new_workspace = run_root / "new_snapshot_workspace"
        # Alternate order to limit systematic temperature/background-load bias.
        if run_number % 2:
            old_result = _run_old_path(
                apsw, source, old_destination, delay_seconds, run_number
            )
            new_result = _run_new_path(
                apsw, source, new_workspace, delay_seconds, run_number
            )
            order = ["old", "new"]
        else:
            new_result = _run_new_path(
                apsw, source, new_workspace, delay_seconds, run_number
            )
            old_result = _run_old_path(
                apsw, source, old_destination, delay_seconds, run_number
            )
            order = ["new", "old"]

        logical = source_before["logical_sha256"]
        schema = source_before["schema_sha256"]
        validations = (
            old_result["validation"],
            new_result["validation"],
            new_result["initial_replica_validation"],
        )
        equivalent = all(
            item["quick_check"] == "ok"
            and item["logical_sha256"] == logical
            and item["schema_sha256"] == schema
            for item in validations
        )
        if not equivalent:
            raise RuntimeError(
                f"run {run_number} produced a non-equivalent database"
            )
        if not old_result["replica_ready"] or not new_result["replica_ready"]:
            raise RuntimeError(
                f"run {run_number} did not produce a ready local replica"
            )
        runs.append(
            {
                "run": run_number,
                "order": order,
                "old": old_result,
                "new": new_result,
                "logical_equivalence": equivalent,
            }
        )
        if not args.keep_copies:
            _remove_owned_tree(run_root, owned_root)

    source_after = _database_validation(source)
    if source_after["file_sha256"] != source_file_before:
        raise RuntimeError(
            "synthetic source database changed during benchmark"
        )

    old_admission = _median(runs, ("old", "admission_seconds"))
    old_ready = _median(
        runs, ("old", "admission_and_initial_replica_ready_seconds")
    )
    new_admission = _median(runs, ("new", "admission_seconds"))
    new_ready = _median(
        runs, ("new", "admission_and_initial_replica_ready_seconds")
    )
    new_backup = _median(runs, ("new", "backup_seconds"))
    saved_seconds = old_ready - new_ready
    improvement_percent = (
        (saved_seconds / old_ready * 100.0) if old_ready else 0.0
    )

    budget_evidence: dict[str, Any] = {
        "simulated_smb": True,
        "basis": (
            "median wall-clock backup stage with injected delay on each "
            "real SQLite source xRead"
        ),
        "median_new_backup_seconds": new_backup,
        "reported": new_backup > 6.0,
    }
    if new_backup > 6.0:
        budget_evidence.update(
            {
                "six_second_budget": "fails",
                "thirty_second_budget": "passes"
                if new_ready <= 30.0
                else "fails",
                "median_new_admission_seconds": new_admission,
                "median_new_initial_replica_ready_seconds": new_ready,
            }
        )

    report = {
        "benchmark": (
            "RemCard startup snapshot: two source scans vs one source snapshot"
        ),
        "scope": (
            "algorithm-level; real SQLite operations and production startup "
            "functions with scoped APSW adapters; not GUI or real SMB"
        ),
        "generated_at_epoch": time.time(),
        "environment": {
            "python": sys.version.split()[0],
            "sqlite": sqlite3.sqlite_version,
            "apsw": apsw.apswversion(),
            "platform": sys.platform,
        },
        "configuration": {
            "runs": args.runs,
            "read_delay_ms_per_xread": args.read_delay_ms,
            "backup_pages_per_step": 256,
            "fresh_source_connection_per_phase": True,
            "new_replica_readiness": (
                "copy_for_replica + "
                "LocalReplicaSync.install_startup_snapshot + "
                "sync_once unchanged-state confirmation + is_ready"
            ),
            "source_path": str(source),
            "owned_workspace_root": str(owned_root),
            "output_path": str(output_path),
        },
        "database": database,
        "source_validation": source_after,
        "runs": runs,
        "medians": {
            "old_admission_seconds": old_admission,
            "old_admission_and_initial_replica_ready_seconds": old_ready,
            "new_admission_seconds": new_admission,
            "new_admission_and_initial_replica_ready_seconds": new_ready,
            "new_backup_seconds": new_backup,
            "seconds_saved_to_initial_replica_ready": round(saved_seconds, 6),
            "improvement_percent_initial_replica_ready": round(
                improvement_percent, 3
            ),
            "old_source_xread_calls": _median(
                runs, ("old", "source_reads", "combined", "xread_calls")
            ),
            "new_source_xread_calls": _median(
                runs, ("new", "source_reads", "combined", "xread_calls")
            ),
            "old_source_requested_bytes": _median(
                runs, ("old", "source_reads", "combined", "requested_bytes")
            ),
            "new_source_requested_bytes": _median(
                runs, ("new", "source_reads", "combined", "requested_bytes")
            ),
        },
        "simulated_budget_evidence": budget_evidence,
        "limits": [
            (
                "Artificial latency is deterministic per SQLite VFS xRead; "
                "it does not model SMB jitter, reconnects, oplocks, "
                "antivirus, or concurrent writers."
            ),
            (
                "The benchmark uses real production guard/snapshot "
                "functions through scoped APSW adapters, but it does not "
                "launch the GUI or worker subprocess."
            ),
            (
                "Local copies, quick_check, hashing, lock files, and fsync "
                "execute on the benchmark host and are included where the "
                "production function performs them."
            ),
            (
                "This captured setup uses Python 3.10 and SQLite 3.40.1; "
                "the project build interpreter uses Python 3.11 and SQLite "
                "3.38.4, which was not benchmarked because an exact APSW "
                "3.38.4 package is unavailable."
            ),
        ],
    }
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"output": str(output_path), "medians": report["medians"]},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
