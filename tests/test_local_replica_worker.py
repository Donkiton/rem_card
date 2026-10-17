from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from rem_card.app import local_replica_worker as worker


@pytest.fixture
def source_db(tmp_path: Path) -> Path:
    path = tmp_path / "central.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE source_rows(value TEXT)")
        conn.execute("INSERT INTO source_rows VALUES ('synthetic')")
    return path


def _message(source_db: Path, tmp_path: Path) -> dict:
    return {
        "central_db_path": str(source_db),
        "temp_db_path": str(tmp_path / "destination.sync_tmp.db"),
        "local_state": {},
        "writer_lock_path": str(tmp_path / "writer.lock"),
    }


class _BackupSource:
    def __init__(self, connection: sqlite3.Connection, error: Exception):
        self._connection = connection
        self._error = error

    def execute(self, *args, **kwargs):
        return self._connection.execute(*args, **kwargs)

    def close(self) -> None:
        self._connection.close()

    def backup(self, _destination, **_kwargs) -> None:
        raise self._error


def _backup_raises(source_db, tmp_path, monkeypatch, error):
    original_open = worker._open_central_readonly

    def open_failing_backup(path: str):
        return _BackupSource(original_open(path), error)

    monkeypatch.setattr(worker, "_open_central_readonly", open_failing_backup)
    return worker._sync_snapshot(_message(source_db, tmp_path))


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            sqlite3.OperationalError("database or disk is full"),
            id="canonical-full-message",
        ),
        pytest.param(
            sqlite3.OperationalError("synthetic SQLITE_FULL"),
            id="sqlite-full-code",
        ),
    ],
)
def test_backup_full_destination_is_typed(source_db, tmp_path, monkeypatch, error):
    if "SQLITE_FULL" in str(error):
        error.sqlite_errorcode = getattr(sqlite3, "SQLITE_FULL", 13)

    with pytest.raises(worker.LocalReplicaDestinationError) as raised:
        _backup_raises(source_db, tmp_path, monkeypatch, error)

    assert raised.value.__cause__ is error


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(sqlite3.OperationalError("disk I/O error"), id="sqlite-ioerr"),
        pytest.param(OSError("network path unavailable"), id="network-oserror"),
        pytest.param(sqlite3.OperationalError("database is locked"), id="sqlite-busy"),
    ],
)
def test_backup_source_or_busy_failure_remains_original(
    source_db, tmp_path, monkeypatch, error
):
    with pytest.raises(type(error)) as raised:
        _backup_raises(source_db, tmp_path, monkeypatch, error)

    assert raised.value is error


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(worker.LocalReplicaWriterBusy(), id="writer"),
        pytest.param(worker.LocalReplicaRotationBusy(), id="rotation"),
        pytest.param(worker.LocalReplicaSnapshotBusy(), id="snapshot-gate"),
    ],
)
def test_backup_control_failures_remain_original(
    source_db, tmp_path, monkeypatch, error
):
    with pytest.raises(type(error)) as raised:
        _backup_raises(source_db, tmp_path, monkeypatch, error)

    assert raised.value is error


class _CheckpointRow:
    def fetchone(self):
        return (1, 0, 0)


class _BadCheckpointDestination:
    def execute(self, statement: str, *_args, **_kwargs):
        assert statement == "PRAGMA wal_checkpoint(TRUNCATE)"
        return _CheckpointRow()

    def close(self) -> None:
        pass


class _CompletedBackupSource:
    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection

    def execute(self, *args, **kwargs):
        return self._connection.execute(*args, **kwargs)

    def close(self) -> None:
        self._connection.close()

    def backup(self, _destination, *, progress, **_kwargs) -> None:
        progress(getattr(sqlite3, "SQLITE_DONE", 101), 0, 1)


def test_bad_checkpoint_row_is_typed_local_destination_failure(
    source_db, tmp_path, monkeypatch
):
    original_open = worker._open_central_readonly
    monkeypatch.setattr(
        worker,
        "_open_central_readonly",
        lambda path: _CompletedBackupSource(original_open(path)),
    )
    monkeypatch.setattr(
        worker,
        "_open_temp_replica",
        lambda _path: _BadCheckpointDestination(),
    )

    with pytest.raises(worker.LocalReplicaDestinationError) as raised:
        worker._sync_snapshot(_message(source_db, tmp_path))

    assert isinstance(raised.value.__cause__, sqlite3.OperationalError)
    assert "WAL checkpoint failed" in str(raised.value)
