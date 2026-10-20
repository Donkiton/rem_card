from __future__ import annotations

import ctypes
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from rem_card.app import update_launcher
from rem_card.app.update_checker import UpdateCandidate, get_update_starting_lock_path


def _candidate(release: Path) -> UpdateCandidate:
    (release / update_launcher.UPDATER_EXE_NAME).write_bytes(b"updater")
    return UpdateCandidate(
        version="5.1.3",
        release_dir=str(release),
        prog_dir=str(release),
        manifest_path=str(release / "manifest.json"),
        manifest={"version": "5.1.3"},
    )


@pytest.fixture
def installation(monkeypatch, tmp_path):
    target = tmp_path / "install"
    release = tmp_path / "release"
    baza = tmp_path / "baza"
    for directory in (target, release, baza):
        directory.mkdir()
    candidate = _candidate(release)
    monkeypatch.setenv("REMCARD_BAZA_DIR", str(baza))
    monkeypatch.setattr(update_launcher, "is_compiled", lambda: True)
    monkeypatch.setattr(update_launcher, "resolve_baza_dir", lambda: str(baza))
    monkeypatch.setattr(update_launcher, "get_executable_dir", lambda: str(target))
    monkeypatch.setattr(update_launcher, "_is_pid_alive", lambda _pid: True)
    monkeypatch.setattr(update_launcher.time, "sleep", lambda _seconds: None)
    return candidate, target, baza


def test_uncontended_starting_lock_needs_no_directory_or_missing_file_probe(monkeypatch, tmp_path):
    candidate = _candidate(tmp_path)
    path = tmp_path / "starting.lock"

    def unexpected_probe(*_args, **_kwargs):
        raise AssertionError("Uncontended lock creation must go straight to O_EXCL")

    monkeypatch.setattr(update_launcher, "_is_lock_active", unexpected_probe)
    monkeypatch.setattr(update_launcher.os, "makedirs", unexpected_probe)

    assert update_launcher._write_starting_lock(str(path), candidate, str(tmp_path)) is True
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["state"] == "starting"
    assert payload["target_version"] == candidate.version
    assert payload["target"] == str(tmp_path)


def test_starting_lock_creates_missing_parent_directory(tmp_path):
    candidate = _candidate(tmp_path)
    path = tmp_path / "locks" / "starting.lock"

    assert update_launcher._write_starting_lock(str(path), candidate, str(tmp_path)) is True
    assert path.is_file()


def test_active_starting_lock_is_preserved(monkeypatch, tmp_path):
    candidate = _candidate(tmp_path)
    path = tmp_path / "starting.lock"
    payload = {"state": "starting", "updater_pid": 12345, "timestamp": time.time()}
    raw = json.dumps(payload)
    path.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(update_launcher, "_is_pid_alive", lambda _pid: True)

    assert update_launcher._write_starting_lock(str(path), candidate, str(tmp_path)) is False
    assert path.read_text(encoding="utf-8") == raw


@pytest.mark.parametrize("state", ["starting", "completed"])
def test_dead_or_terminal_starting_lock_can_be_replaced(monkeypatch, tmp_path, state):
    candidate = _candidate(tmp_path)
    path = tmp_path / "starting.lock"
    path.write_text(
        json.dumps({"state": state, "updater_pid": 12345, "timestamp": time.time()}),
        encoding="utf-8",
    )
    monkeypatch.setattr(update_launcher, "_is_pid_alive", lambda _pid: False)

    assert update_launcher._write_starting_lock(str(path), candidate, str(tmp_path)) is True
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["updater_pid"] == 0
    assert payload["target_version"] == candidate.version


def test_pid_mark_does_not_recreate_lock_already_consumed_by_updater(tmp_path):
    path = tmp_path / "consumed.lock"

    update_launcher._mark_starting_lock_updater_pid(str(path), 12345)

    assert not path.exists()


def test_pid_mark_preserves_launch_metadata(tmp_path):
    path = tmp_path / "starting.lock"
    path.write_text(json.dumps({"state": "starting", "target": "install", "updater_pid": 0}), encoding="utf-8")

    update_launcher._mark_starting_lock_updater_pid(str(path), 12345)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["updater_pid"] == 12345
    assert payload["target"] == "install"
    assert payload["state"] == "starting"


class _NativeWait:
    def __init__(self, result=0):
        self.result = result
        self.calls = []

    def __call__(self, handle, timeout):
        self.calls.append((handle, timeout))
        return self.result


