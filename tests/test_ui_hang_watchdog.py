import threading
import time

import pytest

from rem_card.app.ui_hang_watchdog import UIHeartbeatWatchdog
from rem_card.services import crash_reports


def test_watchdog_dumps_when_ui_heartbeat_stops():
    dumped = threading.Event()
    observed = []
    watchdog = UIHeartbeatWatchdog(
        threshold_sec=0.1,
        cooldown_sec=1.0,
        poll_interval_sec=0.02,
        dump_callback=lambda stalled: (observed.append(stalled), dumped.set()),
    )
    try:
        watchdog.start()
        assert dumped.wait(1.0)
    finally:
        watchdog.stop()

    assert observed[0] >= 0.1


def test_watchdog_does_not_dump_while_heartbeat_advances():
    dumped = threading.Event()
    watchdog = UIHeartbeatWatchdog(
        threshold_sec=0.12,
        cooldown_sec=1.0,
        poll_interval_sec=0.02,
        dump_callback=lambda _stalled: dumped.set(),
    )
    try:
        watchdog.start()
        deadline = time.monotonic() + 0.35
        while time.monotonic() < deadline:
            watchdog.beat()
            time.sleep(0.03)
        assert not dumped.is_set()
    finally:
        watchdog.stop()


def test_thread_dump_is_marked_as_ui_hang(monkeypatch, tmp_path):
    native_path = tmp_path / "ui-hang.log"
    with native_path.open("w+", encoding="utf-8") as fault_file:
        monkeypatch.setattr(crash_reports, "_FAULT_FILE", fault_file)
        outcome = crash_reports.dump_current_thread_stacks(reason="test_ui_hang")
        fault_file.seek(0)
        content = fault_file.read()

    assert outcome == crash_reports.THREAD_DUMP_CAPTURED
    assert "REMCARD_THREAD_DUMP reason=test_ui_hang" in content
    assert crash_reports._native_trace_event_type(content) == "ui_hang"


def test_thread_dump_reports_suppression_after_signature_cap(monkeypatch, tmp_path):
    native_path = tmp_path / "ui-hang.log"
    with native_path.open("w+", encoding="utf-8") as fault_file:
        monkeypatch.setattr(crash_reports, "_FAULT_FILE", fault_file)
        monkeypatch.setattr(crash_reports, "_ui_hang_stack_signature", lambda: "same-stack")
        monkeypatch.setattr(crash_reports.faulthandler, "dump_traceback", lambda **_kwargs: None)
        crash_reports._reset_ui_hang_state_locked()
        try:
            first = crash_reports.dump_current_thread_stacks()
            second = crash_reports.dump_current_thread_stacks()
            third = crash_reports.dump_current_thread_stacks()
        finally:
            crash_reports._reset_ui_hang_state_locked()

    assert first == crash_reports.THREAD_DUMP_CAPTURED
    assert second == crash_reports.THREAD_DUMP_CAPTURED
    assert third == crash_reports.THREAD_DUMP_SUPPRESSED


def test_watchdog_records_captured_and_suppressed_outcomes(monkeypatch):
    metrics = []
    monkeypatch.setattr(
        "rem_card.app.ui_hang_watchdog.record_metric",
        lambda name, value, **fields: metrics.append((name, value, fields)),
    )
    outcomes = iter([crash_reports.THREAD_DUMP_CAPTURED, crash_reports.THREAD_DUMP_SUPPRESSED])
    watchdog = UIHeartbeatWatchdog(
        threshold_sec=0.1,
        cooldown_sec=0.1,
        poll_interval_sec=0.02,
        resume_gap_sec=10.0,
        dump_callback=lambda _stalled: next(outcomes),
    )
    watchdog._last_heartbeat = 1.0
    watchdog._last_poll = 1.0

    assert watchdog._poll_once(1.11) == "captured"
    assert watchdog._poll_once(1.22) == "suppressed"
    assert [item[2]["outcome"] for item in metrics] == ["captured", "suppressed"]
    assert metrics[0][2]["attempted"] == 1
    assert metrics[0][2]["captured"] == 1 and metrics[0][2]["suppressed"] == 0
    assert metrics[1][2]["captured"] == 0 and metrics[1][2]["suppressed"] == 1


