from typing import Any

from PySide6.QtCore import QObject, Signal

from rem_card.app.logger import logger
from rem_card.data.dao.sync_cursor import (
    EPOCH_SYNC_TS,
    is_cursor_newer,
    make_sync_cursor,
    max_sync_cursor,
    normalize_sync_cursor,
)


class DataPoller(QObject):
    """
    Сервис для периодического опроса БД на предмет изменений.
    Обеспечивает синхронизацию данных между разными клиентами (врач/медсестра).
    """

    data_updated = Signal(dict)  # Сигнал отправляет дельту изменений

    def __init__(self, service):
        super().__init__()
        self.service = service
        self.last_sync_time = EPOCH_SYNC_TS  # backward-compatible marker
        self._sync_cursor = make_sync_cursor(EPOCH_SYNC_TS, 0)
        self._is_syncing = False
        self._initialized = False

    def _reset_cursor(self):
        self.last_sync_time = EPOCH_SYNC_TS
        self._sync_cursor = make_sync_cursor(EPOCH_SYNC_TS, 0)

    def reset(self):
        """Сброс состояния при смене пациента."""
        self._reset_cursor()
        self._initialized = False

    def _get_status_dao(self):
        """Безопасное получение status_dao из фасада."""
        if hasattr(self.service, "status_service") and self.service.status_service:
            if hasattr(self.service.status_service, "status_dao"):
                return self.service.status_service.status_dao
        return None

    @staticmethod
    def _to_cursor(value: Any):
        ts, row_id = normalize_sync_cursor(value)
        return make_sync_cursor(ts, row_id)

    def _commit_cursor(self, cursor_value: Any):
        cursor = self._to_cursor(cursor_value)
        self._sync_cursor = cursor
        self.last_sync_time = cursor["updated_at"]

    def on_timer_tick(self, admission_id: int):
        """
        Метод вызывается по таймеру (каждые 1-2 секунды).
        Опрашивает все источники данных независимо.
        """
        if not admission_id or self._is_syncing:
            return

        self._is_syncing = True
        try:
            results = {
                "orders": [],
                "vitals": [],
                "fluids": [],
                "status_events": [],
            }
            sync_candidates = [self._sync_cursor]

            # 1. ORDERS
            try:
                if hasattr(self.service, "orders_dao") and self.service.orders_dao:
                    items, sync = self.service.orders_dao.fetch_updated_orders(admission_id, self._sync_cursor)
                    results["orders"] = items
                    if items:
                        sync_candidates.append(sync)
            except Exception as exc:
                logger.error(f"DataPoller: Error fetching orders: {exc}")

            # 2. VITALS
            try:
                if hasattr(self.service, "vitals_dao") and self.service.vitals_dao:
                    items, sync = self.service.vitals_dao.fetch_updated_vitals(admission_id, self._sync_cursor)
                    results["vitals"] = items
                    if items:
                        sync_candidates.append(sync)
            except Exception as exc:
                logger.error(f"DataPoller: Error fetching vitals: {exc}")

            # 3. FLUIDS
            try:
                if hasattr(self.service, "fluids_dao") and self.service.fluids_dao:
                    items, sync = self.service.fluids_dao.fetch_updated_fluids(admission_id, self._sync_cursor)
                    results["fluids"] = items
                    if items:
                        sync_candidates.append(sync)
            except Exception as exc:
                logger.error(f"DataPoller: Error fetching fluids: {exc}")

            # 4. STATUS EVENTS
            try:
                status_dao = self._get_status_dao()
                if status_dao:
                    items, sync = status_dao.fetch_updated_events(admission_id, self._sync_cursor)
                    results["status_events"] = items
                    if items:
                        sync_candidates.append(sync)
            except Exception as exc:
                logger.error(f"DataPoller: Error fetching status events: {exc}")

            new_cursor = max_sync_cursor(*sync_candidates)

            # Логика отправки сигналов
            if not self._initialized:
                self._initialized = True
                self._commit_cursor(new_cursor)
                results["is_initial"] = True
                results["sync_cursor"] = dict(self._sync_cursor)
                self.data_updated.emit(results)
                return

            if is_cursor_newer(
                new_cursor["updated_at"],
                new_cursor["id"],
                self._sync_cursor["updated_at"],
                self._sync_cursor["id"],
            ):
                logger.debug(
                    "DataPoller: Changes detected for %s. Sync cursor: %s",
                    admission_id,
                    new_cursor,
                )
                self._commit_cursor(new_cursor)
                results["is_initial"] = False
                results["sync_cursor"] = dict(self._sync_cursor)
                self.data_updated.emit(results)

        except Exception as exc:
            logger.error(f"DataPoller: Critical error in sync loop: {exc}", exc_info=True)
        finally:
            self._is_syncing = False
