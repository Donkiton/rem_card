"""Local process barrier for one shared emergency session on the nurse's PC."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import time
import uuid

from rem_card.app.role_session_lock import RoleSessionLock


class EmergencySessionBusy(RuntimeError):
    pass


def _lock(path: Path, owner: str) -> RoleSessionLock:
    return RoleSessionLock(str(path), "emergency_local", owner,
                           stale_timeout_sec=60, heartbeat_sec=5)


@contextmanager
def _gate(directory: Path):
    lock = _lock(directory / "coord.lock", uuid.uuid4().hex)
    deadline = time.monotonic() + 2
    while not lock.acquire():
        if time.monotonic() >= deadline:
            raise EmergencySessionBusy("Другое окно сейчас меняет состояние аварийной сессии. Повторите действие.")
        time.sleep(0.05)
    try:
        yield
    finally:
        lock.release()


def _held(path: Path) -> bool:
    return _lock(path, uuid.uuid4().hex).is_held_by_other()


def active_participants(directory: str | Path, *, excluding: str = "") -> list[str]:
    folder = Path(directory) / "participants"
    if not folder.exists():
        return []
    return [str(path) for path in folder.glob("*.lock")
            if path.name != excluding and _held(path)]


def finish_in_progress(directory: str | Path) -> bool:
    return _held(Path(directory) / "finish.lock")


class EmergencyParticipant:
    def __init__(self, directory: str | Path, role: str):
        self.directory = Path(directory)
        self.identity = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
        self.role = role
        self.filename = self.identity + ".lock"
        self.lock = _lock(self.directory / "participants" / self.filename, self.identity)
        self.finish_lock = None
        self.closed = False
        with _gate(self.directory):
            if finish_in_progress(self.directory):
                raise EmergencySessionBusy(
                    "Другое окно завершает аварийную работу. Дождитесь переноса данных и повторите запуск."
                )
            if not self.lock.acquire():
                raise EmergencySessionBusy("Не удалось зарегистрировать окно аварийной сессии.")

    def request_finish(self) -> None:
        with _gate(self.directory):
            if self.finish_lock is not None:
                return
            finish = _lock(self.directory / "finish.lock", self.identity)
            if not finish.acquire():
                raise EmergencySessionBusy("Завершение аварийной работы уже начато в другом окне.")
            self.finish_lock = finish

    def peers_ready(self) -> bool:
        return not active_participants(self.directory, excluding=self.filename)

    def another_window_is_finishing(self) -> bool:
        return self.finish_lock is None and finish_in_progress(self.directory)

    def cancel_finish(self) -> None:
        finish, self.finish_lock = self.finish_lock, None
        if finish is not None:
            finish.release()

    def release(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.lock.release()
        self.cancel_finish()


@contextmanager
def exclusive_emergency_merge(directory: str | Path):
    """Prevent a new local window from opening between peer checks and COMMIT."""
    directory = Path(directory)
    finish = _lock(directory / "finish.lock", "merge-" + uuid.uuid4().hex)
    with _gate(directory):
        if active_participants(directory) or not finish.acquire():
            raise EmergencySessionBusy("Сначала завершите сохранение и закройте другие окна аварийной сессии.")
    try:
        yield
    finally:
        finish.release()
