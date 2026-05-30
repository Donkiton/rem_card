from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from rem_card.app.settings_db_paths import (
    get_settings_backup_dir,
    get_settings_db_path,
    get_settings_dir,
    get_settings_lock_path,
)


RuntimeContextMode = Literal["network", "emergency", "snapshot"]


def _normalize_path(path: str) -> str:
    return os.path.abspath(os.path.normpath(str(path)))


@dataclass(frozen=True)
class DbRuntimeContext:
    mode: RuntimeContextMode
    medical_db_path: str
    medical_db_lock_path: str
    medical_backups_valid_dir: str
    medical_backup_health_dir: str
    medical_quarantine_dir: str
    medical_snapshots_dir: str
    medical_logs_dir: str
    recovery_lock_path: str
    session_locks_dir: str
    settings_db_path: str
    settings_db_lock_path: str
    settings_backups_dir: str
    settings_backup_health_dir: str
    settings_readonly: bool
    source_label: str
    is_network: bool
    is_emergency: bool
    is_snapshot: bool
    baza_dir: str
    medical_backups_root_dir: str
    medical_invalid_backups_dir: str
    medical_db_rotation_lock_path: str
    medical_client_policy_path: str
    medical_startup_quickcheck_state_path: str
    emergency_session_id: str | None = None

    def __post_init__(self) -> None:
        expected = {
            "network": (True, False, False),
            "emergency": (False, True, False),
            "snapshot": (False, False, True),
        }[self.mode]
        actual = (bool(self.is_network), bool(self.is_emergency), bool(self.is_snapshot))
        if actual != expected:
            raise ValueError(f"Runtime context flags do not match mode {self.mode!r}: {actual!r}")


def build_network_runtime_context() -> DbRuntimeContext:
    from rem_card.app import paths as app_paths

    settings_dir = get_settings_dir(app_paths.BAZA_DIR)
    return DbRuntimeContext(
        mode="network",
        medical_db_path=_normalize_path(app_paths.REMCARD_DB_PATH),
        medical_db_lock_path=_normalize_path(app_paths.DB_LOCK_PATH),
        medical_backups_valid_dir=_normalize_path(app_paths.BACKUPS_VALID_DIR),
        medical_backup_health_dir=_normalize_path(app_paths.BACKUP_HEALTH_DIR),
        medical_quarantine_dir=_normalize_path(app_paths.QUARANTINE_DIR),
        medical_snapshots_dir=_normalize_path(app_paths.SNAPSHOTS_DIR),
        medical_logs_dir=_normalize_path(app_paths.BAZA_LOGS_DIR),
        recovery_lock_path=_normalize_path(app_paths.RECOVERY_LOCK_PATH),
        session_locks_dir=_normalize_path(app_paths.ROLE_LOCKS_DIR),
        settings_db_path=_normalize_path(get_settings_db_path(app_paths.BAZA_DIR)),
        settings_db_lock_path=_normalize_path(get_settings_lock_path(app_paths.BAZA_DIR)),
        settings_backups_dir=_normalize_path(get_settings_backup_dir(app_paths.BAZA_DIR)),
        settings_backup_health_dir=_normalize_path(os.path.join(settings_dir, "backup_health")),
        settings_readonly=False,
        source_label="network",
        is_network=True,
        is_emergency=False,
        is_snapshot=False,
        baza_dir=_normalize_path(app_paths.BAZA_DIR),
        medical_backups_root_dir=_normalize_path(app_paths.BACKUPS_RC_DIR),
        medical_invalid_backups_dir=_normalize_path(app_paths.INVALID_BACKUPS_DIR),
        medical_db_rotation_lock_path=_normalize_path(app_paths.DB_ROTATION_LOCK_PATH),
        medical_client_policy_path=_normalize_path(app_paths.CLIENT_POLICY_PATH),
        medical_startup_quickcheck_state_path=_normalize_path(
            os.path.join(app_paths.BACKUP_HEALTH_DIR, "startup_quick_check_state.json")
        ),
        emergency_session_id=None,
    )


def build_emergency_runtime_context(emergency_session_dir: str) -> DbRuntimeContext:
    return _build_local_runtime_context(
        emergency_session_dir,
        mode="emergency",
        source_label="emergency",
        settings_readonly=True,
    )


def build_settings_snapshot_context(emergency_session_dir: str) -> DbRuntimeContext:
    return _build_local_runtime_context(
        emergency_session_dir,
        mode="snapshot",
        source_label="settings_snapshot",
        settings_readonly=True,
    )


def _build_local_runtime_context(
    emergency_session_dir: str,
    *,
    mode: RuntimeContextMode,
    source_label: str,
    settings_readonly: bool,
) -> DbRuntimeContext:
    root = _normalize_path(emergency_session_dir)
    archiv_dir = os.path.join(root, "archiv")
    backups_root = os.path.join(root, "backups")
    backup_health_dir = os.path.join(root, "backup_health")
    settings_dir = os.path.join(root, "settings")
    is_emergency = mode == "emergency"
    is_snapshot = mode == "snapshot"
    return DbRuntimeContext(
        mode=mode,
        medical_db_path=_normalize_path(os.path.join(archiv_dir, "rao_journal.db")),
        medical_db_lock_path=_normalize_path(os.path.join(archiv_dir, "db.lock")),
        medical_backups_valid_dir=_normalize_path(os.path.join(backups_root, "valid")),
        medical_backup_health_dir=_normalize_path(backup_health_dir),
        medical_quarantine_dir=_normalize_path(os.path.join(root, "quarantine")),
        medical_snapshots_dir=_normalize_path(os.path.join(root, "snapshots")),
        medical_logs_dir=_normalize_path(os.path.join(root, "logs")),
        recovery_lock_path=_normalize_path(os.path.join(root, "locks", "recovery.lock")),
        session_locks_dir=_normalize_path(os.path.join(root, "session_locks")),
        settings_db_path=_normalize_path(os.path.join(settings_dir, "remcard_settings.db")),
        settings_db_lock_path=_normalize_path(os.path.join(settings_dir, "settings.db.lock")),
        settings_backups_dir=_normalize_path(os.path.join(settings_dir, "backups")),
        settings_backup_health_dir=_normalize_path(os.path.join(settings_dir, "backup_health")),
        settings_readonly=bool(settings_readonly),
        source_label=source_label,
        is_network=False,
        is_emergency=is_emergency,
        is_snapshot=is_snapshot,
        baza_dir=root,
        medical_backups_root_dir=_normalize_path(backups_root),
        medical_invalid_backups_dir=_normalize_path(os.path.join(backup_health_dir, "invalid_backups")),
        medical_db_rotation_lock_path=_normalize_path(os.path.join(archiv_dir, "db_rotation.lock")),
        medical_client_policy_path=_normalize_path(os.path.join(root, "config", "client_policy.json")),
        medical_startup_quickcheck_state_path=_normalize_path(
            os.path.join(backup_health_dir, "startup_quick_check_state.json")
        ),
        emergency_session_id=os.path.basename(root) or None,
    )
