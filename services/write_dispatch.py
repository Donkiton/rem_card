"""Общая передача сервисной операции владельцу очереди записи."""

from typing import Any, Callable


def enqueue_service_write(
    data_service,
    description: str,
    operation: Callable[[], Any],
    on_success=None,
    on_error=None,
) -> None:
    """Сохраняет синхронный режим для сервисов без DataService.

    Ошибка callback успеха не является ошибкой самой записи и не передаётся
    в on_error. Исключение очереди также не запускает операцию повторно.
    """
    if data_service:
        data_service.enqueue_write(
            description=description,
            operation=operation,
            on_success=on_success,
            on_error=on_error,
        )
        return
    try:
        result = operation()
    except Exception as exc:
        if on_error:
            on_error(exc)
            return
        raise
    if on_success:
        on_success(result)
