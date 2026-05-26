import os

from rem_card.app.runtime_paths import resolve_baza_dir


SETTINGS_DIR_NAME = "settings"
SETTINGS_DB_FILE_NAME = "remcard_settings.db"
SETTINGS_LOCK_FILE_NAME = "settings.db.lock"


def get_settings_dir(baza_dir: str | None = None) -> str:
    root = os.path.abspath(os.path.normpath(baza_dir or resolve_baza_dir()))
    return os.path.join(root, SETTINGS_DIR_NAME)


def get_settings_db_path(baza_dir: str | None = None) -> str:
    return os.path.join(get_settings_dir(baza_dir), SETTINGS_DB_FILE_NAME)


def get_settings_lock_path(baza_dir: str | None = None) -> str:
    return os.path.join(get_settings_dir(baza_dir), SETTINGS_LOCK_FILE_NAME)


def get_settings_backup_dir(baza_dir: str | None = None) -> str:
    return os.path.join(get_settings_dir(baza_dir), "backups")
