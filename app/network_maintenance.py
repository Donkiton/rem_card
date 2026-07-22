from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass
from typing import Any

from rem_card.app.sqlite_shared import FileWriteLock


NETWORK_SESSION_STALE_SEC = 75.0
NETWORK_MAINTENANCE_LOCK_FILE = "network_maintenance.lock"


@dataclass(frozen=True)
class ActiveNetworkSession:
    role: str
    host: str
    pid: int | None
    path: str
    age_sec: float


def _host_aliases() -> set[str]:
    aliases: set[str] = set()
    for candidate in (socket.gethostname(), socket.getfqdn(), os.environ.get("COMPUTERNAME"), os.environ.get("HOSTNAME")):
        normalized = str(candidate or "").strip().lower()
        if not normalized:
            continue
        aliases.add(normalized)
        aliases.add(normalized.split(".")[0])
    return aliases


def _is_current_process(payload: dict[str, Any]) -> bool:
    try:
        pid = int(payload.get("pid"))
    except (TypeError, ValueError):
        return False
    host = str(payload.get("host") or "").strip().lower()
    return pid == os.getpid() and (host in _host_aliases() or host.split(".")[0] in _host_aliases())


def find_active_network_sessions(
    session_locks_dir: str,
    *,
    stale_sec: float = NETWORK_SESSION_STALE_SEC,
) -> list[ActiveNetworkSession]:
    raw_directory = str(session_locks_dir or "").strip()
    if not raw_directory:
        return []
    directory = os.path.abspath(os.path.normpath(raw_directory))
    if not os.path.isdir(directory):
        return []

    active: list[ActiveNetworkSession] = []
    now = time.time()
    for name in sorted(os.listdir(directory)):
        if not name.lower().endswith(".lock") or name == NETWORK_MAINTENANCE_LOCK_FILE:
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            if not isinstance(payload, dict) or _is_current_process(payload):
                continue
            timestamp = payload.get("timestamp")
            file_age_sec = now - os.path.getmtime(path)
            # RoleSessionLock обновляет heartbeat через mtime, не переписывая JSON
            # на SMB. Поэтому свежесть определяет более новый из двух сигналов.
            payload_age_sec = now - float(timestamp) if isinstance(timestamp, (int, float)) else file_age_sec
            age_sec = min(file_age_sec, payload_age_sec)
            if age_sec > max(1.0, float(stale_sec)):
                continue
            try:
                pid = int(payload.get("pid"))
            except (TypeError, ValueError):
                pid = None
            active.append(
                ActiveNetworkSession(
                    role=str(payload.get("role") or os.path.splitext(name)[0]),
                    host=str(payload.get("host") or ""),
                    pid=pid,
                    path=path,
                    age_sec=max(0.0, age_sec),
                )
            )
        except FileNotFoundError:
            continue
        except Exception:
            # Нечитаемый свежий lock безопаснее считать активной сессией.
            try:
                age_sec = max(0.0, now - os.path.getmtime(path))
            except OSError:
                continue
            if age_sec <= max(1.0, float(stale_sec)):
                active.append(
                    ActiveNetworkSession(
                        role=os.path.splitext(name)[0],
                        host="unknown",
                        pid=None,
                        path=path,
                        age_sec=age_sec,
                    )
                )
    return active


def network_maintenance_lock(session_locks_dir: str) -> FileWriteLock:
    path = os.path.join(os.path.abspath(os.path.normpath(session_locks_dir)), NETWORK_MAINTENANCE_LOCK_FILE)
    return FileWriteLock(path, stale_timeout_sec=10 * 60)
