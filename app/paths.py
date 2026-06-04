import os
import shutil
import sys

from rem_card.app.runtime_paths import (
    get_dev_baza_dir,
    get_executable_dir,
    get_local_logs_dir,
    get_project_root,
    is_compiled as _runtime_is_compiled,
    resolve_baza_dir,
)

def is_compiled() -> bool:
    """Проверяет, запущено ли приложение в скомпилированном виде (PyInstaller/Nuitka)."""
    return _runtime_is_compiled()

def get_resources_dir() -> str:
    """
    Универсальное определение корня ресурсов:
    - PyInstaller onefile → _MEIPASS
    - PyInstaller onedir → _internal
    - Nuitka → папка exe
    - dev → корень проекта
    """
    import os
    import sys

    # PyInstaller onefile
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return sys._MEIPASS

    # Любая compiled версия (PyInstaller onedir + Nuitka)
    if getattr(sys, 'frozen', False) or "__compiled__" in globals():
        base = os.path.dirname(os.path.abspath(sys.executable))

        # 🔴 PyInstaller onedir кладёт ресурсы в _internal
        internal = os.path.join(base, "_internal")
        if os.path.exists(internal):
            return internal

        # Nuitka (ресурсы рядом с exe)
        return base

    # dev режим
    return get_base_dir()
    
def get_base_dir() -> str:
    """
    Определяет корень данных.

    В dev-режиме это корень проекта C:\\Project для ресурсов. В compiled-режиме
    это выбранная пользователем папка базы из Prog\\remcard_data_path.json.
    """
    if is_compiled():
        return resolve_baza_dir()
    else:
        return get_project_root()

def _project_dictionaries_dir() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "dictionaries"))

def _compiled_external_dictionaries_dir() -> str:
    return os.path.join(get_executable_dir(), "rem_card", "data", "dictionaries")

def _compiled_bundled_dictionaries_dir() -> str:
    return os.path.join(get_resources_dir(), "rem_card", "data", "dictionaries")

def _copy_missing_json_files(source_dir: str, target_dir: str):
    if not os.path.isdir(source_dir):
        return

    os.makedirs(target_dir, exist_ok=True)
    for name in sorted(os.listdir(source_dir)):
        source_path = os.path.join(source_dir, name)
        if not os.path.isfile(source_path) or not name.lower().endswith(".json"):
            continue

        target_path = os.path.join(target_dir, name)
        if os.path.exists(target_path):
            continue

        shutil.copy2(source_path, target_path)

def ensure_external_dictionaries_initialized() -> str:
    """
    В compiled-режиме _internal хранит seed-словари для первого импорта в
    settings DB. Внешние JSON рядом с exe больше не создаются и не являются
    runtime-хранилищем.
    """
    if not is_compiled():
        return _project_dictionaries_dir()

    return _compiled_bundled_dictionaries_dir()

def get_seed_dir() -> str:
    if is_compiled():
        return ensure_external_dictionaries_initialized()
    return _project_dictionaries_dir()

def get_icon_dir() -> str:
    if is_compiled():
        return os.path.join(get_resources_dir(), "rem_card", "icon")
    else:
        return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "icon"))

def get_mkb_dir() -> str:
    if not is_compiled():
        return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "mkb"))
    return os.path.join(get_resources_dir(), "rem_card", "data", "mkb")

def get_patient_assets_dir() -> str:
    if not is_compiled():
        return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "patient_assets"))
    return os.path.join(get_resources_dir(), "rem_card", "data", "patient_assets")

def get_user_dict_dir() -> str:
    return get_seed_dir()

NETWORK_ROOT = get_base_dir()
_BAZA_DIR_OVERRIDE = os.environ.get("REMCARD_BAZA_DIR")
if _BAZA_DIR_OVERRIDE:
    BAZA_DIR = os.path.abspath(os.path.normpath(_BAZA_DIR_OVERRIDE.strip().strip('"')))
elif is_compiled():
    BAZA_DIR = resolve_baza_dir()
elif not is_compiled():
    BAZA_DIR = get_dev_baza_dir()
else:
    BAZA_DIR = os.path.join(NETWORK_ROOT, "Baza_rao3_jurnal")

