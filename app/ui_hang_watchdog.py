from __future__ import annotations

import threading
import time
from collections.abc import Callable

from rem_card.app.local_metrics import record_metric
from rem_card.app.logger import logger


class UIHeartbeatWatchdog:
    """Watch a UI heartbeat from a thread independent of the Qt event loop."""

    def __init__(
        self,
        *,
        threshold_sec: float,
        cooldown_sec: float,
        dump_callback: Callable[[float], bool | None],
        poll_interval_sec: float = 0.25,
    ):
        self.threshold_sec = max(0.1, float(threshold_sec))
        self.cooldown_sec = max(self.threshold_sec, float(cooldown_sec))
        self.poll_interval_sec = max(
            0.02,
            min(float(poll_interval_sec), self.threshold_sec / 2.0),
        )
        self._dump_callback = dump_callback
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_heartbeat = time.perf_counter()
        self._last_dump = 0.0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self.beat()
        self._thread = threading.Thread(
            target=self._run,
            name="RemCardUIHeartbeatWatchdog",
            daemon=True,
        )
        self._thread.start()

    def beat(self, now: float | None = None) -> None:
        with self._lock:
            self._last_heartbeat = float(now if now is not None else time.perf_counter())

    def stop(self, *, timeout_sec: float = 0.5) -> None:
        self._stop_evt.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout_sec)))

    def _run(self) -> None:
        while not self._stop_evt.wait(self.poll_interval_sec):
            now = time.perf_counter()
            with self._lock:
                stalled_sec = max(0.0, now - self._last_heartbeat)
                since_dump_sec = max(0.0, now - self._last_dump)
                should_dump = (
                    stalled_sec >= self.threshold_sec
                    and (
                        self._last_dump <= 0.0
                        or since_dump_sec >= self.cooldown_sec
                    )
                )
                if should_dump:
                    self._last_dump = now
            if not should_dump:
                continue
            try:
                dumped = self._dump_callback(stalled_sec)
                metric_name = (
                    "ui_hard_hang_stack_dump"
                    if dumped is not False
                    else "ui_hard_hang_stack_dump_unavailable"
                )
                record_metric(
                    metric_name,
                    round(stalled_sec * 1000.0, 3),
                    force_flush=True,
                    threshold_ms=round(self.threshold_sec * 1000.0, 3),
                    source="ui_heartbeat_watchdog",
                )
                logger.error(
                    "[UIWatchdog] hard_hang_stack_dump stalled_ms=%.1f threshold_ms=%.1f dumped=%s",
                    stalled_sec * 1000.0,
                    self.threshold_sec * 1000.0,
                    int(dumped is not False),
                )
            except Exception:
                logger.exception("UI heartbeat watchdog could not dump thread stacks")
