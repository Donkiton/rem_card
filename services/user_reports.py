from __future__ import annotations

import getpass
import json
import os
import re
import socket
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


USER_REPORTS_DIR_NAME = "users-reports"

REPORT_TYPE_PROBLEM = "problem"
REPORT_TYPE_SUGGESTION = "suggestion"
REPORT_TYPES = {REPORT_TYPE_PROBLEM, REPORT_TYPE_SUGGESTION}

STATUS_NEW = "new"
STATUS_READ = "read"
STATUS_IN_PROGRESS = "in_progress"
STATUS_CLOSED = "closed"
REPORT_STATUSES = {STATUS_NEW, STATUS_READ, STATUS_IN_PROGRESS, STATUS_CLOSED}

TYPE_LABELS = {
    REPORT_TYPE_PROBLEM: "Проблема",
    REPORT_TYPE_SUGGESTION: "Предложение",
}

STATUS_LABELS = {
    STATUS_NEW: "Новый",
    STATUS_READ: "Прочитан",
    STATUS_IN_PROGRESS: "В работе",
    STATUS_CLOSED: "Закрыт",
}

_LOG_TIMESTAMP_RE = re.compile(
    r"^(?P<stamp>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})(?:[,.](?P<fraction>\d{1,6}))?"
)
_ATOMIC_REPLACE_ATTEMPTS = 20
_ATOMIC_REPLACE_RETRY_SECONDS = 0.05
_TRANSIENT_REPLACE_WINERRORS = {5, 32}


@dataclass(frozen=True)
class SubmittedUserReport:
    report_id: str
    directory: Path
    report_path: Path
    status_path: Path
    logs_path: Path | None


def normalize_report_type(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"problem", "bug", "error", "ошибка", "проблема", "баг"}:
        return REPORT_TYPE_PROBLEM
    if raw in {"suggestion", "proposal", "idea", "предложение", "идея"}:
        return REPORT_TYPE_SUGGESTION
    raise ValueError(f"Неизвестный тип репорта: {value!r}")


def normalize_report_status(value: Any) -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "новый": STATUS_NEW,
        "прочитан": STATUS_READ,
        "прочитано": STATUS_READ,
        "в работе": STATUS_IN_PROGRESS,
        "work": STATUS_IN_PROGRESS,
        "closed": STATUS_CLOSED,
        "закрыт": STATUS_CLOSED,
        "закрыто": STATUS_CLOSED,
    }
    normalized = aliases.get(raw, raw)
    if normalized not in REPORT_STATUSES:
        return STATUS_NEW
    return normalized


def report_type_label(value: Any) -> str:
    return TYPE_LABELS.get(normalize_report_type(value), str(value or ""))


def report_status_label(value: Any) -> str:
    return STATUS_LABELS.get(normalize_report_status(value), str(value or ""))


