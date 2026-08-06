import threading
import time

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
        assert crash_reports.dump_current_thread_stacks(reason="test_ui_hang")
        fault_file.seek(0)
        content = fault_file.read()

    assert "REMCARD_THREAD_DUMP reason=test_ui_hang" in content
    assert crash_reports._native_trace_event_type(content) == "ui_hang"