def _install_native_wait(monkeypatch, wait):
    # Keep this portable without changing os.name for pathlib or other tests.
    monkeypatch.setattr(update_launcher, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    monkeypatch.setattr(
        update_launcher.ctypes,
        "windll",
        SimpleNamespace(user32=SimpleNamespace(WaitForInputIdle=wait)),
        raising=False,
    )


@pytest.mark.parametrize("native_result", [0, 258, 0xFFFFFFFF])
def test_native_gui_handoff_is_bounded_and_accepts_timeout_or_api_failure(monkeypatch, native_result):
    wait = _NativeWait(native_result)
    _install_native_wait(monkeypatch, wait)
    process = SimpleNamespace(_handle=0x123456789ABC)

    update_launcher._wait_for_updater_window(process)

    assert wait.calls == [(process._handle, 15_000)]
    assert wait.argtypes == [ctypes.c_void_p, ctypes.c_ulong]
    assert wait.restype is ctypes.c_ulong


@pytest.mark.parametrize("platform,handle", [("posix", 123), ("nt", None)])
def test_gui_handoff_skips_unsupported_process(monkeypatch, platform, handle):
    monkeypatch.setattr(update_launcher, "os", SimpleNamespace(name=platform))
    # Accessing a missing API would fail, but must not prevent the launch.
    monkeypatch.setattr(update_launcher.ctypes, "windll", None, raising=False)

    update_launcher._wait_for_updater_window(SimpleNamespace(_handle=handle))


@pytest.mark.parametrize("wait_for_window", [False, True])
def test_launch_preserves_package_arguments_and_waits_only_when_requested(monkeypatch, installation, wait_for_window):
    candidate, target, _baza = installation
    process = SimpleNamespace(pid=4242, poll=lambda: None)
    launches = []
    waits = []

    def popen(args, **kwargs):
        launches.append((args, kwargs))
        return process

    monkeypatch.setattr(update_launcher, "popen_hidden", popen)
    monkeypatch.setattr(update_launcher, "_wait_for_updater_window", lambda item: waits.append(item))

    assert update_launcher.launch_update(candidate, wait_for_parent=False, wait_for_window=wait_for_window) is True
    assert waits == ([process] if wait_for_window else [])
    args, kwargs = launches[0]
    assert args[0] == os.path.join(candidate.prog_dir, update_launcher.UPDATER_EXE_NAME)
    assert args[args.index("--source") + 1] == candidate.prog_dir
    assert args[args.index("--target") + 1] == str(target)
    assert args[args.index("--parent-pid") + 1] == "0"
    assert "--wait-for-window" not in args
    assert kwargs["cwd"] == candidate.prog_dir


@pytest.mark.parametrize("native_result", [0, 258, 0xFFFFFFFF])
def test_gui_timeout_or_failed_native_wait_does_not_cancel_running_update(monkeypatch, installation, native_result):
    candidate, target, baza = installation
    wait = _NativeWait(native_result)
    _install_native_wait(monkeypatch, wait)
    process = SimpleNamespace(pid=4242, _handle=123, poll=lambda: None)
    monkeypatch.setattr(update_launcher, "popen_hidden", lambda *_args, **_kwargs: process)

    assert update_launcher.launch_update(candidate, wait_for_window=True) is True
    assert wait.calls == [(123, 15_000)]
    assert Path(update_launcher.get_local_update_starting_lock_path(str(target))).is_file()
    assert Path(get_update_starting_lock_path(str(baza), target_dir=str(target))).is_file()


def test_process_exit_during_gui_handoff_cleans_both_starting_locks(monkeypatch, installation):
    candidate, target, baza = installation
    exits = iter([None, 1])
    process = SimpleNamespace(pid=4242, poll=lambda: next(exits))
    monkeypatch.setattr(update_launcher, "popen_hidden", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(update_launcher, "_wait_for_updater_window", lambda _process: None)

    assert update_launcher.launch_update(candidate, wait_for_window=True) is False
    assert not Path(update_launcher.get_local_update_starting_lock_path(str(target))).exists()
    assert not Path(get_update_starting_lock_path(str(baza), target_dir=str(target))).exists()


def test_unified_wrapper_can_request_gui_handoff(monkeypatch, tmp_path):
    candidate = _candidate(tmp_path)
    calls = []

    def launch(item, **kwargs):
        calls.append((item, kwargs))
        return True

    monkeypatch.setattr(update_launcher, "launch_update", launch)

    assert update_launcher.launch_unified_update(candidate, wait_for_parent=False, wait_for_window=True) is True
    assert calls == [(candidate, {"restart_exe": "RemCard.exe", "wait_for_parent": False, "wait_for_window": True})]
