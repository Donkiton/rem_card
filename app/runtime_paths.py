import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from typing import Optional


BAZA_DIR_NAME = "Baza_rao3_jurnal"
DEV_BAZA_DIR_ENV = "REMCARD_DEV_BAZA_DIR"
DATA_PATH_CONFIG_NAME = "remcard_data_path.json"
LOCAL_LOG_RETENTION_DAYS = 30

REQUIRED_BAZA_DIRS = (
    "archiv",
    "archiv/db_cycle_archive",
    "backup_health",
    "backup_health/invalid_backups",
    "backup_health/reports",
    "backups",
    "backups/valid",
    "config",
    "corrupted_db",
    "database",
    "locks",
    "logs",
    "quarantine",
    "quarantine/shared_db",
    "quarantine/snapshots",
    "rem_card",
    "report",
    "session_locks",
    "settings",
    "settings/backups",
    "snapshots",
)


class DataPathConfigurationError(RuntimeError):
    pass


def is_compiled() -> bool:
    if getattr(sys, "frozen", False):
        return True
    if "__compiled__" in globals():
        return True
    exe_name = os.path.basename(sys.executable).lower()
    return exe_name not in ("python.exe", "pythonw.exe", "python", "pythonw")


def get_project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def get_executable_dir() -> str:
    if is_compiled():
        return os.path.dirname(os.path.abspath(sys.executable))
    return get_project_root()


def get_resources_dir() -> str:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return str(sys._MEIPASS)
    if is_compiled():
        base = get_executable_dir()
        internal = os.path.join(base, "_internal")
        if os.path.isdir(internal):
            return internal
        return base
    return get_project_root()


