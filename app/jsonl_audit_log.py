import getpass
import json
import os
import socket
from datetime import datetime
from typing import Any, Optional

from rem_card.app.runtime_paths import get_runtime_logs_dir
from rem_card.app.version import APP_VERSION


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _json_default(value: Any) -> str:
    return str(value)


def _append_jsonl(path: str, payload: dict[str, Any]) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, default=_json_default) + "\n")
        return True
    except Exception:
        return False


def write_audit_event(
    event: str,
    *,
    baza_dir: Optional[str] = None,
    role: Optional[str] = None,
    details: Optional[dict[str, Any]] = None,
):
    payload: dict[str, Any] = {
        "ts": _now_iso(),
        "event": str(event),
        "host": socket.gethostname(),
        "windows_user": getpass.getuser(),
        "pid": os.getpid(),
        "role": role,
        "app_version": APP_VERSION,
    }
    if details:
        payload.update(details)

    log_name = f"audit_{datetime.now().strftime('%Y%m%d')}.jsonl"
    if baza_dir:
        shared_path = os.path.join(baza_dir, "logs", log_name)
        if _append_jsonl(shared_path, payload):
            return

    local_path = os.path.join(get_runtime_logs_dir(), log_name)
    _append_jsonl(local_path, payload)

