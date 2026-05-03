from datetime import datetime, timedelta
from collections import OrderedDict
import time
from PySide6.QtWidgets import QWidget
from PySide6.QtCore import QTimer, Signal
from .nurse_order_card import NurseOrderCard
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.app.logger import logger
from rem_card.services import persistent_snapshot_cache

PENDING_MARK_TTL_SEC = 8.0
SECTOR_1A_BEFORE_MIN = 60
SECTOR_1A_AFTER_MIN = 180
TIME_TIMER_MAX_MS = 60 * 60 * 1000
CURRENT_ORDERS_CACHE_LIMIT = 10

class CurrentNurseOrdersWidget(QWidget):
    """Менеджер назначений в 1а. Сектор 5 теперь принадлежит питанию."""
    localBalanceChanged = Signal()
    balanceRefreshRequested = Signal()

    def __init__(self, service, sector_1a, sector_5, parent=None):
        super().__init__(parent)
        self.service = service 
        self.sector_1a = sector_1a
        self.sector_5 = sector_5
        self.admission_id = None
        self.shift_date = None
        
        self.cards_1a = {}
        self._last_lock_warning_ts = 0.0
        self._pending_marks = {}
        self._all_data = []
        self._snapshot_cache = OrderedDict()
        
        self._time_timer = QTimer(self)
        self._time_timer.setSingleShot(True)
        self._time_timer.timeout.connect(self._render_from_cache)
        self._clear_sector_5()

    def set_context(self, admission_id, shift_date):
        # Если пациент сменился, очищаем кэш виджетов
        if self._cache_key_for(self.admission_id, self.shift_date) != self._cache_key_for(admission_id, shift_date):
            self._clear_all_cards()
            self._pending_marks.clear()
            self._all_data = []

        self.admission_id = admission_id
        self.shift_date = shift_date
        cached_applied = self._apply_cached_snapshot_if_available()
        if cached_applied and self._is_cached_snapshot_current():
            return
        self.refresh_data()

    def _cache_key(self):
        return self._cache_key_for(self.admission_id, self.shift_date)

    @classmethod
    def _cache_key_for(cls, admission_id, shift_date):
        if not admission_id or not shift_date:
            return None
        shift_date = cls._normalize_shift_date(shift_date)
        return (
            int(admission_id),
            shift_date.isoformat(timespec="seconds"),
        )

    @staticmethod
    def _normalize_shift_date(value: datetime) -> datetime:
        normalized = value.replace(microsecond=0)
        shift_start = normalized.replace(hour=8, minute=0, second=0, microsecond=0)
        if normalized.hour < 8:
            shift_start -= timedelta(days=1)
        return shift_start

    def _current_change_id(self) -> int:
        if not self.service or not self.admission_id:
            return 0
        if hasattr(self.service, "get_latest_change_id"):
            try:
                return int(self.service.get_latest_change_id(admission_id=self.admission_id, include_global=False) or 0)
            except TypeError:
                try:
                    return int(self.service.get_latest_change_id(admission_id=self.admission_id) or 0)
                except Exception as exc:
                    logger.warning("CurrentNurseOrdersWidget change_id lookup failed: %s", exc)
            except Exception as exc:
                logger.warning("CurrentNurseOrdersWidget change_id lookup failed: %s", exc)
        return 0

    def _apply_cached_snapshot_if_available(self) -> bool:
        key = self._cache_key()
        if key is None:
            return False
        snapshot = self._snapshot_cache.get(key)
        if snapshot is None:
            snapshot = persistent_snapshot_cache.load_snapshot("current_orders", key)
            if snapshot is None:
                return False
            self._snapshot_cache[key] = snapshot
        self._snapshot_cache.move_to_end(key)
        self._all_data = self._apply_pending_marks(list(snapshot.get("data") or []))
        self._render_from_cache()
        return True

    def _is_cached_snapshot_current(self) -> bool:
        key = self._cache_key()
        if key is None:
            return False
        snapshot = self._snapshot_cache.get(key)
        if snapshot is None:
            return False
        try:
            return self._current_change_id() <= int(snapshot.get("version") or 0)
        except Exception as exc:
            logger.warning("CurrentNurseOrdersWidget cache version check failed: %s", exc)
            return False

    def _store_snapshot_cache(self, data_list):
        key = self._cache_key()
        if key is None:
            return
        self._snapshot_cache[key] = {
            "version": self._current_change_id(),
            "data": [dict(item) for item in (data_list or [])],
        }
        persistent_snapshot_cache.store_snapshot(
            "current_orders",
            key,
            dict(self._snapshot_cache[key]),
            expires_at=persistent_snapshot_cache.expiry_from_cache_key(key, shift_key_index=1),
        )
        self._snapshot_cache.move_to_end(key)
        while len(self._snapshot_cache) > CURRENT_ORDERS_CACHE_LIMIT:
            self._snapshot_cache.popitem(last=False)

    def _clear_all_cards(self):
        if hasattr(self, "_time_timer"):
            self._time_timer.stop()
        self.sector_1a.set_content(None)
        self.cards_1a.clear()
        self._clear_sector_5()

    def _clear_sector_5(self):
        return

    def refresh_data(self, *, force: bool = False):
        if not self.admission_id or not self.shift_date:
            return

        if not force and self._apply_cached_snapshot_if_available() and self._is_cached_snapshot_current():
            return
            
        # Запрашиваем свежие данные
        try:
            all_data = self.service.get_nurse_orders_data(self.admission_id, self.shift_date)
        except Exception as exc:
            if self._is_retryable_lock_error(exc):
                self._log_lock_warning_throttled(exc)
            else:
                logger.error("CurrentNurseOrdersWidget refresh failed: %s", exc, exc_info=True)
            return
        self._store_snapshot_cache(all_data)
        self._all_data = self._apply_pending_marks(all_data)
        self._render_from_cache()

    def handle_data_changes(self, payload: dict):
        if not self.admission_id:
            return

        changed_entities = {
            str(entity)
            for entity in (payload.get("changed_entities") or [])
            if entity is not None
        }
        changes = payload.get("changes") or []
        if not changed_entities:
            changed_entities = {
                str(change.get("entity_name") or "")
                for change in changes
                if change.get("entity_name")
            }

        if not payload.get("forced") and not changed_entities.intersection({"orders", "administrations"}):
            return

        for change in changes:
            entity_name = str(change.get("entity_name") or "")
            if entity_name and entity_name not in {"orders", "administrations"}:
                continue
            admission_id = change.get("admission_id")
            if admission_id is None or int(admission_id) == int(self.admission_id):
                self.refresh_data(force=True)
                return

        if payload.get("forced") or not changes:
            self.refresh_data(force=True)

    def _render_from_cache(self):
        now = datetime.now()
        list_1a_data = [
            item
            for item in self._apply_pending_marks(self._all_data)
            if self._is_visible_in_sector_1a(item, now)
        ]
        list_1a_data.sort(key=self._sort_key_1a)

        self._sync_sector_widgets(self.sector_1a, self.cards_1a, list_1a_data)
        self._clear_sector_5()
        self._schedule_next_time_boundary(now)
        
        if self.sector_1a:
            self.sector_1a.update()
            if hasattr(self.sector_1a, 'scroll_area'):
                self.sector_1a.scroll_area.viewport().update()

    def _sync_sector_widgets(self, sector, cache, data_list):
        """Синхронизирует виджеты в лейауте с данными без мерцания."""
        if sector is None:
            return
        new_ids = {item['id'] for item in data_list}
        
        # 1. Удаляем виджеты, которых больше нет в данных
        for admin_id in list(cache.keys()):
            if admin_id not in new_ids:
                card = cache.pop(admin_id)
                card.setParent(None)
                card.deleteLater()

        # 2. Обновляем или создаем виджеты
        layout = sector.content_layout
        
        for i, item in enumerate(data_list):
            admin_id = item['id']
            if admin_id in cache:
                # Обновляем данные существующего виджета
                card = cache[admin_id]
                card.update_data(item)
            else:
                # Создаем новый виджет
                card = NurseOrderCard(item)
                card.statusChanged.connect(self.handle_status_change)
                cache[admin_id] = card
            
            # 3. Переупорядочиваем в лейауте перед Stretch сектора 1а.
            current_idx = layout.indexOf(card)
            if current_idx != i:
                layout.insertWidget(i, card)
                card.raise_() # Принудительно на передний план

    def _sort_key_1a(self, item):
        planned_dt = datetime.fromisoformat(item['planned_time'])
        return (
            planned_dt.isoformat(),
            item.get('priority', 999),
            item.get('latin', '').lower()
        )

    def _is_visible_in_sector_1a(self, item, now: datetime) -> bool:
        if item.get('comment'):
            return False
        try:
            planned_dt = datetime.fromisoformat(item['planned_time'])
        except Exception:
            return False
        return (
            planned_dt - timedelta(minutes=SECTOR_1A_BEFORE_MIN)
            <= now
            < planned_dt + timedelta(minutes=SECTOR_1A_AFTER_MIN)
        )

    def _schedule_next_time_boundary(self, now: datetime | None = None):
        self._time_timer.stop()
        if not self._all_data:
            return

        now = now or datetime.now()
        next_boundary = None
        for item in self._apply_pending_marks(self._all_data):
            if item.get('comment'):
                continue
            try:
                planned_dt = datetime.fromisoformat(item['planned_time'])
            except Exception:
                continue
            for boundary in (
                planned_dt - timedelta(minutes=SECTOR_1A_BEFORE_MIN),
                planned_dt,
                planned_dt + timedelta(minutes=60),
                planned_dt + timedelta(minutes=SECTOR_1A_AFTER_MIN),
            ):
                if boundary > now and (next_boundary is None or boundary < next_boundary):
                    next_boundary = boundary

        if next_boundary is None:
            return

        delay_ms = max(1, int((next_boundary - now).total_seconds() * 1000) + 25)
        self._time_timer.start(min(delay_ms, TIME_TIMER_MAX_MS))

    @staticmethod
    def _is_retryable_lock_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return (
            "could not acquire sequential write lock" in msg
            or "database is locked" in msg
            or "database table is locked" in msg
            or "database schema is locked" in msg
        )

    def _log_lock_warning_throttled(self, exc: Exception):
        now = time.monotonic()
        if (now - self._last_lock_warning_ts) < 15.0:
            return
        self._last_lock_warning_ts = now
        logger.warning("CurrentNurseOrdersWidget refresh skipped due to DB lock: %s", exc)

    def _set_pending_mark(self, admin_id: int, mark: str):
        self._pending_marks[int(admin_id)] = {
            "mark": str(mark or ""),
            "started_mono": time.monotonic(),
            "actual_time": datetime.now().isoformat(),
        }

    def _get_pending_mark(self, admin_id: int):
        if admin_id is None:
            return None
        pending = self._pending_marks.get(int(admin_id))
        if not pending:
            return None
        if (time.monotonic() - float(pending.get("started_mono") or 0.0)) > PENDING_MARK_TTL_SEC:
            self._pending_marks.pop(int(admin_id), None)
            return None
        return pending

    def _apply_pending_marks(self, data_list):
        if not self._pending_marks:
            return data_list

        now_mono = time.monotonic()
        for admin_id, pending in list(self._pending_marks.items()):
            if (now_mono - float(pending.get("started_mono") or 0.0)) > PENDING_MARK_TTL_SEC:
                self._pending_marks.pop(admin_id, None)

        patched = []
        for item in data_list:
            patched_item = dict(item)
            pending = self._get_pending_mark(patched_item.get("id"))
            if pending:
                mark = pending.get("mark") or ""
                patched_item["comment"] = mark
                patched_item["actual_time"] = patched_item.get("actual_time") or pending.get("actual_time")
            patched.append(patched_item)
        return patched

    def _on_mark_write_success(self, admin_id: int, _result=None):
        # Pending mark stays alive for a short grace window so stale read snapshots
        # cannot briefly resurrect the card in sector 1a.
        pending = self._pending_marks.get(int(admin_id))
        if pending:
            pending["started_mono"] = time.monotonic()
        self.refresh_data()
        self.localBalanceChanged.emit()
        self.balanceRefreshRequested.emit()

    def _on_mark_write_error(self, admin_id: int, exc: Exception):
        self._pending_marks.pop(int(admin_id), None)
        self.refresh_data()
        self.localBalanceChanged.emit()
        self.balanceRefreshRequested.emit()
        CustomMessageBox.warning(self, "Предупреждение", f"Ошибка сохранения: {exc}")

    def _enqueue_write(self, description: str, operation, *, on_success=None, on_error=None):
        if not hasattr(self.service, "enqueue_write"):
            try:
                result = operation()
            except Exception as exc:
                if on_error:
                    on_error(exc)
                else:
                    CustomMessageBox.warning(self, "Предупреждение", f"Ошибка сохранения: {exc}")
                return
            if on_success:
                on_success(result)
            else:
                self.refresh_data()
            return

        self.service.enqueue_write(
            description=description,
            operation=operation,
            on_success=on_success or (lambda _=None: self.refresh_data()),
            on_error=on_error or (lambda exc: CustomMessageBox.warning(self, "Предупреждение", f"Ошибка сохранения: {exc}")),
        )

    def handle_status_change(self, admin_id, mark):
        operation = lambda aid=admin_id, m=mark: self.service.set_nurse_status(aid, m)

        self._set_pending_mark(admin_id, mark)
        self._render_from_cache()
        self.localBalanceChanged.emit()
        self._enqueue_write(
            f"nurse_order_panel_mark:{admin_id}",
            operation,
            on_success=lambda result=None, aid=admin_id: self._on_mark_write_success(aid, result),
            on_error=lambda exc, aid=admin_id: self._on_mark_write_error(aid, exc),
        )
