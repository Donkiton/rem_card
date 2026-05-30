from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Callable

from rem_card.app.emergency_metadata import read_json_file
from rem_card.app.emergency_paths import active_dir, active_session_metadata_path
from rem_card.app.emergency_standby import EmergencyStandbyManager, EmergencyStandbyRefreshResult
from rem_card.app.foreground_activity import should_defer_background_io
from rem_card.app.local_metrics import record_metric
from rem_card.app.logger import logger


DEFAULT_STANDBY_REFRESH_COOLDOWN_SEC = max(
    0.0,
    float(os.environ.get("REMCARD_EMERGENCY_STANDBY_COOLDOWN_SEC", "60")),
)
DEFAULT_STANDBY_REFRESH_FAILURE_BACKOFF_SEC = max(
    1.0,
    float(os.environ.get("REMCARD_EMERGENCY_STANDBY_FAILURE_BACKOFF_SEC", "30")),
)
DEFAULT_STANDBY_REFRESH_MAX_BACKOFF_SEC = max(
    DEFAULT_STANDBY_REFRESH_FAILURE_BACKOFF_SEC,
    float(os.environ.get("REMCARD_EMERGENCY_STANDBY_MAX_BACKOFF_SEC", "600")),
)
DEFAULT_STANDBY_REFRESH_FOREGROUND_IDLE_SEC = max(
    0.0,
    float(os.environ.get("REMCARD_EMERGENCY_STANDBY_FOREGROUND_IDLE_SEC", "5")),
)


@dataclass(frozen=True)
class EmergencyStandbySchedulerStatus:
    enabled: bool
    started: bool
    running: bool
    pending: bool
    last_status: str
    last_reason: str
    last_error: str
    last_success_ts: float
    last_attempt_ts: float
    next_allowed_ts: float
    consecutive_failures: int
    request_count: int
    coalesced_count: int
    refresh_count: int


