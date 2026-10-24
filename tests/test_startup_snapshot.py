import json
import os
import sqlite3
import threading
import time
from contextlib import closing

import pytest

from rem_card.app import role_admission as admission, startup_snapshot as snapshots
from rem_card.app import local_replica_worker as worker
from rem_card.app.local_replica_sync import LocalReplicaSync
from rem_card.app.startup_check_worker import StartupCheckAborted, run_startup_probe


@pytest.fixture(autouse=True)
def isolated_admission(tmp_path, monkeypatch):
    from rem_card.app import runtime_paths
    monkeypatch.setattr(runtime_paths, "get_local_cache_dir", lambda: str(tmp_path / "cache"))
    admission.invalidate_role_admission()
    yield
    admission.invalidate_role_admission()
    for path in list(snapshots._owned_workspaces):
        snapshots.discard({"workspace": path})


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "central" / "archiv" / "rao_journal.db"
    path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript("""
            CREATE TABLE example(id INTEGER PRIMARY KEY, value TEXT);
            INSERT INTO example VALUES (1, 'synthetic');
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO meta VALUES ('db_cycle_started_at', 'synthetic_cycle');
            INSERT INTO meta VALUES ('unified_schema_fastpath_rev', 'synthetic_rev');
            CREATE TABLE change_log(id INTEGER PRIMARY KEY, entity TEXT);
            INSERT INTO change_log VALUES (1, 'example');
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY);
            INSERT INTO schema_migrations VALUES (26);
        """)
    return path


def checked(source):
    workspace = snapshots.create_workspace()
    ok, payload, corrupt = snapshots.check_snapshot(str(source), workspace)
    assert ok, payload
    assert not corrupt
    return json.loads(payload)


def remember(payload):
    admission.remember_full_scan(payload["admission"], generation=admission.admission_generation(),
                                checked_monotonic=time.monotonic(), checked_at_epoch=time.time(),
                                snapshot=payload["snapshot"])
    return admission.recent_admission(payload["admission"]["identity"]["path"])[0]


def test_snapshot_validates_source_without_central_quickcheck_and_without_writes(source, monkeypatch):
    from rem_card.app import startup_db_guard as guard
    before = source.read_bytes()
    monkeypatch.setattr(guard, "_check_quick_direct", lambda *a, **k: pytest.fail("extra central scan"))
    payload = checked(source)
    ok, state, _ = admission.read_only_admission_probe(str(source))
    assert ok and json.loads(state) == payload["admission"]
    assert payload["snapshot"]["state"]["change_cursor"] == 1
    with closing(sqlite3.connect(payload["snapshot"]["path"])) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert conn.execute("SELECT value FROM example").fetchone() == ("synthetic",)
    assert source.read_bytes() == before


def test_same_master_seeds_two_roles_but_does_not_confirm_freshness(source, tmp_path):
    payload = checked(source)
    receipt = remember(payload)
    for role in ("doctor", "nurse"):
        local = tmp_path / role / "replica.db"
        candidate = snapshots.copy_for_replica(receipt, str(local))
        replica = LocalReplicaSync(central_db_path=str(source), local_db_path=str(local))
        try:
            replica.install_startup_snapshot(candidate)
            assert not replica.is_ready(max_stale_sec=60)
            assert replica.last_sync_ok_ts == 0 and replica.last_state == {}
            assert replica._read_local_state()["change_cursor"] == 1
            # Same source state needs a tiny metadata read, not another backup.
            response = worker._sync_snapshot({"central_db_path": str(source),
                                             "temp_db_path": str(local) + ".sync_tmp.test",
                                             "local_state": replica._read_local_state()},
                                            before_backup=lambda: pytest.fail("second network backup"))
            assert response["status"] == "unchanged"
        finally:
            assert replica.stop()
    assert os.path.isfile(payload["snapshot"]["path"])


@pytest.mark.parametrize("change", ["identity", "schema", "cycle", "quarantine"])
def test_changed_source_during_copy_cannot_authorize_entry(source, monkeypatch, change):
    original = worker._sync_snapshot_with_network_lease
    def copy_then_change(*args, **kwargs):
        result = original(*args, **kwargs)
        if change == "identity":
            replacement = source.with_name("replacement.db")
            replacement.write_bytes(source.read_bytes())
            os.replace(replacement, source)
        elif change == "quarantine":
            (source.parent.parent / "quarantine").mkdir()
        else:
            with closing(sqlite3.connect(source)) as conn, conn:
                conn.execute("CREATE TABLE changed(value TEXT)" if change == "schema" else
                             "UPDATE meta SET value='changed' WHERE key='db_cycle_started_at'")
        return result
    monkeypatch.setattr(worker, "_sync_snapshot_with_network_lease", copy_then_change)
    ok, _, corrupt = snapshots.check_snapshot(str(source), snapshots.create_workspace())
    assert not ok and not corrupt