BAZA_LOGS_DIR = os.path.join(BAZA_DIR, "logs")
LOGS_DIR = get_local_logs_dir()
ARCHIV_DIR = os.path.join(BAZA_DIR, "archiv")
REM_CARD_DIR = os.path.join(BAZA_DIR, "rem_card")
REPORT_DIR = os.path.join(BAZA_DIR, "report")
BACKUPS_RC_DIR = os.path.join(BAZA_DIR, "backups")
BACKUPS_VALID_DIR = os.path.join(BACKUPS_RC_DIR, "valid")
SNAPSHOTS_DIR = os.path.join(BAZA_DIR, "snapshots")
CORRUPTED_DB_DIR = os.path.join(BAZA_DIR, "corrupted_db")
BACKUP_HEALTH_DIR = os.path.join(BAZA_DIR, "backup_health")
BACKUP_VALIDATION_REPORTS_DIR = os.path.join(BACKUP_HEALTH_DIR, "reports")
INVALID_BACKUPS_DIR = os.path.join(BACKUP_HEALTH_DIR, "invalid_backups")
CONFIG_DIR = os.path.join(BAZA_DIR, "config")
LOCKS_DIR = os.path.join(BAZA_DIR, "locks")
CLIENT_POLICY_PATH = os.path.join(CONFIG_DIR, "client_policy.json")
QUARANTINE_DIR = os.path.join(BAZA_DIR, "quarantine")
SHARED_DB_QUARANTINE_DIR = os.path.join(QUARANTINE_DIR, "shared_db")
SNAPSHOT_QUARANTINE_DIR = os.path.join(QUARANTINE_DIR, "snapshots")
ROLE_LOCKS_DIR = os.path.join(BAZA_DIR, "session_locks")
DB_CYCLE_ARCHIVE_DIR = os.path.join(ARCHIV_DIR, "db_cycle_archive")
SETTINGS_DIR = os.path.join(BAZA_DIR, "settings")
SETTINGS_BACKUPS_DIR = os.path.join(SETTINGS_DIR, "backups")

SEED_DIR = get_seed_dir()
USER_DICT_DIR = get_user_dict_dir()
MKB_DIR = get_mkb_dir()
MKB_DB_PATH = os.path.join(MKB_DIR, "mkb10.db")
PATIENT_ASSETS_DIR = get_patient_assets_dir()

# Пути к базам данных и координации записи
JOURNAL_DB_PATH = os.path.join(ARCHIV_DIR, "rao_journal.db")
REMCARD_DB_PATH = JOURNAL_DB_PATH
LEGACY_REMCARD_DB_PATH = os.path.join(REM_CARD_DIR, "rem_cards_data.db")
DB_LOCK_PATH = os.path.join(ARCHIV_DIR, "db.lock")
DB_ROTATION_LOCK_PATH = os.path.join(ARCHIV_DIR, "db_rotation.lock")
RECOVERY_LOCK_PATH = os.path.join(LOCKS_DIR, "recovery.lock")

LOCAL_APPDATA = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
LOCAL_CACHE_DIR = os.path.join(LOCAL_APPDATA, "RemCard", "cache")
LOCAL_CACHE_SUFFIX = str(
    os.environ.get("REMCARD_LOCAL_CACHE_SUFFIX")
    or f"{os.getpid()}"
).strip()
LOCAL_REMCARD_REPLICA_PATH = os.path.join(LOCAL_CACHE_DIR, f"rao_journal_local_replica_{LOCAL_CACHE_SUFFIX}.db")
LOCAL_JOURNAL_REPLICA_PATH = os.path.join(LOCAL_CACHE_DIR, f"rao_journal_local_replica_journal_{LOCAL_CACHE_SUFFIX}.db")
LOCAL_REMCARD_OUTBOX_PATH = os.path.join(LOCAL_CACHE_DIR, f"remcard_outbox_{LOCAL_CACHE_SUFFIX}.db")
LOCAL_JOURNAL_OUTBOX_PATH = os.path.join(LOCAL_CACHE_DIR, f"journal_outbox_{LOCAL_CACHE_SUFFIX}.db")

def get_role_lock_path(role: str) -> str:
    safe_role = str(role or "unknown").lower()
    return os.path.join(ROLE_LOCKS_DIR, f"{safe_role}.lock")

# Убедимся, что папки существуют
def ensure_directories():
    allow_shared_create = (not is_compiled()) or os.environ.get("REMCARD_PATH_SETUP_MODE") == "1"
    shared_dirs = [
        BAZA_DIR,
        BAZA_LOGS_DIR,
        ARCHIV_DIR,
        REM_CARD_DIR,
        REPORT_DIR,
        BACKUPS_RC_DIR,
        BACKUPS_VALID_DIR,
        SNAPSHOTS_DIR,
        CORRUPTED_DB_DIR,
        BACKUP_HEALTH_DIR,
        BACKUP_VALIDATION_REPORTS_DIR,
        INVALID_BACKUPS_DIR,
        CONFIG_DIR,
        LOCKS_DIR,
        QUARANTINE_DIR,
        SHARED_DB_QUARANTINE_DIR,
        SNAPSHOT_QUARANTINE_DIR,
        ROLE_LOCKS_DIR,
        DB_CYCLE_ARCHIVE_DIR,
        SETTINGS_DIR,
        SETTINGS_BACKUPS_DIR,
    ]

    for directory in shared_dirs:
        if allow_shared_create:
            os.makedirs(directory, exist_ok=True)
        elif not os.path.isdir(directory):
            raise FileNotFoundError(f"Required shared directory is unavailable: {directory}")

    os.makedirs(LOGS_DIR, exist_ok=True)
    os.makedirs(LOCAL_CACHE_DIR, exist_ok=True)
