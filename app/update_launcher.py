import json
import os
import socket
import subprocess
import sys
import time
from typing import Any, Optional

from rem_card.app.runtime_paths import get_executable_dir, is_compiled, resolve_baza_dir
from rem_card.app.update_checker import UpdateCandidate, get_update_lock_path
from rem_card.app.version import APP_VERSION


UPDATE_LOCK_STALE_SEC = 30 * 60


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


def is_update_in_progress(lock_path: Optional[str] = None) -> bool:
    if not is_compiled():
        return False

    try:
        path = lock_path or get_update_lock_path()
    except Exception:
        return False

    payload = _read_lock_payload(path)
    if not payload:
        return False

    if _payload_age(payload, path) > UPDATE_LOCK_STALE_SEC:
        try:
            os.remove(path)
        except Exception:
            return True
        return False

    return True


def describe_update_lock(lock_path: Optional[str] = None) -> str:
    try:
        path = lock_path or get_update_lock_path()
    except Exception:
        return "Обновление программы уже выполняется."

    payload = _read_lock_payload(path) or {}
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
        lock_path = get_update_lock_path(baza_dir)
        target_dir = get_executable_dir()
    except Exception:
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
        return False


def current_exe_name() -> str:
    return os.path.basename(sys.executable or "")
