import json
import logging
import sqlite3
from types import SimpleNamespace

import pytest

from rem_card.app.sqlite_shared import LocalWriteQueue
from rem_card.app.unified_runtime import CompatibilityError, SessionShutdown, check_client_compatibility


def test_startup_institution_uses_readonly_sqlite_uri(tmp_path, monkeypatch):
    from rem_card.app import unified_access, unified_runtime
    root = tmp_path / 'settings # percent %'
    path = root / 'settings' / 'remcard_settings.db'
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE app_settings(scope TEXT, key TEXT, value_json TEXT)')
        connection.execute('INSERT INTO app_settings VALUES (?, ?, ?)',
                           ('institution', 'identity', '{"short_name":"Hospital"}'))
    leases = []
    monkeypatch.setattr(unified_access, 'SessionLease', lambda *args: SimpleNamespace(
        acquire=lambda: True, release=lambda: leases.append('released')))
    original_connect = sqlite3.connect
    def readonly_connect(filename, **kwargs):
        assert filename.endswith('?mode=ro')
        assert '%23' in filename and '%25' in filename
        connection = original_connect(filename, **kwargs)
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            connection.execute('CREATE TABLE forbidden(value TEXT)')
        return connection
    monkeypatch.setattr(unified_runtime.sqlite3, 'connect', readonly_connect)
    assert unified_runtime.read_institution(str(root)) == {'short_name': 'Hospital'}
    assert leases == ['released']


@pytest.mark.skipif(__import__('os').name != 'nt', reason='Windows UNC path handling')
def test_startup_institution_unc_has_no_uri_authority(monkeypatch):
    from pathlib import Path
    from rem_card.app import unified_access, unified_runtime
    monkeypatch.setattr(unified_access, 'SessionLease', lambda *args: SimpleNamespace(acquire=lambda: True, release=lambda: None))
    monkeypatch.setattr(Path, 'is_file', lambda self: True)
    seen = []
    def connect(filename, **kwargs):
        seen.append(filename)
        return SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchone=lambda: None), close=lambda: None)
    monkeypatch.setattr(unified_runtime.sqlite3, 'connect', connect)
    assert unified_runtime.read_institution(r'\\server\share\Hospital #1') == {}
    assert seen[0].startswith('file:' + '\\\\server\\share\\')
    assert '%231' in seen[0] and seen[0].endswith('?mode=ro')


def test_compatibility_read_never_creates_missing_database(tmp_path):
    with pytest.raises(CompatibilityError):
        check_client_compatibility(str(tmp_path), "4.3.7")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("minimum", ["99.0.0", "broken", ""])
def test_incompatible_or_invalid_policy_blocks_without_modification(tmp_path, minimum):
    policy = tmp_path / "config" / "client_policy.json"
    policy.parent.mkdir()
    contents = json.dumps({"min_client_version": minimum})
    policy.write_text(contents, encoding="utf-8")
    with pytest.raises(CompatibilityError):
        check_client_compatibility(str(tmp_path), "4.3.7")
    assert policy.read_text(encoding="utf-8") == contents


def test_callback_lock_error_never_replays_committed_write():
    queue = LocalWriteQueue(logging.getLogger("unified-test"))
    writes, failures = [], []

    def notify(_):
        raise sqlite3.OperationalError("database is locked")

    queue.submit(func=lambda: writes.append(1), description="test", on_success=notify, on_error=failures.append)
    assert queue.shutdown(timeout=5)
    assert writes == [1]
    assert failures == []


def test_error_callback_does_not_kill_queue():
    queue = LocalWriteQueue(logging.getLogger("unified-test"))
    writes = []

    def fail():
        raise ValueError("mutation rejected")

    def notify(_):
        raise RuntimeError("view already closed")

    queue.submit(func=fail, description="first", on_error=notify)
    queue.submit(func=lambda: writes.append(1), description="second")
    assert queue.shutdown(timeout=5)
    assert writes == [1]


def test_shutdown_retries_unreleased_resources_and_blocks_unknown(monkeypatch):
    import rem_card.app.main as main
    results = iter((False, True))
    monkeypatch.setattr(main, "_shutdown_window_resources", lambda *a: next(results))
    data = SimpleNamespace(_unknown_active_write=True)
    container = SimpleNamespace(data_service=data)
    shutdown = SessionShutdown([container])
    assert not shutdown.run()["ok"]
    data._unknown_active_write = False
    assert not shutdown.run()["ok"]
    assert shutdown.run()["ok"]
    assert shutdown.run()["ok"]
