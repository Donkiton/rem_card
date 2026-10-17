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
        dump_callback: Callable[[float], object],
        poll_interval_sec: float = 0.25,
        resume_gap_sec: float | None = None,
    ):
        self.threshold_sec = max(0.1, float(threshold_sec))
        self.cooldown_sec = max(self.threshold_sec, float(cooldown_sec))
        self.poll_interval_sec = max(
            0.02,
            min(float(poll_interval_sec), self.threshold_sec / 2.0),
        )
        self.resume_gap_sec = max(
            self.threshold_sec,
            float(
                resume_gap_sec
                if resume_gap_sec is not None
                else max(60.0, self.threshold_sec * 4.0, self.poll_interval_sec * 20.0)
            ),
        )
        self._dump_callback = dump_callback
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_heartbeat = time.perf_counter()
        self._last_dump = 0.0
        self._last_poll = self._last_heartbeat
        self._pending_resume_gap_sec = 0.0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        now = time.perf_counter()
        with self._lock:
            self._last_heartbeat = now
            self._last_poll = now
            self._pending_resume_gap_sec = 0.0
        self._thread = threading.Thread(
            target=self._run,
            name="RemCardUIHeartbeatWatchdog",
            daemon=True,
        )
        self._thread.start()

    def beat(
        self,
        now: float | None = None,
        *,
        observed_gap_sec: float | None = None,
    ) -> float:
        current = float(now if now is not None else time.perf_counter())
        resume_gap_to_record = 0.0
        with self._lock:
            watchdog_poll_gap = max(0.0, current - self._last_poll)
            observed_gap = max(0.0, float(observed_gap_sec or 0.0))
            if observed_gap >= self.resume_gap_sec and watchdog_poll_gap >= self.resume_gap_sec:
                self._last_poll = current
                self._pending_resume_gap_sec = max(self._pending_resume_gap_sec, observed_gap)
                resume_gap_to_record = observed_gap
            self._last_heartbeat = current
            resume_gap = self._pending_resume_gap_sec
            self._pending_resume_gap_sec = 0.0
        if resume_gap_to_record:
            self._record_resume_gap(resume_gap_to_record)
        return resume_gap

    def stop(self, *, timeout_sec: float = 0.5) -> None:
        self._stop_evt.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout_sec)))

    def _run(self) -> None:
        while not self._stop_evt.wait(self.poll_interval_sec):
            self._poll_once(time.perf_counter())

    @staticmethod
    def _dump_outcome(result: object) -> str:
        if result == "suppressed":
            return "suppressed"
        if result == "captured" or result is True:
            return "captured"
        return "unavailable"

    def _record_resume_gap(self, gap_sec: float) -> None:
        try:
            record_metric(
                "ui_watchdog_resume_gap_ms",
                round(max(0.0, gap_sec) * 1000.0, 3),
                force_flush=True,
                source="ui_heartbeat_watchdog",
                reason="watchdog_poll_gap_inference",
                resume_threshold_ms=round(self.resume_gap_sec * 1000.0, 3),
            )
            logger.info(
                "[UIWatchdog] resume_gap_ms=%.1f reason=watchdog_poll_gap_inference",
                gap_sec * 1000.0,
            )
        except Exception:
            logger.exception("UI heartbeat watchdog could not record resume gap")

    def _poll_once(self, now: float) -> str | None:
        """Evaluate one sample; a long watchdog-thread gap is a resume, not a UI hang."""
        resume_gap = 0.0
        with self._lock:
            poll_gap_sec = max(0.0, float(now) - self._last_poll)
            self._last_poll = float(now)
            if poll_gap_sec >= self.resume_gap_sec:
                self._last_heartbeat = float(now)
                self._pending_resume_gap_sec = max(self._pending_resume_gap_sec, poll_gap_sec)
                resume_gap = poll_gap_sec
                should_dump = False
                stalled_sec = 0.0
            else:
                stalled_sec = max(0.0, float(now) - self._last_heartbeat)
                since_dump_sec = max(0.0, float(now) - self._last_dump)
                should_dump = (
                    stalled_sec >= self.threshold_sec
                    and (self._last_dump <= 0.0 or since_dump_sec >= self.cooldown_sec)
                )
                if should_dump:
                    self._last_dump = float(now)
        if resume_gap:
            self._record_resume_gap(resume_gap)
            return None
        if not should_dump:
            return None
        try:
            outcome = self._dump_outcome(self._dump_callback(stalled_sec))
        except Exception:
            outcome = "unavailable"
            logger.exception("UI heartbeat watchdog could not dump thread stacks")
        try:
            record_metric(
                "ui_hard_hang_stack_dump",
                round(stalled_sec * 1000.0, 3),
                force_flush=True,
                threshold_ms=round(self.threshold_sec * 1000.0, 3),
                source="ui_heartbeat_watchdog",
                outcome=outcome,
                attempted=1,
                captured=int(outcome == "captured"),
                suppressed=int(outcome == "suppressed"),
            )
            logger.error(
                "[UIWatchdog] hard_hang_stack_dump stalled_ms=%.1f threshold_ms=%.1f outcome=%s",
                stalled_sec * 1000.0,
                self.threshold_sec * 1000.0,
                outcome,
            )
            return outcome
        except Exception:
            logger.exception("UI heartbeat watchdog could not record dump outcome")
            return outcome
