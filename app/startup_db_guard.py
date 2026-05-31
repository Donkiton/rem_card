import json
import os
import re
import shutil
import socket
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from rem_card.app.db_access_classifier import classify_database_access_error
from rem_card.app.jsonl_audit_log import write_audit_event
from rem_card.app.runtime_paths import (
    DataPathConfigurationError,
    get_journal_db_path,
    get_required_baza_paths,
    resolve_baza_dir,
)
from rem_card.app.version import APP_VERSION
from rem_card.app.sqlite_shared import (
    NETWORK_SAFE_DB_PROFILE,
    FileWriteLock,
    configure_connection,
    run_quick_check,
    validate_sqlite_file,
)


REQUIRED_CLIENT_POLICY_VERSION = APP_VERSION
RECOVERY_LOCK_STALE_SEC = 10 * 60
RECOVERY_LOCK_WAIT_SEC = 10 * 60
DB_LOCK_WAIT_SEC = 60
LOCK_HEARTBEAT_SEC = 5


class StartupPolicyError(RuntimeError):
    pass


@dataclass
class StartupGuardResult:
    ok: bool
    recovered: bool = False
    user_message: str = ""
    technical_reason: str = ""
    restored_from: str = ""
    quarantine_path: str = ""
    baza_dir: str = ""


StartupDbGuardResult = StartupGuardResult


STARTUP_UNAVAILABLE_CATEGORIES = {
    "network_unavailable",
    "network_unavailable_or_missing",
    "missing_db",
    "path_inaccessible",
}


class _LockHeartbeat:
    def __init__(self, lock_path: str, *, role: Optional[str], source: str):
        self.lock_path = lock_path
        self.role = role
        self.source = source
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"{source}Heartbeat", daemon=True)

    def start(self):
        self._write()
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=1.0)

    def _payload(self) -> dict[str, Any]:
        return {
            "timestamp": time.time(),
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "role": self.role,
            "source": self.source,
        }

    def _write(self):
        try:
            with open(self.lock_path, "w", encoding="utf-8") as fh:
                json.dump(self._payload(), fh, ensure_ascii=True)
        except Exception:
            pass

    def _run(self):
        while not self._stop.wait(LOCK_HEARTBEAT_SEC):
            self._write()


def _now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _same_path(left: str, right: str) -> bool:
    try:
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))
    except Exception:
        return False


