"""Conservative migration of legacy RemCard shortcuts to the unified EXE."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import sys
import threading
from typing import Any, Callable, Iterable


_LEGACY_TARGETS = frozenset(
    {
        "remcarddoctor.exe",
        "remcardnurse.exe",
        "remcardoperblock.exe",
        "remcardoperblockemergency.exe",
        "remcardoperblockplanned.exe",
        "remcardpathsetup.exe",
    }
)
_ROLE_ARGUMENTS = re.compile(
    r"^--role(?:\s+|=)(?:doctor|nurse|operblock|operblock_emergency|operblock_planned)$",
    re.IGNORECASE,
)
_once_lock = threading.Lock()
_once_result: "ShortcutMigrationResult | None" = None


@dataclass(frozen=True)
class ShortcutMigrationResult:
    scanned: int = 0
    migrated: int = 0
    skipped: int = 0
    errors: tuple[str, ...] = ()


def _default_shortcut_directories() -> tuple[Path, ...]:
    candidates: list[Path] = []
    user_profile = str(os.environ.get("USERPROFILE") or "").strip()
    app_data = str(os.environ.get("APPDATA") or "").strip()
    if user_profile:
        candidates.append(Path(user_profile) / "Desktop")
    if app_data:
        candidates.append(Path(app_data) / "Microsoft" / "Windows" / "Start Menu" / "Programs")

    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = os.path.normcase(os.path.abspath(str(candidate)))
        if normalized not in seen:
            seen.add(normalized)
            result.append(Path(normalized))
    return tuple(result)


def _iter_existing_shortcuts(directories: Iterable[Path]) -> Iterable[Path]:
    seen: set[str] = set()
    for directory in directories:
        root = os.path.abspath(str(directory))
        if not os.path.isdir(root):
            continue
        for current, dir_names, file_names in os.walk(root, followlinks=False):
            # Never descend through directory links or junctions into an
            # unrelated installation tree.
            safe_dirs = []
            for name in dir_names:
                path = os.path.join(current, name)
                if not os.path.islink(path):
                    safe_dirs.append(name)
            dir_names[:] = safe_dirs
            for name in file_names:
                if name.casefold().endswith(".lnk"):
                    path = Path(current) / name
                    normalized = os.path.normcase(os.path.abspath(str(path)))
                    if path.is_file() and normalized not in seen:
                        seen.add(normalized)
                        yield path


def _normalized_target_path(raw_target: object) -> str:
    value = os.path.expandvars(str(raw_target or "").strip())
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1].strip()
    if not value or not os.path.isabs(value):
        return ""
    return os.path.normcase(os.path.abspath(value))


def _arguments_are_safe(raw_arguments: object) -> bool:
    arguments = str(raw_arguments or "").strip()
    return not arguments or bool(_ROLE_ARGUMENTS.fullmatch(arguments))


def _migrate_with_shell(
    shell: Any,
    *,
    executable_path: Path,
    shortcut_directories: Iterable[Path],
) -> ShortcutMigrationResult:
    install_dir = os.path.normcase(os.path.abspath(str(executable_path.parent)))
    unified_exe = os.path.abspath(str(executable_path))
    scanned = migrated = skipped = 0
    errors: list[str] = []

    for shortcut_path in _iter_existing_shortcuts(shortcut_directories):
        scanned += 1
        try:
            shortcut = shell.CreateShortcut(str(shortcut_path))
            target = _normalized_target_path(getattr(shortcut, "TargetPath", ""))
            if not target:
                skipped += 1
                continue
            target_path = Path(target)
            if target_path.name.casefold() not in _LEGACY_TARGETS:
                skipped += 1
                continue
            if os.path.normcase(os.path.abspath(str(target_path.parent))) != install_dir:
                skipped += 1
                continue
            if not _arguments_are_safe(getattr(shortcut, "Arguments", "")):
                skipped += 1
                continue
            if not shortcut_path.is_file():
                skipped += 1
                continue

            shortcut.TargetPath = unified_exe
            shortcut.WorkingDirectory = os.path.abspath(str(executable_path.parent))
            shortcut.IconLocation = f"{unified_exe},0"
            shortcut.Save()
            migrated += 1
        except Exception as exc:
            errors.append(f"{shortcut_path}: {type(exc).__name__}: {exc}")

    return ShortcutMigrationResult(
        scanned=scanned,
        migrated=migrated,
        skipped=skipped,
        errors=tuple(errors),
    )


def _migrate_known_legacy_shortcuts(
    *,
    executable_path: str | os.PathLike[str] | None = None,
    shortcut_directories: Iterable[str | os.PathLike[str]] | None = None,
    shell_factory: Callable[[], Any] | None = None,
) -> ShortcutMigrationResult:
    """Injectable implementation used by isolated tests."""

    executable = Path(executable_path or sys.executable)
    if executable.name.casefold() != "remcard.exe" or not executable.is_file():
        return ShortcutMigrationResult()
    directories = tuple(
        Path(value) for value in (
            shortcut_directories if shortcut_directories is not None else _default_shortcut_directories()
        )
    )
    if not any(path.is_dir() for path in directories):
        return ShortcutMigrationResult()

    if shell_factory is not None:
        return _migrate_with_shell(
            shell_factory(),
            executable_path=executable,
            shortcut_directories=directories,
        )

    pythoncom = None
    com_initialized = False
    try:
        import pythoncom as _pythoncom
        from win32com.client import Dispatch

        pythoncom = _pythoncom
        pythoncom.CoInitialize()
        com_initialized = True
        shell = Dispatch("WScript.Shell")
        return _migrate_with_shell(
            shell,
            executable_path=executable,
            shortcut_directories=directories,
        )
    except Exception as exc:
        return ShortcutMigrationResult(errors=(f"COM unavailable: {type(exc).__name__}: {exc}",))
    finally:
        if pythoncom is not None and com_initialized:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass


def migrate_known_legacy_shortcuts() -> ShortcutMigrationResult:
    """Update existing same-install legacy links in bounded user folders."""

    if not bool(getattr(sys, "frozen", False)):
        return ShortcutMigrationResult()
    return _migrate_known_legacy_shortcuts()


def migrate_known_legacy_shortcuts_once() -> ShortcutMigrationResult:
    """Process shortcuts at most once during the current RemCard process."""

    global _once_result
    with _once_lock:
        if _once_result is None:
            _once_result = migrate_known_legacy_shortcuts()
        return _once_result


__all__ = [
    "ShortcutMigrationResult",
    "migrate_known_legacy_shortcuts",
    "migrate_known_legacy_shortcuts_once",
]
