from __future__ import annotations

import os


EMERGENCY_ROOT_ENV = "REMCARD_EMERGENCY_DB_ROOT"
EMERGENCY_ROOT_DIR_NAME = "emergency_db"
EMERGENCY_APP_DIR_NAME = "RemCard"

STANDBY_DIR_NAME = "standby"
ACTIVE_DIR_NAME = "active"
ARCHIVED_DIR_NAME = "archived"

STANDBY_MEDICAL_DB_FILE_NAME = "rao_journal_standby.db"
STANDBY_SETTINGS_DB_FILE_NAME = "remcard_settings_standby.db"
STANDBY_METADATA_FILE_NAME = "standby_metadata.json"

ACTIVE_BASE_SNAPSHOT_FILE_NAME = "base_snapshot.db"
ACTIVE_MEDICAL_DB_FILE_NAME = "rao_journal_emergency.db"
ACTIVE_SETTINGS_SNAPSHOT_FILE_NAME = "remcard_settings_snapshot.db"
ACTIVE_SESSION_METADATA_FILE_NAME = "emergency_session.json"
EMERGENCY_CLIENT_ID_FILE_NAME = "emergency_client_id.json"


def _normalize_path(path: str) -> str:
    return os.path.abspath(os.path.normpath(str(path)))


def resolve_emergency_root(explicit_root: str | None = None) -> str:
    if explicit_root:
        return _normalize_path(explicit_root)
    override = os.environ.get(EMERGENCY_ROOT_ENV)
    if override:
        return _normalize_path(override)
    program_data = os.environ.get("ProgramData") or os.path.join(os.environ.get("SystemDrive", "C:"), "ProgramData")
    return _normalize_path(os.path.join(program_data, EMERGENCY_APP_DIR_NAME, EMERGENCY_ROOT_DIR_NAME))


def standby_dir(root: str) -> str:
    return os.path.join(resolve_emergency_root(root), STANDBY_DIR_NAME)


def active_dir(root: str) -> str:
    return os.path.join(resolve_emergency_root(root), ACTIVE_DIR_NAME)


def archived_dir(root: str) -> str:
    return os.path.join(resolve_emergency_root(root), ARCHIVED_DIR_NAME)


def active_session_dir(root: str, session_id: str) -> str:
    return os.path.join(active_dir(root), str(session_id))


def archived_session_dir(root: str, session_id: str) -> str:
    return os.path.join(archived_dir(root), str(session_id))


def standby_medical_db_path(root: str) -> str:
    return os.path.join(standby_dir(root), STANDBY_MEDICAL_DB_FILE_NAME)


def standby_settings_db_path(root: str) -> str:
    return os.path.join(standby_dir(root), STANDBY_SETTINGS_DB_FILE_NAME)


def standby_metadata_path(root: str) -> str:
    return os.path.join(standby_dir(root), STANDBY_METADATA_FILE_NAME)


def active_base_snapshot_path(root: str, session_id: str) -> str:
    return os.path.join(active_session_dir(root, session_id), ACTIVE_BASE_SNAPSHOT_FILE_NAME)


def active_medical_db_path(root: str, session_id: str) -> str:
    return os.path.join(active_session_dir(root, session_id), ACTIVE_MEDICAL_DB_FILE_NAME)


def active_settings_snapshot_path(root: str, session_id: str) -> str:
    return os.path.join(active_session_dir(root, session_id), ACTIVE_SETTINGS_SNAPSHOT_FILE_NAME)


def active_session_metadata_path(root: str, session_id: str) -> str:
    return os.path.join(active_session_dir(root, session_id), ACTIVE_SESSION_METADATA_FILE_NAME)


def emergency_client_id_path(root: str) -> str:
    return os.path.join(resolve_emergency_root(root), EMERGENCY_CLIENT_ID_FILE_NAME)
