import json
import os
import socket
import threading
from datetime import datetime
from typing import Any

from rem_card.app.runtime_paths import get_local_logs_dir


_METRICS_LOCK = threading.Lock()


def _enabled() -> bool:
    return os.environ.get("REMCARD_LOCAL_METRICS_ENABLED", "1") != "0"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _metrics_path() -> str:
    log_dir = get_local_logs_dir()
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, f"metrics_{datetime.now().strftime('%Y%m%d')}.jsonl")


def record_metric(name: str, value: Any = None, **fields: Any):
    if not _enabled():
        return
    payload = {
        "ts": _now_iso(),
        "metric": str(name),
        "value": value,
        "host": socket.gethostname(),
        "pid": os.getpid(),
    }
    payload.update(fields)
    try:
        with _METRICS_LOCK:
            with open(_metrics_path(), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass
