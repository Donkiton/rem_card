import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from typing import Optional

from rem_card.app.roles import (
    OPERBLOCK_ROLE_KEYS,
    ROLE_OPERBLOCK,
    ROLE_OPERBLOCK_EMERGENCY,
    ROLE_OPERBLOCK_PLANNED,
    is_operblock_role,
)
from rem_card.app.sqlite_uri import build_sqlite_file_uri


DEFAULT_DEV_DATA_ROOT_NAME = "Baza_rao3_jurnal"
OPERBLOCK_DB_NOT_FOUND_MESSAGE = "БД оперблока не найдена в текущей папке базы"
DEV_BAZA_DIR_ENV = "REMCARD_DEV_BAZA_DIR"
DEV_DATABASE_CONFIG_ENV = "REMCARD_DEV_DATABASE_CONFIG"
DEV_DATABASE_CONFIG_NAME = "dev_database_paths.json"
DEV_DATABASE_CONFIG_DIR_NAME = ".remcard"
DEV_DATABASE_MIGRATION_MARKER_NAME = "dev_database_paths.migration-v1.json"
DEV_RUNTIME_BAZA_PIN_ENV = "REMCARD_INTERNAL_DEV_BAZA_PIN"
DEV_EXISTING_BAZA_ONLY_ENV = "REMCARD_DEV_EXISTING_BAZA_ONLY"
DATA_PATH_CONFIG_NAME = "remcard_data_path.json"
LOCAL_LOG_RETENTION_DAYS = 30
CRASH_REPORT_RETENTION_DAYS = 180
RUNTIME_LOG_MIGRATION_MARKER_NAME = "migration-v1.json"
RUNTIME_LOG_LOCATION_MARKER_NAME = "log_location.json"


def _startup_path_validation_ttl_sec() -> float:
    try:
        value = float(os.environ.get("REMCARD_STARTUP_PATH_VALIDATION_TTL_SEC", "30"))
    except (TypeError, ValueError):
        value = 30.0
    return max(1.0, min(120.0, value))


STARTUP_PATH_VALIDATION_TTL_SEC = _startup_path_validation_ttl_sec()
_STARTUP_PATH_VALIDATION_LOCK = threading.Lock()
_STARTUP_PATH_VALIDATION: dict[str, object] | None = None
_RUNTIME_LOG_LOCATION_LOCK = threading.Lock()
_RUNTIME_LOG_LOCATION_CACHE: tuple[tuple[str, ...], str] | None = None

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


def is_operblock_executable() -> bool:
    exe_name = os.path.basename(str(sys.executable or "")).lower()
    argv0_name = os.path.basename(str(sys.argv[0] if sys.argv else "")).lower()
    argv_text = " ".join(str(arg).lower() for arg in sys.argv)
    ui_role = str(os.environ.get("REMCARD_UI_ROLE", "")).strip().lower()
    return (
        "remcardoperblock" in exe_name
        or "remcardoperblock" in argv0_name
        or "run_operblock" in argv_text
        or any(f"--role {role}" in argv_text or f"--role={role}" in argv_text for role in OPERBLOCK_ROLE_KEYS)
        or is_operblock_role(ui_role)
    )


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


def _write_json_atomic(path: str, payload: dict[str, object]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
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


@contextmanager
def data_path_configuration_guard(timeout_sec: float = 0.5):
    """Serialize first-run/path-setup changes made by sibling role executables."""
    config_path = get_data_path_config_path()
    lock_path = f"{config_path}.setup.lock"
    token = f"{os.getpid()}:{threading.get_ident()}:{time.time_ns()}"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    deadline = time.monotonic() + max(0.1, float(timeout_sec))
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, token.encode("utf-8"))
            finally:
                os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > 10 * 60:
                    os.remove(lock_path)
                    continue
            except FileNotFoundError:
                continue
            except Exception:
                pass
            if time.monotonic() >= deadline:
                raise DataPathConfigurationError(
                    "Папка данных уже настраивается другим запущенным экземпляром RemCard."
                )
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            with open(lock_path, "r", encoding="utf-8") as fh:
                is_ours = fh.read() == token
        except Exception:
            is_ours = False
        if is_ours:
            try:
                os.remove(lock_path)
            except Exception:
                pass


