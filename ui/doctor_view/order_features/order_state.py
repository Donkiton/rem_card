"""OrderStateMixin for the doctor orders widget."""

from PySide6.QtCore import QModelIndex
from copy import deepcopy
from rem_card.app.logger import logger
from rem_card.data.dto.remcard_dto import OrderDTO
from rem_card.data.dto.remcard_dto import OrderStatus


class OrderStateMixin:
    def _is_read_only(self) -> bool:
        """
        Проверяет, заблокирована ли карта для редактирования.
        В данной версии редактирование назначений врачом разрешено ВСЕГДА, 
        независимо от статуса пациента (в т.ч. при Исходе).
        """
        return bool(self._forced_read_only or self._legacy_central_draft_detected)

    def set_forced_read_only(self, enabled: bool):
        self._forced_read_only = bool(enabled)
        if hasattr(self, "input_widget") and self.input_widget is not None:
            self.input_widget.setEnabled(not self._forced_read_only and not self._legacy_central_draft_detected)
        if hasattr(self, "table_view") and self.table_view is not None:
            self.table_view.viewport().update()

    def has_drafts(self) -> bool:
        if self.model is not None:
            return bool(
                self._has_local_draft_changes()
                or self._cached_has_drafts
                or getattr(self.model, "has_any_draft", False)
            )
        return False

    def has_administrations(self) -> bool:
        if self.model is not None:
            return bool(self._cached_has_administrations)
        return False

    def _model_has_administrations(self) -> bool:
        if self.model is None:
            return False
        return any(
            admin is not None and str(getattr(admin, "status", "") or "") not in ("deleted", "cancelled")
            for admin in getattr(self.model, "admin_map", {}).values()
        )

    def has_orders(self) -> bool:
        if self.model is not None:
            return bool(self._cached_has_orders)
        return False

    def _known_current_context_without_drafts(self) -> bool:
        if self.model is None:
            return False
        if self.model.admission_id != self.admission_id or self.model.shift_date != self.shift_date:
            return False
        if self.has_drafts():
            return False
        return bool(
            self._last_applied_snapshot_signature is not None
            or self._cached_has_orders
            or self._cached_has_administrations
            or getattr(self.model, "orders", None)
            or getattr(self.model, "admin_map", None)
        )

    def _source_has_order_drafts(self) -> bool | None:
        if not self.service or not self.admission_id:
            return None
        checker = getattr(self.service, "has_order_drafts", None)
        if not callable(checker):
            return None
        try:
            return bool(checker(self.admission_id, self.shift_date))
        except Exception as exc:
            logger.debug("OrdersWidget draft probe failed: %s", exc)
            return None

    @staticmethod
    def _is_committed_value(value) -> bool:
        try:
            return bool(int(value or 0))
        except Exception:
            return str(value or "").strip().lower() in {"1", "true", "yes"}

    def check_drafts(self):
        self.draftStatusChanged.emit(self.has_drafts())
        self.administrationStatusChanged.emit(self.has_administrations())
        self.ordersPresenceChanged.emit(self.has_orders())
        self._try_apply_pending_structure_sync()

    def _visible_order_ids(self):
        if not self.model:
            return []
        return [
            int(order.id)
            for order in self.model.orders
            if order and order.id is not None and order.status != OrderStatus.DELETED
        ]

    def _visible_order_revision_map(self, order_ids=None):
        if not self.model:
            return {}
        allowed = {int(item) for item in order_ids if item is not None} if order_ids is not None else None
        revisions = {}
        for order in self.model.orders:
            order_id = getattr(order, "id", None)
            if order_id is None:
                continue
            order_id = int(order_id)
            if order_id <= 0:
                continue
            if allowed is not None and order_id not in allowed:
                continue
            revisions[order_id] = int(getattr(order, "revision", 0) or 0)
        return revisions

    def _is_current_context(self, admission_id, shift_date) -> bool:
        try:
            same_admission = int(self.admission_id or 0) == int(admission_id or 0)
        except Exception:
            same_admission = self.admission_id == admission_id
        return same_admission and self.shift_date == shift_date

    def _refresh_model_if_current(self, admission_id, shift_date):
        if self._is_current_context(admission_id, shift_date):
            self._refresh_model()

    def _clear_pending_reorder(self):
        self._pending_reorder_order_ids = []

    def _mark_local_reorder_draft(self):
        self._pending_reorder_order_ids = self._visible_order_ids()
        self._local_draft_dirty_order_ids.update(self._pending_reorder_order_ids)
        self._cached_has_drafts = True
        if self.model is not None:
            if hasattr(self.model, "_set_has_any_draft"):
                self.model._set_has_any_draft(True, emit_order_column=True)
            else:
                self.model.has_any_draft = True
        self.check_drafts()

    def _persist_reorder_draft(self):
        # Reordering is part of the in-memory overlay.  The final order is
        # persisted together with the rest of the draft by finalize_card().
        return

    def _apply_pending_reorder_to_model(self):
        if not self._pending_reorder_order_ids or not self.model:
            return False
        changed = self.model.reorder_by_order_ids(
            self._pending_reorder_order_ids,
            mark_draft=True,
        )
        self._cached_has_drafts = True
        return changed

    def _mark_local_order_row_deleted(self, row: int, order: OrderDTO, *, was_committed: bool):
        if not self.model or row < 0 or row >= len(self.model.orders):
            return

        order_id = int(getattr(order, "id"))
        tombstone = deepcopy(order)
        setattr(tombstone, "_pending_delete", True)

        self.model.beginRemoveRows(QModelIndex(), row, row)
        try:
            self.model.orders.pop(row)
        finally:
            self.model.endRemoveRows()

        self._pending_reorder_order_ids = [
            candidate_id
            for candidate_id in self._pending_reorder_order_ids
            if int(candidate_id) != order_id
        ]
        removed_admin_keys = [
            key for key in self.model.admin_map
            if int(key[0]) == order_id
        ]
        for key in removed_admin_keys:
            self.model.admin_map.pop(key, None)
            self._local_draft_dirty_admin_keys.discard((int(key[0]), str(key[1])))
        if order_id > 0:
            self._local_deleted_orders[order_id] = (row, tombstone)
            self._local_draft_dirty_order_ids.add(order_id)
        else:
            self._local_deleted_orders.pop(order_id, None)
            self._local_draft_dirty_order_ids.discard(order_id)

        has_local_draft = self._has_local_draft_changes()
        if hasattr(self.model, "_set_has_any_draft"):
            self.model._set_has_any_draft(has_local_draft, emit_order_column=True)
        else:
            self.model.has_any_draft = has_local_draft
        self._cached_has_drafts = has_local_draft
        self._cached_has_orders = any(
            item is not None and item.status != OrderStatus.DELETED
            for item in self.model.orders
        )
        self._cached_has_administrations = self._model_has_administrations()
        self.check_drafts()
        self.localBalanceChanged.emit()

    def _clear_local_order_row_pending_delete(self, order_id):
        if self.model is None:
            return
        entry = self._local_deleted_orders.pop(int(order_id), None)
        if entry is None:
            return
        original_row, order = entry
        if hasattr(order, "_pending_delete"):
            delattr(order, "_pending_delete")
        insert_row = max(0, min(int(original_row), len(self.model.orders)))
        self.model.beginInsertRows(QModelIndex(), insert_row, insert_row)
        try:
            self.model.orders.insert(insert_row, order)
        finally:
            self.model.endInsertRows()
        restored_admin_keys = []
        for key, admin in self._draft_baseline_admin_map.items():
            if int(key[0]) != int(order_id):
                continue
            self.model.admin_map[key] = deepcopy(admin)
            restored_admin_keys.append(key)
        if restored_admin_keys:
            self._emit_admin_cell_changes(restored_admin_keys, mark_draft=False)
        self._local_draft_dirty_order_ids.discard(int(order_id))
        has_local_draft = self._has_local_draft_changes()
        if hasattr(self.model, "_set_has_any_draft"):
            self.model._set_has_any_draft(has_local_draft, emit_order_column=True)
        else:
            self.model.has_any_draft = has_local_draft
        self._cached_has_drafts = has_local_draft
        self._cached_has_orders = bool(self.model.orders)
        self.check_drafts()
        self.localBalanceChanged.emit()

    def _mark_pending_structure_sync(self, change_id: int):
        try:
            change_id_int = int(change_id or 0)
        except Exception:
            change_id_int = 0
        if change_id_int <= 0:
            return
        if change_id_int > self._pending_structure_change_id:
            self._pending_structure_change_id = change_id_int

    def _try_apply_pending_structure_sync(self):
        if self._applying_pending_structure_sync:
            return
        if self._pending_structure_change_id <= 0:
            return
        if not self.model or not self.admission_id or not self.service:
            return
        if self.has_drafts():
            return

        self._applying_pending_structure_sync = True
        try:
            logger.info(
                "[OrdersWidget] Applying deferred external structure sync: pending=%s",
                self._pending_structure_change_id,
            )
            self.request_refresh(force=True)
            self._pending_structure_change_id = 0
        except Exception:
            logger.exception("[OrdersWidget] Failed to apply deferred structure sync")
        finally:
            self._applying_pending_structure_sync = False
