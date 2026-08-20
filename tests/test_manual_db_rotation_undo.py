from __future__ import annotations

import os
import sqlite3
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from rem_card.data.dao import db_manager as db_manager_module
from rem_card.data.dao.db_manager import DatabaseManager
from rem_card.app.db_lifecycle import (
    cancel_manual_rotation,
    find_active_rotation_role_locks,
    manual_rotation_undo_status,
    maybe_rotate_database_if_due,
    rotate_database_now,
)
from rem_card.app.role_session_lock import RoleSessionLock
from rem_card.app.sqlite_shared import FileWriteLock, configure_connection
from rem_card.app.unified_db_schema import ensure_unified_schema


def _create_db(path: str, *, patient_name: str = "") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        configure_connection(conn)
        ensure_unified_schema(conn)
        if patient_name:
            conn.execute("INSERT INTO patients(full_name) VALUES (?)", (patient_name,))
    finally:
        conn.close()


def _rotate(db_path: str, archive_dir: str) -> dict:
    return rotate_database_now(
        db_path=db_path,
        archive_dir=archive_dir,
        rotation_lock_path=os.path.join(archive_dir, "db_rotation.lock"),
        db_lock_path=os.path.join(archive_dir, "db.lock"),
        backup_dir=os.path.join(os.path.dirname(archive_dir), "backups", "valid"),
        invalid_dir=os.path.join(os.path.dirname(archive_dir), "backup_health", "invalid_backups"),
        source="manual_rotation",
        runtime_mode="network",
    )


def test_manual_rotation_can_be_cancelled_before_patient_arrives(tmp_path):
    archive_dir = tmp_path / "archiv"
    db_path = archive_dir / "rao_journal.db"
    _create_db(str(db_path), patient_name="Пациент из прежнего цикла")

    result = _rotate(str(db_path), str(archive_dir))

    assert result["status"] == "rotated"
    assert manual_rotation_undo_status(db_path=str(db_path), archive_dir=str(archive_dir))["available"] is True

    undo = cancel_manual_rotation(
        db_path=str(db_path),
        archive_dir=str(archive_dir),
        rotation_lock_path=str(archive_dir / "db_rotation.lock"),
        db_lock_path=str(archive_dir / "db.lock"),
    )

    assert undo["status"] == "undo_rotated"
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 1
    finally:
        conn.close()
    assert os.path.isfile(undo["retained_new_cycle_path"])


def test_manual_rotation_undo_is_disabled_after_patient_insert_even_if_deleted(tmp_path):
    archive_dir = tmp_path / "archiv"
    db_path = archive_dir / "rao_journal.db"
    _create_db(str(db_path))

    assert _rotate(str(db_path), str(archive_dir))["status"] == "rotated"
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        configure_connection(conn)
        patient_id = conn.execute("INSERT INTO patients(full_name) VALUES ('Новый пациент')").lastrowid
        conn.execute("DELETE FROM patients WHERE id = ?", (patient_id,))
    finally:
        conn.close()

    status = manual_rotation_undo_status(db_path=str(db_path), archive_dir=str(archive_dir))

    assert status == {"available": False, "reason": "patient_data_added"}


def test_manual_rotation_undo_expires_after_one_day(tmp_path):
    archive_dir = tmp_path / "archiv"
    db_path = archive_dir / "rao_journal.db"
    _create_db(str(db_path))
    assert _rotate(str(db_path), str(archive_dir))["status"] == "rotated"

    state_path = archive_dir / "manual_rotation_undo.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["expires_at_utc"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    assert manual_rotation_undo_status(db_path=str(db_path), archive_dir=str(archive_dir))["reason"] == "expired"


def test_only_exact_current_role_lock_nonce_is_ignored(tmp_path):
    lock_path = tmp_path / "session_locks" / "doctor.lock"
    lock = RoleSessionLock(str(lock_path), role="doctor", owner_id="doctor-owner", heartbeat_sec=60.0)
    assert lock.acquire()
    try:
        assert find_active_rotation_role_locks({"doctor": str(lock_path)})
        context = lock.ownership_context()
        assert context is not None
        assert find_active_rotation_role_locks(
            {"doctor": str(lock_path)},
            ignored_lock_nonces={context["path"]: context["nonce"]},
        ) == []
    finally:
        lock.release()


def test_rotation_waits_for_active_replica_snapshot_lease(tmp_path):
    archive_dir = tmp_path / "archiv"
    db_path = archive_dir / "rao_journal.db"
    _create_db(str(db_path))
    lease_path = tmp_path / "locks" / "replica_snapshots" / "doctor.lock"
    lease = FileWriteLock(
        str(lease_path),
        lease_duration_sec=30.0,
        allow_expired_lease_cleanup=True,
    )
    assert lease.acquire("doctor-test", "local_replica_snapshot")
    result = {}

    thread = threading.Thread(
        target=lambda: result.update(_rotate(str(db_path), str(archive_dir))),
        daemon=True,
    )
    thread.start()
    try:
        deadline = time.monotonic() + 2.0
        while not (archive_dir / "db_rotation.lock").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (archive_dir / "db_rotation.lock").exists()
        time.sleep(0.15)
        assert thread.is_alive()
    finally:
        lease.release()
    thread.join(timeout=5.0)
    assert result["status"] == "rotated"
    assert not (archive_dir / "db_rotation.lock").exists()


