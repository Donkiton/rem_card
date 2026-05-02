import os
from copy import copy
from PySide6.QtCore import QAbstractTableModel, Qt, QModelIndex, Signal, QTimer
from PySide6.QtGui import QIcon
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Tuple
from ...data.dto.remcard_dto import OrderDTO, OrderType, OrderStatus, AdministrationDTO
from ...data.dao.sync_cursor import EPOCH_SYNC_TS, is_cursor_newer, make_sync_cursor

class OrdersModel(QAbstractTableModel):
    def __init__(self, service, admission_id, shift_date, only_committed: bool = False):
        super().__init__()
        self.service = service # Это OrderService (фасад)
        self.domain_service = None # Будет инициализирован в refresh если нужно
        self.admission_id = admission_id
        self.shift_date = shift_date
        self.only_committed = only_committed
        self.orders: List[OrderDTO] = []
        self.admin_map: Dict[Tuple[int, str], AdministrationDTO] = {} # (order_id, time_iso) -> admin
        self.last_sync_cursor = make_sync_cursor(EPOCH_SYNC_TS, 0)
        self.last_sync_ts = self.last_sync_cursor["updated_at"]  # backward compatibility marker
        self.has_any_draft = False
        self.time_slots = []
        self._rebuild_time_slots()

    def _rebuild_time_slots(self):
        self.start_time, self.end_time = self.service.get_day_period(self.shift_date)
        self.time_slots = []
        curr = self.start_time
        while curr < self.end_time:
            self.time_slots.append(curr)
            curr += timedelta(hours=1)

    def _recompute_draft_flag(self):
        self.has_any_draft = (
            any(
                getattr(o, "is_committed", 0) == 0
                or getattr(o, "draft_sort_order", None) is not None
                for o in self.orders
            )
            or any(getattr(a, "is_committed", 0) == 0 for a in self.admin_map.values())
        )

    def _renumber_local_sort_order(self):
        for idx, order in enumerate(self.orders):
            if order is not None:
                order.sort_order = idx

    def rowCount(self, parent=QModelIndex()):
        return len(self.orders)

    def columnCount(self, parent=QModelIndex()):
        return 1 + len(self.time_slots)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
            
        row = index.row()
        col = index.column()
        
        if row >= len(self.orders):
            return None
            
        order = self.orders[row]
        
        if role == Qt.DisplayRole:
            return ""

        if role == Qt.UserRole:
            # Для первой колонки возвращаем заказ
            if col == 0:
                return order
            # Для ячеек времени возвращаем конкретную администрацию (если есть)
            time_iso = self.time_slots[col-1].isoformat()
            return self.admin_map.get((order.id, time_iso))

        if role == Qt.BackgroundRole:
            # Логика цвета (выбытие пациента и т.д.)
            if col > 0:
                time_point = self.time_slots[col-1]
                # TODO: проверка выбытия пациента
                pass

        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal:
            if section == 0:
                if role == Qt.DisplayRole:
                    return "Назначение"
            else:
                if role == Qt.DisplayRole:
                    return self.time_slots[section-1].strftime("%H")
        return None

    def _fetch_latest_admin_rows(self, delta_only=False):
        """
        Возвращает актуальные (последние по id) строки administrations для текущей смены.
        Если delta_only=True, возвращает только измененные с момента last_sync_ts.
        """
        updated_after = self.last_sync_cursor if delta_only else None
        return self.service.get_latest_administrations(
            admission_id=self.admission_id,
            shift_date=self.shift_date,
            only_committed=self.only_committed,
            include_deleted=True,
            include_cancelled=True,
            include_deleted_orders=True,
            updated_after=updated_after,
        )

    def _build_admin_map(self, rows) -> Dict[Tuple[int, str], AdministrationDTO]:
        result: Dict[Tuple[int, str], AdministrationDTO] = {}
        for r in rows:
            rd = dict(r)
            if rd.get('status') == 'deleted':
                continue

            key = self._row_to_admin_key(rd)
            result[key] = self._row_to_admin_dto(rd)
        return result

    def _row_to_admin_key(self, rd: Dict) -> Tuple[int, str]:
        planned_dt = datetime.fromisoformat(rd['planned_time'])
        return (rd['order_id'], planned_dt.isoformat())

    def _row_to_admin_dto(self, rd: Dict) -> AdministrationDTO:
        planned_dt = datetime.fromisoformat(rd['planned_time'])
        actual_raw = rd.get('actual_time')
        actual_dt = datetime.fromisoformat(actual_raw) if actual_raw else None
        return AdministrationDTO(
            id=rd['id'],
            order_id=rd['order_id'],
            chain_id=rd.get('chain_id'),
            big_chain_id=rd.get('big_chain_id'),
            cell_role=rd['cell_role'],
            planned_time=planned_dt,
            actual_time=actual_dt,
            performer_id=rd.get('performer_id'),
            status=rd['status'],
            is_committed=rd.get('is_committed', 0),
            comment=rd.get('comment') or "",
            volume_ml=rd.get('volume_ml', 0.0),
            updated_at=rd.get('updated_at'),
            last_modified_by=rd.get('last_modified_by')
        )

    def _compute_sync_cursor(self, rows) -> Dict[str, object]:
        max_cursor = make_sync_cursor(EPOCH_SYNC_TS, 0)
        for row in rows:
            rd = dict(row)
            row_updated_at = str(rd.get("updated_at") or max_cursor["updated_at"])
            row_id = int(rd.get("id") or 0)
            if is_cursor_newer(row_updated_at, row_id, max_cursor["updated_at"], max_cursor["id"]):
                max_cursor = make_sync_cursor(row_updated_at, row_id)
        return max_cursor

    def clear_for_context(self, admission_id, shift_date):
        self.beginResetModel()
        self.admission_id = admission_id
        self.shift_date = shift_date
        self.orders = []
        self.admin_map = {}
        self.has_any_draft = False
        self.last_sync_cursor = make_sync_cursor(EPOCH_SYNC_TS, 0)
        self.last_sync_ts = self.last_sync_cursor["updated_at"]
        self._rebuild_time_slots()
        self.endResetModel()

    def apply_snapshot(self, snapshot: Dict[str, object]):
        self.beginResetModel()
        try:
            self.admission_id = snapshot.get("admission_id", self.admission_id)
            self.shift_date = snapshot.get("shift_date", self.shift_date)
            self.only_committed = bool(snapshot.get("only_committed", self.only_committed))
            self._rebuild_time_slots()
            self.orders = [copy(order) for order in (snapshot.get("orders") or [])]
            self._renumber_local_sort_order()
            admin_rows = list(snapshot.get("admin_rows") or [])
            self.admin_map = self._build_admin_map(admin_rows)
            self.has_any_draft = bool(snapshot.get("has_any_draft", False))
            sync_cursor = self._compute_sync_cursor(admin_rows)
            self.last_sync_cursor = sync_cursor
            self.last_sync_ts = sync_cursor["updated_at"]
        finally:
            self.endResetModel()

    def _current_order_signature(self):
        return [self._order_signature(order) for order in self.orders]

    @staticmethod
    def _order_signature(order):
        return (
            getattr(order, "id", None),
            getattr(getattr(order, "status", None), "value", getattr(order, "status", None)),
            int(getattr(order, "is_committed", 0) or 0),
            getattr(order, "draft_sort_order", None),
        )

    @classmethod
    def _orders_signature(cls, orders):
        return [cls._order_signature(order) for order in (orders or [])]

    def _emit_admin_cell_changes(self, changed_keys):
        if not changed_keys:
            return
        row_lookup = {
            order.id: idx
            for idx, order in enumerate(self.orders)
            if order and order.id is not None
        }
        col_lookup = {
            slot.isoformat(): col + 1
            for col, slot in enumerate(self.time_slots)
        }
        emitted = set()
        for order_id, planned_iso in changed_keys:
            row_idx = row_lookup.get(order_id)
            col_idx = col_lookup.get(planned_iso)
            if row_idx is None or col_idx is None:
                continue
            cell_key = (row_idx, col_idx)
            if cell_key in emitted:
                continue
            emitted.add(cell_key)
            cell_idx = self.index(row_idx, col_idx)
            self.dataChanged.emit(cell_idx, cell_idx, [Qt.UserRole])

    def apply_admin_rows_snapshot(self, snapshot: Dict[str, object]) -> bool:
        """
        Применяет snapshot без reset модели, если список назначений не изменился.
        Это сохраняет позицию таблицы и не съедает быстрые клики по отметкам.
        """
        snapshot_orders = [copy(order) for order in (snapshot.get("orders") or [])]
        for idx, order in enumerate(snapshot_orders):
            if order is not None:
                order.sort_order = idx

        if self._current_order_signature() != self._orders_signature(snapshot_orders):
            return False
        if snapshot.get("admission_id", self.admission_id) != self.admission_id:
            return False
        if snapshot.get("shift_date", self.shift_date) != self.shift_date:
            return False
        if bool(snapshot.get("only_committed", self.only_committed)) != bool(self.only_committed):
            return False

        admin_rows = list(snapshot.get("admin_rows") or [])
        new_admin_map = self._build_admin_map(admin_rows)
        changed_keys = {
            key
            for key in set(self.admin_map.keys()) | set(new_admin_map.keys())
            if self.admin_map.get(key) != new_admin_map.get(key)
        }

        self.admin_map = new_admin_map
        self.has_any_draft = bool(snapshot.get("has_any_draft", False))
        sync_cursor = self._compute_sync_cursor(admin_rows)
        self.last_sync_cursor = sync_cursor
        self.last_sync_ts = sync_cursor["updated_at"]

        self._emit_admin_cell_changes(changed_keys)
        return True

    def refresh_admin_marks_only(self) -> bool:
        """
        Легкое Delta-обновление: подтягивает только измененные ячейки из БД.
        Возвращает True, если данные реально изменились.
        """
        if not self.orders or not self.time_slots:
            from rem_card.app.logger import logger
            logger.info(
                "[OrdersClick] model_delta_skip admission_id=%s reason=empty rows=%s slots=%s",
                self.admission_id,
                len(self.orders),
                len(self.time_slots),
            )
            return False

        try:
            started = datetime.now()
            # Используем Delta-загрузку по updated_at
            rows = self._fetch_latest_admin_rows(delta_only=True)
            if not rows:
                from rem_card.app.logger import logger
                logger.info(
                    "[OrdersClick] model_delta_no_rows admission_id=%s cursor=%s",
                    self.admission_id,
                    self.last_sync_cursor,
                )
                return False

            changed = False
            changed_cells: List[Tuple[int, int]] = []
            row_lookup: Dict[int, int] = {
                order.id: idx
                for idx, order in enumerate(self.orders)
                if order and order.id is not None
            }
            col_lookup: Dict[str, int] = {
                slot.isoformat(): col + 1
                for col, slot in enumerate(self.time_slots)
            }

            # Мерджим изменения в существующий admin_map.
            # ВАЖНО: для status='deleted' удаляем ключ, иначе "призрак" ячейки остается в UI.
            new_sync_cursor = dict(self.last_sync_cursor)
            for row in rows:
                rd = dict(row)
                row_updated_at = str(rd.get("updated_at") or new_sync_cursor["updated_at"])
                row_id = int(rd.get("id") or 0)
                if is_cursor_newer(row_updated_at, row_id, new_sync_cursor["updated_at"], new_sync_cursor["id"]):
                    new_sync_cursor = make_sync_cursor(row_updated_at, row_id)

                key = self._row_to_admin_key(rd)
                status = rd.get('status')
                row_idx = row_lookup.get(rd.get("order_id"))
                col_idx = col_lookup.get(key[1])

                if status == 'deleted':
                    if key in self.admin_map:
                        del self.admin_map[key]
                        changed = True
                        if row_idx is not None and col_idx is not None:
                            changed_cells.append((row_idx, col_idx))
                    continue

                new_admin = self._row_to_admin_dto(rd)
                old_admin = self.admin_map.get(key)
                if old_admin != new_admin:
                    self.admin_map[key] = new_admin
                    changed = True
                    if row_idx is not None and col_idx is not None:
                        changed_cells.append((row_idx, col_idx))

            if not changed:
                self.last_sync_cursor = new_sync_cursor
                self.last_sync_ts = new_sync_cursor["updated_at"]
                from rem_card.app.logger import logger
                logger.info(
                    "[OrdersClick] model_delta_no_change admission_id=%s rows=%s cursor=%s",
                    self.admission_id,
                    len(rows),
                    self.last_sync_cursor,
                )
                return False

            self._recompute_draft_flag()
            self.last_sync_cursor = new_sync_cursor
            self.last_sync_ts = new_sync_cursor["updated_at"]

            # По возможности дергаем repaint только для реально измененных ячеек.
            # Это заметно снижает лаг на больших листах назначений.
            if changed_cells:
                emitted = set()
                for row_idx, col_idx in changed_cells:
                    cell_key = (row_idx, col_idx)
                    if cell_key in emitted:
                        continue
                    emitted.add(cell_key)
                    cell_idx = self.index(row_idx, col_idx)
                    self.dataChanged.emit(cell_idx, cell_idx, [Qt.UserRole])
            elif self.rowCount() > 0 and self.columnCount() > 1:
                # Fallback для редких случаев, когда изменились данные вне видимой сетки.
                top_left = self.index(0, 1)
                bottom_right = self.index(self.rowCount() - 1, self.columnCount() - 1)
                self.dataChanged.emit(top_left, bottom_right, [Qt.UserRole])
                
            from rem_card.app.logger import logger
            logger.info(
                "[OrdersClick] model_delta_changed admission_id=%s rows=%s changed_cells=%s cursor=%s elapsed_ms=%s",
                self.admission_id,
                len(rows),
                len(changed_cells),
                self.last_sync_cursor,
                round((datetime.now() - started).total_seconds() * 1000.0, 1),
            )
            return True
        except Exception as e:
            from rem_card.app.logger import logger
            logger.error(f"[OrdersModel] Error in refresh_admin_marks_only: {e}", exc_info=True)
            return False

    def refresh(self):
        from rem_card.app.logger import logger
        logger.debug(f"[OrdersModel] Start refresh for ID {self.admission_id}, date {self.shift_date}, only_committed={self.only_committed}")
        
        self.beginResetModel()
        
        # Обновляем временную сетку (часы) для текущей даты
        self._rebuild_time_slots()
            
        try:
            # 1. Получаем заказы
            all_orders = self.service.get_orders(self.admission_id, self.shift_date, only_committed=self.only_committed)
            if self.only_committed:
                # В режиме медсестры/печати доверяем выборке из БД (она учитывает черновики удаления)
                self.orders = [o for o in all_orders if o]
            else:
                # В режиме врача скрываем то, что помечено на удаление
                self.orders = [o for o in all_orders if o and o.status != OrderStatus.DELETED]
            self._renumber_local_sort_order()
                
            logger.debug(f"[OrdersModel] Loaded {len(self.orders)} orders (only_committed={self.only_committed})")
        except Exception as e:
            logger.error(f"[OrdersModel] Error in OrdersModel.refresh (orders section): {e}", exc_info=True)
            self.orders = []
        
        rows = self._fetch_latest_admin_rows()
        self.admin_map = self._build_admin_map(rows)
        self._recompute_draft_flag()
        if not self.only_committed and hasattr(self.service, "has_order_drafts"):
            try:
                self.has_any_draft = self.has_any_draft or bool(
                    self.service.has_order_drafts(self.admission_id, self.shift_date)
                )
            except Exception:
                pass
        
        # Инициализируем last_sync_ts при полном рефреше
        if rows:
            max_cursor = self._compute_sync_cursor(rows)
            self.last_sync_cursor = max_cursor
            self.last_sync_ts = max_cursor["updated_at"]
        else:
            self.last_sync_cursor = make_sync_cursor(EPOCH_SYNC_TS, 0)
            self.last_sync_ts = self.last_sync_cursor["updated_at"]
        self.endResetModel()

    def move_order_row(self, source_row: int, target_row: int, *, mark_draft: bool = True) -> bool:
        if source_row < 0 or source_row >= len(self.orders):
            return False
        target_row = max(0, min(int(target_row), len(self.orders) - 1))
        if source_row == target_row:
            return False

        self.beginResetModel()
        order = self.orders.pop(source_row)
        self.orders.insert(target_row, order)
        self._renumber_local_sort_order()
        if mark_draft:
            self.has_any_draft = True
        self.endResetModel()
        return True

    def reorder_by_order_ids(self, ordered_ids, *, mark_draft: bool = False) -> bool:
        if not ordered_ids or not self.orders:
            return False

        desired = [int(order_id) for order_id in ordered_ids if order_id is not None]
        if not desired:
            return False

        current_ids = [int(order.id) for order in self.orders if order and order.id is not None]
        if current_ids == desired:
            if mark_draft:
                self.has_any_draft = True
            return False

        desired_set = set(desired)
        by_id = {
            int(order.id): order
            for order in self.orders
            if order and order.id is not None
        }
        ordered = [by_id[order_id] for order_id in desired if order_id in by_id]
        remaining = [
            order
            for order in self.orders
            if not order or order.id is None or int(order.id) not in desired_set
        ]
        if not ordered:
            return False

        self.beginResetModel()
        self.orders = ordered + remaining
        self._renumber_local_sort_order()
        if mark_draft:
            self.has_any_draft = True
        self.endResetModel()
        return True