def _version_tuple(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    public_version = str(value or "").split("+", 1)[0].split("-", 1)[0]
    for part in public_version.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        if digits:
            parts.append(int(digits))
    return tuple(parts or [0])


def _is_semver(value: str) -> bool:
    return bool(re.match(r"^\d+\.\d+\.\d+(?:[+-][0-9A-Za-z.-]+)?$", str(value or "").strip()))


def _is_legacy_date_version(value: str) -> bool:
    return bool(re.match(r"^20\d{2}\.\d{1,2}\.\d{1,2}(?:\.\d+)?$", str(value or "").strip()))


def _compare_client_versions(current: str, minimum: str) -> int:
    current_text = str(current or "").strip()
    minimum_text = str(minimum or "").strip()
    current_is_legacy = _is_legacy_date_version(current_text)
    minimum_is_legacy = _is_legacy_date_version(minimum_text)
    current_is_semver = _is_semver(current_text) and not current_is_legacy
    minimum_is_semver = _is_semver(minimum_text) and not minimum_is_legacy

    if current_is_semver and minimum_is_legacy:
        return 1
    if current_is_legacy and minimum_is_semver:
        return -1

    current_tuple = _version_tuple(current_text)
    minimum_tuple = _version_tuple(minimum_text)
    size = max(len(current_tuple), len(minimum_tuple))
    current_tuple = current_tuple + (0,) * (size - len(current_tuple))
    minimum_tuple = minimum_tuple + (0,) * (size - len(minimum_tuple))

    if current_tuple < minimum_tuple:
        return -1
    if current_tuple > minimum_tuple:
        return 1
    return 0


def _is_confirmed_corruption(reason: str) -> bool:
    text = str(reason or "").lower()
    corruption_markers = (
        "database disk image is malformed",
        "file is not a database",
        "database corruption",
        "sqlite_master",
        "malformed",
        "not a database",
        "quick_check failed",
        "integrity_check failed",
    )
    return any(marker in text for marker in corruption_markers)


def _is_retryable_availability_error(reason: str) -> bool:
    text = str(reason or "").lower()
    retryable_markers = (
        "database is locked",
        "database table is locked",
        "database schema is locked",
        "busy",
        "locked",
    )
    return any(marker in text for marker in retryable_markers)


def is_confirmed_db_corruption_reason(reason: str) -> bool:
    return _is_confirmed_corruption(reason)


def is_retryable_db_availability_reason(reason: str) -> bool:
    return _is_retryable_availability_error(reason)


def _availability_user_message(reason: str) -> str:
    text = str(reason or "").lower()
    if "locked" in text or "busy" in text:
        return "База сейчас занята другим процессом. Повторите запуск через несколько минут."
    if "permission" in text or "access is denied" in text or "readonly" in text or "read-only" in text:
        return "Нет прав на запись или чтение в сетевой папке базы. Работа временно недоступна."
    if "does not exist" in text or "unable to open database file" in text or "network" in text:
        return "База временно недоступна. Проверьте доступ к сетевой папке и повторите запуск."
    if "disk i/o error" in text or "disk io error" in text:
        return "База временно недоступна из-за ошибки диска или сети. Повторите запуск позже."
    return "Не удалось проверить базу данных. Работа временно недоступна. Сообщите ответственному."


def _lock_timeout_user_message(reason: str) -> str:
    text = str(reason or "").lower()
    if "recovery lock" in text:
        return "База сейчас восстанавливается другим клиентом. Повторите запуск через несколько минут."
    return "База сейчас занята другим процессом. Повторите запуск через несколько минут."


def _is_missing_db_file(reason: str) -> bool:
    text = str(reason or "").lower()
    return "database file does not exist" in text


def _startup_access_category(reason: str, *, confirmed_corruption: bool = False) -> str:
    if _is_missing_db_file(reason):
        return "missing_db"
    if _is_retryable_availability_error(reason):
        return "locked_busy"
    if confirmed_corruption or _is_confirmed_corruption(reason):
        return "corruption"
    category = classify_database_access_error(RuntimeError(str(reason or "")))
    if category == "network_unavailable":
        return "network_unavailable"
    if category == "locked_busy":
        return "locked_busy"
    if category in {"policy_block", "schema_incompatible"}:
        return str(category)
    return "unknown"


def _is_startup_unavailable_category(category: str) -> bool:
    return str(category or "") in STARTUP_UNAVAILABLE_CATEGORIES


def startup_auto_recovery_allowed(db_path: str, failure_reason: str, *, confirmed_corruption: bool | None = None) -> bool:
    if not db_path or not os.path.isfile(db_path):
        return False
    reason = str(failure_reason or "")
    if confirmed_corruption is None:
        confirmed_corruption = _is_confirmed_corruption(reason)
    if _is_missing_db_file(reason) or _is_retryable_availability_error(reason):
        return False
    if classify_database_access_error(RuntimeError(reason)) in {"network_unavailable", "locked_busy"}:
        return False
    return bool(confirmed_corruption)


def _startup_unavailable_result(
    *,
    role: Optional[str],
    baza_dir: str,
    reason: str,
    category: str,
) -> StartupGuardResult:
    write_audit_event(
        "shared_db_startup_unavailable",
        baza_dir=baza_dir,
        role=role,
        details={"reason": reason, "category": category, "recovery_allowed": False},
    )
    return StartupGuardResult(
        ok=False,
        user_message=_availability_user_message(reason),
        technical_reason=reason,
        baza_dir=baza_dir,
    )


def _ensure_guard_dirs(baza_dir: str):
    for path in get_required_baza_paths(baza_dir):
        os.makedirs(path, exist_ok=True)
    extra_dirs = (
        os.path.join(baza_dir, "config"),
        os.path.join(baza_dir, "locks"),
        os.path.join(baza_dir, "quarantine"),
        os.path.join(baza_dir, "quarantine", "shared_db"),
        os.path.join(baza_dir, "quarantine", "snapshots"),
        os.path.join(baza_dir, "logs"),
        os.path.join(baza_dir, "backups"),
        os.path.join(baza_dir, "backups", "valid"),
        os.path.join(baza_dir, "snapshots"),
        os.path.join(baza_dir, "backup_health"),
        os.path.join(baza_dir, "backup_health", "invalid_backups"),
    )
    for path in extra_dirs:
        os.makedirs(path, exist_ok=True)


def _default_client_policy() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "min_client_version": REQUIRED_CLIENT_POLICY_VERSION,
        "required_db_profile": NETWORK_SAFE_DB_PROFILE,
        "wal_allowed_on_shared_db": False,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _load_or_create_client_policy(baza_dir: str, role: Optional[str]) -> dict[str, Any]:
    config_dir = os.path.join(baza_dir, "config")
    os.makedirs(config_dir, exist_ok=True)
    policy_path = os.path.join(config_dir, "client_policy.json")

    if not os.path.exists(policy_path):
        policy = _default_client_policy()
        with open(policy_path, "w", encoding="utf-8") as fh:
            json.dump(policy, fh, ensure_ascii=False, indent=2)
        write_audit_event(
            "client_policy_created",
            baza_dir=baza_dir,
            role=role,
            details={"policy_path": policy_path, "required_db_profile": NETWORK_SAFE_DB_PROFILE},
        )
        write_audit_event(
            "client_policy_loaded",
            baza_dir=baza_dir,
            role=role,
            details={"policy_path": policy_path, "min_client_version": policy.get("min_client_version")},
        )
        return policy

    with open(policy_path, "r", encoding="utf-8") as fh:
        policy = json.load(fh)

    changed = False
    if not policy.get("required_db_profile"):
        policy["required_db_profile"] = NETWORK_SAFE_DB_PROFILE
        changed = True
    if "wal_allowed_on_shared_db" not in policy:
        policy["wal_allowed_on_shared_db"] = False
        changed = True
    if not policy.get("min_client_version"):
        policy["min_client_version"] = REQUIRED_CLIENT_POLICY_VERSION
        changed = True
    if changed:
        with open(policy_path, "w", encoding="utf-8") as fh:
            json.dump(policy, fh, ensure_ascii=False, indent=2)

    min_version = str(policy.get("min_client_version") or REQUIRED_CLIENT_POLICY_VERSION)
    if _compare_client_versions(APP_VERSION, min_version) < 0:
        raise StartupPolicyError("Версия программы устарела. Работа с базой заблокирована. Обновите программу.")

    if str(policy.get("required_db_profile") or "").strip() != NETWORK_SAFE_DB_PROFILE:
        raise StartupPolicyError("Профиль доступа к базе не соответствует требованиям. Обновите программу.")

    if bool(policy.get("wal_allowed_on_shared_db")):
        raise StartupPolicyError("Работа заблокирована: WAL для сетевой базы запрещён.")

    write_audit_event(
        "client_policy_loaded",
        baza_dir=baza_dir,
        role=role,
        details={"policy_path": policy_path, "min_client_version": min_version},
    )
    return policy


def update_client_policy_min_version(
    policy_path: str,
    min_client_version: str,
    *,
    role: Optional[str] = None,
    baza_dir: Optional[str] = None,
    reason: str = "schema_migration",
) -> bool:
    os.makedirs(os.path.dirname(policy_path), exist_ok=True)
    created = False
    try:
        with open(policy_path, "r", encoding="utf-8") as fh:
            policy = json.load(fh)
        if not isinstance(policy, dict):
            policy = {}
    except FileNotFoundError:
        policy = _default_client_policy()
        created = True

    changed = created
    if not policy.get("schema_version"):
        policy["schema_version"] = 1
        changed = True
    if not policy.get("required_db_profile"):
        policy["required_db_profile"] = NETWORK_SAFE_DB_PROFILE
        changed = True
    if "wal_allowed_on_shared_db" not in policy:
        policy["wal_allowed_on_shared_db"] = False
        changed = True

    target = str(min_client_version or REQUIRED_CLIENT_POLICY_VERSION)
    current = str(policy.get("min_client_version") or "")
    if not current or _compare_client_versions(current, target) < 0:
        policy["min_client_version"] = target
        policy["min_client_version_updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        policy["min_client_version_reason"] = reason
        changed = True

    if changed:
        with open(policy_path, "w", encoding="utf-8") as fh:
            json.dump(policy, fh, ensure_ascii=False, indent=2)
        write_audit_event(
            "client_policy_min_version_updated",
            baza_dir=baza_dir,
            role=role,
            details={
                "policy_path": policy_path,
                "min_client_version": policy.get("min_client_version"),
                "reason": reason,
            },
        )
    return changed


def _acquire_lock_with_wait(
    lock_path: str,
    *,
    stale_timeout_sec: float,
    wait_sec: float,
    owner_id: str,
    source: str,
    role: Optional[str],
    baza_dir: str,
) -> tuple[FileWriteLock, _LockHeartbeat]:
    lock = FileWriteLock(lock_path, stale_timeout_sec=stale_timeout_sec)
    deadline = time.time() + max(1.0, wait_sec)
    last_log = 0.0
    while time.time() < deadline:
        _remove_stale_startup_lock_if_needed(
            lock_path,
            stale_timeout_sec=stale_timeout_sec,
            source=source,
            role=role,
            baza_dir=baza_dir,
        )
        if lock.acquire(owner_id=owner_id, source=source):
            heartbeat = _LockHeartbeat(lock_path, role=role, source=source)
            heartbeat.start()
            write_audit_event(
                f"{source}_lock_acquired",
                baza_dir=baza_dir,
                role=role,
                details={"lock_path": lock_path},
            )
            return lock, heartbeat
        now = time.time()
        if now - last_log >= 10:
            last_log = now
            write_audit_event(
                f"{source}_lock_wait",
                baza_dir=baza_dir,
                role=role,
                details={"lock_path": lock_path},
            )
        time.sleep(2.0)
    raise TimeoutError(f"Could not acquire {source} lock: {lock_path}")


def _remove_stale_startup_lock_if_needed(
    lock_path: str,
    *,
    stale_timeout_sec: float,
    source: str,
    role: Optional[str],
    baza_dir: str,
):
    try:
        with open(lock_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except FileNotFoundError:
        return
    except Exception:
        return

    ts = payload.get("timestamp")
    if not isinstance(ts, (int, float)):
        stale = True
    else:
        stale = (time.time() - ts) > stale_timeout_sec
    if not stale:
        return

    try:
        os.remove(lock_path)
    except FileNotFoundError:
        return
    except Exception:
        return

    event = "stale_recovery_lock_removed" if source == "recovery" else f"stale_{source}_lock_removed"
    write_audit_event(
        event,
        baza_dir=baza_dir,
        role=role,
        details={"lock_path": lock_path, "stale_payload": payload},
    )


def _release_lock(lock: Optional[FileWriteLock], heartbeat: Optional[_LockHeartbeat]):
    if heartbeat:
        heartbeat.stop()
    if lock:
        lock.release()


def _check_quick(db_path: str) -> tuple[bool, str, bool]:
    if not os.path.exists(db_path):
        return False, "database file does not exist", False
    conn = None
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False, isolation_level=None, timeout=5.0)
        configure_connection(conn, readonly=True, profile="network")
        ok, result = run_quick_check(conn)
        return ok, result, not ok
    except Exception as exc:
        reason = str(exc)
        return False, reason, _is_confirmed_corruption(reason)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _check_quick_with_retries(
    db_path: str,
    *,
    baza_dir: str,
    role: Optional[str],
    attempts: int = 3,
) -> tuple[bool, str, bool]:
    last_result = ""
    last_confirmed_corruption = False
    for attempt in range(1, max(1, attempts) + 1):
        ok, result, confirmed_corruption = _check_quick(db_path)
        if ok or confirmed_corruption or not _is_retryable_availability_error(result):
            return ok, result, confirmed_corruption
        last_result = result
        last_confirmed_corruption = confirmed_corruption
        write_audit_event(
            "db_guard_retry",
            baza_dir=baza_dir,
            role=role,
            details={"db_path": db_path, "attempt": attempt, "reason": result},
        )
        time.sleep(2.0)
    return False, last_result, last_confirmed_corruption


def _apply_network_safe_profile(db_path: str) -> dict[str, Any]:
    conn = None
    try:
        uri = f"file:{db_path}?mode=rw"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False, isolation_level=None, timeout=10.0)
        configure_connection(conn, profile="network")
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
        mmap_size = conn.execute("PRAGMA mmap_size").fetchone()[0]
        return {
            "db_profile": NETWORK_SAFE_DB_PROFILE,
            "journal_mode": str(journal_mode).upper(),
            "synchronous": synchronous,
            "mmap_size": mmap_size,
        }
    finally:
        if conn:
            conn.close()


def _apply_network_safe_profile_with_lock(
    *,
    baza_dir: str,
    db_path: str,
    role: Optional[str],
    owner_id: str,
) -> dict[str, Any]:
    db_lock = None
    db_heartbeat = None
    try:
        db_lock, db_heartbeat = _acquire_lock_with_wait(
            os.path.join(baza_dir, "archiv", "db.lock"),
            stale_timeout_sec=120,
            wait_sec=DB_LOCK_WAIT_SEC,
            owner_id=owner_id,
            source="db_profile",
            role=role,
            baza_dir=baza_dir,
        )
        profile = _apply_network_safe_profile(db_path)
        write_audit_event(
            "sqlite_pragmas_applied",
            baza_dir=baza_dir,
            role=role,
            details={"db_path": db_path, **profile},
        )
        return profile
    finally:
        _release_lock(db_lock, db_heartbeat)


def _iter_recovery_candidates(baza_dir: str, db_path: str) -> list[str]:
    dirs = [
        os.path.join(baza_dir, "backups", "valid"),
        os.path.join(baza_dir, "backups"),
        os.path.join(baza_dir, "snapshots"),
    ]
    candidates: list[str] = []
    seen: set[str] = set()
    for directory in dirs:
        if not os.path.isdir(directory):
            continue
        for name in os.listdir(directory):
            if not name.lower().endswith(".db"):
                continue
            full_path = os.path.join(directory, name)
            if not os.path.isfile(full_path) or _same_path(full_path, db_path):
                continue
            key = os.path.normcase(os.path.abspath(full_path))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(full_path)
    candidates.sort(key=os.path.getmtime, reverse=True)
    return candidates


def _quarantine_invalid_candidate(candidate: str, baza_dir: str, reason: str, role: Optional[str]):
    if not os.path.exists(candidate):
        return
    if os.path.normcase(os.path.abspath(candidate)).find(os.path.normcase(os.path.join(baza_dir, "snapshots"))) >= 0:
        target_dir = os.path.join(baza_dir, "quarantine", "snapshots")
    else:
        target_dir = os.path.join(baza_dir, "backup_health", "invalid_backups")
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, f"invalid_{_now_stamp()}_{os.path.basename(candidate)}")
    counter = 1
    while os.path.exists(target):
        target = os.path.join(target_dir, f"invalid_{_now_stamp()}_{counter}_{os.path.basename(candidate)}")
        counter += 1
    try:
        os.replace(candidate, target)
        candidate_meta = f"{candidate}.meta.json"
        if os.path.exists(candidate_meta):
            os.replace(candidate_meta, f"{target}.meta.json")
        with open(f"{target}.reason.txt", "w", encoding="utf-8") as fh:
            fh.write(f"time={datetime.now().isoformat()}\n")
            fh.write(f"source={candidate}\n")
            fh.write(f"reason={reason}\n")
        write_audit_event(
            "recovery_snapshot_rejected",
            baza_dir=baza_dir,
            role=role,
            details={"candidate": candidate, "quarantine_path": target, "reason": reason},
        )
    except Exception as exc:
        write_audit_event(
            "recovery_snapshot_rejected",
            baza_dir=baza_dir,
            role=role,
            details={"candidate": candidate, "reason": reason, "quarantine_error": str(exc)},
        )


def _select_latest_valid_source(baza_dir: str, db_path: str, role: Optional[str]) -> tuple[Optional[str], str]:
    for candidate in _iter_recovery_candidates(baza_dir, db_path):
        ok, reason = validate_sqlite_file(candidate)
        if ok:
            write_audit_event(
                "recovery_snapshot_selected",
                baza_dir=baza_dir,
                role=role,
                details={"source_path": candidate},
            )
            return candidate, "ok"
        _quarantine_invalid_candidate(candidate, baza_dir, reason, role)
    return None, "no valid backup or snapshot found"


def _quarantine_current_db(db_path: str, baza_dir: str, role: Optional[str], context: dict[str, Any]) -> str:
    quarantine_root = os.path.join(baza_dir, "quarantine", "shared_db", f"corrupted_{_now_stamp()}")
    os.makedirs(quarantine_root, exist_ok=True)

    moved: list[dict[str, str]] = []
    for suffix in ("", "-journal", "-wal", "-shm"):
        source = f"{db_path}{suffix}"
        if not os.path.exists(source):
            continue
        target = os.path.join(quarantine_root, os.path.basename(source))
        os.replace(source, target)
        moved.append({"source": source, "target": target})

    context = dict(context)
    context.update(
        {
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "db_path": db_path,
            "moved_files": moved,
        }
    )
    with open(os.path.join(quarantine_root, "recovery_context.json"), "w", encoding="utf-8") as fh:
        json.dump(context, fh, ensure_ascii=False, indent=2)

    write_audit_event(
        "quarantine_created",
        baza_dir=baza_dir,
        role=role,
        details={"quarantine_path": quarantine_root, "moved_files": moved},
    )
    return quarantine_root


