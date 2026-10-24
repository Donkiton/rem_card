import logging
import json
import multiprocessing
import sqlite3
from pathlib import Path
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import pytest

from rem_card.app import schema_migration_guard as guard
from rem_card.app import client_build_profile, startup_db_guard, unified_db_schema


class _Clock:
    def __init__(self):
        self.elapsed = 0.0

    def monotonic(self):
        return self.elapsed

    def sleep(self, delay):
        assert delay > 0
        self.elapsed += delay


class _Lock:
    def __init__(self, clock, available_at):
        self.clock = clock
        self.available_at = available_at
        self.attempts = []
        self.releases = 0

    def acquire(self, owner_id, source):
        self.attempts.append((owner_id, source))
        return self.clock.elapsed >= self.available_at

    def release(self):
        self.releases += 1


def _acquire(monkeypatch, clock, lock, mode):
    monkeypatch.setattr(guard, "time", clock)
    if mode == "controller":
        controller = SimpleNamespace(lock=lock, owner_id="client", max_retries=20, retry_delay_sec=0.2)
        guard._acquire_controller_file_lock(controller, "migration_test")
    else:
        monkeypatch.setattr(guard, "FileWriteLock", lambda *args, **kwargs: lock)
        assert guard._acquire_file_lock("db.lock", "client", "migration_test", logging.getLogger(__name__)) is lock


@pytest.mark.parametrize("mode", ["controller", "file"])
def test_migration_waits_beyond_normal_write_retry_budget(monkeypatch, mode):
    clock = _Clock()
    lock = _Lock(clock, available_at=6.0)

    _acquire(monkeypatch, clock, lock, mode)

    assert 6.0 <= clock.elapsed < 6.3
    assert len(lock.attempts) > 20
    assert set(lock.attempts) == {("client", "migration_test")}
    assert lock.releases == 0


@pytest.mark.parametrize("mode", ["controller", "file"])
def test_migration_lock_wait_is_bounded_and_does_not_release_another_owner(monkeypatch, mode):
    clock = _Clock()
    lock = _Lock(clock, available_at=float("inf"))

    with pytest.raises(sqlite3.OperationalError, match="Could not acquire db lock for schema migration"):
        _acquire(monkeypatch, clock, lock, mode)

    assert clock.elapsed == pytest.approx(60.0)
    assert lock.releases == 0


def test_migration_timeout_does_not_start_backup_policy_update_or_ddl(monkeypatch):
    clock = _Clock()
    lock = _Lock(clock, available_at=float("inf"))
    monkeypatch.setattr(guard, "time", clock)
    started = []
    monkeypatch.setattr(guard, "_ensure_after_lock", lambda *args, **kwargs: started.append(True))
    controller = SimpleNamespace(
        lock=lock, owner_id="client", max_retries=20, retry_delay_sec=0.2,
        connection_guard=lambda _conn: nullcontext(),
    )

    with pytest.raises(sqlite3.OperationalError, match="Could not acquire db lock"):
        guard.ensure_unified_schema_with_migration_backup(
            None, db_path="unused.db", backup_dir="unused", controller=controller,
        )

    assert started == []
    assert lock.releases == 0


def test_migration_retry_sleep_is_capped_by_remaining_deadline(monkeypatch):
    clock = _Clock()
    lock = _Lock(clock, available_at=float("inf"))
    monkeypatch.setattr(guard, "time", clock)
    controller = SimpleNamespace(lock=lock, owner_id="client", retry_delay_sec=7.0)

    with pytest.raises(sqlite3.OperationalError, match="Could not acquire db lock"):
        guard._acquire_controller_file_lock(controller, "migration_test")

    assert clock.elapsed == pytest.approx(60.0)


