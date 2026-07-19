from __future__ import annotations

import ctypes
import faulthandler
import hashlib
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
EVENT_TYPES = {
    "unhandled_python_exception",
    "unhandled_thread_exception",
    "native_crash",
    "previous_session_unclean",
    "database_corruption",
    "database_unavailable_startup",
    "database_unavailable_runtime",
}
CRASH_SUBDIR_PARTS = ("logs", "diagnostics", "crashes")
SHARED_DIR_NAMES = ("incoming", "processed", "summaries", "quarantine")
MAX_NATIVE_TRACE_CHARS = 16_000
MAX_NATIVE_TRACE_LINES = 80
DATABASE_DEDUP_SECONDS = 30 * 60

_STATE_LOCK = threading.RLock()
_FAULT_FILE = None
_CURRENT_SESSION_ID = ""
_CURRENT_MARKER_PATH: Path | None = None
_CURRENT_NATIVE_PATH: Path | None = None
_DATABASE_LAST_REPORTED: dict[str, float] = {}

_WINDOWS_PATH_RE = re.compile(r"(?i)(?:[a-z]:\\|\\\\)[^\r\n\t\"<>|]+")
_UNIX_PATH_RE = re.compile(r"(?<![\w.])/(?:[^\s/:]+/)+[^\s:]+")


def _now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def _local_appdata_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    if root:
        return Path(root)
    return Path.home() / "AppData" / "Local"