def _quarantine_existing_db_sidecars(db_path: str, baza_dir: str, role: Optional[str], context: dict[str, Any]) -> str:
    existing_paths = [f"{db_path}{suffix}" for suffix in ("", "-journal", "-wal", "-shm") if os.path.exists(f"{db_path}{suffix}")]
    if not existing_paths:
        return ""
    return _quarantine_current_db(db_path, baza_dir, role, context)


def _read_lock_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload if isinstance(payload, dict) else {}
    except FileNotFoundError:
        return None
    except Exception:
        return {"unreadable": True}


def _host_aliases() -> set[str]:
    aliases: set[str] = set()
    for value in (socket.gethostname(), socket.getfqdn(), os.environ.get("COMPUTERNAME"), os.environ.get("HOSTNAME")):
        if not value:
            continue
        host = str(value).strip().lower()
        if not host:
            continue
        aliases.add(host)
        aliases.add(host.split(".")[0])
    return aliases


def _is_local_host_value(host_value: Any) -> bool:
    if not host_value:
        return False
    host = str(host_value).strip().lower()
    if not host:
        return False
    aliases = _host_aliases()
    return host in aliases or host.split(".")[0] in aliases


def _is_pid_alive_local(pid_value: Any) -> bool:
    try:
        pid = int(pid_value)
    except Exception:
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
    return True


