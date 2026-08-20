from __future__ import annotations

import json
import os
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.app import update_launcher, updater_main  # noqa: E402
from rem_card.app.update_checker import UpdateCandidate  # noqa: E402


def _candidate(release_dir: Path, version: str = "4.1.5") -> UpdateCandidate:
    manifest_payload = {"version": version}
    manifest = release_dir / "manifest.json"
    manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")
    (release_dir / update_launcher.UPDATER_EXE_NAME).write_bytes(b"updater")
    return UpdateCandidate(
        version=version,
        release_dir=str(release_dir),
        prog_dir=str(release_dir),
        manifest_path=str(manifest),
        manifest=manifest_payload,
    )


def test_local_starting_lock_allows_only_one_updater_launch(monkeypatch, tmp_path: Path):
    target = tmp_path / "install"
    release = tmp_path / "release"
    baza = tmp_path / "baza"
    target.mkdir()
    release.mkdir()
    baza.mkdir()
    candidate = _candidate(release)
    launches: list[list[str]] = []

    class RunningProcess:
        pid = 4242

        @staticmethod
        def poll():
            return None

    monkeypatch.setattr(update_launcher, "is_compiled", lambda: True)
    monkeypatch.setattr(update_launcher, "resolve_baza_dir", lambda: str(baza))
    monkeypatch.setattr(update_launcher, "get_executable_dir", lambda: str(target))
    monkeypatch.setattr(update_launcher, "_is_pid_alive", lambda _pid: True)
    monkeypatch.setattr(update_launcher.time, "sleep", lambda _seconds: None)

    def fake_popen(args, **_kwargs):
        launches.append([str(item) for item in args])
        return RunningProcess()

    monkeypatch.setattr(update_launcher, "popen_hidden", fake_popen)

    assert update_launcher.launch_update(candidate, wait_for_parent=False) is True
    assert update_launcher.launch_update(candidate, wait_for_parent=False) is False
    assert len(launches) == 1
    assert "--local-starting-lock" in launches[0]
    assert Path(update_launcher.get_local_update_starting_lock_path(str(target))).is_file()


def test_running_process_detection_is_scoped_to_target(monkeypatch, tmp_path: Path):
    target = tmp_path / "install"
    other = tmp_path / "other"
    target.mkdir()
    other.mkdir()
    monkeypatch.setattr(
        updater_main,
        "_iter_running_processes",
        lambda: [
            (101, "RemCardNurse.exe", str(target / "RemCardNurse.exe")),
            (102, "RemCardDoctor.exe", str(other / "RemCardDoctor.exe")),
            (103, "RemCardOperBlock.exe", ""),
            (104, "notepad.exe", ""),
        ],
    )

    assert updater_main._find_running_target_processes(str(target)) == [
        (101, "RemCardNurse.exe"),
        (103, "RemCardOperBlock.exe"),
    ]


def test_wait_for_target_processes_finishes_only_after_app_closes(monkeypatch, tmp_path: Path):
    states = iter([[(101, "RemCardNurse.exe")], []])
    messages: list[str] = []
    monkeypatch.setattr(updater_main, "_find_running_target_processes", lambda _target: next(states))
    monkeypatch.setattr(updater_main.time, "sleep", lambda _seconds: None)

    updater_main._wait_for_target_processes(
        str(tmp_path),
        lambda message, _progress: messages.append(message),
    )

    assert messages and "RemCardNurse.exe" in messages[0]


def test_rename_failure_never_creates_nested_destination(monkeypatch, tmp_path: Path):
    source = tmp_path / "_internal"
    destination = tmp_path / "backup" / "_internal"
    source.mkdir()
    (source / "libcrypto-3.dll").write_bytes(b"locked")
    destination.parent.mkdir()

    monkeypatch.setattr(os, "rename", lambda *_args: (_ for _ in ()).throw(PermissionError("locked")))
    monkeypatch.setattr(updater_main.time, "sleep", lambda _seconds: None)

    try:
        updater_main._rename_path_with_retry(
            str(source),
            str(destination),
            "backup _internal",
            attempts=2,
            delay_sec=0,
        )
    except RuntimeError as exc:
        assert "locked" in str(exc)
    else:
        raise AssertionError("rename must fail when the source is locked")

    assert source.is_dir()
    assert not destination.exists()
    assert not (destination / "_internal").exists()


def test_internal_backup_failure_restores_root_files_without_partial_copy(monkeypatch, tmp_path: Path):
    source = tmp_path / "release"
    target = tmp_path / "install"
    source.mkdir()
    target.mkdir()

    manifest = {"schema_version": 1, "package_type": "full", "version": "4.1.5"}
    (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for exe_name in updater_main.REQUIRED_EXES:
        (source / exe_name).write_bytes(b"new")
    (source / "_internal").mkdir()
    (source / "_internal" / "libcrypto-3.dll").write_bytes(b"new-dll")

    (target / "RemCardNurse.exe").write_bytes(b"old")
    (target / "_internal").mkdir()
    (target / "_internal" / "libcrypto-3.dll").write_bytes(b"old-dll")

    real_rename = os.rename
    locked_internal = os.path.normcase(os.path.abspath(target / "_internal"))

    def fail_locked_internal(current, destination):
        if os.path.normcase(os.path.abspath(current)) == locked_internal:
            raise PermissionError("libcrypto-3.dll is in use")
        return real_rename(current, destination)

    monkeypatch.setattr(os, "rename", fail_locked_internal)
    monkeypatch.setattr(updater_main.time, "sleep", lambda _seconds: None)

    try:
        updater_main._replace_program_dir(
            source_dir=str(source),
            target_dir=str(target),
            status=lambda *_args: None,
            expected_manifest=manifest,
        )
    except RuntimeError as exc:
        assert "libcrypto-3.dll is in use" in str(exc)
    else:
        raise AssertionError("update must fail while _internal is locked")

    assert (target / "RemCardNurse.exe").read_bytes() == b"old"
    assert (target / "_internal" / "libcrypto-3.dll").read_bytes() == b"old-dll"
    assert not list(target.glob("__upd_old_*"))
    assert not list(target.glob("__upd_new_*"))
    assert not (target / "_internal" / "_internal").exists()