def get_dev_baza_dir() -> str:
    override = os.environ.get(DEV_BAZA_DIR_ENV)
    if override:
        return _normalize_baza_dir(override)

    configured = read_dev_database_config().get("active_baza_dir")
    if configured:
        return _normalize_baza_dir(str(configured))

    return os.path.join(get_project_root(), DEFAULT_DEV_DATA_ROOT_NAME)


def _normalize_baza_dir(path: str) -> str:
    raw_path = str(path or "").strip().strip('"')
    if not raw_path:
        return ""
    return os.path.abspath(os.path.normpath(raw_path))


def get_dev_checkout_root() -> str:
    """Return the source checkout that owns developer-only local settings."""
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _get_local_remcard_dir() -> str:
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        return os.path.join(local_appdata, "RemCard")
    return os.path.join(os.path.expanduser("~"), ".remcard")


def get_legacy_dev_database_config_path() -> str:
    """Return the pre-checkout-scoping config path used by older dev versions."""
    return os.path.join(_get_local_remcard_dir(), DEV_DATABASE_CONFIG_NAME)


def get_dev_database_config_path() -> str:
    override = os.environ.get(DEV_DATABASE_CONFIG_ENV)
    if override:
        return os.path.abspath(os.path.normpath(override))
    return os.path.join(
        get_dev_checkout_root(),
        DEV_DATABASE_CONFIG_DIR_NAME,
        DEV_DATABASE_CONFIG_NAME,
    )


def get_dev_database_migration_marker_path() -> str:
    return os.path.join(
        os.path.dirname(get_dev_database_config_path()),
        DEV_DATABASE_MIGRATION_MARKER_NAME,
    )


