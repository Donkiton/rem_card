"""Manual, role-independent retention of recognized shared diagnostic artifacts.

Never opens SQLite. Unknown, active and pending files are preserved. The caller
must pass the current network runtime's root; no global paths or timers here.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


DAY = 86400
MAX_SCAN = 20000
MAX_DELETE = 500
MAX_SCAN_SECONDS = 30
LOCK_NAME = ".service_cleanup.lock"
LOG_RE = re.compile(
    r"(audit|metrics|doctor|nurse|operblock|rem_card|startup|log_maintenance|path_setup)_"
    r"(\d{8})(?:_p\d+_s[0-9a-f]{32}_\d{6,}_(active|closed))?\.(log|jsonl)"
)


@dataclass(frozen=True)
class Candidate:
    relative_path: str
    category: str
    days: int
    size: int
    mtime_ns: int
    device: int
    inode: int


@dataclass
class Inspection:
    root: str
    candidates: list[Candidate] = field(default_factory=list)
    preserved: int = 0
    errors: list[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def bytes(self) -> int:
        return sum(item.size for item in self.candidates)


def _plain(path: Path) -> bool:
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            return False
    return True


def _valid_date(value: str, fmt: str) -> bool:
    try:
        datetime.strptime(value, fmt)
        return True
    except ValueError:
        return False


def _policy(relative: str) -> tuple[str, int] | None:
    """Exact directory and filename allowlist; no recursive wildcard deletion."""
    path = Path(relative)
    parent = path.parent.as_posix()
    name = path.name
    if parent == "logs":
        match = LOG_RE.fullmatch(name)
        if not match or match[3] == "active" or not _valid_date(match[2], "%Y%m%d"):
            return None
        kind = match[1]
        if match[4] != ("jsonl" if kind in {"audit", "metrics"} else "log"):
            return None
        return ("Аудит", 90) if kind == "audit" else ("Метрики", 14) if kind == "metrics" else ("Технические логи", 30)
    if parent in {"backup_health", "settings/backup_health"}:
        match = re.fullmatch(r"daily_backup_(\d{4}-\d{2}-\d{2})\.done\.json", name)
        if match and _valid_date(match[1], "%Y-%m-%d"):
            return "История проверки бэкапов", 90
        match = re.fullmatch(r"daily_backup_report_(\d{4}-\d{2}-\d{2})_[a-z0-9_]+\.json", name)
        if match and _valid_date(match[1], "%Y-%m-%d"):
            return "История проверки бэкапов", 90
        if re.fullmatch(r"startup_quick_check_state\.json\.tmp_\d+_\d+_\d+", name):
            return "Временные файлы", 7
    if parent == "locks/replica_snapshots" and re.fullmatch(
        r"\.[\w.-]+\.lock\.[\w.-]+\.\d+\.[0-9a-f]{32}\.tmp", name
    ):
        return "Временные файлы", 7
    if parent == "quarantine/locks" and re.fullmatch(
        r"[\w.-]+\.lock\.\d{8}_\d{6}_\d{6}\.[0-9a-f]{12}\.[0-9a-f]{8}\.malformed", name
    ):
        return "Карантин блокировок", 30
    if re.fullmatch(r"logs/diagnostics/crashes/processed/\d{4}/\d{2}", parent):
        if re.fullmatch(r"[0-9a-f]{32}\.json", name):
            return "Обработанные отчёты", 30
    if parent == "logs/diagnostics/crashes/summaries" and re.fullmatch(r"crash-summary_\d{8}_\d{6}\.md", name):
        return "Сводки диагностики", 30
    if parent == "logs/diagnostics/crashes/quarantine" and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}_[0-9a-f]{32}\.invalid\.(json|reason\.txt)", name
    ):
        return "Карантин отчётов", 30
    return None


def _signature(info) -> tuple[int, int, int, int]:
    return info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino


def _expected(item: Candidate) -> tuple[int, int, int, int]:
    return item.size, item.mtime_ns, item.device, item.inode


def _delete_exclusive(path: Path, item: Candidate) -> bool:
    """Windows/SMB: deny concurrent opens; delete the verified handle, not a path.

    An existing writer or a replaced file causes a skip. Unsupported platforms
    deliberately remain inspection-only until equivalent guarantees exist.
    """
    if os.name != "nt":
        raise OSError("Очистка поддерживается только в Windows")
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetFileInformationByHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000 | 0x10000, 0, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except Exception:
        kernel.CloseHandle(handle)
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or _signature(info) != _expected(item):
            return False
        if getattr(info, "st_file_attributes", 0) & 0x400:
            return False
        disposition = wintypes.BOOL(True)
        if not kernel.SetFileInformationByHandle(handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
            raise ctypes.WinError(ctypes.get_last_error())
        return True
    finally:
        os.close(fd)


class StorageMaintenanceService:
    def __init__(self, root: str | Path):
        self.root = Path(os.path.abspath(root))

    def _validate_root(self):
        if not _plain(self.root) or not self.root.is_dir():
            raise ValueError("Недопустимая папка базы")
        marker = self.root / "archiv" / "rao_journal.db"
        if not marker.is_file() or not _plain(marker):
            raise ValueError("В выбранной папке не найдена основная база RemCard")

    def inspect(self, *, now: float | None = None) -> Inspection:
        self._validate_root()
        now = time.time() if now is None else now
        result = Inspection(str(self.root))
        started = time.monotonic()
        directories = ["logs", "backup_health", "settings/backup_health", "locks/replica_snapshots", "quarantine/locks"]
        seen = 0
        # Only descend into the crash tree, never into medical or recovery data.
        while directories:
            relative = directories.pop()
            directory = self.root / relative
            try:
                if not directory.exists():
                    continue
                if not _plain(directory):
                    result.errors.append(f"Пропущена ссылка: {relative}")
                    continue
                with os.scandir(directory) as entries:
                    for entry in entries:
                        seen += 1
                        if seen > MAX_SCAN or time.monotonic() - started > MAX_SCAN_SECONDS:
                            result.truncated = True
                            return result
                        path = Path(entry.path)
                        rel = path.relative_to(self.root).as_posix()
                        # scandir's cached Windows stat can omit file identity;
                        # use lstat so preview and exclusive handle agree.
                        info = path.lstat()
                        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                            result.preserved += 1
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            if rel in {"logs/diagnostics", "logs/diagnostics/crashes"} or (
                                rel.startswith("logs/diagnostics/crashes/") and len(Path(rel).parts) <= 6
                            ):
                                directories.append(rel)
                            continue
                        rule = _policy(rel)
                        if not stat.S_ISREG(info.st_mode) or rule is None or info.st_mtime >= now - rule[1] * DAY:
                            result.preserved += 1
                            continue
                        result.candidates.append(Candidate(rel, rule[0], rule[1], *_signature(info)))
            except OSError as exc:
                result.errors.append(f"{relative}: {exc}")
        result.candidates.sort(key=lambda item: (item.mtime_ns, item.relative_path))
        return result

    def clean(self, inspection: Inspection) -> dict:
        self._validate_root()
        if inspection.root != str(self.root) or inspection.errors or inspection.truncated:
            raise ValueError("Нужна полная успешная инспекция этой базы")
        lock = self.root / LOCK_NAME
        try:
            lock.mkdir()
        except FileExistsError as exc:
            raise RuntimeError("Обслуживание уже запущено или осталось незавершённым. Проверьте предыдущий запуск.") from exc
        result = {"removed": 0, "bytes": 0, "skipped": 0, "errors": [], "remaining": 0}
        try:
            fresh = self.inspect()
            if fresh.errors or fresh.truncated:
                raise RuntimeError("Повторная инспекция не завершена; очистка отменена")
            allowed = {item.relative_path: item for item in fresh.candidates}
            selected = inspection.candidates[:MAX_DELETE]
            result["remaining"] = max(0, len(inspection.candidates) - len(selected))
            # Fixed-size latest report; preflight journal failure means no deletions.
            self._report({"status": "started", "planned": len(selected)})
            visited = set()
            for item in selected:
                if item.relative_path in visited or allowed.get(item.relative_path) != item:
                    result["skipped"] += 1
                    continue
                visited.add(item.relative_path)
                path = self.root / item.relative_path
                try:
                    if not _plain(path) or not _delete_exclusive(path, item):
                        result["skipped"] += 1
                        continue
                    result["removed"] += 1
                    result["bytes"] += item.size
                except OSError as exc:
                    result["skipped"] += 1
                    result["errors"].append(f"{item.relative_path}: {exc}")
            result["report_written"] = True
            try:
                self._report({"status": "finished", **result})
            except OSError as exc:
                result["report_written"] = False
                result["errors"].append(f"Не удалось записать итоговый отчёт: {exc}")
            return result
        finally:
            lock.rmdir()

    def _report(self, payload: dict):
        # A single bounded report, outside directories eligible for retention.
        path = self.root / "service_cleanup_last.json"
        temp = self.root / "service_cleanup_last.json.tmp"
        for target in (path, temp):
            if target.exists() and not _plain(target):
                raise ValueError("Файл отчёта обслуживания является ссылкой")
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump({"at": datetime.now().astimezone().isoformat(), **payload}, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
