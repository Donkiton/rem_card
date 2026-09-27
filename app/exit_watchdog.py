"""Process exit guard that remains active when the Qt event loop is blocked."""

from __future__ import annotations

import json
import math
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_DIAGNOSTIC_WAIT_SEC = 0.1


@dataclass(slots=True)
class _ArmSession:
    generation: int
    stage: str
    started_monotonic: float
    deadline_monotonic: float
    wake: threading.Event = field(default_factory=threading.Event)


class ExitWatchdog:
    """Force process termination if an armed shutdown does not finish in time."""

    def __init__(
        self,
        timeout_sec: float = 30.0,
        diagnostic_dir: str | os.PathLike[str] | None = None,
        terminate: Callable[[int], Any] | None = None,
    ) -> None:
        timeout = float(timeout_sec)
        if not math.isfinite(timeout) or timeout < 0.0:
            raise ValueError("timeout_sec must be a finite non-negative number")

        self.timeout_sec = timeout
        self.diagnostic_dir = Path(diagnostic_dir) if diagnostic_dir is not None else None
        self._terminate = terminate if terminate is not None else os._exit
        self._lock = threading.Lock()
        self._session: _ArmSession | None = None
        self._generation = 0
        self._termination_started = False

    @property
    def armed(self) -> bool:
        with self._lock:
            return self._session is not None and not self._termination_started

    def arm(self, stage: str = "shutdown") -> None:
        """Start one deadline; repeated calls leave the original deadline intact."""
        with self._lock:
            if self._session is not None or self._termination_started:
                return
            now = time.monotonic()
            self._generation += 1
            session = _ArmSession(
                generation=self._generation,
                stage=str(stage),
                started_monotonic=now,
                deadline_monotonic=now + self.timeout_sec,
            )
            self._session = session

        thread = threading.Thread(
            target=self._watch,
            args=(session,),
            name=f"RemCardExitWatchdog-{session.generation}",
            daemon=True,
        )
        try:
            thread.start()
        except BaseException:
            with self._lock:
                if self._session is session:
                    self._session = None
            session.wake.set()
            raise

    def set_stage(self, stage: str) -> None:
        with self._lock:
            if self._session is not None and not self._termination_started:
                self._session.stage = str(stage)

    def disarm(self) -> None:
        """Cancel the current generation without waiting for its daemon thread."""
        with self._lock:
            session = self._session
            self._session = None
        if session is not None:
            session.wake.set()

    def force(self) -> None:
        """Trigger termination immediately, even if the guard was not armed."""
        with self._lock:
            if self._termination_started:
                return
            session = self._session
            if session is None:
                now = time.monotonic()
                self._generation += 1
                session = _ArmSession(
                    generation=self._generation,
                    stage="forced",
                    started_monotonic=now,
                    deadline_monotonic=now,
                )
                self._session = session
        self._finish(session, reason="forced")

    def _watch(self, session: _ArmSession) -> None:
        while True:
            remaining = session.deadline_monotonic - time.monotonic()
            if remaining <= 0.0:
                break
            if session.wake.wait(remaining):
                return
        self._finish(session, reason="timeout")

    def _finish(self, session: _ArmSession, *, reason: str) -> None:
        with self._lock:
            if self._session is not session or self._termination_started:
                return
            self._termination_started = True
            self._session = None
            stage = session.stage
        session.wake.set()

        triggered = time.monotonic()
        payload = {
            "reason": reason,
            "pid": os.getpid(),
            "stage": stage,
            "timing": {
                "timeout_sec": self.timeout_sec,
                "armed_monotonic": session.started_monotonic,
                "deadline_monotonic": session.deadline_monotonic,
                "triggered_monotonic": triggered,
                "elapsed_sec": max(0.0, triggered - session.started_monotonic),
                "triggered_unix": time.time(),
            },
        }
        self._save_diagnostic_bounded(payload)
        self._terminate(2)

    def _save_diagnostic_bounded(self, payload: dict[str, Any]) -> None:
        if self.diagnostic_dir is None:
            return

        finished = threading.Event()

        def save() -> None:
            try:
                self._write_diagnostic(payload)
            except BaseException:
                pass
            finally:
                finished.set()

        writer = threading.Thread(
            target=save,
            name="RemCardExitWatchdogDiagnostic",
            daemon=True,
        )
        try:
            writer.start()
        except BaseException:
            return
        finished.wait(_DIAGNOSTIC_WAIT_SEC)

    def _write_diagnostic(self, payload: dict[str, Any]) -> None:
        diagnostic_dir = self.diagnostic_dir
        if diagnostic_dir is None:
            return
        diagnostic_dir.mkdir(parents=True, exist_ok=True)
        path = diagnostic_dir / (
            f"exit_watchdog_{payload['pid']}_{time.time_ns()}.json"
        )
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
