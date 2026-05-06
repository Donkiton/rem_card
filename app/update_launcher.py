import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Optional

from rem_card.app.runtime_paths import get_executable_dir, is_compiled, resolve_baza_dir
from rem_card.app.update_checker import (
    UpdateCandidate,
    get_update_lock_path,
    get_update_starting_lock_path,
    update_lock_payload_matches_target,
    update_lock_scope_id,
)
from rem_card.app.version import APP_VERSION


UPDATE_LOCK_STALE_SEC = 30 * 60
UPDATE_STARTING_LOCK_STALE_SEC = 5 * 60


def _read_lock_payload(lock_path: str) -> Optional[dict[str, Any]]:
    try:
        with open(lock_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload if isinstance(payload, dict) else None
    except FileNotFoundError:
        return None
    except Exception:
        return {"timestamp": _safe_mtime(lock_path), "unreadable": True}


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except Exception:
        return 0.0


def _payload_age(payload: Optional[dict[str, Any]], lock_path: str) -> float:
    if payload and isinstance(payload.get("timestamp"), (int, float)):
        return max(0.0, time.time() - float(payload["timestamp"]))
    mtime = _safe_mtime(lock_path)
    return max(0.0, time.time() - mtime) if mtime else UPDATE_LOCK_STALE_SEC + 1


def _active_lock_payload(path: str, stale_sec: int, *, target_dir: Optional[str] = None) -> Optional[dict[str, Any]]:
    payload = _read_lock_payload(path)
    if not payload:
        return None

    if target_dir and not update_lock_payload_matches_target(payload, target_dir):
        return None

    if _payload_age(payload, path) > stale_sec:
        try:
            os.remove(path)
        except Exception:
            return payload
        return None

    return payload


def _is_lock_active(path: str, stale_sec: int, *, target_dir: Optional[str] = None) -> bool:
    return _active_lock_payload(path, stale_sec, target_dir=target_dir) is not None


def _default_lock_paths(target_dir: str) -> list[tuple[str, int]]:
    paths = [
        (get_update_lock_path(target_dir=target_dir), UPDATE_LOCK_STALE_SEC),
        (get_update_starting_lock_path(target_dir=target_dir), UPDATE_STARTING_LOCK_STALE_SEC),
        (get_update_lock_path(), UPDATE_LOCK_STALE_SEC),
        (get_update_starting_lock_path(), UPDATE_STARTING_LOCK_STALE_SEC),
    ]
    result: list[tuple[str, int]] = []
    seen: set[str] = set()
    for path, stale_sec in paths:
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        result.append((path, stale_sec))
    return result


def get_active_update_lock_payload(
    lock_path: Optional[str] = None,
    *,
    target_dir: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    if not is_compiled():
        return None

    try:
        target = os.path.abspath(target_dir or get_executable_dir())
        if lock_path:
            return _active_lock_payload(lock_path, UPDATE_LOCK_STALE_SEC, target_dir=target)
        for path, stale_sec in _default_lock_paths(target):
            payload = _active_lock_payload(path, stale_sec, target_dir=target)
            if payload:
                return payload
    except Exception:
        return None
    return None


def is_update_in_progress(lock_path: Optional[str] = None, *, target_dir: Optional[str] = None) -> bool:
    return get_active_update_lock_payload(lock_path, target_dir=target_dir) is not None


def describe_update_lock(lock_path: Optional[str] = None, *, target_dir: Optional[str] = None) -> str:
    try:
        payload = get_active_update_lock_payload(lock_path, target_dir=target_dir) or {}
    except Exception:
        return "Обновление программы уже выполняется."
    host = payload.get("host") or "неизвестно"
    started_at = payload.get("started_at") or ""
    version = payload.get("target_version") or ""
    details = []
    if version:
        details.append(f"версия {version}")
    details.append(f"компьютер: {host}")
    if started_at:
        details.append(f"начато: {started_at}")
    return "Обновление программы уже выполняется.\n\n" + "\n".join(details)


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write_starting_lock(path: str, candidate: UpdateCandidate, target_dir: str) -> bool:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if _is_lock_active(path, UPDATE_STARTING_LOCK_STALE_SEC, target_dir=target_dir):
        return False
    payload = {
        "timestamp": time.time(),
        "started_at": _now_text(),
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "state": "starting",
        "source": candidate.prog_dir,
        "target": target_dir,
        "target_scope": update_lock_scope_id(target_dir),
        "target_version": candidate.version,
    }
    raw = json.dumps(payload, ensure_ascii=True, indent=2).encode("utf-8")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, raw)
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        return False
    except Exception:
        return False


def _remove_lock_quietly(path: str):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except Exception:
        pass


def launch_update(
    candidate: UpdateCandidate,
    *,
    restart_exe: Optional[str] = None,
    wait_for_parent: bool = True,
) -> bool:
    if not is_compiled():
        return False

    updater_path = os.path.join(candidate.prog_dir, "RemCardUpdater.exe")
    if not os.path.isfile(updater_path):
        return False

    try:
        baza_dir = resolve_baza_dir()
        target_dir = get_executable_dir()
        lock_path = get_update_lock_path(baza_dir, target_dir=target_dir)
        starting_lock_path = get_update_starting_lock_path(baza_dir, target_dir=target_dir)
    except Exception:
        return False

    if is_update_in_progress(target_dir=target_dir) or not _write_starting_lock(
        starting_lock_path,
        candidate,
        target_dir,
    ):
        return False

    args = [
        updater_path,
        "--source",
        candidate.prog_dir,
        "--target",
        target_dir,
        "--baza-dir",
        baza_dir,
        "--lock",
        lock_path,
        "--starting-lock",
        starting_lock_path,
        "--parent-pid",
        str(os.getpid() if wait_for_parent else 0),
        "--current-version",
        APP_VERSION,
        "--target-version",
        candidate.version,
        "--launcher-host",
        socket.gethostname(),
    ]
    if restart_exe:
        args.extend(["--restart-exe", restart_exe])

    try:
        subprocess.Popen(args)
        return True
    except Exception:
        _remove_lock_quietly(starting_lock_path)
        return False


def current_exe_name() -> str:
    return os.path.basename(sys.executable or "")
