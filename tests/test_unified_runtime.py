import json
import logging
import sqlite3
from types import SimpleNamespace

import pytest

from rem_card.app.sqlite_shared import LocalWriteQueue
from rem_card.app.unified_runtime import CompatibilityError, SessionShutdown, check_client_compatibility


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