def _is_current_process_lock(payload: dict[str, Any]) -> bool:
    try:
        pid = int(payload.get("pid") or -1)
    except Exception:
        return False
    return _is_local_host_value(payload.get("host")) and pid == os.getpid()


def _role_lock_is_stale(path: str, payload: dict[str, Any], stale_timeout_sec: float) -> bool:
    if payload.get("unreadable"):
        return False
    if _is_local_host_value(payload.get("host")) and not _is_pid_alive_local(payload.get("pid")):
        return True
    ts = payload.get("timestamp")
    if isinstance(ts, (int, float)) and (time.time() - float(ts)) > stale_timeout_sec:
        return True
    try:
        return (time.time() - os.path.getmtime(path)) > stale_timeout_sec
    except Exception:
        return False


def _active_other_role_locks(baza_dir: str, *, stale_timeout_sec: float = 75.0) -> list[dict[str, Any]]:
    lock_dir = os.path.join(baza_dir, "session_locks")
    if not os.path.isdir(lock_dir):
        return []

    active: list[dict[str, Any]] = []
    for name in os.listdir(lock_dir):
        if not name.lower().endswith(".lock"):
            continue
        path = os.path.join(lock_dir, name)
        payload = _read_lock_json(path)
        if not payload:
            continue
        if _is_current_process_lock(payload):
            continue
        if _role_lock_is_stale(path, payload, stale_timeout_sec):
            continue
        active.append(
            {
                "path": path,
                "role": payload.get("role") or os.path.splitext(name)[0],
                "host": payload.get("host"),
                "pid": payload.get("pid"),
                "unreadable": bool(payload.get("unreadable")),
            }
        )
    return active