def get_local_crash_spool_dir() -> Path:
    override = str(os.environ.get("REMCARD_CRASH_OUTBOX_DIR") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _local_appdata_dir() / "RemCard" / "crash-outbox"


def get_shared_crash_root(data_root: str | os.PathLike[str] | None = None) -> Path:
    if data_root is None:
        from rem_card.app.runtime_paths import resolve_baza_dir

        data_root = resolve_baza_dir()
    return Path(data_root).expanduser().resolve().joinpath(*CRASH_SUBDIR_PARTS)


def ensure_shared_crash_directories(data_root: str | os.PathLike[str] | None = None) -> dict[str, Path]:
    root = get_shared_crash_root(data_root)
    result = {"root": root}
    for name in SHARED_DIR_NAMES:
        path = root / name
        path.mkdir(parents=True, exist_ok=True)
        result[name] = path
    return result


def _replace_with_retry(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
    last_error: OSError | None = None
    for attempt in range(8):
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            last_error = exc
            if attempt >= 7:
                break
            time.sleep(0.02 * (attempt + 1))
    if last_error is not None:
        raise last_error


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        _replace_with_retry(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
        except Exception:
            pass


def _sanitize_text(value: Any, *, max_chars: int = 2000) -> str:
    text = str(value or "").replace("\x00", "")
    text = _WINDOWS_PATH_RE.sub("<path>", text)
    text = _UNIX_PATH_RE.sub("<path>", text)
    return text[:max_chars]


def _normalize_source_path(filename: str) -> str:
    normalized = str(filename or "").replace("\\", "/")
    lowered = normalized.lower()
    for marker in ("/app/", "/services/", "/ui/", "/data/", "/scripts/"):
        index = lowered.rfind(marker)
        if index >= 0:
            return normalized[index + 1 :]
    return os.path.basename(normalized) or "unknown"


def _traceback_frames(tb) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for frame in traceback.extract_tb(tb)[-40:] if tb is not None else ():
        frames.append(
            {
                "file": _normalize_source_path(frame.filename),
                "function": _sanitize_text(frame.name, max_chars=160),
                "line": int(frame.lineno or 0),
            }
        )
    return frames


def _native_trace_payload(content: str) -> list[str]:
    lines = []
    for raw_line in str(content or "").splitlines()[-MAX_NATIVE_TRACE_LINES:]:
        line = _sanitize_text(raw_line, max_chars=1000).strip()
        if line:
            lines.append(line)
    while lines and sum(len(line) for line in lines) > MAX_NATIVE_TRACE_CHARS:
        lines.pop(0)
    return lines


def _fingerprint(event_type: str, exception_type: str, frames: list[dict[str, Any]], details: dict[str, Any]) -> str:
    stable_details = {
        key: details.get(key)
        for key in ("failure_kind", "phase", "check_result")
        if details.get(key) not in (None, "")
    }
    raw = json.dumps(
        {
            "event_type": event_type,
            "exception_type": exception_type,
            "frames": frames,
            "details": stable_details,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _runtime_metadata(role: str | None = None) -> tuple[str, str]:
    effective_role = str(role or "").strip()
    version = ""
    try:
        from rem_card.app.version import APP_VERSION

        version = str(APP_VERSION or "")
    except Exception:
        pass
    if not effective_role:
        try:
            from rem_card.app.runtime_paths import get_log_file_prefix

            effective_role = get_log_file_prefix()
        except Exception:
            effective_role = "unknown"
    return version, effective_role or "unknown"


def _allowed_details(details: dict[str, Any] | None) -> dict[str, Any]:
    source = details if isinstance(details, dict) else {}
    result: dict[str, Any] = {}
    for key in ("failure_kind", "phase", "check_result", "thread_name", "previous_session_unclean"):
        value = source.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, bool):
            result[key] = value
        else:
            result[key] = _sanitize_text(value, max_chars=500)
    native_trace = source.get("native_trace")
    if isinstance(native_trace, list):
        result["native_trace"] = [_sanitize_text(line, max_chars=1000) for line in native_trace[:MAX_NATIVE_TRACE_LINES]]
    return result


def capture_crash_event(
    event_type: str,
    *,
    role: str | None = None,
    exception_type: str = "",
    frames: list[dict[str, Any]] | None = None,
    details: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> Path | None:
    if event_type not in EVENT_TYPES:
        return None
    try:
        report_id = uuid.uuid4().hex
        clean_frames = list(frames or [])[-40:]
        clean_details = _allowed_details(details)
        app_version, effective_role = _runtime_metadata(role)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "id": report_id,
            "event_type": event_type,
            "occurred_at": _now_iso(),
            "app_version": app_version,
            "role": effective_role,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "session_id": str(session_id or _CURRENT_SESSION_ID or ""),
            "exception_type": _sanitize_text(exception_type, max_chars=200),
            "frames": clean_frames,
            "details": clean_details,
        }
        payload["fingerprint"] = _fingerprint(
            event_type,
            str(payload["exception_type"]),
            clean_frames,
            clean_details,
        )
        target = get_local_crash_spool_dir() / "outbox" / f"{payload['occurred_at'][:10]}_{report_id}.json"
        _atomic_write_json(target, payload)
        return target
    except Exception:
        return None


def capture_exception(
    event_type: str,
    exc_type,
    exc_value,
    exc_traceback,
    *,
    role: str | None = None,
    thread_name: str = "",
) -> Path | None:
    del exc_value  # Exception messages can contain patient or SQL data.
    return capture_crash_event(
        event_type,
        role=role,
        exception_type=getattr(exc_type, "__name__", str(exc_type or "Unknown")),
        frames=_traceback_frames(exc_traceback),
        details={"thread_name": thread_name},
    )


def capture_database_failure(
    failure_kind: str,
    *,
    role: str | None = None,
    phase: str = "",
    check_result: str = "",
) -> Path | None:
    event_type = "database_corruption" if failure_kind == "corruption" else (
        "database_unavailable_runtime" if phase == "runtime" else "database_unavailable_startup"
    )
    details = {
        "failure_kind": failure_kind,
        "phase": phase,
        "check_result": check_result,
    }
    fingerprint = _fingerprint(event_type, "", [], _allowed_details(details))
    now = time.monotonic()
    with _STATE_LOCK:
        last_reported = _DATABASE_LAST_REPORTED.get(fingerprint)
        if last_reported is not None and now - last_reported < DATABASE_DEDUP_SECONDS:
            return None
        _DATABASE_LAST_REPORTED[fingerprint] = now
    report_path = capture_crash_event(event_type, role=role, details=details)
    if report_path is None:
        with _STATE_LOCK:
            if _DATABASE_LAST_REPORTED.get(fingerprint) == now:
                _DATABASE_LAST_REPORTED.pop(fingerprint, None)
    return report_path


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _remove_file(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _process_stale_sessions() -> None:
    sessions_dir = get_local_crash_spool_dir() / "sessions"
    if not sessions_dir.is_dir():
        return
    for marker_path in sorted(sessions_dir.glob("*.json")):
        marker = _read_json(marker_path)
        if not marker:
            _remove_file(marker_path)
            continue
        try:
            marker_pid = int(marker.get("pid") or 0)
        except (TypeError, ValueError):
            marker_pid = 0
        if _pid_is_running(marker_pid):
            continue
        native_path = Path(str(marker.get("native_path") or "")) if marker.get("native_path") else None
        native_content = ""
        if native_path is not None:
            try:
                native_content = native_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                native_content = ""
        if native_content.strip():
            capture_crash_event(
                "native_crash",
                role=str(marker.get("role") or ""),
                details={
                    "native_trace": _native_trace_payload(native_content),
                    "previous_session_unclean": True,
                },
                session_id=str(marker.get("session_id") or ""),
            )
        else:
            capture_crash_event(
                "previous_session_unclean",
                role=str(marker.get("role") or ""),
                details={"previous_session_unclean": True},
                session_id=str(marker.get("session_id") or ""),
            )
        _remove_file(native_path)
        _remove_file(marker_path)


def initialize_crash_session(role: str | None = None) -> str:
    global _FAULT_FILE, _CURRENT_MARKER_PATH, _CURRENT_NATIVE_PATH, _CURRENT_SESSION_ID
    with _STATE_LOCK:
        _process_stale_sessions()
        session_id = uuid.uuid4().hex
        spool = get_local_crash_spool_dir()
        marker_path = spool / "sessions" / f"{session_id}.json"
        native_path = spool / "native" / f"{session_id}.log"
        native_path.parent.mkdir(parents=True, exist_ok=True)
        fault_file = open(native_path, "w", encoding="utf-8")
        app_version, effective_role = _runtime_metadata(role)
        _atomic_write_json(
            marker_path,
            {
                "schema_version": 1,
                "session_id": session_id,
                "started_at": _now_iso(),
                "pid": os.getpid(),
                "role": effective_role,
                "app_version": app_version,
                "native_path": str(native_path),
            },
        )
        try:
            faulthandler.enable(file=fault_file)
        except Exception:
            fault_file.close()
            _remove_file(native_path)
            _remove_file(marker_path)
            raise
        _FAULT_FILE = fault_file
        _CURRENT_SESSION_ID = session_id
        _CURRENT_MARKER_PATH = marker_path
        _CURRENT_NATIVE_PATH = native_path
        return session_id


def finalize_crash_session(exit_code: int | None = None) -> None:
    global _FAULT_FILE, _CURRENT_MARKER_PATH, _CURRENT_NATIVE_PATH, _CURRENT_SESSION_ID
    with _STATE_LOCK:
        fault_file = _FAULT_FILE
        marker_path = _CURRENT_MARKER_PATH
        native_path = _CURRENT_NATIVE_PATH
        session_id = _CURRENT_SESSION_ID
        _FAULT_FILE = None
        _CURRENT_MARKER_PATH = None
        _CURRENT_NATIVE_PATH = None
        _CURRENT_SESSION_ID = ""
        try:
            faulthandler.disable()
        except Exception:
            pass
        try:
            if fault_file is not None:
                fault_file.flush()
                fault_file.close()
        except Exception:
            pass

    native_content = ""
    if native_path is not None:
        try:
            native_content = native_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            native_content = ""
    if native_content.strip():
        capture_crash_event(
            "native_crash",
            details={"native_trace": _native_trace_payload(native_content)},
            session_id=session_id,
        )
    elif exit_code not in (None, 0):
        capture_crash_event(
            "previous_session_unclean",
            details={"previous_session_unclean": True},
            session_id=session_id,
        )
    _remove_file(native_path)
    _remove_file(marker_path)


def flush_local_crash_outbox(data_root: str | os.PathLike[str] | None = None) -> dict[str, int]:
    result = {"delivered": 0, "failed": 0, "remaining": 0}
    outbox = get_local_crash_spool_dir() / "outbox"
    if not outbox.is_dir():
        return result
    try:
        incoming = ensure_shared_crash_directories(data_root)["incoming"]
    except Exception:
        result["remaining"] = len(list(outbox.glob("*.json")))
        result["failed"] = result["remaining"]
        return result

    for source in sorted(outbox.glob("*.json")):
        try:
            raw = source.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
            report_id = str(payload.get("id") or source.stem)
            target = incoming / f"{report_id}.json"
            fd, tmp_name = tempfile.mkstemp(prefix=f".{report_id}.", suffix=".tmp", dir=str(incoming))
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(raw)
                    fh.flush()
                    os.fsync(fh.fileno())
                _replace_with_retry(tmp_name, target)
            finally:
                try:
                    if os.path.exists(tmp_name):
                        os.remove(tmp_name)
                except Exception:
                    pass
            source.unlink()
            result["delivered"] += 1
        except Exception:
            result["failed"] += 1
    result["remaining"] = len(list(outbox.glob("*.json")))
    return result


def validate_crash_payload(payload: Any) -> tuple[bool, str]:
    if not isinstance(payload, dict):
        return False, "payload is not an object"
    if payload.get("schema_version") != SCHEMA_VERSION:
        return False, "unsupported schema_version"
    if str(payload.get("event_type") or "") not in EVENT_TYPES:
        return False, "unsupported event_type"
    if not str(payload.get("id") or ""):
        return False, "missing id"
    if not str(payload.get("fingerprint") or ""):
        return False, "missing fingerprint"
    if not str(payload.get("occurred_at") or ""):
        return False, "missing occurred_at"
    if not isinstance(payload.get("frames"), list) or not isinstance(payload.get("details"), dict):
        return False, "invalid frames or details"
    return True, "ok"
