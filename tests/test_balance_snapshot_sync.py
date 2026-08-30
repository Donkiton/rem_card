import os
import threading
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QObject, Signal, QTimer
from PySide6.QtWidgets import QApplication

from rem_card.ui.shared import balance_snapshot_sync as module
from rem_card.ui.shared.balance_snapshot_sync import BalanceSnapshotSync


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class Worker(QObject):
    succeeded = Signal(object)
    failed = Signal(object)
    finished = Signal()

    def __init__(self, fn, request):
        super().__init__()
        self.request = request
        self.running = False

    def start(self):
        self.running = True

    def isRunning(self):
        return self.running

    def quit(self):
        self.running = False

    def complete(self, snapshot):
        self.succeeded.emit({**self.request, "snapshot": snapshot})
        self.running = False
        self.finished.emit()

    def fail(self):
        self.failed.emit(RuntimeError("test DB unavailable"))
        self.running = False
        self.finished.emit()


@pytest.fixture
def state(app, monkeypatch):
    monkeypatch.setattr(module, "AsyncCallThread", Worker)
    state = SimpleNamespace(context=(7, "today", "live"), sequence=0, applied=[])
    state.sync = BalanceSnapshotSync(
        app, context_provider=lambda: state.context,
        load_snapshot=lambda request: {},
        apply_snapshot=lambda snapshot, request: state.applied.append((snapshot, request)),
        overlay_sequence_provider=lambda: state.sequence, role="test",
    )
    yield state
    state.sync.shutdown()


def snapshot(version, value=100):
    return {"change_id": version, "balance_runtime": {"value": value}, "fluids": [value]}


def start(state):
    state.sync._timer.stop()
    state.sync._start_if_ready()
    return state.sync._worker


def test_burst_is_coalesced_and_navigation_does_not_duplicate_read(state):
    for cursor in range(1, 30):
        state.sync.schedule(cursor)
    worker = start(state)
    assert worker.request["required_change_id"] == 29
    for _ in range(10):
        state.sync.ensure_current()
    assert not state.sync._pending
    worker.complete(snapshot(29))
    assert len(state.applied) == 1
    assert not state.sync.is_dirty
    assert not state.sync._timer.isActive()


def test_write_during_read_discards_old_response_and_keeps_new_overlay(state):
    state.sequence = 1
    state.sync.schedule(10)
    first = start(state)
    state.sequence = 2
    state.sync.schedule(11)
    first.complete(snapshot(10))
    assert not state.applied
    second = start(state)
    assert second.request["overlay_sequence"] == 2
    second.complete(snapshot(11, 200))
    assert state.applied[0][0]["balance_runtime"]["value"] == 200


def test_replica_behind_cursor_never_replaces_display_and_retries_are_bounded(state):
    state.sync.schedule(12)
    for _ in range(3):
        start(state).complete(snapshot(11))
    assert not state.applied
    assert state.sync.is_dirty
    assert not state.sync._pending
    assert not state.sync._timer.isActive()
    assert state.sync._recovery_timer.isActive()
    state.sync.ensure_current()
    start(state).complete(snapshot(12))
    assert not state.sync.is_dirty


def test_failure_keeps_old_data_without_endless_reads(state):
    state.sync.merge_card_snapshot(snapshot(10))
    state.sync.schedule(11)
    for _ in range(3):
        start(state).fail()
    assert state.sync.is_dirty
    assert not state.sync._timer.isActive()
    assert state.sync.merge_card_snapshot({})["balance_runtime"] == {"value": 100}


def test_recovery_after_short_failures_eventually_updates_without_another_change(state):
    state.sync.schedule(11)
    for _ in range(3):
        start(state).fail()
    assert state.sync._recovery_timer.isActive()
    state.sync._recovery_timer.stop()
    state.sync._retry_after_backoff()
    start(state).complete(snapshot(11))
    assert len(state.applied) == 1
    assert not state.sync.is_dirty
    assert not state.sync._recovery_timer.isActive()


def test_ui_apply_failure_is_caught_and_uses_bounded_retry(state):
    def fail_apply(*args):
        raise RuntimeError("test apply failed")

    state.sync._apply_snapshot = fail_apply
    state.sync.schedule(11)
    for _ in range(3):
        start(state).complete(snapshot(11))
    assert state.sync.is_dirty
    assert not state.sync._timer.isActive()
    assert state.sync._recovery_timer.isActive()


@pytest.mark.parametrize("fails", [False, True])
def test_patient_switch_rejects_old_result_and_old_failure(state, fails):
    state.sync.schedule(10)
    old = start(state)
    state.sync.reset()
    state.context = (8, "tomorrow", "archive")
    if fails:
        old.fail()
    else:
        old.complete(snapshot(10))
    assert not state.applied
    assert not state.sync.is_dirty
    assert not state.sync._pending


def test_new_patient_request_waits_for_old_worker_without_starting_parallel_reads(state):
    state.sync.schedule(10)
    old = start(state)
    state.sync.reset()
    state.context = (8, "today", "live")
    state.sync.schedule(20)
    assert start(state) is old
    old.complete(snapshot(10))
    current = start(state)
    assert current.request["context"] == state.context
    current.complete(snapshot(20))
    assert len(state.applied) == 1


def test_full_and_vitals_snapshots_cannot_overwrite_newer_balance(state):
    state.sync.schedule(12)
    start(state).complete(snapshot(12, 300))
    older = snapshot(11, 0)
    older["patient"] = "header"
    merged = state.sync.merge_card_snapshot(older)
    assert merged["balance_runtime"] == {"value": 300}
    assert merged["patient"] == "header"
    assert merged["change_id"] == 11  # do not forge full-card freshness
    assert state.sync.merge_card_snapshot({"change_id": 13})["fluids"] == [300]
    newer = state.sync.merge_card_snapshot(snapshot(14, 400))
    assert newer["balance_runtime"] == {"value": 400}


def test_older_targeted_read_cannot_overwrite_newer_full_card(state):
    state.sync.schedule(10)
    worker = start(state)
    state.sync.merge_card_snapshot(snapshot(12, 300))
    worker.complete(snapshot(11, 200))
    assert not state.applied
    assert state.sync.is_dirty


def test_shutdown_is_nonblocking_and_does_not_apply_pending_result(state):
    state.sync.schedule(10)
    worker = start(state)
    state.sync.shutdown()
    worker.complete(snapshot(10))
    assert not state.applied
    assert not state.sync._timer.isActive()


def test_slow_database_load_runs_off_gui_thread(app):
    entered = threading.Event()
    release = threading.Event()
    thread_ids, applied, heartbeats = [], [], []

    def load(request):
        thread_ids.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        return snapshot(10)

    sync = BalanceSnapshotSync(
        app, context_provider=lambda: (7, "today"), load_snapshot=load,
        apply_snapshot=lambda *args: applied.append(args), role="test", delay_ms=0,
    )
    timer = QTimer()
    timer.setInterval(1)
    timer.timeout.connect(lambda: heartbeats.append(1))
    try:
        timer.start()
        sync.schedule(10)
        deadline = time.monotonic() + 2
        while (not entered.is_set() or len(heartbeats) < 3) and time.monotonic() < deadline:
            app.processEvents()
        assert entered.is_set() and len(heartbeats) >= 3
        assert thread_ids == [thread_ids[0]] and thread_ids[0] != threading.get_ident()
        assert not applied
        release.set()
        deadline = time.monotonic() + 2
        while not applied and time.monotonic() < deadline:
            app.processEvents()
        assert applied
    finally:
        release.set()
        timer.stop()
        sync.shutdown()