def _restore_from_source(source_path: str, db_path: str) -> str:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    temp_path = f"{db_path}.restore_tmp_{os.getpid()}_{_now_stamp()}"
    shutil.copy2(source_path, temp_path)
    ok, reason = validate_sqlite_file(temp_path)
    if not ok:
        try:
            os.remove(temp_path)
        except Exception:
            pass
        raise RuntimeError(f"selected recovery source failed temp validation: {reason}")
    os.replace(temp_path, db_path)
    ok, reason = validate_sqlite_file(db_path)
    if not ok:
        raise RuntimeError(f"restored database failed validation: {reason}")
    return db_path


def _recover_shared_db(
    *,
    baza_dir: str,
    db_path: str,
    role: Optional[str],
    failure_reason: str,
) -> StartupGuardResult:
    if not startup_auto_recovery_allowed(db_path, failure_reason):
        category = _startup_access_category(failure_reason)
        write_audit_event(
            "shared_db_auto_recovery_blocked",
            baza_dir=baza_dir,
            role=role,
            details={
                "reason": failure_reason,
                "category": category,
                "recovery_allowed": False,
                "db_exists": os.path.isfile(db_path),
            },
        )
        return StartupGuardResult(
            ok=False,
            user_message=_availability_user_message(failure_reason),
            technical_reason=failure_reason,
            baza_dir=baza_dir,
        )

    owner_id = f"{socket.gethostname()}:{os.getpid()}:startup_db_guard"
    db_lock_path = os.path.join(baza_dir, "archiv", "db.lock")
    db_lock = None
    db_heartbeat = None
    try:
        db_lock, db_heartbeat = _acquire_lock_with_wait(
            db_lock_path,
            stale_timeout_sec=120,
            wait_sec=DB_LOCK_WAIT_SEC,
            owner_id=owner_id,
            source="db_write",
            role=role,
            baza_dir=baza_dir,
        )
        ok_after_lock, check_after_lock, confirmed_after_lock = _check_quick_with_retries(
            db_path,
            baza_dir=baza_dir,
            role=role,
            attempts=2,
        )
        if ok_after_lock:
            profile = _apply_network_safe_profile(db_path)
            write_audit_event(
                "db_guard_ok",
                baza_dir=baza_dir,
                role=role,
                details={
                    "db_path": db_path,
                    "quick_check": "ok",
                    "after_db_lock_recheck": True,
                    **profile,
                },
            )
            return StartupGuardResult(ok=True, baza_dir=baza_dir)

        failure_reason = check_after_lock or failure_reason
        if not startup_auto_recovery_allowed(db_path, failure_reason, confirmed_corruption=confirmed_after_lock):
            write_audit_event(
                "shared_db_auto_recovery_skipped",
                baza_dir=baza_dir,
                role=role,
                details={
                    "reason": failure_reason,
                    "classification": _startup_access_category(
                        failure_reason,
                        confirmed_corruption=confirmed_after_lock,
                    ),
                },
            )
            return StartupGuardResult(
                ok=False,
                user_message=_availability_user_message(failure_reason),
                technical_reason=failure_reason,
                baza_dir=baza_dir,
            )

        active_clients = _active_other_role_locks(baza_dir)
        if active_clients:
            write_audit_event(
                "shared_db_auto_recovery_blocked",
                baza_dir=baza_dir,
                role=role,
                details={
                    "reason": "active_client",
                    "active_clients": active_clients[:5],
                    "initial_failure": failure_reason,
                },
            )
            return StartupGuardResult(
                ok=False,
                user_message="Автовосстановление базы заблокировано: другое рабочее место активно. Закройте второй клиент или выполните ручное восстановление ответственным.",
                technical_reason="active client lock present",
                baza_dir=baza_dir,
            )

        source_path, select_reason = _select_latest_valid_source(baza_dir, db_path, role)
        if not source_path:
            write_audit_event(
                "shared_db_auto_recovery_failed",
                baza_dir=baza_dir,
                role=role,
                details={"reason": select_reason, "initial_failure": failure_reason},
            )
            return StartupGuardResult(
                ok=False,
                user_message="Не найдена рабочая копия базы. Работа временно недоступна. Сообщите ответственному.",
                technical_reason=select_reason,
                baza_dir=baza_dir,
            )

        quarantine_path = _quarantine_existing_db_sidecars(
            db_path,
            baza_dir,
            role,
            {
                "event": "shared_db_auto_recovery_start",
                "failure_reason": failure_reason,
                "selected_source": source_path,
                "host": socket.gethostname(),
                "pid": os.getpid(),
                "role": role,
                "app_version": APP_VERSION,
            },
        )
        _restore_from_source(source_path, db_path)
        profile = _apply_network_safe_profile(db_path)
        write_audit_event(
            "sqlite_pragmas_applied",
            baza_dir=baza_dir,
            role=role,
            details={"db_path": db_path, **profile},
        )
        write_audit_event(
            "shared_db_auto_recovery_success",
            baza_dir=baza_dir,
            role=role,
            details={
                "restored_from": source_path,
                "quarantine_path": quarantine_path,
                "quick_check_after": "ok",
                **profile,
            },
        )
        return StartupGuardResult(
            ok=True,
            recovered=True,
            user_message="База восстановлена из последней рабочей копии. Работа продолжена.",
            restored_from=source_path,
            quarantine_path=quarantine_path,
            baza_dir=baza_dir,
        )
    except Exception as exc:
        write_audit_event(
            "shared_db_auto_recovery_failed",
            baza_dir=baza_dir,
            role=role,
            details={"reason": str(exc), "initial_failure": failure_reason},
        )
        return StartupGuardResult(
            ok=False,
            user_message="Не удалось автоматически восстановить базу. Работа временно недоступна. Сообщите ответственному.",
            technical_reason=str(exc),
            baza_dir=baza_dir,
        )
    finally:
        _release_lock(db_lock, db_heartbeat)


