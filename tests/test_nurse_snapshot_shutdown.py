from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

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


def _bind_data_change_methods(widget) -> None:
    for name in ("_current_status_is_outcome", "_on_data_changes"):
        setattr(widget, name, getattr(NurseMainWidget, name).__get__(widget))


def test_nurse_slow_status_query_does_not_block_qt_change_dispatch() -> None:
    app = QApplication.instance() or QApplication([])
    del app
    status_calls: list[int] = []

    def slow_current_status(admission_id: int):
        status_calls.append(admission_id)
        time.sleep(0.5)
        return None

    requested_scopes: list[str] = []
    shift_date = datetime(2026, 10, 20, 8, 0)
    widget = SimpleNamespace(
        _is_closing=False,
        _selection_mode="card",
        _card_snapshot_cache=None,
        _card_snapshot_context=None,
        _current_date=shift_date,
        _patient_open_generation=1,
        remcard_service=SimpleNamespace(get_current_status=slow_current_status),
        layout_manager=SimpleNamespace(
            current_admission_id=41,
            selection_stack=SimpleNamespace(currentIndex=lambda: 0),
        ),
        isVisible=lambda: True,
        _changed_entities_from_payload=lambda _payload: {"vitals"},
        _invalidate_vitals_cache_from_payload=lambda *_args: None,
        _is_local_emergency_notice_payload=lambda *_args: False,
        _is_local_orders_force_payload=lambda *_args: False,
        _payload_is_relevant=lambda _payload: True,
        _handle_diet_sync=lambda *_args, **_kwargs: False,
        _refresh_orders_from_payload=lambda *_args, **_kwargs: None,
        _apply_partial_sync_actions=lambda *_args, **_kwargs: None,
        _request_card_snapshot=lambda **kwargs: requested_scopes.append(kwargs.get("load_scope", "full")),
        _payload_force_sources=lambda _payload: [],
    )
    _bind_data_change_methods(widget)
    payload = {
        "changed_entities": ["vitals"],
        "sync_actions": {"vitals_snapshot_required": True},
    }

    loop = QEventLoop()
    started = time.perf_counter()
    heartbeat_elapsed: list[float] = []
    QTimer.singleShot(0, lambda: widget._on_data_changes(payload))
    QTimer.singleShot(
        10,
        lambda: (heartbeat_elapsed.append(time.perf_counter() - started), loop.quit()),
    )
    QTimer.singleShot(1000, loop.quit)
    loop.exec()

    assert heartbeat_elapsed and heartbeat_elapsed[0] < 0.25
    assert status_calls == []
    assert requested_scopes == ["patient_open_vitals"]


def test_nurse_outcome_from_current_snapshot_still_skips_vitals_refresh() -> None:
    current_date = datetime(2026, 10, 20, 10, 15)
    normalized_snapshot_date = datetime(2026, 10, 20, 8, 0)
    outcome = SimpleNamespace(status=SimpleNamespace(is_outcome=lambda: True))
    status_calls: list[int] = []
    requested_scopes: list[str] = []
    widget = SimpleNamespace(
        _is_closing=False,
        _selection_mode="card",
        _card_snapshot_cache={
            "admission_id": 41,
            "shift_date": normalized_snapshot_date,
            "status": outcome,
        },
        _card_snapshot_context=(41, current_date, 1),
        _current_date=current_date,
        _patient_open_generation=1,
        remcard_service=SimpleNamespace(
            get_current_status=lambda admission_id: status_calls.append(admission_id)
        ),
        layout_manager=SimpleNamespace(
            current_admission_id=41,
            selection_stack=SimpleNamespace(currentIndex=lambda: 0),
        ),
        isVisible=lambda: True,
        _changed_entities_from_payload=lambda _payload: {"vitals"},
        _invalidate_vitals_cache_from_payload=lambda *_args: None,
        _is_local_emergency_notice_payload=lambda *_args: False,
        _is_local_orders_force_payload=lambda *_args: False,
        _payload_is_relevant=lambda _payload: True,
        _handle_diet_sync=lambda *_args, **_kwargs: False,
        _refresh_orders_from_payload=lambda *_args, **_kwargs: None,
        _apply_partial_sync_actions=lambda *_args, **_kwargs: None,
        _request_card_snapshot=lambda **kwargs: requested_scopes.append(kwargs.get("load_scope", "full")),
        _payload_force_sources=lambda _payload: [],
    )
    _bind_data_change_methods(widget)

    widget._on_data_changes(
        {
            "changed_entities": ["vitals"],
            "sync_actions": {"vitals_snapshot_required": True},
        }
    )

    assert requested_scopes == []
    assert status_calls == []

    widget._on_data_changes(
        {
            "changed_entities": ["patient_status_events"],
            "sync_actions": {
                "status_refresh": True,
                "vitals_snapshot_required": True,
            },
        }
    )

    assert requested_scopes == ["patient_open_vitals"]
    assert status_calls == []