def test_watchdog_does_not_treat_ambiguous_none_as_captured():
    assert UIHeartbeatWatchdog._dump_outcome(None) == "unavailable"


def test_watchdog_treats_long_poll_gap_as_resume_but_detects_continuous_gui_block(monkeypatch):
    metrics = []
    monkeypatch.setattr(
        "rem_card.app.ui_hang_watchdog.record_metric",
        lambda name, value, **fields: metrics.append((name, value, fields)),
    )
    observed = []
    watchdog = UIHeartbeatWatchdog(
        threshold_sec=0.1,
        cooldown_sec=1.0,
        poll_interval_sec=0.02,
        resume_gap_sec=1.0,
        dump_callback=lambda stalled: (observed.append(stalled), "captured")[1],
    )
    watchdog._last_heartbeat = 10.0
    watchdog._last_poll = 10.0

    assert watchdog._poll_once(10.0 + 20 * 60 * 60) is None
    assert observed == []
    assert metrics == [
        (
            "ui_watchdog_resume_gap_ms",
            pytest.approx(20 * 60 * 60 * 1000.0),
            {
                "force_flush": True,
                "source": "ui_heartbeat_watchdog",
                "reason": "watchdog_poll_gap_inference",
                "resume_threshold_ms": 1000.0,
            },
        )
    ]

    resumed_at = 10.0 + 20 * 60 * 60
    assert watchdog._poll_once(resumed_at + 0.04) is None
    assert watchdog._poll_once(resumed_at + 0.08) is None
    assert watchdog._poll_once(resumed_at + 0.12) == "captured"
    assert observed == [pytest.approx(0.12)]


def test_gui_watchdog_poll_suppresses_resume_gap_pause(monkeypatch):
    from types import SimpleNamespace
    from rem_card.ui import main_window

    beats = []
    hard_watchdog = SimpleNamespace(
        beat=lambda now, **kwargs: (beats.append((now, kwargs)), 20 * 60 * 60)[1],
    )
    window = SimpleNamespace(
        _hard_ui_watchdog=hard_watchdog,
        _event_loop_watchdog_last_ts=10.0,
    )
    monkeypatch.setattr(main_window.time, "perf_counter", lambda: 10.0 + 20 * 60 * 60)
    monkeypatch.setattr(
        main_window,
        "record_metric",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("pause metric must be suppressed")),
    )

    main_window.MainWindow._poll_event_loop_watchdog(window, 250)

    assert window._event_loop_watchdog_last_ts == 10.0 + 20 * 60 * 60
    assert beats[0][1]["observed_gap_sec"] == pytest.approx(20 * 60 * 60)


def test_gui_gap_remains_a_hang_when_watchdog_thread_kept_polling(monkeypatch):
    metrics = []
    monkeypatch.setattr(
        "rem_card.app.ui_hang_watchdog.record_metric",
        lambda name, value, **fields: metrics.append((name, value, fields)),
    )
    watchdog = UIHeartbeatWatchdog(
        threshold_sec=0.1,
        cooldown_sec=1.0,
        poll_interval_sec=0.02,
        resume_gap_sec=1.0,
        dump_callback=lambda _stalled: "captured",
    )
    watchdog._last_poll = 100.09
    watchdog._last_heartbeat = 10.0

    assert watchdog.beat(100.1, observed_gap_sec=90.1) == 0.0
    assert metrics == []


def test_disabled_gui_watchdog_creates_no_timer_or_thread(monkeypatch):
    from types import SimpleNamespace
    from rem_card.ui import main_window

    window = SimpleNamespace(_event_loop_watchdog_timer=None)
    monkeypatch.setenv("REMCARD_UI_WATCHDOG_ENABLED", "0")

    main_window.MainWindow._start_event_loop_watchdog(window)

    assert not hasattr(window, "_hard_ui_watchdog")
    assert window._event_loop_watchdog_timer is None
