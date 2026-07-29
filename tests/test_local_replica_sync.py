from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from rem_card.app.local_replica_sync import (
    LocalReplicaSync,
    build_local_replica_path,
)
from rem_card.app.local_replica_worker import (
    LocalReplicaRotationBusy,
    LocalReplicaWorkerClient,
    LocalReplicaWorkerTimeout,
    replica_snapshot_lease_dir,
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
        client = LocalReplicaWorkerClient(
            central_db_path=str(self.central_path),
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
        finally:
            client.close()

        self.assertLess(time.monotonic() - started, 1.2)
        self.assertFalse(temp_path.exists())
        self.assertFalse(Path(f"{temp_path}-wal").exists())

    def test_failed_sync_disables_local_reads_until_replica_recovers(self):
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

        self.assertFalse(self.replica.is_ready(max_stale_sec=10.0))
        health = self.replica.health_snapshot()
        self.assertEqual(health["consecutive_failures"], 1)
        self.assertEqual(
            health["last_sync_error_class"],
            "LocalReplicaWorkerTimeout",
        )

        self.replica._worker_client = LocalReplicaWorkerClient(
            central_db_path=str(self.central_path),
            timeout_sec=2.0,
        )
        self.assertTrue(self.replica.sync_once())
        self.assertTrue(self.replica.is_ready(max_stale_sec=10.0))

    def test_snapshot_uses_reader_lease_without_creating_rotation_lock(self):
        rotation_lock_path = self.root / "db_rotation.lock"
        client = LocalReplicaWorkerClient(
            central_db_path=str(self.central_path),
            rotation_lock_path=str(rotation_lock_path),
            timeout_sec=2.0,
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
        lease_dir = Path(replica_snapshot_lease_dir(str(self.central_path)))
        deadline = time.monotonic() + 1.0
        while (
            not list(lease_dir.glob("*.lock"))
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        competing_lock = FileWriteLock(str(rotation_lock_path))
        try:
            self.assertFalse(rotation_lock_path.exists())
            self.assertTrue(list(lease_dir.glob("*.lock")))
            self.assertTrue(
                competing_lock.acquire(
                    owner_id="test-rotation",
                    source="db_rotation",
                )
            )
            thread.join(timeout=2.0)
        finally:
            competing_lock.release()
            client.close()

        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", result)
        self.assertEqual(result["value"]["status"], "snapshot_ready")
        self.assertFalse(rotation_lock_path.exists())
        self.assertFalse(list(lease_dir.glob("*.lock")))

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

        self.assertLess(time.monotonic() - started, 0.5)

    def test_expired_legacy_replica_lock_is_recovered_safely(self):
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
            result = client.sync(
                local_state={},
                temp_db_path=str(self.root / "recovered.sync_tmp.db"),
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


if __name__ == "__main__":
    unittest.main()
