import logging
import os
import sys
import time
import functools
from datetime import datetime
from rem_card.app.runtime_log_storage import RuntimeLogHandler, storage_enabled

from rem_card.app.runtime_paths import (
    cleanup_old_local_logs,
    get_log_file_prefix,
    get_runtime_log_directory_candidates,
    get_runtime_logs_dir,
    is_compiled,
    migrate_legacy_runtime_logs,
    record_runtime_log_location,
)
from rem_card.app.compact_logging import CompactLogHandler


LOGS_DIR = get_runtime_logs_dir()


def _ensure_logger_directories() -> str | None:
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        return None
    except OSError as exc:
        return str(exc)


def _logger_directory_candidates() -> tuple[str, ...]:
    return get_runtime_log_directory_candidates()


def _create_file_handler(formatter: logging.Formatter):
    warnings: list[str] = []
    for log_dir in _logger_directory_candidates():
        try:
            os.makedirs(log_dir, exist_ok=True)
        except OSError as exc:
            warnings.append(f"{log_dir}: {exc}")
            continue
        try:
            cleanup_old_local_logs(log_dir)
        except OSError as exc:
            warnings.append(f"cleanup {log_dir}: {exc}")
        if (
            is_compiled()
            and os.path.normcase(os.path.abspath(log_dir))
            == os.path.normcase(os.path.abspath(LOGS_DIR))
        ):
            try:
                migration = migrate_legacy_runtime_logs(log_dir)
                if migration.get("errors"):
                    warnings.append(
                        f"legacy log migration {log_dir}: "
                        + "; ".join(str(item) for item in migration["errors"])
                    )
            except Exception as exc:
                warnings.append(f"legacy log migration {log_dir}: {exc}")
        log_file = os.path.join(
            log_dir,
            f"{get_log_file_prefix()}_{datetime.now().strftime('%Y%m%d')}.log",
        )
        try:
            handler = (
                RuntimeLogHandler(log_dir, get_log_file_prefix())
                if storage_enabled()
                else logging.FileHandler(log_file, encoding="utf-8")
            )
        except OSError as exc:
            warnings.append(f"{log_file}: {exc}")
            continue
        handler.setFormatter(formatter)
        if os.path.normcase(os.path.abspath(log_dir)) != os.path.normcase(os.path.abspath(LOGS_DIR)):
            record_runtime_log_location(
                log_dir,
                preferred_dir=LOGS_DIR,
                fallback_reason="; ".join(warnings),
            )
        return CompactLogHandler(handler), warnings
    return None, warnings


def setup_logger():
    logger = logging.getLogger("RemCard")
    if getattr(logger, "_remcard_configured", False):
        return logger

    # Меняем уровень на INFO, чтобы избежать спама DEBUG логов (например, SQL-запросов)
    logger.setLevel(logging.INFO)

    # Формат логирования
    formatter = logging.Formatter('%(asctime)s | %(levelname)-8s | %(name)s | %(message)s')

    # Handler для консоли
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # Файл всегда локальный. Если профиль пользователя недоступен, используем
    # системную temp-папку; полный отказ файловой системы не блокирует запуск.
    file_handler, file_warnings = _create_file_handler(formatter)
    if file_handler is not None:
        logger.addHandler(file_handler)
    logger._remcard_configured = True
    for warning in file_warnings:
        logger.warning("Log file is unavailable; fallback was attempted: %s", warning)
    if file_handler is None:
        logger.error("All local log files are unavailable; continuing with console logging only")

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


def finalize_crash_handler(exit_code: int | None = None, *, crash_recorded: bool = False):
    try:
        from rem_card.services.crash_reports import finalize_crash_session

        finalize_crash_session(exit_code=exit_code, crash_recorded=crash_recorded)
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
