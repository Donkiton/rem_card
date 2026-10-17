from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import socket
import sqlite3
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from rem_card.app.local_replica_sync import (
    LocalReplicaSync,
    build_local_replica_path,
)
from rem_card.app.local_replica_worker import (
    LocalReplicaRotationBusy,
    LocalReplicaSnapshotBusy,
    LocalReplicaWriterBusy,
    LocalReplicaWorkerClient,
    LocalReplicaWorkerError,
    LocalReplicaWorkerTimeout,
    _open_central_readonly,
    _sync_snapshot,
    _sync_snapshot_with_network_lease,
)
from rem_card.app.sqlite_shared import FileWriteLock
from rem_card.data.dao.db_manager import DatabaseManager


def _create_central_database(
    path: Path,
    *,
    cycle: str,
    values: list[str],
    schema_revision: str = "23",
) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE change_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_name TEXT NOT NULL,
                entity_id INTEGER,
                admission_id INTEGER,
                action TEXT NOT NULL
            );
            CREATE TABLE replica_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                value TEXT NOT NULL
            );
            """
        )
        conn.executemany(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            (
                ("db_cycle_started_at", cycle),
                ("unified_schema_fastpath_rev", schema_revision),
            ),
        )
        for value in values:
            cursor = conn.execute(
                "INSERT INTO replica_items(value) VALUES (?)",
                (value,),
            )
            conn.execute(
                """
                INSERT INTO change_log (
                    entity_name,
                    entity_id,
                    admission_id,
                    action
                )
                VALUES ('replica_items', ?, 1, 'insert')
                """,
                (cursor.lastrowid,),
            )
        conn.commit()
    finally:
        conn.close()


def _hold_snapshot_locks_until_killed(pipe, gate_path, lease_path):
    locks = []
    for path, source in (
        (gate_path, "local_replica_snapshot_gate"),
        (lease_path, "local_replica_snapshot"),
    ):
        lock = FileWriteLock(path, lease_duration_sec=15.0)
        assert lock.acquire(owner_id="test-worker", source=source)
        locks.append(lock)
    pipe.send("locked")
    # No voluntary release: exercise abrupt process death, not finally cleanup.
    time.sleep(60)


class _BlockingWorkerClient:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def sync(self, **_kwargs):
        self.started.set()
        self.release.wait(2.0)
        raise RuntimeError("test worker stopped")

    def close(self):
        self.release.set()


class _FailingWorkerClient:
    def sync(self, **_kwargs):
        raise LocalReplicaWorkerTimeout(0.3)

    def close(self):
        pass


class _DeferredWorkerClient:
    def __init__(self, exc: Exception):
        self.exc = exc

    def sync(self, **_kwargs):
        raise self.exc

    def close(self):
        pass


class LocalReplicaSyncTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.central_path = self.root / "central.db"
        self.local_path = self.root / "local.db"
        _create_central_database(
            self.central_path,
            cycle="cycle-a",
            values=["one", "two"],
        )
        self.replica = None

    def tearDown(self):
        if self.replica is not None:
            self.replica.stop()
        self._tmp.cleanup()

    def test_snapshot_is_read_only_and_reports_fresh_state(self):
        self.replica = LocalReplicaSync(
            central_db_path=str(self.central_path),
            local_db_path=str(self.local_path),
            sync_interval_sec=60.0,
        )

        self.assertTrue(self.replica.sync_once())

        rows = self.replica.fetch_all(
            "SELECT id, value FROM replica_items ORDER BY id"
        )
        self.assertEqual([tuple(row) for row in rows], [(1, "one"), (2, "two")])
        self.assertEqual(self.replica.fetch_one("PRAGMA query_only")[0], 1)
        self.assertTrue(self.replica.is_ready(max_stale_sec=10.0))
        health = self.replica.health_snapshot()
        self.assertEqual(health["state"]["db_cycle"], "cycle-a")
        self.assertEqual(health["state"]["change_cursor"], 2)

    def test_rotation_with_lower_change_cursor_replaces_replica(self):
        self.replica = LocalReplicaSync(
            central_db_path=str(self.central_path),
            local_db_path=str(self.local_path),
            sync_interval_sec=60.0,
        )
        self.assertTrue(self.replica.sync_once())

        replacement = self.root / "replacement.db"
        _create_central_database(
            replacement,
            cycle="cycle-b",
            values=["new-cycle-only"],
        )
        os.replace(replacement, self.central_path)

        self.assertTrue(self.replica.sync_once())

        rows = self.replica.fetch_all(
            "SELECT id, value FROM replica_items ORDER BY id"
        )
        self.assertEqual([tuple(row) for row in rows], [(1, "new-cycle-only")])
        self.assertEqual(
            self.replica.health_snapshot()["state"],
            {
                "db_cycle": "cycle-b",
                "change_cursor": 1,
                "schema_revision": "23",
            },
        )

    def test_start_never_waits_for_initial_network_sync(self):
        worker = _BlockingWorkerClient()
        self.replica = LocalReplicaSync(
            central_db_path=str(self.central_path),
            local_db_path=str(self.local_path),
            sync_interval_sec=60.0,
            worker_client=worker,
        )

        started = time.monotonic()
        self.replica.start()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.1)
        self.assertTrue(worker.started.wait(1.0))
        self.assertFalse(self.replica.is_ready(max_stale_sec=10.0))

    def test_worker_timeout_is_hard_and_cleans_temporary_snapshot(self):
        lease_dir = self.root / "worker-timeout-leases"
        client = LocalReplicaWorkerClient(
            central_db_path=str(self.central_path),
            snapshot_lease_dir=str(lease_dir),
            timeout_sec=0.3,
        )
        temp_path = self.root / "timeout.sync_tmp.db"
        started = time.monotonic()
        try:
            with self.assertRaises(LocalReplicaWorkerTimeout):
                client.sync(
                    local_state={},
                    temp_db_path=str(temp_path),
                    debug_delay_sec=0.8,
                )
            timeout_elapsed = time.monotonic() - started
            self.assertLess(timeout_elapsed, 1.2)
            self.assertFalse(temp_path.exists())
            self.assertFalse(Path(f"{temp_path}-wal").exists())

            result = client.sync(
                local_state={},
                temp_db_path=str(temp_path),
                timeout_sec=2.0,
            )
            self.assertEqual(result["status"], "snapshot_ready")
            self.assertFalse(Path(client.snapshot_gate_path).exists())
        finally:
            client.close()

        self.assertFalse(list(lease_dir.glob("*.lock")))

    def test_killed_worker_releases_its_snapshot_locks_for_other_clients(self):
        client = LocalReplicaWorkerClient(
            central_db_path=str(self.central_path),
            snapshot_lease_dir=str(self.root / "killed-reader-leases"),
        )
        context = multiprocessing.get_context("spawn")
        parent_pipe, child_pipe = context.Pipe()
        process = context.Process(
            target=_hold_snapshot_locks_until_killed,
            args=(child_pipe, client.snapshot_gate_path, client.snapshot_lease_path),
        )
        process.start()
        child_pipe.close()
        client._process, client._pipe = process, parent_pipe
        try:
            self.assertTrue(parent_pipe.poll(10), "worker failed to publish locks")
            self.assertEqual(parent_pipe.recv(), "locked")
            gate = Path(client.snapshot_gate_path)
            lease = Path(client.snapshot_lease_path)
            self.assertTrue(gate.exists() and lease.exists())
            client._terminate()
            deadline = time.monotonic() + 5
            while (gate.exists() or lease.exists()) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(gate.exists())
            self.assertFalse(lease.exists())
            replacement = FileWriteLock(str(gate))
            self.assertTrue(replacement.acquire(owner_id="next-client", source="local_replica_snapshot_gate"))
            replacement.release()
        finally:
            client.close()

    def test_stopped_worker_cleanup_preserves_other_owners_and_sources(self):
        path = self.root / "protected.lock"
        for pid, host, source, alive in (
            (987654, socket.gethostname(), "local_replica_snapshot_gate", True),
            (987654, socket.gethostname(), "local_replica_snapshot_gate", None),
            (123456, socket.gethostname(), "local_replica_snapshot_gate", False),
            (987654, "foreign-host", "local_replica_snapshot_gate", False),
            (987654, socket.gethostname(), "database_write", False),
        ):
            with self.subTest(pid=pid, host=host, source=source, alive=alive):
                content = json.dumps(dict(pid=pid, host=host, source=source,
                    timestamp=time.time()-1000, lock_token="another-owner"))
                path.write_text(content, encoding="utf-8")
                with patch("rem_card.app.sqlite_shared.is_local_pid_alive", return_value=alive):
                    self.assertFalse(FileWriteLock(str(path)).cleanup_stopped_local_owner(
                        owner_pid=987654, owner_source="local_replica_snapshot_gate"))
                self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_worker_timeout_covers_network_lease_stage(self):
        lease_dir = self.root / "lease-timeout"
        client = LocalReplicaWorkerClient(
            central_db_path=str(self.central_path),
            snapshot_lease_dir=str(lease_dir),
            timeout_sec=2.0,
        )
        temp_path = self.root / "lease-timeout.sync_tmp.db"
        current_state = {
            "db_cycle": "cycle-a",
            "change_cursor": 2,
            "schema_revision": "23",
        }
        captured = None
        try:
            self.assertEqual(
                client.sync(
                    local_state=current_state,
                    temp_db_path=str(temp_path),
                )["status"],
                "unchanged",
            )
            started = time.monotonic()
            with self.assertRaises(LocalReplicaWorkerTimeout) as captured:
                client.sync(
                    local_state=current_state,
                    temp_db_path=str(temp_path),
                    timeout_sec=0.4,
                    debug_lease_delay_sec=1.0,
                )
        finally:
            client.close()

        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(captured.exception.stage, "lease_acquire")
        self.assertFalse(temp_path.exists())
        self.assertFalse(list(lease_dir.glob("*.lock")))

    def test_worker_mutex_wait_obeys_same_hard_deadline(self):
        client = LocalReplicaWorkerClient(
            central_db_path=str(self.central_path),
            timeout_sec=0.2,
        )
        self.assertTrue(client._mutex.acquire(timeout=0.1))
        started = time.monotonic()
        try:
            with self.assertRaises(LocalReplicaWorkerTimeout) as captured:
                client.sync(
                    local_state={},
                    temp_db_path=str(self.root / "mutex-timeout.sync_tmp.db"),
                )
        finally:
            client._mutex.release()
        self.assertEqual(captured.exception.stage, "mutex_wait")
        self.assertLess(time.monotonic() - started, 0.7)
        self.assertIsNone(client._process)

    def test_progress_storm_cannot_extend_worker_deadline(self):
        class ProgressPipe:
            def send(self, _message):
                return None

            def poll(self, _timeout):
                return True

            def recv(self):
                return {"progress": "backup"}

            def close(self):
                return None

        class StoppableProcess:
            pid = None

            def __init__(self):
                self.alive = True

            def is_alive(self):
                return self.alive

            def terminate(self):
                self.alive = False

            def kill(self):
                self.alive = False

            def join(self, timeout=None):
                del timeout

            def close(self):
                return None

        client = LocalReplicaWorkerClient(
            central_db_path=str(self.central_path),
            timeout_sec=0.2,
        )
        client._process = StoppableProcess()
        client._pipe = ProgressPipe()
        started = time.monotonic()
        with self.assertRaises(LocalReplicaWorkerTimeout) as captured:
            client.sync(
                local_state={},
                temp_db_path=str(self.root / "progress-storm.sync_tmp.db"),
            )
        self.assertEqual(captured.exception.stage, "backup")
        self.assertLess(time.monotonic() - started, 0.7)

    def test_failed_termination_retains_and_fences_worker_handles(self):
        class StubbornProcess:
            pid = 987654

            def __init__(self):
                self.alive = True
                self.terminate_calls = 0
                self.kill_calls = 0

            def is_alive(self):
                return self.alive

            def terminate(self):
                self.terminate_calls += 1

            def kill(self):
                self.kill_calls += 1

            def join(self, timeout=None):
                del timeout

        process = StubbornProcess()
        pipe = SimpleNamespace(close=lambda: None)
        client = LocalReplicaWorkerClient(central_db_path=str(self.central_path))
        active_temp = self.root / "still-owned.sync_tmp.db"
        active_temp.write_bytes(b"protected partial snapshot")
        sidecar = Path(f"{active_temp}-wal")
        sidecar.write_bytes(b"protected WAL")
        client._process = process
        client._pipe = pipe
        client._active_temp_path = str(active_temp)

        self.assertFalse(client.can_cleanup_temp_path(str(active_temp)))
        self.assertFalse(client._terminate())
        self.assertIs(client._process, process)
        self.assertIs(client._pipe, pipe)
        self.assertTrue(client._fenced)
        self.assertFalse(client.can_cleanup_temp_path(str(active_temp)))
        self.assertTrue(active_temp.exists())
        self.assertTrue(sidecar.exists())
        with self.assertRaises(LocalReplicaWorkerError):
            client._ensure_started()
        self.assertEqual(process.terminate_calls, 2)
        self.assertEqual(process.kill_calls, 2)
        process.alive = False
        with patch.object(
            client,
            "_bounded_cleanup_stopped_worker_locks",
            return_value=True,
        ):
            self.assertTrue(client._terminate())
        self.assertIsNone(client._process)
        self.assertIsNone(client._pipe)
        self.assertFalse(active_temp.exists())
        self.assertFalse(sidecar.exists())
        self.assertEqual(client._active_temp_path, "")

    def test_failed_stopped_snapshot_unlink_retains_path_for_retry(self):
        client = LocalReplicaWorkerClient(central_db_path=str(self.central_path))
        active_temp = self.root / "stopped-unlink-denied.sync_tmp.db"
        active_temp.write_bytes(b"stopped snapshot")
        client._active_temp_path = str(active_temp)
        original_remove = os.remove

        def deny_snapshot_unlink(path):
            if os.path.abspath(path) == os.path.abspath(active_temp):
                raise PermissionError("snapshot temporarily held by antivirus")
            return original_remove(path)

        with patch("rem_card.app.local_replica_worker.os.remove", side_effect=deny_snapshot_unlink):
            self.assertFalse(client._terminate())
        self.assertEqual(client._active_temp_path, str(active_temp))
        self.assertTrue(active_temp.exists())
        self.assertTrue(client._terminate())
        self.assertFalse(active_temp.exists())
        self.assertEqual(client._active_temp_path, "")

    def test_liveness_probe_error_is_not_treated_as_process_death(self):
        class UnknownProcess:
            pid = 987654

            def is_alive(self):
                raise OSError("liveness unavailable")

            def terminate(self):
                return None

            def kill(self):
                return None

            def join(self, timeout=None):
                del timeout

        process = UnknownProcess()
        pipe = SimpleNamespace(close=lambda: None)
        client = LocalReplicaWorkerClient(central_db_path=str(self.central_path))
        client._process = process
        client._pipe = pipe

        self.assertFalse(client._terminate())
        self.assertIs(client._process, process)
        self.assertIs(client._pipe, pipe)
        self.assertTrue(client._fenced)

    def test_unconfirmed_cleanup_is_retained_and_coalesced(self):
        class UnknownCleanup:
            def is_alive(self):
                return True

            def terminate(self):
                return None

            def kill(self):
                return None

            def join(self, timeout=None):
                del timeout

        cleanup = UnknownCleanup()
        pipe = SimpleNamespace(close=lambda: None, poll=lambda _timeout: False)
        client = LocalReplicaWorkerClient(central_db_path=str(self.central_path))
        client._cleanup_process = cleanup
        client._cleanup_pipe = pipe
        client._cleanup_required_pid = 987654

        with patch("rem_card.app.local_replica_worker.multiprocessing.get_context") as context:
            self.assertFalse(client._bounded_cleanup_stopped_worker_locks(987654))
            self.assertFalse(client._bounded_cleanup_stopped_worker_locks(987654))

        context.assert_not_called()
        self.assertIs(client._cleanup_process, cleanup)
        self.assertIs(client._cleanup_pipe, pipe)

    def test_stopped_worker_cleanup_is_coalesced_without_daemon_threads(self):
        class StopsOnTerminate:
            pid = 987654

            def __init__(self):
                self.alive = True

            def is_alive(self):
                return self.alive

            def terminate(self):
                self.alive = False

            def join(self, timeout=None):
                del timeout

            def close(self):
                return None

        client = LocalReplicaWorkerClient(central_db_path=str(self.central_path))
        client._process = StopsOnTerminate()
        client._pipe = SimpleNamespace(close=lambda: None)
        with (
            patch.object(client, "_bounded_cleanup_stopped_worker_locks") as cleanup,
            patch("rem_card.app.local_replica_worker.threading.Thread") as thread_factory,
        ):
            self.assertTrue(client._terminate())
            self.assertTrue(client._terminate())

        cleanup.assert_called_once_with(987654, deadline=None)
        thread_factory.assert_not_called()

    def test_close_cancels_concurrent_sync_without_racing_pipe(self):
        client = LocalReplicaWorkerClient(
            central_db_path=str(self.central_path),
            snapshot_lease_dir=str(self.root / "close-race-leases"),
            timeout_sec=3.0,
        )
        result = {}

        def run_sync():
            try:
                client.sync(
                    local_state={},
                    temp_db_path=str(self.root / "close-race.sync_tmp.db"),
                    debug_delay_sec=1.0,
                )
            except Exception as exc:
                result["error"] = exc

        thread = threading.Thread(target=run_sync, daemon=True)
        thread.start()
        deadline = time.monotonic() + 2.0
        while client._process is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNotNone(client._process)
        self.assertTrue(client.close(timeout_sec=2.0))
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(result.get("error"), LocalReplicaWorkerError)
        self.assertIsNone(client._process)
        self.assertIsNone(client._pipe)

    def test_snapshot_progress_names_real_io_stages(self):
        stages = []
        temp_path = self.root / "progress-stages.sync_tmp.db"
        result = _sync_snapshot_with_network_lease(
            {
                "central_db_path": str(self.central_path),
                "temp_db_path": str(temp_path),
                "local_state": {},
                "snapshot_lease_path": str(self.root / "stage-leases" / "reader.lock"),
                "snapshot_gate_path": str(self.root / "stage-gate.lock"),
                "writer_lock_path": str(self.root / "db.lock"),
                "lease_duration_sec": 15.0,
            },
            progress_callback=stages.append,
        )
        self.assertEqual(result["status"], "snapshot_ready")
        for stage in (
            "lease_acquire",
            "central_probe",
            "central_open",
            "state_read",
            "snapshot_gate_acquire",
            "backup",
            "checkpoint",
            "quick_check",
            "fsync",
            "snapshot_gate_release",
            "lease_release",
        ):
            self.assertIn(stage, stages)
        self.assertLess(stages.index("backup"), stages.index("quick_check"))
        self.assertLess(stages.index("quick_check"), stages.index("fsync"))

    def test_release_progress_failure_cannot_leave_snapshot_locks(self):
        for failed_stage in ("snapshot_gate_release", "lease_release"):
            with self.subTest(failed_stage=failed_stage):
                gate = self.root / f"{failed_stage}.gate.lock"
                lease = self.root / f"{failed_stage}.lease.lock"

                def broken_progress(stage):
                    if stage == failed_stage:
                        raise BrokenPipeError("diagnostic IPC closed during release")

                result = _sync_snapshot_with_network_lease(
                    {"central_db_path": str(self.central_path), "local_state": {},
                     "temp_db_path": str(self.root / f"{failed_stage}.tmp.db"),
                     "snapshot_gate_path": str(gate), "snapshot_lease_path": str(lease),
                     "writer_lock_path": str(self.root / "db.lock")},
                    progress_callback=broken_progress,
                )
                self.assertEqual(result["status"], "snapshot_ready")
                self.assertFalse(gate.exists())
                self.assertFalse(lease.exists())

    def test_failed_sync_keeps_recent_local_replica_until_it_is_stale(self):
        self.replica = LocalReplicaSync(
            central_db_path=str(self.central_path),
            local_db_path=str(self.local_path),
            sync_interval_sec=60.0,
        )
        self.assertTrue(self.replica.sync_once())
        self.assertTrue(self.replica.is_ready(max_stale_sec=10.0))

        self.replica._worker_client.close()
        self.replica._worker_client = _FailingWorkerClient()
        self.assertFalse(self.replica.sync_once())

        self.assertTrue(self.replica.is_ready(max_stale_sec=10.0))
        health = self.replica.health_snapshot()
        self.assertEqual(health["consecutive_failures"], 1)
        self.assertTrue(health["degraded"])
        self.assertEqual(
            health["last_sync_error_class"],
            "LocalReplicaWorkerTimeout",
        )
        self.replica.last_sync_ok_ts = time.time() - 11.0
        self.assertFalse(self.replica.is_ready(max_stale_sec=10.0))

        self.replica._worker_client = LocalReplicaWorkerClient(
            central_db_path=str(self.central_path),
            timeout_sec=2.0,
        )
        self.assertTrue(self.replica.sync_once())
        self.assertTrue(self.replica.is_ready(max_stale_sec=10.0))

    def test_failed_new_generation_open_rolls_back_previous_valid_replica(self):
        self.replica = LocalReplicaSync(
            central_db_path=str(self.central_path),
            local_db_path=str(self.local_path),
            sync_interval_sec=60.0,
        )
        self.assertTrue(self.replica.sync_once())
        previous_health = self.replica.health_snapshot()
        previous_rows = self.replica.fetch_all(
            "SELECT value FROM replica_items ORDER BY id"
        )
        conn = sqlite3.connect(self.central_path)
        try:
            cursor = conn.execute(
                "INSERT INTO replica_items(value) VALUES ('new-generation')"
            )
            conn.execute(
                "INSERT INTO change_log(entity_name, entity_id, admission_id, action) "
                "VALUES ('replica_items', ?, 1, 'insert')",
                (cursor.lastrowid,),
            )
            conn.commit()
        finally:
            conn.close()

        original_open = self.replica._open_validated_local
        calls = 0

        def fail_new_generation_once(path):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise sqlite3.DatabaseError("simulated promoted generation open failure")
            return original_open(path)

        with patch.object(
            self.replica,
            "_open_validated_local",
            side_effect=fail_new_generation_once,
        ):
            self.assertFalse(self.replica.sync_once())

        self.assertEqual(
            self.replica.fetch_all("SELECT value FROM replica_items ORDER BY id"),
            previous_rows,
        )
        health = self.replica.health_snapshot()
        self.assertEqual(health["last_sync_ok_ts"], previous_health["last_sync_ok_ts"])
        self.assertEqual(health["state"], previous_health["state"])
        self.assertTrue(health["degraded"])
        self.assertFalse(list(self.root.glob("local.db.rollback.*")))

    def test_rollback_copy_failure_reopens_previous_generation(self):
        self.replica = LocalReplicaSync(
            central_db_path=str(self.central_path),
            local_db_path=str(self.local_path),
            sync_interval_sec=60.0,
        )
        self.assertTrue(self.replica.sync_once())
        previous_rows = self.replica.fetch_all(
            "SELECT value FROM replica_items ORDER BY id"
        )
        replacement = self.root / "valid-new-generation.db"
        _create_central_database(
            replacement,
            cycle="cycle-b",
            values=["replacement"],
        )

        with (
            patch("rem_card.app.local_replica_sync.os.link", side_effect=OSError("link denied")),
            patch("rem_card.app.local_replica_sync.shutil.copy2", side_effect=OSError("copy denied")),
        ):
            with self.assertRaises(OSError):
                self.replica._swap_local_replica(str(replacement))

        self.assertEqual(
            self.replica.fetch_all("SELECT value FROM replica_items ORDER BY id"),
            previous_rows,
        )

    def test_partial_rollback_copy_cannot_overwrite_intact_previous_generation(self):
        self.replica = LocalReplicaSync(
            central_db_path=str(self.central_path), local_db_path=str(self.local_path),
        )
        self.assertTrue(self.replica.sync_once())
        previous_bytes = self.local_path.read_bytes()
        replacement = self.root / "partial-copy-generation.db"
        _create_central_database(replacement, cycle="cycle-b", values=["replacement"])

        def fail_after_partial_copy(_source, destination):
            Path(destination).write_bytes(b"incomplete rollback")
            raise OSError("disk full after a partial copy")

        with (
            patch("rem_card.app.local_replica_sync.os.link", side_effect=OSError("no hardlinks")),
            patch("rem_card.app.local_replica_sync.shutil.copy2", side_effect=fail_after_partial_copy),
        ):
            with self.assertRaises(OSError):
                self.replica._swap_local_replica(str(replacement))

        self.assertEqual(self.local_path.read_bytes(), previous_bytes)
        self.assertEqual(
            [tuple(row) for row in self.replica.fetch_all("SELECT value FROM replica_items ORDER BY id")],
            [("one",), ("two",)],
        )
        self.assertFalse(list(self.root.glob("local.db.rollback.*")))

    def test_promotion_replace_failure_reopens_previous_generation(self):
        self.replica = LocalReplicaSync(
            central_db_path=str(self.central_path),
            local_db_path=str(self.local_path),
            sync_interval_sec=60.0,
        )
        self.assertTrue(self.replica.sync_once())
        previous_rows = self.replica.fetch_all(
            "SELECT value FROM replica_items ORDER BY id"
        )
        replacement = self.root / "replace-failure-generation.db"
        _create_central_database(
            replacement,
            cycle="cycle-b",
            values=["replacement"],
        )
        original_replace = os.replace

        def fail_promotion(source, target):
            if os.path.abspath(source) == os.path.abspath(replacement):
                raise PermissionError("simulated atomic promotion failure")
            return original_replace(source, target)

        with patch(
            "rem_card.app.local_replica_sync.os.replace",
            side_effect=fail_promotion,
        ):
            with self.assertRaises(PermissionError):
                self.replica._swap_local_replica(str(replacement))

        self.assertEqual(
            self.replica.fetch_all("SELECT value FROM replica_items ORDER BY id"),
            previous_rows,
        )
        self.assertTrue(replacement.exists())

    def test_restart_recovers_validated_rollback_artifact(self):
        rollback = Path(f"{self.local_path}.rollback.recovery")
        shutil.copy2(self.central_path, rollback)
        self.local_path.write_bytes(b"broken promoted generation")
        replica = LocalReplicaSync(
            central_db_path=str(self.central_path),
            local_db_path=str(self.local_path),
            sync_interval_sec=60.0,
            worker_client=_FailingWorkerClient(),
        )

        try:
            replica._ensure_local_conn()

            self.assertEqual(
                [
                    tuple(row)
                    for row in replica.fetch_all(
                        "SELECT value FROM replica_items ORDER BY id"
                    )
                ],
                [("one",), ("two",)],
            )
            self.assertFalse(rollback.exists())
            self.assertEqual(replica.health_snapshot()["last_sync_ok_ts"], 0.0)
        finally:
            replica._close_local_conn()

    def test_stop_before_worker_response_prevents_late_publication(self):
        started = threading.Event()
        release = threading.Event()

        class DelayedSnapshotWorker:
            def sync(worker_self, **kwargs):
                shutil.copy2(self.central_path, kwargs["temp_db_path"])
                started.set()
                release.wait(2.0)
                return {
                    "status": "snapshot_ready",
                    "state": {
                        "db_cycle": "cycle-a",
                        "change_cursor": 2,
                        "schema_revision": "23",
                    },
                    "temp_db_path": kwargs["temp_db_path"],
                }

            def close(worker_self, **_kwargs):
                release.set()
                return True

        replica = LocalReplicaSync(
            central_db_path=str(self.central_path),
            local_db_path=str(self.local_path),
            sync_interval_sec=60.0,
            worker_client=DelayedSnapshotWorker(),
        )
        result = {}
        thread = threading.Thread(
            target=lambda: result.setdefault("sync", replica.sync_once()),
            daemon=True,
        )
        replica._thread = thread
        thread.start()
        self.assertTrue(started.wait(1.0))
        self.assertTrue(replica.stop(timeout_sec=2.0))
        self.assertFalse(result.get("sync"))
        self.assertFalse(self.local_path.exists())
        self.assertEqual(replica.health_snapshot()["last_sync_ok_ts"], 0.0)

    def test_stop_serializes_with_publication_and_preserves_its_deadline(self):
        self.replica = LocalReplicaSync(
            central_db_path=str(self.central_path), local_db_path=str(self.local_path),
        )
        self.assertTrue(self.replica.sync_once())
        self.assertTrue(self.replica._worker_client.close(timeout_sec=1.0))
        replacement = self.root / "publication-race.db"
        _create_central_database(replacement, cycle="cycle-new", values=["new generation"])
        opened, release = threading.Event(), threading.Event()
        original_open = self.replica._open_validated_local

        class SnapshotWorker:
            def sync(worker_self, **kwargs):
                shutil.copy2(replacement, kwargs["temp_db_path"])
                return {"status": "snapshot_ready", "temp_db_path": kwargs["temp_db_path"],
                        "state": {"db_cycle": "cycle-new"}}

            def close(worker_self, **_kwargs):
                return True

        def pause_before_publication(path):
            connection = original_open(path)
            opened.set()
            if not release.wait(2.0):
                connection.close()
                raise TimeoutError("publication test did not release")
            return connection

        self.replica._worker_client = SnapshotWorker()
        result = {}
        thread = threading.Thread(target=lambda: result.setdefault("ok", self.replica.sync_once()))
        self.replica._thread = thread
        with patch.object(self.replica, "_open_validated_local", side_effect=pause_before_publication):
            thread.start()
            try:
                self.assertTrue(opened.wait(1.0))
                started = time.monotonic()
                self.assertFalse(self.replica.stop(timeout_sec=0.03))
                self.assertLess(time.monotonic() - started, 0.7)
                self.assertFalse(self.replica._stop_evt.is_set())
                self.assertIs(self.replica._thread, thread)
            finally:
                release.set()
                thread.join(2.0)
        self.assertFalse(thread.is_alive())
        self.assertTrue(result["ok"], "publication completed before stop was accepted")
        self.assertEqual(self.replica.last_state, {"db_cycle": "cycle-new"})
        self.assertTrue(self.replica.stop(timeout_sec=1.0))
        self.assertTrue(self.replica._stop_evt.is_set())

    def test_stop_timeout_retains_unfinished_thread_and_local_connection(self):
        class UnfinishedThread:
            def is_alive(self):
                return True

            def join(self, timeout=None):
                del timeout

        class UnfinishedWorker:
            def close(self, **_kwargs):
                return False

        replica = LocalReplicaSync(
            central_db_path=str(self.central_path),
            local_db_path=str(self.local_path),
            worker_client=UnfinishedWorker(),
        )
        shutil.copy2(self.central_path, self.local_path)
        replica._ensure_local_conn()
        retained_conn = replica._local_conn
        retained_thread = UnfinishedThread()
        replica._thread = retained_thread

        self.assertFalse(replica.stop(timeout_sec=0.01))
        self.assertIs(replica._thread, retained_thread)
        self.assertIs(replica._local_conn, retained_conn)
        replica._close_local_conn()

    def test_snapshot_uses_reader_lease_without_creating_rotation_lock(self):
        rotation_lock_path = self.root / "db_rotation.lock"
        lease_dir = self.root / "reader-leases"
        client = LocalReplicaWorkerClient(
            central_db_path=str(self.central_path),
            rotation_lock_path=str(rotation_lock_path),
            snapshot_lease_dir=str(lease_dir),
            timeout_sec=5.0,
        )
        temp_path = self.root / "locked.sync_tmp.db"
        result = {}

        def run_sync():
            try:
                result["value"] = client.sync(
                    local_state={},
                    temp_db_path=str(temp_path),
                    debug_delay_sec=0.6,
                )
            except Exception as exc:
                result["error"] = exc

        thread = threading.Thread(target=run_sync, daemon=True)
        thread.start()
        deadline = time.monotonic() + 1.0
        while (
            not list(lease_dir.glob("*.lock"))
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        try:
            self.assertFalse(rotation_lock_path.exists())
            self.assertTrue(list(lease_dir.glob("*.lock")))
            thread.join(timeout=6.0)
        finally:
            client.close()

        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", result)
        self.assertEqual(result["value"]["status"], "snapshot_ready")
        self.assertFalse(rotation_lock_path.exists())
        self.assertFalse(list(lease_dir.glob("*.lock")))

    def test_two_roles_on_same_workstation_do_not_copy_in_parallel(self):
        lease_dir = self.root / "shared-reader-leases"
        first = LocalReplicaWorkerClient(
            central_db_path=str(self.central_path),
            snapshot_lease_dir=str(lease_dir),
            snapshot_lease_id="nurse",
            timeout_sec=3.0,
        )
        second = LocalReplicaWorkerClient(
            central_db_path=str(self.central_path),
            snapshot_lease_dir=str(lease_dir),
            snapshot_lease_id="doctor",
            timeout_sec=3.0,
        )
        gate_path = Path(first.snapshot_gate_path)
        gate_holder = FileWriteLock(
            str(gate_path),
            stale_timeout_sec=60.0,
            allow_expired_lease_cleanup=False,
            allow_legacy_replica_cleanup=False,
            allow_malformed_cleanup=False,
        )
        first_temp_path = self.root / "nurse.sync_tmp.db"
        second_temp_path = self.root / "doctor.sync_tmp.db"
        try:
            self.assertTrue(
                gate_holder.acquire(
                    owner_id="test-nurse-client",
                    source="local_replica_snapshot_gate",
                )
            )
            self.assertTrue(gate_path.exists())
            with self.assertRaises(LocalReplicaSnapshotBusy):
                second.sync(
                    local_state={},
                    temp_db_path=str(second_temp_path),
                )
            self.assertTrue(gate_path.exists())
            self.assertFalse(second_temp_path.exists())

            self.assertTrue(gate_holder.release())
            first_result = first.sync(
                local_state={},
                temp_db_path=str(first_temp_path),
            )
        finally:
            gate_holder.release()
            first.close()
            second.close()

        self.assertEqual(first_result["status"], "snapshot_ready")
        self.assertTrue(first_temp_path.exists())
        self.assertFalse(gate_path.exists())
        self.assertFalse(list(lease_dir.glob("*.lock")))

    def test_foreign_workstation_and_legacy_gate_do_not_block_copy(self):
        lease_dir = self.root / "independent-reader-leases"
        with patch("rem_card.app.local_replica_worker.socket.gethostname", return_value="PC-A"):
            first = LocalReplicaWorkerClient(
                central_db_path=str(self.central_path), snapshot_lease_dir=str(lease_dir),
            )
        with patch("rem_card.app.local_replica_worker.socket.gethostname", return_value="PC-B"):
            second = LocalReplicaWorkerClient(
                central_db_path=str(self.central_path), snapshot_lease_dir=str(lease_dir),
            )
        self.assertNotEqual(first.snapshot_gate_path, second.snapshot_gate_path)
        foreign = FileWriteLock(first.snapshot_gate_path)
        legacy = FileWriteLock(str(self.root / "replica_snapshot_copy.lock"))
        self.assertTrue(foreign.acquire("PC-A", "local_replica_snapshot_gate"))
        self.assertTrue(legacy.acquire("old-client", "local_replica_snapshot_gate"))
        try:
            result = second.sync(local_state={}, temp_db_path=str(self.root / "independent.db"))
            self.assertEqual(result["status"], "snapshot_ready")
            self.assertTrue(Path(first.snapshot_gate_path).exists())
            self.assertTrue(Path(legacy.lock_path).exists())
            self.assertFalse(list(lease_dir.glob("*.lock")))
        finally:
            foreign.release()
            legacy.release()
            first.close()
            second.close()

    def test_two_clients_never_take_over_an_expired_foreign_snapshot_gate(self):
        lease_dir = self.root / "expired-gate-reader-leases"
        first = LocalReplicaWorkerClient(
            central_db_path=str(self.central_path),
            snapshot_lease_dir=str(lease_dir),
            snapshot_lease_id="nurse",
            timeout_sec=3.0,
        )
        second = LocalReplicaWorkerClient(
            central_db_path=str(self.central_path),
            snapshot_lease_dir=str(lease_dir),
            snapshot_lease_id="doctor",
            timeout_sec=3.0,
        )
        gate_path = Path(first.snapshot_gate_path)
        gate_path.parent.mkdir(parents=True, exist_ok=True)
        gate_payload = {
            "timestamp": time.time() - 120.0,
            "lease_expires_at": time.time() - 60.0,
            "pid": 999999,
            "host": f"foreign-{socket.gethostname()}",
            "user_id": "expired-owner",
            "source": "local_replica_snapshot_gate",
            "thread_id": 1,
            "lock_token": "expired-gate-token",
        }
        gate_path.write_text(json.dumps(gate_payload), encoding="utf-8")
        outcomes = []
        outcomes_lock = threading.Lock()

        def run_client(client, temp_name):
            try:
                client.sync(
                    local_state={},
                    temp_db_path=str(self.root / temp_name),
                )
                outcome = "unexpected-success"
            except Exception as exc:
                outcome = exc
            with outcomes_lock:
                outcomes.append(outcome)

        threads = [
            threading.Thread(
                target=run_client,
                args=(first, "expired-nurse.sync_tmp.db"),
                daemon=True,
            ),
            threading.Thread(
                target=run_client,
                args=(second, "expired-doctor.sync_tmp.db"),
                daemon=True,
            ),
        ]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5.0)
        finally:
            self.assertTrue(first.close())
            self.assertTrue(second.close())

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(outcomes), 2)
        self.assertTrue(
            all(isinstance(item, LocalReplicaSnapshotBusy) for item in outcomes)
        )
        persisted = json.loads(gate_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["lock_token"], "expired-gate-token")
        self.assertFalse(list(lease_dir.glob("*.lock")))
        gate_path.unlink()

    def test_clinical_writer_has_priority_over_new_snapshot(self):
        lease_dir = self.root / "writer-priority-leases"
        client = LocalReplicaWorkerClient(
            central_db_path=str(self.central_path),
            snapshot_lease_dir=str(lease_dir),
            timeout_sec=2.0,
        )
        writer_lock = FileWriteLock(client.writer_lock_path)
        self.assertTrue(writer_lock.acquire("test-nurse", "nurse_order_mark:test"))
        try:
            with self.assertRaises(LocalReplicaWriterBusy):
                client.sync(
                    local_state={},
                    temp_db_path=str(self.root / "writer-priority.sync_tmp.db"),
                )
        finally:
            writer_lock.release()
            client.close()

        self.assertFalse(Path(client.snapshot_gate_path).exists())
        self.assertFalse(list(lease_dir.glob("*.lock")))

    def test_snapshot_yields_when_writer_arrives_before_backup(self):
        lease_dir = self.root / "late-writer-leases"
        client = LocalReplicaWorkerClient(
            central_db_path=str(self.central_path),
            snapshot_lease_dir=str(lease_dir),
            timeout_sec=3.0,
        )
        result = {}

        def run_sync():
            try:
                result["value"] = client.sync(
                    local_state={},
                    temp_db_path=str(self.root / "late-writer.sync_tmp.db"),
                    debug_delay_sec=0.6,
                )
            except Exception as exc:
                result["error"] = exc

        thread = threading.Thread(target=run_sync, daemon=True)
        thread.start()
        gate_path = Path(client.snapshot_gate_path)
        deadline = time.monotonic() + 2.0
        while not gate_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        writer_lock = FileWriteLock(client.writer_lock_path)
        try:
            self.assertTrue(gate_path.exists())
            self.assertTrue(
                writer_lock.acquire("test-doctor", "ivl_status_update:test")
            )
            thread.join(timeout=5.0)
        finally:
            writer_lock.release()
            client.close()

        self.assertFalse(thread.is_alive())
        self.assertIsInstance(result.get("error"), LocalReplicaWriterBusy)
        self.assertFalse(gate_path.exists())
        self.assertFalse(list(lease_dir.glob("*.lock")))

    def test_running_backup_checks_for_a_new_writer_between_page_batches(self):
        writer_lock_path = self.root / "db.lock"
        temp_path = self.root / "interruptible.sync_tmp.db"
        backup_calls = []

        class InterruptibleConnection:
            def __init__(self, conn):
                self.conn = conn

            def execute(self, *args, **kwargs):
                return self.conn.execute(*args, **kwargs)

            def close(self):
                self.conn.close()

            def backup(self, target, *, pages, progress, sleep):
                backup_calls.append((pages, sleep))
                progress(sqlite3.SQLITE_OK, 2, 2)
                writer_lock_path.write_text("writer pending", encoding="utf-8")
                progress(sqlite3.SQLITE_BUSY, 1, 2)

        def open_interruptible(path):
            return InterruptibleConnection(_open_central_readonly(path))

        try:
            with patch(
                "rem_card.app.local_replica_worker._open_central_readonly",
                side_effect=open_interruptible,
            ):
                with self.assertRaises(LocalReplicaWriterBusy):
                    _sync_snapshot(
                        {
                            "central_db_path": str(self.central_path),
                            "temp_db_path": str(temp_path),
                            "local_state": {},
                            "writer_lock_path": str(writer_lock_path),
                        },
                        before_backup=lambda: None,
                    )
        finally:
            writer_lock_path.unlink(missing_ok=True)

        self.assertEqual(backup_calls, [(256, 0.01)])
        self.assertFalse(temp_path.exists())

    def test_rotation_busy_keeps_last_good_replica_available(self):
        rotation_lock_path = self.root / "db_rotation.lock"
        self.replica = LocalReplicaSync(
            central_db_path=str(self.central_path),
            local_db_path=str(self.local_path),
            rotation_lock_path=str(rotation_lock_path),
            sync_interval_sec=60.0,
        )
        self.assertTrue(self.replica.sync_once())
        lock = FileWriteLock(str(rotation_lock_path))
        self.assertTrue(lock.acquire("test-rotation", "db_rotation"))
        try:
            self.assertFalse(self.replica.sync_once())
            self.assertTrue(self.replica.is_ready(max_stale_sec=10.0))
            health = self.replica.health_snapshot()
            self.assertEqual(health["consecutive_failures"], 0)
            self.assertEqual(
                health["last_sync_error_class"],
                "LocalReplicaRotationBusy",
            )
        finally:
            lock.release()

    def test_expected_replica_contention_does_not_degrade_health(self):
        deferred_errors = (
            LocalReplicaRotationBusy(),
            LocalReplicaSnapshotBusy(),
            LocalReplicaWriterBusy(),
        )
        for exc in deferred_errors:
            with self.subTest(error_class=type(exc).__name__):
                replica = LocalReplicaSync(
                    central_db_path=str(self.central_path),
                    local_db_path=str(
                        self.root / f"{type(exc).__name__}.db"
                    ),
                    sync_interval_sec=60.0,
                    worker_client=_DeferredWorkerClient(exc),
                )
                self.assertFalse(replica.sync_once())
                health = replica.health_snapshot()
                self.assertFalse(health["degraded"])
                self.assertEqual(health["consecutive_failures"], 0)
                self.assertEqual(
                    health["last_sync_error_class"],
                    type(exc).__name__,
                )
                self.assertEqual(health["retry_backoff_sec"], 60.0)

    def test_deferred_snapshot_does_not_hide_preceding_real_failure(self):
        worker = _FailingWorkerClient()
        replica = LocalReplicaSync(
            central_db_path=str(self.central_path),
            local_db_path=str(self.root / "deferred-after-failure.db"),
            sync_interval_sec=60.0,
            worker_client=worker,
        )
        self.assertFalse(replica.sync_once())
        worker.sync = _DeferredWorkerClient(LocalReplicaWriterBusy()).sync

        self.assertFalse(replica.sync_once())

        health = replica.health_snapshot()
        self.assertTrue(health["degraded"])
        self.assertEqual(health["consecutive_failures"], 1)
        self.assertEqual(
            health["last_sync_error_class"],
            "LocalReplicaWorkerTimeout",
        )
        self.assertEqual(health["retry_backoff_sec"], 60.0)

    def test_old_empty_lock_is_quarantined_before_reuse(self):
        lock_path = self.root / "empty.lock"
        lock_path.write_bytes(b"")
        old = time.time() - 60.0
        os.utime(lock_path, (old, old))
        quarantine_dir = self.root / "lock_quarantine"
        lock = FileWriteLock(
            str(lock_path),
            allow_malformed_cleanup=True,
            malformed_grace_sec=0.1,
            malformed_quarantine_dir=str(quarantine_dir),
        )
        try:
            self.assertTrue(lock.acquire("new-owner", "db_rotation"))
            self.assertTrue(list(quarantine_dir.glob("*.malformed")))
        finally:
            lock.release()

    def test_lock_payload_is_staged_before_live_path_is_published(self):
        lock_path = self.root / "staged.lock"
        lock = FileWriteLock(str(lock_path))
        write_started = threading.Event()
        allow_write = threading.Event()
        published = threading.Event()
        allow_release = threading.Event()
        result = {}
        original_write = os.write

        def blocked_write(fd, data):
            write_started.set()
            if not allow_write.wait(2.0):
                raise TimeoutError("test did not release staged write")
            return original_write(fd, data)

        def acquire_lock():
            result["acquired"] = lock.acquire("owner", "test")
            published.set()
            if result["acquired"]:
                allow_release.wait(2.0)
                result["released"] = lock.release()

        with patch("rem_card.app.sqlite_shared.os.write", side_effect=blocked_write):
            thread = threading.Thread(target=acquire_lock, daemon=True)
            thread.start()
            self.assertTrue(write_started.wait(1.0))
            self.assertFalse(lock_path.exists())
            allow_write.set()
            self.assertTrue(published.wait(2.0))
            self.assertTrue(result.get("acquired"))
            self.assertIsInstance(
                json.loads(lock_path.read_text(encoding="utf-8")),
                dict,
            )
            allow_release.set()
            thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        self.assertTrue(result.get("released"))

    def test_failed_staged_write_never_leaves_live_or_temp_lock(self):
        lock_path = self.root / "failed-staged.lock"
        lock = FileWriteLock(str(lock_path))

        with patch(
            "rem_card.app.sqlite_shared.os.write",
            side_effect=OSError("simulated interrupted SMB write"),
        ):
            self.assertFalse(lock.acquire("owner", "test"))

        self.assertFalse(lock_path.exists())
        self.assertFalse(list(self.root.glob(".failed-staged.lock.*.tmp")))

    def test_busy_lock_does_not_write_staging_payload(self):
        lock_path = self.root / "busy.lock"
        owner = FileWriteLock(str(lock_path))
        contender = FileWriteLock(str(lock_path))
        self.assertTrue(owner.acquire("owner", "test"))
        try:
            with patch.object(
                contender,
                "_write_staged_payload",
                side_effect=AssertionError("busy path must not stage a payload"),
            ):
                self.assertFalse(contender.acquire("contender", "test"))
        finally:
            owner.release()

    def test_simultaneous_atomic_publication_has_exactly_one_owner(self):
        lock_path = self.root / "simultaneous.lock"
        contenders = [FileWriteLock(str(lock_path)) for _ in range(2)]
        barrier = threading.Barrier(2)
        allow_release = threading.Event()
        results = []
        results_lock = threading.Lock()

        def run_contender(index, contender):
            original_stage = contender._write_staged_payload

            def stage_together(raw):
                staged_path = original_stage(raw)
                barrier.wait(timeout=2.0)
                return staged_path

            with patch.object(
                contender,
                "_write_staged_payload",
                side_effect=stage_together,
            ):
                acquired = contender.acquire(f"owner-{index}", "test")
            with results_lock:
                results.append((index, acquired))
            if acquired:
                allow_release.wait(2.0)
                contender.release()

        threads = [
            threading.Thread(
                target=run_contender,
                args=(index, contender),
                daemon=True,
            )
            for index, contender in enumerate(contenders)
        ]
        for thread in threads:
            thread.start()

        deadline = time.monotonic() + 2.0
        while len(results) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(sum(1 for _, acquired in results if acquired), 1)
        self.assertTrue(lock_path.exists())
        self.assertIsInstance(
            json.loads(lock_path.read_text(encoding="utf-8")),
            dict,
        )

        allow_release.set()
        for thread in threads:
            thread.join(timeout=2.0)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertFalse(lock_path.exists())
        self.assertFalse(list(self.root.glob(".simultaneous.lock.*.tmp")))

    def test_failed_atomic_publish_never_leaves_live_or_temp_lock(self):
        lock_path = self.root / "failed-publish.lock"
        lock = FileWriteLock(str(lock_path))

        with patch(
            "rem_card.app.sqlite_shared.os.rename",
            side_effect=OSError("simulated interrupted SMB publish"),
        ):
            self.assertFalse(lock.acquire("owner", "test"))

        self.assertFalse(lock_path.exists())
        self.assertFalse(list(self.root.glob(".failed-publish.lock.*.tmp")))

    def test_failed_atomic_refresh_preserves_previous_valid_payload(self):
        lock_path = self.root / "refresh.lock"
        lock = FileWriteLock(str(lock_path), lease_duration_sec=30.0)
        self.assertTrue(lock.acquire("owner", "test"))
        original = lock_path.read_bytes()

        with patch(
            "rem_card.app.sqlite_shared.os.replace",
            side_effect=OSError("simulated interrupted SMB replace"),
        ):
            self.assertFalse(lock.refresh(metadata={"role": "nurse"}))

        self.assertEqual(lock_path.read_bytes(), original)
        self.assertIsInstance(json.loads(original.decode("utf-8")), dict)
        self.assertFalse(list(self.root.glob(".refresh.lock.*.tmp")))
        self.assertTrue(lock.release())

    def test_snapshot_gate_preserves_stable_empty_file_until_maintenance(self):
        gate_path = self.root / "replica_snapshot_copy.lock"
        lease_path = self.root / "replica_snapshots" / "nurse.lock"
        quarantine_dir = self.root / "quarantine" / "locks"
        gate_path.write_bytes(b"")
        old = time.time() - 60.0
        os.utime(gate_path, (old, old))

        with self.assertRaises(LocalReplicaSnapshotBusy):
            _sync_snapshot_with_network_lease(
                {
                    "central_db_path": str(self.central_path),
                    "temp_db_path": str(self.root / "empty-gate.sync_tmp.db"),
                    "local_state": {},
                    "snapshot_lease_path": str(lease_path),
                    "snapshot_gate_path": str(gate_path),
                    "writer_lock_path": str(self.root / "db.lock"),
                    "malformed_quarantine_dir": str(quarantine_dir),
                    "lease_duration_sec": 15.0,
                }
            )

        self.assertTrue(gate_path.exists())
        self.assertEqual(gate_path.read_bytes(), b"")
        self.assertFalse(lease_path.exists())
        self.assertFalse(list(quarantine_dir.glob("*.malformed")))

    def test_busy_rotation_lock_fails_fast_without_worker_timeout(self):
        rotation_lock_path = self.root / "db_rotation.lock"
        competing_lock = FileWriteLock(str(rotation_lock_path))
        self.assertTrue(
            competing_lock.acquire(
                owner_id="test-rotation",
                source="db_rotation",
            )
        )
        client = LocalReplicaWorkerClient(
            central_db_path=str(self.central_path),
            rotation_lock_path=str(rotation_lock_path),
            timeout_sec=2.0,
        )
        started = time.monotonic()
        try:
            with self.assertRaises(LocalReplicaRotationBusy):
                client.sync(
                    local_state={},
                    temp_db_path=str(self.root / "busy.sync_tmp.db"),
                )
        finally:
            competing_lock.release()
            client.close()

        self.assertLess(time.monotonic() - started, 1.5)

    def test_expired_foreign_legacy_replica_lock_is_preserved(self):
        rotation_lock_path = self.root / "db_rotation.lock"
        rotation_lock_path.write_text(
            json.dumps(
                {
                    "timestamp": time.time() - 120.0,
                    "pid": 999999,
                    "host": f"foreign-{socket.gethostname()}",
                    "user_id": "old-client",
                    "source": "local_replica_sync",
                    "thread_id": 1,
                }
            ),
            encoding="utf-8",
        )
        client = LocalReplicaWorkerClient(
            central_db_path=str(self.central_path),
            rotation_lock_path=str(rotation_lock_path),
            timeout_sec=2.0,
        )
        try:
            with self.assertRaises(LocalReplicaRotationBusy):
                client.sync(
                    local_state={},
                    temp_db_path=str(self.root / "foreign-preserved.sync_tmp.db"),
                )
        finally:
            client.close()

        self.assertTrue(rotation_lock_path.exists())

    def test_expired_local_dead_legacy_replica_lock_is_recovered_safely(self):
        rotation_lock_path = self.root / "db_rotation.lock"
        rotation_lock_path.write_text(
            json.dumps(
                {
                    "timestamp": time.time() - 120.0,
                    "pid": 999999,
                    "host": socket.gethostname(),
                    "user_id": "old-local-client",
                    "source": "local_replica_sync",
                    "thread_id": 1,
                }
            ),
            encoding="utf-8",
        )
        client = LocalReplicaWorkerClient(
            central_db_path=str(self.central_path),
            rotation_lock_path=str(rotation_lock_path),
            timeout_sec=2.0,
        )
        try:
            result = client.sync(
                local_state={},
                temp_db_path=str(self.root / "local-dead-recovered.sync_tmp.db"),
            )
        finally:
            client.close()

        self.assertEqual(result["status"], "snapshot_ready")
        self.assertFalse(rotation_lock_path.exists())

    def test_release_never_deletes_lock_after_ownership_changes(self):
        lock_path = self.root / "ownership.lock"
        lock = FileWriteLock(str(lock_path))
        self.assertTrue(lock.acquire("owner-a", "test"))
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        payload["lock_token"] = "replacement-owner"
        payload["user_id"] = "owner-b"
        lock_path.write_text(json.dumps(payload), encoding="utf-8")

        self.assertFalse(lock.release())
        self.assertTrue(lock_path.exists())

    def test_replica_failure_backoff_is_bounded_and_reported(self):
        self.replica = LocalReplicaSync(
            central_db_path=str(self.central_path),
            local_db_path=str(self.local_path),
            worker_client=_FailingWorkerClient(),
        )

        for expected in (15.0, 30.0, 60.0, 300.0, 300.0):
            self.assertFalse(self.replica.sync_once())
            health = self.replica.health_snapshot()
            self.assertEqual(health["retry_backoff_sec"], expected)
            self.assertGreater(health["retry_after_sec"], 0.0)

    def test_fast_refresh_requests_do_not_bypass_failure_backoff(self):
        self.replica = LocalReplicaSync(
            central_db_path=str(self.central_path), local_db_path=str(self.local_path),
            worker_client=_FailingWorkerClient(),
        )
        self.assertFalse(self.replica.sync_once())
        self.replica.trigger_fast_sync()
        started = time.monotonic()
        with patch("rem_card.app.local_replica_sync.os.path.exists", side_effect=AssertionError("network probe")):
            self.assertFalse(self.replica._wait_for_retry(0.06, "LocalReplicaWorkerTimeout"))
        self.assertGreaterEqual(time.monotonic() - started, 0.05)
        self.assertFalse(self.replica._fast_sync_evt.is_set())

    def test_stop_interrupts_failure_backoff_without_network_probe(self):
        self.replica = LocalReplicaSync(
            central_db_path=str(self.central_path), local_db_path=str(self.local_path),
            worker_client=_FailingWorkerClient(),
        )
        self.assertFalse(self.replica.sync_once())
        self.replica._stop_evt.set()
        started = time.monotonic()
        self.assertFalse(self.replica._wait_for_retry(30.0, "LocalReplicaWorkerTimeout"))
        self.assertLess(time.monotonic() - started, 0.5)

    def test_local_path_is_stable_and_scoped_by_role_and_database(self):
        kwargs = {
            "cache_dir": str(self.root / "cache"),
            "central_db_path": str(self.central_path),
            "client_id": "4bc493d0-a4d3-4f04-a026-b4686ebf69ea",
            "role": "operblock_planned",
        }
        first = build_local_replica_path(**kwargs)
        second = build_local_replica_path(**kwargs)
        other_role = build_local_replica_path(
            **{**kwargs, "role": "doctor"}
        )
        other_database = build_local_replica_path(
            **{
                **kwargs,
                "central_db_path": str(self.root / "other.db"),
            }
        )

        self.assertEqual(first, second)
        self.assertNotEqual(first, other_role)
        self.assertNotEqual(first, other_database)
        self.assertTrue(
            first.startswith(
                str(
                    self.root
                    / "cache"
                    / "replicas"
                    / "4bc493d0-a4d3-4f04-a026-b4686ebf69ea"
                )
            )
        )

    def test_local_reads_wait_until_replica_contains_required_write(self):
        state = {
            "db_cycle": "cycle-a",
            "change_cursor": 4,
            "schema_revision": "23",
        }
        replica = SimpleNamespace(
            is_ready=lambda **_kwargs: True,
            health_snapshot=lambda: {"state": dict(state)},
        )
        manager = DatabaseManager.__new__(DatabaseManager)
        manager._thread_state = threading.local()
        manager._local_replica = replica
        manager._prefer_central_reads_until = 0.0
        manager._local_replica_visibility_lock = threading.Lock()
        manager._required_local_replica_cursor = 5
        manager._local_replica_cycle_seen = "cycle-a"

        self.assertFalse(DatabaseManager._should_read_from_local(manager))
        state["change_cursor"] = 5
        self.assertTrue(DatabaseManager._should_read_from_local(manager))

        state["db_cycle"] = "cycle-b"
        state["change_cursor"] = 0
        self.assertTrue(DatabaseManager._should_read_from_local(manager))
        self.assertEqual(manager._required_local_replica_cursor, 0)

    def test_snapshot_scope_keeps_replica_rows_and_cursor_on_same_generation(self):
        self.replica = LocalReplicaSync(
            central_db_path=str(self.central_path),
            local_db_path=str(self.local_path),
            sync_interval_sec=60.0,
        )
        self.assertTrue(self.replica.sync_once())

        manager = DatabaseManager.__new__(DatabaseManager)
        manager._thread_state = threading.local()
        manager._local_replica = self.replica
        manager._prefer_central_reads_until = 0.0
        manager._local_replica_visibility_lock = threading.Lock()
        manager._required_local_replica_cursor = 0
        manager._local_replica_cycle_seen = "cycle-a"

        with DatabaseManager.snapshot_read_scope(manager):
            self.assertEqual(
                DatabaseManager.current_snapshot_read_source(manager),
                "local_replica",
            )
            self.assertEqual(DatabaseManager.get_latest_change_id(manager), 2)

            conn = sqlite3.connect(self.central_path)
            try:
                cursor = conn.execute(
                    "INSERT INTO replica_items(value) VALUES ('central-new')"
                )
                conn.execute(
                    """
                    INSERT INTO change_log (
                        entity_name,
                        entity_id,
                        admission_id,
                        action
                    )
                    VALUES ('replica_items', ?, 1, 'insert')
                    """,
                    (cursor.lastrowid,),
                )
                conn.commit()
            finally:
                conn.close()

            DatabaseManager._mark_local_replica_required_cursor(manager, 3)
            rows = DatabaseManager.fetch_all_remcard(
                manager,
                "SELECT value FROM replica_items ORDER BY id",
            )

            self.assertEqual([row[0] for row in rows], ["one", "two"])
            self.assertEqual(DatabaseManager.get_latest_change_id(manager), 2)

        self.assertFalse(DatabaseManager._should_read_from_local(manager))


if __name__ == "__main__":
    unittest.main()
