from __future__ import annotations

from pathlib import Path


class _FakeShortcut:
    def __init__(self, target: Path | str, arguments: str = ""):
        self.TargetPath = str(target)
        self.Arguments = arguments
        self.WorkingDirectory = "unchanged"
        self.IconLocation = "unchanged"
        self.save_calls = 0

    def Save(self):
        self.save_calls += 1


class _FakeShell:
    def __init__(self, shortcuts):
        self.shortcuts = {str(Path(path)): value for path, value in shortcuts.items()}
        self.opened = []

    def CreateShortcut(self, path):
        self.opened.append(path)
        return self.shortcuts[str(Path(path))]


def _link(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"existing shortcut")
    return path


def test_migrates_only_known_same_install_targets_and_keeps_safe_role_arguments(tmp_path):
    from rem_card.app.unified_shortcuts import _migrate_known_legacy_shortcuts

    install = tmp_path / "Prog"
    desktop = tmp_path / "Desktop"
    start_menu = tmp_path / "Start Menu" / "Programs" / "RemCard"
    install.mkdir()
    executable = install / "RemCard.exe"
    executable.write_bytes(b"exe")

    doctor_link = _link(desktop, "Врач.lnk")
    nurse_link = _link(start_menu, "Медсестра.lnk")
    setup_link = _link(start_menu, "Настройка пути.lnk")
    shortcuts = {
        doctor_link: _FakeShortcut(install / "RemCardDoctor.exe"),
        nurse_link: _FakeShortcut(install / "RemCardNurse.exe", "--role nurse"),
        setup_link: _FakeShortcut(install / "RemCardPathSetup.exe"),
    }
    shell = _FakeShell(shortcuts)

    result = _migrate_known_legacy_shortcuts(
        executable_path=executable,
        shortcut_directories=(desktop, start_menu.parent),
        shell_factory=lambda: shell,
    )

    assert result.scanned == result.migrated == 3
    assert not result.errors
    for shortcut in shortcuts.values():
        assert Path(shortcut.TargetPath) == executable
        assert Path(shortcut.WorkingDirectory) == install
        assert shortcut.IconLocation == f"{executable},0"
        assert shortcut.save_calls == 1
    assert shortcuts[nurse_link].Arguments == "--role nurse"


def test_skips_foreign_unknown_and_custom_argument_shortcuts_without_saving(tmp_path):
    from rem_card.app.unified_shortcuts import _migrate_known_legacy_shortcuts

    install = tmp_path / "Prog"
    other_install = tmp_path / "Other" / "Prog"
    desktop = tmp_path / "Desktop"
    install.mkdir(parents=True)
    other_install.mkdir(parents=True)
    executable = install / "RemCard.exe"
    executable.write_bytes(b"exe")

    foreign_link = _link(desktop, "Другой RemCard.lnk")
    unknown_link = _link(desktop, "Unknown.lnk")
    custom_link = _link(desktop, "Custom.lnk")
    relative_link = _link(desktop, "Relative.lnk")
    shortcuts = {
        foreign_link: _FakeShortcut(other_install / "RemCardDoctor.exe"),
        unknown_link: _FakeShortcut(install / "Other.exe"),
        custom_link: _FakeShortcut(install / "RemCardNurse.exe", "--role nurse --debug"),
        relative_link: _FakeShortcut("RemCardDoctor.exe"),
    }
    shell = _FakeShell(shortcuts)

    result = _migrate_known_legacy_shortcuts(
        executable_path=executable,
        shortcut_directories=(desktop,),
        shell_factory=lambda: shell,
    )

    assert result.scanned == result.skipped == 4
    assert result.migrated == 0
    assert not result.errors
    assert all(shortcut.save_calls == 0 for shortcut in shortcuts.values())
    assert all(shortcut.WorkingDirectory == "unchanged" for shortcut in shortcuts.values())


def test_missing_user_directories_do_not_initialize_com_or_create_links(tmp_path):
    from rem_card.app.unified_shortcuts import _migrate_known_legacy_shortcuts

    executable = tmp_path / "Prog" / "RemCard.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"exe")
    factory_calls = []

    result = _migrate_known_legacy_shortcuts(
        executable_path=executable,
        shortcut_directories=(tmp_path / "missing-desktop", tmp_path / "missing-menu"),
        shell_factory=lambda: factory_calls.append(True),
    )

    assert result.scanned == result.migrated == result.skipped == 0
    assert not result.errors
    assert factory_calls == []


def test_non_unified_executable_is_never_used_as_migration_target(tmp_path):
    from rem_card.app.unified_shortcuts import _migrate_known_legacy_shortcuts

    desktop = tmp_path / "Desktop"
    link = _link(desktop, "Врач.lnk")
    legacy_executable = tmp_path / "Prog" / "RemCardDoctor.exe"
    shortcut = _FakeShortcut(legacy_executable)
    shell = _FakeShell({link: shortcut})

    result = _migrate_known_legacy_shortcuts(
        executable_path=legacy_executable,
        shortcut_directories=(desktop,),
        shell_factory=lambda: shell,
    )

    assert result == type(result)()
    assert shell.opened == []
    assert shortcut.save_calls == 0


def test_process_level_wrapper_runs_migration_once(monkeypatch):
    from rem_card.app import unified_shortcuts

    expected = unified_shortcuts.ShortcutMigrationResult(scanned=1, migrated=1)
    calls = []
    monkeypatch.setattr(unified_shortcuts, "_once_result", None)
    monkeypatch.setattr(
        unified_shortcuts,
        "migrate_known_legacy_shortcuts",
        lambda: calls.append(True) or expected,
    )

    assert unified_shortcuts.migrate_known_legacy_shortcuts_once() is expected
    assert unified_shortcuts.migrate_known_legacy_shortcuts_once() is expected
    assert calls == [True]


def test_public_migration_is_noop_outside_compiled_application(monkeypatch):
    from rem_card.app import unified_shortcuts

    monkeypatch.setattr(unified_shortcuts.sys, "frozen", False, raising=False)
    monkeypatch.setattr(
        unified_shortcuts,
        "_migrate_known_legacy_shortcuts",
        lambda: (_ for _ in ()).throw(AssertionError("COM must not be initialized")),
    )

    assert unified_shortcuts.migrate_known_legacy_shortcuts() == unified_shortcuts.ShortcutMigrationResult()