def test_clinical_write_during_copy_does_not_fake_a_current_replica(source, monkeypatch):
    original = worker._sync_snapshot_with_network_lease
    def copy_then_write(*args, **kwargs):
        result = original(*args, **kwargs)
        with closing(sqlite3.connect(source)) as conn, conn:
            conn.execute("INSERT INTO example VALUES (2, 'new synthetic')")
            conn.execute("INSERT INTO change_log VALUES (2, 'example')")
        return result
    monkeypatch.setattr(worker, "_sync_snapshot_with_network_lease", copy_then_write)
    payload = checked(source)
    assert payload["snapshot"]["state"]["change_cursor"] == 1
    assert snapshots._source_state(str(source))[1]["change_cursor"] == 2


def test_invalid_local_snapshot_does_not_establish_source_corruption(source, monkeypatch):
    def broken(message, **kwargs):
        with open(message["temp_db_path"], "wb") as handle:
            handle.write(b"invalid destination" * 100)
        return {"status": "snapshot_ready", "state": snapshots._source_state(str(source))[1]}
    monkeypatch.setattr(worker, "_sync_snapshot_with_network_lease", broken)
    ok, _, corrupt = snapshots.check_snapshot(str(source), snapshots.create_workspace())
    assert not ok and not corrupt


@pytest.mark.parametrize("method", ["invalidate", "expire", "tamper"])
def test_snapshot_cannot_be_reused_after_invalidation_expiry_or_tampering(source, tmp_path, method):
    receipt = remember(checked(source))
    master = receipt["snapshot"]["path"]
    if method == "invalidate":
        admission.invalidate_role_admission()
        assert not os.path.exists(master)
    elif method == "expire":
        receipt["checked_monotonic"] -= admission.ADMISSION_TTL_SEC
        assert admission.recent_admission(str(source))[0] is None
        assert not os.path.exists(master)
    else:
        with open(master, "ab") as handle:
            handle.write(b"tampered")
    assert snapshots.copy_for_replica(receipt, str(tmp_path / "local" / "replica.db")) is None


def test_real_child_snapshot_is_readonly_and_emits_progress(source):
    before = source.read_bytes()
    progress = []
    result = run_startup_probe(str(source), threading.Event(), admission_mode="snapshot",
                               progress=progress.append)
    assert result[0], result[1]
    payload = json.loads(result[1])
    assert payload["snapshot"]["source"] == payload["admission"]
    assert any(item["stage"] == "backup" and item.get("total", 0) > 0 for item in progress)
    assert source.read_bytes() == before


def _hold_snapshot_locks(path, sender):
    from rem_card.app.sqlite_shared import FileWriteLock
    from rem_card.app.local_replica_worker import replica_snapshot_gate_path
    import socket
    root = os.path.dirname(os.path.dirname(path))
    owner = f"{socket.gethostname()}:{os.getpid()}:local_replica_snapshot"
    lease = FileWriteLock(os.path.join(root, "locks", "replica_snapshots", f"startup_{os.getpid()}.lock"))
    gate = FileWriteLock(replica_snapshot_gate_path(path))
    assert lease.acquire(owner, "local_replica_snapshot")
    assert gate.acquire(owner, "local_replica_snapshot_gate")
    conn = sqlite3.connect(path)
    conn.execute("BEGIN EXCLUSIVE")
    time.sleep(20)
    sender.send((True, "ok", False))


@pytest.mark.parametrize("cancelled", [False, True])
def test_aborted_snapshot_worker_releases_handles_and_owned_network_leases(source, cancelled):
    from rem_card.app.local_replica_worker import replica_snapshot_gate_path
    event = threading.Event()
    timer = threading.Timer(1.2, event.set) if cancelled else None
    if timer:
        timer.start()
    try:
        with pytest.raises(StartupCheckAborted) as error:
            run_startup_probe(str(source), event, admission_mode="snapshot", _target=_hold_snapshot_locks,
                               timeout=20 if cancelled else 1.2)
        assert error.value.reason == ("cancelled" if cancelled else "timeout")
        assert not os.path.exists(replica_snapshot_gate_path(str(source)))
        assert not list((source.parent.parent / "locks" / "replica_snapshots").glob("*.lock"))
        with closing(sqlite3.connect(source, timeout=0.1)) as conn:
            conn.execute("BEGIN EXCLUSIVE")
    finally:
        if timer:
            timer.cancel()


def test_unowned_cleanup_path_is_never_removed(tmp_path):
    folder = tmp_path / "unowned"
    folder.mkdir()
    snapshots.discard({"workspace": str(folder)})
    assert folder.is_dir()