@pytest.fixture
def legacy_database(tmp_path):
    connections = []

    def create(version):
        db_path = tmp_path / f"legacy-{version}.db"
        conn = sqlite3.connect(db_path)
        connections.append(conn)
        unified_db_schema.ensure_unified_schema(conn)
        conn.execute("DROP INDEX idx_order_audit_admission_id")
        if version == 26:
            conn.execute("DROP INDEX idx_admin_committed_planned_expr_order_id")
        conn.execute("DELETE FROM schema_migrations WHERE version > ?", (version,))
        conn.execute("UPDATE meta SET value=? WHERE key=?", (version, unified_db_schema.SCHEMA_FASTPATH_META_KEY))
        conn.execute("INSERT INTO patients(full_name) VALUES('synthetic migration case')")
        conn.execute("INSERT INTO admissions(patient_id,bed_number,history_number,admission_datetime) "
                     "VALUES(1,1,'synthetic','2026-10-17 08:00:00')")
        conn.execute("INSERT INTO order_audit_log(admission_id,action_type,payload) VALUES(1,'synthetic','retained')")
        conn.commit()
        policy = tmp_path / "config" / "client_policy.json"
        policy.parent.mkdir(exist_ok=True)
        policy.write_text(json.dumps({"min_client_version": "5.1.6", "custom": {"retained": True}}), encoding="utf-8")
        kwargs = dict(db_path=str(db_path), backup_dir=str(tmp_path / "backups"), policy_path=str(policy),
                      min_client_version="5.1.6")
        return conn, policy, kwargs

    yield create
    for conn in connections:
        conn.close()


@pytest.mark.parametrize("version", [26, 27])
def test_mandatory_28_migration_validates_backup_preserves_data_and_is_idempotent(legacy_database, version):
    conn, policy, kwargs = legacy_database(version)
    preserved_tables = ("patients", "admissions", "orders", "administrations", "order_audit_log",
                        "medical_audit_log", "change_log")
    clinical_before = {table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                       for table in preserved_tables}
    result = guard.ensure_unified_schema_with_migration_backup(conn, **kwargs)

    assert result.migrated and result.policy_updated
    with sqlite3.connect(result.backup_path) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert backup.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (version,)
        assert backup.execute("SELECT payload FROM order_audit_log").fetchone() == ("retained",)
        assert not backup.execute("SELECT 1 FROM sqlite_master WHERE name='idx_order_audit_admission_id'").fetchone()
    assert conn.execute("SELECT payload FROM order_audit_log").fetchone() == ("retained",)
    assert {table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in preserved_tables} == clinical_before
    assert conn.execute("SELECT version FROM schema_migrations WHERE version IN (27,28) ORDER BY version").fetchall() == [(27,), (28,)]
    assert unified_db_schema.is_unified_schema_ready(conn)
    plan = conn.execute("EXPLAIN QUERY PLAN DELETE FROM order_audit_log WHERE admission_id=?", (1,)).fetchall()
    assert "idx_order_audit_admission_id" in str(plan)
    persisted_policy = json.loads(policy.read_text(encoding="utf-8"))
    assert persisted_policy["min_client_version"] == "5.1.11"
    assert persisted_policy["custom"] == {"retained": True}
    before = list(conn.iterdump())
    backups = sorted(Path(kwargs["backup_dir"]).glob("*.db"))
    again = guard.ensure_unified_schema_with_migration_backup(conn, **kwargs)
    assert not again.migrated and not again.policy_updated
    assert list(conn.iterdump()) == before
    assert sorted(Path(kwargs["backup_dir"]).glob("*.db")) == backups


def test_backup_failure_does_not_raise_policy_floor_or_change_database(legacy_database, monkeypatch):
    conn, policy, kwargs = legacy_database(27)
    before, policy_before = list(conn.iterdump()), policy.read_bytes()

    def fail_backup(*args, **kwargs):
        raise OSError("synthetic backup failure")

    monkeypatch.setattr(guard, "_create_validated_backup", fail_backup)
    with pytest.raises(OSError, match="backup failure"):
        guard.ensure_unified_schema_with_migration_backup(conn, **kwargs)
    assert list(conn.iterdump()) == before
    assert policy.read_bytes() == policy_before


