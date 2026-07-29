import os

from rem_card.app.runtime_paths import resolve_baza_dir


SETTINGS_DIR_NAME = "settings"
SETTINGS_DB_FILE_NAME = "remcard_settings.db"
SETTINGS_LOCK_FILE_NAME = "settings.db.lock"
SETTINGS_BACKGROUNDS_DIR_NAME = "backgrounds"
SETTINGS_ICON_ASSETS_DIR_NAME = "icon_assets"


def get_settings_dir(baza_dir: str | None = None) -> str:
    root = os.path.abspath(os.path.normpath(baza_dir or resolve_baza_dir()))
    return os.path.join(root, SETTINGS_DIR_NAME)


def get_settings_db_path(baza_dir: str | None = None) -> str:
    return os.path.join(get_settings_dir(baza_dir), SETTINGS_DB_FILE_NAME)


def get_settings_lock_path(baza_dir: str | None = None) -> str:
    return os.path.join(get_settings_dir(baza_dir), SETTINGS_LOCK_FILE_NAME)


def get_settings_backup_dir(baza_dir: str | None = None) -> str:
    return os.path.join(get_settings_dir(baza_dir), "backups")


def get_settings_backgrounds_dir(baza_dir: str | None = None) -> str:
    return os.path.join(get_settings_dir(baza_dir), SETTINGS_BACKGROUNDS_DIR_NAME)


def get_settings_icon_assets_dir(baza_dir: str | None = None) -> str:
    return os.path.join(get_settings_dir(baza_dir), SETTINGS_ICON_ASSETS_DIR_NAME)


def get_settings_media_dir_from_db_path(
    settings_db_path: str,
    directory_name: str,
) -> str:
    raw_db_path = str(settings_db_path or "").strip()
    if not raw_db_path:
        raise ValueError("Путь к БД настроек не задан.")
    db_path = os.path.abspath(os.path.normpath(raw_db_path))
    safe_name = os.path.basename(str(directory_name or "").strip())
    if not safe_name or safe_name != str(directory_name or "").strip():
        raise ValueError("Некорректное имя каталога медиа.")
    return os.path.join(os.path.dirname(db_path), safe_name)


def get_settings_backgrounds_dir_from_db_path(settings_db_path: str) -> str:
    return get_settings_media_dir_from_db_path(
        settings_db_path,
        SETTINGS_BACKGROUNDS_DIR_NAME,
    )


def get_settings_icon_assets_dir_from_db_path(settings_db_path: str) -> str:
    return get_settings_media_dir_from_db_path(
        settings_db_path,
        SETTINGS_ICON_ASSETS_DIR_NAME,
    )
