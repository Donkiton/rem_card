import ctypes
import json
import os
from pathlib import Path

import pytest

from rem_card.app import runtime_paths
from rem_card.app.local_replica_sync import build_local_replica_path
from rem_card.services import local_replica_health as health


def test_dev_cache_is_stable_separate_from_release_and_other_checkout(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(runtime_paths, "is_compiled", lambda: True)
    release = Path(runtime_paths.get_local_cache_dir())
    assert release == tmp_path / "RemCard" / "cache"
    release.mkdir(parents=True)
    sentinel = release / "replica.db"
    sentinel.write_bytes(b"release")
    monkeypatch.setattr(runtime_paths, "is_compiled", lambda: False)
    monkeypatch.setattr(runtime_paths, "get_dev_checkout_root", lambda: str(tmp_path / "checkout1"))
    development = Path(runtime_paths.get_local_cache_dir())
    assert development != release and release not in development.parents
    assert runtime_paths.get_local_cache_dir() == str(development)
    monkeypatch.setattr(runtime_paths, "get_dev_checkout_root", lambda: str(tmp_path / "checkout2"))
    assert Path(runtime_paths.get_local_cache_dir()) not in (development, release)
    assert sentinel.read_bytes() == b"release"


def test_same_database_client_and_role_use_distinct_replica_and_health_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(runtime_paths, "get_dev_checkout_root", lambda: str(tmp_path / "checkout"))
    arguments = dict(central_db_path=str(tmp_path / "central.db"), client_id="same-client", role="doctor")
    monkeypatch.setattr(runtime_paths, "is_compiled", lambda: True)
    release = build_local_replica_path(cache_dir=runtime_paths.get_local_cache_dir(), **arguments)
    monkeypatch.setattr(runtime_paths, "is_compiled", lambda: False)
    development = build_local_replica_path(cache_dir=runtime_paths.get_local_cache_dir(), **arguments)
    assert release != development
    assert Path(release).name == Path(development).name
    assert health.build_local_replica_health_path(release) != health.build_local_replica_health_path(development)


@pytest.mark.parametrize("winerror", [5, 32])
def test_health_replace_retries_transient_windows_denial(tmp_path, monkeypatch, winerror):
    path = tmp_path / "health.json"
    path.write_text('{"old": true}')
    replace = os.replace
    calls = []
    sleeps = []

    def locked_then_free(source, target):
        calls.append(target)
        if len(calls) < 3:
            assert json.loads(path.read_text()) == {"old": True}
            error = PermissionError("Windows sharing lock")
            error.winerror = winerror
            raise error
        replace(source, target)

    monkeypatch.setattr(health.os, "replace", locked_then_free)
    monkeypatch.setattr(health.time, "sleep", sleeps.append)
    health._atomic_write_json(path, {"new": True})
    assert len(calls) == 3 and sleeps == [0.025, 0.05]
    assert json.loads(path.read_text()) == {"new": True}
    assert list(tmp_path.iterdir()) == [path]


def test_health_permanent_denial_is_bounded_and_keeps_old_file(tmp_path, monkeypatch):
    path = tmp_path / "health.json"
    path.write_text('{"old": true}')
    calls = []
    sleeps = []

    def denied(*_args):
        calls.append(1)
        error = PermissionError("Access denied")
        error.winerror = 5
        raise error

    monkeypatch.setattr(health.os, "replace", denied)
    monkeypatch.setattr(health.time, "sleep", sleeps.append)
    with pytest.raises(PermissionError):
        health._atomic_write_json(path, {"new": True})
    assert len(calls) == 4 and sum(sleeps) == pytest.approx(0.15)
    assert json.loads(path.read_text()) == {"old": True}
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows sharing semantics")
def test_real_windows_reader_lock_is_recovered_without_deleting_health(tmp_path, monkeypatch):
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel.CreateFileW
    create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create_file.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    path = tmp_path / "health.json"
    path.write_text('{"old": true}')
    handle = create_file(str(path), 0x80000000, 1 | 2, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value
    released = []

    def release_on_retry(_seconds):
        assert path.exists() and json.loads(path.read_text()) == {"old": True}
        kernel.CloseHandle(handle)
        released.append(True)

    monkeypatch.setattr(health.time, "sleep", release_on_retry)
    try:
        health._atomic_write_json(path, {"new": True})
    finally:
        if not released:
            kernel.CloseHandle(handle)
    assert released == [True]
    assert json.loads(path.read_text()) == {"new": True}


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows sharing semantics")
def test_dev_quarantine_does_not_replace_locked_previous_archive(tmp_path, monkeypatch):
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    path = tmp_path / "dev_database_paths.json"
    payload = '{"active_baza_dir": ["invalid"]}'
    path.write_text(payload)
    monkeypatch.setattr(runtime_paths.time, "time", lambda: 1234567890.0)
    previous = tmp_path / "dev_database_paths.json.broken.1234567890"
    previous.write_text("older corrupt config")
    handle = kernel.CreateFileW(str(previous), 0x80000000, 1 | 2, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        archived = runtime_paths._quarantine_broken_dev_database_config(str(path))
        assert archived and Path(archived).read_text() == payload
        assert not path.exists()
        assert previous.read_text() == "older corrupt config"
    finally:
        kernel.CloseHandle(handle)
