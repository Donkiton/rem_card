"""DraftManagementMixin for the doctor orders widget."""

from PySide6.QtCore import QModelIndex
from PySide6.QtCore import Qt
from copy import copy
from copy import deepcopy
from datetime import datetime
from rem_card.app.local_metrics import record_metric
from rem_card.app.logger import logger
from rem_card.data.dto.remcard_dto import AdministrationDTO
from rem_card.data.dto.remcard_dto import OrderDTO
from rem_card.data.dto.remcard_dto import OrderStatus
from rem_card.data.dto.remcard_dto import OrderType
from rem_card.services.order_domain_service import NURSE_MARK_EXECUTED
from rem_card.services.order_domain_service import NURSE_MARK_NOT_EXECUTED
from rem_card.services.order_service import OrderConflictError
from rem_card.services.orders_sync_observability import record_orders_sync_event
import time


class DraftManagementMixin:
    def _current_model_order_ids(self):
        if not self.model:
            return []
        return [o.id for o in self.model.orders if o and o.status != OrderStatus.DELETED]

    def _reset_change_cursor(self):
        self._last_polled_change_id = 0
        self._last_polled_context_key = None
        self._last_poll_monotonic = 0.0

    def _reset_cached_state(self):
        self._cached_has_drafts = False
        self._cached_has_administrations = False
        self._cached_has_orders = False
        self._last_applied_snapshot_signature = None
        self._legacy_central_draft_detected = False
        self._pending_admin_cell_write_keys.clear()
        self._recent_admin_cell_clicks.clear()
        self._discard_deferred_forced_reload_after_guard(discard_reason="context_reset")
        self._clear_local_cell_draft_guard()
        self._reset_local_draft_tracking(clear_baseline=True)

    def _reset_local_draft_tracking(self, *, clear_baseline: bool = False):
        self._local_draft_dirty_order_ids.clear()
        self._local_draft_dirty_admin_keys.clear()
        self._local_deleted_orders.clear()
        self._next_local_order_id = -1
        self._next_local_admin_id = -1
        if clear_baseline:
            self._draft_baseline_snapshot = None
            self._draft_baseline_admin_map = {}

    def _capture_local_draft_baseline(self, snapshot):
        if self._has_local_draft_changes() or self.model is None or self._legacy_central_draft_detected:
            return
        baseline = dict(snapshot or {})
        baseline["orders"] = deepcopy(list(snapshot.get("orders") or []))
        baseline["admin_rows"] = [dict(row) for row in (snapshot.get("admin_rows") or [])]
        baseline["has_any_draft"] = False
        baseline["only_committed"] = True
        self._draft_baseline_snapshot = baseline
        self._draft_baseline_admin_map = {
            key: deepcopy(value)
            for key, value in getattr(self.model, "admin_map", {}).items()
        }
        self._reset_local_draft_tracking(clear_baseline=False)

    def _has_local_draft_changes(self) -> bool:
        return bool(
            self._local_draft_dirty_order_ids
            or self._local_draft_dirty_admin_keys
            or self._pending_reorder_order_ids
        )

    def _allocate_local_order_id(self) -> int:
        value = int(self._next_local_order_id)
        self._next_local_order_id -= 1
        return value

    def _allocate_local_admin_id(self) -> int:
        value = int(self._next_local_admin_id)
        self._next_local_admin_id -= 1
        return value

    def _mark_local_order_dirty(self, order_id):
        if order_id is None:
            return
        self._local_draft_dirty_order_ids.add(int(order_id))
        self._cached_has_drafts = True
        if self.model is not None:
            self.model._set_has_any_draft(True, emit_order_column=True)
        self._local_cell_draft_guard = True
        self.check_drafts()

    def _mark_local_admin_dirty(self, keys):
        for key in keys or ():
            if key is None:
                continue
            normalized_key = (int(key[0]), str(key[1]))
            current = self.model.admin_map.get(normalized_key) if self.model is not None else None
            baseline = self._draft_baseline_admin_map.get(normalized_key)

            def shape(admin):
                if admin is None:
                    return None
                status = str(getattr(admin, "status", "") or "")
                if status in {"deleted", "cancelled"} and baseline is None:
                    return None
                return (
                    status,
                    str(getattr(admin, "cell_role", "") or ""),
                    str(getattr(admin, "big_chain_id", "") or ""),
                    float(getattr(admin, "volume_ml", 0.0) or 0.0),
                )

            if shape(current) == shape(baseline):
                self._local_draft_dirty_admin_keys.discard(normalized_key)
                if self.model is not None:
                    if baseline is None:
                        self.model.admin_map.pop(normalized_key, None)
                    else:
                        self.model.admin_map[normalized_key] = deepcopy(baseline)
                continue
            self._local_draft_dirty_admin_keys.add(normalized_key)
        if self._local_draft_dirty_admin_keys:
            self._cached_has_drafts = True
            self._local_cell_draft_guard = True

    def _build_local_draft_payload(self):
        if self.model is None:
            return None
        deleted_orders = [
            deepcopy(entry[1])
            for entry in self._local_deleted_orders.values()
        ]
        expected_revisions = self._visible_order_revision_map()
        for deleted_order in deleted_orders:
            order_id = int(getattr(deleted_order, "id", 0) or 0)
            if order_id > 0:
                expected_revisions[order_id] = int(getattr(deleted_order, "revision", 0) or 0)
        return {
            "orders": deepcopy(list(self.model.orders)) + deleted_orders,
            "admin_map": deepcopy(dict(self.model.admin_map)),
            "dirty_admin_keys": tuple(sorted(self._local_draft_dirty_admin_keys)),
            "baseline_admin_map": deepcopy(dict(self._draft_baseline_admin_map)),
            "expected_revisions": expected_revisions,
            "expected_active_order_ids": tuple(sorted(
                int(order.id)
                for order in (self._draft_baseline_snapshot or {}).get("orders", ())
                if order is not None
                and getattr(order, "id", None) is not None
                and int(order.id) > 0
                and self._is_committed_value(getattr(order, "is_committed", 0))
                and getattr(order, "status", None) not in {OrderStatus.DELETED, OrderStatus.CANCELLED}
            )),
        }

    def is_draft_save_pending(self) -> bool:
        return bool(self._local_draft_save_pending)

    def _restore_local_draft_baseline(self) -> bool:
        if self.model is None or not self._draft_baseline_snapshot:
            return False
        scroll_value = self._capture_table_scroll()
        self.model.apply_snapshot(deepcopy(self._draft_baseline_snapshot))
        self._clear_pending_reorder()
        self._reset_local_draft_tracking(clear_baseline=False)
        self._cached_has_drafts = False
        self._cached_has_administrations = self._model_has_administrations()
        self._cached_has_orders = any(
            order and order.status != OrderStatus.DELETED
            for order in self.model.orders
        )
        self._clear_local_cell_draft_guard()
        self._restore_table_scroll(scroll_value)
        self.check_drafts()
        self.localBalanceChanged.emit()
        return True

    def _order_has_committed_execution(self, order_id) -> bool:
        if self.model is None or order_id is None:
            return False
        for (candidate_id, _planned), admin in self.model.admin_map.items():
            if int(candidate_id) != int(order_id) or admin is None:
                continue
            if not self._is_committed_value(getattr(admin, "is_committed", 0)):
                continue
            if str(getattr(admin, "comment", "") or "") in {
                NURSE_MARK_EXECUTED,
                NURSE_MARK_NOT_EXECUTED,
            }:
                return True
        return False

    def _clone_order_as_local_draft(self, source: OrderDTO, *, created_at: datetime | None = None) -> OrderDTO:
        order = deepcopy(source)
        order.id = self._allocate_local_order_id()
        order.admission_id = int(self.admission_id)
        order.is_committed = 0
        order.revision = 0
        order.draft_sort_order = None
        order.status = OrderStatus.ACTIVE
        order.created_at = created_at or getattr(order, "created_at", None) or datetime.now()
        order.administrations = []
        if hasattr(order, "_pending_delete"):
            delattr(order, "_pending_delete")
        return order

    def _insert_local_orders_batch(
        self,
        orders,
        *,
        replace_existing: bool = False,
        source_admin_rows=None,
        source_shift_date: datetime | None = None,
    ):
        self._ensure_model_initialized()
        if self.model is None:
            return False
        sources = list(orders or ())
        if not sources:
            return False
        if replace_existing:
            blocked = [
                order for order in self.model.orders
                if order is not None
                and int(getattr(order, "id", 0) or 0) > 0
                and self._order_has_committed_execution(order.id)
            ]
            if blocked:
                self._show_warning(
                    "Лист содержит уже выполненные назначения. Они сохранены как медицинский факт; "
                    "удалите только будущие ячейки или добавьте назначения без полной замены листа."
                )
                return False

        start, end = self.service.get_day_period(self.shift_date)
        now = datetime.now()
        created_at = now if start <= now < end else start
        source_to_local_order_id = {}
        local_orders = []
        for source in sources:
            local_order = self._clone_order_as_local_draft(source, created_at=created_at)
            source_id = getattr(source, "id", None)
            if source_id is not None:
                source_to_local_order_id[int(source_id)] = int(local_order.id)
            local_orders.append(local_order)

        local_admin_rows = []
        if source_admin_rows and source_shift_date is not None:
            source_start, _ = self.service.get_day_period(source_shift_date)
            time_diff = start - source_start
            copied_chain_ids = {}

            def row_value(row, name, default=None):
                if isinstance(row, dict):
                    return row.get(name, default)
                try:
                    return row[name]
                except Exception:
                    return getattr(row, name, default)

            for source_admin in source_admin_rows:
                source_order_id = int(row_value(source_admin, "order_id", 0) or 0)
                local_order_id = source_to_local_order_id.get(source_order_id)
                if local_order_id is None:
                    continue
                source_planned = datetime.fromisoformat(
                    str(row_value(source_admin, "planned_time")).replace(" ", "T")
                )
                local_planned = source_planned + time_diff
                if not (start <= local_planned < end):
                    continue
                source_chain_id = str(row_value(source_admin, "big_chain_id", "") or "")
                local_chain_id = None
                if source_chain_id:
                    chain_key = (source_order_id, source_chain_id)
                    local_chain_id = copied_chain_ids.setdefault(
                        chain_key,
                        f"local-copy:{local_order_id}:{len(copied_chain_ids) + 1}",
                    )
                admin = AdministrationDTO(
                    id=self._allocate_local_admin_id(),
                    order_id=local_order_id,
                    big_chain_id=local_chain_id,
                    cell_role=str(row_value(source_admin, "cell_role", "single") or "single"),
                    planned_time=local_planned,
                    status=str(row_value(source_admin, "status", "planned") or "planned"),
                    is_committed=0,
                    comment="",
                    volume_ml=float(row_value(source_admin, "volume_ml", 0.0) or 0.0),
                )
                key = (local_order_id, local_planned.isoformat())
                local_admin_rows.append((key, admin))

        # Сначала полностью подготавливаем новый пакет. Так ошибка в исходных
        # данных не оставит текущий лист частично помеченным на удаление.
        if replace_existing:
            for row in range(len(self.model.orders) - 1, -1, -1):
                order = self.model.orders[row]
                if order is not None:
                    self._mark_local_order_row_deleted(
                        row,
                        order,
                        was_committed=self._is_committed_value(getattr(order, "is_committed", 0)),
                    )

        for local_order in local_orders:
            self._insert_local_order_after_add(local_order)

        copied_admin_keys = []
        for key, admin in local_admin_rows:
            self.model.admin_map[key] = admin
            copied_admin_keys.append(key)
        if copied_admin_keys:
            self._mark_local_admin_dirty(copied_admin_keys)
            self._emit_admin_cell_changes(copied_admin_keys)
            self.localBalanceChanged.emit()
        return True

    def _clear_local_times(self):
        if self.model is None:
            return
        changed_keys = []
        preserved_fact = False
        for key, admin in list(self.model.admin_map.items()):
            if admin is None or str(getattr(admin, "status", "") or "") in {"deleted", "cancelled"}:
                continue
            if (
                self._is_committed_value(getattr(admin, "is_committed", 0))
                and str(getattr(admin, "comment", "") or "") in {NURSE_MARK_EXECUTED, NURSE_MARK_NOT_EXECUTED}
            ):
                preserved_fact = True
                continue
            baseline = self._draft_baseline_admin_map.get(key)
            if baseline is None:
                self.model.admin_map.pop(key, None)
            else:
                tombstone = deepcopy(admin)
                tombstone.id = self._allocate_local_admin_id()
                tombstone.status = "deleted"
                tombstone.is_committed = 0
                tombstone.comment = ""
                tombstone.actual_time = None
                tombstone.performer_id = None
                self.model.admin_map[key] = tombstone
            changed_keys.append(key)
        if changed_keys:
            self._emit_admin_cell_changes(changed_keys)
        if preserved_fact:
            self._show_warning(
                "Выполненные и отмеченные как невыполненные ячейки оставлены как медицинский факт."
            )

    def _clear_local_orders(self):
        if self.model is None:
            return
        blocked = False
        for row in range(len(self.model.orders) - 1, -1, -1):
            order = self.model.orders[row]
            if order is None or getattr(order, "_pending_delete", False):
                continue
            if int(getattr(order, "id", 0) or 0) > 0 and self._order_has_committed_execution(order.id):
                blocked = True
                continue
            self._mark_local_order_row_deleted(
                row,
                order,
                was_committed=self._is_committed_value(getattr(order, "is_committed", 0)),
            )
        if blocked:
            self._show_warning(
                "Назначения с выполненными введениями оставлены как медицинский факт. "
                "Для них можно убрать только будущие ячейки."
            )

    def finalize_card(self):
        if not self.admission_id or self._forced_read_only: return
        if self._local_draft_save_pending:
            return
        if not self._has_local_draft_changes():
            if self._legacy_central_draft_detected:
                target_admission_id = self.admission_id
                target_shift_date = self.shift_date

                def legacy_success():
                    self._local_draft_save_pending = False
                    if not self._is_current_context(target_admission_id, target_shift_date):
                        return
                    self._legacy_central_draft_detected = False
                    self._cached_has_drafts = False
                    self._refresh_model(source="post_finalize")
                    self.localDraftResolutionFinished.emit(True)

                def legacy_error(_exc):
                    self._local_draft_save_pending = False
                    self.check_drafts()
                    self.localDraftResolutionFinished.emit(False)

                self._local_draft_save_pending = True
                try:
                    self._enqueue_write(
                        f"orders_finalize_legacy:{target_admission_id}",
                        operation=lambda: self.service.finalize_order_card(
                            target_admission_id,
                            shift_date=target_shift_date,
                            expected_revisions=self._visible_order_revision_map(),
                        ),
                        on_success=legacy_success,
                        on_error=legacy_error,
                    )
                except Exception:
                    self._local_draft_save_pending = False
                    raise
            return
        target_admission_id = self.admission_id
        target_shift_date = self.shift_date
        payload = self._build_local_draft_payload()
        if not payload:
            return

        def after_success(result):
            self._local_draft_save_pending = False
            if not self._is_current_context(target_admission_id, target_shift_date):
                return
            from rem_card.app.logger import logger
            logger.info(f"Карта назначений для ID {target_admission_id} успешно сохранена")
            self._reset_local_draft_tracking(clear_baseline=True)
            self._cached_has_drafts = False
            self._discard_deferred_forced_reload_after_guard(discard_reason="post_finalize")
            self._clear_local_cell_draft_guard()
            self._admin_only_snapshot_until = 0.0
            self._clear_pending_reorder()
            self._post_finalize_retry_count = 0
            self._last_applied_snapshot_signature = None

            snapshot = result.get("snapshot") if isinstance(result, dict) else None
            snapshot_applied = False
            if isinstance(snapshot, dict):
                snapshot_admission_id = int(snapshot.get("admission_id") or 0)
                snapshot_shift_date = snapshot.get("shift_date")
                if (
                    snapshot_admission_id == int(target_admission_id)
                    and snapshot_shift_date == target_shift_date
                    and bool(snapshot.get("only_committed", False))
                ):
                    try:
                        coordinator = self._get_read_coordinator()
                        if coordinator is not None:
                            snapshot = coordinator.accept_committed_orders_snapshot(
                                self._build_orders_context(),
                                snapshot,
                            )
                        snapshot_applied = self._apply_snapshot_data(
                            snapshot=snapshot,
                            admission_id=target_admission_id,
                            shift_date=target_shift_date,
                            context_key=self._current_context_key(),
                        )
                    except Exception:
                        logger.exception(
                            "[OrdersWidget] committed snapshot apply failed admission_id=%s",
                            target_admission_id,
                        )

            if snapshot_applied:
                record_metric(
                    "orders_post_finalize_snapshot_apply",
                    1,
                    admission_id=target_admission_id,
                    source="post_finalize",
                    result="success",
                    path="write_result",
                )
            else:
                if self.model is not None:
                    self.model._set_has_any_draft(False, emit_order_column=True)
                record_metric(
                    "orders_post_finalize_snapshot_apply",
                    1,
                    admission_id=target_admission_id,
                    source="post_finalize",
                    result="fallback",
                    path="async_refresh",
                )
                self._refresh_model(source="post_finalize")
            self.localDraftResolutionFinished.emit(True)

        def after_error(exc):
            self._local_draft_save_pending = False
            if isinstance(exc, OrderConflictError):
                coordinator = self._get_read_coordinator()
                conflict_context_hash = None
                if coordinator is not None:
                    coordinator.invalidate_orders_for_admission(
                        target_admission_id,
                        shift_date=target_shift_date,
                        reason=f"write_conflict:{getattr(exc, 'reason', 'unknown')}",
                    )
                    if self._is_current_context(target_admission_id, target_shift_date):
                        conflict_context_hash = self._build_orders_context().hash()
                record_orders_sync_event(
                    "conflict",
                    role="doctor",
                    admission_id=int(target_admission_id or 0),
                    context_hash=conflict_context_hash,
                    reason=str(getattr(exc, "reason", "unknown")),
                    immediate=True,
                )
                if coordinator is not None and self._is_current_context(target_admission_id, target_shift_date):
                    self._reset_local_draft_tracking(clear_baseline=True)
                    self._clear_pending_reorder()
                    self._cached_has_drafts = False
                    if self.model is not None:
                        self.model._set_has_any_draft(False, emit_order_column=True)
                    self._discard_deferred_forced_reload_after_guard(discard_reason="write_conflict")
                    self._clear_local_cell_draft_guard()
                    self._request_snapshot(
                        force=True,
                        source="write_conflict",
                        priority="HIGH",
                        invalidate_reason="write_conflict",
                    )
            self.check_drafts()
            self.localDraftResolutionFinished.emit(False)

        self._local_draft_save_pending = True
        try:
            self._enqueue_write(
                f"orders_finalize:{target_admission_id}",
                operation=lambda data=payload: self.service.commit_local_order_draft(
                    target_admission_id,
                    target_shift_date,
                    orders=data["orders"],
                    admin_map=data["admin_map"],
                    dirty_admin_keys=data["dirty_admin_keys"],
                    baseline_admin_map=data["baseline_admin_map"],
                    expected_revisions=data["expected_revisions"],
                    expected_active_order_ids=data["expected_active_order_ids"],
                ),
                on_success=after_success,
                on_error=after_error,
                pass_result_to_success=True,
            )
        except Exception:
            self._local_draft_save_pending = False
            raise

    def clear_drafts(self):
        if not self.admission_id or self._forced_read_only: return
        if self._local_draft_save_pending:
            return
        if not self._has_local_draft_changes():
            if self._legacy_central_draft_detected:
                target_admission_id = self.admission_id
                target_shift_date = self.shift_date

                def legacy_success():
                    self._local_draft_save_pending = False
                    if not self._is_current_context(target_admission_id, target_shift_date):
                        return
                    self._legacy_central_draft_detected = False
                    self._cached_has_drafts = False
                    self._refresh_model(source="orders_discard_legacy_draft")
                    self.localDraftResolutionFinished.emit(True)

                def legacy_error(_exc):
                    self._local_draft_save_pending = False
                    self.check_drafts()
                    self.localDraftResolutionFinished.emit(False)

                self._local_draft_save_pending = True
                try:
                    self._enqueue_write(
                        f"orders_discard_legacy:{target_admission_id}",
                        operation=lambda: self.service.clear_order_drafts(
                            target_admission_id,
                            target_shift_date,
                            expected_revisions=self._visible_order_revision_map(),
                        ),
                        on_success=legacy_success,
                        on_error=legacy_error,
                    )
                except Exception:
                    self._local_draft_save_pending = False
                    raise
            return
        if not self._restore_local_draft_baseline():
            self.request_refresh(force=True, source="orders_discard_local_draft", priority="HIGH")

    def _insert_local_order_after_add(self, order: OrderDTO):
        if order is None or getattr(order, "id", None) is None:
            logger.warning(
                "[OrdersWidget] local order insert skipped: id unavailable admission_id=%s",
                self.admission_id,
            )
            self._schedule_fast_sync()
            return
        self._ensure_model_initialized()
        if self.model is None:
            self._schedule_fast_sync()
            return

        order_id = int(order.id)
        if any(existing and getattr(existing, "id", None) is not None and int(existing.id) == order_id for existing in self.model.orders):
            self._schedule_fast_sync()
            return

        scroll_value = self._capture_table_scroll()
        row = len(self.model.orders)
        draft_changed = False
        local_order = copy(order)
        local_order.sort_order = max(
            (int(getattr(existing, "sort_order", 0) or 0) for existing in self.model.orders if existing is not None),
            default=-1,
        ) + 1
        self.model.beginInsertRows(QModelIndex(), row, row)
        try:
            self.model.orders.append(local_order)
            if hasattr(self.model, "_recompute_draft_flag"):
                draft_changed = bool(self.model._recompute_draft_flag())
        finally:
            self.model.endInsertRows()
        if draft_changed and hasattr(self.model, "_emit_order_column_changes"):
            self.model._emit_order_column_changes()

        self._cached_has_drafts = bool(getattr(self.model, "has_any_draft", False)) or bool(self._pending_reorder_order_ids)
        self._cached_has_orders = any(
            item and item.status != OrderStatus.DELETED
            for item in self.model.orders
        )
        self._cached_has_administrations = self._model_has_administrations()
        self._admin_only_snapshot_until = time.monotonic() + self._admin_only_snapshot_window_sec
        self._apply_table_header_layout()
        self._restore_table_scroll(scroll_value)
        self._mark_local_order_dirty(order_id)
        self.check_drafts()
        self.localBalanceChanged.emit()

    def _replace_local_order_after_edit(self, row: int, order_id: int, updated_order: OrderDTO):
        self._ensure_model_initialized()
        if self.model is None or updated_order is None:
            self._schedule_fast_sync()
            return

        target_row = row
        if target_row < 0 or target_row >= len(self.model.orders) or getattr(self.model.orders[target_row], "id", None) != order_id:
            target_row = next(
                (
                    idx
                    for idx, item in enumerate(self.model.orders)
                    if item and getattr(item, "id", None) == order_id
                ),
                -1,
            )
        if target_row < 0:
            self._schedule_fast_sync()
            return

        local_order = copy(updated_order)
        local_order.id = order_id
        local_order.admission_id = self.admission_id
        self.model.orders[target_row] = local_order
        self._mark_local_order_dirty(order_id)
        if hasattr(self.model, "_recompute_draft_flag"):
            self.model._recompute_draft_flag(emit_order_column=True)
        self._cached_has_drafts = bool(getattr(self.model, "has_any_draft", False))
        self._cached_has_orders = any(
            item and item.status != OrderStatus.DELETED
            for item in self.model.orders
        )
        self._cached_has_administrations = self._model_has_administrations()
        self._last_applied_snapshot_signature = None
        idx_left = self.model.index(target_row, 0)
        idx_right = self.model.index(target_row, max(0, self.model.columnCount() - 1))
        self.model.dataChanged.emit(idx_left, idx_right, [Qt.UserRole])
        self.check_drafts()
        self.localBalanceChanged.emit()

    def on_prescription_input(self, text):
        if self._is_read_only(): return
        from rem_card.ui.doctor_view.components.order_input_handler import OrderInputHandler

        target_admission_id = self.admission_id
        target_shift_date = self.shift_date
        new_order = OrderInputHandler.parse_input_to_dto(text, self.admission_id)
        new_order.id = self._allocate_local_order_id()
        new_order.is_committed = 0
        now = datetime.now()
        start, end = self.service.get_day_period(self.shift_date)
        new_order.created_at = now if start <= now < end else start

        if self._is_current_context(target_admission_id, target_shift_date):
            self._insert_local_order_after_add(new_order)

    def has_cvp_order(self) -> bool:
        from rem_card.services.order_service import CVP_QUICK_ORDER_KEY, OrderService

        model_has_cvp = self.model is not None and any(
            str(getattr(order, "drug_key", "") or "") == CVP_QUICK_ORDER_KEY
            or OrderService._is_cvp_order_text(getattr(order, "latin", ""))
            or OrderService._is_cvp_order_text(getattr(order, "_order_text", ""))
            for order in self.model.orders
            if order is not None
        )
        if model_has_cvp:
            return True
        if self._draft_baseline_snapshot is not None:
            return False
        checker = getattr(self.service, "has_cvp_order", None)
        if not callable(checker) or not self.admission_id or not self.shift_date:
            return False
        try:
            return bool(checker(self.admission_id, self.shift_date))
        except Exception as exc:
            logger.warning("CVP order fallback probe failed: %s", exc)
            return False

    def add_cvp_order_if_missing(self):
        if self._is_read_only() or not self.service or not self.admission_id or not self.shift_date:
            return None, False
        from rem_card.services.order_service import CVP_QUICK_ORDER_KEY, CVP_QUICK_ORDER_TEXT, OrderService

        self._ensure_model_initialized()
        if self.model is not None:
            existing = next(
                (
                    order
                    for order in self.model.orders
                    if order is not None
                    and (
                        str(getattr(order, "drug_key", "") or "") == CVP_QUICK_ORDER_KEY
                        or OrderService._is_cvp_order_text(getattr(order, "latin", ""))
                        or OrderService._is_cvp_order_text(getattr(order, "_order_text", ""))
                    )
                ),
                None,
            )
            if existing is not None:
                return existing, False
        if self._draft_baseline_snapshot is None and self.has_cvp_order():
            return None, False

        now = datetime.now()
        start, end = self.service.get_day_period(self.shift_date)
        created_at = now if start <= now < end else start
        order = OrderDTO(
            id=self._allocate_local_order_id(),
            admission_id=int(self.admission_id),
            drug_key=CVP_QUICK_ORDER_KEY,
            latin=CVP_QUICK_ORDER_TEXT,
            type=OrderType.MEDICATION,
            status=OrderStatus.ACTIVE,
            dose_value=0.0,
            dose_unit="",
            frequency=1,
            duration_min=0,
            is_committed=0,
            created_at=created_at,
            last_modified_by="doctor",
        )
        self._insert_local_order_after_add(order)
        return order, True

    def _build_order_edit_dialog(self, order: OrderDTO):
        from rem_card.ui.doctor_view.administration_dialog import (
            DrugCharacteristicsDialog,
            ManualEntryDialog,
            MultiCompCharacteristicsDialog,
        )
        from rem_card.services.prescription_engine import engine

        engine.reload_if_changed(force_check=True)
        drug_key = str(getattr(order, "drug_key", "") or "").strip()
        drug_data = engine.drugs.get(drug_key, {}) if drug_key else {}
        if drug_data.get("is_multicomp"):
            dialog = MultiCompCharacteristicsDialog(drug_key, parent=self, initial_order=order)
            dialog.title_bar.title_label.setText("Редактирование назначения")
            return dialog
        if drug_data and drug_key.lower() not in ("ruchnoivvod", "ruki"):
            return DrugCharacteristicsDialog(
                drug_key,
                initial_dose=getattr(order, "dose_value", None),
                parent=self,
                initial_order=order,
            )

        dialog = ManualEntryDialog(self, title="Редактирование назначения", initial_order=order)
        if drug_key:
            dialog.drug_key = drug_key
        return dialog

    def _open_order_edit_dialog(self, index):
        if self._is_read_only() or not index.isValid() or index.column() != 0 or not self.model:
            return
        row = index.row()
        if row < 0 or row >= len(self.model.orders):
            return
        order = self.model.orders[row]
        if not order or getattr(order, "_pending_delete", False):
            return
        order_id = getattr(order, "id", None)
        if order_id is None:
            self._show_warning("Назначение еще не сохранено. Обновите список и повторите редактирование.")
            return
        if int(order_id) > 0 and self._order_has_committed_execution(order_id):
            self._show_warning(
                "Назначение уже имеет выполненные введения. Чтобы не изменить медицинский факт, "
                "создайте новое назначение и скорректируйте только будущие ячейки."
            )
            return

        dialog = self._build_order_edit_dialog(order)
        if not dialog.exec():
            return

        from rem_card.ui.doctor_view.components.order_input_handler import OrderInputHandler

        edited_order = OrderInputHandler.parse_input_to_dto(dialog.result_text, self.admission_id)
        edited_order.id = int(order_id)
        edited_order.is_committed = 0
        edited_order.status = OrderStatus.ACTIVE
        edited_order.sort_order = getattr(order, "sort_order", 0) or 0
        edited_order.draft_sort_order = getattr(order, "draft_sort_order", None)
        edited_order.created_at = getattr(order, "created_at", None) or datetime.now()
        edited_order.revision = int(getattr(order, "revision", 0) or 0)
        edited_order.last_modified_by = "doctor"
        self._replace_local_order_after_edit(row, int(order_id), edited_order)
