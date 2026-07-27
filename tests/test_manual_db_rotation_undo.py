from __future__ import annotations

import os
import sqlite3
import json
from datetime import datetime, timedelta, timezone

from rem_card.app.db_lifecycle import (
    cancel_manual_rotation,
    find_active_rotation_role_locks,
    manual_rotation_undo_status,
    rotate_database_now,
)
from rem_card.app.role_session_lock import RoleSessionLock
from rem_card.app.sqlite_shared import configure_connection
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
