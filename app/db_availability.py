import logging
import sqlite3
import threading
import time
from typing import Optional


DB_UNAVAILABLE_MESSAGE = (
    "База данных недоступна. Проверьте выбранную сетевую папку базы. "
    "Сохранение невозможно до восстановления доступа."
)

_NOTIFY_THROTTLE_SEC = 10.0
_last_notify_ts = 0.0
_notifier = None
_notifier_lock = threading.Lock()


class DatabaseUnavailableError(RuntimeError):
    pass


class DatabaseClosedError(RuntimeError):
    pass


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
) -> DatabaseUnavailableError:
    wrapped = to_database_unavailable_error(exc)
    active_logger = logger or logging.getLogger("RemCard")
    active_logger.error("%s unavailable: %s", context, exc, exc_info=True)
    _show_warning_throttled()
    return wrapped


def _show_warning_throttled():
    global _last_notify_ts
    now = time.time()
    if (now - _last_notify_ts) < _NOTIFY_THROTTLE_SEC:
        return
    _last_notify_ts = now

    notifier = _get_qt_notifier()
    if notifier is not None:
        notifier.show_requested.emit(DB_UNAVAILABLE_MESSAGE)


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
                try:
                    from rem_card.ui.shared.custom_message_box import CustomMessageBox

                    CustomMessageBox.warning(None, "База данных недоступна", message)
                except Exception:
                    try:
                        from PySide6.QtWidgets import QMessageBox

                        QMessageBox.warning(None, "База данных недоступна", message)
                    except Exception:
                        pass

        _notifier = _DatabaseWarningNotifier()
        _notifier.moveToThread(app.thread())
        return _notifier
