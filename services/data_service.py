from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Signal, Qt, Slot

from rem_card.app.logger import logger
from rem_card.app.sqlite_shared import LocalWriteQueue
from rem_card.services.data_update_monitor import DataUpdateMonitor
from rem_card.services.sync_coordinator import SyncCoordinator


class DataService(QObject):
    write_failed = Signal(str)
    write_finished = Signal(str)
    changes_detected = Signal(dict)
    _success_callback_requested = Signal(object, object)
    _error_callback_requested = Signal(object, object)

    def __init__(self, db_manager):
        super().__init__()
        self.db = db_manager
        self._queue = LocalWriteQueue(logger=logger)
        self._monitor = DataUpdateMonitor(self)
        self._sync_coordinator = SyncCoordinator()
        self._poll_maintenance_tasks: list[Callable[[], Any]] = []
        self._monitor.changes_detected.connect(self._emit_coordinated_changes, Qt.QueuedConnection)
        self._success_callback_requested.connect(self._dispatch_success_callback, Qt.QueuedConnection)
        self._error_callback_requested.connect(self._dispatch_error_callback, Qt.QueuedConnection)
        self._monitor.start()

    def get_data_version(self) -> int:
        return self.db.get_data_version()

    def get_latest_change_id(self, admission_id: Optional[int] = None, include_global: bool = True) -> int:
        return self.db.get_latest_change_id(admission_id=admission_id, include_global=include_global)

    def fetch_changes_since(self, last_change_id: int, admission_id: Optional[int] = None, include_global: bool = True):
        return self.db.fetch_changes_since(
            last_change_id=last_change_id,
            admission_id=admission_id,
            include_global=include_global,
        )

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

    def run_poll_maintenance_tasks(self):
        for task in list(self._poll_maintenance_tasks):
            try:
                task()
            except Exception as exc:
                logger.warning("DataService poll maintenance task failed: %s", exc, exc_info=True)

    def run_write(self, description: str, operation: Callable):
        result = self.db.run_write_operation(operation, source=description)
        self.write_finished.emit(description)
        self.request_immediate_refresh(force_emit=True, source=description)
        return result

    def enqueue_write(
        self,
        description: str,
        operation: Callable[[], Any],
        on_success: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ):
        def handle_success(result):
            self.write_finished.emit(description)
            self.request_immediate_refresh(force_emit=True, source=description)
            self._success_callback_requested.emit(on_success, result)

        def handle_error(exc: Exception):
            logger.error("Queued write failed for %s: %s", description, exc)
            self.write_failed.emit(f"{description}: {exc}")
            self._error_callback_requested.emit(on_error, exc)

        self._queue.submit(
            func=operation,
            description=description,
            on_success=handle_success,
            on_error=handle_error,
        )

    @Slot(object, object)
    def _dispatch_success_callback(self, callback: Optional[Callable[[Any], None]], result: Any):
        if not callback:
            return
        try:
            callback(result)
        except Exception as exc:
            logger.error("DataService success callback failed: %s", exc, exc_info=True)

    @Slot(object, object)
    def _dispatch_error_callback(self, callback: Optional[Callable[[Exception], None]], exc: Exception):
        if not callback:
            return
        try:
            callback(exc)
        except Exception as callback_exc:
            logger.error("DataService error callback failed: %s", callback_exc, exc_info=True)

    def shutdown(self):
        if self._monitor and self._monitor.isRunning():
            self._monitor.stop()
            self._monitor.wait(1500)
        self._queue.shutdown(timeout=5.0)
        try:
            from rem_card.app.local_metrics import flush_metrics

            flush_metrics(timeout=1.0)
        except Exception:
            pass

    def request_immediate_refresh(self, *, force_emit: bool = False, source: str = ""):
        if self._monitor:
            self._monitor.request_refresh(force_emit=force_emit, source=source)

    @Slot(dict)
    def _emit_coordinated_changes(self, payload: dict):
        self.changes_detected.emit(self._sync_coordinator.classify(payload or {}))