def recover_shared_db_with_locks(
    *,
    baza_dir: str,
    db_path: str,
    role: Optional[str],
    failure_reason: str,
) -> StartupGuardResult:
    if not startup_auto_recovery_allowed(db_path, failure_reason):
        category = _startup_access_category(failure_reason)
        write_audit_event(
            "shared_db_auto_recovery_blocked",
            baza_dir=baza_dir,
            role=role,
            details={
                "reason": failure_reason,
                "category": category,
                "recovery_allowed": False,
                "db_exists": os.path.isfile(db_path),
            },
        )
        return StartupGuardResult(
            ok=False,
            user_message=_availability_user_message(failure_reason),
            technical_reason=failure_reason,
            baza_dir=baza_dir,
        )

    recovery_lock = None
    recovery_heartbeat = None
    owner_id = f"{socket.gethostname()}:{os.getpid()}:startup_db_guard"
    recovery_lock_path = os.path.join(baza_dir, "locks", "recovery.lock")
    try:
        recovery_lock, recovery_heartbeat = _acquire_lock_with_wait(
            recovery_lock_path,
            stale_timeout_sec=RECOVERY_LOCK_STALE_SEC,
            wait_sec=RECOVERY_LOCK_WAIT_SEC,
            owner_id=owner_id,
            source="recovery",
            role=role,
            baza_dir=baza_dir,
        )
        return _recover_shared_db(
            baza_dir=baza_dir,
            db_path=db_path,
            role=role,
            failure_reason=failure_reason,
        )
    except TimeoutError as exc:
        write_audit_event(
            "shared_db_auto_recovery_blocked",
            baza_dir=baza_dir,
            role=role,
            details={"reason": "recovery_lock_busy", "lock_path": recovery_lock_path, "error": str(exc)},
        )
        return StartupGuardResult(
            ok=False,
            user_message=_lock_timeout_user_message(str(exc)),
            technical_reason=str(exc),
            baza_dir=baza_dir,
        )
    finally:
        _release_lock(recovery_lock, recovery_heartbeat)