def test_not_due_auto_rotation_does_not_create_rotation_lock(tmp_path):
    archive_dir = tmp_path / "archiv"
    db_path = archive_dir / "rao_journal.db"
    _create_db(str(db_path))
    rotation_lock_path = archive_dir / "db_rotation.lock"

    result = maybe_rotate_database_if_due(
        db_path=str(db_path),
        archive_dir=str(archive_dir),
        rotation_lock_path=str(rotation_lock_path),
        max_age_days=180,
        force=False,
    )

    assert result["status"] == "not_due"
    assert not rotation_lock_path.exists()


def test_busy_db_lock_reports_owner(tmp_path):
    archive_dir = tmp_path / "archiv"
    db_path = archive_dir / "rao_journal.db"
    db_lock_path = archive_dir / "db.lock"
    _create_db(str(db_path))
    lock = FileWriteLock(str(db_lock_path))
    assert lock.acquire("test-owner", "test_write")
    try:
        result = _rotate(str(db_path), str(archive_dir))
    finally:
        lock.release()

    assert result["status"] == "db_lock_busy"
    assert result["lock_path"] == str(db_lock_path)
    assert result["lock_owner"]["readable"] is True
    assert result["lock_owner"]["holder_user_id"] == "test-owner"
    assert result["lock_owner"]["holder_source"] == "test_write"


def test_automatic_rotation_retries_only_db_lock_busy(tmp_path):
    manager = object.__new__(DatabaseManager)
    manager.db_path = str(tmp_path / "archiv" / "rao_journal.db")
    manager.medical_db_rotation_lock_path = str(tmp_path / "archiv" / "db_rotation.lock")
    manager.medical_db_lock_path = str(tmp_path / "archiv" / "db.lock")
    manager.medical_backups_valid_dir = str(tmp_path / "backups" / "valid")
    manager.medical_invalid_backups_dir = str(tmp_path / "backup_health" / "invalid_backups")
    manager.baza_dir = str(tmp_path)
    manager.runtime_context = SimpleNamespace(mode="network")
    manager._startup_pre_connect_fingerprint = {}
    outcomes = [
        {
            "status": "db_lock_busy",
            "lock_owner": {"holder_host": "WS-01", "holder_pid": 101, "holder_source": "write"},
        },
        {
            "status": "db_lock_busy",
            "lock_owner": {"holder_host": "WS-01", "holder_pid": 101, "holder_source": "write"},
        },
        {"status": "not_due"},
    ]

    with (
        patch.object(db_manager_module, "AUTO_ROTATION_DB_LOCK_RETRY_DELAYS_SEC", (0.0, 0.0)),
        patch.object(db_manager_module, "maybe_rotate_database_if_due", side_effect=outcomes) as rotate,
        patch.object(db_manager_module.time, "sleep") as sleep,
    ):
        result = manager.maybe_rotate_database_after_doctor_exit()

    assert result["status"] == "not_due"
    assert rotate.call_count == 3
    assert sleep.call_count == 2


def test_manual_rotation_does_not_retry_db_lock_busy_and_resumes_service(tmp_path):
    manager = object.__new__(DatabaseManager)
    manager.db_path = str(tmp_path / "archiv" / "rao_journal.db")
    manager.medical_db_rotation_lock_path = str(tmp_path / "archiv" / "db_rotation.lock")
    manager.medical_db_lock_path = str(tmp_path / "archiv" / "db.lock")
    manager.medical_backups_valid_dir = str(tmp_path / "backups" / "valid")
    manager.medical_invalid_backups_dir = str(tmp_path / "backup_health" / "invalid_backups")
    manager.baza_dir = str(tmp_path)
    manager.runtime_context = SimpleNamespace(mode="network")
    manager._local_replica = None
    manager._closed = False
    manager._central_io_lock = threading.RLock()
    manager._remcard_conn = None
    manager._journal_conn = None
    manager._startup_pre_connect_fingerprint = {}
    manager.active_rotation_role_locks = lambda _context=None: []
    manager.active_rotation_emergency_sessions = lambda: []
    manager._rotation_blocking_role_lock_paths = lambda: {}
    manager._rotation_blocking_emergency_roots = lambda: []
    manager._close_central_read_connection = lambda: None
    manager._init_connections = lambda: None
    events = []
    manager.set_rotation_quiesce_hooks(
        lambda: events.append("paused") or {"ok": True, "monitor_was_enabled": True},
        lambda _token: events.append("resumed"),
    )

    with patch.object(
        db_manager_module,
        "rotate_database_now",
        return_value={
            "status": "db_lock_busy",
            "lock_owner": {"holder_host": "WS-02", "holder_pid": 202},
        },
    ) as rotate:
        result = manager.rotate_database_manually()

    assert result["status"] == "db_lock_busy"
    rotate.assert_called_once()
    assert events == ["paused", "resumed"]
