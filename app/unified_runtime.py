"""Role lifecycle boundaries shared by the unified shell and its tests."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
import threading
import time
import uuid


class CompatibilityError(RuntimeError):
    pass


class CentralUnavailable(CompatibilityError):
    """The central source could not be read; a separately isolated local mode may be offered."""


def read_institution(root: str) -> dict:
    """Short read-only startup read, protected by the same maintenance barrier."""
    from rem_card.app.unified_access import SessionLease
    from rem_card.app.sqlite_uri import build_sqlite_file_uri

    lease = SessionLease(root, "startup_settings")
    if not lease.acquire():
        return {}
    try:
        path = Path(root) / "settings" / "remcard_settings.db"
        if not path.is_file():
            return {}
        connection = sqlite3.connect(build_sqlite_file_uri(path, mode="ro"), uri=True, timeout=2)
        try:
            row = connection.execute("SELECT value_json FROM app_settings WHERE scope=? AND key=?", ("institution", "identity")).fetchone()
            value = json.loads(row[0]) if row else {}
            return value if isinstance(value, dict) else {}
        finally:
            connection.close()
    finally:
        lease.release()


def check_client_compatibility(root: str, version: str) -> None:
    """Read the policy before constructing a writable database manager."""
    from rem_card.app.startup_db_guard import _compare_client_versions

    path = Path(root) / "config" / "client_policy.json"
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if not (Path(root) / "archiv" / "rao_journal.db").is_file():
            raise CentralUnavailable("Общая база недоступна. Проверьте соединение и выбранный путь.")
        return
    except OSError as exc:
        raise CentralUnavailable("Не удалось подключиться к общей базе.") from exc
    except ValueError as exc:
        raise CompatibilityError("Повреждены требования к версии RemCard. Вход заблокирован.") from exc
    if not isinstance(policy, dict) or not isinstance(policy.get("min_client_version"), str):
        raise CompatibilityError("Повреждены требования к версии RemCard. Вход заблокирован.")
    minimum = policy["min_client_version"]
    if not re.fullmatch(r"\d+(?:\.\d+)+(?:[-+][A-Za-z0-9.-]+)?", minimum):
        raise CompatibilityError("Повреждены требования к версии RemCard. Вход заблокирован.")
    if _compare_client_versions(version, minimum) < 0:
        raise CompatibilityError(f"Требуется обновление RemCard до версии {minimum} или новее.")


def lifecycle_event(event: str, *, session_id: str = "", role: str = "", **fields) -> None:
    from rem_card.app.local_metrics import record_metric

    record_metric(event, 1, session_id=session_id, role=role, pid=os.getpid(), **fields)


class SessionShutdown:
    """Own accepted writes until their result is known; never depend on a form callback."""

    def __init__(self, containers, *, session_id: str | None = None, role: str = ""):
        self.containers = []
        for container in containers:
            if not any(existing is container for existing in self.containers):
                self.containers.append(container)
        self.session_id = session_id or uuid.uuid4().hex
        self.role = role
        self._lock = threading.Lock()
        self._completed: set[int] = set()

    def run(self) -> dict:
        from rem_card.app.main import _shutdown_window_resources
        from rem_card.app.logger import logger
        from types import SimpleNamespace

        with self._lock:
            started = time.monotonic()
            blocked = []
            for container in self.containers:
                if id(container) in self._completed:
                    continue
                data = getattr(container, "data_service", None)
                # Unknown write is not an unsaved form. Keep both container and lease.
                if bool(getattr(data, "_unknown_active_write", False)):
                    blocked.append("Результат сетевой записи не подтверждён")
                    continue
                window = SimpleNamespace(iter_runtime_containers=lambda c=container: [c])
                if _shutdown_window_resources(window, logger):
                    self._completed.add(id(container))
                else:
                    blocked.append("Ожидание завершения записи или закрытия соединения")
            ok = len(self._completed) == len(self.containers)
            lifecycle_event("role_resources_released" if ok else "role_leave_blocked",
                            session_id=self.session_id, role=self.role,
                            elapsed_ms=round((time.monotonic() - started) * 1000, 2),
                            remaining=len(self.containers) - len(self._completed))
            return {"ok": ok, "blocked": blocked}