def _mark_dev_database_migration_decided_unlocked(
    *,
    status: str,
    legacy_path: str | None = None,
) -> None:
    if os.environ.get(DEV_DATABASE_CONFIG_ENV):
        return
    marker_path = get_dev_database_migration_marker_path()
    if os.path.isfile(marker_path):
        return
    _write_json_atomic(
        marker_path,
        {
            "version": 1,
            "status": status,
            "legacy_config_path": legacy_path,
            "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    )


def _migrate_legacy_dev_database_config_unlocked() -> Optional[str]:
    """Seed this checkout once from the former user-global dev config."""
    if os.environ.get(DEV_DATABASE_CONFIG_ENV):
        return None

    config_path = get_dev_database_config_path()
    marker_path = get_dev_database_migration_marker_path()
    legacy_path = get_legacy_dev_database_config_path()
    if os.path.isfile(config_path):
        try:
            _mark_dev_database_migration_decided_unlocked(
                status="scoped_config_exists",
                legacy_path=legacy_path,
            )
        except Exception:
            pass
        return None
    if os.path.isfile(marker_path):
        return None

    imported_path = None
    if os.path.isfile(legacy_path):
        _copy_file_atomic(legacy_path, config_path)
        imported_path = legacy_path

    try:
        _mark_dev_database_migration_decided_unlocked(
            status="legacy_imported" if imported_path else "no_legacy_config",
            legacy_path=legacy_path,
        )
    except Exception:
        # The scoped config remains authoritative once copied. A marker write
        # failure must not invalidate an otherwise usable selection.
        pass
    return imported_path


def get_dev_local_operation_lock_path(lock_key: str) -> str:
    """Keep dev-only operation mutexes away from a selected production database."""
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        base_dir = os.path.join(local_appdata, "RemCard", "dev_operation_locks")
    else:
        base_dir = os.path.join(os.path.expanduser("~"), ".remcard", "dev_operation_locks")
    safe_key = "".join(
        char if char.isalnum() or char in ("-", "_") else "_"
        for char in str(lock_key or "operation").lower()
    )
    return os.path.join(base_dir, f"{safe_key or 'operation'}.lock")


def _normalize_dev_baza_dirs(paths) -> list[str]:
    if isinstance(paths, (str, bytes, os.PathLike)):
        paths = [paths]

    normalized_paths: list[str] = []
    seen: set[str] = set()
    for raw_path in paths or ():
        if not str(raw_path or "").strip().strip('"'):
            continue
        normalized = _normalize_baza_dir(os.fsdecode(raw_path))
        key = os.path.normcase(normalized)
        if key in seen:
            continue
        seen.add(key)
        normalized_paths.append(normalized)
    return normalized_paths


@contextmanager
def _dev_database_config_guard(timeout_sec: float = 5.0):
    config_path = get_dev_database_config_path()
    lock_path = f"{config_path}.lock"
    token = f"{os.getpid()}:{threading.get_ident()}:{time.time_ns()}"
    try:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        deadline = time.monotonic() + max(0.1, float(timeout_sec))
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(lock_path) > 60.0:
                        os.remove(lock_path)
                        continue
                except FileNotFoundError:
                    continue
                except Exception:
                    pass
                if time.monotonic() >= deadline:
                    raise DataPathConfigurationError(
                        "Локальные настройки списка dev-баз временно заняты другим процессом."
                    )
                time.sleep(0.05)
                continue

            try:
                payload = token.encode("utf-8")
                offset = 0
                try:
                    while offset < len(payload):
                        written = os.write(fd, payload[offset:])
                        if written <= 0:
                            raise OSError("Не удалось записать токен блокировки dev-настроек")
                        offset += written
                finally:
                    os.close(fd)
            except Exception:
                # The O_EXCL file belongs to this process.  Do not leave a
                # minute-long orphan if token persistence itself fails.
                try:
                    os.remove(lock_path)
                except Exception:
                    pass
                raise
            break
    except Exception as exc:
        if isinstance(exc, DataPathConfigurationError):
            raise
        raise DataPathConfigurationError(
            f"Не удалось заблокировать локальные настройки dev-базы: {exc}"
        ) from exc

    try:
        yield
    finally:
        try:
            with open(lock_path, "r", encoding="utf-8") as fh:
                is_ours = fh.read() == token
        except FileNotFoundError:
            is_ours = False
        except Exception:
            is_ours = False
        if is_ours:
            for attempt in range(10):
                try:
                    os.remove(lock_path)
                    break
                except FileNotFoundError:
                    break
                except PermissionError:
                    if attempt >= 9:
                        break
                    time.sleep(0.03)


def _read_dev_database_config_unlocked() -> dict[str, object]:
    """Read the developer-local database selection and its saved path list."""
    config_path = get_dev_database_config_path()
    try:
        _migrate_legacy_dev_database_config_unlocked()
        with open(config_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except FileNotFoundError:
        return {"active_baza_dir": None, "saved_baza_dirs": []}
    except Exception as exc:
        _quarantine_broken_dev_database_config(config_path)
        return {
            "active_baza_dir": None,
            "saved_baza_dirs": [],
            "load_error": f"Не удалось прочитать {config_path}: {exc}",
        }

    if not isinstance(payload, dict):
        _quarantine_broken_dev_database_config(config_path)
        return {
            "active_baza_dir": None,
            "saved_baza_dirs": [],
            "load_error": f"Некорректный формат файла настроек dev-базы: {config_path}",
        }

    try:
        raw_active = (
            payload.get("active_baza_dir")
            or payload.get("active_path")
            or payload.get("baza_dir")
        )
        if raw_active is not None and not isinstance(raw_active, str):
            raise TypeError("active_baza_dir должен быть строкой")
        active = None
        if str(raw_active or "").strip().strip('"'):
            active = _normalize_baza_dir(str(raw_active))

        raw_saved = payload.get("saved_baza_dirs")
        if raw_saved is None:
            raw_saved = payload.get("saved_paths") or []
        if not isinstance(raw_saved, list) or any(
            not isinstance(path, str) for path in raw_saved
        ):
            raise TypeError("saved_baza_dirs должен быть списком строк")
        saved = _normalize_dev_baza_dirs(raw_saved)
    except Exception as exc:
        _quarantine_broken_dev_database_config(config_path)
        return {
            "active_baza_dir": None,
            "saved_baza_dirs": [],
            "load_error": f"Некорректное содержимое {config_path}: {exc}",
        }

    if active:
        active_key = os.path.normcase(active)
        saved = [active] + [path for path in saved if os.path.normcase(path) != active_key]

    return {
        "active_baza_dir": active,
        "saved_baza_dirs": saved,
    }


def read_dev_database_config() -> dict[str, object]:
    with _dev_database_config_guard():
        return _read_dev_database_config_unlocked()


def _quarantine_broken_dev_database_config(config_path: str) -> Optional[str]:
    if not os.path.isfile(config_path):
        return None
    quarantine_path = f"{config_path}.broken.{int(time.time())}"
    try:
        os.replace(config_path, quarantine_path)
        return quarantine_path
    except Exception:
        return None


def _write_dev_database_config_unlocked(
    active_baza_dir: str | None,
    saved_baza_dirs=(),
) -> str:
    """Atomically persist the active dev database outside every database folder."""
    active = None
    if str(active_baza_dir or "").strip().strip('"'):
        active = _normalize_baza_dir(str(active_baza_dir))

    saved = _normalize_dev_baza_dirs(saved_baza_dirs)
    if active:
        active_key = os.path.normcase(active)
        saved = [path for path in saved if os.path.normcase(path) != active_key]
        saved.insert(0, active)

    config_path = get_dev_database_config_path()
    config_dir = os.path.dirname(config_path)
    os.makedirs(config_dir, exist_ok=True)
    payload = {
        "version": 2,
        "active_baza_dir": active,
        "saved_baza_dirs": saved,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        _write_json_atomic(config_path, payload)
    except Exception as exc:
        raise DataPathConfigurationError(f"Не удалось сохранить {config_path}: {exc}") from exc
    try:
        _mark_dev_database_migration_decided_unlocked(status="scoped_config_written")
    except Exception:
        pass
    return config_path


def write_dev_database_config(
    active_baza_dir: str | None,
    saved_baza_dirs=(),
) -> str:
    with _dev_database_config_guard():
        return _write_dev_database_config_unlocked(active_baza_dir, saved_baza_dirs)


def save_dev_baza_dir(baza_dir: str) -> str:
    if not str(baza_dir or "").strip().strip('"'):
        raise DataPathConfigurationError("Выберите папку базы данных.")
    with _dev_database_config_guard():
        config = _read_dev_database_config_unlocked()
        return _write_dev_database_config_unlocked(
            baza_dir,
            config.get("saved_baza_dirs") or [],
        )


def add_saved_dev_baza_dir(baza_dir: str) -> str:
    with _dev_database_config_guard():
        config = _read_dev_database_config_unlocked()
        active = config.get("active_baza_dir")
        saved = list(config.get("saved_baza_dirs") or [])
        saved.append(baza_dir)
        return _write_dev_database_config_unlocked(active, saved)


def remove_saved_dev_baza_dir(baza_dir: str) -> str:
    with _dev_database_config_guard():
        config = _read_dev_database_config_unlocked()
        active = config.get("active_baza_dir")
        remove_key = os.path.normcase(_normalize_baza_dir(baza_dir))
        saved = [
            path
            for path in (config.get("saved_baza_dirs") or [])
            if os.path.normcase(_normalize_baza_dir(str(path))) != remove_key
        ]
        return _write_dev_database_config_unlocked(active, saved)


def get_operblock_test_baza_dir() -> str:
    return resolve_baza_dir()


def resolve_operblock_baza_dir(path: str | None = None) -> str:
    if path:
        normalized = _normalize_baza_dir(path)
        if os.path.basename(os.path.dirname(normalized)).lower() == "archiv":
            return os.path.dirname(os.path.dirname(normalized))
        if os.path.basename(normalized).lower() == "archiv":
            return os.path.dirname(normalized)
        return normalized
    return resolve_baza_dir()


def is_operblock_baza_dir(path: str) -> bool:
    return os.path.isfile(get_journal_db_path(resolve_operblock_baza_dir(path)))


def validate_operblock_baza_dir(baza_dir: str) -> tuple[bool, str]:
    normalized = resolve_operblock_baza_dir(baza_dir)
    if not os.path.isdir(normalized):
        return False, f"Папка базы недоступна: {normalized}"
    db_path = get_journal_db_path(normalized)
    if not os.path.isfile(db_path):
        return False, f"{OPERBLOCK_DB_NOT_FOUND_MESSAGE}: {db_path}"
    return True, "ok"


def configure_operblock_runtime_path(role: str | None) -> Optional[dict[str, str]]:
    role_key = str(role or "").strip().lower()
    if not is_operblock_role(role_key):
        return None

    os.environ["REMCARD_LOCAL_FIRST_SYNC"] = "0"
    os.environ["REMCARD_LOCAL_OUTBOX_SYNC"] = "0"
    os.environ["REMCARD_UI_ROLE"] = ROLE_OPERBLOCK
    baza_dir = resolve_baza_dir()

    return {
        "role": role_key or ROLE_OPERBLOCK,
        "data_root": baza_dir,
        "db_path": get_journal_db_path(baza_dir),
        "db_profile": "network",
        "local_db_used": "false",
    }


def read_configured_baza_dir() -> Optional[str]:
    config_path = get_data_path_config_path()
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except FileNotFoundError:
        return None
    except Exception as exc:
        raise DataPathConfigurationError(f"Не удалось прочитать {config_path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise DataPathConfigurationError(
            f"Некорректный формат {config_path}: ожидался JSON-объект."
        )
    raw_path = payload.get("baza_dir") or payload.get("path")
    if not raw_path:
        return None
    if not isinstance(raw_path, str):
        raise DataPathConfigurationError(
            f"Некорректный путь в {config_path}: ожидалась строка."
        )
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
    _write_json_atomic(config_path, payload)
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


def get_runtime_logs_dir() -> str:
    """Return the preferred local log directory without touching Baza."""
    override = os.environ.get("REMCARD_LOCAL_LOGS_DIR")
    if override:
        return os.path.abspath(override)
    base_dir = get_executable_dir() if is_compiled() else get_dev_checkout_root()
    return os.path.join(base_dir, "logs")


def get_legacy_runtime_logs_dir() -> str:
    """Return the AppData log directory used by releases before migration v1."""
    return os.path.join(_get_local_remcard_dir(), "logs")


def get_legacy_crash_spool_dir() -> str:
    """Return the AppData crash spool used by releases before migration v1."""
    return os.path.join(_get_local_remcard_dir(), "crash-outbox")


def get_runtime_log_directory_candidates() -> tuple[str, ...]:
    """Return preferred and emergency fallback locations, in priority order."""
    fallback = os.path.join(tempfile.gettempdir(), "RemCard", "logs")
    candidates: list[str] = []
    for raw_path in (get_runtime_logs_dir(), fallback):
        normalized = os.path.abspath(raw_path)
        if os.path.normcase(normalized) not in {
            os.path.normcase(candidate) for candidate in candidates
        }:
            candidates.append(normalized)
    return tuple(candidates)


def _runtime_log_directory_is_writable(path: str) -> tuple[bool, str]:
    probe_path = os.path.join(
        path,
        f".remcard-write-test-{os.getpid()}-{threading.get_ident()}",
    )
    try:
        os.makedirs(path, exist_ok=True)
        fd = os.open(probe_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        os.remove(probe_path)
        return True, ""
    except OSError as exc:
        try:
            if os.path.exists(probe_path):
                os.remove(probe_path)
        except OSError:
            pass
        return False, str(exc)


def record_runtime_log_location(
    effective_dir: str,
    *,
    preferred_dir: str | None = None,
    fallback_reason: str = "",
) -> None:
    """Persist a small AppData pointer when logs had to leave the program folder."""
    preferred = os.path.abspath(preferred_dir or get_runtime_logs_dir())
    effective = os.path.abspath(effective_dir)
    if os.path.normcase(preferred) == os.path.normcase(effective):
        return
    try:
        _write_json_atomic(
            os.path.join(_get_local_remcard_dir(), RUNTIME_LOG_LOCATION_MARKER_NAME),
            {
                "version": 1,
                "status": "fallback",
                "preferred_log_dir": preferred,
                "effective_log_dir": effective,
                "reason": str(fallback_reason or ""),
                "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
    except Exception:
        pass


def get_writable_runtime_logs_dir() -> str:
    """Resolve a writable local log directory without resolving the database."""
    global _RUNTIME_LOG_LOCATION_CACHE
    candidates = get_runtime_log_directory_candidates()
    cache_key = tuple(os.path.normcase(path) for path in candidates)
    with _RUNTIME_LOG_LOCATION_LOCK:
        if (
            _RUNTIME_LOG_LOCATION_CACHE is not None
            and _RUNTIME_LOG_LOCATION_CACHE[0] == cache_key
        ):
            return _RUNTIME_LOG_LOCATION_CACHE[1]

        failures: list[str] = []
        for candidate in candidates:
            writable, reason = _runtime_log_directory_is_writable(candidate)
            if not writable:
                failures.append(f"{candidate}: {reason}")
                continue
            _RUNTIME_LOG_LOCATION_CACHE = (cache_key, candidate)
            if candidate != candidates[0]:
                record_runtime_log_location(
                    candidate,
                    preferred_dir=candidates[0],
                    fallback_reason="; ".join(failures),
                )
            return candidate
    raise OSError("Нет доступного локального каталога логов: " + "; ".join(failures))


def _paths_are_same(left: str, right: str) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _copy_directory_snapshot(source_dir: str, target_dir: str) -> tuple[int, list[str]]:
    copied = 0
    errors: list[str] = []
    if not os.path.isdir(source_dir):
        return copied, errors
    for root, dir_names, file_names in os.walk(source_dir, followlinks=False):
        dir_names[:] = [
            name
            for name in dir_names
            if not os.path.islink(os.path.join(root, name))
        ]
        relative_root = os.path.relpath(root, source_dir)
        for name in file_names:
            source_path = os.path.join(root, name)
            if os.path.islink(source_path):
                continue
            relative_path = name if relative_root == "." else os.path.join(relative_root, name)
            target_path = os.path.join(target_dir, relative_path)
            try:
                _copy_file_atomic(source_path, target_path)
                copied += 1
            except Exception as exc:
                errors.append(f"{source_path}: {exc}")
    return copied, errors


def migrate_legacy_runtime_logs(target_log_dir: str | None = None) -> dict[str, object]:
    """
    Copy legacy AppData logs into the visible program-local log tree.

    Source files are intentionally retained for the first migration release.
    The copied snapshot is forensic history; crash_reports separately imports
    pending crash files into the active outbox.
    """
    target_root = os.path.abspath(target_log_dir or get_runtime_logs_dir())
    legacy_logs = os.path.abspath(get_legacy_runtime_logs_dir())
    legacy_crashes = os.path.abspath(get_legacy_crash_spool_dir())
    migration_root = os.path.join(target_root, "migrated-appdata")
    marker_path = os.path.join(migration_root, RUNTIME_LOG_MIGRATION_MARKER_NAME)
    result: dict[str, object] = {
        "copied": 0,
        "errors": [],
        "marker": marker_path,
        "skipped": False,
    }
    if os.path.isfile(marker_path):
        result["skipped"] = True
        return result
    if _paths_are_same(target_root, legacy_logs):
        result["skipped"] = True
        return result

    copied_logs, log_errors = _copy_directory_snapshot(
        legacy_logs,
        os.path.join(migration_root, "logs"),
    )
    copied_crashes, crash_errors = _copy_directory_snapshot(
        legacy_crashes,
        os.path.join(migration_root, "crash-outbox"),
    )
    errors = [*log_errors, *crash_errors]
    result["copied"] = copied_logs + copied_crashes
    result["errors"] = errors
    if errors:
        return result
    _write_json_atomic(
        marker_path,
        {
            "version": 1,
            "status": "copied" if result["copied"] else "no_legacy_data",
            "copied_files": int(result["copied"]),
            "legacy_logs_dir": legacy_logs,
            "legacy_crash_spool_dir": legacy_crashes,
            "target_log_dir": target_root,
            "source_files_retained": True,
            "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    )
    return result


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
    if (
        "remcardoperblockemergency" in exe_name
        or "run_operblock_emergency" in argv_text
        or f"--role {ROLE_OPERBLOCK_EMERGENCY}" in argv_text
        or f"--role={ROLE_OPERBLOCK_EMERGENCY}" in argv_text
    ):
        return ROLE_OPERBLOCK_EMERGENCY
    if (
        "remcardoperblockplanned" in exe_name
        or "run_operblock_planned" in argv_text
        or f"--role {ROLE_OPERBLOCK_PLANNED}" in argv_text
        or f"--role={ROLE_OPERBLOCK_PLANNED}" in argv_text
    ):
        return ROLE_OPERBLOCK_PLANNED
    if "remcardoperblock" in exe_name or "run_operblock" in argv_text or "--role operblock" in argv_text:
        return ROLE_OPERBLOCK
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


def _startup_path_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(str(path))))


def mark_startup_baza_paths_validated(baza_dir: str, paths: list[str] | tuple[str, ...]) -> None:
    """Record one successful, process-local startup directory validation."""
    global _STARTUP_PATH_VALIDATION
    root_key = _startup_path_key(_normalize_baza_dir(baza_dir))
    path_keys = {_startup_path_key(path) for path in paths if path}
    path_keys.add(root_key)
    token = {
        "root_key": root_key,
        "path_keys": frozenset(path_keys),
        "validated_at": time.monotonic(),
    }
    with _STARTUP_PATH_VALIDATION_LOCK:
        _STARTUP_PATH_VALIDATION = token


def startup_baza_paths_recently_validated(
    baza_dir: str,
    required_paths: list[str] | tuple[str, ...],
    *,
    max_age_sec: float | None = None,
) -> bool:
    """Return True only for a fresh token covering this root and every path."""
    ttl = STARTUP_PATH_VALIDATION_TTL_SEC if max_age_sec is None else max(0.0, float(max_age_sec))
    root_key = _startup_path_key(_normalize_baza_dir(baza_dir))
    required_keys = {_startup_path_key(path) for path in required_paths if path}
    required_keys.add(root_key)
    with _STARTUP_PATH_VALIDATION_LOCK:
        token = _STARTUP_PATH_VALIDATION
        if token is None:
            return False
        token_root = token.get("root_key")
        token_paths = token.get("path_keys")
        validated_at = token.get("validated_at")
    if token_root != root_key or not isinstance(token_paths, frozenset):
        return False
    try:
        age_sec = time.monotonic() - float(validated_at)
    except (TypeError, ValueError):
        return False
    return 0.0 <= age_sec <= ttl and required_keys.issubset(token_paths)


def clear_startup_baza_path_validation() -> None:
    """Clear the process token; primarily useful for isolated startup tests."""
    global _STARTUP_PATH_VALIDATION
    with _STARTUP_PATH_VALIDATION_LOCK:
        _STARTUP_PATH_VALIDATION = None


def get_journal_db_path(baza_dir: str) -> str:
    return os.path.join(_normalize_baza_dir(baza_dir), "archiv", "rao_journal.db")


def get_existing_sqlite_rw_uri(db_path: str) -> str:
    """Return a writable SQLite URI that refuses to create a missing file."""
    return build_sqlite_file_uri(db_path, mode="rw")


def is_selected_dev_database_file(db_path: str, *relative_parts: str) -> bool:
    """Whether *db_path* belongs to the saved dev root opened existing-only."""
    if is_compiled() or os.environ.get(DEV_EXISTING_BAZA_ONLY_ENV) != "1":
        return False
    selected_root = str(os.environ.get("REMCARD_BAZA_DIR") or "").strip()
    if not selected_root:
        return False
    expected = os.path.join(_normalize_baza_dir(selected_root), *relative_parts)
    actual_key = os.path.normcase(os.path.abspath(os.path.normpath(str(db_path))))
    expected_key = os.path.normcase(os.path.abspath(os.path.normpath(expected)))
    return actual_key == expected_key


def validate_dev_baza_dir(baza_dir: str) -> tuple[bool, str]:
    """Validate a dev database selection without touching session role locks."""
    if not str(baza_dir or "").strip().strip('"'):
        return False, "Укажите папку базы данных."

    normalized = _normalize_baza_dir(baza_dir)
    if not os.path.isdir(normalized):
        return False, f"Папка базы недоступна: {normalized}"

    db_path = get_journal_db_path(normalized)
    if not os.path.isfile(db_path):
        return False, f"В выбранной папке не найдена база данных: {db_path}"

    ok, message = _validate_existing_sqlite_schema(
        db_path,
        required_tables={"patients", "admissions", "beds"},
        label="Основная база данных",
    )
    if not ok:
        return False, message

    settings_db_path = os.path.join(normalized, "settings", "remcard_settings.db")
    if not os.path.isfile(settings_db_path):
        return False, f"В выбранной папке не найдена база настроек: {settings_db_path}"
    ok, message = _validate_existing_sqlite_schema(
        settings_db_path,
        required_tables={"settings_meta", "app_settings"},
        label="База настроек",
    )
    if not ok:
        return False, message

    missing_dirs = [
        path for path in get_required_baza_paths(normalized)
        if not os.path.isdir(path)
    ]
    if missing_dirs:
        relative_dirs = [os.path.relpath(path, normalized) for path in missing_dirs]
        shown = ", ".join(relative_dirs[:6])
        suffix = f" и ещё {len(relative_dirs) - 6}" if len(relative_dirs) > 6 else ""
        return False, f"В папке базы не хватает служебных каталогов: {shown}{suffix}"

    return True, "ok"


def _validate_existing_sqlite_schema(
    db_path: str,
    *,
    required_tables: set[str],
    label: str,
) -> tuple[bool, str]:
    conn = None
    try:
        db_uri = get_existing_sqlite_rw_uri(db_path)
        conn = sqlite3.connect(
            db_uri,
            uri=True,
            check_same_thread=False,
            isolation_level=None,
            timeout=5.0,
        )
        conn.execute("PRAGMA query_only = ON")
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        actual_tables = {str(row[0]) for row in rows if row and row[0]}
        missing = sorted(required_tables - actual_tables)
        if missing:
            return False, f"{label} не похожа на RemCard: нет таблиц {', '.join(missing)}"
    except Exception as exc:
        return False, f"{label} недоступна для работы: {exc}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return True, "ok"


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
        root_existed = os.path.isdir(normalized)
        existing_entries = set(os.listdir(normalized)) if root_existed else set()
        db_path = get_journal_db_path(normalized)
        db_existed = os.path.isfile(db_path)

        if existing_entries and not db_existed:
            allowed_top_level = {part.split("/", 1)[0] for part in REQUIRED_BAZA_DIRS}
            foreign_entries = sorted(name for name in existing_entries if name not in allowed_top_level)
            if foreign_entries:
                shown = ", ".join(foreign_entries[:6])
                suffix = f" и ещё {len(foreign_entries) - 6}" if len(foreign_entries) > 6 else ""
                return False, (
                    "Выбранная папка не пуста и не похожа на папку данных RemCard. "
                    f"Посторонние элементы: {shown}{suffix}"
                )

        if db_existed:
            ok, message = _validate_existing_sqlite_schema(
                db_path,
                required_tables={"patients", "admissions", "beds"},
                label="Основная база данных",
            )
            if not ok:
                return False, message

        os.makedirs(normalized, exist_ok=True)
        for directory in get_required_baza_paths(normalized):
            os.makedirs(directory, exist_ok=True)

        ok, reason = _probe_writable_dir(os.path.join(normalized, "archiv"))
        if not ok:
            return False, f"Нет доступа на запись в папку archiv: {reason}"

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
        from rem_card.services.crash_reports import ensure_shared_crash_directories

        ensure_shared_crash_directories(normalized)
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

        uri = build_sqlite_file_uri(db_path, mode="rw")
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
