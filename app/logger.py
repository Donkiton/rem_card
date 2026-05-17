import logging
import os
import socket
import sys
import time
import functools
from datetime import datetime

from rem_card.app.paths import LOGS_DIR, ensure_directories
from rem_card.app.runtime_paths import cleanup_old_local_logs, get_log_file_prefix

def setup_logger():
    ensure_directories()
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

    return logger

# Создаем глобальный логгер
logger = setup_logger()

import threading
import faulthandler


_FAULT_LOCK = threading.Lock()
_FAULT_FILE = None
_FAULT_LOG_PATH = None

def _extract_fault_payload(content: str) -> str:
    payload_lines = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("--- SESSION START:") and stripped.endswith("---"):
            continue
        if stripped.startswith("--- SESSION END:") and stripped.endswith("---"):
            continue
        payload_lines.append(line)
    if not payload_lines:
        return ""
    return "\n".join(payload_lines[-40:])


def _archive_fault_log(fault_log_path: str, content: str, *, suffix: str = "") -> str:
    suffix_part = f"_{suffix}" if suffix else ""
    archive_name = f"faults_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.getpid()}{suffix_part}.log"
    archive_path = os.path.join(os.path.dirname(fault_log_path), archive_name)
    with open(archive_path, "w", encoding="utf-8") as archive_file:
        archive_file.write(content)
    return archive_path


def init_crash_handler():
    """Инициализация расширенного перехватчика фатальных сбоев (C++ и Python)."""
    global _FAULT_FILE, _FAULT_LOG_PATH
    ensure_directories()
        
    fault_log_path = os.path.join(LOGS_DIR, "faults.log")
    reset_fault_log = True
    
    # 1. Проверяем, были ли сбои в прошлом сеансе
    if os.path.exists(fault_log_path) and os.path.getsize(fault_log_path) > 0:
        reset_fault_log = False
        try:
            with open(fault_log_path, 'r', encoding='utf-8') as f:
                content = f.read()
                payload = _extract_fault_payload(content)
                if payload:
                    archive_path = _archive_fault_log(fault_log_path, content)
                    logger.warning(
                        "!!! FOUND PREVIOUS CRASH LOG (archived to %s) !!!\nLast state:\n%s",
                        archive_path,
                        payload[-500:],
                    )
                reset_fault_log = True
        except Exception as exc:
            logger.warning("Failed to inspect previous crash log %s: %s", fault_log_path, exc)
            
    # 2. Настраиваем faulthandler (запись низкоуровневых ошибок Qt/C++)
    try:
        with _FAULT_LOCK:
            if _FAULT_FILE is not None:
                try:
                    faulthandler.disable()
                except Exception:
                    pass
                try:
                    _FAULT_FILE.close()
                except Exception:
                    pass
                _FAULT_FILE = None
            fault_file = open(fault_log_path, "w" if reset_fault_log else "a", encoding="utf-8")
            # Записываем разделитель сеанса
            fault_file.write(
                "\n--- SESSION START: "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                f"pid={os.getpid()} role={get_log_file_prefix()} host={socket.gethostname()} ---\n"
            )
            fault_file.flush()
            faulthandler.enable(file=fault_file)
            _FAULT_FILE = fault_file
            _FAULT_LOG_PATH = fault_log_path
    except Exception as e:
        logger.error(f"Failed to enable faulthandler: {e}")


def finalize_crash_handler(exit_code: int | None = None):
    """
    Завершает текущий faulthandler-сеанс.

    Если Windows/PySide успел записать native fault, но приложение затем
    завершилось штатно, переносим payload в архив и оставляем чистый marker.
    Так `faults.log` не выглядит как новая ошибка на следующей проверке.
    """
    global _FAULT_FILE, _FAULT_LOG_PATH
    with _FAULT_LOCK:
        fault_log_path = _FAULT_LOG_PATH or os.path.join(LOGS_DIR, "faults.log")
        fault_file = _FAULT_FILE
        _FAULT_FILE = None
        _FAULT_LOG_PATH = None

        try:
            faulthandler.disable()
        except Exception:
            pass
        try:
            if fault_file is not None:
                fault_file.flush()
                fault_file.close()
        except Exception:
            pass

    try:
        with open(fault_log_path, "r", encoding="utf-8") as fh:
            content = fh.read()
    except FileNotFoundError:
        return
    except Exception as exc:
        logger.warning("Failed to finalize crash log %s: %s", fault_log_path, exc)
        return

    payload = _extract_fault_payload(content)
    archived_to = ""
    if payload:
        try:
            archived_to = _archive_fault_log(fault_log_path, content, suffix="graceful")
            logger.warning(
                "Fault handler captured native fault payload during a session that reached shutdown; archived to %s",
                archived_to,
            )
        except Exception as exc:
            logger.warning("Failed to archive finalized crash log %s: %s", fault_log_path, exc)

    try:
        with open(fault_log_path, "w", encoding="utf-8") as fh:
            fh.write(
                "\n--- SESSION END: "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                f"pid={os.getpid()} exit_code={exit_code if exit_code is not None else 'unknown'} "
                f"archived={archived_to or 'none'} ---\n"
            )
    except Exception as exc:
        logger.warning("Failed to reset finalized crash log %s: %s", fault_log_path, exc)


def log_exception(exc_type, exc_value, exc_traceback):
    """Глобальный перехватчик исключений (Python)."""
    if issubclass(exc_type, KeyboardInterrupt):
        if hasattr(sys, '__excepthook__'):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
        
    # Формируем детальное сообщение об ошибке
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
    logger.critical(
        f"--- UNCAUGHT THREAD EXCEPTION ({args.thread.name if args.thread else 'unknown'}) ---\n"
        f"Type: {args.exc_type.__name__ if args.exc_type else 'Unknown'}\n"
        f"Value: {args.exc_value}\n",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback)
    )

threading.excepthook = _log_thread_exception