class EmergencyStandbyScheduler:
    def __init__(
        self,
        *,
        role: str,
        mode: str,
        manager: EmergencyStandbyManager | None = None,
        is_write_queue_idle: Callable[[], bool] | None = None,
        is_foreground_busy: Callable[[], bool] | None = None,
        is_shutdown: Callable[[], bool] | None = None,
        is_safe_to_refresh: Callable[[], bool] | None = None,
        cooldown_sec: float = DEFAULT_STANDBY_REFRESH_COOLDOWN_SEC,
        failure_backoff_sec: float = DEFAULT_STANDBY_REFRESH_FAILURE_BACKOFF_SEC,
        max_backoff_sec: float = DEFAULT_STANDBY_REFRESH_MAX_BACKOFF_SEC,
    ):
        self.role = str(role or "").strip().lower()
        self.mode = str(mode or "").strip().lower()
        self.manager = manager or EmergencyStandbyManager()
        self.is_write_queue_idle = is_write_queue_idle or (lambda: True)
        self.is_foreground_busy = is_foreground_busy or self._default_foreground_busy
        self.is_shutdown = is_shutdown or (lambda: False)
        self.is_safe_to_refresh = is_safe_to_refresh or (lambda: True)
        self.cooldown_sec = max(0.0, float(cooldown_sec or 0.0))
        self.failure_backoff_sec = max(0.0, float(failure_backoff_sec or 0.0))
        self.max_backoff_sec = max(self.failure_backoff_sec, float(max_backoff_sec or 0.0))

        self._lock = threading.Lock()
        self._started = False
        self._shutdown = False
        self._running = False
        self._pending_reason = ""
        self._pending_forced = False
        self._worker_thread: threading.Thread | None = None
        self._last_status = "idle"
        self._last_reason = ""
        self._last_error = ""
        self._last_success_ts = 0.0
        self._last_attempt_ts = 0.0
        self._next_allowed_ts = 0.0
        self._consecutive_failures = 0
        self._request_count = 0
        self._coalesced_count = 0
        self._refresh_count = 0

    @staticmethod
    def is_enabled_for_runtime(role: str | None, mode: str | None) -> bool:
        return str(role or "").strip().lower() == "nurse" and str(mode or "").strip().lower() == "network"

    @property
    def enabled(self) -> bool:
        return self.is_enabled_for_runtime(self.role, self.mode)

    def start(self) -> bool:
        if not self.enabled:
            with self._lock:
                self._last_status = "disabled"
                self._last_reason = f"role={self.role or '-'} mode={self.mode or '-'}"
            return False
        with self._lock:
            self._shutdown = False
            self._started = True
            if self._last_status == "disabled":
                self._last_status = "idle"
                self._last_reason = ""
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        with self._lock:
            self._shutdown = True
            self._started = False
            self._pending_reason = ""
            thread = self._worker_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout or 0.0)))
        with self._lock:
            stopped = not self._running
            self._last_status = "stopped" if stopped else "stopping"
            return stopped

    def request_refresh(self, reason: str = "manual", *, forced: bool = False) -> bool:
        reason = str(reason or "manual")
        with self._lock:
            self._request_count += 1
            if not self.enabled:
                self._last_status = "disabled"
                self._last_reason = reason
                return False
            if self._shutdown or not self._started:
                self._last_status = "stopped"
                self._last_reason = reason
                return False
            if self._running or self._pending_reason:
                self._coalesced_count += 1
            self._pending_reason = reason
            self._pending_forced = self._pending_forced or bool(forced)
        return self._maybe_start_worker()

    def request_refresh_after_write(self, reason: str = "after_write_commit") -> bool:
        return self.request_refresh(reason or "after_write_commit")

    def request_refresh_on_idle(self, reason: str = "idle_periodic") -> bool:
        return self.request_refresh(reason or "idle_periodic")

    def is_refresh_running(self) -> bool:
        with self._lock:
            return bool(self._running)

    def get_status(self) -> dict:
        status = self._snapshot_status()
        return {
            "enabled": status.enabled,
            "started": status.started,
            "running": status.running,
            "pending": status.pending,
            "last_status": status.last_status,
            "last_reason": status.last_reason,
            "last_error": status.last_error,
            "last_success_ts": status.last_success_ts,
            "last_attempt_ts": status.last_attempt_ts,
            "next_allowed_ts": status.next_allowed_ts,
            "consecutive_failures": status.consecutive_failures,
            "request_count": status.request_count,
            "coalesced_count": status.coalesced_count,
            "refresh_count": status.refresh_count,
        }

    def _snapshot_status(self) -> EmergencyStandbySchedulerStatus:
        with self._lock:
            return EmergencyStandbySchedulerStatus(
                enabled=self.enabled,
                started=bool(self._started),
                running=bool(self._running),
                pending=bool(self._pending_reason),
                last_status=str(self._last_status),
                last_reason=str(self._last_reason),
                last_error=str(self._last_error),
                last_success_ts=float(self._last_success_ts),
                last_attempt_ts=float(self._last_attempt_ts),
                next_allowed_ts=float(self._next_allowed_ts),
                consecutive_failures=int(self._consecutive_failures),
                request_count=int(self._request_count),
                coalesced_count=int(self._coalesced_count),
                refresh_count=int(self._refresh_count),
            )

    def _maybe_start_worker(self) -> bool:
        now = time.monotonic()
        with self._lock:
            if not self._started or self._shutdown or self._running or not self._pending_reason:
                return False
            if now < self._next_allowed_ts:
                self._last_status = "deferred"
                self._last_reason = "cooldown"
                return False

        block_reason = self._refresh_block_reason()
        if block_reason:
            with self._lock:
                self._last_status = "deferred"
                self._last_reason = block_reason
            record_metric("emergency_standby_refresh_deferred", 1, reason=block_reason)
            return False

        with self._lock:
            if not self._started or self._shutdown or self._running or not self._pending_reason:
                return False
            reason = self._pending_reason
            forced = bool(self._pending_forced)
            self._pending_reason = ""
            self._pending_forced = False
            self._running = True
            self._last_attempt_ts = time.time()
            self._last_status = "running"
            self._last_reason = reason
            worker = threading.Thread(
                target=self._worker,
                args=(reason, forced),
                name="EmergencyStandbyRefresh",
                daemon=True,
            )
            self._worker_thread = worker
        worker.start()
        return True

    def _worker(self, reason: str, forced: bool) -> None:
        started = time.perf_counter()
        result: EmergencyStandbyRefreshResult | None = None
        status = "error"
        error = ""
        try:
            result = self._refresh_if_needed(reason=reason, forced=forced)
            status = result.status
            if result.ok and result.status == "valid":
                self._record_success(reason, result, started)
            elif result.status in {"current", "deferred"}:
                self._record_skip(reason, result)
            else:
                self._record_failure(reason, result, started)
        except Exception as exc:
            error = str(exc)
            logger.warning("Emergency standby scheduler refresh failed: %s", exc, exc_info=True)
            record_metric(
                "emergency_standby_refresh_failed",
                1,
                reason=reason,
                status="exception",
                exception_class=exc.__class__.__name__,
                error=error,
            )
            status = "error"
        finally:
            with self._lock:
                self._running = False
                self._last_status = status
                self._last_reason = reason
                if error:
                    self._last_error = error
                    self._consecutive_failures += 1
                    self._next_allowed_ts = time.monotonic() + self._failure_delay_locked()
                elif result is not None and result.ok and result.status == "valid":
                    self._last_error = ""
                    self._consecutive_failures = 0
                    self._last_success_ts = time.time()
                    self._refresh_count += 1
                    self._next_allowed_ts = time.monotonic() + self.cooldown_sec
                elif result is not None and result.status == "current":
                    self._last_error = ""
                    self._next_allowed_ts = time.monotonic() + self.cooldown_sec
                elif result is not None and result.status == "deferred":
                    self._last_error = result.reason
                    self._next_allowed_ts = time.monotonic() + min(5.0, max(0.5, self.cooldown_sec))
                else:
                    self._last_error = "" if result is None else result.reason
                    self._consecutive_failures += 1
                    self._next_allowed_ts = time.monotonic() + self._failure_delay_locked()

    def _refresh_if_needed(self, *, reason: str, forced: bool) -> EmergencyStandbyRefreshResult:
        block_reason = self._refresh_block_reason()
        if block_reason:
            return EmergencyStandbyRefreshResult(ok=False, status="deferred", reason=block_reason)

        source_status = self.manager.check_network_sources()
        if not source_status.ok:
            return source_status

        remote_last_change_id = int(source_status.medical_validation.last_change_id if source_status.medical_validation else 0)
        settings_fingerprint = (
            None
            if source_status.settings_validation is None
            else dict(source_status.settings_validation.fingerprint)
        )
        source_schema_version = source_status.medical_validation.schema_version if source_status.medical_validation else None
        if not self.manager.should_refresh_standby(
            remote_last_change_id,
            settings_fingerprint=settings_fingerprint,
            source_schema_version=source_schema_version,
            forced=forced,
        ):
            return EmergencyStandbyRefreshResult(
                ok=True,
                status="current",
                reason="standby is already current",
                metadata=self.manager.store.get_latest_valid_standby(),
                medical_validation=source_status.medical_validation,
                settings_validation=source_status.settings_validation,
            )
        return self.manager.create_or_refresh_standby(forced=forced)

    def _refresh_block_reason(self) -> str:
        if self._shutdown or bool(self.is_shutdown()):
            return "shutdown"
        if self._has_active_emergency_session():
            return "active_emergency_session"
        try:
            if not bool(self.is_write_queue_idle()):
                return "write_queue_busy"
        except Exception as exc:
            logger.debug("Emergency standby write queue probe failed: %s", exc)
            return "write_queue_probe_error"
        try:
            if bool(self.is_foreground_busy()):
                return "foreground_busy"
        except Exception as exc:
            logger.debug("Emergency standby foreground probe failed: %s", exc)
            return "foreground_probe_error"
        try:
            if not bool(self.is_safe_to_refresh()):
                return "unsafe"
        except Exception as exc:
            logger.debug("Emergency standby safety probe failed: %s", exc)
            return "safety_probe_error"
        return ""

    def _has_active_emergency_session(self) -> bool:
        root = getattr(self.manager, "root", None)
        if not root:
            return False
        directory = active_dir(root)
        if not os.path.isdir(directory):
            return False
        try:
            for name in os.listdir(directory):
                session_path = active_session_metadata_path(root, name)
                if not os.path.isfile(session_path):
                    continue
                try:
                    payload = read_json_file(session_path)
                    if str(payload.get("status") or "") in {"active", "merge_failed", "merge_pending", "merging"}:
                        return True
                except Exception:
                    return True
        except OSError:
            return True
        return False

    @staticmethod
    def _default_foreground_busy() -> bool:
        should_defer, _reason, _age_sec = should_defer_background_io(
            idle_window_sec=DEFAULT_STANDBY_REFRESH_FOREGROUND_IDLE_SEC,
            names={"orders", "orders_show"},
        )
        return bool(should_defer)

    def _failure_delay_locked(self) -> float:
        if self.failure_backoff_sec <= 0:
            return 0.0
        exponent = max(0, int(self._consecutive_failures) - 1)
        return min(self.max_backoff_sec, self.failure_backoff_sec * (2 ** exponent))

    def _record_success(
        self,
        reason: str,
        result: EmergencyStandbyRefreshResult,
        started: float,
    ) -> None:
        metadata = result.metadata
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        record_metric(
            "emergency_standby_refresh_success",
            1,
            reason=reason,
            remote_last_change_id=None if metadata is None else metadata.remote_last_change_id,
            standby_last_change_id=None if metadata is None else metadata.remote_last_change_id,
            duration_ms=round(elapsed_ms, 3),
            medical_db_size=None if metadata is None else metadata.medical_db_size,
            settings_db_size=None if metadata is None else metadata.settings_db_size,
        )
        logger.info(
            "Emergency standby refresh success reason=%s status=%s remote_last_change_id=%s duration_ms=%.1f",
            reason,
            result.status,
            None if metadata is None else metadata.remote_last_change_id,
            elapsed_ms,
        )

    def _record_skip(self, reason: str, result: EmergencyStandbyRefreshResult) -> None:
        record_metric(
            "emergency_standby_refresh_skipped",
            1,
            reason=reason,
            status=result.status,
            detail=result.reason,
        )

    def _record_failure(
        self,
        reason: str,
        result: EmergencyStandbyRefreshResult,
        started: float,
    ) -> None:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        record_metric(
            "emergency_standby_refresh_failed",
            1,
            reason=reason,
            status=result.status,
            detail=result.reason,
            exception_class="",
            source_health_status=result.status,
            duration_ms=round(elapsed_ms, 3),
        )
        logger.info(
            "Emergency standby refresh did not complete reason=%s status=%s detail=%s duration_ms=%.1f",
            reason,
            result.status,
            result.reason,
            elapsed_ms,
        )


def create_emergency_standby_scheduler_for_runtime(
    *,
    role: str | None,
    mode: str | None,
    is_write_queue_idle: Callable[[], bool] | None = None,
    is_shutdown: Callable[[], bool] | None = None,
    is_foreground_busy: Callable[[], bool] | None = None,
    is_safe_to_refresh: Callable[[], bool] | None = None,
    manager: EmergencyStandbyManager | None = None,
) -> EmergencyStandbyScheduler | None:
    if not EmergencyStandbyScheduler.is_enabled_for_runtime(role, mode):
        return None
    return EmergencyStandbyScheduler(
        role=str(role or ""),
        mode=str(mode or ""),
        manager=manager,
        is_write_queue_idle=is_write_queue_idle,
        is_shutdown=is_shutdown,
        is_foreground_busy=is_foreground_busy,
        is_safe_to_refresh=is_safe_to_refresh,
    )
