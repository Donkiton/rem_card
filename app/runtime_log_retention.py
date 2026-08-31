"""Bounded, local-only retention for explicitly recognized RemCard log files."""
from __future__ import annotations

import ctypes
import json
import os
import re
import stat
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from rem_card.app.runtime_log_storage import (
    KNOWN_TEXT_PREFIXES, append_log_lines, positive_int_setting, storage_enabled,
)


DEFAULT_TOTAL_BYTES = 512 * 1024 * 1024
_TEXT_PREFIX = "(?:" + "|".join(KNOWN_TEXT_PREFIXES) + r"|runtime_[a-z0-9_-]+)"
_SEGMENT_RE = re.compile(
    rf"^(?P<prefix>{_TEXT_PREFIX}|metrics|audit)_(?P<day>\d{{8}})"
    r"_p(?P<pid>[1-9]\d*)_s[0-9a-f]{32}_\d{6,}_(?P<state>active|closed)\.(?P<ext>log|jsonl)$"
)
_LEGACY_RE = re.compile(
    rf"^(?P<prefix>{_TEXT_PREFIX}|metrics|audit)_(?P<day>\d{{8}})\.(?P<ext>log|jsonl)$"
)
_LOCK = threading.Lock()
_DIRECTORIES: dict[str, None] = {}
_WAKE = threading.Event()
_STOP = threading.Event()
_THREAD: threading.Thread | None = None


@dataclass(frozen=True)
class LogFile:
    path: str
    kind: str
    size: int
    mtime_ns: int
    device: int
    inode: int
    active: bool

    def fingerprint(self) -> tuple[int, int, int, int]:
        return self.size, self.mtime_ns, self.device, self.inode


def _is_local_path(path: Path) -> bool:
    if os.name != "nt":
        return True
    drive = path.drive
    if drive.startswith("\\\\"):
        return False
    return not drive or ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") != 4


def _plain_path(path: Path) -> bool:
    """Refuse symlinks and Windows junctions, including any parent directory."""
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except OSError:
            return False
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            return False
    return True


def _pid_running(pid: int) -> bool:
    if not 0 < pid <= 0xFFFFFFFF:
        return True  # Unverifiable owner: preserve rather than abort cleanup.
    if pid == os.getpid():
        return True
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except OSError:
            return True
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return ctypes.get_last_error() != 87  # Access denied/unknown: preserve.
    try:
        code = wintypes.DWORD()
        return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
    finally:
        kernel.CloseHandle(handle)


def _log_kind(name: str) -> tuple[str, re.Match | None] | None:
    if name == "startup.log":
        return "text", None
    match = _SEGMENT_RE.fullmatch(name) or _LEGACY_RE.fullmatch(name)
    if match is None:
        return None
    try:
        datetime.strptime(match["day"], "%Y%m%d")
    except ValueError:
        return None
    prefix = match["prefix"]
    kind = prefix if prefix in {"metrics", "audit"} else "text"
    if match["ext"] != ("log" if kind == "text" else "jsonl"):
        return None
    return kind, match


def _scan_directories(roots: list[str]) -> list[Path]:
    found: dict[str, Path] = {}
    for raw in roots:
        root = Path(raw).absolute()
        if not _is_local_path(root):
            continue
        for directory in (root, root / "migrated-appdata" / "logs"):
            if directory.is_dir() and _plain_path(directory):
                found[os.path.normcase(str(directory))] = directory
    return list(found.values())


def inventory_logs(roots: list[str], *, now: float | None = None) -> list[LogFile]:
    now = time.time() if now is None else now
    files: list[LogFile] = []
    processes: dict[int, bool] = {}
    for directory in _scan_directories(roots):
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for path in entries:
            parsed = _log_kind(path.name)
            if parsed is None or not _plain_path(path):
                continue
            try:
                info = path.stat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            kind, match = parsed
            segment = match is not None and "state" in match.groupdict()
            if segment:
                pid = int(match["pid"])
                if pid not in processes:
                    processes[pid] = _pid_running(pid)
                active = match["state"] == "active" and processes[pid]
            else:
                # Old clients have no ownership marker. Do not race recent
                # writes; Windows additionally refuses unlink of open logs.
                active = now - info.st_mtime < 60
            files.append(LogFile(
                str(path), kind, info.st_size, info.st_mtime_ns,
                info.st_dev, info.st_ino, active,
            ))
    return files


def plan_log_cleanup(
    roots: list[str], *, now: float | None = None, text_retention_days: int = 30,
) -> dict:
    now = time.time() if now is None else now
    files = inventory_logs(roots, now=now)
    retention = {"text": max(1, text_retention_days), "metrics": 7, "audit": 90}
    limit = positive_int_setting("REMCARD_LOG_TOTAL_BYTES", DEFAULT_TOTAL_BYTES)
    remaining = sum(f.size for f in files if f.kind != "audit")
    selected: list[dict] = []
    selected_paths: set[str] = set()
    for item in sorted(files, key=lambda f: (f.mtime_ns, f.path)):
        if not item.active and item.mtime_ns / 1e9 < now - retention[item.kind] * 86400:
            selected.append({**asdict(item), "reason": "age"})
            selected_paths.add(item.path)
            if item.kind != "audit":
                remaining -= item.size
    for item in sorted(files, key=lambda f: (f.mtime_ns, f.path)):
        if remaining <= limit:
            break
        if item.active or item.kind == "audit" or item.path in selected_paths:
            continue
        selected.append({**asdict(item), "reason": "budget"})
        remaining -= item.size
    return {
        "schema_version": 1, "created_at": datetime.fromtimestamp(now).isoformat(),
        "roots": [str(Path(root).absolute()) for root in roots],
        "budget_bytes": limit, "ordinary_bytes": sum(f.size for f in files if f.kind != "audit"),
        "remaining_bytes": remaining, "active_bytes": sum(f.size for f in files if f.active),
        "candidates": selected,
    }