def test_active_recovery_cannot_fall_back_to_direct_check(source, monkeypatch):
    from rem_card.app import startup_db_guard as guard
    lock = source.parent.parent / "locks" / "recovery.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("synthetic active recovery", encoding="utf-8")
    monkeypatch.setattr(guard, "_check_quick_direct", lambda *a, **k: pytest.fail("unsafe fallback"))
    ok, _, corrupt = snapshots.check_snapshot(str(source), snapshots.create_workspace())
    assert not ok and not corrupt


def test_unavailable_file_identity_keeps_the_legacy_full_check(source, monkeypatch):
    def unavailable(path):
        raise ValueError("database file identity is unavailable")
    monkeypatch.setattr(admission, "file_identity", unavailable)
    assert snapshots.check_snapshot(str(source), snapshots.create_workspace()) == (True, "ok", False)


def test_old_startup_master_does_not_replace_a_newer_valid_role_replica(source, tmp_path):
    receipt = remember(checked(source))
    local = tmp_path / "role" / "replica.db"
    local.parent.mkdir()
    # Create a later consistent generation, as an existing role's worker would.
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute("INSERT INTO example VALUES (2, 'later synthetic')")
        conn.execute("INSERT INTO change_log VALUES (2, 'example')")
    worker._sync_snapshot({"central_db_path": str(source), "temp_db_path": str(local)})
    replica = LocalReplicaSync(central_db_path=str(source), local_db_path=str(local))
    candidate = snapshots.copy_for_replica(receipt, str(local))
    try:
        replica.install_startup_snapshot(candidate)
        assert replica._read_local_state()["change_cursor"] == 2
        assert not replica.is_ready(max_stale_sec=60)
    finally:
        if os.path.exists(candidate):
            os.remove(candidate)
        assert replica.stop()


@pytest.mark.parametrize("busy", [worker.LocalReplicaWriterBusy, worker.LocalReplicaSnapshotBusy])
def test_busy_snapshot_can_use_full_direct_check_without_seed(source, monkeypatch, busy):
    from rem_card.app import startup_diagnostics
    events = []
    monkeypatch.setattr(startup_diagnostics, "startup_event",
                        lambda stage, **fields: events.append((stage, fields)))
    def blocked(*args, **kwargs):
        raise busy()
    monkeypatch.setattr(worker, "_sync_snapshot_with_network_lease", blocked)
    payload = checked(source)
    assert payload["snapshot"] is None
    ok, state, _ = admission.read_only_admission_probe(str(source))
    assert ok and payload["admission"] == json.loads(state)
    reason = "writer_busy" if busy is worker.LocalReplicaWriterBusy else "snapshot_busy"
    assert any(stage == "startup_snapshot_fallback" and fields["reason"] == reason
               for stage, fields in events)


def test_rotation_busy_never_uses_direct_fallback(source, monkeypatch):
    from rem_card.app import startup_db_guard as guard
    def blocked(*args, **kwargs):
        raise worker.LocalReplicaRotationBusy()
    monkeypatch.setattr(worker, "_sync_snapshot_with_network_lease", blocked)
    monkeypatch.setattr(guard, "_check_quick_direct", lambda *a, **k: pytest.fail("rotation fallback"))
    ok, _, corrupt = snapshots.check_snapshot(str(source), snapshots.create_workspace())
    assert not ok and not corrupt


def test_local_destination_failure_keeps_direct_entry_available(source, monkeypatch):
    from rem_card.app import startup_diagnostics
    events = []
    monkeypatch.setattr(startup_diagnostics, "startup_event",
                        lambda stage, **fields: events.append((stage, fields)))
    def blocked(path):
        raise PermissionError("synthetic local disk unavailable")
    monkeypatch.setattr(worker, "_open_temp_replica", blocked)
    payload = checked(source)
    assert payload["snapshot"] is None
    assert any(stage == "startup_snapshot_fallback"
               and fields["reason"] == "local_destination_unavailable"
               for stage, fields in events)


def test_workspace_failure_runs_direct_check_in_the_cancellable_child(source, monkeypatch):
    from rem_card.app import startup_diagnostics
    events = []
    monkeypatch.setattr(startup_diagnostics, "startup_event",
                        lambda stage, **fields: events.append((stage, fields)))
    def blocked():
        raise PermissionError("synthetic cache directory unavailable")
    monkeypatch.setattr(snapshots, "create_workspace", blocked)
    result = run_startup_probe(str(source), threading.Event(), admission_mode="snapshot")
    assert result[0], result[1]
    assert json.loads(result[1])["snapshot"] is None
    assert ("startup_snapshot_workspace_unavailable", {"error_class": "PermissionError"}) in events


