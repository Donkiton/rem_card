from __future__ import annotations

from types import SimpleNamespace

from rem_card.ui.nurse_view.nurse_main_widget import NurseMainWidget


class _Worker:
    def __init__(self, *, running: bool) -> None:
        self.running = running
        self.quit_calls = 0
        self.wait_calls = 0

    def isRunning(self) -> bool:
        return self.running

    def quit(self) -> None:
        self.quit_calls += 1

    def wait(self, _timeout_ms=None) -> bool:
        self.wait_calls += 1
        raise AssertionError("UI shutdown must not wait for a snapshot worker")


def test_nurse_snapshot_shutdown_is_non_blocking_and_invalidates_result() -> None:
    worker = _Worker(running=True)
    widget = SimpleNamespace(
        _snapshot_pending={"load_scope": "full"},
        _snapshot_request_id=17,
        _snapshot_worker=worker,
    )
    disconnected: list[object] = []
    widget._disconnect_snapshot_worker = disconnected.append

    NurseMainWidget._shutdown_snapshot_worker(widget)

    assert widget._snapshot_pending is None
    assert widget._snapshot_worker is None
    assert widget._snapshot_request_id == 18
    assert disconnected == [worker]
    assert worker.quit_calls == 1
    assert worker.wait_calls == 0


def test_nurse_snapshot_shutdown_does_not_quit_finished_worker() -> None:
    worker = _Worker(running=False)
    widget = SimpleNamespace(
        _snapshot_pending=None,
        _snapshot_request_id=3,
        _snapshot_worker=worker,
    )
    widget._disconnect_snapshot_worker = lambda _worker: None

    NurseMainWidget._shutdown_snapshot_worker(widget)

    assert worker.quit_calls == 0
    assert worker.wait_calls == 0
