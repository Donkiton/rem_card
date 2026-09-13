import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional


DB_UNAVAILABLE_MESSAGE = (
    "База данных недоступна. Проверьте выбранную сетевую папку базы. "
    "Сохранение невозможно до восстановления доступа."
)

_NOTIFY_THROTTLE_SEC = 10.0
_last_notify_ts = 0.0
_notifier = None
_notifier_lock = threading.Lock()
_incident_lock = threading.RLock()
_incident_active = False
_warning_enqueued = False
_warning_visible = False
_warning_presented_for_incident = False
_warnings_suppressed = False
_direct_failure_subscribers: dict[int, Callable[["DirectCentralFailureEvent"], object]] = {}
_next_direct_failure_subscriber_id = 1


class DatabaseUnavailableError(RuntimeError):
    pass


class DatabaseClosedError(RuntimeError):
    pass


@dataclass(frozen=True)
class DirectCentralFailureEvent:
    cause: Exception
    wrapped: DatabaseUnavailableError
    context: str
    detected_monotonic: float
    thread_id: int
    database_path: str = ""
    runtime_mode: str = ""


def subscribe_direct_central_failure(
    callback: Callable[[DirectCentralFailureEvent], object],
) -> Callable[[], None]:
    """Subscribe to confirmed direct central database failures.

    A truthy callback result claims user-facing handling for the event, so the
    low-level fallback warning is not queued. The returned function is safe to
    call repeatedly and removes the subscription.
    """
    if not callable(callback):
        raise TypeError("Direct central failure callback must be callable")
    global _next_direct_failure_subscriber_id
    with _incident_lock:
        subscriber_id = _next_direct_failure_subscriber_id
        _next_direct_failure_subscriber_id += 1
        _direct_failure_subscribers[subscriber_id] = callback

    def unsubscribe() -> None:
        with _incident_lock:
            _direct_failure_subscribers.pop(subscriber_id, None)

    return unsubscribe


def notify_direct_central_success() -> None:
    """Reset the current outage incident after an explicit central success."""
    global _incident_active, _warning_enqueued, _warning_presented_for_incident
    with _incident_lock:
        _incident_active = False
        _warning_presented_for_incident = False
        if not _warning_visible:
            _warning_enqueued = False


def set_database_unavailable_warnings_suppressed(suppressed: bool) -> None:
    """Suppress queued late warnings while the application is shutting down."""
    global _warnings_suppressed, _warning_enqueued
    with _incident_lock:
        _warnings_suppressed = bool(suppressed)
        if _warnings_suppressed and not _warning_visible:
            _warning_enqueued = False


def _publish_direct_central_failure(event: DirectCentralFailureEvent) -> bool:
    with _incident_lock:
        subscribers = list(_direct_failure_subscribers.values())
    handled = False
    for callback in subscribers:
        try:
            handled = bool(callback(event)) or handled
        except Exception:
            logging.getLogger("RemCard").exception("Direct central failure subscriber failed")
    return handled


def is_database_unavailable_error(exc: Exception) -> bool:
    if isinstance(exc, DatabaseUnavailableError):
        return True
    if isinstance(exc, sqlite3.OperationalError):
        text = str(exc).lower()
    elif isinstance(exc, (sqlite3.DatabaseError, OSError, PermissionError)):
        text = str(exc).lower()
    else:
        return False

    if "database is locked" in text or "database table is locked" in text:
        return False

    markers = (
        "unable to open database file",
        "disk i/o error",
        "attempt to write a readonly database",
        "readonly database",
        "cannot open",
        "no such file",
        "path not found",
        "network",
        "remote",
        "device is not ready",
        "file system",
        "input/output error",
        "не удается найти",
        "системе не удается",
        "отказано в доступе",
        "недоступ",
        "сетев",
    )
    return any(marker in text for marker in markers)


