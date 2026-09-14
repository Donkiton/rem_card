"""Safe attach-or-create handling for the unified database chooser."""
from __future__ import annotations

import os
import socket
from pathlib import Path


def _selected_root(value: str | os.PathLike[str]) -> Path:
    raw = os.fspath(value) if value is not None else ""
    raw = str(raw).strip().strip('"')
    if not raw:
        raise ValueError("Укажите папку базы данных.")
    return Path(os.path.abspath(os.path.normpath(raw)))


def _validate_existing(root: Path) -> tuple[bool, str]:
    from rem_card.app.runtime_paths import validate_dev_baza_dir

    return validate_dev_baza_dir(str(root))


def _preflight_existing_layout(root: Path) -> tuple[bool, str]:
    """Check path shape without opening SQLite or creating coordination files."""
    from rem_card.app.runtime_paths import get_journal_db_path, get_required_baza_paths

    db_path = Path(get_journal_db_path(str(root)))
    if not db_path.is_file():
        return False, f"В выбранной папке не найдена база данных: {db_path}"
    settings_path = root / "settings" / "remcard_settings.db"
    if not settings_path.is_file():
        return False, f"В выбранной папке не найдена база настроек: {settings_path}"
    missing = [Path(path) for path in get_required_baza_paths(str(root)) if not Path(path).is_dir()]
    if missing:
        relative = [str(path.relative_to(root)) for path in missing]
        shown = ", ".join(relative[:6])
        suffix = f" и ещё {len(relative) - 6}" if len(relative) > 6 else ""
        return False, f"В папке базы не хватает служебных каталогов: {shown}{suffix}"
    return True, "ok"


def _with_session_validation(root: Path) -> tuple[bool, str]:
    """Revalidate an existing root while maintenance admission is held."""
    from rem_card.app.unified_access import SessionLease

    lease = SessionLease(root, "path_validation")
    if not lease.acquire():
        return False, "Выбранная база закрыта на обслуживание."
    try:
        return _validate_existing(root)
    finally:
        lease.release()


def _create_pristine_root(root: Path) -> tuple[bool, str]:
    from rem_card.app.runtime_paths import create_baza_structure_and_db
    from rem_card.app.sqlite_shared import FileWriteLock
    from rem_card.app.unified_access import SessionLease
    from rem_card.data.settings.settings_db import SettingsDatabase

    # SessionLease blocks maintenance from starting during initialization.  A
    # separate exclusive file lock serializes two first-run choosers, because
    # role/session leases are intentionally shared.
    lease = SessionLease(root, "path_initialization")
    if not lease.acquire():
        return False, "Выбранная папка закрыта на обслуживание."

    creation_lock = FileWriteLock(
        str(root / "session_locks" / "database_setup.lock"),
        stale_timeout_sec=10 * 60,
    )
    owner_id = f"{socket.gethostname()}:{os.getpid()}:database_setup"
    try:
        if not creation_lock.acquire(owner_id, "database_setup"):
            return False, "Создание базы в выбранной папке уже выполняется. Повторите позже."

        # Only coordination files created by the leases above may have appeared
        # since the pristine check.  Never bootstrap over user files or a
        # partially populated database root.
        unexpected = sorted(entry.name for entry in root.iterdir() if entry.name != "session_locks")
        if unexpected:
            shown = ", ".join(unexpected[:6])
            suffix = f" и ещё {len(unexpected) - 6}" if len(unexpected) > 6 else ""
            return False, (
                "Выбранная папка изменилась во время подготовки и больше не пуста. "
                f"Элементы: {shown}{suffix}"
            )

        ok, message = create_baza_structure_and_db(str(root))
        if not ok:
            return False, message

        SettingsDatabase(baza_dir=str(root)).ensure_ready()
        ok, message = _validate_existing(root)
        if not ok:
            return False, f"Созданная база не прошла итоговую проверку: {message}"
        return True, "created"
    except Exception as exc:
        return False, f"Не удалось создать базу RemCard: {exc}"
    finally:
        creation_lock.release()
        lease.release()


def prepare_database_root(baza_dir: str | os.PathLike[str]) -> tuple[bool, str]:
    """Attach a complete RemCard root or initialize an existing empty folder.

    Existing folders are validated without schema/bootstrap writes.  Creation
    is allowed only when the selected folder was empty before any RemCard lock
    was established.
    """
    try:
        root = _selected_root(baza_dir)
    except (TypeError, ValueError, OSError) as exc:
        return False, str(exc)

    if not root.is_dir():
        return False, f"Папка базы недоступна: {root}"

    try:
        entries = list(root.iterdir())
    except OSError as exc:
        return False, f"Не удалось прочитать выбранную папку: {exc}"

    if entries:
        ok, message = _preflight_existing_layout(root)
        if not ok:
            return False, message
        ok, message = _with_session_validation(root)
        return (True, "existing") if ok else (False, message)

    return _create_pristine_root(root)
