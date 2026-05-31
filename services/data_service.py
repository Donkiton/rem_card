from datetime import datetime
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal, Qt, Slot

from rem_card.app.db_access_classifier import classify_database_access, classify_database_access_error
from rem_card.app.logger import logger
from rem_card.app.sqlite_shared import LocalWriteQueue
from rem_card.app.runtime_outage import (
    RuntimeNetworkOutageWriteBlockedError,
    runtime_outage_transition_allowed,
)
from rem_card.services.data_update_monitor import DataUpdateMonitor
from rem_card.services.sync_coordinator import SyncCoordinator


class DataService(QObject):
    write_failed = Signal(str)
    write_finished = Signal(str)
    changes_detected = Signal(dict)
    network_outage_detected = Signal(dict)
    restore_probe_status = Signal(dict)
    _success_callback_requested = Signal(object, object)
    _error_callback_requested = Signal(object, object)

    def __init__(self, db_manager):
        super().__init__()
        self.db = db_manager
        self._queue = LocalWriteQueue(logger=logger)
        self._monitor = DataUpdateMonitor(self)
        self._sync_coordinator = SyncCoordinator()
        self._poll_maintenance_tasks: list[Callable[[], Any]] = []
        self._emergency_standby_scheduler = None
        self._emergency_restore_probe_scheduler = None
        self._shutting_down = False
        self._runtime_role = None
        self._network_outage_detected = False
        self._network_outage_info: dict[str, Any] = {}
        self._last_failure_category = ""
        self._unconfirmed_write_count = 0
        self._unknown_active_write = False
        self._outage_signal_emitted = False
        self._last_runtime_outage_shutdown_result = ""
        self._last_runtime_outage_queue_settled = None
        self._monitor.changes_detected.connect(self._emit_coordinated_changes, Qt.QueuedConnection)
        self._monitor.monitor_error.connect(self._handle_monitor_error, Qt.QueuedConnection)
        self._success_callback_requested.connect(self._dispatch_success_callback, Qt.QueuedConnection)
        self._error_callback_requested.connect(self._dispatch_error_callback, Qt.QueuedConnection)
        self._monitor.start()

    def set_runtime_role(self, role: str | None):
        self._runtime_role = str(role or "").strip().lower() or None

    def get_data_version(self) -> int:
        return self.db.get_data_version()

    def get_latest_change_id(self, admission_id: Optional[int] = None, include_global: bool = True) -> int:
        return self.db.get_latest_change_id(admission_id=admission_id, include_global=include_global)

    def get_observed_change_state(self) -> Optional[dict[str, Any]]:
        if not self._monitor:
            return None
        return self._monitor.get_change_state()

    def is_network_outage_detected(self) -> bool:
        return bool(self._network_outage_detected)

    def get_write_queue_state(self) -> dict[str, Any]:
        queue = self._queue
        pending_count = getattr(queue, "pending_count", lambda: 0)
        active_count = getattr(queue, "active_count", lambda: 0)
        accepting = getattr(queue, "is_accepting", lambda: True)
        return {
            "idle": bool(queue.is_idle()),
            "shutting_down": bool(self._shutting_down),
            "pending_count": int(pending_count()),
            "active_write_in_progress": int(active_count()) > 0,
            "active_count": int(active_count()),
            "accepting": bool(accepting()),
            "last_failure_category": str(self._last_failure_category or ""),
            "network_outage_detected": bool(self._network_outage_detected),
            "unconfirmed_write_count": int(self._unconfirmed_write_count),
            "unknown_active_write": bool(self._unknown_active_write),
            "queue_shutdown_result": str(self._last_runtime_outage_shutdown_result or ""),
            "queue_settled": self._last_runtime_outage_queue_settled,
        }

    def fetch_changes_since(self, last_change_id: int, admission_id: Optional[int] = None, include_global: bool = True):
        return self.db.fetch_changes_since(
            last_change_id=last_change_id,
            admission_id=admission_id,
            include_global=include_global,
        )

    def get_latest_settings_change_id(self) -> int:
        from rem_card.services.settings.settings_service import get_settings_service

        return get_settings_service().latest_change_id()

    def fetch_settings_changes_since(self, last_change_id: int) -> list[dict[str, Any]]:
        from rem_card.services.settings.settings_service import get_settings_service

        return get_settings_service().fetch_changes_since(last_change_id)

    def get_changed_entities_since(
        self,
        last_change_id: int,
        admission_id: Optional[int] = None,
        include_global: bool = True,
    ) -> set[str]:
        return self.db.get_changed_entities_since(
            last_change_id=last_change_id,
            admission_id=admission_id,
            include_global=include_global,
        )

    def add_poll_maintenance_task(self, task: Callable[[], Any]):
        if task not in self._poll_maintenance_tasks:
            self._poll_maintenance_tasks.append(task)

    def set_emergency_standby_scheduler(self, scheduler):
        self._emergency_standby_scheduler = scheduler
        if scheduler is None:
            return
        self.write_finished.connect(self._request_emergency_standby_after_write, Qt.QueuedConnection)
        self.changes_detected.connect(self._request_emergency_standby_after_changes, Qt.QueuedConnection)
        self.add_poll_maintenance_task(self._request_emergency_standby_on_idle)

    def set_emergency_restore_probe_scheduler(self, scheduler):
        self._emergency_restore_probe_scheduler = scheduler
        if scheduler is None:
            return
        self.write_finished.connect(self._request_emergency_restore_probe_after_write, Qt.QueuedConnection)

    def emit_restore_probe_status(self, payload: dict[str, Any]):
        if self._shutting_down:
            return
        self.restore_probe_status.emit(dict(payload or {}))

    def run_poll_maintenance_tasks(self):
        for task in list(self._poll_maintenance_tasks):
            try:
                task()
            except Exception as exc:
                logger.warning("DataService poll maintenance task failed: %s", exc, exc_info=True)

    def run_write(self, description: str, operation: Callable):
        if self._reject_write_if_outage(description):
            raise RuntimeNetworkOutageWriteBlockedError("Сетевая база недоступна; запись заблокирована до перезапуска.")
        try:
            result = self.db.run_write_operation(operation, source=description)
        except Exception as exc:
            self._handle_database_access_failure(exc, source=description, write_description=description)
            raise
        self.write_finished.emit(description)
        self.request_immediate_refresh(force_emit=True, source=description)
        return result

    def is_write_queue_idle(self) -> bool:
        return bool(self._queue.is_idle())

    def block_new_writes_for_runtime_outage(self, info: dict[str, Any] | None = None) -> None:
        if self._network_outage_detected:
            return
        self._network_outage_detected = True
        self._network_outage_info = dict(info or {})
        logger.warning("Runtime network outage detected; new writes are blocked: %s", self._network_outage_info)

    def _reject_write_if_outage(self, description: str) -> bool:
        if not self._network_outage_detected:
            return False
        exc = RuntimeNetworkOutageWriteBlockedError("Сетевая база недоступна; запись заблокирована до перезапуска.")
        logger.warning("Queued write rejected after runtime network outage for %s", description)
        self.write_failed.emit(f"{description}: {exc}")
        return True

    def enqueue_write(
        self,
        description: str,
        operation: Callable[[], Any],
        on_success: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ):
        if self._network_outage_detected:
            exc = RuntimeNetworkOutageWriteBlockedError("Сетевая база недоступна; запись заблокирована до перезапуска.")
            logger.warning("Queued write rejected after runtime network outage for %s", description)
            self.write_failed.emit(f"{description}: {exc}")
            if on_error:
                self._error_callback_requested.emit(on_error, exc)
            return False

        if self._shutting_down:
            exc = RuntimeError("Application is shutting down; queued write rejected")
            logger.info("Queued write rejected during shutdown for %s", description)
            self.write_failed.emit(f"{description}: {exc}")
            if on_error:
                try:
                    on_error(exc)
                except Exception as callback_exc:
                    logger.error("DataService shutdown rejection callback failed: %s", callback_exc, exc_info=True)
            return False

        def handle_success(result):
            if self._shutting_down:
                logger.info("Queued write success callbacks skipped during shutdown for %s", description)
                return
            if self._network_outage_detected:
                logger.info("Queued write success callbacks skipped after runtime outage for %s", description)
                return
            self.write_finished.emit(description)
            self.request_immediate_refresh(force_emit=True, source=description)
            self._success_callback_requested.emit(on_success, result)

        def handle_error(exc: Exception):
            logger.error("Queued write failed for %s: %s", description, exc)
            self._handle_database_access_failure(exc, source=description, write_description=description)
            self.write_failed.emit(f"{description}: {exc}")
            self._error_callback_requested.emit(on_error, exc)

        self._queue.submit(
            func=operation,
            description=description,
            on_success=handle_success,
            on_error=handle_error,
        )
        return True

    @Slot(object, object)
    def _dispatch_success_callback(self, callback: Optional[Callable[[Any], None]], result: Any):
        if self._shutting_down or not callback:
            return
        QTimer.singleShot(0, lambda cb=callback, res=result: self._run_success_callback(cb, res))

    def _run_success_callback(self, callback: Callable[[Any], None], result: Any):
        if self._shutting_down or not callback:
            return
        try:
            callback(result)
        except Exception as exc:
            logger.error("DataService success callback failed: %s", exc, exc_info=True)

    @Slot(object, object)
    def _dispatch_error_callback(self, callback: Optional[Callable[[Exception], None]], exc: Exception):
        if self._shutting_down or not callback:
            return
        QTimer.singleShot(0, lambda cb=callback, err=exc: self._run_error_callback(cb, err))

    def _run_error_callback(self, callback: Callable[[Exception], None], exc: Exception):
        if self._shutting_down or not callback:
            return
        try:
            callback(exc)
        except Exception as callback_exc:
            logger.error("DataService error callback failed: %s", callback_exc, exc_info=True)

    def set_shutting_down(self):
        self._shutting_down = True

    def prepare_runtime_outage_shutdown(self, timeout: float = 5.0) -> bool:
        logger.info("Runtime outage shutdown: stopping schedulers, monitor and write queue")
        self.set_shutting_down()
        schedulers_stopped = self._stop_emergency_schedulers(timeout=timeout)
        monitor_stopped = self.stop_data_update_monitor(timeout=timeout)
        queue_drained = self._queue.shutdown(timeout=timeout)
        if not queue_drained:
            self._unknown_active_write = True
            self._unconfirmed_write_count = max(1, int(self._unconfirmed_write_count or 0))
            logger.warning("Runtime outage shutdown continued with unconfirmed queued write state")
        self._last_runtime_outage_queue_settled = bool(queue_drained)
        if queue_drained and schedulers_stopped and monitor_stopped:
            self._last_runtime_outage_shutdown_result = "settled"
        elif not queue_drained:
            self._last_runtime_outage_shutdown_result = "timeout"
        else:
            self._last_runtime_outage_shutdown_result = "failed"
        try:
            from rem_card.app.local_metrics import flush_metrics

            flush_metrics(timeout=1.0)
        except Exception:
            pass
        return bool(schedulers_stopped and monitor_stopped and queue_drained)

    def stop_data_update_monitor(self, timeout: float = 5.0) -> bool:
        if not self._monitor or not self._monitor.isRunning():
            return True
        self._monitor.stop()
        return bool(self._monitor.wait(max(0, int(float(timeout or 0.0) * 1000))))

    def shutdown(self) -> bool:
        logger.info("DataService shutdown: stopping monitor and write queue")
        self.set_shutting_down()
        schedulers_stopped = self._stop_emergency_schedulers(timeout=5.0)
        monitor_stopped = True
        if self._monitor and self._monitor.isRunning():
            self._monitor.stop()
            monitor_stopped = bool(self._monitor.wait(5000))
            if not monitor_stopped:
                logger.warning("DataUpdateMonitor did not stop before DataService shutdown timeout")
        drained = self._queue.shutdown(timeout=5.0)
        try:
            from rem_card.app.local_metrics import flush_metrics

            flush_metrics(timeout=1.0)
        except Exception:
            pass
        logger.info(
            "DataService shutdown result schedulers_stopped=%s monitor_stopped=%s queue_drained=%s",
            schedulers_stopped,
            monitor_stopped,
            drained,
        )
        return bool(schedulers_stopped and monitor_stopped and drained)

    def request_immediate_refresh(self, *, force_emit: bool = False, source: str = ""):
        if self._monitor and not self._shutting_down and not self._network_outage_detected:
            self._monitor.request_refresh(force_emit=force_emit, source=source)

    @Slot(dict)
    def _emit_coordinated_changes(self, payload: dict):
        if self._shutting_down:
            return
        if any((change or {}).get("settings_change") for change in (payload or {}).get("changes", [])):
            try:
                from rem_card.services.settings.settings_service import get_settings_service

                get_settings_service().invalidate_cache()
            except Exception as exc:
                logger.warning("Не удалось инвалидировать кэш settings DB: %s", exc, exc_info=True)
        self.changes_detected.emit(self._sync_coordinator.classify(payload or {}))

    @Slot(str)
    def _request_emergency_standby_after_write(self, description: str):
        if self._shutting_down:
            return
        scheduler = getattr(self, "_emergency_standby_scheduler", None)
        if scheduler is not None:
            scheduler.request_refresh_after_write("after_write_commit")

    @Slot(dict)
    def _request_emergency_standby_after_changes(self, payload: dict):
        if self._shutting_down:
            return
        scheduler = getattr(self, "_emergency_standby_scheduler", None)
        if scheduler is None:
            return
        payload = payload or {}
        previous_change_id = int(payload.get("previous_change_id") or 0)
        last_change_id = int(payload.get("last_change_id") or 0)
        previous_settings_change_id = int(payload.get("previous_settings_change_id") or 0)
        last_settings_change_id = int(payload.get("last_settings_change_id") or 0)
        has_settings_change = any((change or {}).get("settings_change") for change in payload.get("changes", []))
        if has_settings_change or last_settings_change_id > previous_settings_change_id:
            scheduler.request_refresh("settings_changed")
        elif last_change_id > previous_change_id:
            scheduler.request_refresh("change_log_advanced")

    def _request_emergency_standby_on_idle(self):
        if self._shutting_down:
            return
        scheduler = getattr(self, "_emergency_standby_scheduler", None)
        if scheduler is not None:
            scheduler.request_refresh_on_idle("idle_periodic")
            self._check_standby_scheduler_outage_status(scheduler)

    @Slot(str)
    def _request_emergency_restore_probe_after_write(self, description: str):
        if self._shutting_down:
            return
        scheduler = getattr(self, "_emergency_restore_probe_scheduler", None)
        if scheduler is not None:
            scheduler.request_probe("after_local_write")

    def _stop_emergency_schedulers(self, *, timeout: float = 5.0) -> bool:
        stopped = True
        for attr, label in (
            ("_emergency_standby_scheduler", "Emergency standby scheduler"),
            ("_emergency_restore_probe_scheduler", "Emergency restore probe scheduler"),
        ):
            scheduler = getattr(self, attr, None)
            if scheduler is None:
                continue
            try:
                stopped = bool(scheduler.stop(timeout=timeout)) and stopped
            except Exception as exc:
                stopped = False
                logger.warning("%s shutdown failed: %s", label, exc, exc_info=True)
        return stopped

    @Slot(str)
    def _handle_monitor_error(self, message: str):
        self._handle_database_access_failure(RuntimeError(str(message or "")), source="data_update_monitor")

    def _handle_database_access_failure(
        self,
        exc: Exception,
        *,
        source: str,
        write_description: str | None = None,
    ) -> str:
        classification = classify_database_access(exc)
        category = classification.category
        self._last_failure_category = category
        if write_description and runtime_outage_transition_allowed(category):
            self._unconfirmed_write_count += 1
        if not runtime_outage_transition_allowed(category):
            return category

        active_count = int(getattr(self._queue, "active_count", lambda: 0)())
        pending_count = int(getattr(self._queue, "pending_count", lambda: 0)())
        info = {
            "category": category,
            "reason": classification.reason,
            "source": str(source or ""),
            "role": str(self._runtime_role or ""),
            "detected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "write_description": str(write_description or ""),
            "pending_count": pending_count,
            "active_write_in_progress": active_count > 0,
            "unconfirmed_write_count": int(self._unconfirmed_write_count),
        }
        if active_count > 0:
            self._unknown_active_write = True
        self.block_new_writes_for_runtime_outage(info)
        if not self._outage_signal_emitted:
            self._outage_signal_emitted = True
            self.network_outage_detected.emit(dict(info))
        return category

    def _check_standby_scheduler_outage_status(self, scheduler) -> None:
        try:
            status = scheduler.get_status()
        except Exception:
            return
        if int(status.get("consecutive_failures") or 0) < 3:
            return
        text = f"{status.get('last_status', '')} {status.get('last_reason', '')} {status.get('last_error', '')}"
        category = classify_database_access_error(RuntimeError(text))
        if runtime_outage_transition_allowed(category):
            self._handle_database_access_failure(RuntimeError(text), source="emergency_standby_scheduler")
