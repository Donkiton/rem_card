import logging
import os
import sys
import time
import functools
from datetime import datetime

from rem_card.app.paths import LOGS_DIR
from rem_card.app.runtime_paths import cleanup_old_local_logs, get_log_file_prefix


def _ensure_logger_directories() -> str | None:
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        return None
    except OSError as exc:
        return str(exc)


def setup_logger():
    log_dir_warning = _ensure_logger_directories()
    os.makedirs(LOGS_DIR, exist_ok=True)
    cleanup_old_local_logs(LOGS_DIR)

    log_file = os.path.join(LOGS_DIR, f"{get_log_file_prefix()}_{datetime.now().strftime('%Y%m%d')}.log")
    
    logger = logging.getLogger("RemCard")
    if getattr(logger, "_remcard_configured", False):
        return logger

    # Меняем уровень на INFO, чтобы избежать спама DEBUG логов (например, SQL-запросов)
    logger.setLevel(logging.INFO)

    # Формат логирования
    formatter = logging.Formatter('%(asctime)s | %(levelname)-8s | %(name)s | %(message)s')

    # Handler для файла
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Handler для консоли
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    logger._remcard_configured = True
    if log_dir_warning:
        logger.warning("Log directory was unavailable during logger setup: %s", log_dir_warning)

    return logger

# Создаем глобальный логгер
logger = setup_logger()

import threading
def init_crash_handler(role: str | None = None):
    """Start the local-first crash session without touching patient data."""
    try:
        from rem_card.services.crash_reports import initialize_crash_session

        return initialize_crash_session(role=role)
    except Exception as exc:
        logger.error("Failed to initialize crash reporting: %s", exc)
        return ""


def finalize_crash_handler(exit_code: int | None = None):
    try:
        from rem_card.services.crash_reports import finalize_crash_session

        finalize_crash_session(exit_code=exit_code)
    except Exception as exc:
        logger.warning("Failed to finalize crash reporting: %s", exc)


def flush_crash_reports_async():
    def worker():
        try:
            from rem_card.services.crash_reports import flush_local_crash_outbox

            result = flush_local_crash_outbox()
            if result.get("delivered") or result.get("failed"):
                logger.info("Crash report delivery result: %s", result)
        except Exception as exc:
            logger.warning("Crash report delivery failed: %s", exc)

    thread = threading.Thread(target=worker, name="CrashReportDelivery", daemon=True)
    thread.start()
    return thread


def log_exception(exc_type, exc_value, exc_traceback):
    """Глобальный перехватчик исключений (Python)."""
    if issubclass(exc_type, KeyboardInterrupt):
        if hasattr(sys, '__excepthook__'):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
        
    try:
        from rem_card.services.crash_reports import capture_exception

        capture_exception(
            "unhandled_python_exception",
            exc_type,
            exc_value,
            exc_traceback,
        )
    except Exception:
        pass

    # Общий лог сохраняется для локального расследования.
    import traceback
    tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    
    logger.critical(
        f"--- UNCAUGHT PYTHON EXCEPTION ---\n"
        f"Type: {exc_type.__name__}\n"
        f"Value: {exc_value}\n"
        f"{tb_text}"
    )

# Установка перехватчиков
sys.excepthook = log_exception

def log_execution_time(threshold_ms=50):
    """Декоратор для замера времени выполнения функции. Логирует только если превышен порог threshold_ms."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.perf_counter()
            result = func(*args, **kwargs)
            end_time = time.perf_counter()
            execution_time_ms = (end_time - start_time) * 1000
            if execution_time_ms > threshold_ms:
                logger.debug(f"PERF: {func.__name__} took {execution_time_ms:.2f}ms")
            return result
        return wrapper
    return decorator

def _log_thread_exception(args):
    """Глобальный перехватчик исключений для потоков."""
    try:
        from rem_card.services.crash_reports import capture_exception

        capture_exception(
            "unhandled_thread_exception",
            args.exc_type,
            args.exc_value,
            args.exc_traceback,
            thread_name=args.thread.name if args.thread else "unknown",
        )
    except Exception:
        pass
    logger.critical(
        f"--- UNCAUGHT THREAD EXCEPTION ({args.thread.name if args.thread else 'unknown'}) ---\n"
        f"Type: {args.exc_type.__name__ if args.exc_type else 'Unknown'}\n"
        f"Value: {args.exc_value}\n",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback)
    )

threading.excepthook = _log_thread_exception
