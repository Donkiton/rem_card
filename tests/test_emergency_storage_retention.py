from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from rem_card.app.emergency_metadata import (
    EmergencySessionMetadata,
    EmergencyStandbyMetadata,
    atomic_write_json,
    metadata_to_dict,
)
from rem_card.app.emergency_paths import (
    ACTIVE_SESSION_METADATA_FILE_NAME,
    archived_dir,
    standby_generation_dir,
    standby_generation_medical_db_path,
    standby_generation_metadata_path,
    standby_generation_settings_db_path,
    standby_metadata_path,
)
from rem_card.app.emergency_standby import EmergencyStandbyManager
from rem_card.app.emergency_store import EmergencyLocalStore


def _manager(root: Path) -> EmergencyStandbyManager:
    manager = EmergencyStandbyManager.__new__(EmergencyStandbyManager)
    manager.root = str(root)
    manager.settings_required = True
    manager.store = EmergencyLocalStore(root=str(root), settings_required=True)
    manager.store.ensure_root_dirs()
    return manager


def _generation(root: Path, generation_id: str) -> EmergencyStandbyMetadata:
    generation_path = Path(standby_generation_dir(str(root), generation_id))
    generation_path.mkdir(parents=True)
    medical_path = Path(
        standby_generation_medical_db_path(str(root), generation_id)
    )
    settings_path = Path(
        standby_generation_settings_db_path(str(root), generation_id)
    )
    medical_path.write_bytes(b"medical")
    settings_path.write_bytes(b"settings")
    now = datetime.now().replace(microsecond=0).isoformat()
    metadata = EmergencyStandbyMetadata(
        standby_id="standby-test",
        created_at=now,
        updated_at=now,
        source_remote_db_path="network-medical.db",
        source_remote_fingerprint={},
        source_settings_db_path="network-settings.db",
        source_settings_fingerprint={},
        remote_last_change_id=1,
        schema_version=1,
        app_version="test",
        medical_db_path=str(medical_path),
        medical_db_hash="hash-medical",
        medical_db_size=medical_path.stat().st_size,
        medical_db_mtime=medical_path.stat().st_mtime,
        settings_db_path=str(settings_path),
        settings_db_hash="hash-settings",
        settings_db_size=settings_path.stat().st_size,
        settings_db_mtime=settings_path.stat().st_mtime,
        quick_check_status="ok",
        settings_quick_check_status="ok",
        validation_status="valid",
        validation_error=None,
        generation_id=generation_id,
        generation_dir=str(generation_path),
    )
    atomic_write_json(
        standby_generation_metadata_path(str(root), generation_id),
        metadata_to_dict(metadata),
    )
    return metadata


def _session(session_id: str, status: str, ended_at: str) -> EmergencySessionMetadata:
    return EmergencySessionMetadata(
        emergency_session_id=session_id,
        status=status,
        created_at=ended_at,
        started_at=ended_at,
        ended_at=ended_at,
        merged_at=ended_at if status == "merged" else None,
        source_machine="test",
        source_windows_user="test",
        source_client_id="test",
        source_role="nurse",
        app_version="test",
        schema_version=1,
        base_remote_db_path="network.db",
        base_remote_fingerprint={},
        base_last_change_id=1,
        base_snapshot_hash="hash",
        base_snapshot_created_at=ended_at,
        standby_last_change_id=1,
        last_observed_remote_change_id=1,
        local_db_path="local.db",
        base_snapshot_path="base.db",
        settings_snapshot_path="settings.db",
        merge_attempt_count=1,
        last_merge_error=None,
        validation_status="valid",
        validation_error=None,
        discarded_at=ended_at if status == "discarded" else None,
    )


def test_generation_cleanup_keeps_current_and_one_previous(tmp_path):
    manager = _manager(tmp_path)
    old = _generation(tmp_path, "gen_old")
    previous = _generation(tmp_path, "gen_previous")
    current = _generation(tmp_path, "gen_current")
    manager.store.write_standby_metadata(current)

    result = manager.cleanup_standby_generations(
        preferred_previous_generation_id=previous.generation_id,
    )

    assert result["removed_generations"] == 1
    assert not Path(old.generation_dir).exists()
    assert Path(previous.generation_dir).exists()
    assert Path(current.generation_dir).exists()


def test_crash_orphan_cannot_grow_generation_count_without_bound(tmp_path):
    manager = _manager(tmp_path)
    current = _generation(tmp_path, "gen_current")
    manager.store.write_standby_metadata(current)
    _generation(tmp_path, "gen_crash_orphan")
    _generation(tmp_path, "gen_older_orphan")

    manager.cleanup_standby_generations()

    remaining = [
        path
        for path in Path(current.generation_dir).parent.iterdir()
        if path.is_dir()
    ]
    assert len(remaining) == 2
    assert Path(current.generation_dir) in remaining


def test_completed_archives_are_removed_after_seven_days_only(tmp_path):
    store = EmergencyLocalStore(root=str(tmp_path))
    store.ensure_root_dirs()
    now = datetime(2026, 7, 27, 12, 0)
    cases = {
        "old_merged": _session(
            "old_merged",
            "merged",
            (now - timedelta(days=8)).isoformat(),
        ),
        "fresh_discarded": _session(
            "fresh_discarded",
            "discarded",
            (now - timedelta(days=6)).isoformat(),
        ),
        "protected_failed": _session(
            "protected_failed",
            "merge_failed",
            (now - timedelta(days=30)).isoformat(),
        ),
    }
    for session_id, metadata in cases.items():
        session_path = Path(archived_dir(str(tmp_path))) / session_id
        session_path.mkdir(parents=True)
        (session_path / "payload.db").write_bytes(b"x" * 32)
        atomic_write_json(
            str(session_path / ACTIVE_SESSION_METADATA_FILE_NAME),
            metadata_to_dict(metadata),
        )

    result = store.cleanup_completed_archived_sessions(
        retention_days=7,
        now=now,
    )

    archive_root = Path(archived_dir(str(tmp_path)))
    assert result["removed_sessions"] == 1
    assert not (archive_root / "old_merged").exists()
    assert (archive_root / "fresh_discarded").exists()
    assert (archive_root / "protected_failed").exists()


def test_temp_cleanup_waits_twenty_four_hours(tmp_path):
    manager = _manager(tmp_path)
    standby_root = Path(standby_metadata_path(str(tmp_path))).parent
    old_staging = standby_root / ".staging.old"
    fresh_staging = standby_root / ".staging.fresh"
    old_staging.mkdir()
    fresh_staging.mkdir()
    now_ts = time.time()
    os.utime(old_staging, (now_ts - 90000, now_ts - 90000))

    removed = manager.cleanup_failed_temp_files(now_ts=now_ts)

    assert removed == 1
    assert not old_staging.exists()
    assert fresh_staging.exists()