def test_policy_failure_keeps_backup_and_prevents_ddl(legacy_database, monkeypatch):
    conn, policy, kwargs = legacy_database(27)
    before, policy_before = list(conn.iterdump()), policy.read_bytes()

    def fail_policy(*args, **kwargs):
        raise OSError("synthetic policy failure")

    monkeypatch.setattr(startup_db_guard, "_write_client_policy_atomically", fail_policy)
    with pytest.raises(OSError, match="policy failure"):
        guard.ensure_unified_schema_with_migration_backup(conn, **kwargs)
    assert list(conn.iterdump()) == before
    assert policy.read_bytes() == policy_before
    backups = list(Path(kwargs["backup_dir"]).glob("*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("failure", ["ddl_exception", "invalid_contract"])
def test_failed_migration_rolls_back_database_keeps_floor_and_can_retry(legacy_database, monkeypatch, failure):
    conn, policy, kwargs = legacy_database(27)
    before = list(conn.iterdump())
    original = guard.ensure_unified_schema

    def broken_migration(*args, **kwargs):
        original(*args, **kwargs)
        if failure == "ddl_exception":
            raise sqlite3.OperationalError("synthetic DDL failure")
        args[0].execute("DROP INDEX idx_order_audit_admission_id")

    with monkeypatch.context() as patch:
        patch.setattr(guard, "ensure_unified_schema", broken_migration)
        with pytest.raises((sqlite3.OperationalError, RuntimeError)):
            guard.ensure_unified_schema_with_migration_backup(conn, **kwargs)
    assert not conn.in_transaction
    assert list(conn.iterdump()) == before
    assert json.loads(policy.read_text(encoding="utf-8"))["min_client_version"] == "5.1.11"
    assert len(list(Path(kwargs["backup_dir"]).glob("*.db"))) == 1
    assert guard.ensure_unified_schema_with_migration_backup(conn, **kwargs).migrated
    assert unified_db_schema.is_unified_schema_ready(conn)


def test_recheck_after_lock_avoids_repeating_another_clients_migration(legacy_database, monkeypatch):
    conn, policy, kwargs = legacy_database(27)
    released = []

    def another_client_finished(*args):
        unified_db_schema.ensure_unified_schema(conn)
        conn.commit()
        return SimpleNamespace(release=lambda: released.append(True))

    monkeypatch.setattr(guard, "_acquire_file_lock", another_client_finished)
    result = guard.ensure_unified_schema_with_migration_backup(conn, lock_path="synthetic.lock", **kwargs)
    assert not result.migrated and result.policy_updated
    assert not Path(kwargs["backup_dir"]).exists()
    assert released == [True]


@pytest.mark.parametrize("version", [26, 27])
def test_new_no_upgrade_build_rejects_old_schema_without_writes(legacy_database, monkeypatch, version):
    conn, policy, kwargs = legacy_database(version)
    before, policy_before = list(conn.iterdump()), policy.read_bytes()
    monkeypatch.setattr(client_build_profile, "NO_DATABASE_UPGRADES", True)
    with pytest.raises(RuntimeError, match="schema incompatible"):
        guard.ensure_unified_schema_with_migration_backup(conn, **kwargs)
    assert list(conn.iterdump()) == before
    assert policy.read_bytes() == policy_before
    assert not Path(kwargs["backup_dir"]).exists()


@pytest.mark.parametrize("policy_state", ["missing", "old", "invalid"])
def test_no_upgrade_build_cannot_leave_current_schema_with_unprotected_policy(
    legacy_database, monkeypatch, policy_state,
):
    conn, policy, kwargs = legacy_database(27)
    unified_db_schema.ensure_unified_schema(conn)
    conn.commit()
    if policy_state == "missing":
        policy.unlink()
    elif policy_state == "invalid":
        policy.write_text("{", encoding="utf-8")
    before = list(conn.iterdump())
    policy_before = policy.read_bytes() if policy.exists() else None
    monkeypatch.setattr(client_build_profile, "NO_DATABASE_UPGRADES", True)
    with pytest.raises(RuntimeError, match="schema incompatible"):
        guard.ensure_unified_schema_with_migration_backup(conn, **kwargs)
    assert list(conn.iterdump()) == before
    assert (policy.read_bytes() if policy.exists() else None) == policy_before
    assert not Path(kwargs["backup_dir"]).exists()


def test_network_migration_requires_policy_destination_before_writes(legacy_database):
    conn, _policy, kwargs = legacy_database(27)
    kwargs["policy_path"] = None
    before = list(conn.iterdump())
    with pytest.raises(RuntimeError, match="политика доступа"):
        guard.ensure_unified_schema_with_migration_backup(
            conn, runtime_context=SimpleNamespace(is_network=True), **kwargs,
        )
    assert list(conn.iterdump()) == before
    assert not Path(kwargs["backup_dir"]).exists()


def test_current_no_upgrade_contract_still_requires_the_migration_27_index(legacy_database, monkeypatch):
    conn, policy, kwargs = legacy_database(27)
    guard.ensure_unified_schema_with_migration_backup(conn, **kwargs)
    conn.execute("DROP INDEX idx_admin_committed_planned_expr_order_id")
    conn.commit()
    before = list(conn.iterdump())
    policy_before = policy.read_bytes()
    monkeypatch.setattr(client_build_profile, "NO_DATABASE_UPGRADES", True)
    with pytest.raises(RuntimeError, match="schema incompatible"):
        guard.ensure_unified_schema_with_migration_backup(conn, **kwargs)
    assert list(conn.iterdump()) == before
    assert policy.read_bytes() == policy_before


@pytest.mark.parametrize("old_version", ["5.1.6", "5.1.7", "5.1.8", "5.1.9", "5.1.10"])
@pytest.mark.parametrize("no_upgrades", [True, False])
def test_optional_old_clients_cannot_bypass_mandatory_floor(tmp_path, monkeypatch, old_version, no_upgrades):
    policy = tmp_path / "config" / "client_policy.json"
    startup_db_guard.update_client_policy_min_version(str(policy), "5.1.6")
    before = policy.read_bytes()
    monkeypatch.setattr(startup_db_guard, "APP_VERSION", old_version)
    monkeypatch.setattr(client_build_profile, "NO_DATABASE_UPGRADES", no_upgrades)
    with pytest.raises(startup_db_guard.StartupPolicyError, match="устарела"):
        startup_db_guard._load_or_create_client_policy(str(tmp_path), None)
    assert policy.read_bytes() == before


def test_atomic_policy_replace_failure_preserves_previous_file(tmp_path, monkeypatch):
    policy = tmp_path / "client_policy.json"
    policy.write_text('{"min_client_version":"5.1.6","preserved":true}', encoding="utf-8")
    before = policy.read_bytes()

    def refuse_replace(*args):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(startup_db_guard.os, "replace", refuse_replace)
    with pytest.raises(OSError, match="replace failure"):
        startup_db_guard.update_client_policy_min_version(str(policy), "5.1.11")
    assert policy.read_bytes() == before
    assert sorted(tmp_path.iterdir()) == [policy]


def test_policy_update_never_lowers_a_newer_floor(tmp_path):
    policy = tmp_path / "client_policy.json"
    startup_db_guard.update_client_policy_min_version(str(policy), "5.1.12")
    before = policy.read_bytes()
    assert not startup_db_guard.update_client_policy_min_version(str(policy), "5.1.11")
    assert policy.read_bytes() == before


def _repair_policy_racer(baza_dir, phase, ready, resume, results):
    """Pause the actual startup repair, rather than a policy helper in isolation."""
    startup_db_guard.APP_VERSION = "5.1.6"
    if phase == "before_lock":
        original = startup_db_guard._client_policy_write_lock

        @contextmanager
        def paused_lock(path):
            ready.set()
            assert resume.wait(15), "repair was not resumed"
            with original(path):
                yield

        startup_db_guard._client_policy_write_lock = paused_lock
    else:
        original = startup_db_guard._write_client_policy_atomically

        def paused_replace(path, policy):
            ready.set()
            assert resume.wait(15), "repair was not resumed"
            original(path, policy)

        startup_db_guard._write_client_policy_atomically = paused_replace
    try:
        policy = startup_db_guard._load_or_create_client_policy(baza_dir, None)
        results.put(("repair", policy["min_client_version"]))
    except startup_db_guard.StartupPolicyError:
        results.put(("repair", "blocked"))
    except Exception as exc:
        results.put(("error", type(exc).__name__))


def _migrate_policy_racer(db_path, policy_path, backup_dir, lock_attempted, finished, results):
    original = startup_db_guard.FileWriteLock

    class ObservedPolicyLock(original):
        def acquire(self, *args, **kwargs):
            lock_attempted.set()
            return super().acquire(*args, **kwargs)

    startup_db_guard.FileWriteLock = ObservedPolicyLock
    try:
        with sqlite3.connect(db_path) as connection:
            result = guard.ensure_unified_schema_with_migration_backup(
                connection, db_path=db_path, backup_dir=backup_dir,
                policy_path=policy_path, lock_path=db_path + ".lock",
            )
        results.put(("migration", result.migrated))
    except Exception as exc:
        results.put(("error", type(exc).__name__))
    finally:
        finished.set()


@pytest.mark.parametrize("phase", ["before_lock", "before_replace"])
def test_startup_policy_repair_cannot_undo_another_process_migration_floor(legacy_database, phase):
    connection, policy, kwargs = legacy_database(27)
    context = multiprocessing.get_context("spawn")
    ready, resume, lock_attempted, finished = (context.Event() for _ in range(4))
    results = context.Queue()
    repair = context.Process(target=_repair_policy_racer, args=(
        str(policy.parent.parent), phase, ready, resume, results,
    ))
    migration = context.Process(target=_migrate_policy_racer, args=(
        kwargs["db_path"], str(policy), kwargs["backup_dir"], lock_attempted, finished, results,
    ))
    try:
        repair.start()
        assert ready.wait(12), "startup repair did not reach the race window"
        migration.start()
        assert lock_attempted.wait(12), "migration did not reach the policy lock"
        if phase == "before_lock":
            assert finished.wait(12), "repair must not hold a lock before entering its write scope"
        else:
            assert not finished.wait(0.2), "migration bypassed the active startup policy writer"
            assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (27,)
        resume.set()
        for process in (repair, migration):
            process.join(12)
            assert process.exitcode == 0
        outcomes = dict(results.get(timeout=3) for _ in range(2))
        assert outcomes == {"repair": "blocked" if phase == "before_lock" else "5.1.6", "migration": True}
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (28,)
        persisted = json.loads(policy.read_text(encoding="utf-8"))
        assert persisted["min_client_version"] == "5.1.11"
        assert persisted["custom"] == {"retained": True}
        assert not Path(str(policy) + ".lock").exists()
    finally:
        resume.set()
        for process in (repair, migration):
            if process.pid is not None:
                process.join(2)
                if process.is_alive():
                    process.terminate()
                    process.join(5)
                process.close()
        results.close()
        results.join_thread()


def test_complete_startup_policy_uses_read_only_fast_path(tmp_path, monkeypatch):
    policy = tmp_path / "config" / "client_policy.json"
    startup_db_guard.update_client_policy_min_version(str(policy), "5.1.11")
    before = policy.read_bytes()

    def unexpected_lock(*args, **kwargs):
        pytest.fail("complete startup policy must not create or acquire a writer lock")

    monkeypatch.setattr(startup_db_guard, "FileWriteLock", unexpected_lock)
    assert startup_db_guard._load_or_create_client_policy(str(tmp_path), None)["min_client_version"] == "5.1.11"
    assert policy.read_bytes() == before
    assert sorted(policy.parent.iterdir()) == [policy]


@pytest.mark.parametrize("operation", ["create", "repair", "update"])
def test_policy_writer_timeout_preserves_foreign_lock_and_fails_closed(tmp_path, monkeypatch, operation):
    policy = tmp_path / "config" / "client_policy.json"
    policy.parent.mkdir()
    if operation != "create":
        policy.write_text('{"min_client_version":"5.1.6"}', encoding="utf-8")
    before = policy.read_bytes() if policy.exists() else None
    lock_path = Path(str(policy) + ".lock")
    lock_path.write_text(json.dumps({
        "timestamp": 1, "host": "another-workstation", "pid": 12345,
        "user_id": "retained-owner", "thread_id": 1, "source": "client_policy_update",
        "lock_token": "foreign-policy-token",
    }), encoding="utf-8")
    lock_before = lock_path.read_bytes()
    clock = _Clock()
    monkeypatch.setattr(startup_db_guard, "time", clock)
    monkeypatch.setattr(startup_db_guard, "CLIENT_POLICY_LOCK_WAIT_SEC", 0.15)
    with pytest.raises(startup_db_guard.StartupPolicyError, match="занята"):
        if operation == "update":
            startup_db_guard.update_client_policy_min_version(str(policy), "5.1.11")
        else:
            startup_db_guard._load_or_create_client_policy(str(tmp_path), None)
    assert clock.elapsed == pytest.approx(0.15)
    assert (policy.read_bytes() if policy.exists() else None) == before
    assert lock_path.read_bytes() == lock_before


@pytest.mark.parametrize("payload", ["[]", "{"])
def test_policy_update_cannot_replace_invalid_policy_with_default(tmp_path, payload):
    policy = tmp_path / "client_policy.json"
    policy.write_text(payload, encoding="utf-8")
    with pytest.raises((startup_db_guard.StartupPolicyError, ValueError)):
        startup_db_guard.update_client_policy_min_version(str(policy), "5.1.11")
    assert policy.read_text(encoding="utf-8") == payload
    assert sorted(tmp_path.iterdir()) == [policy]