def apply_log_cleanup(plan: dict, roots: list[str], *, text_retention_days: int = 30) -> dict:
    """Revalidate a preview against today's policy, ownership and file identity.

    Never trust deletion paths in a JSON plan. Only the intersection with a
    fresh allowlisted inventory can be removed. Changed files are preserved.
    """
    fresh = plan_log_cleanup(roots, text_retention_days=text_retention_days)
    allowed = {item["path"]: item for item in fresh["candidates"]}
    result = {"removed_files": 0, "freed_bytes": 0, "failed_files": 0, "skipped_files": 0}
    seen: set[str] = set()
    for requested in plan.get("candidates", []):
        name = requested.get("path")
        current = allowed.get(name)
        if name in seen or current is None or any(
            requested.get(key) != current[key] for key in ("size", "mtime_ns", "device", "inode")
        ):
            result["skipped_files"] += 1
            continue
        seen.add(name)
        path = Path(name)
        try:
            if not _plain_path(path):
                result["skipped_files"] += 1
                continue
            info = path.stat()
            if (info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino) != tuple(
                current[key] for key in ("size", "mtime_ns", "device", "inode")
            ):
                result["skipped_files"] += 1
                continue
            path.unlink()
            result["removed_files"] += 1
            result["freed_bytes"] += current["size"]
        except FileNotFoundError:
            result["skipped_files"] += 1
        except OSError:
            result["failed_files"] += 1
    remaining = inventory_logs(roots)
    result["ordinary_bytes"] = sum(f.size for f in remaining if f.kind != "audit")
    result["budget_bytes"] = fresh["budget_bytes"]
    result["over_budget"] = result["ordinary_bytes"] > fresh["budget_bytes"]
    return result


def cleanup_logs(roots: list[str], *, text_retention_days: int = 30, dry_run: bool = False) -> dict:
    plan = plan_log_cleanup(roots, text_retention_days=text_retention_days)
    if dry_run:
        return plan
    return apply_log_cleanup(plan, roots, text_retention_days=text_retention_days)


def _protected_crash_bytes(roots: list[str]) -> int:
    total = 0
    for root in {str(Path(value).absolute()) for value in roots}:
        for relative in ("crashes", "migrated-appdata/crash-outbox"):
            directory = Path(root) / relative
            if not _is_local_path(directory) or not _plain_path(directory):
                continue
            for parent, dirs, names in os.walk(directory, followlinks=False):
                dirs[:] = [name for name in dirs if _plain_path(Path(parent) / name)]
                for name in names:
                    path = Path(parent) / name
                    try:
                        if _plain_path(path):
                            total += path.stat().st_size
                    except OSError:
                        continue
    return total


def run_log_maintenance(roots: list[str]) -> dict:
    dry_run = os.environ.get("REMCARD_LOG_CLEANUP_DRY_RUN", "0") == "1"
    result = cleanup_logs(roots, dry_run=dry_run)
    summary = {key: value for key, value in result.items() if key not in {"roots", "candidates"}}
    if dry_run:
        summary["dry_run"] = True
        summary["candidate_files"] = len(result["candidates"])
    protected = _protected_crash_bytes(roots)
    summary["protected_crash_bytes"] = protected
    summary["protected_crash_over_budget"] = protected > positive_int_setting(
        "REMCARD_LOG_TOTAL_BYTES", DEFAULT_TOTAL_BYTES,
    )
    warning = summary.get("failed_files") or summary.get("over_budget") or summary["protected_crash_over_budget"]
    level = "WARNING" if warning else "INFO"
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} | {level} | RemCard | Log storage maintenance: {json.dumps(summary)}\n"
    for directory in roots:
        if not _is_local_path(Path(directory)):
            continue
        try:
            append_log_lines(directory, "log_maintenance", [line], managed=False)
            break
        except OSError:
            continue
    return result


def _maintenance_worker() -> None:
    while not _STOP.is_set():
        _WAKE.wait(timeout=positive_int_setting("REMCARD_LOG_CLEANUP_INTERVAL_SEC", 300))
        _WAKE.clear()
        if _STOP.is_set():
            break
        if not storage_enabled():
            continue
        with _LOCK:
            directories = list(_DIRECTORIES)
        try:
            run_log_maintenance(directories)
        except Exception:
            # Storage diagnostics must never break clinical work or recurse
            # through the application exception/crash-report handlers.
            continue


def request_log_cleanup(log_dir: str, *, rollover: bool = False) -> None:
    """Schedule local cleanup on startup/rollover, never scan on the UI thread."""
    global _THREAD
    if not storage_enabled() or not _is_local_path(Path(log_dir)):
        return
    from rem_card.app.runtime_paths import get_runtime_log_directory_candidates

    with _LOCK:
        first_use = os.path.abspath(log_dir) not in _DIRECTORIES
        _DIRECTORIES.update(dict.fromkeys([os.path.abspath(log_dir), *get_runtime_log_directory_candidates()]))
        if _THREAD is None or not _THREAD.is_alive():
            _STOP.clear()
            _THREAD = threading.Thread(target=_maintenance_worker, name="RemCardLogMaintenance", daemon=True)
            _THREAD.start()
            first_use = True
    if first_use or rollover:
        _WAKE.set()


def stop_log_maintenance() -> None:
    global _THREAD
    _STOP.set()
    _WAKE.set()
    thread = _THREAD
    if thread is not None and thread.is_alive():
        thread.join(timeout=1.0)
    with _LOCK:
        if thread is None or not thread.is_alive():
            _THREAD = None
            _DIRECTORIES.clear()