def test_nurse_status_partial_refresh_only_schedules_background_events_snapshot() -> None:
    refresh_calls: list[bool] = []
    widget = SimpleNamespace(
        layout_manager=SimpleNamespace(
            refresh_current_status=lambda: (_ for _ in ()).throw(
                AssertionError("synchronous status refresh was called")
            ),
            sector_events=SimpleNamespace(
                refresh=lambda *, force: refresh_calls.append(force)
            ),
        )
    )

    NurseMainWidget._refresh_status_from_db(widget)

    assert refresh_calls == [True]


def test_nurse_stale_outcome_snapshot_does_not_suppress_needed_refresh() -> None:
    shift_date = datetime(2026, 10, 20, 8, 0)
    outcome = SimpleNamespace(status=SimpleNamespace(is_outcome=lambda: True))
    widget = SimpleNamespace(
        _card_snapshot_cache={
            "admission_id": 40,
            "shift_date": shift_date - timedelta(days=1),
            "status": outcome,
        },
        _card_snapshot_context=(40, shift_date - timedelta(days=1), 1),
        _current_date=shift_date,
        _patient_open_generation=2,
        remcard_service=SimpleNamespace(get_current_status=lambda _admission_id: None),
        layout_manager=SimpleNamespace(current_admission_id=41, _current_status_dto=outcome),
    )
    widget._current_status_is_outcome = NurseMainWidget._current_status_is_outcome.__get__(widget)

    assert widget._current_status_is_outcome() is False


def test_nurse_snapshot_from_previous_patient_or_date_is_not_applied() -> None:
    shift_date = datetime(2026, 10, 20, 8, 0)
    for request in (
        {
            "request_id": 8,
            "patient_open_generation": 3,
            "admission_id": 40,
            "shift_date": shift_date,
            "snapshot": {"status": "wrong patient"},
        },
        {
            "request_id": 8,
            "patient_open_generation": 3,
            "admission_id": 41,
            "shift_date": shift_date - timedelta(days=1),
            "snapshot": {"status": "wrong date"},
        },
    ):
        widget = SimpleNamespace(
            _is_closing=False,
            _snapshot_request_id=8,
            _patient_open_generation=3,
            _current_date=shift_date,
            _card_snapshot_cache={"sentinel": True},
            layout_manager=SimpleNamespace(current_admission_id=41),
        )

        NurseMainWidget._apply_card_snapshot(widget, request)

        assert widget._card_snapshot_cache == {"sentinel": True}


def test_nurse_snapshot_from_previous_open_generation_is_not_applied() -> None:
    shift_date = datetime(2026, 10, 20, 8, 0)
    widget = SimpleNamespace(
        _is_closing=False,
        _snapshot_request_id=8,
        _patient_open_generation=4,
        _current_date=shift_date,
        _card_snapshot_cache={"sentinel": True},
        layout_manager=SimpleNamespace(current_admission_id=41),
        _current_snapshot_context_key=lambda **_kwargs: "current-context",
        _balance_snapshot_sync=SimpleNamespace(
            merge_card_snapshot=lambda _snapshot: (_ for _ in ()).throw(
                AssertionError("snapshot from previous open generation was applied")
            )
        ),
    )
    request = {
        "request_id": 8,
        "patient_open_generation": 3,
        "admission_id": 41,
        "shift_date": shift_date,
        "context_key": "current-context",
        "snapshot": {"status": "previous open"},
    }

    NurseMainWidget._apply_card_snapshot(widget, request)

    assert widget._card_snapshot_cache == {"sentinel": True}