def _copy_file_atomic(source_path: str, target_path: str) -> None:
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    tmp_path = f"{target_path}.{os.getpid()}.tmp"
    try:
        shutil.copy2(source_path, tmp_path)
        os.replace(tmp_path, target_path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def sync_external_settings_from_bundle() -> int:
    """
    Runtime-настройки хранятся в центральной settings DB.

    Старые сборки копировали JSON-настройки рядом с exe. Теперь bundled JSON
    допускаются только как seed для первого импорта, поэтому наружу ничего не
    синхронизируем.
    """
    return 0


def get_data_path_config_path() -> str:
    override = os.environ.get("REMCARD_DATA_PATH_CONFIG")
    if override:
        return os.path.abspath(override)
    return os.path.join(get_executable_dir(), DATA_PATH_CONFIG_NAME)


def get_dev_baza_dir() -> str:
    override = os.environ.get(DEV_BAZA_DIR_ENV)
    if override:
        return _normalize_baza_dir(override)

    return os.path.join(get_project_root(), BAZA_DIR_NAME)


def _normalize_baza_dir(path: str) -> str:
    return os.path.abspath(os.path.normpath(str(path or "").strip().strip('"')))


def is_baza_dir_name(path: str) -> bool:
    return os.path.basename(_normalize_baza_dir(path)) == BAZA_DIR_NAME


def read_configured_baza_dir() -> Optional[str]:
    config_path = get_data_path_config_path()
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except FileNotFoundError:
        return None
    except Exception as exc:
        raise DataPathConfigurationError(f"Не удалось прочитать {config_path}: {exc}") from exc

    raw_path = payload.get("baza_dir") or payload.get("path")
    if not raw_path:
        return None
    return _normalize_baza_dir(raw_path)


def write_configured_baza_dir(baza_dir: str) -> str:
    normalized = _normalize_baza_dir(baza_dir)
    if not normalized:
        raise DataPathConfigurationError("Выберите папку базы данных.")

    config_path = get_data_path_config_path()
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    payload = {
        "baza_dir": normalized,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(config_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return config_path


def resolve_baza_dir() -> str:
    override = os.environ.get("REMCARD_BAZA_DIR")
    if override:
        return _normalize_baza_dir(override)

    if is_compiled():
        configured = read_configured_baza_dir()
        if not configured:
            raise DataPathConfigurationError(
                "Путь к папке базы не задан. Запустите RemCardPathSetup.exe."
            )
        return configured

    return get_dev_baza_dir()


def get_local_logs_dir() -> str:
    override = os.environ.get("REMCARD_LOCAL_LOGS_DIR")
    if override:
        return os.path.abspath(override)
    if is_compiled():
        return os.path.join(get_executable_dir(), "logs")
    baza_override = os.environ.get("REMCARD_BAZA_DIR")
    if baza_override:
        return os.path.join(_normalize_baza_dir(baza_override), "logs")
    return os.path.join(get_dev_baza_dir(), "logs")


def get_log_file_prefix() -> str:
    override = os.environ.get("REMCARD_LOG_PREFIX")
    if override:
        return str(override).strip() or "rem_card"

    exe_name = os.path.splitext(os.path.basename(sys.executable or ""))[0].lower()
    argv_text = " ".join(str(arg).lower() for arg in sys.argv)

    if "remcarddoctor" in exe_name or "run_doctor" in argv_text or "--role doctor" in argv_text:
        return "doctor"
    if "remcardnurse" in exe_name or "run_nurse" in argv_text or "--role nurse" in argv_text:
        return "nurse"
    if "remcardpathsetup" in exe_name or "run_path_setup" in argv_text or "--path-setup" in argv_text:
        return "path_setup"
    return "rem_card"


def cleanup_old_local_logs(log_dir: str, retention_days: int = LOCAL_LOG_RETENTION_DAYS) -> int:
    if not os.path.isdir(log_dir):
        return 0
    cutoff_ts = time.time() - (max(1, int(retention_days)) * 86400)
    removed = 0
    for name in os.listdir(log_dir):
        path = os.path.join(log_dir, name)
        if not os.path.isfile(path):
            continue
        lower = name.lower()
        if not (lower.endswith(".log") or lower.endswith(".txt")):
            continue
        try:
            if os.path.getmtime(path) < cutoff_ts:
                os.remove(path)
                removed += 1
        except Exception:
            continue
    return removed


def get_required_baza_paths(baza_dir: str) -> list[str]:
    root = _normalize_baza_dir(baza_dir)
    return [os.path.join(root, part.replace("/", os.sep)) for part in REQUIRED_BAZA_DIRS]


def get_journal_db_path(baza_dir: str) -> str:
    return os.path.join(_normalize_baza_dir(baza_dir), "archiv", "rao_journal.db")


def _probe_writable_dir(directory: str) -> tuple[bool, str]:
    try:
        os.makedirs(directory, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix=".remcard_probe_", suffix=".tmp", dir=directory)
        try:
            os.write(fd, b"1")
        finally:
            os.close(fd)
        os.remove(path)
        return True, "ok"
    except Exception as exc:
        return False, str(exc)


def create_baza_structure_and_db(baza_dir: str) -> tuple[bool, str]:
    normalized = _normalize_baza_dir(baza_dir)
    if not normalized:
        return False, "Выберите папку базы данных."

    try:
        os.makedirs(normalized, exist_ok=True)
        for directory in get_required_baza_paths(normalized):
            os.makedirs(directory, exist_ok=True)

        ok, reason = _probe_writable_dir(os.path.join(normalized, "archiv"))
        if not ok:
            return False, f"Нет доступа на запись в папку archiv: {reason}"

        db_path = get_journal_db_path(normalized)
        conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None, timeout=5.0)
        try:
            from rem_card.app.sqlite_shared import configure_connection, run_quick_check
            from rem_card.app.schema_migration_guard import ensure_unified_schema_with_migration_backup

            configure_connection(conn, profile="network")
            ensure_unified_schema_with_migration_backup(
                conn,
                db_path=db_path,
                backup_dir=os.path.join(normalized, "backups", "valid"),
                invalid_dir=os.path.join(normalized, "backup_health", "invalid_backups"),
                policy_path=os.path.join(normalized, "config", "client_policy.json"),
                baza_dir=normalized,
                lock_path=os.path.join(normalized, "archiv", "db.lock"),
                source="path_setup_schema_init",
            )
            test_row = conn.execute("SELECT 1").fetchone()
            if not test_row or int(test_row[0]) != 1:
                return False, "Тестовый запрос к БД не вернул ожидаемый результат."
            ok, result = run_quick_check(conn)
            if not ok:
                return False, f"Проверка БД не пройдена: {result}"
        finally:
            conn.close()
    except Exception as exc:
        return False, f"Не удалось подготовить папку базы: {exc}"

    return True, "ok"


def validate_baza_dir_for_runtime(baza_dir: Optional[str] = None) -> tuple[bool, str]:
    try:
        normalized = _normalize_baza_dir(baza_dir or resolve_baza_dir())
    except Exception as exc:
        return False, str(exc)

    if not os.path.isdir(normalized):
        return False, f"Папка недоступна: {normalized}"

    missing_dirs = [path for path in get_required_baza_paths(normalized) if not os.path.isdir(path)]
    if missing_dirs:
        return False, "Не найдены нужные подпапки:\n" + "\n".join(missing_dirs[:6])

    ok, reason = _probe_writable_dir(os.path.join(normalized, "session_locks"))
    if not ok:
        return False, f"Нет доступа на запись в session_locks: {reason}"

    db_path = get_journal_db_path(normalized)
    if not os.path.isfile(db_path):
        return False, f"База данных не найдена: {db_path}"

    conn = None
    try:
        from rem_card.app.sqlite_shared import configure_connection, run_quick_check

        uri = f"file:{db_path}?mode=rw"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False, isolation_level=None, timeout=5.0)
        configure_connection(conn, profile="network")
        ok, result = run_quick_check(conn)
        if not ok:
            return False, f"Проверка БД не пройдена: {result}"
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
    except Exception as exc:
        return False, f"База данных недоступна для работы: {exc}"
    finally:
        if conn:
            try:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    return True, "ok"