def _replace_with_retry(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
    """Replace a file after short-lived Windows/SMB reader locks clear."""
    for attempt in range(_ATOMIC_REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt >= _ATOMIC_REPLACE_ATTEMPTS - 1:
                raise
        except OSError as exc:
            if (
                getattr(exc, "winerror", None) not in _TRANSIENT_REPLACE_WINERRORS
                or attempt >= _ATOMIC_REPLACE_ATTEMPTS - 1
            ):
                raise
        time.sleep(_ATOMIC_REPLACE_RETRY_SECONDS)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        _replace_with_retry(tmp_name, path)
    except Exception:
        try:
            os.remove(tmp_name)
        except Exception:
            pass
        raise


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            if not text.endswith("\n"):
                fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        _replace_with_retry(tmp_name, path)
    except Exception:
        try:
            os.remove(tmp_name)
        except Exception:
            pass
        raise


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _local_now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat(sep=" ")


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone().replace(tzinfo=None)
            return parsed.replace(microsecond=0)
        except Exception:
            continue
    return None


def _parse_log_timestamp(line: str) -> datetime | None:
    match = _LOG_TIMESTAMP_RE.match(line or "")
    if not match:
        return None
    stamp = match.group("stamp")
    fraction = (match.group("fraction") or "0").ljust(6, "0")[:6]
    try:
        return datetime.strptime(f"{stamp}.{fraction}", "%Y-%m-%d %H:%M:%S.%f").replace(microsecond=0)
    except Exception:
        return None


def _unique_paths(paths: Iterable[str | os.PathLike[str] | None]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for raw_path in paths:
        if not raw_path:
            continue
        path = Path(raw_path)
        key = os.path.normcase(os.path.abspath(str(path)))
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


class UserReportsService:
    def __init__(
        self,
        *,
        reports_root: str | os.PathLike[str] | None = None,
        logs_dirs: Iterable[str | os.PathLike[str] | None] | None = None,
    ):
        if reports_root is None:
            from rem_card.app.paths import REPORT_DIR

            reports_root = Path(REPORT_DIR) / USER_REPORTS_DIR_NAME
        self.reports_root = Path(reports_root)

        if logs_dirs is None:
            from rem_card.app.paths import BAZA_LOGS_DIR, LOGS_DIR

            logs_dirs = (LOGS_DIR, BAZA_LOGS_DIR)
        self.logs_dirs = _unique_paths(logs_dirs)

    def submit_report(
        self,
        *,
        report_type: str,
        text: str,
        role: str | None = None,
        created_at: datetime | None = None,
        extra_context: dict[str, Any] | None = None,
    ) -> SubmittedUserReport:
        normalized_type = normalize_report_type(report_type)
        clean_text = str(text or "").strip()
        if not clean_text:
            raise ValueError("Текст репорта не заполнен.")

        now = (created_at or _local_now()).replace(microsecond=0)
        report_id = uuid.uuid4().hex[:12]
        folder_name = f"{now.strftime('%Y-%m-%d_%H-%M-%S')}_{report_id}"
        report_dir = self.reports_root / now.strftime("%Y") / folder_name
        report_dir.mkdir(parents=True, exist_ok=False)

        context = self._build_context(role=role)
        if isinstance(extra_context, dict):
            context.update(extra_context)

        payload = {
            "schema_version": 1,
            "id": report_id,
            "type": normalized_type,
            "type_label": TYPE_LABELS[normalized_type],
            "status": STATUS_NEW,
            "status_label": STATUS_LABELS[STATUS_NEW],
            "text": clean_text,
            "created_at": _iso(now),
            "created_by": context,
        }
        status_payload = self._build_status_payload(STATUS_NEW, role=role, updated_at=now)

        report_path = report_dir / "report.json"
        status_path = report_dir / "status.json"
        _atomic_write_json(report_path, payload)
        _atomic_write_json(status_path, status_payload)

        logs_path: Path | None = None
        if normalized_type == REPORT_TYPE_PROBLEM:
            logs_path = report_dir / "logs_last_hour.txt"
            logs_text = self.collect_logs_for_period(now - timedelta(hours=1), now)
            _atomic_write_text(logs_path, logs_text)

        return SubmittedUserReport(
            report_id=report_id,
            directory=report_dir,
            report_path=report_path,
            status_path=status_path,
            logs_path=logs_path,
        )

    def list_reports(self) -> list[dict[str, Any]]:
        if not self.reports_root.exists():
            return []

        items: list[dict[str, Any]] = []
        for report_path in self.reports_root.rglob("report.json"):
            item = self.read_report(report_path.parent)
            if item:
                items.append(item)

        def sort_key(item: dict[str, Any]) -> tuple[datetime, str]:
            created = _parse_iso_datetime(item.get("created_at")) or datetime.fromtimestamp(0)
            return created, str(item.get("id") or "")

        items.sort(key=sort_key, reverse=True)
        return items

    def count_new_reports(self) -> int:
        return sum(1 for item in self.list_reports() if item.get("status") == STATUS_NEW)

    def read_report(self, report_dir: str | os.PathLike[str]) -> dict[str, Any] | None:
        directory = self._normalize_report_dir(report_dir)
        payload = _read_json(directory / "report.json")
        if not payload:
            return None

        status_payload = _read_json(directory / "status.json") or {}
        status = normalize_report_status(status_payload.get("status") or payload.get("status"))
        report_type = normalize_report_type(payload.get("type"))
        created = str(payload.get("created_at") or "")
        text = str(payload.get("text") or "")
        created_by = payload.get("created_by") if isinstance(payload.get("created_by"), dict) else {}
        updated_at = str(status_payload.get("updated_at") or created)

        return {
            "id": str(payload.get("id") or directory.name),
            "directory": str(directory),
            "report_path": str(directory / "report.json"),
            "status_path": str(directory / "status.json"),
            "logs_path": str(directory / "logs_last_hour.txt") if (directory / "logs_last_hour.txt").is_file() else "",
            "type": report_type,
            "type_label": TYPE_LABELS[report_type],
            "status": status,
            "status_label": STATUS_LABELS[status],
            "text": text,
            "created_at": created,
            "updated_at": updated_at,
            "created_by": created_by,
            "status_history": status_payload.get("history") if isinstance(status_payload.get("history"), list) else [],
        }

    def read_logs(self, report_dir: str | os.PathLike[str]) -> str:
        directory = self._normalize_report_dir(report_dir)
        logs_path = directory / "logs_last_hour.txt"
        try:
            return logs_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
        except Exception as exc:
            return f"Не удалось прочитать приложенные логи: {exc}"

    def update_status(
        self,
        report_dir: str | os.PathLike[str],
        status: str,
        *,
        role: str | None = None,
        updated_at: datetime | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        directory = self._normalize_report_dir(report_dir)
        if not (directory / "report.json").is_file():
            raise FileNotFoundError(f"Репорт не найден: {directory}")

        normalized_status = normalize_report_status(status)
        now = (updated_at or _local_now()).replace(microsecond=0)
        old_payload = _read_json(directory / "status.json") or {}
        history = old_payload.get("history") if isinstance(old_payload.get("history"), list) else []
        history = list(history)
        history.append(
            {
                "status": normalized_status,
                "status_label": STATUS_LABELS[normalized_status],
                "updated_at": _iso(now),
                "updated_by": self._build_actor(role=role),
                "note": str(note or ""),
            }
        )
        payload = {
            "schema_version": 1,
            "status": normalized_status,
            "status_label": STATUS_LABELS[normalized_status],
            "updated_at": _iso(now),
            "updated_by": self._build_actor(role=role),
            "history": history[-100:],
        }
        _atomic_write_json(directory / "status.json", payload)
        report = self.read_report(directory)
        return report or {}

    def mark_opened(self, report_dir: str | os.PathLike[str], *, role: str | None = None) -> dict[str, Any] | None:
        report = self.read_report(report_dir)
        if not report:
            return None
        if report.get("status") != STATUS_NEW:
            return report
        return self.update_status(report_dir, STATUS_READ, role=role, note="opened")

    def collect_logs_for_period(self, start: datetime, end: datetime) -> str:
        lines: list[str] = [
            f"Логи за период: {_iso(start)} - {_iso(end)}",
            f"Папки логов: {', '.join(str(path) for path in self.logs_dirs) or 'не заданы'}",
            "",
        ]
        found_files = 0
        cutoff_ts = start.timestamp() - 300

        for log_dir in self.logs_dirs:
            if not log_dir.is_dir():
                continue
            for path in sorted(log_dir.glob("*")):
                if not path.is_file() or path.suffix.lower() not in {".log", ".txt"}:
                    continue
                try:
                    if path.stat().st_mtime < cutoff_ts:
                        continue
                except Exception:
                    continue

                extracted = self._extract_log_file_period(path, start, end)
                if not extracted:
                    continue
                found_files += 1
                lines.append(f"===== {path} =====")
                lines.extend(extracted)
                lines.append("")

        if found_files == 0:
            lines.append("Логи за последний час не найдены.")
        return "\n".join(lines)

    def _extract_log_file_period(self, path: Path, start: datetime, end: datetime) -> list[str]:
        result: list[str] = []
        include_current_block = False
        saw_timestamp = False
        max_lines = 10000
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for raw_line in fh:
                    line = raw_line.rstrip("\n")
                    stamp = _parse_log_timestamp(line)
                    if stamp is not None:
                        saw_timestamp = True
                        include_current_block = start <= stamp <= end
                    if include_current_block:
                        result.append(line)
                        if len(result) >= max_lines:
                            result.append("... лог обрезан: превышен лимит 10000 строк для одного файла ...")
                            break
        except Exception as exc:
            return [f"Не удалось прочитать {path}: {exc}"]
        if not result and not saw_timestamp:
            try:
                all_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception as exc:
                return [f"Не удалось прочитать {path}: {exc}"]
            if all_lines:
                tail = all_lines[-400:]
                prefix = []
                if len(all_lines) > len(tail):
                    prefix.append("... лог без временных меток обрезан до последних 400 строк ...")
                return prefix + tail
        return result

    def _normalize_report_dir(self, report_dir: str | os.PathLike[str]) -> Path:
        path = Path(report_dir)
        if path.name == "report.json":
            return path.parent
        return path

    def _build_status_payload(self, status: str, *, role: str | None, updated_at: datetime) -> dict[str, Any]:
        normalized_status = normalize_report_status(status)
        actor = self._build_actor(role=role)
        history_item = {
            "status": normalized_status,
            "status_label": STATUS_LABELS[normalized_status],
            "updated_at": _iso(updated_at),
            "updated_by": actor,
            "note": "created",
        }
        return {
            "schema_version": 1,
            "status": normalized_status,
            "status_label": STATUS_LABELS[normalized_status],
            "updated_at": _iso(updated_at),
            "updated_by": actor,
            "history": [history_item],
        }

    def _build_actor(self, *, role: str | None) -> dict[str, Any]:
        username = os.environ.get("USERNAME") or os.environ.get("USER") or ""
        if not username:
            try:
                username = getpass.getuser()
            except Exception:
                username = ""
        return {
            "role": str(role or "").strip(),
            "user": username,
            "host": socket.gethostname(),
            "pid": os.getpid(),
        }

    def _build_context(self, *, role: str | None) -> dict[str, Any]:
        context = self._build_actor(role=role)
        try:
            from rem_card.app.paths import BAZA_DIR, REPORT_DIR
            from rem_card.app.version import APP_VERSION

            context.update(
                {
                    "app_version": APP_VERSION,
                    "baza_dir": BAZA_DIR,
                    "report_dir": REPORT_DIR,
                }
            )
        except Exception:
            context.update({"app_version": "", "baza_dir": "", "report_dir": ""})
        context.update(
            {
                "executable": sys.executable,
                "argv": list(sys.argv),
                "cwd": os.getcwd(),
            }
        )
        return context