def test_empty_or_foreign_admission_envelope_is_rejected(source):
    from rem_card.app.startup_check_worker import _validated_snapshot_result
    for state in ({}, {"identity": {"path": "foreign"}}):
        result = (True, json.dumps({"admission": state, "snapshot": None}), False)
        with pytest.raises(StartupCheckAborted, match="Проверка базы"):
            _validated_snapshot_result(result, None, str(source), True)


def test_expired_snapshot_deletion_is_retried_after_transient_local_failure(source, monkeypatch):
    receipt = remember(checked(source))
    workspace = receipt["snapshot"]["workspace"]
    remove = snapshots._remove_workspace
    monkeypatch.setattr(snapshots, "_remove_workspace", lambda *args: False)
    receipt["checked_monotonic"] -= admission.ADMISSION_TTL_SEC
    assert admission.recent_admission(str(source))[0] is None
    assert workspace in snapshots._pending_deletions
    assert os.path.isdir(workspace)
    monkeypatch.setattr(snapshots, "_remove_workspace", remove)
    snapshots._retry_deletions()
    assert not os.path.exists(workspace)
    assert workspace not in snapshots._owned_workspaces
    assert workspace not in snapshots._pending_deletions


def test_exit_cleanup_retries_snapshots_even_without_an_admission_receipt(monkeypatch):
    workspace = snapshots.create_workspace()
    remove = snapshots._remove_workspace
    monkeypatch.setattr(snapshots, "_stopping", False)
    monkeypatch.setattr(snapshots, "_remove_workspace", lambda *args: False)
    snapshots.discard({"workspace": workspace})
    assert admission._receipt is None
    monkeypatch.setattr(snapshots, "_remove_workspace", remove)
    snapshots._cleanup_at_exit()
    assert not os.path.exists(workspace)


@pytest.mark.parametrize("alive", [True, None, False])
def test_restart_sweep_removes_only_verified_stopped_owner_workspaces(tmp_path, monkeypatch, alive):
    from rem_card.app import sqlite_shared
    root = tmp_path / "cache" / "startup_snapshots"
    stale = root / "123456789_deadbeef"
    stale.mkdir(parents=True)
    (stale / snapshots._OWNER_FILE).write_text('{"format":1,"pid":123456789}', encoding="utf-8")
    (stale / "validated.db").write_bytes(b"synthetic clinical image")
    unmarked = root / "123456789_abcdefgh"
    unmarked.mkdir()
    foreign = root / "123456789_foreign_"
    foreign.mkdir()
    (foreign / snapshots._OWNER_FILE).write_text('{"format":1,"pid":7}', encoding="utf-8")
    monkeypatch.setattr(sqlite_shared, "is_local_pid_alive", lambda pid: alive)
    snapshots.create_workspace()
    assert stale.exists() is (alive is not False)
    assert unmarked.is_dir() and foreign.is_dir()


def test_next_process_removes_snapshot_left_by_a_crashed_owner(tmp_path):
    import subprocess
    import sys
    root = str(tmp_path / "cache")
    script = (
        "import os; from rem_card.app import runtime_paths, startup_snapshot; "
        f"runtime_paths.get_local_cache_dir = lambda: {root!r}; "
        "workspace = startup_snapshot.create_workspace(); "
        "open(os.path.join(workspace, 'validated.db'), 'wb').write(b'synthetic'); "
        "print(workspace, flush=True); os._exit(0)"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    workspace = result.stdout.strip()
    assert os.path.isfile(os.path.join(workspace, "validated.db"))
    snapshots.create_workspace()
    assert not os.path.exists(workspace)


@pytest.mark.parametrize("destination_full", [True, False])
def test_backup_error_falls_back_only_when_proven_local(source, monkeypatch, destination_full):
    from rem_card.app import startup_db_guard as guard
    open_central = worker._open_central_readonly

    class FailingBackup:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def backup(self, *args, **kwargs):
            error = sqlite3.OperationalError("database or disk is full" if destination_full else "disk I/O error")
            error.sqlite_errorcode = sqlite3.SQLITE_FULL if destination_full else sqlite3.SQLITE_IOERR
            raise error

    monkeypatch.setattr(worker, "_open_central_readonly", lambda path: FailingBackup(open_central(path)))
    if not destination_full:
        monkeypatch.setattr(guard, "_check_quick_direct", lambda *a, **k: pytest.fail("source I/O fallback"))
    before = source.read_bytes()
    ok, payload, corrupt = snapshots.check_snapshot(str(source), snapshots.create_workspace())
    assert ok is destination_full and not corrupt
    if ok:
        assert json.loads(payload)["snapshot"] is None
    assert source.read_bytes() == before
