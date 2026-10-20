#!/usr/bin/env python3
"""Reproducible synthetic benchmark for full and incremental replicas.

The benchmark invokes the production snapshot worker, delta collector and delta
applier.  A scoped APSW VFS adds latency to SQLite xRead calls made against the
synthetic source only.  Local cache cloning and candidate validation use the
normal local filesystem and no artificial delay.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pickle
import platform
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEPENDENCY_DIR = PROJECT_ROOT / "tmp" / "startup-benchmark-deps"
DEFAULT_OUTPUT = (
    PROJECT_ROOT / "tmp" / "bench-results" / "replica_delta_benchmark.json"
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
            "APSW is required only for this benchmark. For the repository's "
            "default Python 3.10 runtime install the SQLite 3.40 build with:\n"
            f'  {sys.executable} -m pip install --target "{dependency_dir}" '
            "apsw==3.40.1.0"
        ) from exc
    return apsw


class ReadStats:
    def __init__(self, delay_seconds: float):
        self.delay_seconds = float(delay_seconds)
        self.calls = 0
        self.requested_bytes = 0
        self.injected_delay_seconds = 0.0
        self.actual_wait_seconds = 0.0

    def record(self, amount: int) -> None:
        self.calls += 1
        self.requested_bytes += int(amount)
        self.injected_delay_seconds += self.delay_seconds

    def wait(self) -> None:
        if self.delay_seconds <= 0:
            return
        started = time.perf_counter()
        # Windows sleep granularity is too coarse for a stable 1.75 ms delay.
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
    name = f"remcard_delta_latency_{label}_{uuid.uuid4().hex}"

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
            super().__init__(name, self.base_vfs)

        def xOpen(self, filename, flags):
            return LatencyFile(self.base_vfs, filename, flags)

    return LatencyVFS(), name


class ApswCursorAdapter:
    def __init__(self, cursor):
        self._cursor = cursor

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._cursor)

    def fetchone(self):
        try:
            return next(self._cursor)
        except StopIteration:
            return None

    def fetchall(self):
        return list(self._cursor)

    def fetchmany(self, size: int = 1):
        rows = []
        for _ in range(max(0, int(size))):
            try:
                rows.append(next(self._cursor))
            except StopIteration:
                break
        return rows


class ApswConnectionAdapter:
    """sqlite3-compatible surface needed by the production replica pipeline."""

    def __init__(self, connection, apsw):
        self._connection = connection
        self._apsw = apsw
        self._closed = False

    @property
    def in_transaction(self) -> bool:
        return not bool(self._connection.getautocommit())

    def execute(self, sql: str, parameters=()):
        return ApswCursorAdapter(self._connection.execute(sql, parameters))

    def executemany(self, sql: str, parameters):
        return ApswCursorAdapter(self._connection.executemany(sql, parameters))

    def backup(self, target, *, pages: int, progress, sleep: float) -> None:
        with target._connection.backup("main", self._connection, "main") as backup:
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
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA cache_size=-8192")
        connection.execute("PRAGMA mmap_size=0")
        connection.execute("PRAGMA busy_timeout=250")
        return ApswConnectionAdapter(connection, apsw)

    try:
        yield open_source, stats
    finally:
        vfs.unregister()


def _open_local_adapter(apsw, path: str, *args, readonly: bool = False, **kwargs):
    flags = (
        apsw.SQLITE_OPEN_READONLY
        if readonly
        else apsw.SQLITE_OPEN_READWRITE | apsw.SQLITE_OPEN_CREATE
    )
    connection = apsw.Connection(str(path), flags=flags)
    if readonly:
        connection.execute("PRAGMA query_only=ON")
    else:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA temp_store=MEMORY")
    connection.execute("PRAGMA cache_size=-32768")
    connection.execute("PRAGMA mmap_size=134217728")
    connection.execute("PRAGMA busy_timeout=5000")
    return ApswConnectionAdapter(connection, apsw)


def _local_sqlite_facade(apsw):
    def connect(database, *args, **kwargs):
        raw = str(database)
        if raw.startswith("file:"):
            # replica_refresh uses mode=ro and an absolute file URI.
            from urllib.parse import unquote, urlsplit

            parsed = urlsplit(raw)
            path = unquote(parsed.path)
            if os.name == "nt" and path.startswith("/") and len(path) > 3:
                path = path[1:]
            return _open_local_adapter(apsw, path, readonly=True)
        return _open_local_adapter(apsw, raw, readonly=False)

    return SimpleNamespace(connect=connect, Error=sqlite3.Error)


def _deterministic_text(index: int, size: int) -> str:
    seed = hashlib.sha256(f"remcard-delta-benchmark:{index}".encode("ascii")).hexdigest()
    prefix = f"SYNTHETIC PATIENT {index:06d} HISTORICAL DATA | "
    return (prefix + seed * math.ceil(size / len(seed)))[:size]


def _create_database(path: Path, target_mib: float, history_rows: int = 0) -> dict[str, Any]:
    from rem_card.app.unified_db_schema import ensure_unified_schema

    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA page_size=4096")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA foreign_keys=ON")
        ensure_unified_schema(connection, allow_local_schema_upgrades=True)
        connection.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
            ("db_cycle_started_at", "synthetic-benchmark-cycle-1"),
        )
        payload_size = 32 * 1024
        target_bytes = int(target_mib * 1024 * 1024)
        # The unified schema indexes full_name, so the large synthetic value is
        # represented in both the patients table and an index B-tree. Account
        # for both copies so --database-mib targets physical database size.
        patient_count = max(
            8,
            math.ceil((target_bytes - 1024 * 1024) / (payload_size * 2)),
        )
        connection.executemany(
            "INSERT INTO patients(full_name, admission_uid, birth_date, last_name, first_name) "
            "VALUES(?,?,?,?,?)",
            (
                (
                    _deterministic_text(index, payload_size),
                    f"SYN-{index:06d}",
                    "1970-01-01",
                    f"Synthetic-{index:06d}",
                    "Benchmark",
                )
                for index in range(1, patient_count + 1)
            ),
        )
        connection.execute(
            "INSERT INTO admissions(patient_id,bed_number,history_number,admission_datetime,"
            "patient_age,patient_gender,diagnosis_text) VALUES(1,1,'SYN-HIST-1',"
            "'2026-01-01T00:00:00',55,'synthetic','synthetic benchmark')"
        )
        connection.executemany(
            "INSERT INTO vitals(admission_id,datetime,sys,dia,pulse,temp,spo2,rr,gcs,cvp) "
            "VALUES(1,?,?,?,?,?,?,?,?,?)",
            (
                (f"2026-01-01T{hour:02d}:00:00", 120 + hour, 70, 70 + hour, 36.6, 98, 16, 15, 8)
                for hour in range(4)
            ),
        )
        connection.execute(
            "INSERT INTO clinical_events(admission_id,timestamp,event_type,author,data) "
            "VALUES(1,'2026-01-01T01:00:00','NOTE','synthetic','{\"state\":\"baseline\"}')"
        )
        connection.executemany(
            "INSERT INTO lab_data(admission_id,datetime,platelets,bilirubin,creatinine,lactate,pao2) "
            "VALUES(1,?,?,?,?,?,?)",
            (
                (f"2026-01-01T0{index}:30:00", 200 + index, 12, 80, 1.1, 90)
                for index in range(2)
            ),
        )
        connection.executemany(
            "INSERT INTO drugs(code,name,template) VALUES(?,?,?)",
            ((f"SYN-{index}", f"Synthetic drug {index}", "{}") for index in range(5)),
        )
        connection.execute(
            "INSERT INTO medical_audit_log(operation_id,table_name,row_id,admission_id,"
            "action_type,changed_by,before_json,after_json) "
            "VALUES('synthetic-seed','clinical_events',1,1,'seed','benchmark','{}','{}')"
        )
        connection.executemany(
            "INSERT INTO medical_audit_log(operation_id,table_name,row_id,admission_id,"
            "action_type,changed_by,before_json,after_json) VALUES(?,?,?,?,?,?,?,?)",
            ((f"synthetic-history-{index}", "clinical_events", 1, 1, "history", "benchmark", "{}", "{}")
             for index in range(history_rows)),
        )
        connection.commit()
        connection.execute("VACUUM")
        connection.execute("PRAGMA journal_mode=DELETE")
    finally:
        connection.close()
    return {
        "requested_mib": target_mib,
        "actual_bytes": path.stat().st_size,
        "actual_mib": round(path.stat().st_size / 1024 / 1024, 3),
        "page_size": 4096,
        "synthetic_patient_rows": patient_count,
        "text_bytes_per_patient": payload_size,
        "additional_untracked_history_rows": history_rows,
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _logical_digest(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    rows_total = 0
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        objects = list(
            connection.execute(
                "SELECT type,name,tbl_name,COALESCE(sql,'') FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_stat%' ORDER BY type,name"
            )
        )
        digest.update(pickle.dumps(objects, protocol=4))
        tables = [row[1] for row in objects if row[0] == "table"]
        for table in tables:
            quoted = '"' + str(table).replace('"', '""') + '"'
            row_hashes = []
            for row in connection.execute(f"SELECT * FROM {quoted}"):
                row_hashes.append(hashlib.sha256(pickle.dumps(tuple(row), protocol=4)).digest())
                rows_total += 1
            digest.update(str(table).encode("utf-8"))
            for row_hash in sorted(row_hashes):
                digest.update(row_hash)
    finally:
        connection.close()
    return {"quick_check": quick_check, "logical_sha256": digest.hexdigest(), "rows": rows_total}


def _state(path: Path) -> dict[str, Any]:
    from rem_card.app.local_replica_worker import _read_database_state

    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as connection:
        return _read_database_state(connection)


def _baseline_receipt(source: Path, local: Path) -> dict[str, Any]:
    from rem_card.app.replica_provenance import remember_source
    from rem_card.app.replica_source_stamp import capture_source_stamp
    from rem_card.app.role_admission import file_identity

    identity = file_identity(str(source))
    with closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, isolation_level=None)) as conn:
        stamp = capture_source_stamp(conn, str(source))
    state = _state(local)
    remember_source(str(local), identity, source_stamp=stamp, state=state)
    return {"identity": identity, "stamp": stamp, "state": state}


def _mutate_small(source: Path) -> None:
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("UPDATE vitals SET pulse=pulse+1 WHERE id=1")
        connection.execute(
            "UPDATE clinical_events SET data='{\"state\":\"changed\"}' WHERE id=1"
        )


def _mutate_event_limit(source: Path, count: int = 5001) -> None:
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.executemany(
            "UPDATE vitals SET last_modified_by=? WHERE id=1",
            ((f"synthetic-{index}",) for index in range(count)),
        )


def _mutate_retention_gap(source: Path, base_cursor: int) -> None:
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("UPDATE vitals SET pulse=pulse+1 WHERE id=1")
        connection.execute("UPDATE vitals SET pulse=pulse+1 WHERE id=1")
        connection.execute("DELETE FROM change_log WHERE id=?", (base_cursor + 1,))


def _validate_equivalent(source: Path, candidate: Path) -> dict[str, Any]:
    source_result = _logical_digest(source)
    candidate_result = _logical_digest(candidate)
    if source_result["quick_check"] != "ok" or candidate_result["quick_check"] != "ok":
        raise RuntimeError("quick_check failed during out-of-band validation")
    if source_result["logical_sha256"] != candidate_result["logical_sha256"]:
        raise RuntimeError("candidate is not logically equivalent to source")
    return {"equivalent": True, "source": source_result, "candidate": candidate_result}


def _run_direct_full(apsw, source: Path, destination: Path, delay: float, label: str) -> dict[str, Any]:
    from rem_card.app import local_replica_worker as worker

    source_hash_before = _file_sha256(source)
    stages: list[str] = []
    with _source_factory(apsw, source, delay, label) as (open_source, stats):
        started = time.perf_counter()
        with (
            patch.object(worker, "_open_central_readonly", side_effect=open_source),
            patch.object(
                worker,
                "_open_temp_replica",
                side_effect=lambda path: _open_local_adapter(apsw, path),
            ),
        ):
            result = worker._sync_snapshot(
                {
                    "central_db_path": str(source),
                    "temp_db_path": str(destination),
                    "local_state": {},
                    "writer_lock_path": str(destination.parent / "writer.lock"),
                },
                progress_callback=stages.append,
            )
        duration = time.perf_counter() - started
    source_hash_after = _file_sha256(source)
    if result.get("status") != "snapshot_ready":
        raise RuntimeError(f"direct full snapshot did not produce a candidate: {result}")
    validation = _validate_equivalent(source, destination)
    if source_hash_before != source_hash_after:
        raise RuntimeError("source bytes changed during read-only full snapshot")
    return {
        "method": "legacy_full_worker",
        "duration_seconds": round(duration, 6),
        "result_status": result.get("status"),
        "sync_mode": "full",
        "source_reads": stats.as_dict(),
        "source_sha256_unchanged": True,
        "stages": stages,
        "validation": validation,
        "raw_header_reads_excluded": 0,
    }


def _run_refresh(
    apsw,
    source: Path,
    local: Path | None,
    destination: Path,
    receipt: dict[str, Any] | None,
    delay: float,
    label: str,
) -> dict[str, Any]:
    from rem_card.app import local_replica_worker as worker
    from rem_card.app import replica_refresh

    source_hash_before = _file_sha256(source)
    stages: list[str] = []
    message: dict[str, Any] = {
        "central_db_path": str(source),
        "temp_db_path": str(destination),
        "local_state": dict((receipt or {}).get("state") or {}),
        "snapshot_lease_path": str(destination.parent / f"{label}.lease.lock"),
        "snapshot_gate_path": str(destination.parent / f"{label}.gate.lock"),
        "rotation_lock_path": str(destination.parent / f"{label}.rotation.lock"),
        "writer_lock_path": str(destination.parent / f"{label}.writer.lock"),
        "lease_duration_sec": 60.0,
    }
    if local is not None:
        message["local_db_path"] = str(local)
    if receipt:
        message.update(
            {
                "baseline_source_identity": receipt["identity"],
                "baseline_source_stamp": receipt["stamp"],
                "baseline_state": receipt["state"],
            }
        )
    with _source_factory(apsw, source, delay, label) as (open_source, stats):
        started = time.perf_counter()
        with (
            patch.object(worker, "_open_central_readonly", side_effect=open_source),
            patch.object(
                worker,
                "_open_temp_replica",
                side_effect=lambda path: _open_local_adapter(apsw, path),
            ),
            patch.object(replica_refresh, "sqlite3", _local_sqlite_facade(apsw)),
        ):
            result = worker._sync_snapshot_with_network_lease(
                message, progress_callback=stages.append
            )
        duration = time.perf_counter() - started
    source_hash_after = _file_sha256(source)
    if source_hash_before != source_hash_after:
        raise RuntimeError("source bytes changed during read-only refresh")
    candidate = destination if result.get("status") == "snapshot_ready" else local
    if candidate is None or not candidate.exists():
        raise RuntimeError(f"refresh returned no usable candidate: {result}")
    validation = _validate_equivalent(source, candidate)
    sync_mode = str(result.get("sync_mode") or "")
    # capture_source_stamp reads exactly 100 bytes twice in all successful
    # production paths used here. These raw reads bypass SQLite and the VFS.
    raw_header_reads = 2
    return {
        "method": "incremental_worker",
        "duration_seconds": round(duration, 6),
        "result_status": result.get("status"),
        "sync_mode": sync_mode,
        "full_reason": result.get("full_reason"),
        "delta_metrics": dict(result.get("delta_metrics") or {}),
        "source_reads": stats.as_dict(),
        "source_sha256_unchanged": True,
        "stages": stages,
        "validation": validation,
        "raw_header_reads_excluded": raw_header_reads,
        "raw_header_bytes_excluded": raw_header_reads * 100,
    }


def _prepare_pair(template: Path, root: Path, *, mutation: str) -> tuple[Path, list[Path], list[dict[str, Any]]]:
    root.mkdir(parents=True, exist_ok=False)
    source = root / "central" / "archiv" / "rao_journal.db"
    source.parent.mkdir(parents=True)
    shutil.copyfile(template, source)
    locals_: list[Path] = []
    receipts: list[dict[str, Any]] = []
    for index in range(2):
        local = root / f"local_baseline_{index}.db"
        shutil.copyfile(source, local)
        locals_.append(local)
        receipts.append(_baseline_receipt(source, local))
    if mutation == "small":
        _mutate_small(source)
    elif mutation == "event_limit":
        _mutate_event_limit(source)
    elif mutation == "retention_gap":
        _mutate_retention_gap(source, int(receipts[0]["state"]["change_cursor"]))
    elif mutation != "none":
        raise ValueError(mutation)
    return source, locals_, receipts


def _median_summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "duration_seconds": round(statistics.median(run["duration_seconds"] for run in runs), 6),
        "xread_calls": int(statistics.median(run["source_reads"]["xread_calls"] for run in runs)),
        "requested_bytes": int(statistics.median(run["source_reads"]["requested_bytes"] for run in runs)),
    }


def _comparison_summary(pairs: list[dict[str, Any]], old_key: str, new_key: str) -> dict[str, Any]:
    old = _median_summary([pair[old_key] for pair in pairs])
    new = _median_summary([pair[new_key] for pair in pairs])
    return {
        old_key: old,
        new_key: new,
        "latency_speedup_x": round(old["duration_seconds"] / max(new["duration_seconds"], 1e-9), 2),
        "source_byte_reduction_percent": round(
            100.0 * (1.0 - new["requested_bytes"] / max(old["requested_bytes"], 1)), 2
        ),
        "source_xread_reduction_percent": round(
            100.0 * (1.0 - new["xread_calls"] / max(old["xread_calls"], 1)), 2
        ),
    }


def run_benchmark(args) -> dict[str, Any]:
    apsw = _load_apsw(args.dependency_dir)
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with tempfile.TemporaryDirectory(prefix="remcard-replica-delta-") as temporary:
        owned_root = Path(temporary)
        template = owned_root / "synthetic_template.db"
        database = _create_database(template, args.database_mib, args.history_rows)

        first_full: list[dict[str, Any]] = []
        first_root = owned_root / "first_full"
        first_root.mkdir()
        first_source = first_root / "central" / "archiv" / "rao_journal.db"
        first_source.parent.mkdir(parents=True)
        shutil.copyfile(template, first_source)
        first_full.append(
            _run_direct_full(
                apsw, first_source, first_root / "legacy.db", args.delay_seconds, "first_full_old"
            )
        )
        first_full.append(
            _run_refresh(
                apsw,
                first_source,
                None,
                first_root / "new.db",
                None,
                args.delay_seconds,
                "first_full_new",
            )
        )

        changed_pairs: list[dict[str, Any]] = []
        unchanged_pairs: list[dict[str, Any]] = []
        for repeat in range(1, args.repeats + 1):
            source, locals_, receipts = _prepare_pair(
                template, owned_root / f"changed_{repeat}", mutation="small"
            )
            operations = ["legacy", "incremental"] if repeat % 2 else ["incremental", "legacy"]
            pair: dict[str, Any] = {"repeat": repeat, "execution_order": operations}
            for method in operations:
                if method == "legacy":
                    pair["legacy_full"] = _run_direct_full(
                        apsw,
                        source,
                        source.parents[1] / "legacy_candidate.db",
                        args.delay_seconds,
                        f"changed_old_{repeat}",
                    )
                else:
                    pair["incremental"] = _run_refresh(
                        apsw,
                        source,
                        locals_[0],
                        source.parents[1] / "delta_candidate.db",
                        receipts[0],
                        args.delay_seconds,
                        f"changed_delta_{repeat}",
                    )
            changed_pairs.append(pair)

            source, locals_, receipts = _prepare_pair(
                template, owned_root / f"unchanged_{repeat}", mutation="none"
            )
            operations = ["incremental", "legacy"] if repeat % 2 else ["legacy", "incremental"]
            pair = {"repeat": repeat, "execution_order": operations}
            for method in operations:
                if method == "legacy":
                    pair["legacy_full"] = _run_direct_full(
                        apsw,
                        source,
                        source.parents[1] / "legacy_candidate.db",
                        args.delay_seconds,
                        f"unchanged_old_{repeat}",
                    )
                else:
                    pair["unchanged_cached"] = _run_refresh(
                        apsw,
                        source,
                        locals_[0],
                        source.parents[1] / "unused_candidate.db",
                        receipts[0],
                        args.delay_seconds,
                        f"unchanged_new_{repeat}",
                    )
            unchanged_pairs.append(pair)

        fallbacks = []
        for mutation, expected_reason in (
            ("event_limit", "event_limit"),
            ("retention_gap", "change_log_gap"),
        ):
            source, locals_, receipts = _prepare_pair(
                template, owned_root / f"fallback_{mutation}", mutation=mutation
            )
            result = _run_refresh(
                apsw,
                source,
                locals_[0],
                source.parents[1] / "fallback_candidate.db",
                receipts[0],
                0.0,
                f"fallback_{mutation}",
            )
            if result.get("sync_mode") != "full" or result.get("full_reason") != expected_reason:
                raise RuntimeError(f"{mutation} did not choose expected full fallback: {result}")
            fallbacks.append({"case": mutation, "expected_reason": expected_reason, "result": result})

    first_by_method = {run["method"]: run for run in first_full}
    return {
        "benchmark": "remcard_replica_delta_synthetic_v1",
        "started_at": started_at,
        "runtime": {
            "python": sys.version.split()[0],
            "python_executable": sys.executable,
            "platform": platform.platform(),
            "sqlite3": sqlite3.sqlite_version,
            "apsw": apsw.apswversion(),
            "apsw_sqlite": apsw.sqlitelibversion(),
            "application_build_runtime_note": (
                "Source VFS uses the APSW SQLite version reported above. "
                "These are synthetic latency-model measurements, not production SMB measurements."
            ),
        },
        "configuration": {
            "repeats": args.repeats,
            "per_xread_delay_ms": args.delay_seconds * 1000.0,
            "database": database,
            "dataset": "fully synthetic unified schema; no production DB and no Z: drive",
            "source_vfs_scope": "SQLite opens of the synthetic central source only",
            "local_candidate_scope": "local disk, no injected latency",
            "raw_header_disclosure": (
                "capture_source_stamp performs two 100-byte raw header reads per successful "
                "new-worker run; these and filesystem metadata calls bypass SQLite VFS counters"
            ),
            "validation_scope": "logical digest, quick_check and source SHA-256 run after timing",
        },
        "first_full": {
            "runs": first_full,
            "summary": {
                "legacy_full": _median_summary([first_by_method["legacy_full_worker"]]),
                "new_worker_full": _median_summary([first_by_method["incremental_worker"]]),
            },
        },
        "changed_refresh": {
            "description": "one vital update plus one clinical-event update via existing triggers",
            "pairs": changed_pairs,
            "summary": _comparison_summary(changed_pairs, "legacy_full", "incremental"),
        },
        "unchanged_cached_restart": {
            "pairs": unchanged_pairs,
            "summary": _comparison_summary(unchanged_pairs, "legacy_full", "unchanged_cached"),
        },
        "full_fallbacks": fallbacks,
    }


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--database-mib", type=float, default=16.0)
    parser.add_argument("--delay-ms", type=float, default=1.75)
    parser.add_argument("--history-rows", type=int, default=0)
    parser.add_argument("--dependency-dir", type=Path, default=DEFAULT_DEPENDENCY_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.repeats < 3:
        parser.error("--repeats must be at least 3")
    if args.database_mib <= 0 or args.delay_ms < 0 or args.history_rows < 0:
        parser.error("database size must be positive and delay non-negative")
    args.delay_seconds = args.delay_ms / 1000.0
    return args


def main() -> int:
    args = _parse_args()
    result = run_benchmark(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "database": result["configuration"]["database"],
        "changed": result["changed_refresh"]["summary"],
        "unchanged": result["unchanged_cached_restart"]["summary"],
        "fallbacks": [item["result"]["full_reason"] for item in result["full_fallbacks"]],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
