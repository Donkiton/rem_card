"""Общая передача сервисной операции владельцу очереди записи."""

from typing import Any, Callable


def service_write_failure_state(exc: Exception) -> str:
    # Use the same conservative acknowledgement policy as the queue owner,
    # including network failures and a failed transaction rollback.
    from rem_card.services.data_service import DataService

    return DataService._terminal_write_state(exc)


def enqueue_service_write(
    data_service,
    description: str,
    operation: Callable[[], Any],
    on_success=None,
    on_error=None,
    *,
    write_metadata=None,
    on_settled=None,
) -> None:
    """Сохраняет синхронный режим для сервисов без DataService.

    Ошибка callback успеха не является ошибкой самой записи и не передаётся
    в on_error. Исключение очереди также не запускает операцию повторно.
    """
    if data_service:
        lifecycle = {}
        if write_metadata is not None:
            lifecycle["write_metadata"] = write_metadata
        if on_settled is not None:
            lifecycle["on_settled"] = on_settled
        data_service.enqueue_write(
            description=description,
            operation=operation,
            on_success=on_success,
            on_error=on_error,
            **lifecycle,
        )
        return

    def settle(state, payload):
        if on_settled is not None:
            try:
                on_settled(state, payload)
            except Exception:
                # A failed notification cannot change or replay a mutation.
                import logging

                logging.getLogger(__name__).exception("Service write lifecycle notification failed")

    try:
        result = operation()
    except Exception as exc:
        if on_settled is not None:
            settle(service_write_failure_state(exc), exc)
        if on_error:
            on_error(exc)
            return
        raise
    settle("committed", result)
    if on_success:
        on_success(result)