def run_startup_db_guard(role: Optional[str] = None) -> StartupGuardResult:
    try:
        baza_dir = resolve_baza_dir()
    except DataPathConfigurationError as exc:
        return StartupGuardResult(ok=False, user_message=str(exc), technical_reason=str(exc))
    except Exception as exc:
        return StartupGuardResult(
            ok=False,
            user_message="Путь к папке базы недоступен. Запустите RemCardPathSetup.exe.",
            technical_reason=str(exc),
        )

    if not os.path.isdir(baza_dir):
        return StartupGuardResult(
            ok=False,
            user_message=f"Папка базы недоступна: {baza_dir}",
            technical_reason=f"baza dir does not exist: {baza_dir}",
            baza_dir=baza_dir,
        )

    try:
        _ensure_guard_dirs(baza_dir)
        write_audit_event("db_guard_start", baza_dir=baza_dir, role=role, details={"baza_dir": baza_dir})
        _load_or_create_client_policy(baza_dir, role)
    except Exception as exc:
        write_audit_event(
            "db_guard_failed",
            baza_dir=baza_dir,
            role=role,
            details={"stage": "policy_or_dirs", "reason": str(exc)},
        )
        if isinstance(exc, StartupPolicyError):
            return StartupGuardResult(
                ok=False,
                user_message=str(exc),
                technical_reason=str(exc),
                baza_dir=baza_dir,
            )
        return StartupGuardResult(
            ok=False,
            user_message="Не удалось подготовить защитный контур базы. Проверьте доступ к сетевой папке.",
            technical_reason=str(exc),
            baza_dir=baza_dir,
        )

    owner_id = f"{socket.gethostname()}:{os.getpid()}:startup_db_guard"
    db_path = get_journal_db_path(baza_dir)
    try:
        ok, result, confirmed_corruption = _check_quick_with_retries(
            db_path,
            baza_dir=baza_dir,
            role=role,
        )
        if ok:
            profile = _apply_network_safe_profile_with_lock(
                baza_dir=baza_dir,
                db_path=db_path,
                role=role,
                owner_id=owner_id,
            )
            write_audit_event(
                "db_guard_ok",
                baza_dir=baza_dir,
                role=role,
                details={"db_path": db_path, "quick_check": "ok", **profile},
            )
            return StartupGuardResult(ok=True, baza_dir=baza_dir)

        category = _startup_access_category(result, confirmed_corruption=confirmed_corruption)
        if _is_startup_unavailable_category(category):
            write_audit_event(
                "shared_db_missing_detected" if category == "missing_db" else "shared_db_unavailable",
                baza_dir=baza_dir,
                role=role,
                details={"db_path": db_path, "reason": result, "category": category},
            )
            return _startup_unavailable_result(
                role=role,
                baza_dir=baza_dir,
                reason=result,
                category=category,
            )

        if not confirmed_corruption:
            write_audit_event(
                "shared_db_unavailable",
                baza_dir=baza_dir,
                role=role,
                details={"db_path": db_path, "reason": result},
            )
            return StartupGuardResult(
                ok=False,
                user_message=_availability_user_message(result),
                technical_reason=result,
                baza_dir=baza_dir,
            )

        write_audit_event(
            "shared_db_corrupt_detected",
            baza_dir=baza_dir,
            role=role,
            details={"db_path": db_path, "quick_check": result},
        )
        write_audit_event(
            "shared_db_auto_recovery_start",
            baza_dir=baza_dir,
            role=role,
            details={"db_path": db_path, "reason": result},
        )
        return recover_shared_db_with_locks(baza_dir=baza_dir, db_path=db_path, role=role, failure_reason=result)
    except TimeoutError as exc:
        write_audit_event(
            "db_guard_failed",
            baza_dir=baza_dir,
            role=role,
            details={"stage": "lock", "reason": str(exc)},
        )
        return StartupGuardResult(
            ok=False,
            user_message=_lock_timeout_user_message(str(exc)),
            technical_reason=str(exc),
            baza_dir=baza_dir,
        )
    except Exception as exc:
        write_audit_event(
            "db_guard_failed",
            baza_dir=baza_dir,
            role=role,
            details={"stage": "check_or_recovery", "reason": str(exc)},
        )
        return StartupGuardResult(
            ok=False,
            user_message="Не удалось проверить базу данных. Работа временно недоступна. Сообщите ответственному.",
            technical_reason=str(exc),
            baza_dir=baza_dir,
        )