def to_database_unavailable_error(exc: Exception) -> DatabaseUnavailableError:
    if isinstance(exc, DatabaseUnavailableError):
        return exc
    return DatabaseUnavailableError(DB_UNAVAILABLE_MESSAGE)


def notify_database_unavailable(
    exc: Exception,
    *,
    context: str = "database",
    logger: Optional[logging.Logger] = None,
    database_path: str = "",
    runtime_mode: str = "",
) -> DatabaseUnavailableError:
    global _incident_active, _warning_presented_for_incident
    wrapped = to_database_unavailable_error(exc)
    active_logger = logger or logging.getLogger("RemCard")
    active_logger.error("%s unavailable: %s", context, exc, exc_info=True)
    try:
        from rem_card.services.crash_reports import capture_database_failure, flush_local_crash_outbox

        report_path = capture_database_failure(
            "runtime_unavailable",
            phase="runtime",
            check_result="runtime_database_unavailable",
        )
        if report_path is not None:
            threading.Thread(
                target=flush_local_crash_outbox,
                name="CrashReportDatabaseDelivery",
                daemon=True,
            ).start()
    except Exception:
        pass
    if is_database_unavailable_error(exc):
        with _incident_lock:
            _incident_active = True
        event = DirectCentralFailureEvent(
            cause=exc,
            wrapped=wrapped,
            context=str(context or "database"),
            detected_monotonic=time.monotonic(),
            thread_id=threading.get_ident(),
            database_path=str(database_path or ""),
            runtime_mode=str(runtime_mode or ""),
        )
        handled = _publish_direct_central_failure(event)
        if handled:
            with _incident_lock:
                _warning_presented_for_incident = True
        else:
            _show_warning_throttled()
    return wrapped


def _show_warning_throttled():
    global _last_notify_ts, _warning_enqueued, _warning_presented_for_incident
    with _incident_lock:
        if (
            _warnings_suppressed
            or _warning_enqueued
            or _warning_visible
            or _warning_presented_for_incident
        ):
            return
        _warning_enqueued = True
        _warning_presented_for_incident = True
        _last_notify_ts = time.time()

    notifier = _get_qt_notifier()
    if notifier is not None:
        notifier.show_requested.emit(DB_UNAVAILABLE_MESSAGE)
        return
    with _incident_lock:
        _warning_enqueued = False
        _warning_presented_for_incident = False


def _begin_warning_display() -> bool:
    global _warning_enqueued, _warning_visible
    with _incident_lock:
        if _warnings_suppressed or not _incident_active:
            _warning_enqueued = False
            return False
        if _warning_visible:
            _warning_enqueued = False
            return False
        _warning_enqueued = False
        _warning_visible = True
        return True


def _finish_warning_display() -> None:
    global _warning_visible
    with _incident_lock:
        _warning_visible = False


def _get_qt_notifier():
    global _notifier
    try:
        from PySide6.QtCore import QObject, Qt, Signal
        from PySide6.QtWidgets import QApplication
    except Exception:
        return None

    app = QApplication.instance()
    if app is None:
        return None

    with _notifier_lock:
        if _notifier is not None:
            return _notifier

        class _DatabaseWarningNotifier(QObject):
            show_requested = Signal(str)

            def __init__(self):
                super().__init__()
                self.show_requested.connect(self._show, Qt.QueuedConnection)

            def _show(self, message: str):
                if not _begin_warning_display():
                    return
                try:
                    from rem_card.ui.shared.emergency_dialogs import EmergencyActionDialog

                    EmergencyActionDialog.ask(None, "База данных недоступна", message, [("Понятно", 1)])
                except Exception:
                    try:
                        from PySide6.QtWidgets import QMessageBox

                        QMessageBox.warning(None, "База данных недоступна", message)
                    except Exception:
                        pass
                finally:
                    _finish_warning_display()

        _notifier = _DatabaseWarningNotifier()
        _notifier.moveToThread(app.thread())
        return _notifier
