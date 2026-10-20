"""Worker integration, source replacement fencing and publication regressions."""
from contextlib import closing
import sqlite3

import pytest

from rem_card.app import local_replica_worker as worker
from rem_card.app.replica_provenance import remember_source, baseline_matches
from rem_card.app.role_admission import file_identity


@pytest.fixture
def databases(tmp_path):
    source = tmp_path / "central" / "archiv" / "rao_journal.db"
    source.parent.mkdir(parents=True)
    local = tmp_path / "local.db"
    with closing(sqlite3.connect(source)) as conn:
        conn.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO meta VALUES ('db_cycle_started_at','synthetic');
            INSERT INTO meta VALUES ('unified_schema_fastpath_rev','26');
            CREATE TABLE change_log(id INTEGER PRIMARY KEY, entity_name TEXT, entity_id INTEGER, action TEXT);
            INSERT INTO change_log VALUES(1,'patients',1,'insert');
            CREATE TABLE patients(id INTEGER PRIMARY KEY, name TEXT);
            INSERT INTO patients VALUES(1,'synthetic');
        """)
        with closing(sqlite3.connect(local)) as target:
            conn.backup(target)
    remember_source(str(local), file_identity(str(source)))
    message = {"central_db_path": str(source), "local_db_path": str(local),
               "temp_db_path": str(tmp_path / "candidate.db"),
               "snapshot_lease_path": str(tmp_path / "lease.lock"),
               "local_state": {"db_cycle": "synthetic", "schema_revision": "26", "change_cursor": 1}}
    return source, local, message


def test_provenance_rejects_replaced_source_and_local_file(databases):
    source, local, _ = databases
    identity = file_identity(str(source))
    assert baseline_matches(str(local), identity)
    assert not baseline_matches(str(local), {**identity, "inode": -1})
    with closing(sqlite3.connect(local)) as conn, conn:
        conn.execute("UPDATE patients SET name='different'")
    assert not baseline_matches(str(local), identity)


def test_rejected_delta_forces_full_even_with_equal_cursor(databases, monkeypatch):
    source, local, message = databases
    from rem_card.app import replica_refresh
    monkeypatch.setattr(replica_refresh, "try_incremental_snapshot",
                        lambda *a, **k: {"status": "full_required", "reason": "untracked_limit",
                                         "metrics": {"table": "history", "source_rows": 9000, "local_rows": 8999}})
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute("UPDATE patients SET name='central change without notification'")
    response = worker._sync_snapshot_with_network_lease(message)
    assert response["status"] == "snapshot_ready"
    assert response["sync_mode"] == "full"
    assert response["full_reason"] == "untracked_limit"
    assert response["delta_metrics"] == {"table": "history", "source_rows": 9000, "local_rows": 8999}
    with closing(sqlite3.connect(response["temp_db_path"])) as conn:
        assert conn.execute("SELECT name FROM patients").fetchone()[0] == "central change without notification"
    with closing(sqlite3.connect(local)) as conn:
        assert conn.execute("SELECT name FROM patients").fetchone()[0] == "synthetic"


def test_disabled_delta_still_materializes_requested_startup_snapshot(databases, monkeypatch):
    _, _, message = databases
    monkeypatch.setenv("REMCARD_INCREMENTAL_REPLICA", "0")
    response = worker._sync_snapshot_with_network_lease({**message, "require_snapshot": True})
    assert response["status"] == "snapshot_ready"
    assert response["sync_mode"] == "full"
    with closing(sqlite3.connect(response["temp_db_path"])) as conn:
        assert conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 1


def test_incremental_writer_preemption_keeps_previous_replica(databases, monkeypatch):
    _, local, message = databases
    from rem_card.app import replica_refresh
    monkeypatch.setattr(replica_refresh, "try_incremental_snapshot",
                        lambda *a, **k: (_ for _ in ()).throw(worker.LocalReplicaWriterBusy()))
    before = local.read_bytes()
    with pytest.raises(worker.LocalReplicaWriterBusy):
        worker._sync_snapshot_with_network_lease(message)
    assert local.read_bytes() == before
    from pathlib import Path
    assert not Path(message["snapshot_lease_path"]).exists()


def test_unchanged_physical_source_skips_even_unsupported_large_tables(databases, monkeypatch):
    source, local, message = databases
    # The legacy/minimal schema deliberately cannot generate a delta. Its full
    # snapshot nevertheless supports safe unchanged detection on the next poll.
    response = worker._sync_snapshot_with_network_lease(message)
    import os
    os.replace(response["temp_db_path"], local)
    remember_source(str(local), response["source_identity"],
                    source_stamp=response["source_stamp"], state=response["state"])
    from rem_card.app import replica_delta
    monkeypatch.setattr(replica_delta, "collect_delta", lambda *a, **k: pytest.fail("unchanged source scanned"))
    monkeypatch.setattr(worker, "_sync_snapshot", lambda *a, **k: pytest.fail("unchanged source recopied"))
    result = worker._sync_snapshot_with_network_lease(message)
    assert result["status"] == "unchanged"
    # No idle SQLite handle is retained: Windows must allow replacement now.
    replacement = source.with_suffix(".replacement")
    replacement.write_bytes(source.read_bytes())
    os.replace(replacement, source)


def test_untracked_only_commit_invalidates_physical_source_stamp(databases):
    source, local, message = databases
    result = worker._sync_snapshot_with_network_lease(message)
    import os
    os.replace(result["temp_db_path"], local)
    remember_source(str(local), result["source_identity"],
                    source_stamp=result["source_stamp"], state=result["state"])
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute("UPDATE patients SET name='untracked commit'")
    response = worker._sync_snapshot_with_network_lease(message)
    assert response["status"] == "snapshot_ready"
    assert response["source_stamp"] != result["source_stamp"]
    with closing(sqlite3.connect(response["temp_db_path"])) as conn:
        assert conn.execute("SELECT name FROM patients").fetchone()[0] == "untracked commit"


def test_source_stamp_rejects_wal_and_counter_mismatch(databases):
    source, _, _ = databases
    from rem_card.app.replica_source_stamp import capture_source_stamp
    with closing(sqlite3.connect(source)) as conn:
        first = capture_source_stamp(conn, str(source))
        assert first is not None and not conn.in_transaction
        conn.execute("PRAGMA journal_mode=WAL")
        assert capture_source_stamp(conn, str(source)) is None
        conn.execute("PRAGMA journal_mode=DELETE")
    with open(source, "r+b") as handle:
        handle.seek(92)
        handle.write(b"\xff\xff\xff\xff")
    with closing(sqlite3.connect(source)) as conn:
        assert capture_source_stamp(conn, str(source)) is None


def test_source_stamp_blocks_writer_commit_until_header_read_finishes(databases, monkeypatch):
    source, _, _ = databases
    from rem_card.app.replica_source_stamp import capture_source_stamp
    import builtins
    original_open = builtins.open
    writer_attempted = []

    def open_with_writer(*args, **kwargs):
        if args and str(args[0]) == str(source) and args[1] == "rb":
            with closing(sqlite3.connect(source, timeout=0)) as writer:
                writer.execute("UPDATE patients SET name='concurrent'")
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    writer.commit()
                writer.rollback()
                writer_attempted.append(True)
        return original_open(*args, **kwargs)

    with closing(sqlite3.connect(source)) as conn:
        monkeypatch.setattr(builtins, "open", open_with_writer)
        assert capture_source_stamp(conn, str(source)) is not None
    assert writer_attempted == [True]


def test_two_roles_receive_existing_trigger_writes_through_delta(tmp_path, monkeypatch):
    from rem_card.app.unified_db_schema import ensure_unified_schema
    from rem_card.app import local_replica_sync as sync
    source = tmp_path / "central" / "archiv" / "rao_journal.db"
    source.parent.mkdir(parents=True)
    with closing(sqlite3.connect(source)) as conn, conn:
        ensure_unified_schema(conn, allow_local_schema_upgrades=True)
        conn.execute("INSERT INTO meta(key,value) VALUES('db_cycle_started_at','synthetic-roles')")
        conn.execute("INSERT INTO patients(full_name) VALUES('synthetic patient')")
        conn.execute("""INSERT INTO admissions(patient_id,bed_number,history_number,admission_datetime)
                        VALUES(1,1,'synthetic','2026-10-17 08:00:00')""")
    modes = []
    monkeypatch.setattr(sync, "record_metric", lambda name, value, **fields:
                        modes.append(fields.get("sync_mode")) if name == "local_replica_sync_duration_ms" else None)
    replicas = [sync.LocalReplicaSync(central_db_path=str(source), local_db_path=str(tmp_path / f"{role}.db"))
                for role in ("doctor", "nurse")]
    try:
        for replica in replicas:
            assert replica.sync_once()
        modes.clear()
        with closing(sqlite3.connect(source)) as conn, conn:
            # Existing clients write through their existing triggers; no new
            # column, migration, protocol or change_log payload is required.
            conn.execute("INSERT INTO vitals(admission_id,datetime,pulse) VALUES(1,'2026-10-17 09:00:00',83)")
        before = source.read_bytes()
        for replica in replicas:
            assert replica.sync_once()
            assert replica.is_ready(max_stale_sec=60)
            assert replica.fetch_one("SELECT pulse FROM vitals")[0] == 83
        assert modes == ["delta", "delta"]
        assert replicas[0].last_state == replicas[1].last_state
        assert source.read_bytes() == before
        modes.clear()
        for replica in replicas:
            assert replica.sync_once()
        assert modes == ["unchanged", "unchanged"]
    finally:
        for replica in replicas:
            assert replica.stop()
